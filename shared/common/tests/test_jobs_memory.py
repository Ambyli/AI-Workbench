"""Tests for common.jobs.memory (InMemoryRegistry)."""

from __future__ import annotations

import threading
import time

from pydantic import BaseModel

from common.jobs import InMemoryRegistry


class _Meta(BaseModel):
    tag: str


def test_context_manager_registers_and_unregisters():
    reg = InMemoryRegistry()
    seen_id: str | None = None
    with reg.job(_Meta(tag="hello"), initial_phase="running") as job:
        seen_id = job.job_id
        snap = reg.get(job.job_id)
        assert snap is not None
        assert snap.phase == "running"
        assert snap.metadata == {"tag": "hello"}
    # After context exit — job is gone.
    assert seen_id is not None
    assert reg.get(seen_id) is None


def test_phase_transitions_visible_via_get():
    reg = InMemoryRegistry()
    with reg.job(_Meta(tag="t"), initial_phase="a") as job:
        assert reg.get(job.job_id).phase == "a"
        job.set_phase("b")
        assert reg.get(job.job_id).phase == "b"
        job.set_phase("c")
        assert reg.get(job.job_id).phase == "c"


def test_list_all_includes_active_and_reports_capacity():
    reg = InMemoryRegistry(max_concurrent=4)
    with reg.job(_Meta(tag="one"), initial_phase="running"):
        with reg.job(_Meta(tag="two"), initial_phase="running"):
            snap = reg.list_all()
            assert snap.active_count == 2
            assert snap.max_concurrent == 4
            assert snap.available == 2
            tags = sorted(j.metadata["tag"] for j in snap.jobs)
            assert tags == ["one", "two"]
    # Both cleared.
    assert reg.list_all().active_count == 0


def test_cancel_sets_event_and_wait_or_cancel_wakes():
    reg = InMemoryRegistry()
    observed: dict[str, bool | float] = {}

    def worker() -> None:
        with reg.job(_Meta(tag="x"), initial_phase="running") as job:
            observed["job_id"] = job.job_id
            start = time.monotonic()
            observed["cancelled"] = job.wait_or_cancel(timeout=5.0)
            observed["elapsed"] = time.monotonic() - start

    t = threading.Thread(target=worker)
    t.start()
    # Give the worker a moment to register + start waiting.
    time.sleep(0.05)
    ok, was_phase = reg.cancel(observed["job_id"])
    assert ok is True
    assert was_phase == "running"
    t.join(timeout=5.0)
    assert observed["cancelled"] is True
    # Sanity — cancel woke it well before the 5s timeout.
    assert observed["elapsed"] < 1.0


def test_cancel_missing_returns_not_found():
    reg = InMemoryRegistry()
    ok, reason = reg.cancel("does-not-exist")
    assert ok is False
    assert reason == "not_found"


def test_cancel_already_cleaning_up_returns_conflict():
    reg = InMemoryRegistry()
    with reg.job(_Meta(tag="x"), initial_phase="running") as job:
        job.set_phase("cleaning_up")
        ok, reason = reg.cancel(job.job_id)
        assert ok is False
        assert reason == "already_cleaning_up"


def test_uuids_are_unique_across_many_concurrent_jobs():
    reg = InMemoryRegistry()
    ids: list[str] = []
    ids_lock = threading.Lock()

    def worker() -> None:
        with reg.job(_Meta(tag="x"), initial_phase="running") as job:
            with ids_lock:
                ids.append(job.job_id)
            # Hold briefly to overlap with other threads.
            time.sleep(0.05)

    threads = [threading.Thread(target=worker) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(ids) == 50
    assert len(set(ids)) == 50


def test_update_metadata_patches_in_place():
    reg = InMemoryRegistry()
    with reg.job(_Meta(tag="orig"), initial_phase="running") as job:
        job.update_metadata(temp_dir="/tmp/xyz")
        snap = reg.get(job.job_id)
        assert snap.metadata == {"tag": "orig", "temp_dir": "/tmp/xyz"}


def test_dict_metadata_also_accepted():
    reg = InMemoryRegistry()
    with reg.job({"a": 1, "b": "two"}, initial_phase="running") as job:
        snap = reg.get(job.job_id)
        assert snap.metadata == {"a": 1, "b": "two"}
