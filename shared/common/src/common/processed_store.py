"""Persistent "have I processed this external event before?" tracker.

A single SQLite file tracks (key, status, timestamp, metadata) for events
consumed from an external stream — Gmail messages, webhook deliveries, Kafka
offsets, whatever. Callers check ``is_processed(key)`` before doing work and
call ``record(key, status="ok", ...)`` after success. A separate ``pending``
status lets you mark "started but not finished" so a crash mid-processing can
be distinguished from a fresh event on the next tick.

Design
------
- SQLite (single-file, no separate service, journaled writes) so a crashed
  process doesn't corrupt the store.
- One table, one primary key = the external event id. Straightforward dedup.
- Metadata is a JSON blob — schema-free, no migrations. Query with
  ``json_extract(metadata, '$.some_field')`` if you need secondary lookups.
- ``is_processed`` returns True ONLY for ``status="ok"``. Errors are retry-
  eligible; pending is treated as not-yet-done (so a crashed run gets retried).
- Not thread-safe across processes without care — SQLite's own locking handles
  concurrent WRITES within one process, but if you fan out across processes,
  serialize at the caller.
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
