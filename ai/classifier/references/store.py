"""Where references live: one Postgres table and one directory per reference.

    ReferenceRegistry   the ``reference_examples`` table, in the SAME
                        ``classifier-db`` database as the jobs table
                        (``references`` is a reserved SQL word, hence the
                        name). asyncpg, through the one pool ``db.database``
                        owns and ``common.jobs.postgres.PostgresRegistry``
                        beside it shares.
    reference_registry  the process-wide instance.
    reference_files     a ``common.vision.ArtifactStore`` rooted at
                        CLASSIFIER_REFERENCE_DIR, one directory per reference
                        id. It is NEVER swept: ``ArtifactStore.sweep`` deletes
                        any directory whose job row is gone, which is exactly
                        what must not happen to a reference once its creation
                        job expires — so this store has its own root and no
                        sweeper, and no byte cap (CLASSIFIER_REFERENCE_MAX_COUNT
                        bounds it instead).
    reconcile()         startup: a ``pending`` reference whose creation job is
                        gone or finished without readying it becomes
                        ``failed`` — otherwise it would stay pending forever.
    refresh_gauges()    point the reference gauges at the table and the disk.

Types: ``tags`` is ``JSONB`` (the ``?`` operator filters on it), ``record``
is ``JSON`` and the two timestamps are ``TIMESTAMPTZ``, so an operator (or
Trino, as ``postgres_classifier``) can filter on a tag or read a field of the
record without parsing text. ``record`` is JSON rather than JSONB on purpose:
JSONB sorts object keys, and the record's criteria are kept in the order the
reference was built with — the order its examples are shown in. The
``Reference`` this module hands back is unchanged from the SQLite days —
timestamps are ISO-8601 strings, ``tags`` a list, ``record`` a dict — so no
route's response shape moved with the backend.

The in-use check (``jobs_using``) reads the ``jobs`` table of the same
database directly — the registry of ``common.jobs`` has no "which queued
job's metadata mentions X" query, and a scan through ``list_all`` would drag
every result blob along. It only reads ``phase`` and ``metadata``, the two
columns ``common.jobs.postgres`` documents as its schema, and with
``metadata`` a JSONB column the test is the ``?`` key-exists operator rather
than a ``json_each`` scan.

Process flow position: below ``api.references`` (every route), the runner
(``jobs.runners.run_reference``) and ``main``'s lifespan (init + reconcile).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

import asyncpg

from common.vision import ArtifactStore

from config import REFERENCE_DIR
from db import Database, create_schema, database
from logger import logger
from metrics import reference_bytes, reference_dirs, references_by_status
from references.model import STATUSES, Reference

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reference_examples (
    id                 TEXT PRIMARY KEY,
    status             TEXT NOT NULL,
    title              TEXT,
    description        TEXT,
    description_source TEXT,
    tags               JSONB NOT NULL DEFAULT '[]'::jsonb,
    job_id             TEXT,
    source_kind        TEXT NOT NULL,
    source_job_id      TEXT,
    source_item        INTEGER,
    error              TEXT,
    created_at         TIMESTAMPTZ NOT NULL,
    updated_at         TIMESTAMPTZ NOT NULL,
    record             JSON
);
CREATE INDEX IF NOT EXISTS reference_examples_created
    ON reference_examples (created_at DESC);
CREATE INDEX IF NOT EXISTS reference_examples_status
    ON reference_examples (status);
"""

# Job phases in which a job still counts as using a reference.
_LIVE_PHASES = ("staging", "pending", "processing")

