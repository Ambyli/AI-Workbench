"""
WorkerPool — N asyncio workers draining a registry-backed job queue.

Pairs with the persistent registries (``SqliteRegistry``, ``PostgresRegistry``):
producers ``register(..., "pending")`` rows, and this pool runs ``concurrency``
tasks that each loop on ``registry.claim_next()``. Because the claim is atomic
in the registry, the number of workers IS the hard cap on simultaneous jobs
and no two workers ever take the same row.

Handler contract::

    async def handler(job: JobBase) -> dict:
        ...  # return the result dict, or raise to fail the job

The pool owns the terminal transition: a returned dict goes to
``registry.set_result`` (phase → "completed"), an exception goes to
``registry.set_error(str(exc))`` (phase → "failed"). Handlers never touch the
phase themselves. An optional ``on_finish(job, phase, elapsed, error)`` sync
callback fires after every job for metrics.

Wake-up: ``notify()`` wakes idle workers immediately after an enqueue in the
same process. Rows written by another process (or requeued by a crash) are
found by the ``poll_interval`` fallback — so multi-process deployments work,
just with up to ``poll_interval`` of extra latency.

Crash recovery: ``recover()`` bulk-moves rows left in ``claim_to`` (and any
extra ``phases``) back to ``claim_from`` so they run again. Call it once at
startup before ``start()``. Whether the handler can actually redo the job
depends on the consumer having persisted the job's input — see
``common.jobs.payloads.FilePayloadStore``.

Shutdown: ``stop()`` cancels the worker tasks. A job mid-handler is left in
``claim_to`` in the DB (the cancel interrupts the handler before it reaches
set_result/set_error) and is picked up by the next start's ``recover()``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Iterable, Optional, Protocol

from .model import JobBase


class QueueRegistry(Protocol):
    """The subset of the registry API the pool needs. Both persistent
    backends satisfy it."""

    async def claim_next(self, from_phase: str, to_phase: str) -> Optional[JobBase]: ...
    async def reset_phase(self, from_phase: str, to_phase: str) -> int: ...
    async def set_result(self, job_id: str, result: dict[str, Any], phase: str = ...) -> None: ...
    async def set_error(self, job_id: str, error: str, phase: str = ...) -> None: ...


Handler = Callable[[JobBase], Awaitable[dict[str, Any]]]
FinishHook = Callable[[JobBase, str, float, Optional[str]], None]


class WorkerPool:
    """Run ``concurrency`` claim-and-handle loops against ``registry``.

    Args:
        registry: A persistent registry exposing ``claim_next`` / ``reset_phase``
            / ``set_result`` / ``set_error``.
        handler: ``async (JobBase) -> dict``. Return = success, raise = failure.
        concurrency: Number of worker tasks, i.e. max jobs in flight. Clamped to >= 1.
        poll_interval: Seconds an idle worker sleeps before re-checking the
            registry when nothing has called ``notify()``.
        claim_from / claim_to: Phases used by ``claim_next``. Defaults match
            the registries' own defaults ("pending" → "processing").
        on_finish: Optional sync callback ``(job, phase, elapsed_s, error)``
            invoked after each job is persisted, for metrics. Exceptions in it
            are logged and swallowed.
        name: Prefix for task names and log lines.
        logger: Defaults to ``logging.getLogger("common.jobs.worker")``.
    """

    def __init__(
        self,
        registry: QueueRegistry,
        handler: Handler,
        *,
        concurrency: int = 1,
        poll_interval: float = 1.0,
        claim_from: str = "pending",
        claim_to: str = "processing",
        on_finish: Optional[FinishHook] = None,
        name: str = "worker",
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.registry = registry
        self.handler = handler
        self.concurrency = max(1, int(concurrency))
        self.poll_interval = float(poll_interval)
        self.claim_from = claim_from
        self.claim_to = claim_to
        self.on_finish = on_finish
        self.name = name
        self.log = logger or logging.getLogger("common.jobs.worker")
        self._tasks: list[asyncio.Task] = []
        self._wake: Optional[asyncio.Event] = None
        self._in_flight = 0

    # ── Introspection ─────────────────────────────────────────────────────
    @property
    def in_flight(self) -> int:
        """Jobs currently inside the handler (always <= ``concurrency``)."""
        return self._in_flight

    @property
    def running(self) -> bool:
        return any(not t.done() for t in self._tasks)

    # ── Lifecycle ─────────────────────────────────────────────────────────
    async def recover(self, phases: Iterable[str] = ()) -> int:
        """Requeue rows left in ``claim_to`` (plus any extra ``phases``) back
        to ``claim_from``. Returns how many rows moved. Call before ``start()``."""
        moved = await self.registry.reset_phase(self.claim_to, self.claim_from)
        for phase in phases:
            moved += await self.registry.reset_phase(phase, self.claim_from)
        if moved:
            self.log.info("%s: recovered %d interrupted job(s) → %s", self.name, moved, self.claim_from)
        return moved

    def start(self) -> None:
        """Spawn the worker tasks on the running loop. Idempotent while running."""
        if self.running:
            return
        self._wake = asyncio.Event()
        self._tasks = [
            asyncio.create_task(self._worker(i), name=f"{self.name}-{i}")
            for i in range(self.concurrency)
        ]
        self.log.info("%s: started %d worker task(s)", self.name, self.concurrency)

    async def stop(self) -> None:
        """Cancel all worker tasks and wait for them to unwind."""
        for t in self._tasks:
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        self.log.info("%s: stopped", self.name)

    def notify(self) -> None:
        """Wake idle workers — call right after a producer enqueues a job."""
        if self._wake is not None:
            self._wake.set()

    # ── Internals ─────────────────────────────────────────────────────────
    async def _worker(self, idx: int) -> None:
        assert self._wake is not None
        while True:
            try:
                job = await self.registry.claim_next(self.claim_from, self.claim_to)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A transient DB error must not kill the worker — back off and retry.
                self.log.error("%s[%d]: claim failed: %s", self.name, idx, exc)
                await asyncio.sleep(self.poll_interval)
                continue

            if job is None:
                # Nothing waiting: sleep until a producer wakes us or the poll fires.
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=self.poll_interval)
                except asyncio.TimeoutError:
                    pass
                self._wake.clear()
                continue

            await self._handle(job, idx)

    async def _handle(self, job: JobBase, idx: int) -> None:
        """Run one claimed job to a terminal phase. Never raises (except cancel)."""
        self.log.info("%s[%d]: claimed job_id=%s", self.name, idx, job.job_id)
        self._in_flight += 1
        start = time.monotonic()
        phase, error = "completed", None
        try:
            result = await self.handler(job)
            await self.registry.set_result(job.job_id, result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            phase, error = "failed", str(exc)
            try:
                await self.registry.set_error(job.job_id, error)
            except Exception as db_exc:  # pragma: no cover — DB unavailable
                self.log.error("%s[%d]: could not record failure for job_id=%s: %s",
                               self.name, idx, job.job_id, db_exc)
        finally:
            self._in_flight -= 1
        elapsed = time.monotonic() - start
        if phase == "completed":
            self.log.info("%s[%d]: job_id=%s completed in %.2fs", self.name, idx, job.job_id, elapsed)
        else:
            self.log.error("%s[%d]: job_id=%s failed after %.2fs: %s",
                           self.name, idx, job.job_id, elapsed, error)
        if self.on_finish is not None:
            try:
                self.on_finish(job, phase, elapsed, error)
            except Exception as hook_exc:
                self.log.warning("%s: on_finish hook raised: %s", self.name, hook_exc)
