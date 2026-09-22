"""Document Classifier — FastAPI application entry point.

This module wires everything together:
  - Configures logging with correlation ID injection (middleware.py).
  - Initialises the shared ``common.jobs`` SQLite registry, requeues any
    jobs a previous process left mid-flight, and starts CLASSIFIER_MAX_CONCURRENT
    background workers on startup (registry.init, workers.ClassifierQueue).
  - Registers the correlation ID middleware so every request gets a
    traceable [request_id] in its logs and response headers.
  - Instruments all HTTP endpoints with Prometheus metrics via
    prometheus_fastapi_instrumentator.
  - Defines the API endpoints:

    POST /assess            — submit a single-document assessment job (async).
    POST /assess/compare    — submit a comparison job against references (async).
    GET  /jobs              — list recent jobs (from common.jobs.router).
    GET  /jobs/{job_id}     — poll for job status and result (from common.jobs.router).
    DELETE /jobs/{job_id}   — delete a job record (from common.jobs.router).
    GET  /hints             — list all hint values and their LLM rubric definitions.
    GET  /cv-detectors      — list all registered CV detector names grouped by function.
    GET  /document-kinds    — supported upload kinds, text-match modes, OCR status.
    GET  /health            — liveness check.
    GET  /metrics           — Prometheus scrape endpoint (added by Instrumentator).

Uploads are documents, not just images: JPEG/PNG, PDF (native or scanned),
plain text, and .docx all load through ``common.documents`` into a page list
that may carry an image, a text layer, or both. Criteria then run on whichever
of those they need — ``cv`` on page images, ``text`` on the text layer
(OCR-filled when the document is a scan), ``llm`` on one page image plus the
extracted text.

Overall request flow for /assess:
  1. HTTP request arrives → CorrelationIDMiddleware assigns [request_id].
  2. assess_document() validates criteria and the ocr mode, reads file bytes.
  3. Job record created in SQLite via ``jobs_registry.register(..., "staging")``.
  4. Payload (file bytes + criteria + ocr mode) written to
     PAYLOAD_DIR/<job_id>.json, then the row is flipped to "pending" and idle
     workers are woken.
  5. 202 Accepted returned immediately with job_id.
  6. One of the worker tasks atomically claims the row (→ "processing"),
     reads the payload, loads the document, runs OCR + CV + text + LLM, and calls
     ``jobs_registry.set_result(...)`` on success or ``set_error(...)`` on failure.
  7. Caller polls GET /jobs/{job_id} until phase="completed" or "failed".

The jobs table IS the queue — see workers.py / common.jobs.worker — so up to
CLASSIFIER_MAX_CONCURRENT jobs run at once and pending work survives restarts.
"""

import json
import logging
from contextlib import asynccontextmanager

from typing import Optional

from fastapi import FastAPI, File, HTTPException, UploadFile, Form
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel

from common.documents import (
    EXTENSIONS,
    MAX_PATTERN_CHARS,
    UnsupportedDocumentError,
    detect_kind,
)
from common.jobs.router import build_router
from common.jobs.sqlite import SqliteRegistry

from analysis import (
    ocr_engine_status,
    parse_criteria,
    validate_content_type,
    validate_text_criteria,
)
from config import (
    DB_PATH,
    DEFAULT_CRITERIA,
    DOC_MAX_PAGES,
    HINT_RUBRICS,
    LOG_LEVEL,
    PDF_RENDER_DPI,
    TEXT_CHAR_BUDGET,
)
from cv import REGISTRY
from logger import logger
from middleware import CorrelationIDMiddleware, RequestIDFilter, request_id_var
from models import CompareRequest
from metrics import jobs_total
from runners import build_assess_payload, build_compare_payload
from workers import ClassifierQueue

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
# Must happen before any module emits a log line.  The RequestIDFilter injects
# request_id into every record; the format references it as %(request_id)s.

_filter = RequestIDFilter()
_handler = logging.StreamHandler()
_handler.addFilter(_filter)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] [%(request_id)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[_handler],
)
# Apply the level directly to the shared logger (basicConfig sets the root level)
logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
logger.info("Starting Document Classifier (log level=%s)", LOG_LEVEL)


