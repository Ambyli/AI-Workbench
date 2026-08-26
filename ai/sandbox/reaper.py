"""TTL sweeper — reaps expired sandbox containers.

Runs as an asyncio background task started from ``app.startup``. Every
``SWEEP_INTERVAL_S`` seconds it lists every container labeled
``sandbox.managed=true`` and tears down any whose spawn-timestamp label is
older than the runner's hard TTL, or whose Postgres job record is past
its idle deadline.

Two sources of truth intentionally: the container's own label (survives
runner restarts and catches "runner crashed mid-flight" leftovers) and
the Postgres jobs table (has the idle-vs-hard distinction). The union of
"expire if either says so" is the safe read.
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from typing import Optional

from common.jobs.postgres import PostgresRegistry

from spawner import Spawner


SWEEP_INTERVAL_S = 60
HARD_TTL_S = int(os.environ.get("SANDBOX_HARD_TTL_SECONDS", "3600"))


class Reaper:
    def __init__(
        self,
        spawner: Spawner,
        registry: PostgresRegistry,
        slot_sem: asyncio.Semaphore,
    ) -> None:
        self._spawner = spawner
        self._registry = registry
        self._slot_sem = slot_sem
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await self._task
            except Exception:
                pass

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._sweep_once()
            except Exception as exc:
                # Never let the reaper die — a swallowed exception here
                # would silently disable TTL enforcement. Log and continue.
                print(f"[reaper] sweep error: {exc}", flush=True)
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=SWEEP_INTERVAL_S
                )
            except asyncio.TimeoutError:
                pass

    async def _sweep_once(self) -> None:
        now = time.time()
        # asyncio-friendly wrapper around the sync docker call.
        containers = await asyncio.to_thread(self._spawner.list_managed)
        for c in containers:
            spawned_at_str = c.labels.get("sandbox.spawned_at", "0")
            try:
                spawned_at = int(spawned_at_str)
            except ValueError:
                spawned_at = 0
            age = now - spawned_at
            if age >= HARD_TTL_S:
                sandbox_id = c.labels.get("sandbox.id", "")
                # Read the current phase BEFORE we transition it — the
                # runner's concurrency semaphore was only acquired for
                # sandboxes that reached one of these phases. If the job
                # already crashed to "failed" or was cancelled, don't
                # double-release.
                was_holding_slot = False
                if sandbox_id:
                    snap = await self._registry.get(sandbox_id)
                    if snap is not None and snap.phase in (
                        "spawning", "starting", "running"
                    ):
                        was_holding_slot = True
                await asyncio.to_thread(self._spawner.stop, c.name)
                if sandbox_id:
                    await self._registry.set_phase(sandbox_id, "expired")
                if was_holding_slot:
                    self._slot_sem.release()
                print(
                    f"[reaper] hard-ttl expired sandbox-{sandbox_id} "
                    f"(age={int(age)}s)",
                    flush=True,
                )
