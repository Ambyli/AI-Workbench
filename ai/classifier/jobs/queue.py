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
  0. With CLASSIFIER_QUEUE_LIMIT set, ``api.assess`` / ``api.references``
     first ask ``queue.check_room()`` — before the body is read, so a full
     queue costs no URL fetch — and register through ``queue.admit()``; both
     raise ``QueueFullError``, which ``main`` answers 429 + Retry-After. See
     Queue limit below.
  1. ``api.assess`` registers a row (phase "staging", ``admit``) and
     calls ``queue.enqueue()``, which writes the payload, flips the row to
     "pending" (only from "staging" — a cancel or a delete that landed
     mid-submit is never undone), and wakes a worker.
  2. One of CLASSIFIER_MAX_CONCURRENT pool workers atomically claims the row
     (→ "processing") and calls ``handle_job``, which reads the payload,
     dispatches on the row's ``metadata.type`` (``JOB_TYPES``: "assess" →
     ``run_assess``, "reference" → ``run_reference``), and deletes the
     payload once it finishes (a job whose runner was cancelled keeps its
     payload — see Cancel below). A row of a removed job type
     ("compare", "locate" — queued by an older container) fails with a
     message naming it. A "reference" job that fails for ANY reason —
     including a payload that never landed — marks its reference
     ``failed``; a cancelled one leaves that to whoever cancelled it.
     ``handle_job`` also tags every model call the job makes with its id and
     type (``llm.usage`` context vars) and, when the runner returns, adds
     the job's ``usage`` totals to the result.
  3. The pool persists the result / error (→ "completed" | "failed") —
     only while the row is still "processing" — and calls ``_on_finish``
     for metrics.
  4. Callers poll GET /jobs/{job_id} — and GET /jobs/{job_id}/progress for
     how far along it is.

Cancel (``POST /jobs/{id}/cancel``, ``common.jobs.router``). The registry
flips the row to "cancelled" at once, in one transaction that refuses a
finished job. What happens next depends on the phase it was in:
  * "staging" / "pending" — no worker owns the job: the router's
    ``on_cancel`` hook (``on_cancel_request``) runs ``finish_cancelled``
    there and then. A pending row in "cancelled" is never claimed.
  * "processing" — the pool owns it. ``WorkerPool`` runs ``handle_job`` as a
    child task and re-reads the row every ``CANCEL_CHECK_INTERVAL_S``; on
    "cancelled" (or the row gone — ``DELETE /jobs/{id}`` on a running job)
    it cancels the child, waits for it to unwind, writes nothing (its
    terminal writes are guarded, so a runner that finishes a moment after
    the cancel cannot overwrite it either), and calls ``_on_pool_cancel`` →
    ``finish_cancelled`` in the worker task. The ``CancelledError`` reaches
    the runner at its next await: the model request is dropped, and its
    worker, model and OCR slots are free within about a second.
``finish_cancelled`` is the ONE cleanup, whoever calls it: the payload, the
``cancelled`` progress state, a reference job's reference, and — for a
deleted job — the usage rows and artifacts a runner wrote after the DELETE
hook ran. ``handle_job`` itself never decides it: to the handler a shutdown
and a cancel are the same ``CancelledError``.

Queue limit (CLASSIFIER_QUEUE_LIMIT, ``config.QUEUE_LIMIT``; 0 = none). The
limit counts OPEN jobs — every row not completed / failed / cancelled, so
staging, pending and processing alike — which is what lets it narrow
concurrency as well as cap the wait: at the limit of MAX_CONCURRENT nothing
waits, below it some workers stay idle, above it ``limit − MAX_CONCURRENT``
jobs may wait. Two checks, both raising ``QueueFullError`` (never an
HTTPException — ``jobs/`` does not import FastAPI; ``main``'s exception
handler answers 429, counts and logs it):
  * ``check_room`` — the cheap pre-check, the first line of each submit
    handler: one ``count_open()``, no lock. It spares a full queue the body,
    the URL fetches and the SVG image fetches; it cannot decide a race.
  * ``admit`` — the authoritative one, in place of ``registry.register``:
    ``register(..., "staging", max_open=limit)`` counts and inserts in one
    transaction under an advisory lock, so the last slot goes to exactly one
    submit. A staging row counts from the moment it exists, so the slot is
    taken at registration, before the payload is written.
