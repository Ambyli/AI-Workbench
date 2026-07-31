"""Persistent "have I processed this external event before?" tracker.

Tracks (key, status, timestamp, metadata) for events consumed from an
external stream — Gmail messages, webhook deliveries, Kafka offsets,
whatever. Callers check ``is_processed(key)`` before doing work and call
``record(key, status="ok", ...)`` after success. A separate ``pending``
status lets you mark "started but not finished" so a crash mid-processing
can be distinguished from a fresh event on the next tick.

Two backends with the same public API — pick by what you need:

- ``ProcessedStore``          — SQLite, single file, no separate service.
  Zero-setup for local dev, tests, and small projects. Query via
  ``json_extract(metadata, '$.some_field')``.
- ``PostgresProcessedStore``  — Postgres, JSONB metadata, remote-queryable.
  Use when you want operators / dashboards to inspect the store without
  ``docker cp`` or ``docker exec``. Query via ``metadata->>'some_field'``.

Both expose: ``is_processed``, ``get``, ``list_by_status``, ``record``,
``mark_pending`` / ``mark_ok`` / ``mark_error``, ``count``, and the
context-manager protocol.

Design
------
- One table, one primary key = the external event id. Straightforward dedup.
- Metadata is a JSON blob (TEXT in SQLite, JSONB in Postgres) — schema-free,
  no migrations.
- ``is_processed`` returns True ONLY for ``status="ok"``. Errors are retry-
  eligible; pending is treated as not-yet-done (so a crashed run gets retried).
- Not thread-safe across processes without care. SQLite's WAL handles
  concurrent readers plus one writer; Postgres handles both natively via
  its own MVCC. Within a single process both backends serialize at the
  connection layer.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional


@dataclass
class ProcessedRecord:
    """One row from the processed table."""
    key: str
    processed_at: datetime
    status: str          # "pending" | "ok" | "error"
    metadata: dict


class ProcessedStore:
    """SQLite-backed dedup / audit store for consumed external events."""

    SCHEMA = """
        CREATE TABLE IF NOT EXISTS processed (
            key           TEXT PRIMARY KEY,
            processed_at  TEXT NOT NULL,
            status        TEXT NOT NULL,
            metadata      TEXT NOT NULL DEFAULT '{}'
        );
    """
    STATUS_INDEX = "CREATE INDEX IF NOT EXISTS idx_processed_status ON processed(status);"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, isolation_level=None)  # autocommit
        conn.row_factory = sqlite3.Row
        # WAL mode: readers don't block writers; better for a poller that
        # reads on every tick and writes at the end.
        conn.execute("PRAGMA journal_mode = WAL;")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(self.SCHEMA)
            conn.execute(self.STATUS_INDEX)

    # ── Reads ──────────────────────────────────────────────────────────────

    def is_processed(self, key: str) -> bool:
        """True iff a row exists for `key` AND its status is 'ok'.

        Pending or errored rows return False — the caller will retry.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM processed WHERE key = ?", (key,)
            ).fetchone()
        return row is not None and row["status"] == "ok"

    def get(self, key: str) -> Optional[ProcessedRecord]:
        """Return the full record for `key`, regardless of status. None if unknown."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT key, processed_at, status, metadata FROM processed WHERE key = ?",
                (key,),
            ).fetchone()
        return _row_to_record(row) if row else None

    def list_by_status(self, status: str) -> Iterator[ProcessedRecord]:
        """Iterate every row with the given status. Useful for retry loops
        over ``status="error"`` or diagnostic sweeps over ``status="pending"``."""
        with self._connect() as conn:
            for row in conn.execute(
                "SELECT key, processed_at, status, metadata FROM processed "
                "WHERE status = ? ORDER BY processed_at",
                (status,),
            ):
                yield _row_to_record(row)

    # ── Writes ─────────────────────────────────────────────────────────────

    def record(
        self,
        key: str,
        status: str = "ok",
        metadata: Optional[dict] = None,
    ) -> ProcessedRecord:
        """Upsert one row. Returns the record as written.

        Rewrites ``processed_at`` on every call so the timestamp reflects the
        latest state transition, not the first sighting.
        """
        if status not in ("pending", "ok", "error"):
            raise ValueError(
                f"status must be one of 'pending' | 'ok' | 'error', got {status!r}"
            )
        now = datetime.now(timezone.utc)
        meta_json = json.dumps(metadata or {})
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO processed (key, processed_at, status, metadata) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET "
                "  processed_at = excluded.processed_at, "
                "  status = excluded.status, "
                "  metadata = excluded.metadata",
                (key, now.isoformat(timespec="seconds"), status, meta_json),
            )
        return ProcessedRecord(
            key=key, processed_at=now, status=status, metadata=metadata or {}
        )

    def mark_pending(self, key: str, metadata: Optional[dict] = None) -> ProcessedRecord:
        """Sugar for ``.record(key, "pending", metadata)``."""
        return self.record(key, "pending", metadata)

    def mark_ok(self, key: str, metadata: Optional[dict] = None) -> ProcessedRecord:
        """Sugar for ``.record(key, "ok", metadata)``."""
        return self.record(key, "ok", metadata)

    def mark_error(self, key: str, metadata: Optional[dict] = None) -> ProcessedRecord:
        """Sugar for ``.record(key, "error", metadata)``. Metadata should
        typically include an ``error`` field with the exception message."""
        return self.record(key, "error", metadata)

    # ── Housekeeping ───────────────────────────────────────────────────────

    def count(self, status: Optional[str] = None) -> int:
        """Row count, optionally filtered by status."""
        with self._connect() as conn:
            if status is None:
                row = conn.execute("SELECT COUNT(*) AS n FROM processed").fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM processed WHERE status = ?", (status,)
                ).fetchone()
        return int(row["n"])


def _row_to_record(row: sqlite3.Row) -> ProcessedRecord:
    return ProcessedRecord(
        key=row["key"],
        processed_at=datetime.fromisoformat(row["processed_at"]),
        status=row["status"],
        metadata=json.loads(row["metadata"] or "{}"),
    )


# ═══════════════════════════════════════════════════════════════════════════
# Postgres backend
# ═══════════════════════════════════════════════════════════════════════════


class PostgresProcessedStore:
    """Postgres-backed dedup / audit store — same public API as ProcessedStore.

    Use when you want operators to query the store remotely (DBeaver, psql,
    dashboards) without shuffling the SQLite file around.

    Holds one persistent psycopg2 connection in autocommit mode. Each call
    opens a short-lived cursor. Fine for tick-based workloads (a few dozen
    writes per tick); if you want higher throughput or thread-safety across
    workers, wrap the connection in a pool at the caller.

    ``metadata`` is stored as JSONB, so downstream queries can use Postgres's
    native operators:

        SELECT * FROM processed
        WHERE metadata->>'action' = 'escalate'
          AND (metadata->>'forwarded')::bool = false;
    """

    SCHEMA = """
        CREATE TABLE IF NOT EXISTS processed (
            key           TEXT PRIMARY KEY,
            processed_at  TIMESTAMPTZ NOT NULL,
            status        TEXT NOT NULL,
            metadata      JSONB NOT NULL DEFAULT '{}'::jsonb
        );
        CREATE INDEX IF NOT EXISTS idx_processed_status ON processed(status);
    """

    def __init__(self, dsn: str) -> None:
        # Local imports so callers that only need the SQLite backend don't
        # incur a psycopg2 dependency.
        import psycopg2
        from psycopg2.extras import RealDictCursor, Json

        self._Json = Json
        self._RealDictCursor = RealDictCursor
        self._conn = psycopg2.connect(dsn)
        self._conn.autocommit = True
        self._init_schema()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        try:
            self._conn.close()
        except Exception:
            pass
        return False

    def _init_schema(self) -> None:
        with self._conn.cursor() as cur:
            cur.execute(self.SCHEMA)

    def _cursor(self):
        return self._conn.cursor(cursor_factory=self._RealDictCursor)

    # ── Reads ──────────────────────────────────────────────────────────────

    def is_processed(self, key: str) -> bool:
        """True iff a row exists for `key` AND its status is 'ok'."""
        with self._cursor() as cur:
            cur.execute("SELECT status FROM processed WHERE key = %s", (key,))
            row = cur.fetchone()
        return row is not None and row["status"] == "ok"

    def get(self, key: str) -> Optional[ProcessedRecord]:
        """Return the full record for `key`, regardless of status."""
        with self._cursor() as cur:
            cur.execute(
                "SELECT key, processed_at, status, metadata "
                "FROM processed WHERE key = %s",
                (key,),
            )
            row = cur.fetchone()
        return _pg_row_to_record(row) if row else None

    def list_by_status(self, status: str) -> Iterator[ProcessedRecord]:
        """Iterate every row with the given status."""
        with self._cursor() as cur:
            cur.execute(
                "SELECT key, processed_at, status, metadata "
                "FROM processed WHERE status = %s ORDER BY processed_at",
                (status,),
            )
            for row in cur:
                yield _pg_row_to_record(row)

    # ── Writes ─────────────────────────────────────────────────────────────

    def record(
        self,
        key: str,
        status: str = "ok",
        metadata: Optional[dict] = None,
    ) -> ProcessedRecord:
        """Upsert one row. Returns the record as written."""
        if status not in ("pending", "ok", "error"):
            raise ValueError(
                f"status must be one of 'pending' | 'ok' | 'error', got {status!r}"
            )
        now = datetime.now(timezone.utc)
        meta = metadata or {}
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO processed (key, processed_at, status, metadata) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (key) DO UPDATE SET "
                "  processed_at = EXCLUDED.processed_at, "
                "  status       = EXCLUDED.status, "
                "  metadata     = EXCLUDED.metadata",
                (key, now, status, self._Json(meta)),
            )
        return ProcessedRecord(
            key=key, processed_at=now, status=status, metadata=meta
        )

    def mark_pending(self, key: str, metadata: Optional[dict] = None) -> ProcessedRecord:
        return self.record(key, "pending", metadata)

    def mark_ok(self, key: str, metadata: Optional[dict] = None) -> ProcessedRecord:
        return self.record(key, "ok", metadata)

    def mark_error(self, key: str, metadata: Optional[dict] = None) -> ProcessedRecord:
        return self.record(key, "error", metadata)

    # ── Housekeeping ───────────────────────────────────────────────────────

    def count(self, status: Optional[str] = None) -> int:
        with self._cursor() as cur:
            if status is None:
                cur.execute("SELECT COUNT(*) AS n FROM processed")
            else:
                cur.execute(
                    "SELECT COUNT(*) AS n FROM processed WHERE status = %s",
                    (status,),
                )
            row = cur.fetchone()
        return int(row["n"])


def _pg_row_to_record(row: dict) -> ProcessedRecord:
    """Postgres row → ProcessedRecord. `processed_at` is already a tz-aware
    datetime from psycopg2; `metadata` is already a dict (JSONB auto-decode).
    """
    return ProcessedRecord(
        key=row["key"],
        processed_at=row["processed_at"],
        status=row["status"],
        metadata=row["metadata"] or {},
    )
