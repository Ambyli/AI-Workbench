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
from logger import logger
from middleware import CorrelationIDMiddleware, RequestIDFilter, request_id_var
from models import CompareRequest
from workers import job_queue, job_worker, jobs_total, job_queue_depth

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

_filter = RequestIDFilter()
_handler = logging.StreamHandler()
_handler.addFilter(_filter)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] [%(request_id)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[_handler],
)
logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
logger.info("Starting Document Classifier (log level=%s)", LOG_LEVEL)


# ---------------------------------------------------------------------------
# App + lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    await store.init_db()
    worker_task = asyncio.create_task(job_worker())
    logger.info("lifespan: startup complete")
    yield
    worker_task.cancel()
    logger.info("lifespan: shutdown complete")


app = FastAPI(title="Document Classifier", lifespan=lifespan)
app.add_middleware(CorrelationIDMiddleware)

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
            '"type" ("quality" or "feature"). '
            'Example: [{"name": "image sharpness", "type": "quality"}, '
            '{"name": "has solar panels", "type": "feature"}]'
        ),
    ),
):
    """Submit an image assessment job. Returns a job ID immediately.

    Poll GET /jobs/{job_id} for status and results.
    """
    logger.info("assess_document: filename=%s content_type=%s", image.filename, image.content_type)

    criterion_list = parse_criteria(criteria)
    if not criterion_list:
        raise HTTPException(status_code=400, detail="At least one criterion is required")

    contents = await image.read()
    if not contents:
        raise HTTPException(status_code=400, detail="Empty image file")

    req_id = request_id_var.get("-")
    job_id = await store.create_job("assess", req_id)

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

    logger.info("assess_document: queued job_id=%s queue_depth=%d", job_id, job_queue.qsize())
    return JSONResponse(
        status_code=202,
        content={"job_id": job_id, "status": "pending"},
    )


@app.post("/assess/compare", status_code=202)
async def assess_with_reference(request: CompareRequest):
    """Submit a comparison job. Returns a job ID immediately.

    Poll GET /jobs/{job_id} for status and results.
    """
    logger.info("assess_with_reference: %d example(s) aggregation=%s criteria=%s",
                len(request.examples), request.aggregation,
                [c.name for c in request.criteria])

    if not request.criteria:
        raise HTTPException(status_code=400, detail="At least one criterion is required")

    req_id = request_id_var.get("-")
    job_id = await store.create_job("compare", req_id)

    await job_queue.put((job_id, "compare", request, req_id))
    job_queue_depth.set(job_queue.qsize())
    jobs_total.labels(type="compare", status="pending").inc()

    logger.info("assess_with_reference: queued job_id=%s queue_depth=%d", job_id, job_queue.qsize())
    return JSONResponse(
        status_code=202,
        content={"job_id": job_id, "status": "pending"},
    )


@app.get("/jobs/{job_id}")
async def get_job(job_id: str):
    """Get the status and result of a job."""
    logger.info("get_job: job_id=%s", job_id)
    job = await store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    logger.info("get_job: returning job_id=%s status=%s", job_id, job["status"])
    return JSONResponse(content=job)


@app.get("/jobs")
async def list_jobs(limit: int = 20):
    """List recent jobs (most recent first)."""
    logger.info("list_jobs: limit=%d", limit)
    jobs = await store.list_jobs(limit)
    logger.info("list_jobs: returning %d jobs", len(jobs))
    return JSONResponse(content={"jobs": jobs, "count": len(jobs)})


@app.delete("/jobs/{job_id}", status_code=204)
async def delete_job(job_id: str):
    """Delete a job record."""
    logger.info("delete_job: job_id=%s", job_id)
    deleted = await store.delete_job(job_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")


@app.get("/health")
def health():
    logger.debug("health: returning ok")
    return {"status": "ok"}
