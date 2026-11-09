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
  (the sandbox subsystem does this — see ``ai/sandbox/SANDBOX.md``).

The public interface matches ``SqliteRegistry`` exactly — CRUD (with the
bounded admission below), the queue operations
``common.jobs.worker.WorkerPool`` drives (``claim_next`` / ``reset_phase`` /
``count_by_phase``, plus ``count_open``), and the retention query
``common.vision.ArtifactStore.sweep`` prefers (``expired_job_ids``) — so
``common.jobs.router.build_router`` mounts it with no changes and a service
can swap one backend for the other without touching its consumers.

Cancel-safe writes. ``cancel()`` is one transaction — ``SELECT … FOR UPDATE``,
then the UPDATE — so it can never overwrite a job that finished between its
read and its write. ``set_phase`` / ``set_result`` / ``set_error`` take an
optional ``from_phases`` guard (``AND phase = ANY(…)``) and say whether a row
was written: a cancel-aware ``WorkerPool`` stores a result only while the row
is still in its claimed phase. Under READ COMMITTED the guarded UPDATE waits
for a cancel holding the row lock and then re-checks the phase against the
committed row, so of a cancel and a result racing on one job exactly one
lands. Unguarded, every write is the same statement it always was.

Bounded admission. ``register(..., max_open=N)`` refuses a job that would make
more than N OPEN — not terminal: waiting, staging and running alike, and any
consumer's own phases — with ``common.jobs.QueueFull`` and no row written.
The count and the INSERT run in one transaction behind a transaction-scoped
advisory lock (``pg_advisory_xact_lock``), so admissions are serialised
across connections and processes: a plain count-then-insert under READ
COMMITTED would let two submits racing for the last slot both see room. The
lock is held for those two statements only, and nothing else takes it — a
claim, a result write or a cancel never waits on it. ``count_open()`` is the
same count without the lock, a cheap pre-check. Without ``max_open`` (every
existing caller) ``register`` is the single INSERT it always was.

Two ways to give it a connection pool:

* ``PostgresRegistry(dsn)`` — the registry owns a pool: ``init()`` creates
  it, ``close()`` closes it. The sandbox runner does this.
* ``PostgresRegistry(pool=...)`` — the service owns ONE pool and hands it to
  every store that shares the database (the classifier keeps jobs,
  references and model-usage rows in one Postgres). ``init()`` then only
  creates the schema and ``close()`` leaves the pool alone — whoever created
  it closes it, after every store sharing it is done.

Optional dep: ``asyncpg``. If a consumer imports this module without
having asyncpg installed, they get a clean ``ImportError`` at import time.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Iterable, Optional
from uuid import uuid4

import asyncpg
from pydantic import BaseModel

from .admission import QueueFull, check_max_open
from .model import JobBase, JobsListResponse

# Phases a retention sweep may delete. A job still pending or processing is
# never expired, however old — see ``expired_job_ids``. The same set as
# ``common.jobs.sqlite._TERMINAL_PHASES`` and ``common.vision.store``. Every
# other phase is OPEN — what ``count_open()`` and ``register(max_open=)``
# count.
_TERMINAL_PHASES = ("completed", "failed", "cancelled")

# The transaction-scoped advisory lock ``register(max_open=)`` serialises
# admissions on: the bytes "jobs:adm" as a 64-bit key. Advisory locks are
# per database, so every registry on one database (one ``jobs`` table)
# shares it — which is the point.
_ADMIT_LOCK_KEY = int.from_bytes(b"jobs:adm", "big")

_COUNT_OPEN_SQL = "SELECT count(*) FROM jobs WHERE phase <> ALL($1::text[])"


