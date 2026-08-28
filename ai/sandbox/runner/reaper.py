"""TTL sweeper — reaps expired sandbox containers.

Runs as an asyncio background task started from ``app.startup``. Every
``SWEEP_INTERVAL_S`` seconds it lists every container labeled
``sandbox.managed=true`` and tears down any whose spawn-timestamp label
is older than the runner's hard TTL. On the same sweep it queries the
Postgres jobs table for any running sandbox whose ``last_used_at`` in
JSONB metadata is older than the idle TTL and tears those down too.

Two sources of truth intentionally: the container's own label (survives
runner restarts and catches "runner crashed mid-flight" leftovers) and
the Postgres jobs table (has the session/idle metadata). The union of
"expire if either says so" is the safe read.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from typing import Optional

from common.jobs.postgres import PostgresRegistry

from spawner import Spawner


SWEEP_INTERVAL_S = 60
HARD_TTL_S = int(os.environ.get("SANDBOX_HARD_TTL_SECONDS", "3600"))
# Idle TTL bounds "container is up but nobody's touched the session
# recently" — the reaper tears it down even though hard TTL hasn't hit.
# Defaults to the same value as SANDBOX_DEFAULT_TTL_SECONDS (15 min) so
# operators only have to think about one knob unless they want a
# distinct idle policy.
IDLE_TTL_S = int(
    os.environ.get(
        "SANDBOX_IDLE_TTL_SECONDS",
        os.environ.get("SANDBOX_DEFAULT_TTL_SECONDS", "900"),
    )
)


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
        # Track sandbox_ids we've already reaped this sweep so the idle-
        # ttl pass doesn't try to expire the same one the hard-ttl pass
        # just handled (double-release of the semaphore would over-count).
        reaped: set[str] = set()
        for c in containers:
            spawned_at_str = c.labels.get("sandbox.spawned_at", "0")
            try:
                spawned_at = int(spawned_at_str)
            except ValueError:
                spawned_at = 0
            age = now - spawned_at
            if age >= HARD_TTL_S:
                sandbox_id = c.labels.get("sandbox.id", "")
                await self._reap(c.name, sandbox_id)
                if sandbox_id:
                    reaped.add(sandbox_id)
                print(
                    f"[reaper] hard-ttl expired sandbox-{sandbox_id} "
                    f"(age={int(age)}s)",
                    flush=True,
                )

        # Idle-TTL pass: read every running job's metadata and expire
        # any whose last_used_at is older than IDLE_TTL_S. Cheap (one
        # SQL round-trip, one docker stop per idle sandbox) and independent
        # of the docker-labels loop so a sandbox with no label but a live
        # row still gets reaped.
        stale = await self._find_idle(now)
        for sandbox_id, container_name in stale:
            if sandbox_id in reaped:
                continue
            await self._reap(container_name, sandbox_id)
            print(
                f"[reaper] idle-ttl expired sandbox-{sandbox_id} "
                f"(container={container_name})",
                flush=True,
            )

    async def _reap(self, container_name: str, sandbox_id: str) -> None:
        """Common teardown path used by both the hard-TTL and idle-TTL
        checks. Reads phase first so the semaphore is only released when
        the sandbox was actually holding a slot — avoids drift when a
        job crashed to 'failed' between spawn and reap."""
        was_holding_slot = False
        if sandbox_id:
            snap = await self._registry.get(sandbox_id)
            if snap is not None and snap.phase in (
                "spawning", "starting", "running"
            ):
                was_holding_slot = True
        await asyncio.to_thread(self._spawner.stop, container_name)
        if sandbox_id:
            await self._registry.set_phase(sandbox_id, "expired")
        if was_holding_slot:
            self._slot_sem.release()

    async def _find_idle(self, now: float) -> list[tuple[str, str]]:
        """Return (sandbox_id, container_name) for running jobs whose
        last_used_at metadata is older than IDLE_TTL_S. Uses the
        registry's asyncpg pool directly — PostgresRegistry doesn't
        expose metadata-filtered lookup and adding it would leak
        sandbox concepts into the shared library."""
        pool = self._registry._pool
        if pool is None:
            return []
        cutoff = datetime.fromtimestamp(
            now - IDLE_TTL_S, tz=timezone.utc
        )
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, result "
                "FROM jobs "
                "WHERE phase = 'running' "
                "AND (metadata->>'last_used_at')::timestamptz < $1",
                cutoff,
            )
        out: list[tuple[str, str]] = []
        for row in rows:
            result = row["result"] or {}
            if isinstance(result, str):
                result = json.loads(result)
            container_name = result.get("container_name")
            if container_name:
                out.append((row["id"], container_name))
        return out
