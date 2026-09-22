"""Core document analysis pipeline.

This module orchestrates the full assessment flow for one document — which
may be a photo, a PDF (native or scanned), a .txt, or a .docx. Everything is
normalised into a ``common.documents.Document`` (pages that carry an image, a
text layer, or both) before any criterion runs, so nothing below this line
branches on the uploaded file type.

  parse_criteria()          — deserialise the criteria JSON string from a
                              multipart form field into CriterionInput objects
                              and reject bad text patterns early.

  validate_content_type()   — allowlist check on the declared upload type.
                              Advisory only: the magic-byte detection inside
                              common.documents.detect has the final say.
  _validate_image_dimensions() — reject page images that are too small to assess.
  _decode_image_bgr()       — decode raw image bytes → BGR numpy array, applying
                              EXIF orientation correction via PIL. Injected into
                              the document loader as its image decoder.
  load_document_bytes()     — raw bytes → Document (kind detection + per-kind
                              loading, page cap, PDF render DPI).
  _load_bytes_from_input()  — fetch raw bytes from base64 or URL (SSRF-checked).
  get_ocr_engine()          — lazily build the configured OCR engine (or None).

  analyze_document()        — the central pipeline:
                                1. Validate the first page image's dimensions
                                2. Decide whether OCR is needed, and run it
                                3. Resize page images to ≤1000px
                                4. Run CV detectors for type="cv" criteria
                                   (worst page wins on a multi-page document)
                                5. Run deterministic text matching for
                                   type="text" criteria
                                6. One LLM call for type="llm" criteria, with
                                   ONE page image plus the extracted text
                                7. Merge, clamp, resolve dependencies, weight
                                8. Build the result dict (image_info +
                                   document_info)

  analyze_upload()          — thin wrapper for multipart UploadFile inputs.
  analyze_input()           — thin wrapper for DocumentInput (JSON body) inputs.
  resolve_example()         — return a pre-generated analysis or analyse live;
                              used in /assess/compare to avoid redundant calls.

One-image-per-prompt constraint: the vision model (muse-glimmer) is served
WITHOUT ``--limit-mm-per-prompt``, so vLLM accepts at most ONE image per
request. A 12-page PDF therefore contributes exactly one page image to the
prompt (plus all of its text) — see the comment in ``_build_llm_inputs``.

Process flow position: called by runners.py (run_assess, run_compare) after
the job is dequeued.
"""

import asyncio
import base64
import io
import json

import httpx
import numpy as np
from fastapi import HTTPException, UploadFile
from PIL import Image, ImageOps

from common.documents import (
    Document,
    InvalidPatternError,
    RapidOCREngine,
    UnsupportedDocumentError,
    apply_ocr,
    load_document,
    match_text,
)

from config import (
    ACCEPTED_CONTENT_TYPES,
    DOC_MAX_PAGES,
    FUZZY_CREDIT_FLOOR,
    HTTP_CONNECT_TIMEOUT,
    HTTP_TIMEOUT,
    MAX_WORKING_DIMENSION,
    MIN_IMAGE_HEIGHT,
    MIN_IMAGE_WIDTH,
    OCR_ENGINE,
    OCR_MIN_NATIVE_CHARS,
    PDF_RENDER_DPI,
    TEXT_CHAR_BUDGET,
)
from logger import logger
from models import CriterionInput, DocumentInput, ExampleInput
from cv import get_detector
from llm import encode_image_to_base64, build_llm_prompt, call_vllm, validate_and_clamp
from ssrf import validate_url
from utils import verdict_from_score as _verdict_from_score

_http_timeout = httpx.Timeout(HTTP_TIMEOUT, connect=HTTP_CONNECT_TIMEOUT)

# ACCEPTED_CONTENT_TYPES, MAX_WORKING_DIMENSION and FUZZY_CREDIT_FLOOR live in
# config.py (§ Document analysis constants) with the rest of the tunables.

# Process-wide OCR engine. Built on first use because loading three ONNX
# models costs ~1s and a deployment that only scores native PDFs never needs
# it. ``False`` means "already tried and unavailable" so we don't retry the
# import on every job.
_ocr_engine = None


# ---------------------------------------------------------------------------
# Criteria parsing
# ---------------------------------------------------------------------------


def criterion_pattern(c: CriterionInput) -> str:
    """The text a type="text" criterion searches for.

    ``pattern`` when supplied, otherwise the criterion's own name — so
    ``{"name": "Notice to Owner", "type": "text"}`` needs no second field.
    """
    return c.pattern if c.pattern else c.name


def validate_text_criteria(criteria: list[CriterionInput]) -> None:
    """Reject malformed text patterns at submit time rather than at job time.

    A bad regular expression is a client error: catching it here turns it into
    a 400 on the POST instead of a job that fails minutes later in a worker.
    Validation is done by running the matcher's own compile path against an
    empty document, so the rules can never drift from match_text().

    Args:
        criteria: The full criteria list (non-text entries are ignored).

    Raises:
        HTTPException(400): Pattern empty, too long, or an invalid regex.
    """
    empty = Document(kind="txt", pages=[])
    for c in criteria:
        if c.type != "text":
            continue
        try:
            match_text(
                empty,
                criterion_pattern(c),
                c.match,
                case_sensitive=c.case_sensitive,
                fuzzy_threshold=c.fuzzy_threshold,
                min_count=c.min_count,
            )
        except InvalidPatternError as exc:
            logger.error("validate_text_criteria: '%s' rejected: %s", c.name, exc)
            raise HTTPException(
                status_code=400,
                detail=f"Invalid pattern for text criterion '{c.name}': {exc}",
            )


