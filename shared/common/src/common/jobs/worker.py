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

Cancel-aware mode (opt-in: ``cancel_check_interval``). The registry's
``cancel()`` only flips a row's phase; this is what makes it stop the work.
The handler then runs as a CHILD task, and while it runs the worker re-reads
the row every ``cancel_check_interval`` seconds. A row whose phase is
``cancel_phase`` ("cancelled"), or a row that is gone (``DELETE /jobs/{id}``),
gets its child cancelled — the handler sees a ``CancelledError`` at its next
await, which also drops whatever HTTP request or semaphore it held — and the
worker waits for the child to unwind, writes nothing (the row already says
what happened), and records the outcome ``"cancelled"``:
``on_finish(job, "cancelled", elapsed, None)``, then
``on_cancel(job, gone)`` for the consumer's cleanup. The terminal writes are
guarded (``from_phases=(claim_to,)``), so a handler that finishes just after
a cancel cannot write its result over it: the write misses, the row is
re-read, and a cancelled or deleted row makes the outcome ``"cancelled"``
too. A handler that cancels ITSELF (raises ``CancelledError`` with no cancel
from the pool) is a failure — ``"handler was cancelled"`` — never a dead
worker. A read that fails is logged and retried next interval; it never
cancels a job. With ``cancel_check_interval=None`` (the default) none of this
exists and the pool is exactly what it was: the handler runs inside the
worker task and its writes are unguarded.

