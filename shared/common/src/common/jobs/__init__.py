"""
common.jobs — id-addressable job tracking with pluggable storage.

Three backends, same conceptual shape:

* ``InMemoryRegistry`` (from ``common.jobs.memory``) — sync, threading-based,
  ephemeral. Jobs auto-unregister when a context manager exits. For services
  whose captures are synchronous and short-lived, like ``interceptor``.

* ``SqliteRegistry`` (from ``common.jobs.sqlite``) — async, ``aiosqlite``-backed,
  persistent. Jobs survive process restarts and only leave the store on
  explicit ``delete``. Good for single-process services with light write
  concurrency and no need to query the jobs from outside the process (the
  classifier used it until its state moved to ``classifier-db``).

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

The two persistent backends also work as a durable FIFO work queue:
``claim_next(from_phase, to_phase)`` atomically hands the oldest waiting job
to exactly one caller (safe across tasks and processes),
``reset_phase(from, to)`` recovers jobs a crashed worker left mid-flight, and
``count_by_phase()`` drives queue-depth gauges.

* ``WorkerPool`` (from ``common.jobs.worker``) — N asyncio workers looping on
  ``claim_next``, with wake/poll, crash recovery, and an ``on_finish`` metrics
  hook. The consumer supplies ``async handler(JobBase) -> dict``.

* ``FilePayloadStore`` (from ``common.jobs.payloads``) — one JSON file per job
  for inputs too large for ``metadata`` (images, request bodies), so a queued
  job survives a restart. ``ai/classifier/jobs/queue.py`` wires both together.

* ``ConcurrencyLimit`` (from ``common.jobs.limits``) — a process-wide
  ``asyncio.Semaphore`` that is safe across event loops and reports its
  in-flight count and peak. What a job's inner work (model calls, OCR passes,
  per-job fan-out) is bounded with; the classifier holds two (model calls, OCR passes).

Optional deps: ``aiosqlite`` for ``SqliteRegistry``; ``asyncpg`` for
``PostgresRegistry``; ``fastapi`` for ``build_router``. Consumers who don't
use those don't pay the import cost — each submodule imports its optional
dep at top-level and will raise a clear ImportError if the consumer forgot
to depend on it.
"""

from .model import JobBase, JobsListResponse
from .memory import InMemoryRegistry, JobHandle

__all__ = [
    "JobBase",
    "JobsListResponse",
    "InMemoryRegistry",
    "JobHandle",
]