def parse_criteria(raw: str) -> list[CriterionInput]:
    """Parse and validate the criteria JSON string from a multipart form field.

    The /assess endpoint receives criteria as a JSON string (multipart forms
    cannot carry structured objects natively).  This function converts it into
    a typed list of CriterionInput objects and pre-validates any text patterns.

    Args:
        raw: JSON string, e.g. '[{"name":"sharpness","type":"cv","weight":1.0}]'

    Returns:
        List of validated CriterionInput objects.

    Raises:
        HTTPException(400): If the string is not valid JSON, not a list,
            contains items that fail CriterionInput validation, or carries an
            unusable text pattern.
    """
    logger.info("parse_criteria: raw=%s", raw[:200])
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            raise ValueError("criteria must be a JSON array")
        result = [CriterionInput(**item) for item in parsed]
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.error("parse_criteria: failed to parse criteria: %s", exc)
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid criteria JSON: {exc}. "
                "Expected a JSON array of objects, e.g. "
                '[{"name": "image sharpness", "type": "llm", "hint": "quality"}]'
            ),
        )
    validate_text_criteria(result)
    logger.info(
        "parse_criteria: returning %d criteria: %s",
        len(result),
        [f"{c.name}({c.type})" for c in result],
    )
    return result


# ---------------------------------------------------------------------------
# Document loading helpers
# ---------------------------------------------------------------------------


def validate_content_type(content_type: str | None) -> None:
    """Reject a declared upload type that is obviously not a document.

    Advisory only — the bytes are re-checked by ``detect_kind`` during load,
    which is what actually decides the kind. This exists so an obviously wrong
    upload (a video, say) is refused before its bytes are read into memory and
    queued.

    Args:
        content_type: The multipart part's Content-Type, if any.

    Raises:
        HTTPException(400): If the type is present and not in the allowlist.
    """
    if content_type and content_type.split(";")[0].strip().lower() not in ACCEPTED_CONTENT_TYPES:
        logger.warning("validate_content_type: rejected content_type=%s", content_type)
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unsupported content type '{content_type}'. Accepted: JPEG/PNG images, "
                "PDF, plain text, and .docx (see GET /document-kinds)."
            ),
        )


def _validate_image_dimensions(w: int, h: int) -> None:
    """Reject page images that are too small to produce meaningful assessments.

    Images below MIN_IMAGE_WIDTH × MIN_IMAGE_HEIGHT pixels cannot provide
    enough detail for reliable LLM scoring and are refused early.

    Args:
        w, h: Image width and height in pixels.

    Raises:
        HTTPException(400): If either dimension is below the configured minimum.
    """
    logger.debug(
        "_validate_image_dimensions: w=%d h=%d (min %dx%d)",
        w,
        h,
        MIN_IMAGE_WIDTH,
        MIN_IMAGE_HEIGHT,
    )
    if w < MIN_IMAGE_WIDTH or h < MIN_IMAGE_HEIGHT:
        logger.warning("_validate_image_dimensions: image too small (%dx%d)", w, h)
        raise HTTPException(
            status_code=400,
            detail=f"Image too small ({w}×{h} px). Minimum is {MIN_IMAGE_WIDTH}×{MIN_IMAGE_HEIGHT} px.",
        )
    logger.debug("_validate_image_dimensions: dimensions valid")


def _decode_image_bgr(raw: bytes):
    """Decode raw image bytes to a BGR numpy array suitable for OpenCV.

    Passed to ``load_document`` as its image decoder, so every image page in
    the system goes through the same path:

      1. Open with PIL and apply EXIF orientation correction.
         Phone cameras embed orientation metadata; without this step a portrait
         photo may load sideways, producing wrong CV scores.
      2. Convert the PIL RGB array to OpenCV BGR format.
      3. Fall back to direct cv2.imdecode() if PIL fails for any reason.

    Magic-byte validation happens in common.documents.detect before this is
    reached, so there is no format check here.

    Args:
        raw: Raw JPEG or PNG file bytes.

    Returns:
        BGR numpy array (H×W×3).

    Raises:
        HTTPException(400): If decoding fails entirely.
    """
    import cv2

    logger.debug("_decode_image_bgr: decoding %d bytes", len(raw))

    try:
        # PIL handles EXIF orientation (cv2 does not)
        pil_img = Image.open(io.BytesIO(raw))
        pil_img = ImageOps.exif_transpose(pil_img)  # rotate to match camera orientation
        rgb = np.array(pil_img.convert("RGB"))
        bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        logger.debug(
            "_decode_image_bgr: PIL decode + EXIF correction succeeded shape=%s", bgr.shape
        )
    except Exception as exc:
        # PIL failed; fall back to cv2 (no EXIF correction)
        logger.warning(
            "_decode_image_bgr: PIL EXIF correction failed (%s), falling back to cv2", exc
        )
        nparr = np.frombuffer(raw, np.uint8)
        bgr = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

    if bgr is None:
        logger.error("_decode_image_bgr: failed to decode image from %d bytes", len(raw))
        raise HTTPException(status_code=400, detail="Failed to decode image.")

    logger.debug("_decode_image_bgr: returning image shape=%s", bgr.shape)
    return bgr


