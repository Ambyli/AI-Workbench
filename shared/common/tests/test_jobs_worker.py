"""Tests for common.jobs.worker (WorkerPool) against SqliteRegistry."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from common.jobs.model import JobBase
from common.jobs.sqlite import SqliteRegistry
from common.jobs.worker import WorkerPool


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "jobs.db")


async def _wait_terminal(reg, ids, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        snaps = [await reg.get(j) for j in ids]
        if all(s.phase in ("completed", "failed") for s in snaps):
            return snaps
        await asyncio.sleep(0.02)
    raise AssertionError("jobs did not finish: " + str([s.phase for s in snaps]))


def test_pool_caps_concurrency_and_completes_all(db_path: str) -> None:
    async def main():
        reg = SqliteRegistry(db_path)
        await reg.init()
        state = {"in": 0, "peak": 0}

        async def handler(job: JobBase) -> dict:
            state["in"] += 1
            state["peak"] = max(state["peak"], state["in"])
            await asyncio.sleep(0.15)
            state["in"] -= 1
            return {"rid": job.metadata["request_id"]}

        finished = []
        pool = WorkerPool(reg, handler, concurrency=3, poll_interval=0.1,
                          on_finish=lambda j, ph, el, err: finished.append((j.job_id, ph, err)))
        pool.start()
        ids = [await reg.register({"request_id": str(i)}) for i in range(9)]
        pool.notify()
        snaps = await _wait_terminal(reg, ids)
        await pool.stop()

        assert all(s.phase == "completed" for s in snaps)
        assert {s.result["rid"] for s in snaps} == {str(i) for i in range(9)}
        assert state["peak"] == 3
        assert pool.in_flight == 0
        assert len(finished) == 9 and all(ph == "completed" and err is None for _, ph, err in finished)
        assert not pool.running

    asyncio.run(main())


def test_handler_exception_marks_failed_and_pool_survives(db_path: str) -> None:
    async def main():
        reg = SqliteRegistry(db_path)
        await reg.init()

        async def handler(job: JobBase) -> dict:
            if job.metadata["bad"]:
                raise ValueError("boom")
            return {"ok": True}

        pool = WorkerPool(reg, handler, concurrency=1, poll_interval=0.05)
        pool.start()
        bad = await reg.register({"bad": True})
        good = await reg.register({"bad": False})
        pool.notify()
        await _wait_terminal(reg, [bad, good])
        await pool.stop()

        b, g = await reg.get(bad), await reg.get(good)
        assert b.phase == "failed" and b.error == "boom"
        assert g.phase == "completed" and g.result == {"ok": True}

    asyncio.run(main())


def test_recover_requeues_processing_and_extra_phases(db_path: str) -> None:
    async def main():
        reg = SqliteRegistry(db_path)
        await reg.init()
        stuck = await reg.register({}, initial_phase="processing")
        staged = await reg.register({}, initial_phase="staging")
        done = await reg.register({})
        await reg.set_result(done, {})

        async def handler(job):
            return {"redone": True}

        pool = WorkerPool(reg, handler, concurrency=2, poll_interval=0.05)
        moved = await pool.recover(phases=["staging"])
        assert moved == 2
        assert (await reg.get(stuck)).phase == "pending"
        assert (await reg.get(staged)).phase == "pending"

        pool.start()
        await _wait_terminal(reg, [stuck, staged])
        await pool.stop()
        assert (await reg.get(stuck)).result == {"redone": True}
        assert (await reg.get(done)).phase == "completed"

    asyncio.run(main())


def test_poll_picks_up_jobs_without_notify(db_path: str) -> None:
    """Rows written by another process (no notify) are found by the poll."""
    async def main():
        reg = SqliteRegistry(db_path)
        await reg.init()

        async def handler(job):
            return {"seen": True}

        pool = WorkerPool(reg, handler, concurrency=1, poll_interval=0.05)
        pool.start()
        await asyncio.sleep(0.02)  # worker is now idle, waiting on the event
        jid = await reg.register({})
        await _wait_terminal(reg, [jid], timeout=2.0)
        await pool.stop()
        assert (await reg.get(jid)).phase == "completed"

    asyncio.run(main())


def test_stop_leaves_inflight_job_in_processing_for_recovery(db_path: str) -> None:
    async def main():
        reg = SqliteRegistry(db_path)
        await reg.init()
        started = asyncio.Event()

        async def handler(job):
            started.set()
            await asyncio.sleep(10)
            return {}

        pool = WorkerPool(reg, handler, concurrency=1, poll_interval=0.05)
        pool.start()
        jid = await reg.register({})
        pool.notify()
        await asyncio.wait_for(started.wait(), 2.0)
        await pool.stop()
        assert (await reg.get(jid)).phase == "processing"
        assert await pool.recover() == 1
        assert (await reg.get(jid)).phase == "pending"

    asyncio.run(main())
