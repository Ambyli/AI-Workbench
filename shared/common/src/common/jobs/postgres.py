"""
PostgresRegistry — async, asyncpg-backed, persistent job tracking.

Same kickoff-then-poll shape as ``SqliteRegistry`` (register a job, mutate
its phase/result/error over time, callers poll ``GET /jobs/{id}``), but
backed by a real Postgres instance with a connection pool. This is the
right shape for services that:

* have concurrent writers that would collide on a single SQLite file
  (SQLite serializes writes; Postgres does not),
* want ``metadata``/``result`` to be queryable as JSON (``JSONB``, not
  ``TEXT``), so operators can grep the job table from ``psql``,
* need the state store to sit in its own network segment for isolation
  (the sandbox subsystem does this — see ``ai/SANDBOX.md``).

The public interface matches ``SqliteRegistry`` exactly, so
``common.jobs.router.build_router`` mounts it with no changes.

Optional dep: ``asyncpg``. If a consumer imports this module without
having asyncpg installed, they get a clean ``ImportError`` at import time.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

import asyncpg
from pydantic import BaseModel

from .model import JobBase, JobsListResponse


_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id         TEXT PRIMARY KEY,
    phase      TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    metadata   JSONB NOT NULL DEFAULT '{}'::jsonb,
    result     JSONB,
    error      TEXT
);
"""

_INDEX = "CREATE INDEX IF NOT EXISTS jobs_updated_at ON jobs (updated_at DESC);"