# PATCH distinguishes "leave it" from "clear it" (an explicit null).
UNSET: Any = object()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> str:
    """A TIMESTAMPTZ (asyncpg gives an aware datetime) → the ISO-8601 UTC
    string the ``Reference`` model and the API have always carried."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _json(value: Any) -> Any:
    """A JSONB column as asyncpg returns it without a codec (a ``str``) — or
    already decoded — → the Python value; anything unparseable → ``None``."""
    if value is None or isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None


def _affected(status: str) -> int:
    """Rows touched, from an asyncpg command tag (``"UPDATE 1"``,
    ``"DELETE 0"``) — asyncpg's equivalent of sqlite3's ``rowcount``."""
    try:
        return int(status.rsplit(" ", 1)[-1])
    except (AttributeError, ValueError):
        return 0


def _row(row: asyncpg.Record) -> Reference:
    tags = _json(row["tags"])
    record = _json(row["record"])
    return Reference(
        id=row["id"],
        status=row["status"],
        title=row["title"],
        description=row["description"],
        description_source=row["description_source"],
        tags=list(tags) if isinstance(tags, list) else [],
        job_id=row["job_id"],
        source_kind=row["source_kind"],
        source_job_id=row["source_job_id"],
        source_item=row["source_item"],
        error=row["error"],
        created_at=_iso(row["created_at"]),
        updated_at=_iso(row["updated_at"]),
        record=record if isinstance(record, dict) else None,
    )


class ReferenceRegistry:
    """The ``reference_examples`` table.

    Args:
        db: The classifier's :class:`db.Database` — the one pool shared with
            the jobs table (which ``jobs_using`` reads) and ``llm_calls``.
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    async def init(self) -> None:
        """Create the table and its indexes. Idempotent."""
        async with self.db.acquire() as conn:
            await create_schema(conn, "classifier.reference_examples", _SCHEMA)

    # ── Writes ────────────────────────────────────────────────────────────
    async def create(
        self,
        reference_id: str,
        *,
        source_kind: str,
        job_id: Optional[str],
        title: Optional[str] = None,
        description: Optional[str] = None,
        tags: Iterable[str] = (),
        source_job_id: Optional[str] = None,
        source_item: Optional[int] = None,
    ) -> Reference:
        """Insert a ``pending`` reference. A caller-supplied description is
        recorded as ``description_source: "caller"``."""
        now = _now()
        async with self.db.acquire() as conn:
            await conn.execute(
                "INSERT INTO reference_examples (id, status, title, description, "
                "description_source, tags, job_id, source_kind, source_job_id, "
                "source_item, created_at, updated_at) "
                "VALUES ($1, 'pending', $2, $3, $4, $5::jsonb, $6, $7, $8, $9, $10, $11)",
                reference_id, title, description,
                "caller" if description is not None else None,
                json.dumps(list(tags)), job_id, source_kind, source_job_id,
                source_item, now, now,
            )
        ref = await self.get(reference_id)
        assert ref is not None
        return ref

    async def finish(
        self,
        reference_id: str,
        record: dict[str, Any],
        *,
        description: Any = UNSET,
        description_source: Any = UNSET,
    ) -> bool:
        """Freeze ``record`` and mark the reference ``ready``.

        Only a ``pending`` row moves: a reference deleted (or failed) while
        its job ran returns False, and the runner treats that as "nothing to
        finish". ``description`` is only written when given — a caller's own
        description is never replaced by a generated one.
        """
        args: list[Any] = [json.dumps(record), _now()]
        sets = ["status = 'ready'", "record = $1::json", "error = NULL", "updated_at = $2"]
        if description is not UNSET:
            args.append(description)
            sets.append(f"description = ${len(args)}")
        if description_source is not UNSET:
            args.append(description_source)
            sets.append(f"description_source = ${len(args)}")
        args.append(reference_id)
        async with self.db.acquire() as conn:
            status = await conn.execute(
                f"UPDATE reference_examples SET {', '.join(sets)} "
                f"WHERE id = ${len(args)} AND status = 'pending'",
                *args,
            )
        return _affected(status) > 0

    async def fail(self, reference_id: str, error: str) -> bool:
        """Mark a ``pending`` reference ``failed``. A ready one is never
        demoted (a requeued job that crashes after readying it must not undo
        it)."""
        async with self.db.acquire() as conn:
            status = await conn.execute(
                "UPDATE reference_examples SET status = 'failed', error = $1, updated_at = $2 "
                "WHERE id = $3 AND status = 'pending'",
                error, _now(), reference_id,
            )
        return _affected(status) > 0

    async def update_meta(
        self,
        reference_id: str,
        *,
        title: Any = UNSET,
        description: Any = UNSET,
        tags: Any = UNSET,
    ) -> Optional[Reference]:
        """PATCH: title / description / tags only — the content is immutable.
        Returns the updated row, or None when the id is unknown. Setting a
        description marks it ``description_source: "caller"`` (clearing it,
        None)."""
        sets: list[str] = []
        args: list[Any] = []

        def bind(column: str, value: Any, cast: str = "") -> None:
            args.append(value)
            sets.append(f"{column} = ${len(args)}{cast}")

        if title is not UNSET:
            bind("title", title)
        if description is not UNSET:
            bind("description", description)
            bind("description_source", "caller" if description is not None else None)
        if tags is not UNSET:
            bind("tags", json.dumps(list(tags or [])), "::jsonb")
        if sets:
            bind("updated_at", _now())
            args.append(reference_id)
            async with self.db.acquire() as conn:
                await conn.execute(
                    f"UPDATE reference_examples SET {', '.join(sets)} WHERE id = ${len(args)}",
                    *args,
                )
        return await self.get(reference_id)

    async def delete(self, reference_id: str) -> bool:
        async with self.db.acquire() as conn:
            status = await conn.execute(
                "DELETE FROM reference_examples WHERE id = $1", reference_id
            )
        return _affected(status) > 0

    # ── Reads ─────────────────────────────────────────────────────────────
    async def get(self, reference_id: str) -> Optional[Reference]:
        async with self.db.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM reference_examples WHERE id = $1", reference_id
            )
        return _row(row) if row is not None else None

    async def list(
        self,
        *,
        status: Optional[str] = None,
        tag: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Reference], int]:
        """``(page, total)`` newest first, optionally one status and/or one tag
        (tags are stored lowercased, so the match is case-insensitive).

        The tag test is JSONB's ``?`` — "is this string an element of the
        array" — which is what the SQLite version's ``json_each`` scan did.
        """
        where: list[str] = []
        args: list[Any] = []
        if status is not None:
            args.append(status)
            where.append(f"status = ${len(args)}")
        if tag is not None:
            args.append(tag.strip().lower())
            where.append(f"tags ? ${len(args)}")
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        page_args = [*args, int(limit), int(offset)]
        async with self.db.acquire() as conn:
            total = await conn.fetchval(
                f"SELECT COUNT(*) FROM reference_examples{clause}", *args
            )
            rows = await conn.fetch(
                f"SELECT * FROM reference_examples{clause} "
                f"ORDER BY created_at DESC, id DESC "
                f"LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}",
                *page_args,
            )
        return [_row(r) for r in rows], int(total or 0)

    async def count(self) -> int:
        async with self.db.acquire() as conn:
            return int(await conn.fetchval("SELECT COUNT(*) FROM reference_examples") or 0)

    async def count_by_status(self) -> dict[str, int]:
        async with self.db.acquire() as conn:
            rows = await conn.fetch(
                "SELECT status, COUNT(*) AS n FROM reference_examples GROUP BY status"
            )
        counts = {s: 0 for s in STATUSES}
        counts.update({r["status"]: int(r["n"]) for r in rows})
        return counts

    async def pending(self) -> list[Reference]:
        refs, _ = await self.list(status="pending", limit=100_000)
        return refs

    async def jobs_using(self, reference_id: str, *, include_creation: bool = True) -> list[str]:
        """Ids of jobs still queued or running that read this reference — an
        assess job listing it in ``metadata.references``, or (with
        ``include_creation``) its own creation job (``metadata.reference_id``).

        Pass ``include_creation=False`` for a reference that is no longer
        pending: it is readied a moment BEFORE its job row completes, and that
        finishing job must not make a DELETE right after "ready" a 409.
        """
        creation = "metadata->>'reference_id' = $2 OR " if include_creation else ""
        query = (
            "SELECT id FROM jobs WHERE phase = ANY($1::text[]) AND ("
            f"{creation}COALESCE(metadata->'references', '[]'::jsonb) ? $2)"
        )
        try:
            async with self.db.acquire() as conn:
                rows = await conn.fetch(query, list(_LIVE_PHASES), reference_id)
        except asyncpg.UndefinedTableError as exc:
            # No jobs table yet (a registry used on its own, in a test).
            logger.debug("jobs_using: %s", exc)
            return []
        return sorted(r["id"] for r in rows)


