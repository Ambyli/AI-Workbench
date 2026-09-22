"""POST /locate — the same pipeline, with the scoring taken out.

`/assess` answers "how good is this document?" and mentions, if asked, where
it looked. `/locate` answers only "where is X?", which is a different job with
a different cost profile: no weighted score, no verdict, no dependency
resolution. A caller who wants boxes for a UI overlay should not have to
submit criteria, read a verdict, and throw it away.

    parse_features()  — the `features` form field: a JSON array of bare
                        strings, of CriterionInput-shaped objects, or a mix of
                        the two.
    locate_document() — the endpoint: validate, detect the kind, enqueue.

Feature resolution. A bare string has no ``type``, and defaulting it to
``llm`` (the CriterionInput default) would send the common case —
``["has faces", "has sky"]`` — to a vision model that a registered OpenCV
detector answers for free. So a bare string resolves to whichever path can
produce geometry most cheaply:

    1. ``cv``       — a registered OpenCV detector matches the name.
    2. ``detector`` — DETECTOR_URL is configured, so the open-vocabulary
                      service can find anything nameable. This is what makes
                      ``["bicycle"]`` return boxes with no LLM call at all.
    3. ``llm``      — neither. With ``regions.llm_boxes`` the bounding-box
                      enforcement loop runs for it: a presence-only scoring
                      call to have a score to gate on, then ask -> validate ->
                      verify-by-crop -> retry, and the feature comes back with
                      an accepted box (or every rejected attempt) and a
                      ``localization`` record. WITHOUT ``llm_boxes`` no call
                      is made at all and the feature comes back with no
                      regions and a reason saying which option to set.

The result says which path each feature took in its ``method`` field, and
never a score or a verdict — even for the LLM path, whose scoring call
happened only to gate the loop. An object spelled out in full is taken at its
word.

Process flow position: mounted by ``main``; the payload builder is
``jobs.payloads.build_locate_payload`` and the runner is
``jobs.runners.run_locate``, which ``jobs.queue.handle_job`` dispatches to on
``metadata.type == "locate"``.
"""

import json
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from common.documents import UnsupportedDocumentError, detect_kind

from analysis import validate_content_type, validate_text_criteria
from api.assess import _enqueue
from api.schemas import ClassifierMetadata, CriterionInput, parse_regions_option
from cv import get_detector
# Imported as a module, not by name: `detector_client.is_configured()` reads
# DETECTOR_URL at call time, which is what lets a test point it at a stub.
from detector import client as detector_client
from jobs.payloads import build_locate_payload
from jobs.queue import jobs_registry
from logger import logger
from metrics import jobs_total
from middleware import request_id_var

router = APIRouter(tags=["locate"])


def parse_features(raw: str) -> list[CriterionInput]:
    """Parse the `/locate` ``features`` field into criteria.

    Accepts either spelling, per § 5 of the regions plan::

        ["has faces", "has sky"]
        [{"name": "Notice to Owner", "type": "text", "match": "fuzzy"}]
        ["has faces", {"name": "case number", "type": "text", "match": "regex",
                       "pattern": "CASE-\\\\d{5}"}]

    Args:
        raw: The JSON string from the multipart form field.

    Returns:
        Validated CriterionInput objects, bare strings resolved to a ``cv``,
        ``detector`` or ``llm`` path (see the module docstring). A bare string
        that lands on ``llm`` is given ``hint="presence"``: "where is X" is a
        presence question by construction, and the enforcement loop only runs
        for presence/auto criteria.

    Raises:
        HTTPException(400): Not valid JSON, not an array, empty, or an entry
            that fails CriterionInput validation / carries a bad text pattern.
    """
    logger.info("parse_features: raw=%s", raw[:200])
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid features JSON: {exc}. Expected an array of names or of "
                'criterion objects, e.g. ["has faces", {"name": "Notice to Owner", '
                '"type": "text", "match": "fuzzy"}]'
            ),
        )
    if not isinstance(parsed, list) or not parsed:
        raise HTTPException(
            status_code=400, detail="features must be a non-empty JSON array"
        )

    features: list[CriterionInput] = []
    for item in parsed:
        try:
            if isinstance(item, str):
                name = item.strip()
                if not name:
                    raise ValueError("feature name must not be empty")
                # A bare name is only useful if something can localise it.
                if get_detector(name):
                    resolved = "cv"
                elif detector_client.is_configured():
                    resolved = "detector"
                else:
                    resolved = "llm"
                features.append(
                    CriterionInput(name=name, type=resolved, hint="presence")
                    if resolved == "llm"
                    else CriterionInput(name=name, type=resolved)
                )
            elif isinstance(item, dict):
                features.append(CriterionInput(**item))
            else:
                raise ValueError(
                    f"expected a string or an object, got {type(item).__name__}"
                )
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"Invalid feature: {exc}")

    validate_text_criteria(features)
    logger.info(
        "parse_features: returning %d feature(s): %s",
        len(features),
        [f"{f.name}({f.type})" for f in features],
    )
    return features


