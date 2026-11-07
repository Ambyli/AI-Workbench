"""Document Classifier — FastAPI application entry point.

This module does four things and nothing else:
  - Configures logging with correlation ID injection (middleware.py).
  - Builds the app, attaches the correlation ID middleware, and instruments
    every endpoint with Prometheus metrics.
  - Owns the lifespan: open the one Postgres pool (``db.database``, the
    classifier-db container), create the ``common.jobs`` jobs table and the
    references and model-usage tables beside it, fail references whose
    creation job is gone, requeue jobs a previous process left mid-flight,
    start CLASSIFIER_MAX_CONCURRENT workers and the artifact sweeper — and
    stop them again.
  - Mounts the routers.

Every endpoint handler lives in ``api/``:

    api.assess         POST /assess — the one analysis endpoint (JSON or
                       multipart, parsed into one AssessRequest)
    api.introspection  GET /models, /criterion-types, /hints, /cv-detectors,
                       /document-kinds, /health
    api.artifacts      the four /jobs/{job_id}/artifacts routes
    api.references     POST/GET/PATCH/DELETE /references — saved worked
                       examples, each built by a "reference" job
    api.usage          GET /jobs/{job_id}/usage, GET /usage — the model
                       calls each job made (llm_calls), which outlive the job
    common.jobs.router GET /jobs, GET /jobs/{id}, POST /jobs/{id}/cancel,
                       DELETE /jobs/{id}, and GET /jobs/{id}/progress
                       (build_progress_router) — how far along a job is, and
                       its checkpoint history

/locate and /assess/compare were removed and answer 404 like any unknown
route; /locate's job is ``score: false`` on a criterion of /assess.

Overall request flow for /assess:
  1. HTTP request arrives → CorrelationIDMiddleware assigns [request_id].
  2. ``api.assess.assess`` first asks the queue for room
     (``queue.check_room`` — with CLASSIFIER_QUEUE_LIMIT set, a full queue
     is a 429 + Retry-After before the body is even read; see
     ``queue_full`` below), then validates the request (per-type criterion
     options, caps, dependencies), resolves every document's bytes (base64,
     inline text, or an SSRF-checked URL fetch), detects each kind, counts
     the items (every page of every document) and refuses more than
     CLASSIFIER_MAX_ITEMS — all before anything is queued.
  3. Job record created in Postgres via ``queue.admit(...)`` — the
     registry's ``register(..., "staging", max_open=CLASSIFIER_QUEUE_LIMIT)``,
     the authoritative check (one transaction, so the last slot goes to one
     submit).
  4. Payload (every document's bytes + validated criteria) written to
     PAYLOAD_DIR/<job_id>.json, then the row is flipped to "pending" and idle
     workers are woken.
  5. 202 Accepted returned immediately with job_id.
  6. One of the worker tasks atomically claims the row (→ "processing") and
     runs ``jobs.runners.run_assess``: load every page of every document,
     build one context per item, evaluate every (criterion, item) unit
     independently (per-item dependency gating, CLASSIFIER_MAX_UNITS_PER_JOB
     at once, model calls bounded per model by models.yaml's max_concurrent
     and OCR by
     CLASSIFIER_OCR_WORKERS across the process), aggregate each criterion
     (pages → documents), weigh per item and overall, and store the
     artifacts.
  7. Caller polls GET /jobs/{job_id} until phase="completed", "failed" or
     "cancelled" (``?wait=<s>`` holds each poll until then, up to
     JOB_WAIT_MAX_S; POST /jobs/{job_id}/cancel stops a job within about
     CANCEL_CHECK_INTERVAL_S — see jobs/queue.py § Cancel) —
     ``metadata.progress`` says how far along it is meanwhile (the job's
     ``common.jobs.progress`` gauge, flushed about once a second), and
     GET /jobs/{job_id}/progress pages through its checkpoint history.

The jobs table IS the queue — see jobs/queue.py and common.jobs.worker — so up
to CLASSIFIER_MAX_CONCURRENT jobs run at once and pending work survives
restarts.

A request carries a list of documents: JPEG/PNG/HEIC/HEIF/AVIF photos, PDFs of
any page count (native or scanned), SVGs, plain text, and .docx, all loaded
through ``common.documents`` into pages that may carry an image, a text
layer, or both. Every page of every document is one item.

Every job writes a per-job artifact directory (regions.json, one
text.p<item>.<key>.json per item and text layer its criteria used, one base
image per item, manifest.json with the item map); the SVG / PNG / preview
layers are rendered on first fetch by the artifact routes. A background sweeper prunes those directories, and the
expired job rows themselves, past JOB_TTL_HOURS.

Every vision-model request is also written as one ``llm_calls`` row linked
to its job (``llm.usage``). Those rows are NOT swept — they outlive the job,
so its cost stays readable at GET /jobs/{id}/usage — and only DELETE
/jobs/{id} removes them.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator

from common.jobs.router import build_progress_router, build_router

from api import artifacts as artifacts_api
from api import assess, introspection, references as references_api
from api import usage as usage_api
from config import JOB_WAIT_MAX_S, LOG_LEVEL
from db import database
from jobs.queue import QueueFullError, delete_job_side_data, jobs_registry, queue, sweeper
from llm import client as llm_client
from llm import models as llm_models
from llm import usage as llm_usage
from logger import logger
from metrics import jobs_open, jobs_rejected
from middleware import CorrelationIDMiddleware, RequestIDFilter
from references.store import reconcile as reconcile_references
from references.store import reference_registry
from references.store import refresh_gauges as refresh_reference_gauges

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
      - Load and validate the model registry (``llm.models.startup_check``,
        ``models.yaml``) FIRST: a file that does not validate, or a default
        model that is unavailable, stops the container here with the reason
        in the log — and one WARNING names any replaced env var
        (VISION_LLM_*, CLASSIFIER_MAX_LLM_CALLS, …) still set.
      - Open the Postgres pool (``database.init()``), sized from the
        registry. No CLASSIFIER_DB_HOST,
        or a database that refuses the connection, fails startup here, with
        the reason in the log — the service has no other store.
      - Create the ``jobs``, ``job_progress``, ``reference_examples`` and
        ``llm_calls`` tables (idempotent), all in the one classifier-db
        database — ``job_progress`` right after ``jobs``, whose rows its
        foreign key references (``ON DELETE CASCADE``: a job's progress
        history goes with its row, through DELETE /jobs/{id} or the sweeper).
      - Reconcile references: a ``pending`` one whose creation job is gone or
        finished without readying it is marked ``failed`` (its job may have
        expired, or failed while no process was there to record it).
      - ``queue.start()``: requeue jobs the previous process left in
        "processing" / "staging" (and put their progress snapshot back to
        ``queued``), sweep orphan payload files, and start
        CLASSIFIER_MAX_CONCURRENT worker tasks — logging
        CLASSIFIER_QUEUE_LIMIT beside it (a WARNING when it is the smaller,
        so some workers will stay idle).
      - ``sweeper.start()``: prune artifact directories and job rows past
        JOB_TTL_HOURS once immediately (a container that was down through a
        whole TTL window catches up on boot), then every
        CLASSIFIER_ARTIFACT_SWEEP_INTERVAL_S.

    On shutdown (when the context exits):
      - ``queue.stop()``: cancel the worker tasks. Jobs mid-flight stay
        "processing" in the DB and are requeued by the next startup.
      - ``sweeper.stop()``: cancel the sweep loop. A sweep is idempotent, so
        an interrupted one is simply redone next boot.
      - ``llm_client.aclose()``: close the pooled vision-model HTTP client.
      - ``database.close()``: close the Postgres pool, last — the workers and
        the sweeper that borrow from it are already stopped.
    """
    llm_models.startup_check()
    await database.init()
    await jobs_registry.init()
    await queue.progress.init()
    await reference_registry.init()
    await llm_usage.usage_store.init()
    failed = await reconcile_references(jobs_registry)
    if failed:
        logger.warning("lifespan: %d pending reference(s) reconciled to failed", failed)
    await refresh_reference_gauges()
    await queue.start()
    await sweeper.start()
    logger.info("lifespan: startup complete")

    yield  # server runs here

    await sweeper.stop()
    await queue.stop()
    await llm_client.aclose()
    await database.close()
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


