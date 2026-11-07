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


def test_cancel_refuses_every_terminal_phase_and_leaves_the_row(db_path: str) -> None:
    reg = SqliteRegistry(db_path)
    _run(reg.init())
    done = _run(reg.register(_Meta(type="assess", request_id="d")))
    failed = _run(reg.register(_Meta(type="assess", request_id="f")))
    running = _run(reg.register(_Meta(type="assess", request_id="p"), "processing"))
    _run(reg.set_result(done, {"ok": True}))
    _run(reg.set_error(failed, "boom"))
    assert _run(reg.cancel(done)) == (False, "already_completed")
    assert _run(reg.cancel(failed)) == (False, "already_failed")
    assert _run(reg.get(done)).result == {"ok": True}
    assert _run(reg.get(failed)).error == "boom"
    assert _run(reg.cancel(running)) == (True, "processing")


def test_guarded_writes_say_whether_they_landed(db_path: str) -> None:
    reg = SqliteRegistry(db_path)
    _run(reg.init())
    job_id = _run(reg.register(_Meta(type="assess", request_id="g")))
    # set_phase: only from a listed phase.
    assert _run(reg.set_phase(job_id, "processing", from_phases=("pending",))) is True
    assert _run(reg.set_phase(job_id, "pending", from_phases=("staging",))) is False
    assert _run(reg.get(job_id)).phase == "processing"
    # set_result / set_error: refused outside the guard, nothing written.
    assert _run(reg.set_result(job_id, {"x": 1}, from_phases=("pending",))) is False
    assert _run(reg.set_error(job_id, "no", from_phases=["staging", "pending"])) is False
    got = _run(reg.get(job_id))
    assert (got.phase, got.result, got.error) == ("processing", None, None)
    assert _run(reg.set_result(job_id, {"x": 1}, from_phases=("processing",))) is True
    assert _run(reg.set_error(job_id, "late", from_phases=("processing",))) is False
    got = _run(reg.get(job_id))
    assert (got.phase, got.result, got.error) == ("completed", {"x": 1}, None)


def test_unguarded_writes_still_land_anywhere_and_report_a_missing_row(db_path: str) -> None:
    reg = SqliteRegistry(db_path)
    _run(reg.init())
    job_id = _run(reg.register(_Meta(type="assess", request_id="u")))
    _run(reg.cancel(job_id))
    # Exactly the old behaviour: no guard, so it overwrites even a cancel.
    assert _run(reg.set_result(job_id, {"ok": True})) is True
    assert _run(reg.get(job_id)).phase == "completed"
    assert _run(reg.set_phase("ghost", "pending")) is False
    assert _run(reg.set_result("ghost", {})) is False
    assert _run(reg.set_error("ghost", "x")) is False


def test_a_guard_that_names_no_phase_is_refused(db_path: str) -> None:
    reg = SqliteRegistry(db_path)
    _run(reg.init())
    job_id = _run(reg.register(_Meta(type="assess", request_id="b")))
    with pytest.raises(TypeError):
        _run(reg.set_phase(job_id, "pending", from_phases="processing"))
    with pytest.raises(ValueError):
        _run(reg.set_result(job_id, {}, from_phases=()))


def test_a_cancel_and_a_guarded_result_racing_have_one_winner(db_path: str) -> None:
    """Parity with the Postgres race test: whichever lands first, the other
    is refused, and the row says what the winner wrote."""
    reg = SqliteRegistry(db_path)
    _run(reg.init())

    async def race():
        outcomes = []
        for i in range(20):
            job_id = await reg.register(_Meta(type="assess", request_id=str(i)), "processing")
            stored, (cancelled, _) = await asyncio.gather(
                reg.set_result(job_id, {"i": i}, from_phases=("processing",)),
                reg.cancel(job_id),
            )
            row = await reg.get(job_id)
            outcomes.append((stored, cancelled, row.phase))
        return outcomes

    for stored, cancelled, phase in _run(race()):
        assert stored != cancelled
        assert phase == ("completed" if stored else "cancelled")


def test_delete(db_path: str) -> None:
    reg = SqliteRegistry(db_path)
    _run(reg.init())
    job_id = _run(reg.register(_Meta(type="assess", request_id="r"), "pending"))
    assert _run(reg.delete(job_id)) is True
    assert _run(reg.get(job_id)) is None
    assert _run(reg.delete(job_id)) is False


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


