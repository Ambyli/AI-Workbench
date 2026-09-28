"""Document Classifier — FastAPI application entry point.

This module does four things and nothing else:
  - Configures logging with correlation ID injection (middleware.py).
  - Builds the app, attaches the correlation ID middleware, and instruments
    every endpoint with Prometheus metrics.
  - Owns the lifespan: initialise the shared ``common.jobs`` SQLite registry,
    requeue jobs a previous process left mid-flight, start
    CLASSIFIER_MAX_CONCURRENT workers and the artifact sweeper — and stop them
    again.
  - Mounts the routers.

Every endpoint handler lives in ``api/``:

    api.assess         POST /assess — the one analysis endpoint (JSON or
                       multipart, parsed into one AssessRequest)
    api.introspection  GET /criterion-types, /hints, /cv-detectors,
                       /document-kinds, /health
    api.artifacts      the four /jobs/{job_id}/artifacts routes
    common.jobs.router GET /jobs, GET /jobs/{id}, DELETE /jobs/{id}

/locate and /assess/compare were removed and answer 404 like any unknown
route; /locate's job is ``score: false`` on a criterion of /assess.

Overall request flow for /assess:
  1. HTTP request arrives → CorrelationIDMiddleware assigns [request_id].
  2. ``api.assess.assess`` validates the request (per-type criterion
     options, caps, dependencies), resolves every document's bytes (base64,
     inline text, or an SSRF-checked URL fetch), detects each kind, counts
     the items (every page of every document) and refuses more than
     CLASSIFIER_MAX_ITEMS — all before anything is queued.
  3. Job record created in SQLite via ``jobs_registry.register(..., "staging")``.
  4. Payload (every document's bytes + validated criteria) written to
     PAYLOAD_DIR/<job_id>.json, then the row is flipped to "pending" and idle
     workers are woken.
  5. 202 Accepted returned immediately with job_id.
  6. One of the worker tasks atomically claims the row (→ "processing") and
     runs ``jobs.runners.run_assess``: load every page of every document,
     build one context per item, evaluate every (criterion, item) unit
     independently (per-item dependency gating, CLASSIFIER_MAX_UNITS_PER_JOB
     at once, model calls bounded by CLASSIFIER_MAX_LLM_CALLS and OCR by
     CLASSIFIER_OCR_WORKERS across the process), aggregate each criterion
     (pages → documents), weigh per item and overall, and store the
     artifacts.
  7. Caller polls GET /jobs/{job_id} until phase="completed" or "failed".

The jobs table IS the queue — see jobs/queue.py and common.jobs.worker — so up
to CLASSIFIER_MAX_CONCURRENT jobs run at once and pending work survives
restarts.

A request carries a list of documents: JPEG/PNG photos, PDFs of any page
count (native or scanned), plain text, and .docx, all loaded through
``common.documents`` into pages that may carry an image, a text layer, or
both. Every page of every document is one item.

Every job writes a per-job artifact directory (regions.json, one
text.p<item>.<key>.json per item and text layer its criteria used, one base
image per item, manifest.json with the item map); the SVG / PNG / preview
layers are rendered on first fetch by the artifact routes. A background sweeper prunes those directories, and the
expired job rows themselves, past JOB_TTL_HOURS.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator

from common.jobs.router import build_router

from api import artifacts as artifacts_api
from api import assess, introspection
from config import LOG_LEVEL
from jobs.queue import jobs_registry, queue, sweeper
from logger import logger
from middleware import CorrelationIDMiddleware, RequestIDFilter
from regions.sweeper import delete_artifacts_for_job

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
      - Initialise (or migrate) the shared SQLite job registry.
      - ``queue.start()``: requeue jobs the previous process left in
        "processing" / "staging", sweep orphan payload files, and start
        CLASSIFIER_MAX_CONCURRENT worker tasks.
      - ``sweeper.start()``: prune artifact directories and job rows past
        JOB_TTL_HOURS once immediately (a container that was down through a
        whole TTL window catches up on boot), then every
        CLASSIFIER_ARTIFACT_SWEEP_INTERVAL_S.

    On shutdown (when the context exits):
      - ``queue.stop()``: cancel the worker tasks. Jobs mid-flight stay
        "processing" in the DB and are requeued by the next startup.
      - ``sweeper.stop()``: cancel the sweep loop. A sweep is idempotent, so
        an interrupted one is simply redone next boot.
    """
    await jobs_registry.init()
    await queue.start()
    await sweeper.start()
    logger.info("lifespan: startup complete")

    yield  # server runs here

    await sweeper.stop()
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
#
# on_delete takes the job's artifact directory with the row, so DELETE
# /jobs/{id} can never leave layer files that nothing points at.
app.include_router(
    build_router(
        jobs_registry,
        include_delete=True,
        include_cancel=False,
        on_delete=delete_artifacts_for_job,
    )
)

# Mount the four artifact routes. They are three path segments deep
# (/jobs/{id}/artifacts…) so they cannot collide with the jobs router's
# /jobs/{job_id}, whatever the include order.
app.include_router(artifacts_api.build_artifacts_router(jobs_registry))

# The endpoints themselves: /assess, then the introspection routes and
# /health.
app.include_router(assess.router)
app.include_router(introspection.router)
