"""Document Classifier — FastAPI application entry point.

This module wires everything together:
  - Configures logging with correlation ID injection (middleware.py).
  - Initialises the SQLite job store and starts the background worker on
    startup (store.init_db, workers.job_worker).
  - Registers the correlation ID middleware so every request gets a
    traceable [request_id] in its logs and response headers.
  - Instruments all HTTP endpoints with Prometheus metrics via
    prometheus_fastapi_instrumentator.
  - Defines the five API endpoints:

    POST /assess            — submit a single-image assessment job (async).
    POST /assess/compare    — submit a comparison job against references (async).
    GET  /jobs/{job_id}     — poll for job status and result.
    GET  /jobs              — list recent jobs.
    DELETE /jobs/{job_id}   — delete a job record.
    GET  /hints             — list all hint values and their LLM rubric definitions.
    GET  /cv-detectors      — list all registered CV detector names grouped by function.
    GET  /health            — liveness check.
    GET  /metrics           — Prometheus scrape endpoint (added by Instrumentator).

Overall request flow for /assess:
  1. HTTP request arrives → CorrelationIDMiddleware assigns [request_id].
  2. assess_document() validates criteria, reads image bytes.
  3. Job record created in SQLite (status=pending).
  4. Job enqueued into workers.job_queue.
  5. 202 Accepted returned immediately with job_id.
  6. Background worker dequeues job → runs CV + LLM → stores result.
  7. Caller polls GET /jobs/{job_id} until status=completed.
"""

import asyncio
import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, UploadFile, Form
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator

import store
from analysis import parse_criteria, analyze_upload
from config import DEFAULT_CRITERIA, LOG_LEVEL
from cv import REGISTRY
from llm import HINT_RUBRICS
from logger import logger
from middleware import CorrelationIDMiddleware, RequestIDFilter, request_id_var
from models import CompareRequest
from workers import job_queue, job_worker, jobs_total, job_queue_depth

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
# App lifecycle — startup and shutdown
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage resources that must exist for the full lifetime of the server.

    On startup:
      - Initialise (or reuse) the SQLite job database.
      - Start the background job worker as an asyncio Task.

    On shutdown (when the context exits):
      - Cancel the worker task cleanly.
    """
    # Startup — database must be ready before the worker starts accepting jobs
    await store.init_db()
    worker_task = asyncio.create_task(job_worker())
    logger.info("lifespan: startup complete")

    yield  # server runs here

    # Shutdown — cancel the worker (in-flight jobs will be marked failed on restart)
    worker_task.cancel()
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
    Poll GET /jobs/{job_id} until status is "completed" or "failed".

    Steps:
      1. Parse and validate the criteria JSON.
      2. Read the uploaded image bytes into memory.
      3. Create a job record in the DB (status=pending).
      4. Enqueue the job for background processing.
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
    job_id = await store.create_job("assess", req_id)

    # Step 4 — enqueue; the background worker will pick this up and run the analysis
    await job_queue.put((
        job_id,
        "assess",
        {
            "image_bytes": contents,
            "content_type": image.content_type,
            "filename": image.filename or "upload",
            "criteria": criterion_list,
        },
        req_id,
    ))
    job_queue_depth.set(job_queue.qsize())
    jobs_total.labels(type="assess", status="pending").inc()

    # Step 5 — return immediately; caller polls for the result
    logger.info("assess_document: queued job_id=%s queue_depth=%d", job_id, job_queue.qsize())
    return JSONResponse(
        status_code=202,
        content={"job_id": job_id, "status": "pending"},
    )


@app.post("/assess/compare", status_code=202)
async def assess_with_reference(request: CompareRequest):
    """Submit a comparison job against one or more reference examples.

    Returns 202 Accepted immediately with a job_id.
    Poll GET /jobs/{job_id} until status is "completed" or "failed".

    The entire CompareRequest (including all example images or their
    pre_generated_analysis blobs) is passed through the queue in memory —
    no re-parsing is needed inside the worker.

    Steps:
      1. Validate that at least one criterion was provided.
      2. Create a job record in the DB (status=pending).
      3. Enqueue the full CompareRequest for background processing.
      4. Return the job_id to the caller.
    """
    logger.info("assess_with_reference: %d example(s) aggregation=%s criteria=%s",
                len(request.examples), request.aggregation,
                [c.name for c in request.criteria])

    # Step 1 — validate criteria (Pydantic enforces examples min_length=1)
    if not request.criteria:
        raise HTTPException(status_code=400, detail="At least one criterion is required")

    # Step 2 — create job record
    req_id = request_id_var.get("-")
    job_id = await store.create_job("compare", req_id)

    # Step 3 — enqueue the full request object (workers._run_compare receives it directly)
    await job_queue.put((job_id, "compare", request, req_id))
    job_queue_depth.set(job_queue.qsize())
    jobs_total.labels(type="compare", status="pending").inc()

    # Step 4 — return immediately
    logger.info("assess_with_reference: queued job_id=%s queue_depth=%d", job_id, job_queue.qsize())
    return JSONResponse(
        status_code=202,
        content={"job_id": job_id, "status": "pending"},
    )


@app.get("/jobs/{job_id}")
async def get_job(job_id: str):
    """Return the current status and result of a job.

    Callers should poll this endpoint after submitting a job.
    The response includes status and, when completed, the full result dict.
    """
    logger.info("get_job: job_id=%s", job_id)
    job = await store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    logger.info("get_job: returning job_id=%s status=%s", job_id, job["status"])
    return JSONResponse(content=job)


@app.get("/jobs")
async def list_jobs(limit: int = 20):
    """Return the most recent jobs (newest first), without result blobs.

    Useful for monitoring and debugging.  Use GET /jobs/{job_id} for the
    full result of a specific job.
    """
    logger.info("list_jobs: limit=%d", limit)
    jobs = await store.list_jobs(limit)
    logger.info("list_jobs: returning %d jobs", len(jobs))
    return JSONResponse(content={"jobs": jobs, "count": len(jobs)})


@app.delete("/jobs/{job_id}", status_code=204)
async def delete_job(job_id: str):
    """Permanently delete a job record from the database.

    Returns 204 No Content on success, 404 if the job does not exist.
    Deleting a job that is currently processing does not stop the worker —
    the worker will still write its result (which will be an orphaned row
    if the delete completes first).
    """
    logger.info("delete_job: job_id=%s", job_id)
    deleted = await store.delete_job(job_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")


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

    # Group registry names by the underlying function so callers can see
    # which aliases map to the same detector.
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
