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

  * **Weighted.** ``async with limit.hold(n)`` takes n slots at once for one
    unit of work that costs n on the far side (the classifier's sampling
    confidence asks a model server for n completions in ONE request). The n
    slots are collected under a per-loop gate, so two multi-slot holders can
    never deadlock on half each; ``in_flight`` / ``peak`` count slots.

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
        # The multi-slot gate (``hold``), per loop like the semaphore.
        self._gates: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = (
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

    def _gate(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        gate = self._gates.get(loop)
        if gate is None:
            gate = asyncio.Lock()
            self._gates[loop] = gate
        return gate

    def hold(self, slots: int = 1) -> "_Hold":
        """``async with limit.hold(n):`` — take ``n`` slots at once.

        For one request that is n units of work on the server (a model asked
        for ``n`` completions in one call). The n slots are collected under a
        per-loop gate, so two multi-slot holders never each sit on half of
        the slots waiting for the other's (the deadlock taking them one at a
        time would allow); single-slot holders never hold partial sets, so
        they cannot block the gate holder for longer than their own work.
        ``in_flight`` / ``peak`` count SLOTS, so a ``hold(3)`` adds 3.

        Raises:
            ValueError: ``slots`` < 1, or more than ``limit`` (it could never
                be satisfied).
        """
        if slots < 1 or slots > self.limit:
            raise ValueError(f"slots must be 1..{self.limit}, got {slots}")
        return _Hold(self, int(slots))

    def reset_peak(self) -> None:
        """Forget the high-water mark (tests measure one scenario at a time)."""
        self.peak = self.in_flight

    def __repr__(self) -> str:
        return (
            f"ConcurrencyLimit(name={self.name!r}, limit={self.limit}, "
            f"in_flight={self.in_flight}, peak={self.peak})"
        )


class _Hold:
    """``ConcurrencyLimit.hold(n)``'s context manager."""

    def __init__(self, limit: ConcurrencyLimit, slots: int) -> None:
        self._limit = limit
        self.slots = slots

    async def __aenter__(self) -> ConcurrencyLimit:
        limit = self._limit
        if self.slots == 1:
            return await limit.__aenter__()
        sem = limit._semaphore()
        async with limit._gate():
            taken = 0
            try:
                for _ in range(self.slots):
                    await sem.acquire()
                    taken += 1
            except BaseException:
                for _ in range(taken):
                    sem.release()
                raise
        limit.in_flight += self.slots
        limit.peak = max(limit.peak, limit.in_flight)
        return limit

    async def __aexit__(self, exc_type, exc, tb) -> None:
        limit = self._limit
        if self.slots == 1:
            await limit.__aexit__(exc_type, exc, tb)
            return
        limit.in_flight -= self.slots
        sem = limit._semaphore()
        for _ in range(self.slots):
            sem.release()
