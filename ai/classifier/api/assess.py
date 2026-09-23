"""POST /assess and POST /assess/compare — submit, don't wait.

Both endpoints do the same five things and then return 202: validate what can
be validated cheaply (the ocr mode, the declared content type, the criteria
JSON and its text patterns, the regions option), read the bytes, detect the
document kind from them, register a job row in phase "staging", and enqueue.
Everything expensive happens in a worker.

The order matters. A malformed criterion, an unusable regular expression, an
unsupported upload (a legacy .doc, a video) is a **400 on this request**
rather than a job that fails in a worker a minute later — that is the whole
reason kind detection (microseconds) happens here while the full load (PDF
render, OCR) happens there.

    _enqueue()             — payload to disk, THEN the row to "pending", so a
                             worker can never claim a row whose payload is not
                             there yet. Shared with ``api.locate``.
    assess_document()      — POST /assess (multipart).
    assess_with_reference() — POST /assess/compare (JSON).

Process flow position: the top of the stack. Mounted by ``main``; hands work
to ``jobs.queue``.
"""

import json
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from common.documents import UnsupportedDocumentError, detect_kind

from analysis import parse_criteria, validate_content_type, validate_text_criteria
from api.schemas import ClassifierMetadata, CompareRequest, parse_regions_option
from config import DEFAULT_CRITERIA
from jobs.payloads import build_assess_payload, build_compare_payload
from jobs.queue import jobs_registry, queue
from logger import logger
from metrics import jobs_total
from middleware import request_id_var

router = APIRouter(tags=["assess"])

async def _enqueue(job_id: str, payload: dict) -> int:
    """Persist ``payload``, publish the job to the workers, return queue depth.

    The row was registered in phase "staging" so no worker can claim it
    before the payload exists. If the payload write fails the job is marked
    failed and the caller gets a 500 instead of a job that can never run.
    """
    try:
        return await queue.enqueue(job_id, payload)
    except Exception:
        # queue.enqueue already logged and marked the job failed
        raise HTTPException(status_code=500, detail="Could not persist job payload")