def load_document_bytes(
    raw: bytes, filename: str | None = None, content_type: str | None = None
) -> Document:
    """Turn uploaded bytes into a Document, or fail with a 400.

    Kind detection is by magic bytes (``common.documents.detect_kind``), so a
    PDF uploaded as ``image/png`` still loads as a PDF and a legacy ``.doc``
    is rejected with an actionable message.

    Args:
        raw:          Complete file bytes.
        filename:     Original filename, recorded on the Document.
        content_type: Declared MIME type (error messages only).

    Returns:
        A ``Document`` with at least one page.

    Raises:
        HTTPException(400): Unsupported or unparseable bytes.
    """
    logger.debug(
        "load_document_bytes: %d bytes filename=%s content_type=%s",
        len(raw),
        filename,
        content_type,
    )
    try:
        doc = load_document(
            raw,
            filename=filename,
            content_type=content_type,
            max_pages=DOC_MAX_PAGES,
            render_dpi=PDF_RENDER_DPI,
            image_decoder=_decode_image_bgr,
        )
    except UnsupportedDocumentError as exc:
        logger.warning("load_document_bytes: rejected %s: %s", filename, exc)
        raise HTTPException(status_code=400, detail=str(exc))

    logger.info(
        "load_document_bytes: kind=%s pages=%d truncated=%d has_text=%s has_images=%s",
        doc.kind,
        len(doc.pages),
        doc.truncated_pages,
        doc.has_text(),
        doc.has_images(),
    )
    return doc


async def _load_bytes_from_input(data: str, type_: str) -> bytes:
    """Fetch the raw document bytes from a base64 string or a remote URL.

    For URL inputs: performs an SSRF check before fetching (ssrf.validate_url)
    and sets a descriptive User-Agent to avoid 403 responses from servers that
    block default request libraries.

    Args:
        data:  Base64 string or URL string.
        type_: "base64" or "url".

    Returns:
        Raw file bytes (format is determined later, from the bytes themselves).

    Raises:
        HTTPException(400): Invalid base64 data or SSRF-blocked URL.
        HTTPException(502): HTTP error while fetching the URL.
    """
    data_repr = data[:80] if type_ == "url" else f"base64[{len(data)} chars]"
    logger.debug("_load_bytes_from_input: type=%s data=%s", type_, data_repr)

    if type_ == "base64":
        # Decode the base64 payload directly — no network call needed
        try:
            raw = base64.b64decode(data)
        except Exception as exc:
            logger.error("_load_bytes_from_input: invalid base64 data: %s", exc)
            raise HTTPException(status_code=400, detail=f"Invalid base64 data: {exc}")
    else:
        # SSRF check must pass before we fetch anything
        validate_url(data)
        try:
            async with httpx.AsyncClient(timeout=_http_timeout) as client:
                r = await client.get(data, headers={"User-Agent": "Classifier/1.0"})
                r.raise_for_status()
                raw = r.content
            logger.debug("_load_bytes_from_input: fetched %d bytes from URL", len(raw))
        except httpx.HTTPError as exc:
            logger.error(
                "_load_bytes_from_input: failed to fetch URL '%s': %s", data[:80], exc
            )
            raise HTTPException(
                status_code=502, detail=f"Failed to fetch document URL: {exc}"
            )

    if not raw:
        raise HTTPException(status_code=400, detail="Document input was empty.")
    return raw


# ---------------------------------------------------------------------------
# OCR engine
# ---------------------------------------------------------------------------


def get_ocr_engine():
    """Return the configured OCR engine, or None when OCR is unavailable.

    Built once per process and cached. Returns None when
    CLASSIFIER_OCR_ENGINE=none (deliberately disabled) or when the engine's
    dependencies are missing — in that case text criteria on scans fail with
    a clear reason rather than the whole job erroring.

    Returns:
        An object satisfying ``common.documents.OCREngine``, or None.
    """
    global _ocr_engine

    if _ocr_engine is False:
        return None
    if _ocr_engine is not None:
        return _ocr_engine

    if OCR_ENGINE in ("", "none", "off", "disabled"):
        logger.info("get_ocr_engine: OCR disabled (CLASSIFIER_OCR_ENGINE=%s)", OCR_ENGINE)
        _ocr_engine = False
        return None
    if OCR_ENGINE != "rapidocr":
        logger.warning(
            "get_ocr_engine: unknown CLASSIFIER_OCR_ENGINE=%s — OCR disabled "
            "(supported: rapidocr | none)",
            OCR_ENGINE,
        )
        _ocr_engine = False
        return None

    try:
        engine = RapidOCREngine()
        # Touch the underlying engine now so a missing dependency surfaces
        # here (once, at first use) rather than mid-analysis.
        _ = engine.engine
    except Exception as exc:
        logger.error("get_ocr_engine: RapidOCR unavailable (%s) — OCR disabled", exc)
        _ocr_engine = False
        return None

    logger.info("get_ocr_engine: RapidOCR ready")
    _ocr_engine = engine
    return engine


def ocr_engine_status() -> dict:
    """Small introspection blob for GET /document-kinds and document_info."""
    return {
        "engine": OCR_ENGINE or "none",
        "available": get_ocr_engine() is not None,
        "min_native_chars": OCR_MIN_NATIVE_CHARS,
    }


# ---------------------------------------------------------------------------
# Dependency resolution
# ---------------------------------------------------------------------------

