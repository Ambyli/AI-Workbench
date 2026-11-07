"""Tests for common.jobs.postgres (PostgresRegistry).

Requires a running Postgres reachable via the ``TEST_POSTGRES_DSN`` env
var, e.g.:

    TEST_POSTGRES_DSN=postgresql://postgres:postgres@localhost:5432/test_common_jobs

If the env var isn't set the entire module is skipped — matches how
integration-flavored tests are handled elsewhere in this repo. The
sandbox subsystem's operator runbook (``ai/sandbox/SANDBOX.md``) documents how
to spin up a throwaway Postgres for running these locally.

Each test drops and recreates the ``jobs`` table so tests don't interfere
with each other. Faster than spinning up a fresh Postgres per test.
"""

from __future__ import annotations

import os

import asyncpg
import pytest
import pytest_asyncio
from pydantic import BaseModel

from common.jobs.postgres import PostgresRegistry


_DSN = os.environ.get("TEST_POSTGRES_DSN")

pytestmark = [
    pytest.mark.skipif(not _DSN, reason="TEST_POSTGRES_DSN not set"),
    pytest.mark.asyncio,
]


class _Meta(BaseModel):
    type: str
    request_id: str


@pytest_asyncio.fixture
async def registry():
    """Fresh registry per test. Drops any existing ``jobs`` table first —
    ``CASCADE`` because test_jobs_progress_postgres.py's ``job_progress``
    table references it in the same database."""
    conn = await asyncpg.connect(_DSN)
    try:
        await conn.execute("DROP TABLE IF EXISTS jobs CASCADE")
    finally:
        await conn.close()

    reg = PostgresRegistry(_DSN)
    await reg.init()
    try:
        yield reg
    finally:
        await reg.close()


async def test_init_creates_schema(registry: PostgresRegistry) -> None:
    conn = await asyncpg.connect(_DSN)
    try:
        rows = await conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'jobs'"
        )
    finally:
        await conn.close()
    cols = {r["column_name"] for r in rows}
    assert cols == {
        "id",
        "phase",
        "created_at",
        "updated_at",
        "metadata",
        "result",
        "error",
    }


async def test_init_is_idempotent(registry: PostgresRegistry) -> None:
    # Registry is already initialized by the fixture; a second call must
    # not raise or duplicate anything.
    await registry.init()
    await registry.init()
    conn = await asyncpg.connect(_DSN)
    try:
        rows = await conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'jobs'"
        )
    finally:
        await conn.close()
    assert len(rows) == 7


async def test_register_get_roundtrip(registry: PostgresRegistry) -> None:
    job_id = await registry.register(
        _Meta(type="assess", request_id="req-1"), "pending"
    )
    assert len(job_id) == 12
    got = await registry.get(job_id)
    assert got is not None
    assert got.job_id == job_id
    assert got.phase == "pending"
    assert got.metadata == {"type": "assess", "request_id": "req-1"}
    assert got.result is None
    assert got.error is None


async def test_set_phase_result_error(registry: PostgresRegistry) -> None:
    job_id = await registry.register(
        _Meta(type="assess", request_id="r"), "pending"
    )
    await registry.set_phase(job_id, "processing")
    got = await registry.get(job_id)
    assert got is not None and got.phase == "processing"

    await registry.set_result(job_id, {"score": 0.9})
    got = await registry.get(job_id)
    assert got is not None
    assert got.phase == "completed"
    assert got.result == {"score": 0.9}

    job_id2 = await registry.register(
        _Meta(type="assess", request_id="r"), "pending"
    )
    await registry.set_error(job_id2, "boom")
    got2 = await registry.get(job_id2)
    assert got2 is not None
    assert got2.phase == "failed"
    assert got2.error == "boom"


async def test_update_metadata_merges(registry: PostgresRegistry) -> None:
    job_id = await registry.register({"a": 1, "b": 2}, "running")
    await registry.update_metadata(job_id, {"b": 20, "c": 3})
    got = await registry.get(job_id)
    assert got is not None
    assert got.metadata == {"a": 1, "b": 20, "c": 3}