# A submit refused at CLASSIFIER_QUEUE_LIMIT (jobs/queue.py: check_room, the
# pre-check before the body is read, or admit, the authoritative one at
# registration). 429 with FastAPI's own {"detail": …} shape — the same one
# the edge's 503 uses — and Retry-After in whole seconds: the queue's
# estimate from its recent job run times, 2–60 s (10 until a job has
# finished). No job id exists, so there is nothing for a caller to cancel or
# delete. Counted (classifier_jobs_rejected_total{type, reason="queue_full"};
# jobs_total{status="pending"} never sees it) and logged once per refusal.
# The edge's `listen classifier` retries connection failures only, so a 429
# passes it untouched; LiteLLM's /v1/classifier pass-through copies the
# upstream status, body and headers (all but hop-by-hop ones, as read in its
# 1.93 source — API.md § Deploying this version has the `curl -i` that
# confirms it on the running image).
@app.exception_handler(QueueFullError)
async def queue_full(request: Request, exc: QueueFullError) -> JSONResponse:
    jobs_rejected.labels(type=exc.job_type, reason="queue_full").inc()
    jobs_open.set(exc.open_jobs)
    logger.info(
        "queue: refused a %s submit (429) — %d jobs open, CLASSIFIER_QUEUE_LIMIT=%d, "
        "Retry-After %d s", exc.job_type, exc.open_jobs, exc.limit, exc.retry_after_s,
    )
    return JSONResponse(
        status_code=429,
        content={"detail": exc.detail},
        headers={"Retry-After": str(exc.retry_after_s)},
    )