@router.post("/locate", status_code=202)
async def locate_document(
    file: Optional[UploadFile] = File(
        default=None,
        description=(
            "The document to search: JPEG, PNG, PDF, .txt, or .docx. The kind is "
            "detected from the bytes, not from this part's filename or content type."
        ),
    ),
    image: Optional[UploadFile] = File(
        default=None,
        description="Deprecated alias for 'file', accepted for symmetry with /assess.",
    ),
    features: str = Form(
        ...,
        description=(
            'JSON array of what to find. Entries are either names — ["has faces", '
            '"has sky"], resolved to an OpenCV detector when one matches — or full '
            'criterion objects, which is how a text feature sets its match mode: '
            '[{"name": "Notice to Owner", "type": "text", "match": "fuzzy"}].'
        ),
    ),
    ocr: str = Form(
        default="auto",
        description="Text-recognition policy: auto (default) | always | never.",
    ),
    regions: str = Form(
        default="true",
        description=(
            "Layer options. `enabled` is implied — that is the point of this "
            "endpoint — so the shorthand only chooses formats: 'true' (SVG, the "
            "default), 'svg,png,preview', 'none' for regions.json only. Send the "
            'full JSON object — e.g. {"layers":["svg"],"llm_boxes":true} — to '
            "run the bounding-box enforcement loop for features that resolve to "
            "the LLM, or to turn the open-vocabulary detector on."
        ),
    ),
):
    """Submit a scoring-free "where is X?" job.

    Same pipeline as /assess with the judgement removed: no weighted score, no
    verdict, no dependency resolution. An `llm` feature costs no call at all
    unless `regions.llm_boxes` is set, in which case a presence-only scoring
    call runs to give the bounding-box enforcement loop something to gate on
    and its judgement is then discarded — `features[name]` carries
    `localization` and regions, never a score or a verdict. Returns 202
    Accepted; poll GET /jobs/{job_id}.

    The result carries `features` where an assessment would carry
    `assessment`, plus the same `artifacts` and `page_geometry` blocks and the
    same artifact endpoints as any other job.

    Steps:
      1. Resolve the uploaded part and validate the ocr mode + content type.
      2. Parse `features` and the `regions` options (both 400 on bad input).
      3. Detect the kind from the bytes so an unsupported upload is a 400 now.
      4. Create the job row (phase=staging, metadata.type="locate").
      5. Write the payload, flip to pending, wake a worker.
    """
    # Step 1 — the uploaded part, same two field names as /assess
    upload = file or image
    if upload is None:
        raise HTTPException(
            status_code=400,
            detail="No document uploaded. Send the file as the 'file' multipart field.",
        )
    logger.info(
        "locate_document: filename=%s content_type=%s ocr=%s",
        upload.filename, upload.content_type, ocr,
    )
    if ocr not in ("auto", "always", "never"):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid ocr mode '{ocr}'. Expected one of: auto, always, never.",
        )
    validate_content_type(upload.content_type)

    # Step 2 — features and layer options
    feature_list = parse_features(features)
    try:
        region_options = parse_regions_option(regions or "true")
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid regions option: {exc}")

    # Step 3 — bytes, then the kind from the bytes
    contents = await upload.read()
    if not contents:
        raise HTTPException(status_code=400, detail="Empty document file")
    try:
        kind = detect_kind(contents, filename=upload.filename, content_type=upload.content_type)
    except UnsupportedDocumentError as exc:
        logger.warning("locate_document: rejected %s: %s", upload.filename, exc)
        raise HTTPException(status_code=400, detail=str(exc))
    logger.info("locate_document: detected kind=%s (%d bytes)", kind, len(contents))

    # Step 4 — the job row
    req_id = request_id_var.get("-")
    job_id = await jobs_registry.register(
        ClassifierMetadata(type="locate", request_id=req_id),
        initial_phase="staging",
    )

    # Step 5 — payload to disk first, THEN pending
    depth = await _enqueue(
        job_id,
        build_locate_payload(
            contents,
            upload.content_type,
            upload.filename or "upload",
            feature_list,
            ocr,
            region_options,
            job_id,
        ),
    )
    jobs_total.labels(type="locate", status="pending").inc()

    logger.info("locate_document: queued job_id=%s queue_depth=%d", job_id, depth)
    return JSONResponse(
        status_code=202,
        content={"job_id": job_id, "phase": "pending"},
    )