async def test_list_all_orders_by_updated_at_desc(
    registry: PostgresRegistry,
) -> None:
    ids = []
    for i in range(3):
        ids.append(
            await registry.register(
                _Meta(type="assess", request_id=f"r{i}"), "pending"
            )
        )
    # Bump the middle one so it's freshest.
    await registry.set_phase(ids[1], "processing")
    got = await registry.list_all(limit=10)
    assert got.active_count == 3
    assert got.jobs[0].job_id == ids[1]


async def test_cancel_transitions_phase_and_refuses_terminal(
    registry: PostgresRegistry,
) -> None:
    job_id = await registry.register(
        _Meta(type="assess", request_id="r"), "pending"
    )
    ok, was = await registry.cancel(job_id)
    assert (ok, was) == (True, "pending")
    got = await registry.get(job_id)
    assert got is not None and got.phase == "cancelled"

    ok2, reason = await registry.cancel(job_id)
    assert ok2 is False
    assert reason == "already_cancelled"

    ok3, reason3 = await registry.cancel("nonexistent")
    assert ok3 is False
    assert reason3 == "not_found"


async def test_cancel_refuses_every_terminal_phase_and_leaves_the_row(
    registry: PostgresRegistry,
) -> None:
    done = await registry.register({"n": 1})
    failed = await registry.register({"n": 2})
    running = await registry.register({"n": 3}, "processing")
    await registry.set_result(done, {"ok": True})
    await registry.set_error(failed, "boom")
    assert await registry.cancel(done) == (False, "already_completed")
    assert await registry.cancel(failed) == (False, "already_failed")
    assert (await registry.get(done)).result == {"ok": True}
    assert (await registry.get(failed)).error == "boom"
    assert await registry.cancel(running) == (True, "processing")


async def test_guarded_writes_say_whether_they_landed(registry: PostgresRegistry) -> None:
    job_id = await registry.register({"n": 1})
    assert await registry.set_phase(job_id, "processing", from_phases=("pending",)) is True
    assert await registry.set_phase(job_id, "pending", from_phases=("staging",)) is False
    assert (await registry.get(job_id)).phase == "processing"
    assert await registry.set_result(job_id, {"x": 1}, from_phases=("pending",)) is False
    assert await registry.set_error(job_id, "no", from_phases=["staging", "pending"]) is False
    got = await registry.get(job_id)
    assert (got.phase, got.result, got.error) == ("processing", None, None)
    assert await registry.set_result(job_id, {"x": 1}, from_phases=("processing",)) is True
    assert await registry.set_error(job_id, "late", from_phases=("processing",)) is False
    got = await registry.get(job_id)
    assert (got.phase, got.result, got.error) == ("completed", {"x": 1}, None)


async def test_unguarded_writes_still_land_anywhere_and_report_a_missing_row(
    registry: PostgresRegistry,
) -> None:
    job_id = await registry.register({"n": 1})
    await registry.cancel(job_id)
    # Exactly the old behaviour: no guard, so it overwrites even a cancel.
    assert await registry.set_result(job_id, {"ok": True}) is True
    assert (await registry.get(job_id)).phase == "completed"
    assert await registry.set_phase("ghost", "pending") is False
    assert await registry.set_result("ghost", {}) is False
    assert await registry.set_error("ghost", "x") is False


async def test_a_guard_that_names_no_phase_is_refused(registry: PostgresRegistry) -> None:
    job_id = await registry.register({"n": 1})
    with pytest.raises(TypeError):
        await registry.set_phase(job_id, "pending", from_phases="processing")
    with pytest.raises(ValueError):
        await registry.set_result(job_id, {}, from_phases=())


async def test_a_cancel_holding_the_row_lock_wins_over_a_guarded_result(
    registry: PostgresRegistry,
) -> None:
    """READ COMMITTED: the guarded UPDATE waits for the cancel's row lock,
    then re-checks the phase against the committed row — and misses. The
    other connection runs exactly cancel()'s statements, held open."""
    import asyncio

    job_id = await registry.register({"n": 1}, "processing")
    conn = await asyncpg.connect(_DSN)
    try:
        tx = conn.transaction()
        await tx.start()
        await conn.execute("SELECT phase FROM jobs WHERE id = $1 FOR UPDATE", job_id)
        await conn.execute("UPDATE jobs SET phase = 'cancelled' WHERE id = $1", job_id)
        write = asyncio.create_task(
            registry.set_result(job_id, {"late": True}, from_phases=("processing",))
        )
        await asyncio.sleep(0.3)
        assert not write.done()  # waiting on the lock
        await tx.commit()
        assert await asyncio.wait_for(write, 5) is False
    finally:
        await conn.close()
    got = await registry.get(job_id)
    assert (got.phase, got.result) == ("cancelled", None)


