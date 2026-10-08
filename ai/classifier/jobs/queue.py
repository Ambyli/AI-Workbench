"""Queue glue: wires the shared WorkerPool + FilePayloadStore to the runners.

The classifier uses an async job pattern so that POST /assess can return a
job ID immediately (202 Accepted) without blocking the HTTP connection for
the full duration of the model calls.

Division of labour:
  common.jobs.worker.WorkerPool      — N claim-and-handle loops, wake/poll, recovery
  common.jobs.payloads.FilePayloadStore — job inputs on disk so the queue survives restarts
  jobs.runners                       — the actual work (run_assess, run_reference)
  metrics.py                         — Prometheus objects shared with the endpoints
  this module                        — ClassifierQueue: enqueue, handle_job, lifecycle

Flow:
  1. ``api.assess`` registers a row (phase "staging") and
     calls ``queue.enqueue()``, which writes the payload, flips the row to
     "pending", and wakes a worker.
  2. One of CLASSIFIER_MAX_CONCURRENT pool workers atomically claims the row
     (→ "processing") and calls ``handle_job``, which reads the payload,
     dispatches on the row's ``metadata.type`` (``JOB_TYPES``: "assess" →
     ``run_assess``, "reference" → ``run_reference``), and deletes the
     payload once it finishes (a job cancelled by shutdown keeps its payload
     so the requeued row can be re-run). A row of a removed job type
     ("compare", "locate" — queued by an older container) fails with a
     message naming it. A "reference" job that fails for ANY reason —
     including a payload that never landed — marks its reference
     ``failed``; a cancelled one leaves it ``pending`` for the requeue.
     ``handle_job`` also tags every model call the job makes with its id and
     type (``llm.usage`` context vars) and, when the runner returns, adds
     the job's ``usage`` totals to the result.
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
from common.jobs.postgres import PostgresRegistry
from common.jobs.worker import WorkerPool

from config import MAX_CONCURRENT, PAYLOAD_DIR, WORKER_POLL_INTERVAL_S
from db import database
from jobs.runners import run_assess, run_reference
from llm import usage as llm_usage
from logger import logger
from metrics import job_duration, job_queue_depth, jobs_in_flight, jobs_total
from middleware import request_id_var
from references.store import reference_registry, refresh_gauges
from regions.sweeper import ArtifactSweeper

# The default job type (a row with no metadata.type is an assess), and every
# type this container runs. Rows of the removed types fail by name.
JOB_TYPE = "assess"
JOB_TYPES = frozenset({"assess", "reference"})


class ClassifierQueue:
    """Owns the payload store and worker pool for one registry.

    main.py builds one instance at import time and drives it from the
    FastAPI lifespan (``start`` / ``stop``) and the submit endpoints
    (``enqueue``).
    """

    def __init__(self, registry: PostgresRegistry) -> None:
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
    async def start(self, *, sweep_orphans: bool = True) -> None:
        """Recover interrupted jobs, sweep orphan payloads, start the workers.
        The registry must already be ``init()``-ed.

        ``sweep_orphans=False`` keeps every payload file: ``main`` passes it
        while the pre-Postgres SQLite file is still unmigrated, when a payload
        with no row is a queued job whose row has not been copied yet."""
        requeued = await self.pool.recover(phases=["staging"])
        swept = await self.payloads.sweep(self.registry) if sweep_orphans else 0
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
        job_type = job.metadata.get("type", JOB_TYPE)
        # Every model call this job makes — in every unit task the scheduler
        # spawns, which copy this context — is recorded against it (llm.usage).
        # Reset in `finally`: the pool awaits this handler in its own loop
        # task, which must not carry one job's id into the next.
        usage_tokens = (
            llm_usage.job_id_var.set(job.job_id),
            llm_usage.job_type_var.set(job_type),
        )
        try:
            payload = await self.payloads.read(job.job_id)
            if payload is None:
                raise RuntimeError(
                    "payload missing — the job's input file was removed or never "
                    "written (usually a restart between enqueue and payload write)"
                )
            if job_type not in JOB_TYPES:
                raise RuntimeError(
                    f"job type {job_type!r} no longer exists — /locate and "
                    "/assess/compare were removed; resubmit to POST /assess"
                )
            # Looked up on the module at call time so a test can replace them.
            if job_type == "reference":
                result = await run_reference(payload)
            else:
                result = await run_assess(payload)
            return await _with_usage(job.job_id, result)
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
        except Exception as exc:
            if job_type == "reference":
                await _fail_reference(job, exc)
            raise
        finally:
            llm_usage.job_type_var.reset(usage_tokens[1])
            llm_usage.job_id_var.reset(usage_tokens[0])
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


async def _with_usage(job_id: str, result: Any) -> Any:
    """Put the job's model-usage totals into its result as ``usage``.

    The same numbers ``GET /jobs/{id}/usage`` reports as ``totals`` (calls,
    errors, seconds, the five token totals, the models), plus ``usage_url``
    for the per-call rows — read from the ``llm_calls`` rows this job's calls
    have just written, so the result and the endpoint can never disagree. A
    failed job has no result; its usage is still at the URL.

    Never raises: a usage read that fails leaves the result without the block
    rather than failing a job whose actual work succeeded.
    """
    if not isinstance(result, dict):
        return result
    try:
        result["usage"] = await llm_usage.usage_store.job_usage(job_id)
    except Exception as exc:  # noqa: BLE001 — accounting never fails a job
        logger.warning("queue: could not read model usage for job %s: %s", job_id, exc)
    return result


async def _fail_reference(job: JobBase, exc: BaseException) -> None:
    """Mark a failed creation job's reference ``failed``. Never raises — the
    job is failing already, and the startup reconcile is the backstop."""
    reference_id = job.metadata.get("reference_id")
    if not reference_id:
        return
    try:
        if await reference_registry.fail(reference_id, f"creation job {job.job_id} failed: {exc}"):
            logger.warning("queue: reference %s failed with its job %s", reference_id, job.job_id)
        await refresh_gauges()
    except Exception as db_exc:  # pragma: no cover — DB unavailable
        logger.error("queue: could not mark reference %s failed: %s", reference_id, db_exc)


# ---------------------------------------------------------------------------
# Process-wide singletons
# ---------------------------------------------------------------------------
# One registry for the process, shared by the endpoints (which register and
# enqueue), the worker pool (which claims and completes), and the artifact
# sweeper (which prunes expired rows AND their directories). It borrows the
# one pool ``db.database`` owns (``pool=`` — the registry never creates or
# closes it), so the jobs table, ``reference_examples`` and ``llm_calls``
# share one connection budget in one database. Built at import time with no
# pool yet: ``main``'s lifespan runs ``database.init()`` and then
# ``jobs_registry.init()``, which creates the table idempotently.
#
# ``result_type="json"``: the result column is JSON, not JSONB. JSONB sorts
# object keys, and a job result's key order is part of what it says —
# ``per_criterion_scores`` lists the criteria in the order the request asked
# them, exactly as the SQLite text column used to keep it. ``metadata`` stays
# JSONB (``references.store.jobs_using`` tests it with ``?``).

jobs_registry = PostgresRegistry(pool=database, result_type="json")
queue = ClassifierQueue(jobs_registry)
sweeper = ArtifactSweeper(jobs_registry)