Why the cleanup runs in the worker and not in the handler: ``on_cancel`` is
called by the worker task after the child has ENDED. A handler that cleaned
up inside its own ``except CancelledError`` would run that cleanup in a task
that has been cancelled once — on Python 3.11 such a task still counts the
cancel (``Task.cancelling()``), and an ``asyncio.timeout`` inside the cleanup
then turns its expiry into a ``CancelledError``. The worker task was never
cancelled, so the cleanup runs in an ordinary task, and the handler needs no
"shutdown or user cancel?" branch: to the handler, both are a
``CancelledError``.

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
In cancel-aware mode the worker cancels its child, waits for it to unwind
and re-raises — the row stays in ``claim_to`` exactly the same way, and
neither ``on_finish`` nor ``on_cancel`` runs (a shutdown is not a cancel).
A registry whose cleanup raises over the cancel (so ``claim_next`` — or the
cancel check's ``get`` — raises an ordinary exception instead) still stops: a
worker whose read fails while a cancel is pending re-raises it rather than
backing off and retrying.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
import time
from typing import Any, Awaitable, Callable, Iterable, Optional, Protocol

from .model import JobBase


class QueueRegistry(Protocol):
    """The subset of the registry API the pool needs. Both persistent
    backends satisfy it. ``get`` and the ``from_phases`` keywords are only
    used in cancel-aware mode — a plain pool calls ``set_result`` /
    ``set_error`` without them, as it always did."""

    async def claim_next(self, from_phase: str, to_phase: str) -> Optional[JobBase]: ...
    async def reset_phase(self, from_phase: str, to_phase: str) -> int: ...
    async def get(self, job_id: str) -> Optional[JobBase]: ...
    async def set_result(
        self, job_id: str, result: dict[str, Any], phase: str = ...,
        *, from_phases: Optional[Iterable[str]] = ...,
    ) -> Optional[bool]: ...
    async def set_error(
        self, job_id: str, error: str, phase: str = ...,
        *, from_phases: Optional[Iterable[str]] = ...,
    ) -> Optional[bool]: ...


Handler = Callable[[JobBase], Awaitable[dict[str, Any]]]
FinishHook = Callable[[JobBase, str, float, Optional[str]], None]
# ``(job, gone)`` — sync, or async (awaited). ``gone``: the row was deleted
# rather than cancelled.
CancelHook = Callable[[JobBase, bool], Optional[Awaitable[Any]]]

# The outcome a cancel-aware worker records for a job stopped by a cancel or
# a delete (``on_finish``'s ``phase``).
CANCELLED = "cancelled"


class WorkerPool:
    """Run ``concurrency`` claim-and-handle loops against ``registry``.

    Args:
        registry: A persistent registry exposing ``claim_next`` / ``reset_phase``
            / ``set_result`` / ``set_error`` (and ``get`` plus the
            ``from_phases`` guard, for ``cancel_check_interval``).
        handler: ``async (JobBase) -> dict``. Return = success, raise = failure.
        concurrency: Number of worker tasks, i.e. max jobs in flight. Clamped to >= 1.
        poll_interval: Seconds an idle worker sleeps before re-checking the
            registry when nothing has called ``notify()``.
        claim_from / claim_to: Phases used by ``claim_next``. Defaults match
            the registries' own defaults ("pending" → "processing").
        on_finish: Optional sync callback ``(job, phase, elapsed_s, error)``
            invoked after each job is persisted, for metrics. Exceptions in it
            are logged and swallowed. ``phase`` is "completed", "failed" or —
            cancel-aware only — "cancelled".
        cancel_check_interval: Seconds between the cancel checks of a running
            job (one ``registry.get`` each), or None (the default) for a pool
            that never looks — byte for byte the pool before cancel existed.
            Must be a finite number > 0 when given. See the module docstring.
        cancel_phase: The phase ``registry.cancel()`` writes ("cancelled").
        on_cancel: Optional ``(job, gone)`` hook, sync or async, run by the
            worker AFTER the cancelled handler has unwound and after
            ``on_finish`` — the consumer's cleanup (its payload, partial side
            data). ``gone`` is True when the row was deleted rather than
            cancelled. Exceptions are logged and swallowed; a shutdown that
            arrives while it runs interrupts it.
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
        cancel_check_interval: Optional[float] = None,
        cancel_phase: str = CANCELLED,
        on_cancel: Optional[CancelHook] = None,
        name: str = "worker",
        logger: Optional[logging.Logger] = None,
    ) -> None:
        if cancel_check_interval is not None and not (
            math.isfinite(cancel_check_interval) and cancel_check_interval > 0
        ):
            raise ValueError(
                f"cancel_check_interval must be a finite number > 0 or None, "
                f"not {cancel_check_interval!r}"
            )
        self.registry = registry
        self.handler = handler
        self.concurrency = max(1, int(concurrency))
        self.poll_interval = float(poll_interval)
        self.claim_from = claim_from
        self.claim_to = claim_to
        self.on_finish = on_finish
        self.cancel_check_interval = (
            None if cancel_check_interval is None else float(cancel_check_interval)
        )
        self.cancel_phase = cancel_phase
        self.on_cancel = on_cancel
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
                if _cancel_pending():
                    # stop() cancelled us and the registry turned the cancel
                    # into an ordinary error (a cleanup that raised over it).
                    # Retrying would leave a half-cancelled task looping —
                    # one whose next wait_for never wakes, so stop() hangs.
                    raise asyncio.CancelledError() from exc
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

            if self.cancel_check_interval is None:
                await self._handle(job, idx)
            else:
                await self._handle_watched(job, idx)

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
        self._finished(job, phase, elapsed, error)

    # ── Cancel-aware mode ─────────────────────────────────────────────────
    async def _handle_watched(self, job: JobBase, idx: int) -> None:
        """``_handle`` with the cancel check: the handler runs as a child
        task the worker watches. Never raises (except a shutdown cancel)."""
        self.log.info("%s[%d]: claimed job_id=%s", self.name, idx, job.job_id)
        self._in_flight += 1
        start = time.monotonic()
        child = asyncio.create_task(self.handler(job), name=f"{self.name}-{idx}-{job.job_id}")
        try:
            try:
                stopped = await self._watch(child, job, idx)
            except asyncio.CancelledError:
                # A shutdown (stop() cancelled this worker), not a user cancel:
                # take the handler down with us and leave the row in claim_to
                # for recover() — no write, no on_finish, no on_cancel.
                child.cancel()
                await asyncio.wait({child})
                self._reap(child, job, idx)
                raise
            if stopped is None:
                phase, error, gone = await self._settle(child, job, idx)
            else:
                phase, error, gone = CANCELLED, None, stopped
        finally:
            self._in_flight -= 1
        elapsed = time.monotonic() - start
        if phase == CANCELLED:
            self.log.info("%s[%d]: job_id=%s cancelled after %.2fs (%s)", self.name, idx,
                          job.job_id, elapsed, "row deleted" if gone else "cancel requested")
        elif phase == "completed":
            self.log.info("%s[%d]: job_id=%s completed in %.2fs", self.name, idx, job.job_id, elapsed)
        else:
            self.log.error("%s[%d]: job_id=%s failed after %.2fs: %s",
                           self.name, idx, job.job_id, elapsed, error)
        self._finished(job, phase, elapsed, error)
        if phase == CANCELLED and self.on_cancel is not None:
            try:
                outcome = self.on_cancel(job, gone)
                if inspect.isawaitable(outcome):
                    await outcome
            except asyncio.CancelledError:
                raise
            except Exception as hook_exc:
                self.log.warning("%s: on_cancel hook raised for job_id=%s: %s",
                                 self.name, job.job_id, hook_exc)

    async def _watch(self, child: asyncio.Task, job: JobBase, idx: int) -> Optional[bool]:
        """Wait for ``child``, re-reading the row every interval. Returns None
        when the child ended on its own; otherwise it has cancelled the child
        and waited for it to unwind, and returns ``gone`` (the row was deleted
        rather than cancelled)."""
        assert self.cancel_check_interval is not None
        while True:
            # wait() never raises the child's exception or cancel, and a
            # cancel of THIS task (shutdown) does not cancel the child.
            done, _ = await asyncio.wait({child}, timeout=self.cancel_check_interval)
            if done:
                return None
            try:
                row = await self.registry.get(job.job_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if _cancel_pending():
                    # A shutdown the registry turned into an ordinary error:
                    # unwind as one (see _worker).
                    raise asyncio.CancelledError() from exc
                # A read error never cancels a job; the next interval retries.
                self.log.warning("%s[%d]: cancel check failed for job_id=%s: %s",
                                 self.name, idx, job.job_id, exc)
                continue
            if row is not None and row.phase != self.cancel_phase:
                continue
            # The cancel (or the delete) won. A child that finished during the
            # read is discarded too: its result never reaches the row.
            child.cancel()
            await asyncio.wait({child})
            self._reap(child, job, idx)
            return row is None

    def _reap(self, child: asyncio.Task, job: JobBase, idx: int) -> None:
        """Collect the outcome of a child this worker stopped, so asyncio
        never reports "Task exception was never retrieved" for it. A handler
        that raised something other than the cancel while unwinding is worth
        a line; its exception goes nowhere else."""
        if child.cancelled():
            return
        exc = child.exception()
        if exc is not None:
            self.log.warning("%s[%d]: job_id=%s raised while being stopped: %s",
                             self.name, idx, job.job_id, exc)

    async def _settle(
        self, child: asyncio.Task, job: JobBase, idx: int
    ) -> tuple[str, Optional[str], bool]:
        """Persist what a child that ended on its own produced, with the
        writes guarded to ``claim_to``. Returns ``(phase, error, gone)`` —
        ``phase`` ``"cancelled"`` when a guarded write missed because a cancel
        or a delete landed after the handler finished."""
        guard = (self.claim_to,)
        if child.cancelled():
            # The handler cancelled itself (nothing here asked it to): a
            # failure, never a reason for the worker to die.
            phase, error, result = "failed", "handler was cancelled", None
        elif child.exception() is not None:
            phase, error, result = "failed", str(child.exception()), None
        else:
            phase, error, result = "completed", None, child.result()
        try:
            if phase == "completed":
                stored = await self.registry.set_result(job.job_id, result, from_phases=guard)
            else:
                stored = await self.registry.set_error(job.job_id, error, from_phases=guard)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if phase != "completed":
                self.log.error("%s[%d]: could not record failure for job_id=%s: %s",
                               self.name, idx, job.job_id, exc)
                return phase, error, False
            # As in the plain path: a result that cannot be stored fails the job.
            phase, error = "failed", str(exc)
            try:
                stored = await self.registry.set_error(job.job_id, error, from_phases=guard)
            except asyncio.CancelledError:
                raise
            except Exception as db_exc:
                self.log.error("%s[%d]: could not record failure for job_id=%s: %s",
                               self.name, idx, job.job_id, db_exc)
                return phase, error, False
        if stored is not False:
            return phase, error, False
        return await self._missed(job, idx, phase, error)

    async def _missed(
        self, job: JobBase, idx: int, phase: str, error: Optional[str]
    ) -> tuple[str, Optional[str], bool]:
        """A guarded terminal write touched no row: re-read it to say why."""
        try:
            row = await self.registry.get(job.job_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.log.error("%s[%d]: the %s write for job_id=%s was refused and the row "
                           "could not be re-read: %s", self.name, idx, phase, job.job_id, exc)
            return phase, error, False
        if row is None:
            return CANCELLED, None, True
        if row.phase == self.cancel_phase:
            return CANCELLED, None, False
        # Neither ours nor cancelled (another process reset it?): leave it.
        self.log.warning("%s[%d]: job_id=%s is %r, not %r — its %s outcome was not stored",
                         self.name, idx, job.job_id, row.phase, self.claim_to, phase)
        return phase, error, False

    def _finished(self, job: JobBase, phase: str, elapsed: float, error: Optional[str]) -> None:
        if self.on_finish is not None:
            try:
                self.on_finish(job, phase, elapsed, error)
            except Exception as hook_exc:
                self.log.warning("%s: on_finish hook raised: %s", self.name, hook_exc)


def _cancel_pending() -> bool:
    """Whether the current task has a cancel requested that it has not yet
    raised — i.e. something swallowed it. ``Task.cancelling()`` is 3.11+;
    on 3.10 this is always False and the pool keeps its old behaviour."""
    task = asyncio.current_task()
    cancelling = getattr(task, "cancelling", None)
    return bool(cancelling and cancelling())