A cancelled or deleted job frees its slot at once; rows a restart requeues
keep theirs, so a container that comes back above the limit refuses until
they drain. The refusal's ``Retry-After`` is ``retry_after_s`` — an estimate
from the run times of the last ``QUEUE_RETRY_AFTER_HISTORY`` finished jobs
(``_on_finish`` keeps them), see ``retry_after_estimate``.

Progress (``common.jobs.progress``). ``enqueue`` writes the ``queued``
snapshot (0%, attempt 0) while the row is still "staging", so no worker can
have claimed it yet and a running gauge can never be overwritten by it.
``handle_job`` runs the runner inside ``ProgressGauge(...).running()`` with
the job type's ``STAGES`` and the next attempt number: the runners, the
scheduler, the llm evaluator and the pipeline then ``plan`` / ``task`` /
``checkpoint`` through the gauge the context carries, and the gauge's final
flush (``done`` at 100, or ``failed``) lands BEFORE the pool's
``set_result`` / ``set_error`` — the terminal snapshot is never newer than
the phase. ``start()`` puts the snapshot of every job ``recover()`` sent back
to the queue back to ``queued``. Progress writes never fail a job: every
store error is logged and swallowed.

Why the DB is the queue, not an asyncio.Queue: an in-memory queue loses every
pending job on restart and can't be shared across processes. With the
registry as the queue, ``start()`` requeues rows a previous process left in
"processing", and the payload on disk means the work can actually be redone.

The three process-wide singletons at the bottom — the registry, the queue and
the artifact sweeper — live here rather than in ``main`` so the endpoint
modules can reach them without importing the app they are mounted on.
"""

import asyncio
import math
import statistics
from collections import deque
from typing import Any, Iterable, Optional

from pydantic import BaseModel

from common.jobs import QueueFull
from common.jobs.model import JobBase
from common.jobs.payloads import FilePayloadStore
from common.jobs.postgres import PostgresRegistry
from common.jobs.progress import ProgressGauge, Stage, mark_queued, next_attempt
from common.jobs.progress_postgres import PostgresProgressStore
from common.jobs.worker import WorkerPool

from config import (
    CANCEL_CHECK_INTERVAL_S,
    MAX_CONCURRENT,
    PAYLOAD_DIR,
    PROGRESS_FLUSH_INTERVAL_S,
    QUEUE_LIMIT,
    QUEUE_RETRY_AFTER_DEFAULT_S,
    QUEUE_RETRY_AFTER_HISTORY,
    QUEUE_RETRY_AFTER_MAX_S,
    QUEUE_RETRY_AFTER_MIN_S,
    WORKER_POLL_INTERVAL_S,
)
from db import database
from jobs.runners import run_assess, run_reference
from llm import usage as llm_usage
from logger import logger
from metrics import (
    job_duration,
    job_queue_depth,
    job_queue_limit,
    jobs_in_flight,
    jobs_open,
    jobs_total,
)
from middleware import request_id_var
from references.store import reference_files, reference_registry, refresh_gauges
from regions.sweeper import ArtifactSweeper, delete_artifacts_for_job

# The default job type (a row with no metadata.type is an assess), and every
# type this container runs. Rows of the removed types fail by name.
JOB_TYPE = "assess"
JOB_TYPES = frozenset({"assess", "reference"})

# Each job type's progress stages (common.jobs.progress.Stage: name, title,
# weight — weights are shares of 100). The names are what the code checkpoints
# against: jobs.runners plans and loads "load", analysis.scheduler plans and
# runs "units" (one per (criterion, item) unit; an llm unit with boxes is two
# steps, scored then located), analysis.pipeline enters "artifacts", and the
# reference runner's describe + render are "finalize". "units" dominates
# because that is where the model calls are. A reference job whose criteria
# were all supplied skips units and artifacts — entering finalize completes
# them.
STAGES: dict[str, tuple[Stage, ...]] = {
    "assess": (
        Stage("load", "Loading documents", 10),
        Stage("units", "Evaluating criteria", 85),
        Stage("artifacts", "Writing artifacts", 5),
    ),
    "reference": (
        Stage("load", "Loading documents", 5),
        Stage("units", "Evaluating criteria", 70),
        Stage("artifacts", "Writing artifacts", 5),
        Stage("finalize", "Building reference", 20),
    ),
}

# The phases a job ends in — main's jobs router has the same three as its
# ``terminal_phases``, and common.jobs' registries treat every OTHER phase as
# open (``count_open``, ``register(max_open=)``). Here: the open-jobs gauge,
# read from ``count_by_phase``.
TERMINAL_PHASES = ("completed", "failed", "cancelled")

# The job outcomes whose run time feeds the Retry-After estimate: a cancelled
# job's elapsed is when someone stopped it, not how long a job takes.
_TIMED_OUTCOMES = frozenset({"completed", "failed"})


class QueueFullError(Exception):
    """A submit refused because CLASSIFIER_QUEUE_LIMIT jobs are already open.

    Raised by ``ClassifierQueue.check_room`` / ``admit`` before any job row
    exists, and answered by ``main``'s exception handler: a 429 whose body is
    FastAPI's ``{"detail": …}`` shape (``detail`` below) and whose
    ``Retry-After`` header is ``retry_after_s``. Plain ``Exception``, not an
    ``HTTPException`` — ``jobs/`` does not import FastAPI.
    """

    def __init__(self, job_type: str, open_jobs: int, limit: int, retry_after_s: int) -> None:
        self.job_type = job_type
        self.open_jobs = int(open_jobs)
        self.limit = int(limit)
        self.retry_after_s = int(retry_after_s)
        super().__init__(self.detail)

    @property
    def detail(self) -> str:
        """The 429's ``detail``: what is full, the cap, and the wait."""
        return (
            f"the classifier queue is full: {self.open_jobs} jobs waiting or running, the "
            f"CLASSIFIER_QUEUE_LIMIT cap ({self.limit}). Retry after {self.retry_after_s} s."
        )


