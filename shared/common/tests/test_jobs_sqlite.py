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


# ── Queue operations ──────────────────────────────────────────────────────
def test_claim_next_is_fifo_and_flips_phase(db_path: str) -> None:
    reg = SqliteRegistry(db_path)
    _run(reg.init())
    a = _run(reg.register(_Meta(type="assess", request_id="a")))
    b = _run(reg.register(_Meta(type="assess", request_id="b")))

    first = _run(reg.claim_next())
    assert first is not None
    assert first.job_id == a
    assert first.phase == "processing"
    assert _run(reg.get(a)).phase == "processing"
    assert _run(reg.get(b)).phase == "pending"

    second = _run(reg.claim_next())
    assert second.job_id == b
    assert _run(reg.claim_next()) is None


def test_claim_next_ignores_other_phases(db_path: str) -> None:
    reg = SqliteRegistry(db_path)
    _run(reg.init())
    _run(reg.register(_Meta(type="assess", request_id="s"), initial_phase="staging"))
    done = _run(reg.register(_Meta(type="assess", request_id="d")))
    _run(reg.set_result(done, {"ok": True}))
    assert _run(reg.claim_next()) is None


def test_concurrent_claims_never_hand_out_the_same_job(db_path: str) -> None:
    """N workers racing on claim_next must get N distinct jobs, and once the
    queue is drained every further claim returns None."""
    reg = SqliteRegistry(db_path)
    _run(reg.init())
    ids = {_run(reg.register(_Meta(type="assess", request_id=str(i)))) for i in range(8)}

    async def race():
        return await asyncio.gather(*(reg.claim_next() for _ in range(12)))

    claimed = _run(race())
    got = [j.job_id for j in claimed if j is not None]
    assert len(got) == 8
    assert set(got) == ids
    assert sum(1 for j in claimed if j is None) == 4


def test_reset_phase_requeues_processing(db_path: str) -> None:
    reg = SqliteRegistry(db_path)
    _run(reg.init())
    a = _run(reg.register(_Meta(type="assess", request_id="a")))
    b = _run(reg.register(_Meta(type="assess", request_id="b")))
    done = _run(reg.register(_Meta(type="assess", request_id="c")))
    _run(reg.claim_next())
    _run(reg.claim_next())
    _run(reg.set_result(done, {"ok": True}))

    moved = _run(reg.reset_phase("processing", "pending"))
    assert moved == 2
    assert _run(reg.get(a)).phase == "pending"
    assert _run(reg.get(b)).phase == "pending"
    assert _run(reg.get(done)).phase == "completed"
    assert _run(reg.reset_phase("processing", "pending")) == 0


def test_count_by_phase(db_path: str) -> None:
    reg = SqliteRegistry(db_path)
    _run(reg.init())
    assert _run(reg.count_by_phase()) == {}
    for _ in range(3):
        _run(reg.register(_Meta(type="assess", request_id="x")))
    _run(reg.claim_next())
    assert _run(reg.count_by_phase()) == {"pending": 2, "processing": 1}


# ── expired_job_ids (retention) ────────────────────────────────────────────
def _age(reg: SqliteRegistry, job_id: str, hours: float, *, suffix: str = "") -> None:
    """Backdate a row's created_at, optionally in a different ISO spelling.

    ``suffix="Z"`` writes the same instant the way a producer that used
    ``strftime`` would; the query has to treat it as equal to ``+00:00``.
    """
    from datetime import datetime, timedelta, timezone

    stamp = datetime.now(timezone.utc) - timedelta(hours=hours)
    text = stamp.isoformat() if not suffix else (
        stamp.replace(tzinfo=None).isoformat(timespec="seconds") + suffix
    )
    with sqlite3.connect(reg.db_path) as conn:
        conn.execute("UPDATE jobs SET created_at = ? WHERE id = ?", (text, job_id))
        conn.commit()


def _cutoff(hours: float):
    from datetime import datetime, timedelta, timezone

    return datetime.now(timezone.utc) - timedelta(hours=hours)


