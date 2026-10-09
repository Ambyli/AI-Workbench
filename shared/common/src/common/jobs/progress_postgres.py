"""PostgresProgressStore — a ``ProgressGauge``'s snapshot and history in Postgres.

Two places, written together:

* **The snapshot** goes into the job row itself, as ``metadata.progress``
  (``metadata || jsonb_build_object('progress', …)``), so ``GET /jobs/{id}``
  shows how far along a job is with no change to the jobs router — and a
  poller that only ever reads the job row gets it for free.
* **The history** is one row per event in ``job_progress``, keyed
  ``(job_id, attempt, seq)``, for "what happened when" — read back by
  ``common.jobs.router.build_progress_router``
  (``GET /jobs/{job_id}/progress``).

Schema (created idempotently by ``init()``)::

    CREATE TABLE job_progress (
        id      BIGSERIAL PRIMARY KEY,
        job_id  TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        attempt INTEGER NOT NULL,
        seq     INTEGER NOT NULL,
        at      TIMESTAMPTZ NOT NULL,
        stage   TEXT,
        label   TEXT,
        idx     INTEGER,
        length  INTEGER,
        percent REAL,
        state   TEXT NOT NULL,
        UNIQUE (job_id, attempt, seq)
    );

**Retention is the foreign key.** ``ON DELETE CASCADE`` means whatever
removes a job row — ``DELETE /jobs/{id}``, the TTL sweeper
(``common.vision.ArtifactStore.sweep``) — removes its history in the same
statement, with no hook to wire and nothing left to sweep. That is also why
``init()`` must run AFTER the registry's: the table it references has to
exist.

**One transaction per flush.** The snapshot update and the event inserts
commit together, so the history never runs ahead of (or behind) the
snapshot. A gauge flushes at most once per ``flush_interval`` (about a
second), however many checkpoints it took in between — one round trip
batch, not three per checkpoint. Inserts are ``ON CONFLICT DO NOTHING`` on
``(job_id, attempt, seq)``, so a flush retried after an ambiguous failure
cannot duplicate rows.

A flush for a job that no longer exists (deleted mid-run) updates nothing
and is dropped quietly; so is the foreign-key violation a delete racing the
flush can produce. Bumping ``updated_at`` on every flush is harmless: expiry
is keyed on ``created_at`` (``PostgresRegistry.expired_job_ids``).

Attempts: ``queued()`` writes attempt 0 (one ``queued`` event, seq 1) at
enqueue; each run takes ``next_attempt()`` = the highest attempt so far + 1,
so a job requeued after a restart keeps its first run's history beside the
second's. ``requeued()`` puts the snapshot of every job a restart sent back
to the queue back to ``queued`` — without it a requeued job would show 43%
while it waits for a worker.

Like ``PostgresRegistry(pool=...)`` it borrows a pool it never creates or
closes: an ``asyncpg.Pool``, or anything whose ``acquire()`` is an async
context manager yielding an asyncpg connection (the classifier's
``db.Database`` facade).

Process flow position: the store behind ``common.jobs.progress``; depends on
the ``jobs`` table of ``common.jobs.postgres``.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

import asyncpg

from .progress import QUEUED, RUNNING, Stage, queued_snapshot

_log = logging.getLogger("common.jobs.progress")

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS {table} (
    id      BIGSERIAL PRIMARY KEY,
    job_id  TEXT NOT NULL REFERENCES {jobs}(id) ON DELETE CASCADE,
    attempt INTEGER NOT NULL,
    seq     INTEGER NOT NULL,
    at      TIMESTAMPTZ NOT NULL,
    stage   TEXT,
    label   TEXT,
    idx     INTEGER,
    length  INTEGER,
    percent REAL,
    state   TEXT NOT NULL,
    UNIQUE (job_id, attempt, seq)
);
"""


def _identifier(name: str, what: str) -> str:
    if not _IDENTIFIER.match(name):
        raise ValueError(f"{what} must be a plain SQL identifier, got {name!r}")
    return name