def retry_after_estimate(
    durations: Iterable[float], open_jobs: int, *, limit: int, workers: int
) -> int:
    """The ``Retry-After`` (whole seconds) for a submit refused at ``limit``.

    ``durations`` are recent jobs' run times (claim → result, seconds). The
    wait is how long until enough slots free up: the median run time × the
    slots needed (``open_jobs − limit + 1`` — one, unless a restart or a
    lowered limit left more open than the limit) ÷ the jobs that finish in
    parallel (``min(limit, workers)``), rounded up and clamped to
    [QUEUE_RETRY_AFTER_MIN_S, QUEUE_RETRY_AFTER_MAX_S]. No durations yet (no
    job finished since startup), or no limit, gives
    QUEUE_RETRY_AFTER_DEFAULT_S — the edge's own 503 value. Pure: the same
    inputs, the same answer.
    """
    samples = [float(d) for d in durations]
    if not samples or limit < 1:
        return QUEUE_RETRY_AFTER_DEFAULT_S
    busy = max(1, min(int(limit), int(workers)))
    excess = max(1, int(open_jobs) - int(limit) + 1)
    wait = math.ceil(statistics.median(samples) * excess / busy)
    return max(QUEUE_RETRY_AFTER_MIN_S, min(QUEUE_RETRY_AFTER_MAX_S, wait))


