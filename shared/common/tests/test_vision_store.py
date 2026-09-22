"""Tests for common.vision.store.ArtifactStore.

The store is where a bug is expensive: it serves files by a name that arrives
in a URL, it deletes directories, and it prunes job rows. So the tests lean on
the refusal paths (unsafe names, unsafe ids) as hard as on the happy ones, and
the sweep tests use a real SqliteRegistry rather than a stand-in.
"""

from __future__ import annotations

import asyncio
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import BaseModel

from common.jobs.sqlite import SqliteRegistry
from common.vision import ArtifactStore, build_manifest, content_type_for


class _Meta(BaseModel):
    type: str = "assess"


def _store(tmp_path: Path, **kwargs) -> ArtifactStore:
    return ArtifactStore(tmp_path / "artifacts", **kwargs)


# ── write / list / open ────────────────────────────────────────────────────
def test_write_returns_a_manifest_entry_and_creates_the_directory(tmp_path):
    store = _store(tmp_path)
    entry = store.write("job1", "p0.svg", "<svg/>")
    assert entry == {"name": "p0.svg", "bytes": 6, "content_type": "image/svg+xml"}
    assert store.exists("job1")
    assert store.open("job1", "p0.svg") == b"<svg/>"


def test_list_is_sorted_and_ignores_temp_files(tmp_path):
    store = _store(tmp_path)
    store.write("job1", "p1.svg", "b")
    store.write("job1", "p0.svg", "a")
    (store.dir_for("job1") / "leftover.tmp").write_bytes(b"x")
    assert [e["name"] for e in store.list("job1")] == ["p0.svg", "p1.svg"]


def test_open_missing_file_or_job_returns_none(tmp_path):
    store = _store(tmp_path)
    assert store.open("nope", "p0.svg") is None
    store.write("job1", "p0.svg", "a")
    assert store.open("job1", "p9.svg") is None


def test_unsafe_names_and_ids_are_refused(tmp_path):
    store = _store(tmp_path)
    for bad in ("../secret", "a/b", "a\\b", ".hidden", ""):
        with pytest.raises(ValueError):
            store.path_of("job1", bad)
    for bad_id in ("../..", "a/b", ""):
        with pytest.raises(ValueError):
            store.dir_for(bad_id)
    # The read helpers swallow it rather than 500-ing an endpoint.
    assert store.open("job1", "../secret") is None
    assert store.exists("../..") is False


def test_write_json_round_trips(tmp_path):
    store = _store(tmp_path)
    store.write_json("job1", "regions.json", {"criteria": {"has faces": {"regions": []}}})
    assert store.read_json("job1", "regions.json")["criteria"]["has faces"] == {"regions": []}


def test_content_type_map_covers_the_layer_formats():
    assert content_type_for("regions.json") == "application/json"
    assert content_type_for("p0.svg") == "image/svg+xml"
    assert content_type_for("p0.layer.png") == "image/png"
    assert content_type_for("p0.preview.jpg") == "image/jpeg"
    assert content_type_for("mystery.bin") == "application/octet-stream"


# ── manifest ───────────────────────────────────────────────────────────────
def test_manifest_refreshes_files_and_total_bytes_from_disk(tmp_path):
    store = _store(tmp_path)
    store.write_json(
        "job1", "manifest.json",
        build_manifest("job1", files=[], page_geometry=[], ttl_hours=24),
    )
    store.write("job1", "p0.svg", "<svg/>")
    manifest = store.manifest("job1")
    assert [f["name"] for f in manifest["files"]] == ["manifest.json", "p0.svg"]
    assert manifest["total_bytes"] == store.total_bytes("job1")
    assert manifest["expires_at"] > manifest["created_at"]


def test_manifest_is_none_when_the_job_has_no_directory(tmp_path):
    assert _store(tmp_path).manifest("job1") is None


# ── byte cap ───────────────────────────────────────────────────────────────
def test_cap_drops_pngs_first_then_previews_and_never_svg_or_json(tmp_path):
    store = _store(tmp_path, max_bytes=1000)
    store.write("job1", "regions.json", b"j" * 300)
    store.write("job1", "p0.svg", b"s" * 300)
    store.write("job1", "p0.preview.jpg", b"p" * 400)
    store.write("job1", "p0.layer.png", b"n" * 800)

    dropped = store.enforce_cap("job1")
    assert dropped == ["p0.layer.png"]
    names = {e["name"] for e in store.list("job1")}
    assert names == {"regions.json", "p0.svg", "p0.preview.jpg"}
    assert store.total_bytes("job1") <= 1000


