"""Tests for common.jobs.limits.ConcurrencyLimit."""

from __future__ import annotations

import asyncio

import pytest

from common.jobs.limits import ConcurrencyLimit


def test_never_more_than_the_limit_in_flight():
    limit = ConcurrencyLimit(2, name="t")

    async def work():
        async with limit:
            await asyncio.sleep(0.01)

    async def main():
        await asyncio.gather(*(work() for _ in range(10)))

    asyncio.run(main())
    assert limit.peak == 2
    assert limit.in_flight == 0


def test_survives_a_second_event_loop():
    """A plain asyncio.Semaphore contended on loop 1 fails on loop 2."""
    limit = ConcurrencyLimit(1)

    async def contend():
        async def hold():
            async with limit:
                await asyncio.sleep(0.005)

        await asyncio.gather(hold(), hold(), hold())

    asyncio.run(contend())
    asyncio.run(contend())  # would raise "bound to a different event loop"
    assert limit.peak == 1


def test_a_new_limit_applies_to_the_next_loop():
    limit = ConcurrencyLimit(1)
    limit.limit = 3

    async def main():
        async def hold():
            async with limit:
                await asyncio.sleep(0.01)

        await asyncio.gather(*(hold() for _ in range(6)))

    asyncio.run(main())
    assert limit.peak == 3


def test_release_on_exception():
    limit = ConcurrencyLimit(1)

    async def main():
        with pytest.raises(RuntimeError):
            async with limit:
                raise RuntimeError("boom")
        async with limit:  # would deadlock if the slot leaked
            pass

    asyncio.run(asyncio.wait_for(main(), timeout=2))
    assert limit.in_flight == 0


def test_limit_must_be_positive():
    with pytest.raises(ValueError):
        ConcurrencyLimit(0)
