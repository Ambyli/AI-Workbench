"""Tests for common.jobs.progress_postgres and build_progress_router.

Requires a running Postgres reachable via ``TEST_POSTGRES_DSN`` — skipped
without it, like ``test_jobs_postgres.py``, which shares the same database:

    TEST_POSTGRES_DSN=postgresql://postgres@localhost:5432/postgres

Each test drops both tables (history first — it references ``jobs``) and
recreates them, and drops them again afterwards so ``test_jobs_postgres.py``
never finds a table hanging off its ``jobs``.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager

import asyncpg
import pytest
import pytest_asyncio

from common.jobs.postgres import PostgresRegistry
from common.jobs.progress import DONE, ProgressGauge, Stage, checkpoint, plan
from common.jobs.progress_postgres import PostgresProgressStore

_DSN = os.environ.get("TEST_POSTGRES_DSN")

pytestmark = pytest.mark.skipif(not _DSN, reason="TEST_POSTGRES_DSN not set")

STAGES = (
    Stage("load", "Loading documents", 10),
    Stage("units", "Evaluating criteria", 85),
    Stage("artifacts", "Writing artifacts", 5),
)


async def _drop() -> None:
    conn = await asyncpg.connect(_DSN)
    try:
        await conn.execute("DROP TABLE IF EXISTS job_progress")
        await conn.execute("DROP TABLE IF EXISTS jobs")
    finally:
        await conn.close()


async def _fetch(sql: str, *args):
    conn = await asyncpg.connect(_DSN)
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def stores():
    await _drop()
    pool = await asyncpg.create_pool(_DSN, min_size=1, max_size=4)
    registry = PostgresRegistry(pool=pool)
    store = PostgresProgressStore(pool=pool)
    await registry.init()
    await store.init()
    try:
        yield registry, store
    finally:
        await pool.close()
        await _drop()


@pytest.mark.asyncio
async def test_init_is_idempotent_and_creates_the_table(stores):
    _, store = stores
    await store.init()
    await store.init()
    cols = {r["column_name"] for r in await _fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'job_progress'"
    )}
    assert cols == {"id", "job_id", "attempt", "seq", "at", "stage", "label", "idx",
                    "length", "percent", "state"}


@pytest.mark.asyncio
async def test_a_bad_identifier_is_refused():
    with pytest.raises(ValueError):
        PostgresProgressStore(pool=object(), table="job_progress; DROP TABLE jobs")
    with pytest.raises(ValueError):
        PostgresProgressStore(pool=None)


@pytest.mark.asyncio
async def test_queued_then_a_run_writes_the_snapshot_and_the_history(stores):
    registry, store = stores
    job_id = await registry.register({"type": "assess"}, initial_phase="staging")
    await store.queued(job_id, stages=STAGES)
    job = await registry.get(job_id)
    assert job.metadata["type"] == "assess"  # merged, not replaced
    assert job.metadata["progress"]["state"] == "queued"
    assert job.metadata["progress"]["percent"] == 0
    assert [s["name"] for s in job.metadata["progress"]["stages"]] == ["load", "units", "artifacts"]
    assert await store.next_attempt(job_id) == 1

    gauge = ProgressGauge(job_id, STAGES, store=store, flush_interval=0.05, attempt=1)
    async with gauge.running():
        plan("units", 3)
        for i in range(3):
            checkpoint(f"unit {i}", stage="units")

    job = await registry.get(job_id)
    assert job.metadata["progress"]["state"] == DONE
    assert job.metadata["progress"]["percent"] == 100
    assert job.metadata["progress"]["attempt"] == 1
    history = await store.history(job_id)
    assert history["attempt"] == 1
    labels = [e["label"] for e in history["events"]]
    assert labels == ["started", "unit 0", "unit 1", "unit 2", "done"]
    assert [e["seq"] for e in history["events"]] == [1, 2, 3, 4, 5]
    assert history["events"][2]["index"] == 2 and history["events"][2]["length"] == 3
    assert history["events"][2]["percent"] == round(10 + 85 * 2 / 3, 1)
    assert history["next_after"] == 5
    # The queued event is attempt 0.
    queued = await store.history(job_id, attempt=0)
    assert [(e["seq"], e["state"]) for e in queued["events"]] == [(1, "queued")]
    assert await store.attempts(job_id) == [0, 1]
    assert await store.next_attempt(job_id) == 2


@pytest.mark.asyncio
async def test_a_flush_is_atomic(stores):
    registry, store = stores
    job_id = await registry.register({"type": "assess"})
    good = {"seq": 1, "at": "2026-10-09T00:00:00+00:00", "stage": "units", "label": "a",
            "index": 1, "length": 2, "percent": 50.0, "state": "running"}
    bad = dict(good, seq=2, label=12345)  # a TEXT column refuses an int — mid-transaction
    with pytest.raises(Exception):
        await store.flush(job_id, {"attempt": 1, "state": "running", "percent": 50.0}, [good, bad])
    job = await registry.get(job_id)
    assert "progress" not in job.metadata  # the snapshot rolled back with the events
    assert (await store.history(job_id))["events"] == []


@pytest.mark.asyncio
async def test_a_flush_twice_does_not_duplicate_rows(stores):
    registry, store = stores
    job_id = await registry.register({})
    event = {"seq": 1, "at": "2026-10-09T00:00:00Z", "stage": None, "label": "started",
             "index": 0, "length": 0, "percent": 0.0, "state": "running"}
    snapshot = {"attempt": 1, "state": "running", "percent": 0.0}
    await store.flush(job_id, snapshot, [event])
    await store.flush(job_id, snapshot, [event])
    assert len((await store.history(job_id))["events"]) == 1


@pytest.mark.asyncio
async def test_a_flush_for_a_deleted_job_is_dropped_quietly(stores):
    registry, store = stores
    job_id = await registry.register({})
    await registry.delete(job_id)
    event = {"seq": 1, "at": "2026-10-09T00:00:00Z", "stage": None, "label": "x",
             "index": 0, "length": 0, "percent": 0.0, "state": "running"}
    await store.flush(job_id, {"attempt": 1, "state": "running"}, [event])  # no raise
    assert (await _fetch("SELECT count(*) AS n FROM job_progress"))[0]["n"] == 0


@pytest.mark.asyncio
async def test_deleting_the_job_deletes_its_history(stores):
    registry, store = stores
    keep = await registry.register({})
    gone = await registry.register({})
    for job_id in (keep, gone):
        await store.queued(job_id)
        gauge = ProgressGauge(job_id, STAGES, store=store, attempt=1)
        async with gauge.running():
            checkpoint("x", stage="units")
    assert await registry.delete(gone)
    rows = await _fetch("SELECT DISTINCT job_id FROM job_progress")
    assert {r["job_id"] for r in rows} == {keep}


@pytest.mark.asyncio
async def test_requeued_resets_running_snapshots_to_queued(stores):
    registry, store = stores
    stuck = await registry.register({}, initial_phase="processing")
    finished = await registry.register({}, initial_phase="completed")
    for job_id in (stuck, finished):
        await store.queued(job_id, stages=STAGES)
    # A run that was interrupted: it flushed "running" and never finished.
    gauge = ProgressGauge(stuck, STAGES, store=store, attempt=1)
    gauge.plan("units", 4)
    gauge.advance("units", 2, label="halfway")
    await gauge.flush()
    assert (await registry.get(stuck)).metadata["progress"]["state"] == "running"

    await registry.reset_phase("processing", "pending")
    assert await store.requeued() == 1
    assert await store.requeued() == 0  # nothing still says running

    snap = (await registry.get(stuck)).metadata["progress"]
    assert snap["state"] == "queued" and snap["percent"] == 0 and snap["attempt"] == 1
    assert snap["label"] == "requeued after a restart"
    assert [s["index"] for s in snap["stages"]] == [0, 0, 0]
    history = await store.history(stuck, attempt=1)
    assert history["events"][-1]["label"] == "requeued after a restart"
    assert history["events"][-1]["state"] == "queued"
    # The next run is a new attempt.
    assert await store.next_attempt(stuck) == 2
    assert (await registry.get(finished)).metadata["progress"]["state"] == "queued"


@pytest.mark.asyncio
async def test_history_pages_with_after_and_limit(stores):
    registry, store = stores
    job_id = await registry.register({})
    gauge = ProgressGauge(job_id, STAGES, store=store, attempt=1)
    async with gauge.running():
        plan("units", 10)
        for i in range(10):
            checkpoint(f"u{i}", stage="units")
    first = await store.history(job_id, limit=5)
    assert [e["seq"] for e in first["events"]] == [1, 2, 3, 4, 5]
    second = await store.history(job_id, after=first["next_after"], limit=5)
    assert [e["seq"] for e in second["events"]] == [6, 7, 8, 9, 10]
    third = await store.history(job_id, after=second["next_after"], limit=5)
    # seq 1 is "started", so the ten checkpoints are 2..11 and "done" is 12.
    assert [e["label"] for e in third["events"]] == ["u9", "done"]
    empty = await store.history(job_id, after=third["next_after"])
    assert empty["events"] == [] and empty["next_after"] == third["next_after"]
    unknown = await store.history("nope")
    assert unknown == {"attempt": None, "events": [], "next_after": 0}


def test_the_progress_router():
    """GET /jobs/{job_id}/progress through FastAPI, with the pool created on
    the TestClient's own loop (an asyncpg pool belongs to its loop)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from common.jobs.router import build_progress_router, build_router

    asyncio.run(_drop())
    state: dict = {}

    @asynccontextmanager
    async def lifespan(app):
        pool = await asyncpg.create_pool(_DSN, min_size=1, max_size=4)
        registry = PostgresRegistry(pool=pool)
        store = PostgresProgressStore(pool=pool)
        await registry.init()
        await store.init()
        job_id = await registry.register({"type": "assess"})
        await store.queued(job_id, stages=STAGES)
        gauge = ProgressGauge(job_id, STAGES, store=store, attempt=1)
        async with gauge.running():
            plan("units", 3)
            for i in range(3):
                checkpoint(f"u{i}", stage="units")
        state["job_id"] = job_id
        app.include_router(build_router(registry))
        app.include_router(build_progress_router(registry, store))
        yield
        await pool.close()

    app = FastAPI(lifespan=lifespan)
    try:
        with TestClient(app) as client:
            job_id = state["job_id"]
            body = client.get(f"/jobs/{job_id}/progress").json()
            assert body["job_id"] == job_id and body["phase"] == "pending"
            assert body["progress"]["state"] == "done" and body["progress"]["percent"] == 100
            assert body["attempt"] == 1
            assert [e["label"] for e in body["events"]] == ["started", "u0", "u1", "u2", "done"]
            assert body["next_after"] == 5

            page = client.get(f"/jobs/{job_id}/progress", params={"after": 2, "limit": 2}).json()
            assert [e["seq"] for e in page["events"]] == [3, 4] and page["next_after"] == 4

            queued = client.get(f"/jobs/{job_id}/progress", params={"attempt": 0}).json()
            assert [e["state"] for e in queued["events"]] == ["queued"]

            # limit is clamped, not refused
            assert client.get(f"/jobs/{job_id}/progress", params={"limit": 5000}).status_code == 200
            assert client.get(f"/jobs/{job_id}/progress", params={"limit": 0}).status_code == 200

            # The snapshot is on the plain job route too.
            assert client.get(f"/jobs/{job_id}").json()["metadata"]["progress"]["state"] == "done"

            assert client.get("/jobs/nope/progress").status_code == 404
    finally:
        asyncio.run(_drop())