def apply_dependencies(assessment: dict, criteria: list[CriterionInput]) -> dict:
    """Mark dependent criteria SKIPPED if their dependency did not PASS.

    Called after validate_and_clamp() but before compute_weighted_score() so
    that skipped criteria are excluded from the weighted calculation.

    A criterion is skipped when its depends_on target has any verdict other
    than PASS — including FAIL, MARGINAL, or SKIPPED (propagating chains).
    Skipped criteria receive verdict="SKIPPED", score=None, and contribute
    zero weight to the overall score.

    Multiple passes are run until no further changes occur, which correctly
    resolves dependency chains of arbitrary depth (A → B → C).

    Args:
        assessment: Clamped assessment dict containing per_criterion_scores.
        criteria:   Full criteria list — only entries with depends_on are checked.

    Returns:
        The same assessment dict with any dependent criteria marked SKIPPED.
    """
    per_criterion = assessment.get("per_criterion_scores", {})

    # Quick exit if no criteria have dependencies
    if not any(c.depends_on for c in criteria):
        return assessment

    # Map criterion name → depends_on name for fast lookup
    dependency_map = {c.name: c.depends_on for c in criteria if c.depends_on}

    # Multi-pass to resolve chains: keep iterating until nothing changes
    changed = True
    while changed:
        changed = False
        for criterion_name, depends_on_name in dependency_map.items():
            if criterion_name not in per_criterion:
                continue

            current = per_criterion[criterion_name]

            # Already skipped — nothing to do
            if isinstance(current, dict) and current.get("verdict") == "SKIPPED":
                continue

            # Check the dependency's verdict
            dep_result = per_criterion.get(depends_on_name, {})
            dep_verdict = dep_result.get("verdict", "FAIL") if isinstance(dep_result, dict) else "FAIL"

            if dep_verdict != "PASS":
                logger.info(
                    "apply_dependencies: skipping '%s' — dependency '%s' verdict=%s",
                    criterion_name, depends_on_name, dep_verdict,
                )
                per_criterion[criterion_name] = {
                    "verdict":    "SKIPPED",
                    "score":      None,
                    "confidence": None,
                    "reason":     (
                        f"Skipped - dependency '{depends_on_name}' "
                        f"did not pass (verdict: {dep_verdict})."
                    ),
                    "method":     "skipped",
                }
                changed = True

    assessment["per_criterion_scores"] = per_criterion
    return assessment


# ---------------------------------------------------------------------------
# Weighted score calculation
# ---------------------------------------------------------------------------


def compute_weighted_score(assessment: dict, criteria: list[CriterionInput]) -> dict:
    """Compute the weighted overall score and attach the breakdown to the assessment.

    Called only on combined_assessment after validate_and_clamp() has already
    clamped all per-criterion scores.  Overwrites overall_score and
    overall_verdict with the weighted result and adds weighted_score_breakdown.

    Args:
        assessment: Clamped assessment dict containing per_criterion_scores.
        criteria:   Full criteria list providing each criterion's weight.

    Returns:
        The same assessment dict with overall_score, overall_verdict, and
        weighted_score_breakdown updated in place.
    """
    per_criterion = assessment.get("per_criterion_scores", {})
    matched = [
        (c, per_criterion[c.name])
        for c in criteria
        if c.name in per_criterion
        and isinstance(per_criterion[c.name], dict)
        and per_criterion[c.name].get("verdict") != "SKIPPED"
    ]

    if not matched:
        logger.warning("compute_weighted_score: no matched criteria — skipping")
        return assessment

    total_weight = sum(c.weight for c, _ in matched)
    if total_weight == 0:
        logger.warning("compute_weighted_score: total_weight is 0 — skipping")
        return assessment

    weighted_sum = sum(val["score"] * c.weight for c, val in matched)
    unrounded = weighted_sum / total_weight
    weighted_score = max(1, min(10, round(unrounded)))

    assessment["overall_score"] = weighted_score
    assessment["overall_verdict"] = _verdict_from_score(weighted_score)
    assessment["weighted_score_breakdown"] = {
        "formula": "sum(score * weight) / total_weight",
        "total_weight": round(total_weight, 4),
        "weighted_sum": round(weighted_sum, 4),
        "unrounded_average": round(unrounded, 4),
        "final_score": weighted_score,
        "per_criterion": {
            c.name: {
                "score": val["score"],
                "weight": c.weight,
                "contribution": round(val["score"] * c.weight, 4),
            }
            for c, val in matched
        },
    }
    logger.info(
        "compute_weighted_score: final_score=%s weights=%s",
        weighted_score,
        {c.name: c.weight for c, _ in matched},
    )
    return assessment


# ---------------------------------------------------------------------------
# Per-criterion evaluation helpers
# ---------------------------------------------------------------------------


def _skipped_result(reason: str) -> dict:
    """The SKIPPED shape used by apply_dependencies, reused for criteria that
    cannot be evaluated at all (e.g. a cv criterion on a .docx).

    SKIPPED criteria carry no score and are excluded from the weighted average
    entirely — which is the right answer for "not applicable", as opposed to
    FAIL, which would drag the document's score down for the wrong reason.
    """
    return {
        "verdict":    "SKIPPED",
        "score":      None,
        "confidence": None,
        "reason":     reason,
        "method":     "skipped",
    }


def _run_cv_criterion(detector, name: str, page_images: list[tuple[int, object]]) -> dict:
    """Run one CV detector across every page image; the worst page wins.

    A multi-page document is only as sharp / well-exposed as its worst page —
    one blurred page of a five-page contract is still an unusable document —
    so the minimum score is reported, with every page's measurement kept in a
    ``pages`` list for the caller to inspect.

    Args:
        detector:    The detector callable from cv.get_detector().
        name:        Criterion name (for logging).
        page_images: ``[(page_index, bgr_image), ...]``, already resized.

    Returns:
        A standard CV result dict, plus ``pages`` and ``page`` (the index of
        the page the reported score came from).
    """
    per_page: list[dict] = []
    for index, image in page_images:
        result = dict(detector(image))
        result["page"] = index
        per_page.append(result)

    worst = min(per_page, key=lambda r: r.get("score", 10))
    merged = dict(worst)
    merged["method"] = "cv"
    if len(per_page) > 1:
        merged["pages"] = [
            {
                "index": r["page"],
                "score": r.get("score"),
                "verdict": r.get("verdict"),
                "detail": r.get("detail"),
            }
            for r in per_page
        ]
        merged["detail"] = (
            f"{worst.get('detail', '')} "
            f"(worst of {len(per_page)} pages — page {worst['page']})"
        ).strip()
    logger.debug(
        "_run_cv_criterion: '%s' pages=%d worst_page=%s score=%s",
        name,
        len(per_page),
        worst.get("page"),
        merged.get("score"),
    )
    return merged