@router.post("/assess", status_code=202)
async def assess_document(
    file: Optional[UploadFile] = File(
        default=None,
        description=(
            "The document to assess: JPEG, PNG, PDF, .txt, or .docx. The kind is "
            "detected from the bytes, not from this part's filename or content type."
        ),
    ),
    image: Optional[UploadFile] = File(
        default=None,
        description="Deprecated alias for 'file', kept so existing callers keep working.",
    ),
    criteria: str = Form(
        default=json.dumps(DEFAULT_CRITERIA),
        description=(
            'JSON array of criterion objects. Each must have "name" and optionally '
            '"type" ("llm" default | "cv" | "text"), "hint", "weight" (float, default 1.0), '
            'and "depends_on". type="text" additionally takes "pattern" (defaults to '
            '"name"), "match" ("contains" default | "exact" | "regex" | "fuzzy"), '
            '"case_sensitive", "fuzzy_threshold", and "min_count". '
            'Example: [{"name": "image sharpness", "type": "cv", "weight": 1.0}, '
            '{"name": "Notice to Owner", "type": "text", "match": "fuzzy", "weight": 3.0}]'
        ),
    ),
    ocr: str = Form(
        default="auto",
        description=(
            "Text-recognition policy: 'auto' (default — OCR only when text is needed "
            "and missing), 'always' (OCR every page image), 'never' (skip OCR; text "
            "criteria then fail with 'no text available')."
        ),
    ),
    regions: str = Form(
        default="",
        description=(
            "Where-did-you-find-it options, OFF by default. Shorthand: 'true' "
            "(regions + the SVG layer), 'svg,png,preview' (those layers), 'none' "
            "(regions.json only), 'false' or absent (off). Or a JSON object — "
            '{"enabled": true, "layers": ["svg"], "layers_per_criterion": false}. '
            "See API.md § Regions and layers."
        ),
    ),
):
    """Submit a single-document assessment job.

    Accepts JPEG/PNG, PDF, plain text, and .docx. Returns 202 Accepted
    immediately with a job_id; poll GET /jobs/{job_id} until phase is
    "completed" or "failed".

    Steps:
      1. Resolve the uploaded part ('file', or the legacy 'image' alias).
      2. Parse and validate the criteria JSON and the ocr mode.
      3. Read the uploaded bytes into memory and detect the document kind
         from its magic bytes (unsupported kinds are a 400 here, not a
         failed job).
      4. Create a job record in the DB (phase=staging) via jobs_registry.
      5. Write the payload to disk, flip the row to pending, wake a worker.
      6. Return the job_id to the caller.
    """
    # Step 1 — 'file' is the current field name; 'image' is the original one
    # and still accepted, because every existing caller sends it.
    upload = file or image
    if upload is None:
        raise HTTPException(
            status_code=400,
            detail="No document uploaded. Send the file as the 'file' multipart field "
                   "(the legacy name 'image' is also accepted).",
        )
    logger.info(
        "assess_document: filename=%s content_type=%s field=%s ocr=%s",
        upload.filename,
        upload.content_type,
        "file" if file is not None else "image",
        ocr,
    )

    # Step 2 — validate the ocr mode, the declared content type, and criteria
    if ocr not in ("auto", "always", "never"):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid ocr mode '{ocr}'. Expected one of: auto, always, never.",
        )
    validate_content_type(upload.content_type)

    # A malformed regions option is a client error, so it is a 400 here
    # rather than a job that fails in a worker a minute later.
    try:
        region_options = parse_regions_option(regions)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid regions option: {exc}")

    criterion_list = parse_criteria(criteria)
    if not criterion_list:
        raise HTTPException(status_code=400, detail="At least one criterion is required")

    # Step 3 — read bytes before returning (UploadFile is only readable during the request)
    contents = await upload.read()
    if not contents:
        raise HTTPException(status_code=400, detail="Empty document file")

    # Step 3b — identify the kind from the bytes NOW, so an unsupported upload
    # (a legacy .doc, a video, a spreadsheet) is a 400 on this request rather
    # than a job that fails in a worker a minute later. Magic-byte detection is
    # microseconds; the full load (PDF render, OCR) still happens in the worker.
    try:
        kind = detect_kind(contents, filename=upload.filename, content_type=upload.content_type)
    except UnsupportedDocumentError as exc:
        logger.warning("assess_document: rejected %s: %s", upload.filename, exc)
        raise HTTPException(status_code=400, detail=str(exc))
    logger.info("assess_document: detected kind=%s (%d bytes)", kind, len(contents))

    # Step 4 — create a job record so the caller has an ID to poll
    req_id = request_id_var.get("-")
    job_id = await jobs_registry.register(
        ClassifierMetadata(type="assess", request_id=req_id),
        initial_phase="staging",
    )

    # Step 5 — enqueue: payload to disk first, THEN flip to pending so a worker
    # can never claim a row whose payload isn't there yet.
    depth = await _enqueue(
        job_id,
        build_assess_payload(
            contents,
            upload.content_type,
            upload.filename or "upload",
            criterion_list,
            ocr,
            region_options,
            job_id,
        ),
    )
    jobs_total.labels(type="assess", status="pending").inc()

    # Step 6 — return immediately; caller polls for the result
    logger.info("assess_document: queued job_id=%s queue_depth=%d", job_id, depth)
    return JSONResponse(
        status_code=202,
        content={"job_id": job_id, "phase": "pending"},
    )


@router.post("/assess/compare", status_code=202)
async def assess_with_reference(request: CompareRequest):
    """Submit a comparison job against one or more reference examples.

    Returns 202 Accepted immediately with a job_id.
    Poll GET /jobs/{job_id} until phase is "completed" or "failed".

    Subject and examples may be any supported document kind (JPEG/PNG, PDF,
    .txt, .docx) and all of them are analysed under the request's single
    ``ocr`` policy, so their text layers are comparable.

    The entire CompareRequest (including all example documents or their
    pre_generated_analysis blobs) is written to the payload store and
    re-validated by the worker, so the job survives a restart.
    """
    logger.info("assess_with_reference: %d example(s) aggregation=%s ocr=%s criteria=%s",
                len(request.examples), request.aggregation, request.ocr,
                [c.name for c in request.criteria])

    # Step 1 — validate criteria (Pydantic enforces examples min_length=1)
    if not request.criteria:
        raise HTTPException(status_code=400, detail="At least one criterion is required")
    # Reject an unusable text pattern now rather than in a worker minutes later
    validate_text_criteria(request.criteria)

    # Step 2 — create job record
    req_id = request_id_var.get("-")
    job_id = await jobs_registry.register(
        ClassifierMetadata(type="compare", request_id=req_id),
        initial_phase="staging",
    )

    # Step 3 — enqueue the full request object (runners.run_compare re-validates it)
    depth = await _enqueue(job_id, build_compare_payload(request, job_id))
    jobs_total.labels(type="compare", status="pending").inc()

    # Step 4 — return immediately
    logger.info("assess_with_reference: queued job_id=%s queue_depth=%d", job_id, depth)
    return JSONResponse(
        status_code=202,
        content={"job_id": job_id, "phase": "pending"},
    )
