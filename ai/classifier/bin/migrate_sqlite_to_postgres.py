"""One-shot: copy the classifier's old SQLite state into classifier-db Postgres.

Until the move to Postgres the classifier kept everything in one SQLite file,
``/data/classifier.db`` on the ``classifier_data`` volume. The new container
starts on an EMPTY ``classifier-db`` and never reads that file; this script
moves its three tables across, once, run by the operator:

    # The intended order: BEFORE the new classifier first starts —
    # classifier-db up, the old container stopped, the script in a one-off
    # container of the new image (same volume, same env):
    make build classifier
    make up classifier classifier-db
    docker stop classifier
    docker compose -f ai/classifier/docker-compose.classifier.yml --env-file .env \\
        -p ai-classifier run --rm --no-deps classifier \\
        uv run python bin/migrate_sqlite_to_postgres.py      # --dry-run to count first
    make up classifier

    # Or, if the new classifier is already running:
    docker exec classifier uv run python bin/migrate_sqlite_to_postgres.py

Why before: a running classifier prunes what its database does not know —
queued payloads whose row is missing, and EVERY job artifact directory whose
row is missing (the TTL sweeper's orphan pass). Migrating first means the
rows are there when it looks. Started first anyway, ``main``'s startup guard
holds both sweeps back (with an ERROR in the log) for as long as this file
exists without the ``classifier.db.migrated`` marker a real run of this
script writes beside it — so the ``docker exec`` route loses nothing either,
provided the classifier is restarted afterwards (``make up classifier``) to
turn the sweeps back on.

What it copies, in order:

    reference_examples  the one that matters. References never expire, and
                        their files on the volume (CLASSIFIER_REFERENCE_DIR,
                        one directory per id) are named by ids only this table
                        knows. ``tags`` / ``record`` move from JSON text to
                        JSONB / JSON, the timestamps from ISO text to
                        TIMESTAMPTZ.
    jobs                every row, so finished results stay readable until
                        JOB_TTL_HOURS expires them. A job that was NOT finished
                        (``staging`` / ``pending`` / ``processing``) keeps its
                        phase when its payload file is still in PAYLOAD_DIR —
                        it is, when this runs before the new container's first
                        start — and the new workers run it (startup recovery
                        requeues ``staging`` / ``processing``). Without the
                        payload (the first start sweeps any whose row is
                        missing) it cannot be resumed, and arrives ``failed``
                        with an error saying to resubmit. Let the queue drain
                        before upgrading and there are none. A pre-``common.jobs`` row
                        (``status`` instead of ``phase``) is read too.
    llm_calls           when the table exists (the usage-records change shipped
                        on SQLite first). Ids are kept, and the BIGSERIAL
                        sequence is moved past the largest one, so a re-run
                        recognises every row and new calls never collide.
                        ``usage_json`` text becomes the ``usage`` JSONB column.

Idempotent: every insert is ``ON CONFLICT (id) DO NOTHING``, so a second run
copies nothing and says so — the counts it prints are read / inserted /
already present / unreadable per table. It never modifies or deletes the
SQLite file (it is opened read-only); remove it by hand once the counts look
right, which also silences the startup warning ``main`` logs while the file is
there and ``reference_examples`` is empty.

Finally it runs the same reference reconcile the lifespan runs at startup, so
a reference still ``pending`` on a creation job that came across ``failed``
is failed now rather than at the next restart.

Process flow position: none — a standalone operator tool (the Dockerfile
ships ``bin/`` in the image). It uses the classifier's own config, pool and
stores, so the tables it creates are exactly the ones the service uses.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVICE = os.path.dirname(_HERE)
if _SERVICE not in sys.path:  # run as `python bin/…` from ai/classifier, or from anywhere
    sys.path.insert(0, _SERVICE)

from common.jobs.payloads import FilePayloadStore  # noqa: E402

from config import LEGACY_SQLITE_MARKER, LEGACY_SQLITE_PATH, PAYLOAD_DIR  # noqa: E402
from db import database  # noqa: E402
from jobs.queue import jobs_registry  # noqa: E402
from llm import usage as llm_usage  # noqa: E402
from references.store import reconcile, reference_registry  # noqa: E402

# Phases of a job that had not finished when the old container stopped.
_UNFINISHED = ("staging", "pending", "processing")
_UNFINISHED_ERROR = (
    "not finished when the classifier moved from SQLite to Postgres "
    "(bin/migrate_sqlite_to_postgres.py) — its queued input was not carried "
    "over; resubmit the request"
)

# Rows per INSERT batch.
_CHUNK = 500

# Where a queued job's input lives — the same store the queue reads.
_PAYLOADS = FilePayloadStore(PAYLOAD_DIR)


class Skip(ValueError):
    """A row that cannot be converted (an unreadable timestamp)."""


# ---------------------------------------------------------------------------
# Value conversion
# ---------------------------------------------------------------------------


def _ts(value: Any) -> datetime:
    """ISO-8601 text → aware UTC datetime (naive is read as UTC)."""
    if not value:
        raise Skip("empty timestamp")
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise Skip(f"unreadable timestamp {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _json_text(value: Any, *, default: Optional[str], want: Optional[type] = None) -> Optional[str]:
    """JSON text from SQLite → JSON text for Postgres, re-serialised so that
    anything Postgres would reject becomes ``default`` instead of failing the
    batch. ``want`` (dict / list) also rejects valid JSON of the wrong shape.
    Key order is preserved (``json.loads`` keeps it), which matters for the
    ``json`` columns."""
    if value is None or value == "":
        return default
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default
    if want is not None and not isinstance(parsed, want):
        return default
    return json.dumps(parsed)


def _int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Reading SQLite
# ---------------------------------------------------------------------------


def _columns(db: sqlite3.Connection, table: str) -> Optional[set[str]]:
    """The table's column names, or None when it does not exist."""
    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    if row is None:
        return None
    return {r[1] for r in db.execute(f"PRAGMA table_info({table})")}


