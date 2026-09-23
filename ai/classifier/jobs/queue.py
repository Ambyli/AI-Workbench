"""Queue glue: wires the shared WorkerPool + FilePayloadStore to the runners.

The classifier uses an async job pattern so that POST /assess,
POST /assess/compare and POST /locate can return a job ID immediately
(202 Accepted) without blocking the HTTP connection for the full duration of
the LLM call.

Division of labour:
  common.jobs.worker.WorkerPool      — N claim-and-handle loops, wake/poll, recovery
  common.jobs.payloads.FilePayloadStore — job inputs on disk so the queue survives restarts
  jobs.runners                       — the actual assess / compare / locate work
  metrics.py                         — Prometheus objects shared with the endpoints
  this module                        — ClassifierQueue: enqueue, handle_job, lifecycle

Flow:
  1. ``api.assess`` / ``api.locate`` registers a row (phase "staging") and
     calls ``queue.enqueue()``, which writes the payload, flips the row to
     "pending", and wakes a worker.
  2. One of CLASSIFIER_MAX_CONCURRENT pool workers atomically claims the row
     (→ "processing") and calls ``handle_job``, which reads the payload,
     dispatches on ``metadata.type`` ("assess" | "compare" | "locate") to a
     runner, and deletes the payload once it finishes (a job cancelled by
     shutdown keeps its payload so the requeued row can be re-run).
  3. The pool persists the result / error (→ "completed" | "failed") and
     calls ``_on_finish`` for metrics.
  4. Callers poll GET /jobs/{job_id}.

Why the DB is the queue, not an asyncio.Queue: an in-memory queue loses every
pending job on restart and can't be shared across processes. With the
registry as the queue, ``start()`` requeues rows a previous process left in
"processing", and the payload on disk means the work can actually be redone.

The three process-wide singletons at the bottom — the registry, the queue and
the artifact sweeper — live here rather than in ``main`` so the endpoint
modules can reach them without importing the app they are mounted on.
"""

import asyncio
from typing import Any, Optional

from common.jobs.model import JobBase
from common.jobs.payloads import FilePayloadStore
from common.jobs.sqlite import SqliteRegistry
from common.jobs.worker import WorkerPool

from config import DB_PATH, MAX_CONCURRENT, PAYLOAD_DIR, WORKER_POLL_INTERVAL_S
from jobs.runners import run_assess, run_compare, run_locate
from logger import logger
from metrics import job_duration, job_queue_depth, jobs_in_flight, jobs_total
from middleware import request_id_var
from regions.sweeper import ArtifactSweeper

_RUNNERS = {"assess": run_assess, "compare": run_compare, "locate": run_locate}


class ClassifierQueue:
    """Owns the payload store and worker pool for one registry.

    main.py builds one instance at import time and drives it from the
    FastAPI lifespan (``start`` / ``stop``) and the submit endpoints
    (``enqueue``).
    """

    def __init__(self, registry: SqliteRegistry) -> None:
        self.registry = registry
        self.payloads = FilePayloadStore(PAYLOAD_DIR)
        self.pool = WorkerPool(
            registry,
            self.handle_job,
            concurrency=MAX_CONCURRENT,
            poll_interval=WORKER_POLL_INTERVAL_S,
            on_finish=self._on_finish,
            name="classifier-worker",
            logger=logger,
        )

    # ── Lifecycle (called from main.lifespan) ─────────────────────────────
    async def start(self) -> None:
        """Recover interrupted jobs, sweep orphan payloads, start the workers.
        The registry must already be ``init()``-ed."""
        requeued = await self.pool.recover(phases=["staging"])
        swept = await self.payloads.sweep(self.registry)
        pending = await self.refresh_queue_depth()
        logger.info("queue: recovery requeued=%d orphan_payloads_removed=%d pending=%d "
                    "max_concurrent=%d", requeued, swept, pending, MAX_CONCURRENT)
        self.pool.start()

    async def stop(self) -> None:
        """Cancel the workers. Jobs mid-flight stay "processing" in the DB and
        are requeued by the next ``start()``."""
        await self.pool.stop()

    # ── Producer side (called from the submit endpoints) ──────────────────
    async def enqueue(self, job_id: str, payload: dict[str, Any]) -> int:
        """Persist ``payload``, publish the job to the workers, return queue depth.

        The row must have been registered in phase "staging" so no worker can
        claim it before the payload exists. Raises if the payload write fails,
        after marking the job failed — the caller turns that into a 500.
        """
        try:
            await self.payloads.write(job_id, payload)
        except Exception as exc:
            logger.error("enqueue: payload write failed for job_id=%s: %s", job_id, exc)
            await self.registry.set_error(job_id, f"could not persist job payload: {exc}")
            raise
        await self.registry.set_phase(job_id, "pending")
        self.pool.notify()
        return await self.refresh_queue_depth()

    async def refresh_queue_depth(self) -> int:
        """Re-read the pending count from the registry into the gauge."""
        counts = await self.registry.count_by_phase()
        pending = counts.get("pending", 0)
        job_queue_depth.set(pending)
        return pending

    # ── Consumer side (called by the pool) ────────────────────────────────
    async def handle_job(self, job: JobBase) -> dict:
        """WorkerPool handler: payload → runner → result. Raising fails the job."""
        # Restore the correlation ID so logs are traceable to the originating request
        request_id_var.set(job.metadata.get("request_id", "-"))
        jobs_in_flight.inc()
        keep_payload = False
        try:
            payload = await self.payloads.read(job.job_id)
            if payload is None:
                raise RuntimeError(
                    "payload missing — the job's input file was removed or never "
                    "written (usually a restart between enqueue and payload write)"
                )
            runner = _RUNNERS.get(job.metadata.get("type", ""), run_assess)
            return await runner(payload)
        except asyncio.CancelledError:
            # A shutdown mid-job: `docker compose stop` / `make up classifier`
            # sends SIGTERM, uvicorn runs the lifespan shutdown, and
            # `pool.stop()` cancels this task. The row is left in "processing"
            # on purpose so the next start's `recover()` requeues it — and the
            # worker that picks it up needs the INPUT to re-run it. Deleting the
            # payload here is what used to turn every graceful restart into a
            # "payload missing" failure for the jobs that were in flight.
            keep_payload = True
            raise
        finally:
            jobs_in_flight.dec()
            if not keep_payload:
                # Completed or failed: the input is no longer needed. (A hard
                # kill skips this block entirely, which is also fine — the
                # startup sweep only removes payloads of terminal or missing
                # rows, so a "processing" row's payload survives either way.)
                await self.payloads.delete(job.job_id)

    def _on_finish(self, job: JobBase, phase: str, elapsed: float, error: Optional[str]) -> None:
        job_type = job.metadata.get("type", "assess")
        jobs_total.labels(type=job_type, status=phase).inc()
        job_duration.labels(type=job_type).observe(elapsed)
        # Depth gauge is refreshed by the next enqueue/claim; keep the hook sync + cheap.


# ---------------------------------------------------------------------------
# Process-wide singletons
# ---------------------------------------------------------------------------
# One registry for the process, shared by the endpoints (which register and
# enqueue), the worker pool (which claims and completes), and the artifact
# sweeper (which prunes expired rows AND their directories). Its schema
# migration from the classifier's legacy layout (status → phase, add metadata
# column) runs idempotently in ``init()``, which ``main``'s lifespan calls.

jobs_registry = SqliteRegistry(DB_PATH)
queue = ClassifierQueue(jobs_registry)
sweeper = ArtifactSweeper(jobs_registry)
