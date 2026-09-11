"""Queue glue: wires the shared WorkerPool + FilePayloadStore to the runners.

The classifier uses an async job pattern so that POST /assess and
POST /assess/compare can return a job ID immediately (202 Accepted) without
blocking the HTTP connection for the full duration of the LLM call.

Division of labour:
  common.jobs.worker.WorkerPool      — N claim-and-handle loops, wake/poll, recovery
  common.jobs.payloads.FilePayloadStore — job inputs on disk so the queue survives restarts
  runners.py                         — the actual assess / compare work
  metrics.py                         — Prometheus objects shared with main.py
  this module                        — ClassifierQueue: enqueue, handle_job, lifecycle

Flow:
  1. main.py registers a row (phase "staging") and calls ``queue.enqueue()``,
     which writes the payload, flips the row to "pending", and wakes a worker.
  2. One of CLASSIFIER_MAX_CONCURRENT pool workers atomically claims the row
     (→ "processing") and calls ``handle_job``, which reads the payload,
     dispatches on ``metadata.type`` to a runner, and deletes the payload.
  3. The pool persists the result / error (→ "completed" | "failed") and
     calls ``_on_finish`` for metrics.
  4. Callers poll GET /jobs/{job_id}.

Why the DB is the queue, not an asyncio.Queue: an in-memory queue loses every
pending job on restart and can't be shared across processes. With the
registry as the queue, ``start()`` requeues rows a previous process left in
"processing", and the payload on disk means the work can actually be redone.
"""

from typing import Any, Optional

from common.jobs.model import JobBase
from common.jobs.payloads import FilePayloadStore
from common.jobs.sqlite import SqliteRegistry
from common.jobs.worker import WorkerPool

from config import MAX_CONCURRENT, PAYLOAD_DIR, WORKER_POLL_INTERVAL_S
from logger import logger
from metrics import job_duration, job_queue_depth, jobs_in_flight, jobs_total
from middleware import request_id_var
from runners import run_assess, run_compare

_RUNNERS = {"assess": run_assess, "compare": run_compare}


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
        try:
            payload = await self.payloads.read(job.job_id)
            if payload is None:
                raise RuntimeError(
                    "payload missing — the job's input file was removed or never "
                    "written (usually a restart between enqueue and payload write)"
                )
            runner = _RUNNERS.get(job.metadata.get("type", ""), run_assess)
            return await runner(payload)
        finally:
            jobs_in_flight.dec()
            await self.payloads.delete(job.job_id)

    def _on_finish(self, job: JobBase, phase: str, elapsed: float, error: Optional[str]) -> None:
        job_type = job.metadata.get("type", "assess")
        jobs_total.labels(type=job_type, status=phase).inc()
        job_duration.labels(type=job_type).observe(elapsed)
        # Depth gauge is refreshed by the next enqueue/claim; keep the hook sync + cheap.