class ClassifierQueue:
    """Owns the payload store and worker pool for one registry.

    main.py builds one instance at import time and drives it from the
    FastAPI lifespan (``start`` / ``stop``) and the submit endpoints
    (``check_room``, ``admit``, ``enqueue``).
    """

    def __init__(self, registry: PostgresRegistry) -> None:
        self.registry = registry
        # CLASSIFIER_QUEUE_LIMIT (0 = none). An attribute rather than a
        # module constant read in place so a test can set it on the one
        # queue the app uses.
        self.queue_limit = QUEUE_LIMIT
        # The run times (seconds, claim → result) of the last
        # QUEUE_RETRY_AFTER_HISTORY completed / failed jobs — what a
        # refusal's Retry-After is estimated from. In memory: a restart
        # starts over at the default.
        self.durations: deque[float] = deque(maxlen=QUEUE_RETRY_AFTER_HISTORY)
        self.payloads = FilePayloadStore(PAYLOAD_DIR)
        # The snapshot goes into the registry's own rows (metadata.progress)
        # and the history into job_progress beside them, on the same pool.
        # main's lifespan creates the table, after the jobs table it references.
        self.progress = PostgresProgressStore(pool=database)
        # Cancel-aware: each running job's row is re-read every
        # CANCEL_CHECK_INTERVAL_S, and a cancelled or deleted one has its
        # handle_job cancelled; _on_pool_cancel cleans up after it unwound.
        self.pool = WorkerPool(
            registry,
            self.handle_job,
            concurrency=MAX_CONCURRENT,
            poll_interval=WORKER_POLL_INTERVAL_S,
            on_finish=self._on_finish,
            cancel_check_interval=CANCEL_CHECK_INTERVAL_S,
            on_cancel=self._on_pool_cancel,
            name="classifier-worker",
            logger=logger,
        )

    # ── Lifecycle (called from main.lifespan) ─────────────────────────────
    async def start(self) -> None:
        """Recover interrupted jobs, sweep orphan payloads, start the workers.
        The registry must already be ``init()``-ed."""
        requeued = await self.pool.recover(phases=["staging"])
        try:
            # A requeued row's snapshot still says "running" at whatever
            # percent the dead process last flushed; put it back to queued.
            await self.progress.requeued()
        except Exception as exc:  # noqa: BLE001 — progress never stops a start
            logger.warning("queue: could not reset the progress of requeued jobs: %s", exc)
        swept = await self.payloads.sweep(self.registry)
        pending, open_jobs = await self._refresh_gauges()
        limit = self.queue_limit
        job_queue_limit.set(limit)
        logger.info("queue: recovery requeued=%d orphan_payloads_removed=%d pending=%d "
                    "max_concurrent=%d queue_limit=%d", requeued, swept, pending,
                    self.pool.concurrency, limit)
        if 0 < limit < self.pool.concurrency:
            logger.warning(
                "queue: CLASSIFIER_QUEUE_LIMIT=%d is below CLASSIFIER_MAX_CONCURRENT=%d — "
                "at most %d jobs run at once; %d workers stay idle",
                limit, self.pool.concurrency, limit, self.pool.concurrency - limit,
            )
        if limit and open_jobs >= limit:
            logger.info(
                "queue: %d jobs are already open (waiting or running), at or over "
                "CLASSIFIER_QUEUE_LIMIT=%d — submits answer 429 until it drains",
                open_jobs, limit,
            )
        self.pool.start()

    async def stop(self) -> None:
        """Cancel the workers. Jobs mid-flight stay "processing" in the DB and
        are requeued by the next ``start()``."""
        await self.pool.stop()

    # ── Producer side (called from the submit endpoints) ──────────────────
    async def check_room(self, job_type: str) -> None:
        """The fast pre-check: raise ``QueueFullError`` when the queue is
        already at CLASSIFIER_QUEUE_LIMIT. The first line of each submit
        handler, so a full queue answers before the body is read, a URL
        fetched or an SVG image inlined. One ``count_open()`` — no lock, so
        it can let two racing submits through; ``admit`` decides those.
        Returns at once, with no query, when there is no limit."""
        limit = self.queue_limit
        if limit <= 0:
            return
        open_jobs = await self.registry.count_open()
        if open_jobs >= limit:
            raise self._full(job_type, open_jobs, limit)

    async def admit(self, metadata: BaseModel | dict[str, Any], job_type: str) -> str:
        """Register the job's row in phase "staging" and return its id — or
        raise ``QueueFullError`` with no row written when CLASSIFIER_QUEUE_LIMIT
        jobs are already open. The authoritative check: the registry counts
        and inserts in one transaction under an advisory lock
        (``register(max_open=)``), so the last slot goes to one submit only.
        With no limit it is the plain register it replaced."""
        limit = self.queue_limit
        try:
            return await self.registry.register(
                metadata, initial_phase="staging", max_open=limit if limit > 0 else None
            )
        except QueueFull as exc:
            raise self._full(job_type, exc.open_jobs, exc.limit) from None

    def retry_after_s(self, open_jobs: int) -> int:
        """The Retry-After for a refusal with ``open_jobs`` open: the estimate
        (``retry_after_estimate``) from the recent run times this queue has
        seen, its limit and its worker count."""
        return retry_after_estimate(
            self.durations, open_jobs, limit=self.queue_limit, workers=self.pool.concurrency
        )

    def _full(self, job_type: str, open_jobs: int, limit: int) -> QueueFullError:
        return QueueFullError(job_type, open_jobs, limit, self.retry_after_s(open_jobs))

    async def enqueue(self, job_id: str, payload: dict[str, Any]) -> int:
        """Persist ``payload``, publish the job to the workers, return queue depth.

        The row must have been registered in phase "staging" so no worker can
        claim it before the payload exists. Raises if the payload write fails,
        after marking the job failed — the caller turns that into a 500.

        Both writes are guarded to "staging". A cancel (or a DELETE) can land
        between the register and the flip — the job id is not even returned
        yet, but a caller listing ``GET /jobs`` can see it — and an unguarded
        flip would put a cancelled job back in the queue. When the flip
        misses, the job was cancelled or deleted mid-submit: this runs
        ``finish_cancelled`` for what it just wrote (the payload, the queued
        snapshot) — the router's hook may have run BEFORE they existed — and
        returns the queue depth as usual; the caller answers 202 with the job
        id, and the job reads ``cancelled`` (or 404).
        """
        try:
            await self.payloads.write(job_id, payload)
        except Exception as exc:
            logger.error("enqueue: payload write failed for job_id=%s: %s", job_id, exc)
            if not await self.registry.set_error(
                job_id, f"could not persist job payload: {exc}", from_phases=("staging",)
            ):
                logger.warning("enqueue: job_id=%s was cancelled or deleted mid-submit", job_id)
            raise
        # Before the flip to "pending": while the row is "staging" no worker
        # can claim it, so this 0% can never land on top of a running gauge.
        job_type = payload.get("type", JOB_TYPE) if isinstance(payload, dict) else JOB_TYPE
        await mark_queued(self.progress, job_id, stages=_stages(job_type), logger=logger)
        if not await self.registry.set_phase(job_id, "pending", from_phases=("staging",)):
            job = await self.registry.get(job_id)
            logger.info("enqueue: job_id=%s was %s mid-submit — not queued", job_id,
                        "deleted" if job is None else f"moved to {job.phase!r}")
            # A deleted row has no metadata left; the payload names the type
            # and, for a creation job, the reference.
            metadata = job.metadata if job is not None else {
                "type": job_type,
                "reference_id": payload.get("reference_id") if isinstance(payload, dict) else None,
            }
            await self.finish_cancelled(job_id, metadata, gone=job is None)
            return await self.refresh_queue_depth()
        self.pool.notify()
        return await self.refresh_queue_depth()

    async def refresh_queue_depth(self) -> int:
        """Re-read the phase counts into the gauges; return the pending count
        (the queue depth)."""
        pending, _ = await self._refresh_gauges()
        return pending

    async def _refresh_gauges(self) -> tuple[int, int]:
        """One ``count_by_phase`` read into both gauges: the depth (pending
        only) and the open jobs (every phase not terminal — what
        CLASSIFIER_QUEUE_LIMIT counts). Returns ``(pending, open)``."""
        counts = await self.registry.count_by_phase()
        pending = counts.get("pending", 0)
        open_jobs = sum(n for phase, n in counts.items() if phase not in TERMINAL_PHASES)
        job_queue_depth.set(pending)
        jobs_open.set(open_jobs)
        return pending, open_jobs

    # ── Cancel ────────────────────────────────────────────────────────────
    async def on_cancel_request(self, job_id: str, was_phase: str) -> None:
        """The jobs router's ``on_cancel`` hook: ``POST /jobs/{id}/cancel``
        succeeded and the row now says "cancelled".

        A "staging" / "pending" job has no worker, so it is finished here and
        counted (``jobs_total{status="cancelled"}``). A "processing" one is
        left alone: its worker sees the phase within
        ``CANCEL_CHECK_INTERVAL_S``, stops the runner, and the pool's
        ``on_finish`` / ``on_cancel`` count and clean up — after the runner
        can no longer write. The router swallows anything raised here.
        """
        if was_phase not in ("staging", "pending"):
            return
        try:
            job = await self.registry.get(job_id)
        except Exception as exc:  # noqa: BLE001 — the payload can still go
            logger.warning("cancel: could not read job %s: %s", job_id, exc)
            metadata, gone = {}, False
        else:
            metadata, gone = (job.metadata if job is not None else {}), job is None
        logger.info("cancel: job_id=%s cancelled while %s", job_id, was_phase)
        await self.finish_cancelled(job_id, metadata, gone=gone)
        jobs_total.labels(type=metadata.get("type", JOB_TYPE), status="cancelled").inc()

    async def _on_pool_cancel(self, job: JobBase, gone: bool) -> None:
        """WorkerPool ``on_cancel``: a running job was cancelled (or its row
        deleted) and ``handle_job`` has unwound — runs in the worker task,
        never inside the cancelled one. The job's request id is restored
        around the cleanup so its log lines trace to the request that queued
        it, as the job's own did."""
        token = request_id_var.set(job.metadata.get("request_id", "-"))
        try:
            await self.finish_cancelled(job.job_id, job.metadata, gone=gone)
        finally:
            request_id_var.reset(token)

    async def finish_cancelled(
        self, job_id: str, metadata: Optional[dict[str, Any]], *, gone: bool
    ) -> None:
        """Everything a cancelled job leaves behind, once nothing can write
        for it any more. THE one cleanup — the router's hook (a job no worker
        owns), the pool (a running one) and ``enqueue`` (a submit the cancel
        overtook) all call it, possibly more than once for one job, so every
        step is idempotent. Never raises: each step is logged and swallowed.

        1. The payload (the input is never needed again).
        2. While the row exists, its progress snapshot → ``cancelled``
           (``PostgresProgressStore.cancelled``: where it stopped, plus one
           history event). The job's ``llm_calls`` rows stay readable at
           ``GET /jobs/{id}/usage``, and its partial artifact directory is
           swept with the row after JOB_TTL_HOURS — both as for a job that
           failed.
        3. A "reference" creation job's reference → ``failed`` (a ``ready``
           one is never demoted), its files removed — the pair
           ``references.store.reconcile`` uses — and the gauges refreshed.
        4. ``gone`` (the row was deleted while the job ran): the usage rows
           and artifacts again (``delete_job_side_data``) — the DELETE's own
           hook ran while the runner could still write them. A row that
           vanished since the cancel (step 2 finds no row: route-policy
           cancels and then deletes at once, and the worker may have seen
           the cancel first) counts as gone too.
        5. The queue-depth gauge.
        """
        metadata = metadata if isinstance(metadata, dict) else {}
        try:
            await self.payloads.delete(job_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cancel: could not remove the payload of job %s: %s", job_id, exc)
        if not gone:
            try:
                gone = not await self.progress.cancelled(job_id)
            except Exception as exc:  # noqa: BLE001 — progress never fails anything
                logger.warning("cancel: could not mark the progress of job %s cancelled: %s",
                               job_id, exc)
        if metadata.get("type") == "reference":
            await _cancel_reference(job_id, metadata.get("reference_id"))
        if gone:
            await delete_job_side_data(job_id)
        try:
            await self.refresh_queue_depth()
        except Exception as exc:  # noqa: BLE001
            logger.warning("cancel: could not refresh the queue depth: %s", exc)

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
            # This run's gauge: every plan / task / checkpoint below (in the
            # runner, the scheduler's unit tasks, the to_thread loaders)
            # finds it through the context. Its final flush — done, or failed
            # with the exception — completes before this handler returns, so
            # before the pool writes the result or the error.
            gauge = ProgressGauge(
                job.job_id,
                _stages(job_type),
                store=self.progress,
                logger=logger,
                flush_interval=PROGRESS_FLUSH_INTERVAL_S,
                attempt=await next_attempt(self.progress, job.job_id, logger=logger),
            )
            async with gauge.running():
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
            # Two things cancel this task, and keeping the payload is right
            # for both — so this branch never asks which:
            #   * A shutdown mid-job: `docker compose stop` / `make up
            #     classifier` sends SIGTERM, uvicorn runs the lifespan
            #     shutdown, and `pool.stop()` cancels the worker, which
            #     cancels this task. The row is left in "processing" on
            #     purpose so the next start's `recover()` requeues it — and
            #     the worker that picks it up needs the INPUT to re-run it.
            #     Deleting the payload here is what used to turn every
            #     graceful restart into a "payload missing" failure for the
            #     jobs that were in flight.
            #   * A user's cancel (or a DELETE) of the running job: the pool's
            #     cancel check cancels this task, and once it has unwound,
            #     `finish_cancelled` (via `_on_pool_cancel`, in the worker
            #     task) removes the payload with the rest. Cleaning up here
            #     instead would run inside a cancelled task — see
            #     common.jobs.worker on why that is the wrong place.
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
        # phase: completed | failed | cancelled (a running job the pool
        # stopped for a cancel or a delete — counted here, not by the router).
        job_type = job.metadata.get("type", "assess")
        jobs_total.labels(type=job_type, status=phase).inc()
        job_duration.labels(type=job_type).observe(elapsed)
        if phase in _TIMED_OUTCOMES:
            # What the next refusal's Retry-After is estimated from.
            self.durations.append(elapsed)
        # Depth gauge is refreshed by the next enqueue/claim; keep the hook sync + cheap.


def _stages(job_type: str) -> tuple[Stage, ...]:
    """A job type's progress stages; a removed type (which fails at once)
    gets the assess stages, so its gauge still has somewhere to say so."""
    return STAGES.get(job_type, STAGES[JOB_TYPE])


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


async def _cancel_reference(job_id: str, reference_id: Optional[str]) -> None:
    """A cancelled creation job's reference → ``failed`` and its files gone
    (only a ``pending`` one moves — ``ReferenceRegistry.fail`` — so a
    reference readied a moment before the cancel stays ready). Never raises;
    the startup reconcile is the backstop."""
    if not reference_id:
        return
    try:
        if await reference_registry.fail(reference_id, f"creation job {job_id} was cancelled"):
            reference_files.delete(reference_id)
            logger.warning("cancel: reference %s failed — its creation job %s was cancelled",
                           reference_id, job_id)
        await refresh_gauges()
    except Exception as exc:  # noqa: BLE001 — DB unavailable
        logger.error("cancel: could not mark reference %s failed: %s", reference_id, exc)


async def delete_job_side_data(job_id: str) -> None:
    """What hangs off a job besides its row: the artifact directory, then the
    model-usage rows. ``main`` wires it as the jobs router's ``on_delete``
    hook (DELETE /jobs/{id}), and ``finish_cancelled`` runs it again for a
    job deleted while it ran — its runner may have written both after the
    hook.

    Each step is guarded on its own and logged — a filesystem error on the
    directory must not leave the usage rows behind — so it never raises. As
    the DELETE hook it runs before the router's own row check, so a DELETE on
    a job the sweeper already expired still removes its usage, then answers
    404. (``job_progress`` rows need nothing: they cascade with the row.)
    """
    try:
        delete_artifacts_for_job(job_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("delete: could not remove artifacts for job %s: %s", job_id, exc)
    try:
        removed = await llm_usage.usage_store.delete_job(job_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("delete: could not remove model usage rows for job %s: %s", job_id, exc)
        return
    if removed:
        logger.info("delete: removed %d model usage row(s) for job %s", removed, job_id)


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
# them, and JSON keeps that order as written. ``metadata`` stays
# JSONB (``references.store.jobs_using`` tests it with ``?``).

jobs_registry = PostgresRegistry(pool=database, result_type="json")
queue = ClassifierQueue(jobs_registry)
sweeper = ArtifactSweeper(jobs_registry)
