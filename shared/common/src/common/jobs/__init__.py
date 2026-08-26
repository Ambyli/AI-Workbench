"""
common.jobs — id-addressable job tracking with pluggable storage.

Three backends, same conceptual shape:

* ``InMemoryRegistry`` (from ``common.jobs.memory``) — sync, threading-based,
  ephemeral. Jobs auto-unregister when a context manager exits. For services
  whose captures are synchronous and short-lived, like ``interceptor``.

* ``SqliteRegistry`` (from ``common.jobs.sqlite``) — async, ``aiosqlite``-backed,
  persistent. Jobs survive process restarts and only leave the store on
  explicit ``delete``. Good for single-process services with light write
  concurrency, like ``classifier``.

* ``PostgresRegistry`` (from ``common.jobs.postgres``) — async, ``asyncpg``-backed,
  persistent, with a real connection pool and ``JSONB`` metadata/result. For
  services with multiple concurrent writers, a state store that needs to
  live in its own network segment, or operators who want to query the job
  table from ``psql``. Used by the sandbox subsystem.

All three backends produce ``JobBase`` snapshots (see ``common.jobs.model``)
and can be mounted onto a FastAPI app via ``common.jobs.router.build_router``.

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
