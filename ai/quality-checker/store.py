"""Async SQLite job store.

Jobs are persisted to DB_PATH (a Docker volume mount). The same DB will
serve as the analysis cache in a future iteration.

Schema
------
jobs:
    id          TEXT PRIMARY KEY
    status      TEXT  -- pending | processing | completed | failed
    type        TEXT  -- assess | compare
    created_at  TEXT  -- ISO-8601 UTC
    updated_at  TEXT  -- ISO-8601 UTC
    result      TEXT  -- JSON blob (set on completion)
    error       TEXT  -- error message (set on failure)
    request_id  TEXT  -- correlation ID from the originating request
"""

import json
import os
import uuid
from datetime import datetime, timezone

import aiosqlite

from config import DB_PATH
from logger import logger


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def init_db() -> None:
    """Create the DB file and schema if they don't already exist."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    logger.info("init_db: initialising database at %s", DB_PATH)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id         TEXT PRIMARY KEY,
                status     TEXT NOT NULL DEFAULT 'pending',
                type       TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                result     TEXT,
                error      TEXT,
                request_id TEXT
            )
        """)
        await db.commit()
    logger.info("init_db: schema ready")


async def create_job(type_: str, request_id: str = "-") -> str:
    """Insert a new pending job and return its ID."""
    job_id = str(uuid.uuid4())
    now = _now()
    logger.info("create_job: type=%s request_id=%s job_id=%s", type_, request_id, job_id)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO jobs (id, status, type, created_at, updated_at, request_id) "
            "VALUES (?, 'pending', ?, ?, ?, ?)",
            (job_id, type_, now, now, request_id),
        )
        await db.commit()
    return job_id


async def update_job(
    job_id: str,
    status: str,
    result: dict | None = None,
    error: str | None = None,
) -> None:
    logger.info("update_job: job_id=%s status=%s", job_id, status)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE jobs SET status=?, updated_at=?, result=?, error=? WHERE id=?",
            (status, _now(), json.dumps(result) if result is not None else None, error, job_id),
        )
        await db.commit()


async def get_job(job_id: str) -> dict | None:
    logger.debug("get_job: job_id=%s", job_id)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)) as cur:
            row = await cur.fetchone()
    if row is None:
        logger.debug("get_job: not found job_id=%s", job_id)
        return None
    d = dict(row)
    if d.get("result"):
        d["result"] = json.loads(d["result"])
    logger.debug("get_job: found job_id=%s status=%s", job_id, d["status"])
    return d


async def list_jobs(limit: int = 20) -> list[dict]:
    logger.debug("list_jobs: limit=%d", limit)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, status, type, created_at, updated_at, request_id, error "
            "FROM jobs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
    result = [dict(r) for r in rows]
    logger.debug("list_jobs: returning %d jobs", len(result))
    return result


async def delete_job(job_id: str) -> bool:
    """Delete a job. Returns True if a row was deleted."""
    logger.info("delete_job: job_id=%s", job_id)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        await db.commit()
        deleted = cur.rowcount > 0
    logger.info("delete_job: deleted=%s job_id=%s", deleted, job_id)
    return deleted
