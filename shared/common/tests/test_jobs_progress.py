"""Tests for common.jobs.progress — the gauge, the helpers, the flusher.

Pure: no database. The store is a list-backed fake that records every flush,
and can be told to fail or to hang, so the "progress never fails a job" and
"a cancel skips the final flush" rules are pinned without Postgres.
"""

from __future__ import annotations

import asyncio
import logging
import threading

import pytest

from common.jobs import progress
from common.jobs.progress import (
    DONE,
    FAILED,
    RUNNING,
    ProgressGauge,
    Stage,
    mark_queued,
    next_attempt,
    queued_snapshot,
)

STAGES = (
    Stage("load", "Loading documents", 10),
    Stage("units", "Evaluating criteria", 85),
    Stage("artifacts", "Writing artifacts", 5),
)


class FakeStore:
    """Records flushes; ``fail`` makes every flush raise, ``hang`` blocks it."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.hang: asyncio.Event | None = None
        self.flushes: list[tuple[str, dict, list[dict]]] = []
        self.queued_ids: list[str] = []
        self.attempt = 3

    async def flush(self, job_id, snapshot, events):
        if self.hang is not None:
            await self.hang.wait()
        if self.fail:
            raise ConnectionError("database is down")
        self.flushes.append((job_id, snapshot, list(events)))

    async def queued(self, job_id, *, stages=()):
        if self.fail:
            raise ConnectionError("database is down")
        self.queued_ids.append(job_id)

    async def next_attempt(self, job_id):
        if self.fail:
            raise ConnectionError("database is down")
        return self.attempt

    def events(self) -> list[dict]:
        return [e for _, _, events in self.flushes for e in events]

    def last(self) -> dict:
        return self.flushes[-1][1]


# ── Band math ─────────────────────────────────────────────────────────────
def test_percent_is_constant_plus_limit_times_index_over_length():
    g = ProgressGauge("j", STAGES)
    g.plan("load", 2)
    g.advance("load")
    assert g.percent == pytest.approx(10 * 1 / 2)
    g.advance("load")
    assert g.percent == pytest.approx(10)
    g.plan("units", 12)
    g.advance("units", 7)
    # constant (the completed load band) + limit * index / length
    assert g.percent == pytest.approx(10 + 85 * 7 / 12)
    snap = g.snapshot()
    assert snap["stage"] == "units" and snap["stage_title"] == "Evaluating criteria"
    assert (snap["index"], snap["length"]) == (7, 12)
    assert snap["percent"] == round(10 + 85 * 7 / 12, 1)


def test_weights_are_normalised_to_100():
    g = ProgressGauge("j", (Stage("a", "A", 1), Stage("b", "B", 3)))
    assert [s["weight"] for s in g.snapshot()["stages"]] == [25.0, 75.0]
    g.plan("b", 1)  # entering b completes a
    assert g.percent == pytest.approx(25)


def test_bad_stage_lists_are_refused():
    with pytest.raises(ValueError):
        ProgressGauge("j", ())
    with pytest.raises(ValueError):
        ProgressGauge("j", (Stage("a", "A", 1), Stage("a", "A again", 1)))
    with pytest.raises(ValueError):
        ProgressGauge("j", (Stage("a", "A", 0),))


def test_percent_never_decreases_and_stays_below_100_while_running():
    g = ProgressGauge("j", STAGES)
    g.plan("units", 4)
    g.advance("units", 4)
    seen = g.percent
    assert seen < 100
    g.plan("units", 40)  # a re-plan that would shrink the fraction
    assert g.percent == seen
    g.plan("artifacts", 1)
    g.advance("artifacts")
    assert g.percent == pytest.approx(99.9)


def test_entering_a_later_stage_completes_the_earlier_ones():
    g = ProgressGauge("j", STAGES)
    g.plan("load", 5)
    g.advance("load")  # 1 of 5
    g.advance("artifacts", 0, label="writing")
    assert g.percent == pytest.approx(95)
    stages = {s["name"]: s for s in g.snapshot()["stages"]}
    assert stages["load"]["index"] == 1  # counters are left as they were
    assert g.snapshot()["stage"] == "artifacts"


def test_an_unknown_stage_warns_once_and_is_ignored(caplog):
    g = ProgressGauge("j", STAGES)
    with caplog.at_level(logging.WARNING, logger="common.jobs.progress"):
        assert g.advance("finalize") is None
        g.plan("finalize", 3)
        g.checkpoint("x", stage="finalize")
    warnings = [r for r in caplog.records if "no stage 'finalize'" in r.getMessage()]
    assert len(warnings) == 1
    assert g.percent == 0 and g.snapshot()["stage"] is None


# ── Units ─────────────────────────────────────────────────────────────────
def test_a_task_credits_its_steps_on_exit():
    g = ProgressGauge("j", STAGES)
    g.plan("units", 3)
    with g.task("units", 2, label="a · item 0"):
        pass
    assert g.snapshot()["index"] == 2
    with g.task("units", 1, label="b · item 0"):
        pass
    assert g.snapshot()["index"] == 3


def test_a_checkpoint_inside_a_task_is_capped_at_steps_minus_one():
    g = ProgressGauge("j", STAGES)
    g.plan("units", 2)
    with g.task("units", 2, label="u"):
        g.checkpoint("scored")
        assert g.snapshot()["index"] == 1
        g.checkpoint("scored again")  # past the cap: a label update only
        assert g.snapshot()["index"] == 1
        assert g.snapshot()["label"] == "scored again"
    assert g.snapshot()["index"] == 2


def test_a_one_step_task_turns_a_checkpoint_into_a_label_update():
    g = ProgressGauge("j", STAGES)
    g.plan("units", 1)
    with g.task("units", 1, label="cv · item 0"):
        g.checkpoint("cv: scored")
        assert g.snapshot()["index"] == 0
    assert g.snapshot()["index"] == 1


def test_a_task_that_raises_or_returns_early_still_counts_in_full():
    g = ProgressGauge("j", STAGES)
    g.plan("units", 4)
    with pytest.raises(RuntimeError, match="boom"):
        with g.task("units", 2):
            raise RuntimeError("boom")
    assert g.snapshot()["index"] == 2

    def early():
        with g.task("units", 2):
            return "skipped"

    assert early() == "skipped"
    assert g.snapshot()["index"] == 4


def test_a_named_stage_inside_a_task_moves_that_stage_not_the_unit():
    g = ProgressGauge("j", STAGES)
    g.plan("units", 2)
    with g.task("units", 2):
        g.checkpoint("writing", stage="artifacts", n=0)
    assert g.snapshot()["stage"] == "artifacts"


def test_concurrent_advances_from_threads_are_all_counted():
    g = ProgressGauge("j", STAGES)
    g.plan("units", 8 * 250)

    def work():
        for _ in range(250):
            with g.task("units", 1):
                pass

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert g.snapshot()["index"] == 2000
    assert g.snapshot()["seq"] == 2000


def test_concurrent_tasks_each_advance_their_own_unit():
    g = ProgressGauge("j", STAGES)

    async def unit(i: int):
        with progress.task("units", 2, label=f"u{i}"):
            await asyncio.sleep(0)
            progress.checkpoint(f"u{i}: scored")
            await asyncio.sleep(0)

    async def main():
        async with g.running():
            progress.plan("units", 20)
            await asyncio.gather(*(unit(i) for i in range(10)))

    asyncio.run(main())
    stages = {s["name"]: s for s in g.snapshot()["stages"]}
    assert stages["units"]["index"] == 20
    assert g.state == DONE and g.percent == 100


def test_to_thread_workers_see_the_current_gauge():
    g = ProgressGauge("j", STAGES)

    def load(name):
        with progress.task("load", label=name):
            pass

    async def main():
        async with g.running():
            progress.plan("load", 3)
            await asyncio.gather(*(asyncio.to_thread(load, f"d{i}") for i in range(3)))
            return g.snapshot()

    snap = asyncio.run(main())
    assert (snap["stage"], snap["index"], snap["length"]) == ("load", 3, 3)
    assert snap["percent"] == 10


# ── Helpers ───────────────────────────────────────────────────────────────
def test_helpers_are_no_ops_without_a_gauge():
    assert progress.current() is None
    progress.plan("units", 3)
    progress.checkpoint("nothing")
    with progress.task("units", 2) as unit:
        assert unit is None


def test_the_log_line(caplog):
    g = ProgressGauge("j", STAGES)
    log = logging.getLogger("test.progress")
    g.plan("units", 12)
    g.advance("units", 6)
    with caplog.at_level(logging.INFO, logger="test.progress"):
        with g.task("units", 2, label="has a roof · item 3", logger=log):
            g.checkpoint("has a roof: scored", logger=log)
    lines = [r.getMessage() for r in caplog.records if r.name == "test.progress"]
    # 10 (the load band, completed by entering units) + 85 × index / 12
    assert lines[0] == "[progress 59%] units 7/12: has a roof: scored"
    assert lines[1] == "[progress 66%] units 8/12: has a roof · item 3"


def test_the_log_line_without_a_planned_length(caplog):
    g = ProgressGauge("j", STAGES)
    with caplog.at_level(logging.INFO, logger="common.jobs.progress"):
        g.checkpoint("writing artifacts", stage="artifacts", n=0)
    assert "[progress 95%] artifacts: writing artifacts" in [r.getMessage() for r in caplog.records]


def test_queued_snapshot_has_the_running_shape():
    g = ProgressGauge("j", STAGES)
    q = queued_snapshot(STAGES)
    assert set(q) == set(g.snapshot())
    assert q["state"] == "queued" and q["percent"] == 0 and q["attempt"] == 0
    assert [s["name"] for s in q["stages"]] == ["load", "units", "artifacts"]


# ── Flushing ──────────────────────────────────────────────────────────────
def test_running_flushes_throttled_and_finishes_done():
    store = FakeStore()
    g = ProgressGauge("j", STAGES, store=store, flush_interval=0.05, attempt=2)

    async def main():
        async with g.running():
            progress.plan("units", 200)
            for _ in range(10):  # ten bursts of twenty advances, a flush interval apart
                for _ in range(20):
                    with progress.task("units"):
                        pass
                await asyncio.sleep(0.06)

    asyncio.run(main())
    # One flush per interval that saw a change (plus the final one) — not
    # one per advance.
    assert 5 <= len(store.flushes) <= 25
    final = store.last()
    assert final["state"] == DONE and final["percent"] == 100 and final["attempt"] == 2
    events = store.events()
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))  # none lost
    assert events[0]["state"] == RUNNING and events[0]["label"] == "started"
    assert events[-1]["state"] == DONE


def test_nothing_is_flushed_while_nothing_changes():
    store = FakeStore()
    g = ProgressGauge("j", STAGES, store=store, flush_interval=0.02)

    async def main():
        async with g.running():
            await asyncio.sleep(0.02 * 3)  # the "started" event: one flush
            n = len(store.flushes)
            await asyncio.sleep(0.02 * 5)
            assert len(store.flushes) == n

    asyncio.run(main())


def test_an_exception_finishes_failed_and_propagates():
    store = FakeStore()
    g = ProgressGauge("j", STAGES, store=store, flush_interval=10)

    async def main():
        async with g.running():
            progress.plan("units", 4)
            progress.checkpoint("one", stage="units")
            raise ValueError("bad page")

    with pytest.raises(ValueError, match="bad page"):
        asyncio.run(main())
    final = store.last()
    assert final["state"] == FAILED
    assert final["label"] == "failed: ValueError: bad page"
    assert final["percent"] == round(10 + 85 / 4, 1)  # left where it stopped


def test_a_failing_store_never_fails_the_job(caplog):
    store = FakeStore(fail=True)
    g = ProgressGauge("j", STAGES, store=store, flush_interval=0.01)

    async def main():
        async with g.running():
            for i in range(5):
                progress.checkpoint(f"step {i}", stage="units")
                await asyncio.sleep(0.02)
            return "result"

    with caplog.at_level(logging.WARNING):
        assert asyncio.run(main()) == "result"
    # One warning for the streak, not one per flush.
    assert sum("could not store progress" in r.getMessage() for r in caplog.records) == 1
    assert g.state == DONE
    # The events stayed buffered for a later flush.
    assert len(g._events) == 7  # started + 5 checkpoints + done


def test_a_store_that_recovers_gets_the_buffered_events():
    store = FakeStore(fail=True)
    g = ProgressGauge("j", STAGES, store=store, flush_interval=0.01)

    async def main():
        async with g.running():
            progress.checkpoint("a", stage="units")
            await asyncio.sleep(0.05)
            store.fail = False
            progress.checkpoint("b", stage="units")
            await asyncio.sleep(0.05)

    asyncio.run(main())
    assert [e["label"] for e in store.events()] == ["started", "a", "b", "done"]


def test_a_cancel_skips_the_final_flush():
    store = FakeStore()
    g = ProgressGauge("j", STAGES, store=store, flush_interval=10)
    started = None

    async def job():
        async with g.running():
            progress.checkpoint("working", stage="units")
            started.set()
            await asyncio.Event().wait()

    async def main():
        nonlocal started
        started = asyncio.Event()
        t = asyncio.create_task(job())
        await started.wait()
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t

    asyncio.run(main())
    assert store.flushes == []  # the interval never elapsed, and no final flush
    assert g.state == RUNNING
    assert progress.current() is None


def test_a_cancel_during_a_hung_flush_puts_the_events_back():
    store = FakeStore()
    g = ProgressGauge("j", STAGES, store=store, flush_interval=0.01)

    async def job():
        async with g.running():
            progress.checkpoint("x", stage="units")
            await asyncio.Event().wait()

    async def main():
        store.hang = asyncio.Event()  # every flush blocks
        t = asyncio.create_task(job())
        await asyncio.sleep(0.05)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t

    asyncio.run(main())
    assert store.flushes == []
    assert [e["label"] for e in g._events] == ["started", "x"]


def test_the_gauge_is_current_only_inside_running():
    g = ProgressGauge("j", STAGES)

    async def main():
        assert progress.current() is None
        async with g.running():
            assert progress.current() is g
        assert progress.current() is None

    asyncio.run(main())


# ── Queue-side helpers ────────────────────────────────────────────────────
def test_mark_queued_and_next_attempt_never_raise():
    good, bad = FakeStore(), FakeStore(fail=True)

    async def main():
        await mark_queued(good, "j1", stages=STAGES)
        await mark_queued(bad, "j2")
        await mark_queued(None, "j3")
        return (
            await next_attempt(good, "j1"),
            await next_attempt(bad, "j2"),
            await next_attempt(None, "j3"),
        )

    assert asyncio.run(main()) == (3, 1, 1)
    assert good.queued_ids == ["j1"]