def _text_confidence(doc: Document, pages: list[int]) -> int:
    """Confidence 0-100 for a text criterion result.

    Native text is exact, so it scores 100. OCR'd text is only as trustworthy
    as the recogniser said it was, so its mean confidence is carried through.
    A document with no text layer at all scores 0.

    Args:
        doc:   The document that was searched.
        pages: Page indices the match landed on ([] when nothing matched — the
               whole document's text pages are used instead).
    """
    considered = [p for p in doc.pages if p.text.strip()]
    if pages:
        considered = [p for p in considered if p.index in pages]
    if not considered:
        return 0
    if all(p.text_source == "native" for p in considered):
        return 100
    ocr_confs = [p.ocr_confidence or 0.0 for p in considered if p.text_source == "ocr"]
    if not ocr_confs:
        return 100
    return int(round(min(ocr_confs) * 100))


def _evaluate_text_criterion(doc: Document, c: CriterionInput) -> dict:
    """Score one type="text" criterion against the document's text layer.

    Scoring:
      * found                    → 10 (PASS)
      * fuzzy, best_ratio < threshold → 1-6, scaled by how close it got, so a
        near miss is distinguishable from "not there at all"
      * not found                → 1 (FAIL)
      * document has no text     → 1 (FAIL) with a reason naming the cause
        (ocr=never, or the engine is disabled/unavailable)

    Args:
        doc: Loaded (and possibly OCR'd) document.
        c:   The criterion; ``pattern`` defaults to ``name``.

    Returns:
        A per-criterion result dict with method="text" and a structured
        ``detail`` carrying the match counts, pages, and snippets.
    """
    pattern = criterion_pattern(c)

    if not doc.has_text():
        logger.info("_evaluate_text_criterion: '%s' — document has no text layer", c.name)
        return {
            "score": 1,
            "verdict": "FAIL",
            "confidence": 0,
            "method": "text",
            "reason": (
                "No text available for this document (no native text layer and OCR did "
                "not run — ocr=never, or the OCR engine is disabled/unavailable). "
                "Re-submit with ocr=auto or always."
            ),
            "detail": {"pattern": pattern, "match": c.match, "text_source": "none"},
        }

    try:
        res = match_text(
            doc,
            pattern,
            c.match,
            case_sensitive=c.case_sensitive,
            fuzzy_threshold=c.fuzzy_threshold,
            min_count=c.min_count,
        )
    except InvalidPatternError as exc:
        # parse_criteria normally catches this at submit time; a compare job
        # built from a stored payload can still reach here.
        logger.error("_evaluate_text_criterion: '%s' invalid pattern: %s", c.name, exc)
        return {
            "score": 1,
            "verdict": "FAIL",
            "confidence": 0,
            "method": "text",
            "reason": f"Invalid pattern: {exc}",
            "detail": {"pattern": pattern, "match": c.match},
        }

    if res.found:
        score = 10
        reason = (
            f"Found {res.count}× on page(s) {res.pages} via {c.match} match"
            + (f" (best ratio {res.best_ratio:.2f})" if c.match == "fuzzy" else "")
            + "."
        )
    elif c.match == "fuzzy" and res.best_ratio >= FUZZY_CREDIT_FLOOR:
        # Scale the near miss into 2-6 so "almost there" outranks "absent".
        # Below FUZZY_CREDIT_FLOOR there is no credit at all: difflib gives any
        # unrelated pair of phrases ~0.3-0.5, so anything less is noise, not a
        # near miss.
        span = max(1e-6, c.fuzzy_threshold - FUZZY_CREDIT_FLOOR)
        closeness = min(1.0, (res.best_ratio - FUZZY_CREDIT_FLOOR) / span)
        score = max(1, min(6, int(round(1 + 5 * closeness))))
        reason = (
            f"Best fuzzy match scored {res.best_ratio:.2f}, below the "
            f"{c.fuzzy_threshold:.2f} threshold."
        )
    else:
        score = 1
        reason = (
            f"'{pattern}' not found in {res.searched_chars} characters of document text "
            f"({c.match} match, min_count={c.min_count})."
        )

    sources = doc.text_sources()
    result = {
        "score": score,
        "verdict": _verdict_from_score(score),
        "confidence": _text_confidence(doc, res.pages),
        "method": "text",
        "reason": reason,
        "detail": {
            **res.as_dict(),
            "case_sensitive": c.case_sensitive,
            "min_count": c.min_count,
            "fuzzy_threshold": c.fuzzy_threshold if c.match == "fuzzy" else None,
            "text_source": "+".join(sorted(sources)) if sources else "none",
        },
    }
    logger.info(
        "_evaluate_text_criterion: '%s' pattern=%r match=%s found=%s count=%d score=%d",
        c.name,
        pattern,
        c.match,
        res.found,
        res.count,
        score,
    )
    return result


# ---------------------------------------------------------------------------
# Document preparation
# ---------------------------------------------------------------------------


def _needs_ocr(doc: Document, criteria: list[CriterionInput], ocr_mode: str) -> bool:
    """Decide whether this job should spend time on OCR.

    ``always`` — yes, whenever there is a page image at all.
    ``never``  — no.
    ``auto``   — yes when text is both WANTED and MISSING:
                   * wanted: any text criterion (it has nothing to search
                     otherwise), or any llm criterion on a document whose
                     pages have no text — a scan the model reads far better
                     with the recognised text alongside it, and
                   * missing: at least one page carries an image whose text
                     layer is under OCR_MIN_NATIVE_CHARS.
                 A native PDF, a .txt, or a .docx therefore never pays for
                 loading the OCR models.
    """
    if ocr_mode == "never":
        return False
    if not doc.has_images():
        return False  # nothing to recognise: txt/docx have no rendered surface
    if ocr_mode == "always":
        return True

    pages_missing_text = any(
        p.image_bgr is not None and p.text_chars() < OCR_MIN_NATIVE_CHARS
        for p in doc.pages
    )
    if not pages_missing_text:
        return False
    return any(c.type in ("text", "llm") for c in criteria)