def test_expired_job_ids_returns_only_aged_terminal_rows(db_path: str) -> None:
    reg = SqliteRegistry(db_path)
    _run(reg.init())
    old = _run(reg.register(_Meta(type="assess", request_id="old")))
    fresh = _run(reg.register(_Meta(type="assess", request_id="fresh")))
    running = _run(reg.register(_Meta(type="assess", request_id="run")))
    _run(reg.set_result(old, {"ok": True}))
    _run(reg.set_result(fresh, {"ok": True}))
    _run(reg.set_phase(running, "processing"))
    _age(reg, old, 48)
    _age(reg, running, 999)

    assert _run(reg.expired_job_ids(_cutoff(24))) == [old]


def test_expired_job_ids_is_unbounded_unless_limited(db_path: str) -> None:
    reg = SqliteRegistry(db_path)
    _run(reg.init())
    ids = []
    for i in range(7):
        job_id = _run(reg.register(_Meta(type="assess", request_id=str(i))))
        _run(reg.set_result(job_id, {"ok": True}))
        _age(reg, job_id, 48 + i)
        ids.append(job_id)

    got = _run(reg.expired_job_ids(_cutoff(24)))
    assert set(got) == set(ids)
    assert len(_run(reg.expired_job_ids(_cutoff(24), limit=3))) == 3


def test_expired_job_ids_is_oldest_first(db_path: str) -> None:
    reg = SqliteRegistry(db_path)
    _run(reg.init())
    newer = _run(reg.register(_Meta(type="assess", request_id="newer")))
    older = _run(reg.register(_Meta(type="assess", request_id="older")))
    _run(reg.set_result(newer, {"ok": True}))
    _run(reg.set_result(older, {"ok": True}))
    _age(reg, newer, 30)
    _age(reg, older, 90)

    assert _run(reg.expired_job_ids(_cutoff(24))) == [older, newer]


def test_expired_job_ids_treats_a_Z_suffix_as_utc(db_path: str) -> None:
    """`...T10:00:00Z` and `...T10:00:00+00:00` are the same instant but do
    NOT sort alike as raw strings — the query compares on the first 19
    characters so the spelling cannot change the answer."""
    reg = SqliteRegistry(db_path)
    _run(reg.init())
    zed = _run(reg.register(_Meta(type="assess", request_id="z")))
    offset = _run(reg.register(_Meta(type="assess", request_id="o")))
    _run(reg.set_result(zed, {"ok": True}))
    _run(reg.set_result(offset, {"ok": True}))
    _age(reg, zed, 48, suffix="Z")
    _age(reg, offset, 48)

    assert set(_run(reg.expired_job_ids(_cutoff(24)))) == {zed, offset}
    assert _run(reg.expired_job_ids(_cutoff(96))) == []


def test_expired_job_ids_honours_the_phase_filter(db_path: str) -> None:
    reg = SqliteRegistry(db_path)
    _run(reg.init())
    failed = _run(reg.register(_Meta(type="assess", request_id="f")))
    done = _run(reg.register(_Meta(type="assess", request_id="d")))
    _run(reg.set_error(failed, "boom"))
    _run(reg.set_result(done, {"ok": True}))
    _age(reg, failed, 48)
    _age(reg, done, 48)

    assert _run(reg.expired_job_ids(_cutoff(24), ["failed"])) == [failed]
    assert _run(reg.expired_job_ids(_cutoff(24), [])) == []


def test_expired_job_ids_accepts_a_naive_cutoff(db_path: str) -> None:
    """A caller that forgot the tzinfo gets UTC, not a TypeError from the
    comparison — the sweeper is the only caller and it always passes an aware
    one, but this is a public method now."""
    from datetime import datetime, timedelta, timezone

    reg = SqliteRegistry(db_path)
    _run(reg.init())
    job_id = _run(reg.register(_Meta(type="assess", request_id="n")))
    _run(reg.set_result(job_id, {"ok": True}))
    _age(reg, job_id, 48)

    naive = (datetime.now(timezone.utc) - timedelta(hours=24)).replace(tzinfo=None)
    assert _run(reg.expired_job_ids(naive)) == [job_id]
