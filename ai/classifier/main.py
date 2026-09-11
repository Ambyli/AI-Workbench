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

    POST /assess            — submit a single-image assessment job (async).
    POST /assess/compare    — submit a comparison job against references (async).
    GET  /jobs              — list recent jobs (from common.jobs.router).
    GET  /jobs/{job_id}     — poll for job status and result (from common.jobs.router).
    DELETE /jobs/{job_id}   — delete a job record (from common.jobs.router).
    GET  /hints             — list all hint values and their LLM rubric definitions.
    GET  /cv-detectors      — list all registered CV detector names grouped by function.
    GET  /health            — liveness check.
    GET  /metrics           — Prometheus scrape endpoint (added by Instrumentator).

Overall request flow for /assess:
  1. HTTP request arrives → CorrelationIDMiddleware assigns [request_id].
  2. assess_document() validates criteria, reads image bytes.
  3. Job record created in SQLite via ``jobs_registry.register(..., "staging")``.
  4. Payload (image bytes + criteria) written to PAYLOAD_DIR/<job_id>.json,
     then the row is flipped to "pending" and idle workers are woken.
  5. 202 Accepted returned immediately with job_id.
  6. One of the worker tasks atomically claims the row (→ "processing"),
     reads the payload, runs CV + LLM, and calls
     ``jobs_registry.set_result(...)`` on success or ``set_error(...)`` on failure.
  7. Caller polls GET /jobs/{job_id} until phase="completed" or "failed".

The jobs table IS the queue — see workers.py / common.jobs.worker — so up to
CLASSIFIER_MAX_CONCURRENT jobs run at once and pending work survives restarts.
"""

import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, UploadFile, Form
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel

from common.jobs.router import build_router
from common.jobs.sqlite import SqliteRegistry

from analysis import parse_criteria, analyze_upload
from config import DB_PATH, DEFAULT_CRITERIA, LOG_LEVEL
from cv import REGISTRY
from llm import HINT_RUBRICS
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
    image: UploadFile,
    criteria: str = Form(
        default=json.dumps(DEFAULT_CRITERIA),
        description=(
            'JSON array of criterion objects. Each must have "name" and optionally '
            '"type" ("quality" or "feature") and "weight" (float, default 1.0). '
            'Example: [{"name": "image sharpness", "type": "quality", "weight": 1.0}, '
            '{"name": "has solar panels", "type": "feature", "weight": 3.0}]'
        ),
    ),
):
    """Submit a single-image assessment job.

    Returns 202 Accepted immediately with a job_id.
    Poll GET /jobs/{job_id} until phase is "completed" or "failed".

    Steps:
      1. Parse and validate the criteria JSON.
      2. Read the uploaded image bytes into memory.
      3. Create a job record in the DB (phase=staging) via jobs_registry.
      4. Write the payload to disk, flip the row to pending, wake a worker.
      5. Return the job_id to the caller.
    """
    logger.info("assess_document: filename=%s content_type=%s", image.filename, image.content_type)

    # Step 1 — parse criteria from the multipart form field
    criterion_list = parse_criteria(criteria)
    if not criterion_list:
        raise HTTPException(status_code=400, detail="At least one criterion is required")

    # Step 2 — read image bytes before returning (UploadFile is only readable during the request)
    contents = await image.read()
    if not contents:
        raise HTTPException(status_code=400, detail="Empty image file")

    # Step 3 — create a job record so the caller has an ID to poll
    req_id = request_id_var.get("-")
    job_id = await jobs_registry.register(
        ClassifierMetadata(type="assess", request_id=req_id),
        initial_phase="staging",
    )

    # Step 4 — enqueue: payload to disk first, THEN flip to pending so a worker
    # can never claim a row whose payload isn't there yet.
    depth = await _enqueue(
        job_id,
        build_assess_payload(contents, image.content_type, image.filename or "upload", criterion_list),
    )
    jobs_total.labels(type="assess", status="pending").inc()

    # Step 5 — return immediately; caller polls for the result
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

    The entire CompareRequest (including all example images or their
    pre_generated_analysis blobs) is written to the payload store and
    re-validated by the worker, so the job survives a restart.
    """
    logger.info("assess_with_reference: %d example(s) aggregation=%s criteria=%s",
                len(request.examples), request.aggregation,
                [c.name for c in request.criteria])

    # Step 1 — validate criteria (Pydantic enforces examples min_length=1)
    if not request.criteria:
        raise HTTPException(status_code=400, detail="At least one criterion is required")

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


@app.get("/health")
def health():
    """Liveness check used by the Docker healthcheck and load balancers."""
    logger.debug("health: returning ok")
    return {"status": "ok"}
