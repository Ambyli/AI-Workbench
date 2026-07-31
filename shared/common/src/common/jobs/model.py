"""
Shared pydantic response models for `common.jobs`.

Kept intentionally generic-free at runtime: `metadata` is a plain `dict` so
FastAPI + pydantic serialize it into JSON without any type-parameter juggling.
Consumers define their own domain-specific `BaseModel` for the metadata shape
and pass it to `InMemoryRegistry.job(...)` / `SqliteRegistry.register(...)` —
the registry auto-calls `.model_dump()` before storing.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel


class JobBase(BaseModel):
    """Snapshot of one job at a point in time.

    Fields:
        job_id: 12-char hex identifier assigned at registration.
        phase: Freeform string; each consumer defines its own convention.
            Interceptor-api uses ``"cloning" | "capturing" | "cleaning_up"``;
            classifier uses ``"pending" | "processing" | "completed" | "failed"``.
        created_at: ISO-8601 UTC timestamp when the job was registered.
        updated_at: ISO-8601 UTC timestamp of the last phase / result / error
            transition. Equals ``created_at`` for a job that hasn't transitioned.
        elapsed_seconds: Wall-clock seconds since ``created_at``, computed at
            snapshot time from a monotonic clock. Present for ephemeral jobs;
            derived from ``created_at`` for persistent (SQLite-backed) jobs
            where the monotonic anchor is process-local and doesn't survive
            restarts.
        metadata: Consumer-defined dict describing the job. Typically the
            result of a pydantic ``BaseModel.model_dump()``.
        result: Populated on successful completion for persistent backends.
            Ephemeral backends never set this — a completed ephemeral job is
            already gone from the registry.
        error: Populated on failure for persistent backends. Same caveat as
            ``result`` for ephemeral.
    """

    job_id: str
    phase: str
    created_at: str
    updated_at: str
    elapsed_seconds: float
    metadata: dict[str, Any]
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None


class JobsListResponse(BaseModel):
    """Response body for ``GET /jobs``.

    ``max_concurrent`` and ``available`` are populated by backends that have
    a concurrency ceiling (like the in-memory backend fronted by a port pool);
    persistent backends leave them ``None``.
    """

    active_count: int
    jobs: list[JobBase]
    max_concurrent: Optional[int] = None
    available: Optional[int] = None