# ---------------------------------------------------------------------------
# Process-wide instances
# ---------------------------------------------------------------------------

reference_registry = ReferenceRegistry(database)


# No max_bytes and no sweeper: see the module docstring.
reference_files = ArtifactStore(REFERENCE_DIR)


async def reconcile(jobs_registry: Any, registry: ReferenceRegistry = reference_registry) -> int:
    """Fail every ``pending`` reference whose creation job cannot ready it.

    A pending reference whose job row is gone (swept, deleted), failed, or
    finished without readying it would otherwise stay pending forever. A job
    still staging / pending / processing is left alone — the queue's own
    recovery requeues it and the runner finishes the reference. Returns how
    many were failed.
    """
    failed = 0
    for ref in await registry.pending():
        job = await jobs_registry.get(ref.job_id) if ref.job_id else None
        if job is not None and job.phase in _LIVE_PHASES:
            continue
        if job is None:
            why = f"its creation job {ref.job_id!r} no longer exists"
        elif job.phase == "failed":
            why = f"its creation job {ref.job_id!r} failed: {job.error or 'no error recorded'}"
        else:
            why = f"its creation job {ref.job_id!r} ended ({job.phase}) without readying it"
        if await registry.fail(ref.id, why):
            failed += 1
            reference_files.delete(ref.id)
            logger.warning("references.reconcile: %s failed — %s", ref.id, why)
    return failed


async def refresh_gauges(registry: ReferenceRegistry = reference_registry) -> None:
    """Point ``classifier_references`` and the reference disk gauges at what
    is in the table and on disk. Never raises — a gauge is not worth a 500."""
    try:
        counts = await registry.count_by_status()
        for status, n in counts.items():
            references_by_status.labels(status=status).set(n)
        stats = reference_files.stats()
        reference_bytes.set(stats["bytes"])
        reference_dirs.set(stats["dirs"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("references.refresh_gauges: %s", exc)
