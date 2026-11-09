"""
common.jobs — id-addressable job tracking with pluggable storage.

Three backends, same conceptual shape:

* ``InMemoryRegistry`` (from ``common.jobs.memory``) — sync, threading-based,
  ephemeral. Jobs auto-unregister when a context manager exits. For services
  whose captures are synchronous and short-lived, like ``interceptor``.

* ``SqliteRegistry`` (from ``common.jobs.sqlite``) — async, ``aiosqlite``-backed,
  persistent. Jobs survive process restarts and only leave the store on
  explicit ``delete``. Good for single-process services with light write
  concurrency and no need to query the jobs from outside the process.

* ``PostgresRegistry`` (from ``common.jobs.postgres``) — async, ``asyncpg``-backed,
  persistent, with a real connection pool and ``JSONB`` metadata/result. For
  services with multiple concurrent writers, a state store that needs to
  live in its own network segment, or operators who want to query the job
  table from ``psql`` (or federate it into Trino). It either owns its pool
  (``dsn=`` — the sandbox subsystem) or shares one the service owns
  (``pool=`` — the classifier, whose references and model-usage tables live
  in the same database).

All three backends produce ``JobBase`` snapshots (see ``common.jobs.model``)
and can be mounted onto a FastAPI app via ``common.jobs.router.build_router``.
Given ``terminal_phases`` (async registries only), its ``GET /jobs/{id}``
long-polls: ``?wait=<seconds>`` holds the request until the job reaches one
of those phases or ``max_wait_s`` (default 60) passes, so a caller waiting on
a job makes one or two requests instead of one every second or two — each of
which a metered gateway in front would count. The classifier mounts it that
way; without ``terminal_phases`` the route is unchanged.

The two persistent backends also work as a durable FIFO work queue:
``claim_next(from_phase, to_phase)`` atomically hands the oldest waiting job
to exactly one caller (safe across tasks and processes),
``reset_phase(from, to)`` recovers jobs a crashed worker left mid-flight, and
``count_by_phase()`` drives queue-depth gauges.

They can also bound that queue. ``register(metadata, phase, max_open=N)``
admits a job only while fewer than N are OPEN — every phase but completed /
failed / cancelled, so waiting and running alike — counting and inserting as
one atomic step (Postgres: one transaction behind an advisory lock; SQLite:
one ``BEGIN IMMEDIATE``), and otherwise raises ``QueueFull`` (``.open_jobs``,
``.limit``; from ``common.jobs.admission``, exported here) with nothing
written. ``count_open()`` is the same count without the atomicity — a cheap
pre-check. Without ``max_open`` nothing changes. The classifier turns
``QueueFull`` into a 429 with a ``Retry-After`` (``CLASSIFIER_QUEUE_LIMIT``).
``InMemoryRegistry`` has neither: its jobs live inside a caller's ``with``
block, and a service in front of it bounds them with its own port pool or
semaphore.

* ``WorkerPool`` (from ``common.jobs.worker``) — N asyncio workers looping on
  ``claim_next``, with wake/poll, crash recovery, and an ``on_finish`` metrics
  hook. The consumer supplies ``async handler(JobBase) -> dict``. Opt-in
  cancel awareness (``cancel_check_interval``): the handler runs as a child
  task, the worker re-reads the row each interval and cancels the child when
  the row turns ``cancelled`` (``registry.cancel``) or is deleted, its
  terminal writes are guarded (``from_phases``) so a late result never
  overwrites a cancel, and an ``on_cancel(job, gone)`` hook cleans up after
  the handler has unwound. That pool is what makes ``POST /jobs/{id}/cancel``
  stop the work on a persistent registry; the classifier runs one.

* ``FilePayloadStore`` (from ``common.jobs.payloads``) — one JSON file per job
  for inputs too large for ``metadata`` (images, request bodies), so a queued
  job survives a restart. ``ai/classifier/jobs/queue.py`` wires both together.

* ``ConcurrencyLimit`` (from ``common.jobs.limits``) — a process-wide
  ``asyncio.Semaphore`` that is safe across event loops and reports its
  in-flight count and peak. What a job's inner work (model calls, OCR passes,
  per-job fan-out) is bounded with; the classifier holds two (model calls, OCR passes).

How far along a running job is — the piece a submit-and-poll caller otherwise
cannot see:

* ``ProgressGauge`` (from ``common.jobs.progress``, stdlib only) — a job's
  stages, each worth a share of 100 (``percent = Σ completed weights +
  weight × index / length``), advanced at checkpoint moments by the module
  helpers ``checkpoint()`` / ``plan()`` / ``task()``, which find the job's
  gauge through a ContextVar and are no-ops outside one. ``checkpoint()``
  also writes the log line, so logging a milestone and moving the gauge are
  one call. ``async with gauge.running()`` flushes it to a store about once a
  second and writes the terminal ``done`` / ``failed`` state before the
  handler returns. Progress never fails a job. The fourth terminal state,
  ``cancelled``, is never the gauge's: a cancel skips its final flush, and
  the cleanup after a user's cancel writes it (``PostgresProgressStore.
  cancelled``).

* ``PostgresProgressStore`` (from ``common.jobs.progress_postgres``) — the
  snapshot in the job row's ``metadata.progress`` (so ``GET /jobs/{id}``
  shows it as-is) and one ``job_progress`` row per event, foreign-keyed to
  the jobs table ``ON DELETE CASCADE`` so the history goes with its job.
  ``build_progress_router`` (in ``common.jobs.router``) serves it as
  ``GET /jobs/{job_id}/progress``. The classifier is the first consumer.

Optional deps: ``aiosqlite`` for ``SqliteRegistry``; ``asyncpg`` for
``PostgresRegistry`` and ``PostgresProgressStore``; ``fastapi`` for
``build_router`` / ``build_progress_router``. Consumers who don't
use those don't pay the import cost — each submodule imports its optional
dep at top-level and will raise a clear ImportError if the consumer forgot
to depend on it.
"""

from .admission import QueueFull
from .model import JobBase, JobsListResponse
from .memory import InMemoryRegistry, JobHandle

__all__ = [
    "JobBase",
    "JobsListResponse",
    "InMemoryRegistry",
    "JobHandle",
    "QueueFull",
]