def _resized_page_images(doc: Document) -> list[tuple[int, object]]:
    """Every page image, resized to ≤MAX_WORKING_DIMENSION on the long side.

    Done once and shared by the CV detectors and the LLM prompt so both see
    exactly the same pixels (and so the CV thresholds keep the meaning they
    were tuned with on single images).
    """
    import cv2

    working: list[tuple[int, object]] = []
    for index, image in doc.page_images():
        h, w = image.shape[:2]
        if max(h, w) > MAX_WORKING_DIMENSION:
            scale = MAX_WORKING_DIMENSION / max(h, w)
            image = cv2.resize(
                image, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA
            )
            logger.debug("_resized_page_images: page %d resized to %s", index, image.shape)
        working.append((index, image))
    return working


def _document_text_for_prompt(doc: Document) -> tuple[str, bool]:
    """Extracted text for the LLM prompt, truncated to the configured budget.

    Returns:
        ``(text, truncated)`` — ``text`` is "" when the document has none.
    """
    text = doc.full_text()
    if TEXT_CHAR_BUDGET and len(text) > TEXT_CHAR_BUDGET:
        logger.info(
            "_document_text_for_prompt: truncating %d chars to the %d-char budget",
            len(text),
            TEXT_CHAR_BUDGET,
        )
        return text[:TEXT_CHAR_BUDGET], True
    return text, False


def _pick_prompt_page(
    working_images: list[tuple[int, object]], text_results: dict
) -> int | None:
    """Choose the ONE page image the vision model gets to see.

    The vision model (muse-glimmer) is served WITHOUT --limit-mm-per-prompt,
    so vLLM accepts at most one image per request — sending more fails the
    whole call. Until that flag is set (Phase 2), one page has to represent
    the document, so we pick the most informative one:

      1. The first page a text criterion actually matched on — if the caller
         asked about "Notice to Owner" and it's on page 3, page 3 is the page
         worth looking at.
      2. Otherwise page 0, the cover/first page.

    Args:
        working_images: ``[(page_index, image), ...]``; empty for txt/docx.
        text_results:   Per-criterion text results, to read matched pages from.

    Returns:
        The chosen page index, or None when the document has no images.
    """
    if not working_images:
        return None
    available = {index for index, _ in working_images}
    for result in text_results.values():
        detail = result.get("detail")
        if isinstance(detail, dict):
            for page_index in detail.get("pages", []) or []:
                if page_index in available:
                    return page_index
    return working_images[0][0]


# ---------------------------------------------------------------------------
# Core analysis pipeline
# ---------------------------------------------------------------------------