def test_cap_falls_through_to_previews_when_dropping_pngs_is_not_enough(tmp_path):
    store = _store(tmp_path, max_bytes=700)
    store.write("job1", "regions.json", b"j" * 300)
    store.write("job1", "p0.svg", b"s" * 300)
    store.write("job1", "p0.preview.jpg", b"p" * 400)
    store.write("job1", "p0.base.jpg", b"b" * 400)
    store.write("job1", "p0.layer.png", b"n" * 800)

    dropped = store.enforce_cap("job1")
    assert "p0.layer.png" in dropped
    remaining = {e["name"] for e in store.list("job1")}
    assert {"regions.json", "p0.svg"} <= remaining
    assert store.total_bytes("job1") <= 700


def test_cap_is_a_no_op_under_the_limit_or_when_disabled(tmp_path):
    store = _store(tmp_path, max_bytes=0)
    store.write("job1", "p0.layer.png", b"n" * 5000)
    assert store.enforce_cap("job1") == []
    assert store.enforce_cap("job1", max_bytes=99999) == []


# ── delete / zip / stats ───────────────────────────────────────────────────
def test_delete_removes_the_directory_once(tmp_path):
    store = _store(tmp_path)
    store.write("job1", "p0.svg", "a")
    assert store.delete("job1") is True
    assert store.exists("job1") is False
    assert store.delete("job1") is False


def test_delete_file_removes_one_entry(tmp_path):
    store = _store(tmp_path)
    store.write("job1", "p0.svg", "a")
    assert store.delete_file("job1", "p0.svg") is True
    assert store.delete_file("job1", "p0.svg") is False


def test_zip_contains_every_file_under_the_job_id(tmp_path):
    store = _store(tmp_path)
    store.write("job1", "regions.json", '{"a": 1}')
    store.write("job1", "p0.svg", "<svg/>")

    blob = b"".join(store.zip_chunks("job1"))
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        assert sorted(archive.namelist()) == ["job1/p0.svg", "job1/regions.json"]
        assert json.loads(archive.read("job1/regions.json")) == {"a": 1}


def test_zip_of_an_empty_job_is_still_a_valid_archive(tmp_path):
    store = _store(tmp_path)
    blob = b"".join(store.zip_chunks("job1"))
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        assert archive.namelist() == []


def test_stats_counts_directories_and_bytes(tmp_path):
    store = _store(tmp_path)
    store.write("job1", "p0.svg", b"a" * 10)
    store.write("job2", "p0.svg", b"a" * 20)
    assert store.stats() == {"dirs": 2, "bytes": 30}


# ── sweep ──────────────────────────────────────────────────────────────────
def _registry(tmp_path: Path) -> SqliteRegistry:
    registry = SqliteRegistry(str(tmp_path / "jobs.db"))
    asyncio.run(registry.init())
    return registry


def _age_job(registry: SqliteRegistry, job_id: str, hours: float) -> None:
    """Backdate a row's created_at so the TTL can be exercised without sleeping."""
    import sqlite3

    stamp = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with sqlite3.connect(registry.db_path) as db:
        db.execute("UPDATE jobs SET created_at = ? WHERE id = ?", (stamp, job_id))
        db.commit()


def test_sweep_removes_a_directory_whose_job_is_gone(tmp_path):
    store = _store(tmp_path)
    registry = _registry(tmp_path)
    store.write("orphan", "p0.svg", "a")

    out = asyncio.run(store.sweep(registry, ttl_hours=24))
    assert out["dirs_removed"] == 1
    assert store.exists("orphan") is False


def test_sweep_prunes_an_aged_job_and_its_directory(tmp_path):
    store = _store(tmp_path)
    registry = _registry(tmp_path)
    job_id = asyncio.run(registry.register(_Meta(), "pending"))
    asyncio.run(registry.set_result(job_id, {"ok": True}))
    store.write(job_id, "p0.svg", "a")
    _age_job(registry, job_id, hours=48)

    out = asyncio.run(store.sweep(registry, ttl_hours=24))
    assert out["dirs_removed"] == 1
    assert out["jobs_removed"] == 1
    assert store.exists(job_id) is False
    assert asyncio.run(registry.get(job_id)) is None