def test_cancel_after_commit_raises_cancelled_not_a_rollback_error(
    db_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancel does not stop SQL already in aiosqlite's thread, so it can
    arrive after the COMMIT landed; the cleanup's ROLLBACK then fails with
    "no transaction is active". That error used to replace the
    CancelledError, which left WorkerPool.stop() waiting forever on a worker
    that never unwound. Simulated deterministically: COMMIT runs, then the
    await raises CancelledError."""
    import aiosqlite

    reg = SqliteRegistry(db_path)
    _run(reg.init())
    jid = _run(reg.register(_Meta(type="assess", request_id="c")))

    real_execute = aiosqlite.Connection.execute

    def execute(self, sql, *args, **kwargs):
        result = real_execute(self, sql, *args, **kwargs)
        if sql != "COMMIT":
            return result

        async def commit_then_cancelled():
            await result
            raise asyncio.CancelledError()

        return commit_then_cancelled()

    monkeypatch.setattr(aiosqlite.Connection, "execute", execute)

    async def claim():
        with pytest.raises(asyncio.CancelledError):
            await reg.claim_next()

    _run(claim())
    monkeypatch.undo()
    # The claim committed before the cancel: the row is "processing", which
    # is what WorkerPool.recover() exists to requeue.
    assert _run(reg.get(jid)).phase == "processing"


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


# ── Bounded admission (register(max_open=), count_open) ───────────────────
# The same cases as test_jobs_postgres.py's, for the interface parity the
# module promises.
def test_max_open_admits_up_to_the_limit_and_refuses_with_no_row(db_path: str) -> None:
    from common.jobs import QueueFull

    reg = SqliteRegistry(db_path)
    _run(reg.init())
    ids = [_run(reg.register({"i": i}, "pending", max_open=3)) for i in range(3)]
    assert len(set(ids)) == 3
    with pytest.raises(QueueFull) as info:
        _run(reg.register({"i": 3}, "pending", max_open=3))
    assert (info.value.open_jobs, info.value.limit) == (3, 3)
    assert _run(reg.count_by_phase()) == {"pending": 3}


def test_terminal_rows_do_not_count_staging_and_custom_phases_do(db_path: str) -> None:
    from common.jobs import QueueFull

    reg = SqliteRegistry(db_path)
    _run(reg.init())
    for phase in ("completed", "failed", "cancelled"):
        _run(reg.register({"done": phase}, phase))
    assert _run(reg.count_open()) == 0
    staged = _run(reg.register({}, "staging"))
    for phase in ("processing", "capturing"):
        _run(reg.register({"open": phase}, phase))
    assert _run(reg.count_open()) == 3
    with pytest.raises(QueueFull):
        _run(reg.register({}, "staging", max_open=3))
    assert _run(reg.cancel(staged)) == (True, "staging")  # a cancel frees its slot
    _run(reg.register({}, "staging", max_open=3))
    assert _run(reg.count_open()) == 3


def test_open_rows_already_over_the_limit_refuse_until_they_drain(db_path: str) -> None:
    from common.jobs import QueueFull

    reg = SqliteRegistry(db_path)
    _run(reg.init())
    ids = [_run(reg.register({"i": i}, "pending")) for i in range(4)]
    with pytest.raises(QueueFull) as info:
        _run(reg.register({}, "pending", max_open=2))
    assert info.value.open_jobs == 4
    for job_id in ids[:2]:
        _run(reg.set_error(job_id, "boom"))
    with pytest.raises(QueueFull):
        _run(reg.register({}, "pending", max_open=2))
    _run(reg.delete(ids[2]))
    _run(reg.register({}, "pending", max_open=2))
    assert _run(reg.count_open()) == 2


def test_concurrent_registers_on_separate_connections_never_overshoot(db_path: str) -> None:
    """20 submits racing for 5 slots. Each register opens its own aiosqlite
    connection — its own thread — so the count and the insert of one can
    interleave with another's unless BEGIN IMMEDIATE serialises them."""
    from common.jobs import QueueFull

    reg = SqliteRegistry(db_path)
    _run(reg.init())

    async def race():
        return await asyncio.gather(
            *(reg.register({"racer": i}, "staging", max_open=5) for i in range(20)),
            return_exceptions=True,
        )

    outcomes = _run(race())
    admitted = [o for o in outcomes if isinstance(o, str)]
    refused = [o for o in outcomes if isinstance(o, QueueFull)]
    assert len(admitted) == 5 and len(refused) == 15, outcomes
    assert _run(reg.count_by_phase()) == {"staging": 5}


def test_max_open_none_is_unbounded_and_below_one_is_refused(db_path: str) -> None:
    reg = SqliteRegistry(db_path)
    _run(reg.init())
    for i in range(8):
        _run(reg.register({"i": i}, "pending", max_open=None))
    assert _run(reg.count_open()) == 8
    with pytest.raises(ValueError, match="None means no limit"):
        _run(reg.register({}, "pending", max_open=0))
    with pytest.raises(TypeError):
        _run(reg.register({}, "pending", max_open=True))
    assert _run(reg.count_open()) == 8


def test_count_open_counts_every_unfinished_phase(db_path: str) -> None:
    reg = SqliteRegistry(db_path)
    _run(reg.init())
    assert _run(reg.count_open()) == 0
    a = _run(reg.register({}, "staging"))
    b = _run(reg.register({}))
    _run(reg.register({}))
    _run(reg.claim_next())
    assert _run(reg.count_open()) == 3
    _run(reg.set_error(a, "boom"))
    _run(reg.set_phase(b, "completed"))
    assert _run(reg.count_open()) == 1


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