async def analyze_document(
    doc: Document,
    criteria: list[CriterionInput],
    ocr_mode: str = "auto",
) -> dict:
    """Run the full assessment pipeline on a loaded Document.

    This is the central function that all entry points (analyze_upload,
    analyze_input) ultimately call.

    Pipeline steps:
      1. Validate the first page image's dimensions (image-bearing kinds only).
      2. Run OCR when it is needed and available (see _needs_ocr).
      3. Resize page images to ≤1000px on the long side.
      4. Run CV detectors for type="cv" criteria — worst page wins; criteria
         are SKIPPED when the document has no page images (txt/docx).
      5. Run deterministic text matching for type="text" criteria.
      6. One LLM call for type="llm" criteria (plus any cv fallbacks), with
         ONE page image and the extracted document text.
      7. Merge all three, clamp, resolve dependencies, compute the weighted score.
      8. Assemble the result dict (image_info kept for compatibility, plus the
         richer document_info).

    Args:
        doc:      Loaded document (from load_document_bytes).
        criteria: List of CriterionInput objects defining what to assess.
        ocr_mode: "auto" | "always" | "never" — see models.CompareRequest.ocr.

    Returns:
        dict with image_info, document_info, assessment, and verdict.
    """
    logger.info(
        "analyze_document: kind=%s pages=%d ocr=%s criteria=%s",
        doc.kind,
        len(doc.pages),
        ocr_mode,
        [f"{c.name}({c.type})" for c in criteria],
    )

    # Step 1 — reject page images that are too small for reliable assessment.
    # Only the first image-bearing page is checked: a document is rejected for
    # being a thumbnail, not for having one small trailing page.
    first_image_page = next((p for p in doc.pages if p.image_bgr is not None), None)
    if first_image_page is not None:
        _validate_image_dimensions(first_image_page.width, first_image_page.height)

    # Step 2 — OCR. Blocking (ONNX inference), so it runs in a worker thread
    # to keep the event loop free for other concurrent jobs.
    # The needs-check comes first so a native PDF / txt / docx never pays the
    # ~1s cost of loading the ONNX models it has no use for.
    ocr_ran = False
    ocr_pages = 0
    if _needs_ocr(doc, criteria, ocr_mode):
        engine = get_ocr_engine()
        if engine is None:
            logger.warning(
                "analyze_document: OCR wanted but unavailable (engine=%s) — text "
                "criteria on image-only pages will fail",
                OCR_ENGINE,
            )
        else:
            effective_mode = "always" if ocr_mode == "always" else "auto"
            logger.info("analyze_document: running OCR (mode=%s)", effective_mode)
            ocr_pages = await asyncio.to_thread(
                apply_ocr,
                doc,
                engine,
                mode=effective_mode,
                min_native_chars=OCR_MIN_NATIVE_CHARS,
            )
            ocr_ran = True
            logger.info("analyze_document: OCR filled %d page(s)", ocr_pages)

    # Step 3 — one resized copy of each page image, shared by CV and the LLM
    working_images = _resized_page_images(doc)

    # Step 4/5/6 — partition criteria by evaluation path.
    # type="cv"   → OpenCV detector (falls back to the LLM if no detector matches)
    # type="text" → deterministic text match, no tokens
    # type="llm"  → the vision model
    cv_criteria = [c for c in criteria if c.type == "cv"]
    text_criteria = [c for c in criteria if c.type == "text"]
    llm_criteria = [c for c in criteria if c.type == "llm"]

    # Step 4 — CV detectors
    _cv_per_criterion: dict = {}
    for c in cv_criteria:
        detector = get_detector(c.name)
        if not detector:
            # No registered detector — fall back to LLM transparently
            logger.warning(
                "analyze_document: no CV detector for '%s', falling back to LLM", c.name
            )
            llm_criteria.append(c)
            continue
        if not working_images:
            # .txt / .docx have no rendered surface — not a failure, just not
            # applicable, so the criterion is excluded from the weighting.
            logger.info(
                "analyze_document: '%s' skipped — document has no page images", c.name
            )
            _cv_per_criterion[c.name] = _skipped_result(
                f"Skipped - document has no page images ({doc.kind} documents are "
                "text-only, so OpenCV criteria cannot be evaluated)."
            )
            continue
        logger.info("analyze_document: CV detector running for '%s'", c.name)
        _cv_per_criterion[c.name] = _run_cv_criterion(detector, c.name, working_images)

    # Compute CV overall verdict and score immediately after all detectors have run,
    # then build cv_assessment with overall_verdict and overall_score first so they
    # appear at the top of the dict. SKIPPED entries contribute to neither.
    scored_cv = {
        k: v for k, v in _cv_per_criterion.items() if v.get("verdict") != "SKIPPED"
    }
    if scored_cv:
        cv_scores = [
            r["score"] for r in scored_cv.values() if isinstance(r.get("score"), (int, float))
        ]
        cv_failures = sum(1 for r in scored_cv.values() if r.get("verdict") == "FAIL")
        cv_verdict = (
            "FAIL" if cv_failures >= 2 else ("MARGINAL" if cv_failures == 1 else "PASS")
        )
        cv_assessment: dict = {
            "overall_verdict": cv_verdict,
            "overall_score": round(sum(cv_scores) / len(cv_scores)) if cv_scores else 5,
            "per_criterion_scores": _cv_per_criterion,
        }
        logger.debug(
            "analyze_document: cv_assessment verdict=%s score=%s criteria=%s",
            cv_assessment["overall_verdict"],
            cv_assessment["overall_score"],
            {k: v["verdict"] for k, v in _cv_per_criterion.items()},
        )
    else:
        cv_assessment: dict = (
            {"overall_verdict": None, "overall_score": None,
             "per_criterion_scores": _cv_per_criterion}
            if _cv_per_criterion
            else {}
        )

    # Step 5 — text criteria (deterministic, no tokens)
    _text_per_criterion: dict = {
        c.name: _evaluate_text_criterion(doc, c) for c in text_criteria
    }

    # Step 6 — one LLM call for all LLM-bound criteria (type="llm" + any cv
    # fallbacks). Skipped entirely when every criterion resolved via CV/text.
    document_text, text_truncated = _document_text_for_prompt(doc)
    prompt_page = _pick_prompt_page(working_images, _text_per_criterion)
    if llm_criteria:
        image_b64 = None
        if prompt_page is not None:
            page_image = next(img for idx, img in working_images if idx == prompt_page)
            image_b64 = encode_image_to_base64(page_image)
        logger.info(
            "analyze_document: LLM call — %d criteria, image=%s, document_text=%d chars%s",
            len(llm_criteria),
            f"page {prompt_page}" if prompt_page is not None else "none (text-only)",
            len(document_text),
            " (truncated)" if text_truncated else "",
        )
        llm_raw = await call_vllm(
            build_llm_prompt(
                image_b64,
                llm_criteria,
                document_text=document_text,
                document_kind=doc.kind,
                page_index=prompt_page,
                page_count=len(doc.pages),
                text_truncated=text_truncated,
            )
        )
        for val in (
            llm_raw.get("assessment", {}).get("per_criterion_scores", {}).values()
        ):
            if isinstance(val, dict):
                val["method"] = "llm"
        # Validate and clamp LLM assessment using only the LLM-bound criteria.
        llm_assessment = validate_and_clamp(llm_raw.get("assessment", {}), llm_criteria)
    else:
        logger.info(
            "analyze_document: all criteria resolved via CV/text — skipping LLM call"
        )
        llm_assessment = {
            "overall_verdict": None,
            "overall_score": None,
            "per_criterion_scores": {},
        }

    # Step 7 — build combined assessment (CV + text + LLM) and compute the
    # weighted overall score across ALL criteria. Each path stays clean in its
    # own keys; combined is the single source for the weighted breakdown.
    combined_raw = {
        "overall_verdict": "...",
        "overall_score": 0,
        "per_criterion_scores": {
            **cv_assessment.get("per_criterion_scores", {}),
            **_text_per_criterion,
            **llm_assessment.get("per_criterion_scores", {}),
        },
    }
    combined_assessment = validate_and_clamp(combined_raw, criteria)
    combined_assessment = apply_dependencies(combined_assessment, criteria)
    combined_assessment = compute_weighted_score(combined_assessment, criteria)

    # Step 8 — assemble the final response dict.
    # combined_verdict is derived directly from the weighted_score_breakdown
    # final_score so the connection between score and verdict is explicit.
    breakdown = combined_assessment.get("weighted_score_breakdown", {})
    combined_verdict = combined_assessment.get("overall_verdict", "PASS")
    logger.debug(
        "analyze_document: combined breakdown final_score=%s → verdict=%s",
        breakdown.get("final_score"),
        combined_verdict,
    )

    # image_info keeps the pre-document shape so existing consumers (and
    # stored pre_generated_analysis blobs) keep working: page 0's image when
    # there is one, zeros otherwise.
    image_info = {
        "width": first_image_page.width if first_image_page else 0,
        "height": first_image_page.height if first_image_page else 0,
        "format": doc.content_type,
        "size_bytes": doc.size_bytes,
    }

    result = {
        "image_info": image_info,
        "document_info": {
            "kind": doc.kind,
            "filename": doc.filename,
            "page_count": len(doc.pages),
            "truncated_pages": doc.truncated_pages,
            "pages": [
                {
                    "index": p.index,
                    "width": p.width,
                    "height": p.height,
                    "text_source": p.text_source,
                    "ocr_confidence": p.ocr_confidence,
                    "text_chars": p.text_chars(),
                }
                for p in doc.pages
            ],
            "ocr": {
                "mode": ocr_mode,
                "engine": OCR_ENGINE or "none",
                "ran": ocr_ran,
                "pages_recognised": ocr_pages,
            },
            "text_sent_to_llm": {
                "chars": len(document_text) if llm_criteria else 0,
                "truncated": text_truncated if llm_criteria else False,
                "budget": TEXT_CHAR_BUDGET,
            },
            "llm_image_page": prompt_page if llm_criteria else None,
        },
        "assessment": combined_assessment,  # combined assessment with all criteria and weighted score
        "verdict": combined_verdict,  # final verdict
    }
    logger.info(
        "analyze_document: returning verdict=%s overall_score=%s",
        result["verdict"],
        combined_assessment.get("overall_score", "n/a"),
    )
    return result