async def test_a_cancel_waits_for_a_result_holding_the_row_lock_and_refuses(
    registry: PostgresRegistry,
) -> None:
    """The other order: the guarded result write holds the lock, cancel()'s
    SELECT … FOR UPDATE waits for it, then sees ``completed``."""
    import asyncio

    job_id = await registry.register({"n": 1}, "processing")
    conn = await asyncpg.connect(_DSN)
    try:
        tx = conn.transaction()
        await tx.start()
        status = await conn.execute(
            "UPDATE jobs SET phase = 'completed', result = '{\"ok\": true}'::jsonb "
            "WHERE id = $1 AND phase = ANY($2::text[])",
            job_id, ["processing"],
        )
        assert status == "UPDATE 1"
        cancel = asyncio.create_task(registry.cancel(job_id))
        await asyncio.sleep(0.3)
        assert not cancel.done()  # waiting on the lock
        await tx.commit()
        assert await asyncio.wait_for(cancel, 5) == (False, "already_completed")
    finally:
        await conn.close()
    got = await registry.get(job_id)
    assert (got.phase, got.result) == ("completed", {"ok": True})


async def test_a_cancel_and_a_guarded_result_racing_have_one_winner(
    registry: PostgresRegistry,
) -> None:
    """Whichever lands first, the other is refused, and the row says what
    the winner wrote — over and over, on two pooled connections."""
    import asyncio

    for i in range(20):
        job_id = await registry.register({"i": i}, "processing")
        stored, (cancelled, why) = await asyncio.gather(
            registry.set_result(job_id, {"i": i}, from_phases=("processing",)),
            registry.cancel(job_id),
        )
        got = await registry.get(job_id)
        assert stored != cancelled, (stored, cancelled, why)
        if stored:
            assert why == "already_completed" and got.phase == "completed"
            assert got.result == {"i": i}
        else:
            assert why == "processing" and got.phase == "cancelled" and got.result is None


async def test_delete(registry: PostgresRegistry) -> None:
    job_id = await registry.register(
        _Meta(type="assess", request_id="r"), "pending"
    )
    assert await registry.delete(job_id) is True
    assert await registry.get(job_id) is None
    assert await registry.delete(job_id) is False


async def test_concurrent_registers_no_collision(
    registry: PostgresRegistry,
) -> None:
    """Fire many concurrent registers — the pool should handle them and no
    two jobs should collide on the primary key. Validates the asyncpg pool
    is genuinely concurrent (SQLite would serialize)."""
    import asyncio

    ids = await asyncio.gather(
        *(
            registry.register({"i": i}, "pending")
            for i in range(20)
        )
    )
    assert len(set(ids)) == 20  # all unique
    listing = await registry.list_all(limit=100)
    assert listing.active_count == 20


async def test_metadata_is_queryable_jsonb(registry: PostgresRegistry) -> None:
    """metadata is stored as JSONB — operators can query it from psql
    with JSON operators. Prove it by filtering with ``->>``."""
    await registry.register({"kind": "streamlit", "user": "amber"}, "pending")
    await registry.register({"kind": "vite", "user": "amber"}, "pending")
    await registry.register({"kind": "streamlit", "user": "other"}, "pending")

    conn = await asyncpg.connect(_DSN)
    try:
        rows = await conn.fetch(
            "SELECT id FROM jobs WHERE metadata->>'kind' = 'streamlit'"
        )
    finally:
        await conn.close()
    assert len(rows) == 2


# ── Queue operations ──────────────────────────────────────────────────────
async def test_claim_next_is_fifo_and_flips_phase(registry: PostgresRegistry) -> None:
    a = await registry.register(_Meta(type="assess", request_id="a"))
    b = await registry.register(_Meta(type="assess", request_id="b"))

    first = await registry.claim_next()
    assert first is not None and first.job_id == a and first.phase == "processing"
    assert (await registry.get(b)).phase == "pending"
    second = await registry.claim_next()
    assert second.job_id == b
    assert await registry.claim_next() is None