def _ts(value: Any) -> datetime:
    """An event's ``at`` (ISO string or datetime) → an aware UTC datetime."""
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            parsed = datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds")
    return str(value)


def _int_or_none(value: Any) -> Optional[int]:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


class PostgresProgressStore:
    """The progress snapshot (in the job row) and history (``job_progress``).

    Args:
        pool:       The pool the jobs table lives on — see the module docstring.
        table:      The history table's name.
        jobs_table: The jobs table it references (``PostgresRegistry``'s).
    """

    def __init__(self, *, pool: Any, table: str = "job_progress", jobs_table: str = "jobs") -> None:
        if pool is None:
            raise ValueError("PostgresProgressStore needs pool=")
        self.pool = pool
        self.table = _identifier(table, "table")
        self.jobs_table = _identifier(jobs_table, "jobs_table")

    # ── Schema ────────────────────────────────────────────────────────────
    async def init(self) -> None:
        """Create the history table. Idempotent. Run after the registry's
        ``init()`` — the foreign key needs the jobs table. Takes an advisory
        lock so two processes starting at once cannot race on the catalog."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1))", f"progress:{self.table}"
                )
                await conn.execute(_SCHEMA.format(table=self.table, jobs=self.jobs_table))

    # ── Writes ────────────────────────────────────────────────────────────
    async def flush(
        self, job_id: str, snapshot: dict[str, Any], events: list[dict[str, Any]]
    ) -> None:
        """The snapshot into ``metadata.progress`` and the events into the
        history, in one transaction. A job that is gone is skipped, not an
        error (see the module docstring)."""
        attempt = int(snapshot.get("attempt") or 0)
        rows = [
            (
                job_id,
                attempt,
                int(e["seq"]),
                _ts(e.get("at")),
                e.get("stage"),
                e.get("label"),
                _int_or_none(e.get("index")),
                _int_or_none(e.get("length")),
                float(e["percent"]) if e.get("percent") is not None else None,
                e.get("state") or snapshot.get("state") or RUNNING,
            )
            for e in events
        ]
        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    status = await conn.execute(
                        f"UPDATE {self.jobs_table} SET metadata = metadata || "
                        "jsonb_build_object('progress', $1::jsonb), updated_at = $2 "
                        "WHERE id = $3",
                        json.dumps(snapshot),
                        datetime.now(timezone.utc),
                        job_id,
                    )
                    if status.endswith(" 0"):
                        _log.debug("progress: job %s is gone — dropping its progress", job_id)
                        return
                    if rows:
                        await conn.executemany(
                            f"INSERT INTO {self.table} "
                            "(job_id, attempt, seq, at, stage, label, idx, length, percent, state) "
                            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) "
                            "ON CONFLICT (job_id, attempt, seq) DO NOTHING",
                            rows,
                        )
        except asyncpg.ForeignKeyViolationError:
            # The job was deleted between the UPDATE and the INSERT.
            _log.debug("progress: job %s was deleted mid-flush — dropping its progress", job_id)

    async def queued(self, job_id: str, *, stages: Sequence[Stage] = ()) -> None:
        """Attempt 0: the ``queued`` snapshot (0%) and its one event, seq 1."""
        snapshot = queued_snapshot(stages, attempt=0)
        snapshot["seq"] = 1
        event = {
            "seq": 1, "at": snapshot["updated_at"], "stage": None, "label": "queued",
            "index": 0, "length": 0, "percent": 0.0, "state": QUEUED,
        }
        await self.flush(job_id, snapshot, [event])

    async def requeued(self, phase: str = "pending") -> int:
        """Reset the snapshot of every job in ``phase`` whose snapshot still
        says ``running`` back to ``queued`` (keeping its attempt and stages,
        counters zeroed), and add a ``requeued`` event to that attempt's
        history. Returns how many jobs it reset. Run once at startup, after
        the queue has requeued what a previous process left in flight."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                found = await conn.fetch(
                    f"SELECT id, metadata->'progress' AS progress FROM {self.jobs_table} "
                    "WHERE phase = $1 AND metadata->'progress'->>'state' = $2 FOR UPDATE",
                    phase,
                    RUNNING,
                )
                if not found:
                    return 0
                updates, events = [], []
                for row in found:
                    previous = row["progress"]
                    if isinstance(previous, str):
                        previous = json.loads(previous)
                    previous = previous if isinstance(previous, dict) else {}
                    attempt = int(previous.get("attempt") or 0)
                    seq = int(previous.get("seq") or 0) + 1
                    snapshot = queued_snapshot(attempt=attempt, label="requeued after a restart")
                    snapshot["seq"] = seq
                    snapshot["stages"] = [
                        {**s, "index": 0, "length": 0}
                        for s in (previous.get("stages") or []) if isinstance(s, dict)
                    ]
                    updates.append((json.dumps(snapshot), row["id"]))
                    events.append((
                        row["id"], attempt, seq, _ts(snapshot["updated_at"]), None,
                        snapshot["label"], 0, 0, 0.0, QUEUED,
                    ))
                await conn.executemany(
                    f"UPDATE {self.jobs_table} SET metadata = metadata || "
                    "jsonb_build_object('progress', $1::jsonb) WHERE id = $2",
                    updates,
                )
                await conn.executemany(
                    f"INSERT INTO {self.table} "
                    "(job_id, attempt, seq, at, stage, label, idx, length, percent, state) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) "
                    "ON CONFLICT (job_id, attempt, seq) DO NOTHING",
                    events,
                )
        return len(found)

    # ── Reads ─────────────────────────────────────────────────────────────
    async def next_attempt(self, job_id: str) -> int:
        """The highest attempt recorded for ``job_id`` + 1 (1 for a job with
        no history, or only its ``queued`` event at attempt 0)."""
        async with self.pool.acquire() as conn:
            value = await conn.fetchval(
                f"SELECT COALESCE(MAX(attempt), 0) FROM {self.table} WHERE job_id = $1",
                job_id,
            )
        return int(value or 0) + 1

    async def attempts(self, job_id: str) -> list[int]:
        """Every attempt with history, oldest first (0 is the queued event)."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT DISTINCT attempt FROM {self.table} WHERE job_id = $1 ORDER BY attempt",
                job_id,
            )
        return [int(r["attempt"]) for r in rows]

    async def history(
        self,
        job_id: str,
        *,
        attempt: Optional[int] = None,
        after: int = 0,
        limit: int = 200,
    ) -> dict[str, Any]:
        """One attempt's events with ``seq > after``, oldest first.

        Returns ``{attempt, events, next_after}``: ``attempt`` defaults to the
        latest one (``None`` when the job has no history at all), each event
        is ``{seq, at, stage, label, index, length, percent, state}``, and
        ``next_after`` is the last ``seq`` returned (``after`` when none) —
        pass it back as ``after`` to page on.
        """
        after = max(0, int(after))
        limit = max(1, int(limit))
        async with self.pool.acquire() as conn:
            if attempt is None:
                attempt = await conn.fetchval(
                    f"SELECT MAX(attempt) FROM {self.table} WHERE job_id = $1", job_id
                )
            if attempt is None:
                return {"attempt": None, "events": [], "next_after": after}
            rows = await conn.fetch(
                f"SELECT seq, at, stage, label, idx, length, percent, state FROM {self.table} "
                "WHERE job_id = $1 AND attempt = $2 AND seq > $3 ORDER BY seq LIMIT $4",
                job_id,
                int(attempt),
                after,
                limit,
            )
        events = [
            {
                "seq": r["seq"],
                "at": _iso(r["at"]),
                "stage": r["stage"],
                "label": r["label"],
                "index": r["idx"],
                "length": r["length"],
                # REAL is float32: 42.9 reads back as 42.900001.
                "percent": round(float(r["percent"]), 1) if r["percent"] is not None else None,
                "state": r["state"],
            }
            for r in rows
        ]
        return {
            "attempt": int(attempt),
            "events": events,
            "next_after": events[-1]["seq"] if events else after,
        }
