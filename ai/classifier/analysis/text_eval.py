"""The `text` evaluation path — deterministic search of the text layer.

No tokens, no image: a type="text" criterion is answered by
``common.documents.match_text`` against whatever text the document has, native
or OCR'd. The scoring rubric is the interesting part — a fuzzy near miss
scores 1-6 in proportion to how close it got, so "almost there" is
distinguishable from "not there at all".

    _text_confidence()        — how much to trust the text that was searched.
    _evaluate_text_criterion() — one criterion → a result and its regions.
    _text_regions()           — where the hits landed, from whichever source
                                has geometry (OCR line polygons, or PyMuPDF on
                                a native PDF page).

Process flow position: step 5 of ``analysis.pipeline.analyze_document``.
"""

from common.documents import (
    Document,
    InvalidPatternError,
    match_text,
    pdf_text_regions,
)
from common.vision import PageGeometry, Region

from analysis.criteria import criterion_pattern
from api.schemas import CriterionInput
from config import FUZZY_CREDIT_FLOOR, TEXT_REGION_MAX_HITS
from logger import logger
from utils import verdict_from_score as _verdict_from_score


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


def _evaluate_text_criterion(
    doc: Document, c: CriterionInput, geometries: dict[int, PageGeometry] | None = None
) -> tuple[dict, list[Region]]:
    """Score one type="text" criterion against the document's text layer.

    Scoring:
      * found                    → 10 (PASS)
      * fuzzy, best_ratio < threshold → 1-6, scaled by how close it got, so a
        near miss is distinguishable from "not there at all"
      * not found                → 1 (FAIL)
      * document has no text     → 1 (FAIL) with a reason naming the cause
        (ocr=never, or the engine is disabled/unavailable)

    Args:
        doc:        Loaded (and possibly OCR'd) document.
        c:          The criterion; ``pattern`` defaults to ``name``.
        geometries: ``{page_index: PageGeometry}`` when the request asked for
                    regions; None to skip the (not free) locate pass.

    Returns:
        ``(result, regions)`` — a per-criterion result dict with
        method="text" and a structured ``detail`` carrying the match counts,
        pages and snippets, plus the hits' page geometry in ORIGINAL page
        pixels (empty unless ``geometries`` was supplied).
    """
    pattern = criterion_pattern(c)
    locate = geometries is not None

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
        }, []

    try:
        res = match_text(
            doc,
            pattern,
            c.match,
            case_sensitive=c.case_sensitive,
            fuzzy_threshold=c.fuzzy_threshold,
            min_count=c.min_count,
            locate=locate,
            label=c.name,
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
        }, []

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
    regions = _text_regions(doc, c, res, geometries) if locate else []
    logger.info(
        "_evaluate_text_criterion: '%s' pattern=%r match=%s found=%s count=%d score=%d "
        "regions=%d",
        c.name,
        pattern,
        c.match,
        res.found,
        res.count,
        score,
        len(regions),
    )
    return result, regions


def _text_regions(
    doc: Document,
    c: CriterionInput,
    res,
    geometries: dict[int, PageGeometry],
) -> list[Region]:
    """Where a text criterion's hits landed, from whichever source has geometry.

    Two paths, and a document can use both (a PDF whose page 1 is digital and
    whose page 2 is a scan):

      OCR'd pages      ``match_text(locate=True)`` already mapped the offsets
                       to line polygons; they are in page-image pixels, which
                       is where they belong, so they pass straight through.
      Native PDF pages need the file itself — ``pdf_text_regions`` re-opens
                       ``doc.source_bytes``. Without ``keep_source=True`` at
                       load time there is nothing to re-open and those pages
                       contribute nothing (logged once, not an error).

    .txt / .docx have no geometry at all and yield nothing — their hits are
    still reported in ``detail.snippets``.
    """
    regions: list[Region] = [r for r in res.regions if r.page in geometries]

    native_pages = {
        page.index
        for page in doc.pages
        if page.text_source == "native" and page.index in geometries
    }
    if doc.kind == "pdf" and native_pages:
        if not doc.source_bytes:
            logger.debug(
                "_text_regions: '%s' — native PDF pages %s cannot be localised "
                "(document loaded without keep_source)",
                c.name,
                sorted(native_pages),
            )
        else:
            for page_index in sorted(native_pages):
                page = doc.page(page_index)
                hits = [h for h in res.hits if h.page == page_index]
                if page is None or (not hits and c.match not in ("contains", "exact")):
                    continue
                regions.extend(
                    pdf_text_regions(
                        doc.source_bytes,
                        page,
                        hits,
                        pattern=criterion_pattern(c),
                        mode=c.match,
                        label=c.name,
                        max_regions=TEXT_REGION_MAX_HITS,
                    )
                )
    return regions[: TEXT_REGION_MAX_HITS * max(1, len(geometries))]
