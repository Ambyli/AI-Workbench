"""Tests for common.jobs.worker (WorkerPool) against SqliteRegistry.

The plain pool first (what every consumer had before cancel existed), then
the cancel-aware pool (``cancel_check_interval``): a cancel or a delete
mid-handler stops the handler and frees the worker, a result that lands
after a cancel is never stored, a shutdown still leaves the row for
``recover()``, a handler that cancels itself fails its job without killing
the worker, and a cancel check that cannot read the row never cancels.
"""

from __future__ import annotations

import asyncio
import sys
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


class _CancelSwallowingRegistry:
    """A claim_next whose cleanup raises over a cancel — what SqliteRegistry
    did when its ROLLBACK failed after the COMMIT had landed. Only ONE cancel
    is swallowed (it was a race, not a habit), so on a regression the test's
    own timeout can still unwind the pool and the test fails instead of
    hanging the suite."""

    def __init__(self) -> None:
        self.claims = 0
        self.swallow = 1

    async def claim_next(self, from_phase="pending", to_phase="processing"):
        self.claims += 1
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            if self.swallow:
                self.swallow -= 1
                raise RuntimeError("cleanup failed over the cancel")
            raise
        return None

    async def reset_phase(self, from_phase, to_phase):
        return 0

    async def set_result(self, job_id, result, phase="completed"):
        pass

    async def set_error(self, job_id, error, phase="failed"):
        pass


def test_stop_returns_when_the_registry_swallows_the_cancel() -> None:
    """stop() must unwind a worker even if claim_next turned the cancel into
    an ordinary error; retrying would leave a half-cancelled task looping."""
    async def main():
        reg = _CancelSwallowingRegistry()

        async def handler(job):
            return {}

        pool = WorkerPool(reg, handler, concurrency=2, poll_interval=0.01)
        pool.start()
        await asyncio.sleep(0.05)  # both workers are inside claim_next
        await asyncio.wait_for(pool.stop(), timeout=2.0)
        assert not pool.running
        assert reg.swallow == 0  # the race really happened
        assert reg.claims == 2  # stopped, not backed off and retried

    asyncio.run(main())


class _RecordingRegistry(SqliteRegistry):
    """Records what the POOL asks of the registry — a call from a task that is
    not a worker (the test's own polling) is not the pool's."""

    def __init__(self, db_path: str) -> None:
        super().__init__(db_path)
        self.calls: list = []

    def _from_pool(self) -> bool:
        task = asyncio.current_task()
        return task is not None and task.get_name().startswith("worker-")

    async def get(self, job_id):
        if self._from_pool():
            self.calls.append("get")
        return await super().get(job_id)

    async def set_result(self, job_id, result, phase="completed", **kw):
        self.calls.append(("set_result", kw))
        return await super().set_result(job_id, result, phase, **kw)

    async def set_error(self, job_id, error, phase="failed", **kw):
        self.calls.append(("set_error", kw))
        return await super().set_error(job_id, error, phase, **kw)


def test_the_plain_pool_never_reads_the_row_and_writes_unguarded(db_path: str) -> None:
    """Without cancel_check_interval the pool is what it always was: the
    handler runs inside the worker task, nothing re-reads the row, and the
    terminal write carries no guard."""
    async def main():
        reg = _RecordingRegistry(db_path)
        await reg.init()
        ran_in: list[str] = []

        async def handler(job):
            ran_in.append(asyncio.current_task().get_name())
            return {"ok": True}

        pool = WorkerPool(reg, handler, concurrency=1, poll_interval=0.05)
        pool.start()
        jid = await reg.register({})
        pool.notify()
        await _wait_terminal(reg, [jid])
        await pool.stop()
        assert ran_in == ["worker-0"]
        assert reg.calls == [("set_result", {})]

    asyncio.run(main())


# ── Cancel-aware mode (cancel_check_interval) ─────────────────────────────
# Short enough that a cancel is seen within a few ticks, long enough to be a
# real interval on Windows' ~15 ms timer.
CHECK_S = 0.02


