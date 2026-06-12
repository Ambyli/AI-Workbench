"""Async SQLite job store.

Jobs submitted to /assess and /assess/compare are queued immediately and
processed in the background.  This module persists job state so that:
  - Callers can poll GET /jobs/{job_id} for status and results.
  - Results survive a container restart (DB file is on a mounted volume).
  - Job history is available for debugging via GET /jobs.

The DB file lives at DB_PATH (/data/classifier.db by default), which is
mounted from the 'classifier_data' Docker named volume in docker-compose.

Schema
------
jobs:
    id          TEXT PRIMARY KEY  — UUID assigned at creation
    status      TEXT              — pending | processing | completed | failed
    type        TEXT              — assess | compare
    created_at  TEXT              — ISO-8601 UTC timestamp
    updated_at  TEXT              — ISO-8601 UTC timestamp (set on each transition)
    result      TEXT              — JSON blob written on completion
    error       TEXT              — error message written on failure
    request_id  TEXT              — correlation ID from the originating request

Future: an analysis_cache table (keyed on image_hash + criteria_hash) will
be added here to skip redundant LLM calls for identical inputs.

Process flow position: called by workers.job_worker() to track job state
transitions, and by main.py endpoints to create/query/delete jobs.
"""

import json
import os
import uuid
from datetime import datetime, timezone

import aiosqlite

from config import DB_PATH
from logger import logger


def _now() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


async def init_db() -> None:
    """Create the DB file and jobs table if they don't already exist.

    Called once at app startup (main.lifespan).  Safe to call repeatedly —
    CREATE TABLE IF NOT EXISTS is idempotent.
    """
    # Ensure the /data directory exists (the volume may not pre-create it)
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
    """Insert a new job record in 'pending' status and return its UUID.

    Called by main.py immediately after the HTTP request is received,
    before the job is enqueued for processing.

    Args:
        type_:      "assess" or "compare".
        request_id: Correlation ID from the inbound request for log tracing.

    Returns:
        The new job's UUID string.
    """
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
    """Update a job's status, and optionally store its result or error.

    Called by workers.job_worker() at each stage:
      - "processing" when the worker picks up the job.
      - "completed"  with the result dict when analysis succeeds.
      - "failed"     with the error string when an exception occurs.

    Args:
        job_id:  The job's UUID.
        status:  New status string.
        result:  Analysis result dict (serialised to JSON, set on completion).
        error:   Error message string (set on failure).
    """
    logger.info("update_job: job_id=%s status=%s", job_id, status)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE jobs SET status=?, updated_at=?, result=?, error=? WHERE id=?",
            (status, _now(), json.dumps(result) if result is not None else None, error, job_id),
        )
        await db.commit()


async def get_job(job_id: str) -> dict | None:
    """Fetch a single job record by ID and deserialise the result JSON.

    Called by GET /jobs/{job_id} to let callers poll for completion.

    Args:
        job_id: The job's UUID.

    Returns:
        Job dict with all columns, or None if no job with that ID exists.
        The 'result' column is deserialised from JSON if present.
    """
    logger.debug("get_job: job_id=%s", job_id)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)) as cur:
            row = await cur.fetchone()
    if row is None:
        logger.debug("get_job: not found job_id=%s", job_id)
        return None
    d = dict(row)
    # Deserialise result JSON back to a dict for the API response
    if d.get("result"):
        d["result"] = json.loads(d["result"])
    logger.debug("get_job: found job_id=%s status=%s", job_id, d["status"])
    return d


async def list_jobs(limit: int = 20) -> list[dict]:
    """Return the most recent jobs, newest first, without the result blob.

    The result column is excluded to keep the listing response compact —
    callers should use get_job() for the full result of a specific job.

    Args:
        limit: Maximum number of jobs to return (default 20).

    Returns:
        List of job dicts ordered by created_at descending.
    """
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
    """Permanently delete a job record from the database.

    Called by DELETE /jobs/{job_id}.

    Args:
        job_id: The job's UUID.

    Returns:
        True if a row was deleted, False if no job with that ID existed.
    """
    logger.info("delete_job: job_id=%s", job_id)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        await db.commit()
        deleted = cur.rowcount > 0
    logger.info("delete_job: deleted=%s job_id=%s", deleted, job_id)
    return deleted