# ``{result_type}`` is ``JSONB`` (the default) or ``JSON`` — see the
# ``result_type`` argument of ``PostgresRegistry``.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id         TEXT PRIMARY KEY,
    phase      TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    metadata   JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    result     {result_type},
    error      TEXT
);
"""

_RESULT_TYPES = ("jsonb", "json")

_INDEX = "CREATE INDEX IF NOT EXISTS jobs_updated_at ON jobs (updated_at DESC);"


class PostgresRegistry:
    """Persistent job registry backed by a single Postgres table.

    Args:
        dsn: Postgres DSN (e.g. ``postgresql://user:pw@host:5432/dbname``).
            The pool is created on ``init()`` and reused for the lifetime of
            the process. Give this OR ``pool``, not both.
        pool: An externally owned pool — an ``asyncpg.Pool``, or any object
            whose ``acquire()`` returns an async context manager yielding an
            asyncpg connection (the classifier passes a facade that also
            serves callers on a different event loop). The registry never
            creates or closes it, and ``min_size`` / ``max_size`` are
            ignored. Usable from construction on: the owner decides when it
            is ready, so a process can build the registry at import time and
            create the real pool later in its startup.
        min_size: Minimum pool size. Defaults to 1 — enough for a service
            that mostly serves reads with occasional writes.
        max_size: Maximum pool size. Defaults to 10 — comfortably above
            ``SANDBOX_MAX_CONCURRENT=8`` so no request queues waiting for a
            connection.
        result_type: ``"jsonb"`` (default) or ``"json"`` for the ``result``
            column — ``"json"`` when the result's key order must survive the
            round trip (see below).

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

    ``result_type="json"`` stores ``result`` as ``JSON`` instead. ``JSONB``
    normalises a document — object keys come back SORTED (by length, then
    bytes), not in the order they were written — and a result whose key
    order is part of its meaning (the classifier's ``per_criterion_scores``
    lists criteria in request order) must not be reordered by its store.
    ``JSON`` keeps the text verbatim and still supports ``->`` / ``->>`` for
    operators. ``metadata`` stays ``JSONB`` either way: ``update_metadata``
    merges with the JSONB-only ``||`` operator. The type is only applied when
    the table is created; an existing table keeps whatever it has.
    """

    def __init__(
        self,
        dsn: Optional[str] = None,
        *,
        pool: Any = None,
        min_size: int = 1,
        max_size: int = 10,
        result_type: str = "jsonb",
    ) -> None:
        if (dsn is None) == (pool is None):
            raise ValueError("PostgresRegistry needs exactly one of dsn= or pool=")
        if result_type.lower() not in _RESULT_TYPES:
            raise ValueError(f"result_type must be one of {_RESULT_TYPES}, not {result_type!r}")
        self.result_type = result_type.lower()
        self.dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._external_pool = pool
        self._pool: Optional[asyncpg.Pool] = None

    @property
    def owns_pool(self) -> bool:
        """True when this registry creates (and closes) its own pool."""
        return self._external_pool is None

    # ── Init + shutdown ───────────────────────────────────────────────────
    async def init(self) -> None:
        """Create the pool (when the registry owns one) and the schema.
        Idempotent — safe to call more than once, though callers typically
        only call it during FastAPI's startup event."""
        if self.owns_pool and self._pool is None:
            self._pool = await asyncpg.create_pool(
                self.dsn,
                min_size=self._min_size,
                max_size=self._max_size,
            )
        async with self._require_pool().acquire() as conn:
            await conn.execute(_SCHEMA.format(result_type=self.result_type.upper()))
            await conn.execute(_INDEX)

    async def close(self) -> None:
        """Close the pool this registry created. Idempotent, and a no-op for
        an external pool — its owner closes it."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    def _require_pool(self) -> Any:
        if self._external_pool is not None:
            return self._external_pool
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
        *,
        max_open: Optional[int] = None,
    ) -> str:
        """Insert a new job in ``initial_phase`` and return its id.

        ``max_open`` bounds the queue: with N, the job is admitted only while
        fewer than N jobs are open (any phase but completed / failed /
        cancelled — the new row counts from the moment it is written, so a
        submit takes its slot at registration, before its payload lands);
        otherwise ``common.jobs.QueueFull(open_jobs, N)`` is raised and
        nothing is written. The count and the INSERT run in one transaction
        under ``pg_advisory_xact_lock``, so N concurrent submits on separate
        connections — or separate processes — can never overshoot. ``None``
        (the default) is the single INSERT with no transaction and no lock;
        below 1 is a ``ValueError`` (``check_max_open``).
        """
        limit = check_max_open(max_open)
        job_id = uuid4().hex[:12]
        now = _now_utc()
        meta_json = _dump_metadata(metadata)
        insert = (
            "INSERT INTO jobs (id, phase, created_at, updated_at, metadata) "
            "VALUES ($1, $2, $3, $4, $5::jsonb)"
        )
        pool = self._require_pool()
        async with pool.acquire() as conn:
            if limit is None:
                await conn.execute(insert, job_id, initial_phase, now, now, meta_json)
                return job_id
            async with conn.transaction():
                # Held until the transaction ends — a commit, or the rollback
                # QueueFull causes — so the next admitter counts after this
                # one's row is committed (READ COMMITTED: each statement sees
                # every commit before it started).
                await conn.execute("SELECT pg_advisory_xact_lock($1)", _ADMIT_LOCK_KEY)
                open_jobs = int(await conn.fetchval(_COUNT_OPEN_SQL, list(_TERMINAL_PHASES)))
                if open_jobs >= limit:
                    raise QueueFull(open_jobs, limit)
                await conn.execute(insert, job_id, initial_phase, now, now, meta_json)
        return job_id

    async def count_open(self) -> int:
        """How many jobs are open — in any phase but completed / failed /
        cancelled: waiting and running alike. The count ``register``'s
        ``max_open`` checks, without its lock: a cheap pre-check (one
        ``count(*)`` over the table, which retention keeps to a TTL window),
        never the admission itself — two callers can both see room here."""
        pool = self._require_pool()
        async with pool.acquire() as conn:
            return int(await conn.fetchval(_COUNT_OPEN_SQL, list(_TERMINAL_PHASES)))

    async def set_phase(
        self,
        job_id: str,
        phase: str,
        *,
        from_phases: Optional[Iterable[str]] = None,
    ) -> bool:
        """Move the job to ``phase``. Returns whether a row was written.

        ``from_phases`` guards the write: the row moves only while its phase
        is one of them (the classifier flips ``staging`` → ``pending`` this
        way, so a cancel that landed mid-submit is never undone). Without it
        the write is unconditional, exactly as it always was, and False only
        means the job does not exist.
        """
        return await self._update(
            "UPDATE jobs SET phase = $1, updated_at = $2 WHERE id = $3",
            [phase, _now_utc(), job_id],
            from_phases,
        )

    async def set_result(
        self,
        job_id: str,
        result: dict[str, Any],
        phase: str = "completed",
        *,
        from_phases: Optional[Iterable[str]] = None,
    ) -> bool:
        """Store the result JSON and transition to ``phase`` (default
        ``"completed"``). Returns whether a row was written; ``from_phases``
        guards it as in ``set_phase`` — a cancel-aware ``WorkerPool`` passes
        its claimed phase, so a result never lands on a cancelled job."""
        return await self._update(
            f"UPDATE jobs SET phase = $1, result = $2::{self.result_type}, updated_at = $3 "
            "WHERE id = $4",
            [phase, json.dumps(result), _now_utc(), job_id],
            from_phases,
        )

    async def set_error(
        self,
        job_id: str,
        error: str,
        phase: str = "failed",
        *,
        from_phases: Optional[Iterable[str]] = None,
    ) -> bool:
        """Store ``error`` and transition to ``phase`` (default ``"failed"``).
        Returns whether a row was written; ``from_phases`` guards it as in
        ``set_phase``."""
        return await self._update(
            "UPDATE jobs SET phase = $1, error = $2, updated_at = $3 "
            "WHERE id = $4",
            [phase, error, _now_utc(), job_id],
            from_phases,
        )

    async def _update(
        self, sql: str, args: list[Any], from_phases: Optional[Iterable[str]]
    ) -> bool:
        """Run one ``UPDATE … WHERE id = …``, with ``AND phase = ANY(…)``
        appended when guarded; whether it touched a row."""
        guard = _guard_phases(from_phases)
        if guard is not None:
            args = [*args, guard]
            sql += f" AND phase = ANY(${len(args)}::text[])"
        pool = self._require_pool()
        async with pool.acquire() as conn:
            status = await conn.execute(sql, *args)
        # asyncpg returns tags like "UPDATE 1" or "UPDATE 0".
        return not status.endswith(" 0")

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

        Atomic: one transaction reads the phase ``FOR UPDATE`` and writes the
        new one, so a job that completes or fails in between is refused
        (``already_<phase>``), never overwritten — and a guarded result write
        racing it waits for the lock, re-reads the phase and misses.

        The registry only flips the phase; it has no in-memory cancel event.
        Stopping the work is the worker's side: a ``WorkerPool`` built with
        ``cancel_check_interval`` re-reads the row while its handler runs and
        cancels the handler when it sees ``"cancelled"`` (or the row gone).
        A plain pool never looks: its handler runs on, and its unguarded
        result write lands over the ``"cancelled"``.

        Returns ``(ok, phase_or_reason)`` same as the other backends:
        ``(True, was_phase)``, ``(False, "not_found")`` or
        ``(False, "already_<phase>")`` for a job already in a terminal phase.
        """
        pool = self._require_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT phase FROM jobs WHERE id = $1 FOR UPDATE", job_id
                )
                if row is None:
                    return False, "not_found"
                was_phase = row["phase"]
                if was_phase in _TERMINAL_PHASES:
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


    # ── Queue operations ──────────────────────────────────────────────────
    # Same contract as ``SqliteRegistry``: the jobs table is a durable FIFO
    # work queue. ``FOR UPDATE SKIP LOCKED`` lets many workers (tasks,
    # processes, or replicas) claim concurrently without blocking each
    # other or ever taking the same row.

    async def claim_next(
        self,
        from_phase: str = "pending",
        to_phase: str = "processing",
    ) -> Optional[JobBase]:
        """Atomically move the oldest job in ``from_phase`` to ``to_phase``
        and return its snapshot (already reflecting ``to_phase``), or
        ``None`` when nothing is waiting."""
        pool = self._require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE jobs SET phase = $1, updated_at = $2 "
                "WHERE id = ("
                "  SELECT id FROM jobs WHERE phase = $3 "
                "  ORDER BY created_at ASC LIMIT 1 FOR UPDATE SKIP LOCKED"
                ") "
                "RETURNING id, phase, created_at, updated_at, metadata, result, error",
                to_phase,
                _now_utc(),
                from_phase,
            )
        if row is None:
            return None
        return _row_to_jobbase(row)

    async def reset_phase(self, from_phase: str, to_phase: str) -> int:
        """Bulk-transition every job in ``from_phase`` to ``to_phase`` and
        return how many rows moved. Used for crash recovery at startup
        (``"processing"`` → ``"pending"``)."""
        pool = self._require_pool()
        async with pool.acquire() as conn:
            status = await conn.execute(
                "UPDATE jobs SET phase = $1, updated_at = $2 WHERE phase = $3",
                to_phase,
                _now_utc(),
                from_phase,
            )
        # asyncpg returns tags like "UPDATE 3".
        try:
            return int(status.rsplit(" ", 1)[1])
        except (IndexError, ValueError):
            return 0

    # ── Retention ─────────────────────────────────────────────────────────
    async def expired_job_ids(
        self,
        cutoff: datetime,
        phases: Iterable[str] = _TERMINAL_PHASES,
        *,
        limit: Optional[int] = None,
    ) -> list[str]:
        """Ids of jobs in ``phases`` whose ``created_at`` predates ``cutoff``.

        Same contract as ``SqliteRegistry.expired_job_ids`` and for the same
        reason: it is the bounded alternative to ``list_all`` that
        ``common.vision.ArtifactStore.sweep`` prefers — ids only, filtered in
        SQL, no ceiling unless one is asked for — so a service that takes
        more than a page of jobs inside one TTL window still reaches its
        oldest expired rows, without dragging every ``result`` blob along.

        No string-truncation trick is needed here, unlike the SQLite backend:
        ``created_at`` is a ``TIMESTAMPTZ``, so this is a real instant-to-
        instant comparison at microsecond resolution, whatever offset either
        side was written with.

        Args:
            cutoff: Jobs created strictly before this instant are expired. A
                naive datetime is read as UTC.
            phases: Phases eligible for expiry. Defaults to the terminal set —
                a job still pending or processing is never expired, however
                old, because a queue that backed up should drain, not
                evaporate.
            limit:  Optional cap, oldest first. ``None`` returns every match.

        Returns:
            Job ids, oldest first.
        """
        phase_list = list(phases)
        if not phase_list:
            return []
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=timezone.utc)
        sql = (
            "SELECT id FROM jobs WHERE phase = ANY($1::text[]) AND created_at < $2 "
            "ORDER BY created_at ASC, id ASC"
        )
        args: list[Any] = [phase_list, cutoff]
        if limit is not None:
            sql += " LIMIT $3"
            args.append(int(limit))
        pool = self._require_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *args)
        return [r["id"] for r in rows]

    async def count_by_phase(self) -> dict[str, int]:
        """Return ``{phase: row_count}`` for every phase present."""
        pool = self._require_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT phase, COUNT(*) AS n FROM jobs GROUP BY phase"
            )
        return {r["phase"]: r["n"] for r in rows}


# ── Helpers ────────────────────────────────────────────────────────────────
def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _dump_metadata(metadata: BaseModel | dict[str, Any]) -> str:
    if isinstance(metadata, BaseModel):
        return metadata.model_dump_json()
    return json.dumps(metadata)


def _guard_phases(from_phases: Optional[Iterable[str]]) -> Optional[list[str]]:
    """A write's ``from_phases`` as a list, or None for an unguarded write.
    Refuses a bare string (it would iterate into one-letter "phases") and an
    empty guard (a write no row could ever pass) — the same checks as
    ``common.jobs.sqlite``'s."""
    if from_phases is None:
        return None
    if isinstance(from_phases, str):
        raise TypeError("from_phases takes a collection of phase names, not one string")
    phases = list(from_phases)
    if not phases:
        raise ValueError("from_phases names no phase: the write could never land")
    return phases


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
