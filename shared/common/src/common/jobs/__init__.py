"""
common.jobs — id-addressable job tracking with pluggable storage.

Two backends, same conceptual shape:

* ``InMemoryRegistry`` (from ``common.jobs.memory``) — sync, threading-based,
  ephemeral. Jobs auto-unregister when a context manager exits. For services
  whose captures are synchronous and short-lived, like ``interceptor-api``.

* ``SqliteRegistry`` (from ``common.jobs.sqlite``) — async, ``aiosqlite``-backed,
  persistent. Jobs survive process restarts and only leave the store on
  explicit ``delete``. For services that kick off work and let callers poll
  for completion, like ``classifier``.

Both backends produce ``JobBase`` snapshots (see ``common.jobs.model``) and
can be mounted onto a FastAPI app via ``common.jobs.router.build_router``.

Optional deps: ``aiosqlite`` for ``SqliteRegistry``; ``fastapi`` for
``build_router``. Consumers who don't use those don't pay the import cost —
each submodule imports its optional dep at top-level and will raise a clear
ImportError if the consumer forgot to depend on it.
"""

from .model import JobBase, JobsListResponse
from .memory import InMemoryRegistry, JobHandle

__all__ = [
    "JobBase",
    "JobsListResponse",
    "InMemoryRegistry",
    "JobHandle",
]
