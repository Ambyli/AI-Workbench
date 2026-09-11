"""Tests for common.jobs.payloads (FilePayloadStore)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from common.jobs.payloads import FilePayloadStore
from common.jobs.sqlite import SqliteRegistry


def _run(coro):
    return asyncio.run(coro)


def test_write_read_delete_roundtrip(tmp_path: Path) -> None:
    store = FilePayloadStore(tmp_path / "payloads")
    assert _run(store.read("abc")) is None
    _run(store.write("abc", {"x": 1, "nested": [1, 2]}))
    assert _run(store.read("abc")) == {"x": 1, "nested": [1, 2]}
    assert store.ids() == ["abc"]
    assert not list((tmp_path / "payloads").glob("*.tmp"))  # atomic write left no temp
    assert _run(store.delete("abc")) is True
    assert _run(store.delete("abc")) is False
    assert _run(store.read("abc")) is None


def test_rejects_unsafe_ids(tmp_path: Path) -> None:
    store = FilePayloadStore(tmp_path)
    for bad in ("", "..", "a/b", "a\\b"):
        with pytest.raises(ValueError):
            store.path(bad)


def test_sweep_removes_orphans_and_terminal_keeps_live(tmp_path: Path) -> None:
    async def main():
        reg = SqliteRegistry(str(tmp_path / "jobs.db"))
        await reg.init()
        store = FilePayloadStore(tmp_path / "payloads")

        pending = await reg.register({})
        processing = await reg.register({}, initial_phase="processing")
        done = await reg.register({})
        await reg.set_result(done, {})
        for jid in (pending, processing, done, "ghost"):
            await store.write(jid, {"j": jid})
        (tmp_path / "payloads" / "half.json.tmp").write_text("{")

        removed = await store.sweep(reg)
        assert removed == 2  # done + ghost
        assert store.ids() == sorted([pending, processing])
        assert not list((tmp_path / "payloads").glob("*.tmp"))

    _run(main())


def test_sweep_on_missing_root_is_noop(tmp_path: Path) -> None:
    async def main():
        reg = SqliteRegistry(str(tmp_path / "jobs.db"))
        await reg.init()
        store = FilePayloadStore(tmp_path / "does-not-exist")
        assert await store.sweep(reg) == 0
        assert store.ids() == []

    _run(main())
