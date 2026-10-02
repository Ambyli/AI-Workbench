"""Where references live: one SQLite table and one directory per reference.

    ReferenceRegistry   the ``reference_examples`` table, in the SAME
                        ``classifier.db`` as the jobs table (``references`` is
                        a reserved SQL word, hence the name). aiosqlite, one
                        connection per call, exactly like
                        ``common.jobs.sqlite.SqliteRegistry`` beside it.
    reference_registry  the process-wide instance (DB_PATH).
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

The in-use check (``jobs_using``) reads the ``jobs`` table of the same DB
directly — the registry of ``common.jobs`` has no "which queued job's
metadata mentions X" query, and a scan through ``list_all`` would drag every
result blob along. It only reads ``phase`` and ``metadata``, the two columns
``common.jobs.sqlite`` documents as its schema.

Process flow position: below ``api.references`` (every route), the runner
(``jobs.runners.run_reference``) and ``main``'s lifespan (init + reconcile).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

import aiosqlite

from common.vision import ArtifactStore

from config import DB_PATH, REFERENCE_DIR
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
    tags               TEXT NOT NULL DEFAULT '[]',
    job_id             TEXT,
    source_kind        TEXT NOT NULL,
    source_job_id      TEXT,
    source_item        INTEGER,
    error              TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    record             TEXT
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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row(row: aiosqlite.Row) -> Reference:
    try:
        tags = json.loads(row["tags"] or "[]")
    except json.JSONDecodeError:
        tags = []
    record = None
    if row["record"]:
        try:
            record = json.loads(row["record"])
        except json.JSONDecodeError:
            record = None
    return Reference(
        id=row["id"],
        status=row["status"],
        title=row["title"],
        description=row["description"],
        description_source=row["description_source"],
        tags=list(tags),
        job_id=row["job_id"],
        source_kind=row["source_kind"],
        source_job_id=row["source_job_id"],
        source_item=row["source_item"],
        error=row["error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        record=record,
    )


class ReferenceRegistry:
    """The ``reference_examples`` table.

    Args:
        db_path: The classifier's SQLite file (DB_PATH) — shared with the jobs
                 table, which ``jobs_using`` reads.
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    async def init(self) -> None:
        """Create the table and its indexes. Idempotent."""
        if self.db_path != ":memory:":
            os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(_SCHEMA)
            await db.commit()

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
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO reference_examples (id, status, title, description, "
                "description_source, tags, job_id, source_kind, source_job_id, "
                "source_item, created_at, updated_at) "
                "VALUES (?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    reference_id, title, description,
                    "caller" if description is not None else None,
                    json.dumps(list(tags)), job_id, source_kind, source_job_id,
                    source_item, now, now,
                ),
            )
            await db.commit()
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
        sets = ["status = 'ready'", "record = ?", "error = NULL", "updated_at = ?"]
        args: list[Any] = [json.dumps(record), _now()]
        if description is not UNSET:
            sets.append("description = ?")
            args.append(description)
        if description_source is not UNSET:
            sets.append("description_source = ?")
            args.append(description_source)
        args.append(reference_id)
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                f"UPDATE reference_examples SET {', '.join(sets)} "
                "WHERE id = ? AND status = 'pending'",
                args,
            )
            await db.commit()
            return cur.rowcount > 0

    async def fail(self, reference_id: str, error: str) -> bool:
        """Mark a ``pending`` reference ``failed``. A ready one is never
        demoted (a requeued job that crashes after readying it must not undo
        it)."""
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "UPDATE reference_examples SET status = 'failed', error = ?, updated_at = ? "
                "WHERE id = ? AND status = 'pending'",
                (error, _now(), reference_id),
            )
            await db.commit()
            return cur.rowcount > 0

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
        if title is not UNSET:
            sets.append("title = ?")
            args.append(title)
        if description is not UNSET:
            sets += ["description = ?", "description_source = ?"]
            args += [description, "caller" if description is not None else None]
        if tags is not UNSET:
            sets.append("tags = ?")
            args.append(json.dumps(list(tags or [])))
        if sets:
            sets.append("updated_at = ?")
            args += [_now(), reference_id]
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    f"UPDATE reference_examples SET {', '.join(sets)} WHERE id = ?", args
                )
                await db.commit()
        return await self.get(reference_id)

    async def delete(self, reference_id: str) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "DELETE FROM reference_examples WHERE id = ?", (reference_id,)
            )
            await db.commit()
            return cur.rowcount > 0

    # ── Reads ─────────────────────────────────────────────────────────────
    async def get(self, reference_id: str) -> Optional[Reference]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM reference_examples WHERE id = ?", (reference_id,)
            ) as cur:
                row = await cur.fetchone()
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
        (tags are stored lowercased, so the match is case-insensitive)."""
        where: list[str] = []
        args: list[Any] = []
        if status is not None:
            where.append("status = ?")
            args.append(status)
        if tag is not None:
            where.append("EXISTS (SELECT 1 FROM json_each(tags) WHERE value = ?)")
            args.append(tag.strip().lower())
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT COUNT(*) AS n FROM reference_examples{clause}", args
            ) as cur:
                total = int((await cur.fetchone())["n"])
            async with db.execute(
                f"SELECT * FROM reference_examples{clause} "
                "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                [*args, int(limit), int(offset)],
            ) as cur:
                rows = await cur.fetchall()
        return [_row(r) for r in rows], total

    async def count(self) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT COUNT(*) FROM reference_examples") as cur:
                return int((await cur.fetchone())[0])

    async def count_by_status(self) -> dict[str, int]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT status, COUNT(*) FROM reference_examples GROUP BY status"
            ) as cur:
                rows = await cur.fetchall()
        counts = {s: 0 for s in STATUSES}
        counts.update({status: int(n) for status, n in rows})
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
        marks = ", ".join("?" for _ in _LIVE_PHASES)
        creation = "json_extract(metadata, '$.reference_id') = ? OR " if include_creation else ""
        query = (
            f"SELECT id FROM jobs WHERE phase IN ({marks}) AND ({creation}EXISTS ("
            "SELECT 1 FROM json_each(COALESCE(json_extract(metadata, '$.references'), '[]')) "
            "WHERE value = ?))"
        )
        args = (*_LIVE_PHASES, *((reference_id,) if include_creation else ()), reference_id)
        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute(query, args) as cur:
                    rows = await cur.fetchall()
        except aiosqlite.OperationalError as exc:
            # No jobs table yet (a registry used on its own, in a test).
            logger.debug("jobs_using: %s", exc)
            return []
        return sorted(r[0] for r in rows)


# ---------------------------------------------------------------------------
# Process-wide instances
# ---------------------------------------------------------------------------

reference_registry = ReferenceRegistry(DB_PATH)

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
