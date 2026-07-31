"""Tests for common.jobs.sqlite (SqliteRegistry).

Uses a tmp_path DB file. In-memory `:memory:` isn't used because each
aiosqlite.connect() call opens a fresh connection; a :memory: DB is
per-connection and wouldn't survive between calls.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from pydantic import BaseModel

from common.jobs.sqlite import SqliteRegistry


class _Meta(BaseModel):
    type: str
    request_id: str


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "jobs.db")


def _run(coro):
    return asyncio.run(coro)


def test_init_creates_schema(db_path: str) -> None:
    reg = SqliteRegistry(db_path)
    _run(reg.init())
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("PRAGMA table_info(jobs)").fetchall()
    cols = {r[1] for r in rows}
    assert cols == {
        "id",
        "phase",
        "created_at",
        "updated_at",
        "metadata",
        "result",
        "error",
    }


def test_init_is_idempotent(db_path: str) -> None:
    reg = SqliteRegistry(db_path)
    _run(reg.init())
    _run(reg.init())
    _run(reg.init())
    # Should still be one table, seven columns.
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("PRAGMA table_info(jobs)").fetchall()
    assert len(rows) == 7


def test_register_get_roundtrip(db_path: str) -> None:
    reg = SqliteRegistry(db_path)
    _run(reg.init())
    job_id = _run(reg.register(_Meta(type="assess", request_id="req-1"), "pending"))
    assert len(job_id) == 12  # uuid4().hex[:12]
    got = _run(reg.get(job_id))
    assert got is not None
    assert got.job_id == job_id
    assert got.phase == "pending"
    assert got.metadata == {"type": "assess", "request_id": "req-1"}
    assert got.result is None
    assert got.error is None


def test_set_phase_result_error(db_path: str) -> None:
    reg = SqliteRegistry(db_path)
    _run(reg.init())
    job_id = _run(reg.register(_Meta(type="assess", request_id="r"), "pending"))
    _run(reg.set_phase(job_id, "processing"))
    assert _run(reg.get(job_id)).phase == "processing"
    _run(reg.set_result(job_id, {"score": 0.9}))
    got = _run(reg.get(job_id))
    assert got.phase == "completed"
    assert got.result == {"score": 0.9}
    # Setting error moves it to "failed" and populates error.
    job_id2 = _run(reg.register(_Meta(type="assess", request_id="r"), "pending"))
    _run(reg.set_error(job_id2, "boom"))
    got2 = _run(reg.get(job_id2))
    assert got2.phase == "failed"
    assert got2.error == "boom"


def test_update_metadata_merges(db_path: str) -> None:
    reg = SqliteRegistry(db_path)
    _run(reg.init())
    job_id = _run(reg.register({"a": 1, "b": 2}, "running"))
    _run(reg.update_metadata(job_id, {"b": 20, "c": 3}))
    got = _run(reg.get(job_id))
    assert got.metadata == {"a": 1, "b": 20, "c": 3}


def test_list_all_orders_by_updated_at_desc(db_path: str) -> None:
    reg = SqliteRegistry(db_path)
    _run(reg.init())
    ids = [
        _run(reg.register(_Meta(type="assess", request_id=f"r{i}"), "pending"))
        for i in range(3)
    ]
    # Bump the middle one so it's freshest.
    _run(reg.set_phase(ids[1], "processing"))
    got = _run(reg.list_all(limit=10))
    assert got.active_count == 3
    assert got.jobs[0].job_id == ids[1]  # most recently updated first


def test_cancel_transitions_phase_and_refuses_terminal(db_path: str) -> None:
    reg = SqliteRegistry(db_path)
    _run(reg.init())
    job_id = _run(reg.register(_Meta(type="assess", request_id="r"), "pending"))
    ok, was = _run(reg.cancel(job_id))
    assert (ok, was) == (True, "pending")
    assert _run(reg.get(job_id)).phase == "cancelled"

    # Terminal → refuse.
    ok2, reason = _run(reg.cancel(job_id))
    assert ok2 is False
    assert reason == "already_cancelled"

    # Unknown → not_found.
    ok3, reason3 = _run(reg.cancel("nonexistent"))
    assert ok3 is False
    assert reason3 == "not_found"


def test_delete(db_path: str) -> None:
    reg = SqliteRegistry(db_path)
    _run(reg.init())
    job_id = _run(reg.register(_Meta(type="assess", request_id="r"), "pending"))
    assert _run(reg.delete(job_id)) is True
    assert _run(reg.get(job_id)) is None
    assert _run(reg.delete(job_id)) is False


def test_legacy_schema_migration(tmp_path: Path) -> None:
    """A legacy classifier-shaped DB is migrated in place by init()."""
    db_path = str(tmp_path / "legacy.db")
    # Create the OLD schema and seed one row.
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE jobs (
                id         TEXT PRIMARY KEY,
                status     TEXT NOT NULL DEFAULT 'pending',
                type       TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                result     TEXT,
                error      TEXT,
                request_id TEXT
            );
            """
        )
        conn.execute(
            "INSERT INTO jobs (id, status, type, created_at, updated_at, request_id) "
            "VALUES ('old-id', 'completed', 'assess', "
            "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', 'req-old')"
        )
        conn.commit()

    reg = SqliteRegistry(db_path)
    _run(reg.init())

    # Schema should now have `phase` and `metadata`.
    with sqlite3.connect(db_path) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    assert "phase" in cols
    assert "metadata" in cols

    # Legacy row's metadata should be back-filled from type + request_id.
    got = _run(reg.get("old-id"))
    assert got is not None
    assert got.phase == "completed"
    assert got.metadata == {"type": "assess", "request_id": "req-old"}


def test_migration_is_idempotent(tmp_path: Path) -> None:
    db_path = str(tmp_path / "legacy.db")
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE jobs (
                id         TEXT PRIMARY KEY,
                status     TEXT NOT NULL DEFAULT 'pending',
                type       TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                result     TEXT,
                error      TEXT,
                request_id TEXT
            );
            """
        )
        conn.commit()

    reg = SqliteRegistry(db_path)
    _run(reg.init())
    _run(reg.init())  # second run: no-op, no errors
    _run(reg.init())  # third