async def test_concurrent_claims_never_hand_out_the_same_job(
    registry: PostgresRegistry,
) -> None:
    import asyncio

    ids = {await registry.register(_Meta(type="assess", request_id=str(i))) for i in range(8)}
    claimed = await asyncio.gather(*(registry.claim_next() for _ in range(12)))
    got = [j.job_id for j in claimed if j is not None]
    assert len(got) == 8 and set(got) == ids
    assert sum(1 for j in claimed if j is None) == 4


async def test_reset_phase_and_count_by_phase(registry: PostgresRegistry) -> None:
    for i in range(3):
        await registry.register(_Meta(type="assess", request_id=str(i)))
    await registry.claim_next()
    assert await registry.count_by_phase() == {"pending": 2, "processing": 1}
    assert await registry.reset_phase("processing", "pending") == 1
    assert await registry.count_by_phase() == {"pending": 3}


# ── Bounded admission (register(max_open=), count_open) ───────────────────
async def test_max_open_admits_up_to_the_limit_and_refuses_with_no_row(
    registry: PostgresRegistry,
) -> None:
    from common.jobs import QueueFull

    ids = [await registry.register({"i": i}, "pending", max_open=3) for i in range(3)]
    assert len(set(ids)) == 3
    with pytest.raises(QueueFull) as info:
        await registry.register({"i": 3}, "pending", max_open=3)
    assert (info.value.open_jobs, info.value.limit) == (3, 3)
    assert "3 job(s) waiting or running" in str(info.value)
    # Nothing was written for the refusal.
    assert await registry.count_by_phase() == {"pending": 3}
    assert (await registry.list_all(limit=10)).active_count == 3


async def test_terminal_rows_do_not_count_staging_and_custom_phases_do(
    registry: PostgresRegistry,
) -> None:
    from common.jobs import QueueFull

    for phase in ("completed", "failed", "cancelled"):
        await registry.register({"done": phase}, phase)
    assert await registry.count_open() == 0
    # The classifier's "staging", a running job, an interceptor-style phase.
    for phase in ("staging", "processing", "capturing"):
        await registry.register({"open": phase}, phase)
    assert await registry.count_open() == 3
    with pytest.raises(QueueFull) as info:
        await registry.register({}, "staging", max_open=3)
    assert info.value.open_jobs == 3
    # One finishing frees its slot at once — a cancel included.
    staged = [j for j in (await registry.list_all(limit=20)).jobs if j.phase == "staging"][0]
    assert await registry.cancel(staged.job_id) == (True, "staging")
    assert await registry.count_open() == 2
    await registry.register({}, "staging", max_open=3)
    assert await registry.count_open() == 3


async def test_open_rows_already_over_the_limit_refuse_until_they_drain(
    registry: PostgresRegistry,
) -> None:
    """As after a restart (rows requeued) or a lowered limit: QueueFull
    reports the real count, and admission resumes only below the limit."""
    from common.jobs import QueueFull

    ids = [await registry.register({"i": i}, "pending") for i in range(5)]
    with pytest.raises(QueueFull) as info:
        await registry.register({}, "pending", max_open=2)
    assert (info.value.open_jobs, info.value.limit) == (5, 2)
    for job_id in ids[:3]:
        await registry.set_result(job_id, {"ok": True})
    with pytest.raises(QueueFull):
        await registry.register({}, "pending", max_open=2)  # 2 open: still full
    await registry.delete(ids[3])  # a DELETE frees a slot too
    await registry.register({}, "pending", max_open=2)
    assert await registry.count_open() == 2


async def test_concurrent_registers_on_separate_connections_never_overshoot(
    registry: PostgresRegistry,
) -> None:
    """The race the advisory lock exists for: 20 submits on 20 connections
    (20 registries, a pool of one each — as 20 processes would be) racing
    for 5 slots. A count-then-insert without the lock lets several see room."""
    import asyncio

    from common.jobs import QueueFull

    racers = [PostgresRegistry(_DSN, min_size=1, max_size=1) for _ in range(20)]
    for r in racers:
        await r.init()
    try:
        outcomes = await asyncio.gather(
            *(r.register({"racer": i}, "staging", max_open=5) for i, r in enumerate(racers)),
            return_exceptions=True,
        )
    finally:
        for r in racers:
            await r.close()
    admitted = [o for o in outcomes if isinstance(o, str)]
    refused = [o for o in outcomes if isinstance(o, QueueFull)]
    assert len(admitted) == 5 and len(refused) == 15, outcomes
    assert all(e.open_jobs == 5 and e.limit == 5 for e in refused)
    assert await registry.count_open() == 5
    assert await registry.count_by_phase() == {"staging": 5}