# ---------------------------------------------------------------------------
# Shared jobs registry — persistent SQLite backend from common.jobs
# ---------------------------------------------------------------------------
# One process-wide registry, shared by main.py (endpoints) and the
# ClassifierQueue (worker pool). Its schema migration from classifier's legacy
# layout (status → phase, add metadata column) runs idempotently in ``init()``.

class ClassifierMetadata(BaseModel):
    """Per-job metadata for the classifier. Everything that used to live in
    the legacy ``type`` + ``request_id`` columns is now stored in the shared
    ``jobs.metadata`` JSON blob under this shape."""

    type: str        # "assess" | "compare"
    request_id: str


jobs_registry = SqliteRegistry(DB_PATH)
queue = ClassifierQueue(jobs_registry)


# ---------------------------------------------------------------------------
# App lifecycle — startup and shutdown
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage resources that must exist for the full lifetime of the server.

    On startup:
      - Initialise (or migrate) the shared SQLite job registry.
      - ``queue.start()``: requeue jobs the previous process left in
        "processing" / "staging", sweep orphan payload files, and start
        CLASSIFIER_MAX_CONCURRENT worker tasks.

    On shutdown (when the context exits):
      - ``queue.stop()``: cancel the worker tasks. Jobs mid-flight stay
        "processing" in the DB and are requeued by the next startup.
    """
    await jobs_registry.init()
    await queue.start()
    logger.info("lifespan: startup complete")

    yield  # server runs here

    await queue.stop()
    logger.info("lifespan: shutdown complete")


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(title="Document Classifier", lifespan=lifespan)

# Attach correlation ID middleware — wraps every request before route handlers run
app.add_middleware(CorrelationIDMiddleware)

# Auto-instrument all endpoints with HTTP request/latency metrics.
# /metrics and /health are excluded to avoid polluting the metric set.
Instrumentator(
    should_group_status_codes=True,
    excluded_handlers=["/metrics", "/health"],
).instrument(app).expose(app)

# Mount /jobs, /jobs/{id}, DELETE /jobs/{id} — see common.jobs.router.
# No cancel endpoint: the worker doesn't currently observe a cancel signal
# mid-analysis. If we later teach the worker to poll for phase="cancelled"
# and bail out, flip include_cancel=True here.
app.include_router(
    build_router(jobs_registry, include_delete=True, include_cancel=False)
)


# ---------------------------------------------------------------------------
# Enqueue helper
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/assess", status_code=202)
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
        ),
    )
    jobs_total.labels(type="assess", status="pending").inc()

    # Step 6 — return immediately; caller polls for the result
    logger.info("assess_document: queued job_id=%s queue_depth=%d", job_id, depth)
    return JSONResponse(
        status_code=202,
        content={"job_id": job_id, "phase": "pending"},
    )


@app.post("/assess/compare", status_code=202)
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
    depth = await _enqueue(job_id, build_compare_payload(request))
    jobs_total.labels(type="compare", status="pending").inc()

    # Step 4 — return immediately
    logger.info("assess_with_reference: queued job_id=%s queue_depth=%d", job_id, depth)
    return JSONResponse(
        status_code=202,
        content={"job_id": job_id, "phase": "pending"},
    )


@app.get("/hints")
def list_hints():
    """Return all available hint values and their LLM scoring instructions.

    Hints control which rubric the LLM uses when scoring a criterion.
    Set hint on any criterion (type='llm', or type='cv' as a fallback).
    """
    logger.debug("list_hints: returning %d hint definitions", len(HINT_RUBRICS))
    return JSONResponse(content={"hints": HINT_RUBRICS})


@app.get("/cv-detectors")
def list_cv_detectors():
    """Return all registered CV detector names, grouped by detector function.

    Use these names as the 'name' field of a criterion with type='cv'.
    Fuzzy matching is applied at runtime, so near-matches also work.
    """
    logger.debug("list_cv_detectors: building detector map from %d registry entries", len(REGISTRY))

    grouped: dict[str, list[str]] = {}
    for name, fn in REGISTRY.items():
        fn_name = fn.__name__
        grouped.setdefault(fn_name, []).append(name)

    detectors = [
        {"function": fn_name, "names": sorted(names)}
        for fn_name, names in sorted(grouped.items())
    ]

    logger.debug("list_cv_detectors: returning %d detectors", len(detectors))
    return JSONResponse(content={"detectors": detectors, "total_names": len(REGISTRY)})


@app.get("/document-kinds")
def list_document_kinds():
    """Return the upload kinds this service accepts and how they are handled.

    A cheap introspection endpoint, like /hints and /cv-detectors: it answers
    "what can I send, what will it be able to evaluate, and is OCR actually
    available right now?" without submitting a job. Nothing here is a
    per-request setting — the limits come from the container's environment.
    """
    logger.debug("list_document_kinds: building capability map")

    kinds = [
        {
            "kind": "image",
            "extensions": EXTENSIONS["image"],
            "content_types": ["image/jpeg", "image/png"],
            "detection": "magic bytes: FF D8 FF (JPEG) / 89 50 4E 47 0D 0A 1A 0A (PNG)",
            "pages": "1",
            "has_page_images": True,
            "native_text": False,
            "notes": "EXIF orientation is applied on load. Text criteria need OCR.",
        },
        {
            "kind": "pdf",
            "extensions": EXTENSIONS["pdf"],
            "content_types": ["application/pdf"],
            "detection": "magic bytes: %PDF- (anywhere in the first 1 KB)",
            "pages": f"up to CLASSIFIER_DOC_MAX_PAGES ({DOC_MAX_PAGES}); extras are "
                     "reported in document_info.truncated_pages",
            "has_page_images": True,
            "native_text": True,
            "notes": f"Each page is rendered at {PDF_RENDER_DPI} dpi for cv/llm criteria. "
                     "A scanned PDF has no native text layer, so OCR fills it in.",
        },
        {
            "kind": "txt",
            "extensions": EXTENSIONS["txt"],
            "content_types": ["text/plain"],
            "detection": "decodes as UTF-8 (BOM allowed), no NUL bytes, mostly printable",
            "pages": "1",
            "has_page_images": False,
            "native_text": True,
            "notes": "No rendered surface — cv criteria are SKIPPED and the llm call is "
                     "text-only.",
        },
        {
            "kind": "docx",
            "extensions": EXTENSIONS["docx"],
            "content_types": [
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            ],
            "detection": "ZIP magic PK\\x03\\x04 containing word/document.xml",
            "pages": "1 (python-docx reads XML, not a laid-out page)",
            "has_page_images": False,
            "native_text": True,
            "notes": "Paragraphs and table cells are extracted in document order; table "
                     "rows are flattened to 'cell | cell'. cv criteria are SKIPPED.",
        },
    ]

    return JSONResponse(
        content={
            "kinds": kinds,
            "unsupported": [
                {
                    "kind": "doc",
                    "reason": "Legacy OLE2 Word files are not readable by python-docx. "
                              "Convert to .docx and re-upload.",
                    "detection": "magic bytes: D0 CF 11 E0 A1 B1 1A E1",
                }
            ],
            "text_match_modes": {
                "contains": "substring anywhere in the document text (default)",
                "exact": "whole word or whole line (word-boundary anchored)",
                "regex": f"Python regular expression, max {MAX_PATTERN_CHARS} characters",
                "fuzzy": "best sliding-window similarity — use this on OCR'd text",
            },
            "ocr": {
                **ocr_engine_status(),
                "modes": ["auto", "always", "never"],
                "default": "auto",
            },
            "limits": {
                "max_pages": DOC_MAX_PAGES,
                "pdf_render_dpi": PDF_RENDER_DPI,
                "llm_text_char_budget": TEXT_CHAR_BUDGET,
                "images_per_llm_prompt": 1,
            },
        }
    )


@app.get("/health")
def health():
    """Liveness check used by the Docker healthcheck and load balancers."""
    logger.debug("health: returning ok")
    return {"status": "ok"}