def test_sweep_keeps_a_fresh_job(tmp_path):
    store = _store(tmp_path)
    registry = _registry(tmp_path)
    job_id = asyncio.run(registry.register(_Meta(), "pending"))
    asyncio.run(registry.set_result(job_id, {"ok": True}))
    store.write(job_id, "p0.svg", "a")

    out = asyncio.run(store.sweep(registry, ttl_hours=24))
    # `scanned` counts EXPIRED candidates pass 2 considered, not rows read —
    # the bounded expired_job_ids query never returns a fresh row at all.
    assert out == {"dirs_removed": 0, "jobs_removed": 0, "scanned": 0}
    assert store.exists(job_id) is True


def test_sweep_never_prunes_a_job_that_is_still_running(tmp_path):
    store = _store(tmp_path)
    registry = _registry(tmp_path)
    job_id = asyncio.run(registry.register(_Meta(), "processing"))
    store.write(job_id, "p0.svg", "a")
    _age_job(registry, job_id, hours=999)

    out = asyncio.run(store.sweep(registry, ttl_hours=1))
    assert out["jobs_removed"] == 0
    assert store.exists(job_id) is True


def test_sweep_prunes_an_aged_row_that_never_had_artifacts(tmp_path):
    store = _store(tmp_path)
    registry = _registry(tmp_path)
    job_id = asyncio.run(registry.register(_Meta(), "pending"))
    asyncio.run(registry.set_error(job_id, "boom"))
    _age_job(registry, job_id, hours=48)

    out = asyncio.run(store.sweep(registry, ttl_hours=24))
    assert out["jobs_removed"] == 1
    assert asyncio.run(registry.get(job_id)) is None


def test_sweep_can_leave_job_rows_alone(tmp_path):
    store = _store(tmp_path)
    registry = _registry(tmp_path)
    job_id = asyncio.run(registry.register(_Meta(), "pending"))
    asyncio.run(registry.set_result(job_id, {"ok": True}))
    store.write(job_id, "p0.svg", "a")
    _age_job(registry, job_id, hours=48)

    out = asyncio.run(store.sweep(registry, ttl_hours=24, delete_jobs=False))
    assert out["dirs_removed"] == 1
    assert out["jobs_removed"] == 0
    assert asyncio.run(registry.get(job_id)) is not None


def test_sweep_reaches_expired_rows_past_the_list_all_window(tmp_path):
    """The phase-2 fix: pass 2 must not stop at `job_scan_limit` rows.

    Before ``SqliteRegistry.expired_job_ids``, pass 2 read the N most
    recently UPDATED jobs and filtered them here — so with more than N jobs
    inside one TTL window the oldest expired rows were never reached, which
    is exactly the case a busy deployment hits. The scan limit is forced to 2
    here; all five aged rows must still go.
    """
    store = _store(tmp_path)
    registry = _registry(tmp_path)
    job_ids = []
    for _ in range(5):
        job_id = asyncio.run(registry.register(_Meta(), "pending"))
        asyncio.run(registry.set_result(job_id, {"ok": True}))
        _age_job(registry, job_id, hours=48)
        job_ids.append(job_id)

    out = asyncio.run(store.sweep(registry, ttl_hours=24, job_scan_limit=2))
    assert out["jobs_removed"] == 5
    assert out["scanned"] == 5
    assert all(asyncio.run(registry.get(j)) is None for j in job_ids)


def test_sweep_falls_back_to_list_all_without_expired_job_ids(tmp_path):
    """A registry with no ``expired_job_ids`` still gets swept.

    The method is new; anything implementing the older registry shape must
    keep working, capped scan and all.
    """

    class _LegacyRegistry:
        def __init__(self, inner: SqliteRegistry) -> None:
            self._inner = inner
            self.list_all_calls = 0

        def get(self, job_id):
            return self._inner.get(job_id)

        def delete(self, job_id):
            return self._inner.delete(job_id)

        def list_all(self, limit: int = 20):
            self.list_all_calls += 1
            return self._inner.list_all(limit=limit)

    store = _store(tmp_path)
    inner = _registry(tmp_path)
    legacy = _LegacyRegistry(inner)
    job_id = asyncio.run(inner.register(_Meta(), "pending"))
    asyncio.run(inner.set_result(job_id, {"ok": True}))
    _age_job(inner, job_id, hours=48)

    out = asyncio.run(store.sweep(legacy, ttl_hours=24))
    assert legacy.list_all_calls == 1
    assert out["jobs_removed"] == 1
    assert asyncio.run(inner.get(job_id)) is None