def _rows(db: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    return db.execute(f"SELECT * FROM {table}").fetchall()


def _get(row: sqlite3.Row, cols: set[str], name: str) -> Any:
    return row[name] if name in cols else None


# ---------------------------------------------------------------------------
# Per-table conversion: SQLite row → Postgres parameter tuple
# ---------------------------------------------------------------------------

_REF_COLUMNS = (
    "id", "status", "title", "description", "description_source", "tags",
    "job_id", "source_kind", "source_job_id", "source_item", "error",
    "created_at", "updated_at", "record",
)


def _reference(row: sqlite3.Row, cols: set[str]) -> tuple:
    return (
        row["id"],
        _get(row, cols, "status") or "failed",
        _get(row, cols, "title"),
        _get(row, cols, "description"),
        _get(row, cols, "description_source"),
        _json_text(_get(row, cols, "tags"), default="[]", want=list),
        _get(row, cols, "job_id"),
        _get(row, cols, "source_kind") or "document",
        _get(row, cols, "source_job_id"),
        _int(_get(row, cols, "source_item")),
        _get(row, cols, "error"),
        _ts(_get(row, cols, "created_at")),
        _ts(_get(row, cols, "updated_at") or _get(row, cols, "created_at")),
        _json_text(_get(row, cols, "record"), default=None, want=dict),
    )


_JOB_COLUMNS = ("id", "phase", "created_at", "updated_at", "metadata", "result", "error")


def _job(row: sqlite3.Row, cols: set[str]) -> tuple:
    phase = _get(row, cols, "phase") or _get(row, cols, "status") or "failed"
    error = _get(row, cols, "error")
    metadata = _json_text(_get(row, cols, "metadata"), default=None, want=dict)
    if metadata is None:
        # The pre-common.jobs schema kept type / request_id in their own
        # columns; SqliteRegistry back-filled them into metadata the same way.
        legacy = {k: row[k] for k in ("type", "request_id") if k in cols and row[k] is not None}
        metadata = json.dumps(legacy)
    if phase in _UNFINISHED and not _PAYLOADS.path(row["id"]).exists():
        phase, error = "failed", _UNFINISHED_ERROR
    return (
        row["id"],
        phase,
        _ts(_get(row, cols, "created_at")),
        _ts(_get(row, cols, "updated_at") or _get(row, cols, "created_at")),
        metadata,
        _json_text(_get(row, cols, "result"), default=None, want=dict),
        error,
    )


_CALL_COLUMNS = ("id", *llm_usage.COLUMNS)
_INT_CALL_COLUMNS = {
    "item", "document", "attempt", "max_tokens", "http_status", "prompt_tokens",
    "cached_tokens", "completion_tokens", "reasoning_tokens", "total_tokens",
}


def _call(row: sqlite3.Row, cols: set[str]) -> tuple:
    out: list[Any] = []
    for column in _CALL_COLUMNS:
        if column == "id":
            out.append(int(row["id"]))
        elif column == "started_at":
            out.append(_ts(_get(row, cols, "started_at")))
        elif column == "usage":
            out.append(_json_text(_get(row, cols, "usage_json"), default=None, want=dict))
        elif column == "seconds":
            out.append(_float(_get(row, cols, "seconds")) or 0.0)
        elif column in _INT_CALL_COLUMNS:
            out.append(_int(_get(row, cols, column)))
        elif column in ("kind", "outcome"):
            out.append(_get(row, cols, column) or "unknown")
        else:
            out.append(_get(row, cols, column))
    return tuple(out)


# ---------------------------------------------------------------------------
# Writing Postgres
# ---------------------------------------------------------------------------


def _insert_sql(table: str, columns: Iterable[str], casts: dict[str, str]) -> str:
    columns = list(columns)
    marks = [f"${n}{casts.get(c, '')}" for n, c in enumerate(columns, start=1)]
    return (
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join(marks)}) "
        "ON CONFLICT (id) DO NOTHING"
    )


