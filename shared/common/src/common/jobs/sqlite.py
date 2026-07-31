"""
SqliteRegistry — async, aiosqlite-backed, persistent job tracking.

Kickoff-then-poll shape (no context manager): the caller registers a job,
stores the returned ``job_id``, then updates phase / result / error at
distinct points in time from wherever it likes. Jobs survive process
restarts; they only leave the store on an explicit ``delete()`` call or the
consumer's own retention policy.

This is the right shape for services that hand off work to a background
worker and expose ``POST kickoff → GET /jobs/{id}`` polling — like
``classifier``, whose ``/assess`` endpoint returns immediately with a
``job_id`` while the actual analysis happens in an asyncio task.

Optional dep: ``aiosqlite``. If a consumer imports this module without
having aiosqlite installed, they get a clean ``ImportError`` at import time.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

import aiosqlite
from pydantic import BaseModel

from .model import JobBase, JobsListResponse


_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id         TEXT PRIMARY KEY,
    phase      TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata   TEXT NOT NULL,
    result     TEXT,
    error      TEXT
);
"""

_INDEX = "CREATE INDEX IF NOT EXISTS jobs_updated_at ON jobs (updated_at DESC);"


class SqliteRegistry:
    """Persistent job registry backed by a single SQLite table.

    Args:
        db_path: Filesystem path to the SQLite DB. Parent dir is auto-created.
            Pass ``":memory:"`` for tests.

    Schema (created idempotently by ``init()``):

    .. code-block:: sql

        CREATE TABLE jobs (
            id         TEXT PRIMARY KEY,
            phase      TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            metadata   TEXT NOT NULL,   -- JSON: consumer's metadata dict
            result     TEXT,             -- JSON, set on completion
            error      TEXT
        );

    Migration: if an older schema exists (with a ``status`` column instead of
    ``phase`` and separate ``type`` + ``request_id`` columns — the shape
    classifier used before adopting ``common.jobs``), ``init()`` renames
    ``status`` → ``phase``, adds ``metadata``, and back-fills each row's
    ``metadata`` from the legacy ``type`` and ``request_id`` columns.
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    # ── Init + migration ──────────────────────────────────────────────────
    async def init(self) -> None:
        """Create the schema or migrate an older one. Idempotent."""
        if self.db_path != ":memory:":
            os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            existing = await self._existing_columns(db)
            if existing is None:
                # Fresh install — create from scratch.
                await db.executescript(_SCHEMA + _INDEX)
                await db.commit()
                return

            # Table exists — figure out whether it's the new shape or the old
            # classifier shape and migrate the old one in place.
            has_phase = "phase" in existing
            has_status = "status" in existing
            has_metadata = "metadata" in existing
            has_type = "type" in existing
            has_request_id = "request_id" in existing

            if has_phase and has_metadata:
                # Already migrated — just make sure the index exists.
                await db.executescript(_INDEX)
                await db.commit()
                return

            # Legacy schema detected. Rename status → phase if needed, then
            # add the metadata column, then back-fill from the legacy columns.
            if has_status and not has_phase:
                await db.execute("ALTER TABLE jobs RENAME COLUMN status TO phase")
            if not has_metadata:
                await db.execute("ALTER TABLE jobs ADD COLUMN metadata TEXT")
                # Back-fill metadata from whichever legacy columns are present.
                if has_type or has_request_id:
                    select_cols = "id"
                    if has_type:
                        select_cols += ", type"
                    if has_request_id:
                        select_cols += ", request_id"
                    async with db.execute(
                        f"SELECT {select_cols} FROM jobs"
                    ) as cur:
                        rows = await cur.fetchall()
                    for row in rows:
                        i = 1
                        meta: dict[str, Any] = {}
                        if has_type:
                            meta["type"] = row[i]
                            i += 1
                        if has_request_id:
                            meta["request_id"] = row[i]
                        await db.execute(
                            "UPDATE jobs SET metadata = ? WHERE id = ?",
                            (json.dumps(meta), row[0]),
                        )
                else:
                    await db.execute(
                        "UPDATE jobs SET metadata = ? WHERE metadata IS NULL",
                        (json.dumps({}),),
                    )
                # Enforce NOT NULL after back-fill by copying the table.
                # SQLite doesn't support ALTER COLUMN NOT NULL directly, but
                # our reads tolerate NULL metadata (parsed as {}) so we skip
                # the recreate — cheap and safe.
            await db.executescript(_INDEX)
            await db.commit()

    @staticmethod
    async def _existing_columns(db: aiosqlite.Connection) -> Optional[set[str]]:
        """Return the set of column names for the ``jobs`` table, or ``None``
        if the table doesn't exist yet."""
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='jobs'"
        ) as cur:
            if await cur.fetchone() is None:
                return None
        async with db.execute("PRAGMA table_info(jobs)") as cur:
            rows = await cur.fetchall()
        return {r[1] for r in rows}  # (cid, name, type, notnull, dflt, pk)

    # ── CRUD ──────────────────────────────────────────────────────────────
    async def register(
        self,
        metadata: BaseModel | dict[str, Any],
        initial_phase: str = "pending",
    ) -> str:
        """Insert a new job in ``initial_phase`` and return its id."""
        job_id = uuid4().hex[:12]
        now = _now_iso()
        meta_json = _dump_metadata(metadata)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO jobs (id, phase, created_at, updated_at, metadata) "
                "VALUES (?, ?, ?, ?, ?)",
                (job_id, initial_phase, now, now, meta_json),
            )
            await db.commit()
        return job_id

    async def set_phase(self, job_id: str, phase: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE jobs SET phase = ?, updated_at = ? WHERE id = ?",
                (phase, _now_iso(), job_id),
            )
            await db.commit()

    async def set_result(
        self, job_id: str, result: dict[str, Any], phase: str = "completed"
    ) -> None:
        """Store the result JSON and transition to ``phase`` (default
        ``"completed"``). Result is stored as compact JSON."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE jobs SET phase = ?, result = ?, updated_at = ? WHERE id = ?",
                (phase, json.dumps(result), _now_iso(), job_id),
            )
            await db.commit()

    async def set_error(
        self, job_id: str, error: str, phase: str = "failed"
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE jobs SET phase = ?, error = ?, updated_at = ? WHERE id = ?",
                (phase, error, _now_iso(), job_id),
            )
            await db.commit()

    async def update_metadata(
        self, job_id: str, fields: dict[str, Any]
    ) -> None:
        """Merge ``fields`` into the stored metadata JSON. No-op if the job
        doesn't exist."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT metadata FROM jobs WHERE id = ?", (job_id,)
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                return
            current = _load_json(row["metadata"]) or {}
            current.update(fields)
            await db.execute(
                "UPDATE jobs SET metadata = ?, updated_at = ? WHERE id = ?",
                (json.dumps(current), _now_iso(), job_id),
            )
            await db.commit()

    async def get(self, job_id: str) -> Optional[JobBase]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ) as cur:
                row = await cur.fetchone()
        if row is None:
            return None
        return _row_to_jobbase(row)

    async def list_all(self, limit: int = 20) -> JobsListResponse:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM jobs ORDER BY updated_at DESC LIMIT ?", (limit,)
            ) as cur:
                rows = await cur.fetchall()
        snaps = [_row_to_jobbase(r) for r in rows]
        return JobsListResponse(active_count=len(snaps), jobs=snaps)

    async def cancel(self, job_id: str) -> tuple[bool, str]:
        """Mark a job as cancelled by setting ``phase="cancelled"``.

        The SQLite backend has no in-memory cancel event — a worker that
        wants to cooperatively abort must poll ``get(job_id).phase`` and
        bail out when it observes ``"cancelled"``. Wiring the worker to do
        this is the consumer's job.

        Returns ``(ok, phase_or_reason)`` same as the in-memory backend.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT phase FROM jobs WHERE id = ?", (job_id,)
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                return False, "not_found"
            was_phase = row["phase"]
            if was_phase in ("completed", "failed", "cancelled"):
                return False, f"already_{was_phase}"
            await db.execute(
                "UPDATE jobs SET phase = ?, updated_at = ? WHERE id = ?",
                ("cancelled", _now_iso(), job_id),
            )
            await db.commit()
        return True, was_phase

    async def delete(self, job_id: str) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            await db.commit()
            return cur.rowcount > 0


# ── Helpers ────────────────────────────────────────────────────────────────
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dump_metadata(metadata: BaseModel | dict[str, Any]) -> str:
    if isinstance(metadata, BaseModel):
        return metadata.model_dump_json()
    return json.dumps(metadata)


def _load_json(raw: Optional[str]) -> Optional[dict[str, Any]]:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _row_to_jobbase(row: aiosqlite.Row) -> JobBase:
    """Build a ``JobBase`` from a ``sqlite3.Row``. Computes
    ``elapsed_seconds`` from ``updated_at - created_at`` since we don't have
    a monotonic anchor for persisted jobs."""
    metadata = _load_json(row["metadata"]) or {}
    result = _load_json(row["result"])
    try:
        created = datetime.fromisoformat(row["created_at"])
        updated = datetime.fromisoformat(row["updated_at"])
        elapsed = max(0.0, (updated - created).total_seconds())
    except Exception:
        elapsed = 0.0
    return JobBase(
        job_id=row["id"],
        phase=row["phase"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        elapsed_seconds=round(elapsed, 3),
        metadata=metadata,
        result=result,
        error=row["error"],
    )