async def test_max_open_none_is_unbounded_and_below_one_is_refused(
    registry: PostgresRegistry,
) -> None:
    for i in range(12):
        await registry.register({"i": i}, "pending", max_open=None)
    assert await registry.count_open() == 12
    for bad in (0, -1):
        with pytest.raises(ValueError, match="None means no limit"):
            await registry.register({}, "pending", max_open=bad)
    for bad in (True, 2.5, "3"):
        with pytest.raises(TypeError):
            await registry.register({}, "pending", max_open=bad)
    assert await registry.count_open() == 12  # nothing written by a refusal


async def test_count_open_counts_every_unfinished_phase(registry: PostgresRegistry) -> None:
    assert await registry.count_open() == 0
    a = await registry.register({}, "staging")
    b = await registry.register({})
    await registry.register({})
    await registry.claim_next()
    assert await registry.count_open() == 3
    await registry.set_error(a, "boom")
    assert await registry.count_open() == 2
    await registry.set_phase(b, "completed")
    assert await registry.count_open() == 1


async def test_max_open_works_on_an_external_pool(registry: PostgresRegistry) -> None:
    """The classifier hands the registry its own pool (``pool=``): the
    admission's transaction runs on a connection from it like any write."""
    from common.jobs import QueueFull

    pool = await asyncpg.create_pool(_DSN, min_size=1, max_size=2)
    try:
        shared = PostgresRegistry(pool=pool)
        await shared.register({}, "staging", max_open=1)
        with pytest.raises(QueueFull):
            await shared.register({}, "staging", max_open=1)
        assert await shared.count_open() == 1
    finally:
        await pool.close()


# ── Retention ─────────────────────────────────────────────────────────────
async def _backdate(job_id: str, seconds: float) -> None:
    """Move a job's created_at ``seconds`` into the past."""
    conn = await asyncpg.connect(_DSN)
    try:
        await conn.execute(
            "UPDATE jobs SET created_at = created_at - make_interval(secs => $1) "
            "WHERE id = $2",
            float(seconds),
            job_id,
        )
    finally:
        await conn.close()


async def test_expired_job_ids_terminal_only_oldest_first(
    registry: PostgresRegistry,
) -> None:
    from datetime import datetime, timedelta, timezone

    old_done = await registry.register({"n": 1})
    await registry.set_result(old_done, {"ok": True})
    older_failed = await registry.register({"n": 2})
    await registry.set_error(older_failed, "boom")
    old_pending = await registry.register({"n": 3})          # never expires
    fresh_done = await registry.register({"n": 4})
    await registry.set_result(fresh_done, {"ok": True})
    await _backdate(old_done, 3600)
    await _backdate(older_failed, 7200)
    await _backdate(old_pending, 7200)

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
    assert await registry.expired_job_ids(cutoff) == [older_failed, old_done]
    assert await registry.expired_job_ids(cutoff, limit=1) == [older_failed]
    assert await registry.expired_job_ids(cutoff, ["completed"]) == [old_done]
    assert await registry.expired_job_ids(cutoff, []) == []
    # A naive cutoff is read as UTC.
    naive = cutoff.replace(tzinfo=None)
    assert await registry.expired_job_ids(naive) == [older_failed, old_done]


async def test_expired_job_ids_drives_the_artifact_sweep(
    registry: PostgresRegistry, tmp_path
) -> None:
    """``ArtifactStore.sweep`` takes the bounded expired_job_ids route and
    removes both the expired row and its directory."""
    from common.vision import ArtifactStore

    expired = await registry.register({"n": 1})
    await registry.set_result(expired, {"ok": True})
    kept = await registry.register({"n": 2})
    await registry.set_result(kept, {"ok": True})
    await _backdate(expired, 7200)

    store = ArtifactStore(str(tmp_path))
    store.write_json(expired, "regions.json", {"regions": []})
    store.write_json(kept, "regions.json", {"regions": []})
    out = await store.sweep(registry, ttl_hours=1)
    assert out["jobs_removed"] == 1 and out["dirs_removed"] == 1
    assert await registry.get(expired) is None
    assert await registry.get(kept) is not None
    assert store.job_ids() == [kept]