async def _copy(
    conn: Any,
    *,
    table: str,
    rows: list[sqlite3.Row],
    cols: set[str],
    convert: Callable[[sqlite3.Row, set[str]], tuple],
    columns: tuple[str, ...],
    casts: dict[str, str],
    dry_run: bool,
) -> dict[str, int]:
    """Convert and insert ``rows``; ``{read, inserted, present, unreadable}``.

    "present" is counted BEFORE each batch (ids already in the table), so the
    numbers stay right even while the running classifier writes new rows —
    those have ids this file never had.
    """
    stats = {"read": len(rows), "inserted": 0, "present": 0, "unreadable": 0}
    converted: list[tuple] = []
    for row in rows:
        try:
            converted.append(convert(row, cols))
        except Skip as exc:
            stats["unreadable"] += 1
            print(f"  {table}: skipping id={row['id']!r}: {exc}")
    id_type = "bigint[]" if table == "llm_calls" else "text[]"
    sql = _insert_sql(table, columns, casts)
    for start in range(0, len(converted), _CHUNK):
        batch = converted[start:start + _CHUNK]
        ids = [r[0] for r in batch]
        present = await conn.fetchval(
            f"SELECT COUNT(*) FROM {table} WHERE id = ANY($1::{id_type})", ids,
        )
        stats["present"] += int(present)
        if not dry_run:
            async with conn.transaction():
                await conn.executemany(sql, batch)
        stats["inserted"] += len(batch) - int(present)
    return stats


async def migrate(path: str, *, dry_run: bool = False) -> dict[str, dict[str, int]]:
    """Copy every table the SQLite file at ``path`` has; per-table counts."""
    source = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    report: dict[str, dict[str, int]] = {}
    await database.init()
    try:
        # The exact tables the service uses — created now if the classifier
        # has not started against this database yet.
        await jobs_registry.init()
        await reference_registry.init()
        await llm_usage.usage_store.init()
        result_cast = f"::{jobs_registry.result_type}"
        plan = (
            ("reference_examples", _reference, _REF_COLUMNS,
             {"tags": "::jsonb", "record": "::json"}),
            ("jobs", _job, _JOB_COLUMNS, {"metadata": "::jsonb", "result": result_cast}),
            ("llm_calls", _call, _CALL_COLUMNS, {"usage": "::jsonb"}),
        )
        async with database.acquire() as conn:
            for table, convert, columns, casts in plan:
                cols = _columns(source, table)
                if cols is None:
                    print(f"{table}: not in {path} — nothing to copy")
                    continue
                report[table] = await _copy(
                    conn, table=table, rows=_rows(source, table), cols=cols,
                    convert=convert, columns=columns, casts=casts, dry_run=dry_run,
                )
            if not dry_run and report.get("llm_calls", {}).get("read"):
                # Ids were copied explicitly, so the sequence never advanced.
                await conn.execute(
                    "SELECT setval(pg_get_serial_sequence('llm_calls', 'id'), "
                    "GREATEST((SELECT COALESCE(MAX(id), 0) FROM llm_calls), 1))"
                )
        if not dry_run and "reference_examples" in report:
            failed = await reconcile(jobs_registry)
            if failed:
                print(f"reference_examples: {failed} pending reference(s) reconciled to failed")
    finally:
        source.close()
        await database.close()
    return report


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Copy the classifier's old SQLite state (jobs, reference_examples, "
                    "llm_calls) into classifier-db Postgres. Idempotent.",
    )
    parser.add_argument("--sqlite", default=LEGACY_SQLITE_PATH,
                        help=f"the old database file (default {LEGACY_SQLITE_PATH})")
    parser.add_argument("--dry-run", action="store_true",
                        help="read and count, write nothing")
    args = parser.parse_args(argv)

    if not os.path.exists(args.sqlite):
        print(f"{args.sqlite} does not exist — nothing to migrate.")
        return 0
    print(f"{'DRY RUN — ' if args.dry_run else ''}migrating {args.sqlite} → classifier-db")
    report = asyncio.run(migrate(args.sqlite, dry_run=args.dry_run))
    verb = "would insert" if args.dry_run else "inserted"
    for table, s in report.items():
        print(f"{table}: read {s['read']}, {verb} {s['inserted']}, "
              f"already present {s['present']}, unreadable {s['unreadable']}")
    if not args.dry_run:
        # The marker lifts the startup guard in main: until it exists, a
        # classifier that finds the old file skips its orphan sweeps. Only
        # written for the default path — a migration of some other file says
        # nothing about the one on the volume.
        if os.path.abspath(args.sqlite) == os.path.abspath(LEGACY_SQLITE_PATH):
            with open(LEGACY_SQLITE_MARKER, "w", encoding="utf-8") as fh:
                json.dump({"migrated_at": datetime.now(timezone.utc).isoformat(),
                           "report": report}, fh, indent=2)
            print(f"Wrote {LEGACY_SQLITE_MARKER} — restart the classifier "
                  "(make up classifier) so its sweeps run again.")
        print(f"Done. Check the counts, then remove {args.sqlite} by hand.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