# Mount GET /jobs, GET /jobs/{id}, POST /jobs/{id}/cancel and DELETE
# /jobs/{id} — see common.jobs.router.
#
# The cancel route flips the row to "cancelled" at once (one transaction that
# refuses a finished job: 409 already_<phase>). It is mounted because the
# queue's WorkerPool is cancel-aware (CANCEL_CHECK_INTERVAL_S): a running
# job's worker sees the phase within about a second and cancels its runner.
# on_cancel finishes a job no worker owns yet (staging / pending) — see
# jobs/queue.py § Cancel; a processing job is the pool's to finish.
#
# on_delete takes the job's artifact directory AND its model-usage rows with
# the row, so DELETE /jobs/{id} can never leave layer files that nothing
# points at. The usage rows are the one thing the TTL sweeper leaves behind
# (they outlive the job on purpose), so this delete is the only way they go.
# A DELETE on a RUNNING job stops it like a cancel (the pool sees the row
# gone), and the pool runs the same cleanup again once the runner has
# unwound, for what it wrote after this hook.
#
# terminal_phases turns on GET /jobs/{id}?wait= (the long-poll): the request
# is held until the job is completed, failed or cancelled — the only phases a
# job ends in (the worker pool's set_result / set_error, the cancel route;
# staging, pending and processing all move on) — or JOB_WAIT_MAX_S passes.
# A phase a job can END in that is missing here makes every wait on it run to
# the cap.
app.include_router(
    build_router(
        jobs_registry,
        include_delete=True,
        include_cancel=True,
        on_delete=delete_job_side_data,
        on_cancel=queue.on_cancel_request,
        terminal_phases=("completed", "failed", "cancelled"),
        max_wait_s=JOB_WAIT_MAX_S,
    )
)

# Mount the four artifact routes. They are three path segments deep
# (/jobs/{id}/artifacts…) so they cannot collide with the jobs router's
# /jobs/{job_id}, whatever the include order.
app.include_router(artifacts_api.build_artifacts_router(jobs_registry))

# GET /jobs/{id}/usage (three segments deep, like the artifact routes) and
# GET /usage — what each job's model calls cost.
app.include_router(usage_api.build_usage_router(jobs_registry))

# GET /jobs/{id}/progress — the progress snapshot (also on GET /jobs/{id} as
# metadata.progress) plus a page of the job's checkpoint history. Three
# segments deep as well. No on_delete wiring: job_progress rows cascade with
# the job row.
app.include_router(build_progress_router(jobs_registry, queue.progress))

# The endpoints themselves: /assess, /references, then the introspection
# routes and /health.
app.include_router(assess.router)
app.include_router(references_api.router)
app.include_router(introspection.router)