async def analyze_upload(
    upload: UploadFile, criteria: list[CriterionInput], ocr_mode: str = "auto"
) -> dict:
    """Entry point for multipart file uploads (POST /assess via form data).

    Reads the uploaded file, validates the content type, loads it as a
    document, and delegates to analyze_document().

    Args:
        upload:   FastAPI UploadFile from a multipart/form-data request.
        criteria: Parsed list of CriterionInput objects.
        ocr_mode: "auto" | "always" | "never".

    Returns:
        Analysis result dict from analyze_document().
    """
    logger.info(
        "analyze_upload: filename=%s content_type=%s ocr=%s criteria=%s",
        upload.filename,
        upload.content_type,
        ocr_mode,
        [c.name for c in criteria],
    )

    # Validate the declared content type before reading the whole file; the
    # magic bytes decide the actual kind during load.
    validate_content_type(upload.content_type)

    contents = await upload.read()
    if not contents:
        raise HTTPException(status_code=400, detail="Empty document file")

    logger.debug("analyze_upload: read %d bytes", len(contents))
    doc = load_document_bytes(contents, upload.filename, upload.content_type)
    result = await analyze_document(doc, criteria, ocr_mode)
    logger.info(
        "analyze_upload: returning combined_verdict=%s", result["verdict"]
    )
    return result


async def analyze_input(
    img: DocumentInput, criteria: list[CriterionInput], ocr_mode: str = "auto"
) -> dict:
    """Entry point for DocumentInput objects from a JSON request body.

    Loads the bytes from a base64 string or URL, detects the kind, and
    delegates to analyze_document().

    Args:
        img:      DocumentInput with data and type fields.
        criteria: List of CriterionInput objects.
        ocr_mode: "auto" | "always" | "never".

    Returns:
        Analysis result dict from analyze_document().
    """
    data_repr = img.data[:80] if img.type == "url" else f"base64[{len(img.data)} chars]"
    logger.info(
        "analyze_input: type=%s data=%s ocr=%s criteria=%s",
        img.type,
        data_repr,
        ocr_mode,
        [c.name for c in criteria],
    )
    raw = await _load_bytes_from_input(img.data, img.type)
    filename = img.data.rsplit("/", 1)[-1][:120] if img.type == "url" else "inline"
    doc = load_document_bytes(raw, filename, None)
    result = await analyze_document(doc, criteria, ocr_mode)
    logger.info(
        "analyze_input: returning combined_verdict=%s", result["verdict"]
    )
    return result


async def resolve_example(
    example: ExampleInput, criteria: list[CriterionInput], ocr_mode: str = "auto"
) -> dict:
    """Return the analysis for a reference example, live or pre-generated.

    Used in /assess/compare to obtain an analysis for each reference document.
    If pre_generated_analysis is provided, it is returned immediately without
    any LLM call — this is the recommended pattern for stable reference
    documents to avoid redundant token usage.

    Args:
        example:  ExampleInput including the document and optional prior analysis.
        criteria: The criteria to apply if a live analysis is needed.
        ocr_mode: "auto" | "always" | "never".

    Returns:
        Analysis result dict (same shape as analyze_document output).
    """
    pre_generated = example.pre_generated_analysis is not None
    logger.info(
        "resolve_example: type=%s weight=%s pre_generated=%s criteria=%s",
        example.type,
        example.weight,
        pre_generated,
        [c.name for c in criteria],
    )

    if pre_generated:
        # Skip the LLM entirely — use the cached result
        logger.info("resolve_example: using pre-generated analysis, skipping LLM call")
        return example.pre_generated_analysis

    # Analyse live — same path as a regular /assess call
    result = await analyze_input(
        DocumentInput(data=example.data, type=example.type), criteria, ocr_mode
    )
    logger.info(
        "resolve_example: returning combined_verdict=%s", result["verdict"]
    )
    return result