class PostgresRegistry:
    """Persistent job registry backed by a single Postgres table.

    Args:
        dsn: Postgres DSN (e.g. ``postgresql://user:pw@host:5432/dbname``).
            The pool is created on ``init()`` and reused for the lifetime of
            the process.
        min_size: Minimum pool size. Defaults to 1 — enough for a service
            that mostly serves reads with occasional writes.
        max_size: Maximum pool size. Defaults to 10 — comfortably above
            ``SANDBOX_MAX_CONCURRENT=8`` so no request queues waiting for a
            connection.

    Schema (created idempotently by ``init()``):

    .. code-block:: sql

        CREATE TABLE jobs (
            id         TEXT PRIMARY KEY,
            phase      TEXT NOT NULL DEFAULT 'pending',
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            metadata   JSONB NOT NULL DEFAULT '{}'::jsonb,
            result     JSONB,
            error      TEXT
        );

    ``metadata`` and ``result`` are stored as ``JSONB`` so operators can
    inspect and filter them from ``psql`` — a genuine improvement over the
    SQLite backend's ``TEXT``-of-JSON. Timestamps are ``TIMESTAMPTZ`` so
    Postgres handles the timezone offset instead of our helpers parsing
    ISO-8601 strings.
    """

    def __init__(
        self,
        dsn: str,
        *,
        min_size: int = 1,
        max_size: int = 10,
    ) -> None:
        self.dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._pool: Optional[asyncpg.Pool] = None

    # ── Init + shutdown ───────────────────────────────────────────────────
    async def init(self) -> None:
        """Create the pool and the schema. Idempotent — safe to call more
        than once, though callers typically only call it during FastAPI's
        startup event."""
        if self._pool is None:
            self._pool = await asyncpg.create_pool(
                self.dsn,
                min_size=self._min_size,
                max_size=self._max_size,
            )
        async with self._pool.acquire() as conn:
            await conn.execute(_SCHEMA)
            await conn.execute(_INDEX)

    async def close(self) -> None:
        """Close the pool. Idempotent."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    def _require_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError(
                "PostgresRegistry.init() must be awaited before use"
            )
        return self._pool

    # ── CRUD ──────────────────────────────────────────────────────────────
    async def register(
        self,
        metadata: BaseModel | dict[str, Any],
        initial_phase: str = "pending",
    ) -> str:
        """Insert a new job in ``initial_phase`` and return its id."""
        job_id = uuid4().hex[:12]
        now = _now_utc()
        meta_json = _dump_metadata(metadata)
        pool = self._require_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO jobs (id, phase, created_at, updated_at, metadata) "
                "VALUES ($1, $2, $3, $4, $5::jsonb)",
                job_id,
                initial_phase,
                now,
                now,
                meta_json,
            )
        return job_id

    async def set_phase(self, job_id: str, phase: str) -> None:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE jobs SET phase = $1, updated_at = $2 WHERE id = $3",
                phase,
                _now_utc(),
                job_id,
            )

    async def set_result(
        self, job_id: str, result: dict[str, Any], phase: str = "completed"
    ) -> None:
        """Store the result JSON and transition to ``phase`` (default
        ``"completed"``)."""
        pool = self._require_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE jobs SET phase = $1, result = $2::jsonb, updated_at = $3 "
                "WHERE id = $4",
                phase,
                json.dumps(result),
                _now_utc(),
                job_id,
            )

    async def set_error(
        self, job_id: str, error: str, phase: str = "failed"
    ) -> None:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE jobs SET phase = $1, error = $2, updated_at = $3 "
                "WHERE id = $4",
                phase,
                error,
                _now_utc(),
                job_id,
            )

    async def update_metadata(
        self, job_id: str, fields: dict[str, Any]
    ) -> None:
        """Merge ``fields`` into the stored metadata JSON. No-op if the job
        doesn't exist.

        Uses Postgres' ``||`` JSONB concat, which behaves as a shallow
        merge with right-side precedence — same semantics as the SQLite
        backend's read-modify-write path.
        """
        pool = self._require_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE jobs SET metadata = metadata || $1::jsonb, "
                "updated_at = $2 WHERE id = $3",
                json.dumps(fields),
                _now_utc(),
                job_id,
            )

    async def get(self, job_id: str) -> Optional[JobBase]:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, phase, created_at, updated_at, metadata, result, error "
                "FROM jobs WHERE id = $1",
                job_id,
            )
        if row is None:
            return None
        return _row_to_jobbase(row)

    async def list_all(self, limit: int = 20) -> JobsListResponse:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, phase, created_at, updated_at, metadata, result, error "
                "FROM jobs ORDER BY updated_at DESC LIMIT $1",
                limit,
            )
        snaps = [_row_to_jobbase(r) for r in rows]
        return JobsListResponse(active_count=len(snaps), jobs=snaps)

    async def cancel(self, job_id: str) -> tuple[bool, str]:
        """Mark a job as cancelled by setting ``phase="cancelled"``.

        Like the SQLite backend, the Postgres backend has no in-memory
        cancel event — a worker that wants to cooperatively abort must
        poll ``get(job_id).phase`` and bail out when it observes
        ``"cancelled"``. Wiring the worker to do this is the consumer's job.

        Returns ``(ok, phase_or_reason)`` same as the other backends.
        """
        pool = self._require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT phase FROM jobs WHERE id = $1", job_id
            )
            if row is None:
                return False, "not_found"
            was_phase = row["phase"]
            if was_phase in ("completed", "failed", "cancelled"):
                return False, f"already_{was_phase}"
            await conn.execute(
                "UPDATE jobs SET phase = $1, updated_at = $2 WHERE id = $3",
                "cancelled",
                _now_utc(),
                job_id,
            )
        return True, was_phase

    async def delete(self, job_id: str) -> bool:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            status = await conn.execute(
                "DELETE FROM jobs WHERE id = $1", job_id
            )
        # asyncpg returns tags like "DELETE 1" or "DELETE 0".
        return status.endswith(" 1")


# ── Helpers ────────────────────────────────────────────────────────────────
def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _dump_metadata(metadata: BaseModel | dict[str, Any]) -> str:
    if isinstance(metadata, BaseModel):
        return metadata.model_dump_json()
    return json.dumps(metadata)


def _row_to_jobbase(row: asyncpg.Record) -> JobBase:
    """Build a ``JobBase`` from an ``asyncpg.Record``.

    ``metadata`` and ``result`` come back as Python ``dict``s already
    (asyncpg decodes JSONB automatically when the codec is registered — see
    ``PostgresRegistry.init``'s pool setup); if the codec isn't registered
    they come back as ``str`` and we json-decode here as a fallback.

    ``elapsed_seconds`` is derived from ``updated_at - created_at`` since a
    persistent store has no monotonic anchor that survives process restarts
    — same choice ``SqliteRegistry`` makes.
    """
    created = row["created_at"]
    updated = row["updated_at"]
    try:
        elapsed = max(0.0, (updated - created).total_seconds())
    except Exception:
        elapsed = 0.0

    metadata = _maybe_json(row["metadata"]) or {}
    result = _maybe_json(row["result"])

    return JobBase(
        job_id=row["id"],
        phase=row["phase"],
        created_at=created.isoformat(),
        updated_at=updated.isoformat(),
        elapsed_seconds=round(elapsed, 3),
        metadata=metadata,
        result=result,
        error=row["error"],
    )


def _maybe_json(value: Any) -> Optional[dict[str, Any]]:
    """asyncpg decodes JSONB to str by default unless a codec is set up.
    Accept both — dicts pass through, strings get parsed."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return None