async def _wait_until(condition, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out waiting for the pool")
        await asyncio.sleep(0.01)


def _cancelling() -> int:
    """The current task's pending-cancel count (3.11+; 0 before)."""
    cancelling = getattr(asyncio.current_task(), "cancelling", None)
    return cancelling() if cancelling else 0


class _Hanging:
    """A handler: a job with ``metadata.hang`` waits until it is cancelled,
    any other job returns at once. Records which runs saw a cancel and which
    had unwound — so a hook can check it ran AFTER the handler ended."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.saw_cancel: list[str] = []
        self.unwound: list[str] = []
        self.ran_in: list[str] = []

    async def __call__(self, job: JobBase) -> dict:
        self.ran_in.append(asyncio.current_task().get_name())
        if not job.metadata.get("hang"):
            return {"ok": True}
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.saw_cancel.append(job.job_id)
            raise
        finally:
            self.unwound.append(job.job_id)
        return {}


def test_a_cancel_mid_handler_stops_it_and_frees_the_worker(db_path: str) -> None:
    async def main():
        reg = _RecordingRegistry(db_path)
        await reg.init()
        handler = _Hanging()
        finished: list = []
        cleanups: list = []

        async def on_cancel(job, gone):
            # After the handler has unwound, in a task nobody cancelled.
            cleanups.append((job.job_id, gone, job.job_id in handler.unwound, _cancelling()))

        pool = WorkerPool(
            reg, handler, concurrency=1, poll_interval=0.05,
            cancel_check_interval=CHECK_S, on_cancel=on_cancel,
            on_finish=lambda j, ph, el, err: finished.append((j.job_id, ph, err)),
        )
        pool.start()
        hung = await reg.register({"hang": True})
        pool.notify()
        await asyncio.wait_for(handler.started.wait(), 2.0)
        assert pool.in_flight == 1
        assert await reg.cancel(hung) == (True, "processing")
        # One worker: the next job runs only if the cancel freed it.
        nxt = await reg.register({})
        pool.notify()
        await _wait_terminal(reg, [nxt])
        await pool.stop()

        row = await reg.get(hung)
        assert (row.phase, row.result, row.error) == ("cancelled", None, None)
        assert handler.saw_cancel == [hung]
        assert handler.ran_in[0] == f"worker-0-{hung}"  # a child task, not the worker
        assert finished == [(hung, "cancelled", None), (nxt, "completed", None)]
        assert cleanups == [(hung, False, True, 0)]
        assert (await reg.get(nxt)).result == {"ok": True}
        assert pool.in_flight == 0
        # The cancelled job was never written; the next one's write was guarded.
        writes = [c for c in reg.calls if c != "get"]
        assert writes == [("set_result", {"from_phases": ("processing",)})]
        assert "get" in reg.calls

    asyncio.run(main())


def test_a_row_deleted_mid_handler_stops_it_as_gone(db_path: str) -> None:
    async def main():
        reg = SqliteRegistry(db_path)
        await reg.init()
        handler = _Hanging()
        finished: list = []
        cleanups: list = []
        pool = WorkerPool(
            reg, handler, concurrency=1, poll_interval=0.05,
            cancel_check_interval=CHECK_S,
            on_cancel=lambda job, gone: cleanups.append((job.job_id, gone)),  # a sync hook
            on_finish=lambda j, ph, el, err: finished.append((j.job_id, ph, err)),
        )
        pool.start()
        hung = await reg.register({"hang": True})
        pool.notify()
        await asyncio.wait_for(handler.started.wait(), 2.0)
        assert await reg.delete(hung)
        await _wait_until(lambda: cleanups)
        await pool.stop()
        assert cleanups == [(hung, True)]
        assert finished == [(hung, "cancelled", None)]
        assert handler.saw_cancel == [hung]
        assert await reg.get(hung) is None

    asyncio.run(main())


def test_a_result_that_lands_after_the_cancel_is_never_stored(db_path: str) -> None:
    """The cancel arrives while the handler is finishing — after the last
    check, before the write. (The interval is long, so no check ever runs:
    the guarded write alone must refuse it.) A result, an error and a row
    deleted under the handler all end as a cancel."""
    async def main():
        reg = SqliteRegistry(db_path)
        await reg.init()

        async def handler(job):
            if job.metadata.get("delete"):
                await reg.delete(job.job_id)
            else:
                await reg.cancel(job.job_id)
            if job.metadata.get("fail"):
                raise RuntimeError("too late to matter")
            return {"late": True}

        finished: list = []
        cleanups: list = []
        pool = WorkerPool(
            reg, handler, concurrency=1, poll_interval=0.05, cancel_check_interval=30.0,
            on_cancel=lambda job, gone: cleanups.append((job.job_id, gone)),
            on_finish=lambda j, ph, el, err: finished.append((j.job_id, ph, err)),
        )
        pool.start()
        ok = await reg.register({})
        bad = await reg.register({"fail": True})
        gone = await reg.register({"delete": True})
        pool.notify()
        await _wait_until(lambda: len(cleanups) == 3)
        await pool.stop()
        for jid in (ok, bad):
            row = await reg.get(jid)
            assert (row.phase, row.result, row.error) == ("cancelled", None, None)
        assert await reg.get(gone) is None
        assert finished == [(ok, "cancelled", None), (bad, "cancelled", None),
                            (gone, "cancelled", None)]
        assert cleanups == [(ok, False), (bad, False), (gone, True)]

    asyncio.run(main())


def test_shutdown_in_cancel_aware_mode_leaves_the_row_for_recover(db_path: str) -> None:
    """stop() is not a cancel: the handler is taken down, the row stays in
    processing for recover(), and neither hook runs."""
    async def main():
        reg = SqliteRegistry(db_path)
        await reg.init()
        handler = _Hanging()
        hooks: list = []
        pool = WorkerPool(
            reg, handler, concurrency=1, poll_interval=0.05,
            cancel_check_interval=CHECK_S,
            on_cancel=lambda job, gone: hooks.append("on_cancel"),
            on_finish=lambda *args: hooks.append("on_finish"),
        )
        pool.start()
        jid = await reg.register({"hang": True})
        pool.notify()
        await asyncio.wait_for(handler.started.wait(), 2.0)
        await asyncio.wait_for(pool.stop(), timeout=5.0)
        assert not pool.running and pool.in_flight == 0
        assert (await reg.get(jid)).phase == "processing"
        assert handler.saw_cancel == [jid] and handler.unwound == [jid]
        assert hooks == []
        assert await pool.recover() == 1

    asyncio.run(main())


def test_a_handler_that_cancels_itself_fails_and_the_worker_survives(db_path: str) -> None:
    """A CancelledError the pool did not ask for is the handler's failure —
    in the plain pool it would have killed the worker."""
    async def main():
        reg = SqliteRegistry(db_path)
        await reg.init()

        async def handler(job):
            if job.metadata.get("self_cancel"):
                raise asyncio.CancelledError()
            return {"ok": True}

        pool = WorkerPool(reg, handler, concurrency=1, poll_interval=0.05,
                          cancel_check_interval=CHECK_S)
        pool.start()
        bad = await reg.register({"self_cancel": True})
        good = await reg.register({})
        pool.notify()
        await _wait_terminal(reg, [bad, good])
        await pool.stop()
        b, g = await reg.get(bad), await reg.get(good)
        assert (b.phase, b.error) == ("failed", "handler was cancelled")
        assert g.phase == "completed" and g.result == {"ok": True}

    asyncio.run(main())


def test_a_handler_that_raises_while_stopped_is_still_a_cancel(db_path: str) -> None:
    """Cleanup that raises over the cancel changes nothing: the row says
    cancelled, the hook runs, the worker carries on."""
    async def main():
        reg = SqliteRegistry(db_path)
        await reg.init()
        started = asyncio.Event()

        async def handler(job):
            if not job.metadata.get("hang"):
                return {"ok": True}
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise RuntimeError("cleanup failed over the cancel")
            return {}

        cleanups: list = []
        pool = WorkerPool(reg, handler, concurrency=1, poll_interval=0.05,
                          cancel_check_interval=CHECK_S,
                          on_cancel=lambda job, gone: cleanups.append(job.job_id))
        pool.start()
        hung = await reg.register({"hang": True})
        pool.notify()
        await asyncio.wait_for(started.wait(), 2.0)
        await reg.cancel(hung)
        nxt = await reg.register({})
        pool.notify()
        await _wait_terminal(reg, [nxt])
        await pool.stop()
        assert cleanups == [hung]
        assert (await reg.get(hung)).phase == "cancelled"

    asyncio.run(main())


def test_an_on_cancel_that_raises_is_swallowed(db_path: str) -> None:
    async def main():
        reg = SqliteRegistry(db_path)
        await reg.init()
        handler = _Hanging()

        async def on_cancel(job, gone):
            raise RuntimeError("the payload directory is on fire")

        pool = WorkerPool(reg, handler, concurrency=1, poll_interval=0.05,
                          cancel_check_interval=CHECK_S, on_cancel=on_cancel)
        pool.start()
        hung = await reg.register({"hang": True})
        pool.notify()
        await asyncio.wait_for(handler.started.wait(), 2.0)
        await reg.cancel(hung)
        nxt = await reg.register({})
        pool.notify()
        await _wait_terminal(reg, [nxt])
        assert pool.running
        await pool.stop()

    asyncio.run(main())


class _FlakyReads(SqliteRegistry):
    """``get`` fails while ``failing`` — counting each failure."""

    def __init__(self, db_path: str) -> None:
        super().__init__(db_path)
        self.failing = True
        self.failures = 0

    async def get(self, job_id):
        if self.failing:
            self.failures += 1
            raise RuntimeError("database hiccup")
        return await super().get(job_id)


def test_a_cancel_check_that_cannot_read_never_cancels(db_path: str) -> None:
    async def main():
        reg = _FlakyReads(db_path)
        await reg.init()

        async def handler(job):
            while reg.failures < 3:  # three checks failed while it ran
                await asyncio.sleep(0.01)
            reg.failing = False
            return {"ok": True}

        pool = WorkerPool(reg, handler, concurrency=1, poll_interval=0.05,
                          cancel_check_interval=CHECK_S)
        pool.start()
        jid = await reg.register({})
        pool.notify()
        await _wait_until(lambda: not reg.failing)
        await _wait_terminal(reg, [jid])
        await pool.stop()
        assert (await reg.get(jid)).result == {"ok": True}

    asyncio.run(main())


@pytest.mark.parametrize("interval", [0, -1.0, float("nan"), float("inf")])
def test_a_cancel_check_interval_that_could_never_tick_is_refused(interval) -> None:
    async def handler(job):
        return {}

    with pytest.raises(ValueError, match="cancel_check_interval"):
        WorkerPool(_CancelSwallowingRegistry(), handler, cancel_check_interval=interval)


@pytest.mark.skipif(sys.version_info < (3, 11), reason="asyncio.timeout is 3.11+")
def test_the_cancel_cleanup_runs_where_asyncio_timeout_still_works(db_path: str) -> None:
    """The reason on_cancel runs in the worker: on 3.11 (the classifier
    image's Python) an ``asyncio.timeout`` that expires inside a task that
    was cancelled once reports a CancelledError, not a TimeoutError — the
    task still counts its cancel. In the hook, a task nobody cancelled, it
    is a TimeoutError on every version."""
    async def main():
        reg = SqliteRegistry(db_path)
        await reg.init()
        handler = _Hanging()
        seen: list = []

        async def on_cancel(job, gone):
            try:
                async with asyncio.timeout(0.01):
                    await asyncio.sleep(1)
            except TimeoutError:
                seen.append("timeout")

        pool = WorkerPool(reg, handler, concurrency=1, poll_interval=0.05,
                          cancel_check_interval=CHECK_S, on_cancel=on_cancel)
        pool.start()
        hung = await reg.register({"hang": True})
        pool.notify()
        await asyncio.wait_for(handler.started.wait(), 2.0)
        await reg.cancel(hung)
        await _wait_until(lambda: seen)
        await pool.stop()
        assert seen == ["timeout"]

    asyncio.run(main())