# ── External pool ─────────────────────────────────────────────────────────
async def test_constructor_needs_exactly_one_of_dsn_and_pool() -> None:
    with pytest.raises(ValueError):
        PostgresRegistry()
    with pytest.raises(ValueError):
        PostgresRegistry(_DSN, pool=object())


async def test_external_pool_is_used_and_never_closed() -> None:
    conn = await asyncpg.connect(_DSN)
    try:
        await conn.execute("DROP TABLE IF EXISTS jobs CASCADE")
    finally:
        await conn.close()

    pool = await asyncpg.create_pool(_DSN, min_size=1, max_size=2)
    try:
        reg = PostgresRegistry(pool=pool)
        assert reg.owns_pool is False
        await reg.init()          # schema only — no pool of its own
        job_id = await reg.register({"via": "external"})
        assert (await reg.get(job_id)).metadata == {"via": "external"}

        await reg.close()         # a no-op for a pool it does not own
        assert not pool._closed
        async with pool.acquire() as c:
            assert await c.fetchval("SELECT count(*) FROM jobs") == 1

        # A second registry on the same pool sees the same rows.
        other = PostgresRegistry(pool=pool)
        await other.init()
        assert (await other.get(job_id)) is not None
    finally:
        await pool.close()


async def test_external_pool_may_be_any_acquire_facade() -> None:
    """Anything whose ``acquire()`` yields an asyncpg connection will do —
    the classifier hands in a facade, not an ``asyncpg.Pool``."""
    from contextlib import asynccontextmanager

    class OneShot:
        def __init__(self) -> None:
            self.acquired = 0

        @asynccontextmanager
        async def acquire(self):
            self.acquired += 1
            conn = await asyncpg.connect(_DSN)
            try:
                yield conn
            finally:
                await conn.close()

    conn = await asyncpg.connect(_DSN)
    try:
        await conn.execute("DROP TABLE IF EXISTS jobs CASCADE")
    finally:
        await conn.close()

    facade = OneShot()
    reg = PostgresRegistry(pool=facade)
    await reg.init()
    job_id = await reg.register({"x": 1})
    claimed = await reg.claim_next()
    assert claimed is not None and claimed.job_id == job_id
    assert await reg.count_by_phase() == {"processing": 1}
    assert facade.acquired == 4   # init, register, claim, count


# ── result_type ───────────────────────────────────────────────────────────
async def test_result_type_json_keeps_key_order() -> None:
    """JSONB sorts object keys; ``result_type="json"`` stores the text
    verbatim, so a result whose key order means something survives."""
    conn = await asyncpg.connect(_DSN)
    try:
        await conn.execute("DROP TABLE IF EXISTS jobs CASCADE")
    finally:
        await conn.close()

    ordered = {"zeta": 1, "alpha": 2, "document legibility": 3, "b": 4}
    reg = PostgresRegistry(_DSN, result_type="json")
    await reg.init()
    try:
        job_id = await reg.register({"k": 1})
        await reg.set_result(job_id, {"scores": ordered})
        got = await reg.get(job_id)
        assert list(got.result["scores"]) == list(ordered)
        conn = await asyncpg.connect(_DSN)
        try:
            kind = await conn.fetchval(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_name = 'jobs' AND column_name = 'result'"
            )
        finally:
            await conn.close()
        assert kind == "json"
    finally:
        await reg.close()


async def test_result_type_jsonb_is_the_default_and_reorders(
    registry: PostgresRegistry,
) -> None:
    """Pins WHY the option exists: the default JSONB column does reorder."""
    job_id = await registry.register({"k": 1})
    await registry.set_result(job_id, {"b": 1, "a": 2})
    assert list((await registry.get(job_id)).result) == ["a", "b"]


async def test_result_type_is_validated() -> None:
    with pytest.raises(ValueError):
        PostgresRegistry(_DSN, result_type="text")
