"""Process-wide concurrency limits that survive more than one event loop.

``ConcurrencyLimit`` is an ``asyncio.Semaphore`` with two additions a service
with several job workers needs:

  * **Loop-local.** An ``asyncio.Semaphore`` binds to the event loop it is
    first contended on, and raises "is bound to a different event loop" when
    used from another one. A module-level limit therefore breaks as soon as
    code runs under a second loop — a test suite calling ``asyncio.run`` per
    test, a CLI that runs one loop per file. This keeps one semaphore per
    running loop (weakly keyed, so a finished loop's semaphore goes with it);
    within a loop — which is where a service's workers all live — it is one
    shared limit.
  * **Observable.** ``in_flight`` and ``peak`` say how many holders there are
    now and the most there have ever been at once, so a test can assert the
    limit held and a metrics hook can report it without wrapping every call.

``limit`` is read when a loop's semaphore is first created, so assigning a new
value before a fresh loop starts (a test lowering it) takes effect for that
loop.

Usage::

    LLM_CALLS = ConcurrencyLimit(4, name="llm")

    async with LLM_CALLS:
        await client.post(...)

Process flow position: a leaf utility; imports nothing from this package.
"""

from __future__ import annotations

import asyncio
import weakref


class ConcurrencyLimit:
    """At most ``limit`` holders at once, per running event loop."""

    def __init__(self, limit: int, *, name: str = "") -> None:
        if limit < 1:
            raise ValueError(f"limit must be >= 1, got {limit}")
        self.limit = int(limit)
        self.name = name
        self.in_flight = 0
        self.peak = 0
        self._by_loop: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = (
            weakref.WeakKeyDictionary()
        )

    def _semaphore(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        sem = self._by_loop.get(loop)
        if sem is None:
            sem = asyncio.Semaphore(self.limit)
            self._by_loop[loop] = sem
        return sem

    async def __aenter__(self) -> "ConcurrencyLimit":
        await self._semaphore().acquire()
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.in_flight -= 1
        self._semaphore().release()

    def reset_peak(self) -> None:
        """Forget the high-water mark (tests measure one scenario at a time)."""
        self.peak = self.in_flight

    def __repr__(self) -> str:
        return (
            f"ConcurrencyLimit(name={self.name!r}, limit={self.limit}, "
            f"in_flight={self.in_flight}, peak={self.peak})"
        )
