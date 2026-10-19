"""The /metrics poller: one asyncio task probing every known backend
CONCURRENTLY about once a second.

It is started lazily by the first filter call (on LiteLLM's own event loop)
and restarted if it has died. Backends are learnt from the deployments the
filter sees (OverflowFilter.learn -> Poller.watch), so there is nothing to
configure. A dead poller means stale data, and stale data means UNKNOWN --
the hook then changes nothing.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Dict, Optional

from ..log import logger
from .state import BackendState
from .vllm import Fetch, metrics_url, parse_queue_metrics

# Data older than this many poll intervals is "unknown" -> keep.
STALE_POLLS = 5


class Poller:
    """Owns the BackendState of every backend it was asked to watch."""

    def __init__(
        self,
        fetch: Fetch,
        clock: Callable[[], float],
        busy_after: float,
        idle_after: float,
        poll_interval: float,
        probe_timeout: float,
        start: bool,
        stats: Dict[str, int],
    ) -> None:
        self.busy_after = busy_after
        self.idle_after = idle_after
        self.poll_interval = poll_interval
        self.probe_timeout = probe_timeout
        self.stale_after = STALE_POLLS * poll_interval
        self.backends: Dict[str, BackendState] = {}
        self.stats = stats
        self._fetch = fetch
        self._clock = clock
        self._start = start
        self._task: Optional[asyncio.Task] = None
        self._task_loop: Optional[asyncio.AbstractEventLoop] = None

    def watch(self, api_base: str) -> None:
        """Start tracking a backend (no-op when already tracked)."""
        if api_base not in self.backends:
            self.backends[api_base] = BackendState(api_base=api_base, url=metrics_url(api_base))

    def verdict(self, api_base: str, now: float) -> str:
        """OK / BUSY / DOWN / UNKNOWN of a WATCHED backend (KeyError otherwise)."""
        return self.backends[api_base].verdict(now, self.stale_after)

    def running(self) -> bool:
        task = self._task
        return bool(task is not None and not task.done())

    def snapshot(self, now: float) -> Dict[str, Any]:
        return {b: s.snapshot(now, self.stale_after) for b, s in self.backends.items()}

    def report(self, now: float) -> Dict[str, Dict[str, Any]]:
        """api_base -> {verdict, waiting, running} of every watched backend
        (BackendState.report) -- the /decide request's `backends`."""
        return {b: s.report(now, self.stale_after) for b, s in self.backends.items()}

    def ensure_running(self) -> None:
        if not self._start:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # called outside a loop (tests); nothing to schedule on
        task = self._task
        if task is not None and not task.done() and self._task_loop is loop:
            return
        if task is not None and task.done() and not task.cancelled():
            exc = task.exception()
            if exc is not None:
                logger.warning("poller died (%r); restarting", exc)
            self.stats["poller_restarts"] += 1
        self._task_loop = loop
        self._task = loop.create_task(self._poll_forever(), name="zeo-overflow-poller")

    async def _poll_forever(self) -> None:
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # keep polling; a dead poller = stale = no spills
                logger.warning("poll pass failed: %r", exc)
            await asyncio.sleep(self.poll_interval)

    async def poll_once(self) -> None:
        """Probe every known backend concurrently and fold in the results."""
        states = list(self.backends.values())
        if not states:
            return
        results = await asyncio.gather(
            *(asyncio.wait_for(self._fetch(s.url), timeout=self.probe_timeout) for s in states),
            return_exceptions=True,
        )
        now = self._clock()
        for st, res in zip(states, results):
            if isinstance(res, BaseException):
                if isinstance(res, asyncio.CancelledError):
                    raise res
                st.apply_failure(now, f"{type(res).__name__}: {res}" if str(res) else type(res).__name__)
            else:
                waiting, running = parse_queue_metrics(res if isinstance(res, str) else "")
                st.apply_sample(now, waiting, running, self.busy_after, self.idle_after)
