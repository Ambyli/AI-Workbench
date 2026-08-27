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
    """Fresh registry per test. Drops any existing ``jobs`` table first."""
    conn = await asyncpg.connect(_DSN)
    try:
        await conn.execute("DROP TABLE IF EXISTS jobs")
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
