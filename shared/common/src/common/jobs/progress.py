"""Job progress: a gauge a job advances at checkpoints, flushed to a store.

A submit-and-poll job says ``pending`` / ``processing`` / ``completed`` and
nothing in between, so a job that takes minutes looks exactly like one that
has hung. ``ProgressGauge`` is the "how far along is it" half: the job is cut
into STAGES, each worth a share of 100, and the job moves through them at
checkpoint moments::

    percent = Σ weight of the completed stages + weight × index / length

— one band per stage, ``constant + limit × index / length`` inside it. The
running stage's ``length`` is planned when the job knows it (``plan``), its
``index`` moves with every ``advance``.

The pieces:

    Stage            ``(name, title, weight)``. Weights are normalised to 100,
                     so ``10 / 85 / 5`` and ``2 / 17 / 1`` mean the same.
    ProgressGauge    one job attempt's progress, in memory, behind a
                     ``threading.Lock`` — a job's units finish concurrently
                     (and some in worker threads, ``asyncio.to_thread``), so
                     the counter is advanced under a lock, never
                     read-modify-written through the database.
    gauge.task()     a context manager for ONE unit of work worth ``steps``:
                     a ``checkpoint()`` inside it moves that unit by one (at
                     most ``steps - 1``), and leaving the block credits
                     whatever is left — so a unit that was skipped, errored or
                     short-circuited still counts in full, and ``length`` is
                     always reached.
    gauge.running()  ``async with`` around the job: makes the gauge CURRENT
                     (a ContextVar, so the module helpers below find it from
                     anywhere in the job — child tasks and ``to_thread``
                     workers copy the context), flushes the snapshot and the
                     new events to the store every ``flush_interval`` while
                     anything changed, and on exit writes the terminal state
                     (``done`` at 100, or ``failed``) in one final flush.
    checkpoint() / plan() / task()
                     the module helpers a job's code calls. They resolve the
                     current gauge and are no-ops when there is none, so a
                     library function can carry checkpoints and still be
                     called directly (tests, scripts) with no gauge at all.
    ProgressStore    where flushes go — ``common.jobs.progress_postgres.
                     PostgresProgressStore`` keeps the snapshot in the job
                     row's ``metadata.progress`` and one history row per event.
    mark_queued() / next_attempt()
                     never-raising wrappers for the two store calls a queue
                     makes outside a running gauge.

**Logging is a checkpoint, not a side effect.** ``checkpoint(label,
logger=…)`` advances the gauge AND writes the log line —
``[progress 43%] units 7/12: <label>`` — through the logger given (or the
gauge's own). A plain ``logger.info`` never moves the gauge: progress is
something the job declares, not something inferred from its logs.

**Rules the gauge keeps, whatever the caller does:**

  * ``percent`` never decreases, and stays below 100 until the job is done —
    100 means finished, not "the last stage's counter filled up".
  * Entering a later stage (``plan`` or ``advance`` on it) marks every earlier
    stage complete — a job that skips a stage (a reference with nothing to
    evaluate) still lands on the right number.
  * An unknown stage name is logged ONCE with a warning and otherwise
    ignored, so a pipeline shared by several job types can name a stage one
    of them does not declare.
  * **Progress can never fail a job.** A store that raises is logged and
    swallowed (the events stay buffered for the next flush, up to a cap);
    the module helpers swallow their own errors too. Same rule as the
    classifier's ``llm.usage.record_call``.
  * A cancelled job (a shutdown mid-run) skips the final flush: the queue
    requeues it, and the next run is a new ATTEMPT with its own history.

Every change appends one event ``{seq, at, stage, label, index, length,
percent, state}`` to an in-memory buffer; ``seq`` counts from 1 within an
attempt (attempt 0 holds the one ``queued`` event a store writes at enqueue).
Events are buffered only when there is a store to send them to.

Process flow position: a leaf utility, stdlib only; imports nothing from this
package. ``common.jobs.progress_postgres`` implements the store and
``common.jobs.router.build_progress_router`` serves what it wrote.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Iterator, Optional, Protocol, Sequence

_log = logging.getLogger("common.jobs.progress")

# Snapshot / event states.
QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"

# While a job runs, percent is held just under 100: a stage whose counter has
# filled is not a finished job (the terminal write, the result, are still to
# come), and a poller that sees 100 should be able to trust it.
_RUNNING_CEILING = 99.9

# Events kept in memory while the store keeps failing. Past it the OLDEST are
# dropped — the snapshot is always current, the history just gets a gap.
MAX_BUFFERED_EVENTS = 5000

# A failure label is a sentence, not a traceback.
_LABEL_MAX_CHARS = 300


def now_iso() -> str:
    """UTC with microseconds — every ``at`` / ``updated_at`` the gauge writes."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class Stage:
    """One band of a job's progress.

    Attributes:
        name:   What code calls it (``plan("units", …)``).
        title:  What a person reads (``"Evaluating criteria"``).
        weight: Its share, relative to the other stages' — normalised to 100.
    """

    name: str
    title: str
    weight: float


def _normalised(stages: Sequence[Stage]) -> list[tuple[Stage, float]]:
    """Each stage with its weight scaled so the weights add up to 100."""
    if not stages:
        raise ValueError("a gauge needs at least one stage")
    names = [s.name for s in stages]
    if len(set(names)) != len(names):
        raise ValueError(f"stage names must be unique, got {names}")
    if any(s.weight < 0 for s in stages):
        raise ValueError("stage weights must be >= 0")
    total = float(sum(s.weight for s in stages))
    if total <= 0:
        raise ValueError("stage weights must add up to more than 0")
    return [(s, 100.0 * s.weight / total) for s in stages]


def queued_snapshot(
    stages: Sequence[Stage] = (), *, attempt: int = 0, label: str = "queued"
) -> dict[str, Any]:
    """The snapshot of a job that is waiting for a worker — 0%, no stage.

    Same keys as :meth:`ProgressGauge.snapshot`, so a poller reads one shape
    whatever the state. ``stages`` may be empty when the writer does not know
    the job's stages (a bulk requeue).
    """
    listed = _normalised(stages) if stages else []
    return {
        "state": QUEUED,
        "percent": 0.0,
        "stage": None,
        "stage_title": None,
        "label": label,
        "index": 0,
        "length": 0,
        "attempt": attempt,
        "seq": 0,
        "updated_at": now_iso(),
        "stages": [
            {"name": s.name, "title": s.title, "weight": round(w, 3), "index": 0, "length": 0}
            for s, w in listed
        ],
    }


class ProgressStore(Protocol):
    """Where a gauge's flushes go. ``PostgresProgressStore`` is the one
    implementation; a test passes a list-backed fake."""

    async def flush(
        self, job_id: str, snapshot: dict[str, Any], events: list[dict[str, Any]]
    ) -> None:
        """Store ``snapshot`` as the job's current progress and append
        ``events`` to its history — together, or not at all."""
        ...

    async def queued(self, job_id: str, *, stages: Sequence[Stage] = ()) -> None:
        """Write the ``queued`` snapshot (attempt 0) and its event."""
        ...

    async def next_attempt(self, job_id: str) -> int:
        """The attempt number the next run of ``job_id`` should use."""
        ...


class _StageState:
    """A stage's mutable counters. Only touched under the gauge's lock."""

    __slots__ = ("stage", "weight", "index", "length", "complete")

    def __init__(self, stage: Stage, weight: float) -> None:
        self.stage = stage
        self.weight = weight
        self.index = 0
        self.length = 0
        self.complete = False

    def fraction(self) -> float:
        if self.complete:
            return 1.0
        if self.length <= 0:
            return 0.0
        return min(1.0, self.index / self.length)


class Unit:
    """One unit of work inside :meth:`ProgressGauge.task` — see there."""

    __slots__ = ("gauge", "position", "steps", "label", "done", "level", "logger")

    def __init__(
        self,
        gauge: "ProgressGauge",
        position: int,
        steps: int,
        label: Optional[str],
        logger: Optional[logging.Logger],
        level: int,
    ) -> None:
        self.gauge = gauge
        self.position = position
        self.steps = steps
        self.label = label
        self.logger = logger
        self.level = level
        self.done = 0

    @property
    def stage(self) -> str:
        return self.gauge._stages[self.position].stage.name

    def step(self, label: Optional[str], logger: Optional[logging.Logger], level: int) -> None:
        """A checkpoint inside the unit: one step, capped at ``steps - 1`` —
        the last step belongs to the unit's exit. Past the cap it is a label
        update (and a log line), not a move."""
        with self.gauge._lock:
            n = 0
            if self.done < self.steps - 1:
                self.done += 1
                n = 1
            event = self.gauge._advance_locked(self.position, n, label)
        self.gauge._log_event(event, logger or self.logger, level)

    def finish(self) -> None:
        """Credit what is left of the unit — every step, however it ended."""
        with self.gauge._lock:
            remaining = max(0, self.steps - self.done)
            self.done = self.steps
            event = self.gauge._advance_locked(self.position, remaining, self.label)
        self.gauge._log_event(event, self.logger, self.level)


class ProgressGauge:
    """One attempt of one job's progress.

    Args:
        job_id:         The job the flushes are written against.
        stages:         The job type's stages, in order.
        store:          Where flushes go; ``None`` keeps everything in memory
                        (no events are buffered then).
        logger:         Default logger for checkpoint lines; defaults to
                        ``common.jobs.progress``.
        flush_interval: Seconds between flushes while ``running()`` and
                        something changed. Floor 0.01.
        attempt:        Which run of the job this is (``next_attempt``);
                        written on the snapshot and every history row.

    Every public method is thread-safe. ``flush()`` is meant to have one
    caller at a time — the flusher while running, then the final flush after
    the flusher has stopped.
    """

    def __init__(
        self,
        job_id: str,
        stages: Sequence[Stage],
        *,
        store: Optional[ProgressStore] = None,
        logger: Optional[logging.Logger] = None,
        flush_interval: float = 1.0,
        attempt: int = 1,
    ) -> None:
        self.job_id = job_id
        self.store = store
        self.logger = logger or _log
        self.flush_interval = max(0.01, float(flush_interval))
        self.attempt = int(attempt)
        self._stages = [_StageState(s, w) for s, w in _normalised(stages)]
        self._position = {st.stage.name: i for i, st in enumerate(self._stages)}
        self._lock = threading.Lock()
        self._current = -1  # the furthest stage entered; -1 before any
        self._percent = 0.0
        self._state = RUNNING
        self._label: Optional[str] = None
        self._seq = 0
        self._updated_at = now_iso()
        self._events: list[dict[str, Any]] = []
        self._dirty = False
        self._warned: set[str] = set()
        self._store_failing = False
        self._dropped = 0

    # ── Reading ───────────────────────────────────────────────────────────
    @property
    def percent(self) -> float:
        with self._lock:
            return self._percent

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def snapshot(self) -> dict[str, Any]:
        """The current progress — what a poller reads as ``metadata.progress``::

            {state, percent, stage, stage_title, label, index, length,
             attempt, seq, updated_at,
             stages: [{name, title, weight, index, length}]}

        ``stage`` / ``index`` / ``length`` are the furthest stage entered
        (``None`` / 0 / 0 before any).
        """
        with self._lock:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> dict[str, Any]:
        current = self._stages[self._current] if self._current >= 0 else None
        return {
            "state": self._state,
            "percent": round(self._percent, 1),
            "stage": current.stage.name if current else None,
            "stage_title": current.stage.title if current else None,
            "label": self._label,
            "index": current.index if current else 0,
            "length": current.length if current else 0,
            "attempt": self.attempt,
            "seq": self._seq,
            "updated_at": self._updated_at,
            "stages": [
                {
                    "name": st.stage.name,
                    "title": st.stage.title,
                    "weight": round(st.weight, 3),
                    "index": st.index,
                    "length": st.length,
                }
                for st in self._stages
            ],
        }

    # ── Moving ────────────────────────────────────────────────────────────
    def plan(self, stage: str, length: int) -> None:
        """Set ``stage``'s denominator — and enter it, completing every
        earlier stage. Unknown stages are ignored (warned once)."""
        with self._lock:
            position = self._lookup_locked(stage)
            if position is None:
                return
            self._enter_locked(position)
            self._stages[position].length = max(0, int(length))
            self._recompute_locked()
            self._touch_locked()

    def advance(self, stage: str, n: int = 1, label: Optional[str] = None) -> Optional[dict[str, Any]]:
        """Move ``stage`` on by ``n`` (entering it, completing earlier ones)
        and return the event recorded, or ``None`` for an unknown stage.
        Writes no log line — :meth:`checkpoint` is advance plus the log."""
        with self._lock:
            position = self._lookup_locked(stage)
            if position is None:
                return None
            return self._advance_locked(position, n, label)

    def checkpoint(
        self,
        label: str,
        *,
        stage: Optional[str] = None,
        n: int = 1,
        logger: Optional[logging.Logger] = None,
        level: int = logging.INFO,
    ) -> None:
        """Advance and log ``[progress 43%] units 7/12: <label>``.

        Inside a :meth:`task` of this gauge (and with no other ``stage``
        named) it moves that unit — one step, capped at ``steps - 1``.
        Otherwise it moves ``stage`` by ``n`` (``n=0`` enters a stage and
        updates the label without counting); with neither, the stage the job
        is in.
        """
        unit = _current_unit.get()
        if unit is not None and unit.gauge is self and stage in (None, unit.stage):
            unit.step(label, logger, level)
            return
        with self._lock:
            if stage is None:
                position = self._current if self._current >= 0 else 0
            else:
                position = self._lookup_locked(stage)
                if position is None:
                    return
            event = self._advance_locked(position, n, label)
        self._log_event(event, logger, level)

    @contextmanager
    def task(
        self,
        stage: str,
        steps: int = 1,
        label: Optional[str] = None,
        *,
        logger: Optional[logging.Logger] = None,
        level: int = logging.INFO,
    ) -> Iterator[Optional[Unit]]:
        """One unit of work worth ``steps`` in ``stage``.

        The block's exit — normal, an exception, an early return — credits
        every step the unit's checkpoints did not, and logs the unit's
        ``label``. Exceptions pass through untouched. Yields the
        :class:`Unit`, or ``None`` for an unknown stage (then nothing is
        counted). Works in a worker thread as well as in a task: the current
        unit is a ContextVar, which ``asyncio.to_thread`` copies.
        """
        with self._lock:
            position = self._lookup_locked(stage)
        if position is None:
            yield None
            return
        unit = Unit(self, position, max(1, int(steps)), label, logger, level)
        token = _current_unit.set(unit)
        try:
            yield unit
        finally:
            _current_unit.reset(token)
            try:
                unit.finish()
            except Exception as exc:  # noqa: BLE001 — progress never fails a job
                _log.warning("progress: could not credit a unit of job %s: %s", self.job_id, exc)

    # ── Running + flushing ────────────────────────────────────────────────
    @asynccontextmanager
    async def running(self) -> AsyncIterator["ProgressGauge"]:
        """Make this the current gauge for the block and flush it while it runs.

        On a normal exit the state becomes ``done`` (100%), on an exception
        ``failed`` (the exception propagates), and either is written in one
        final, forced flush before this returns — so a terminal snapshot
        always lands before whatever the caller writes next (the job's
        result). A cancel stops the flusher and skips the final flush: the
        requeued run is a new attempt. Store errors are logged, never raised.
        """
        token = current_gauge.set(self)
        with self._lock:
            self._state = RUNNING
            self._label = "started"
            self._touch_locked()
            self._record_locked(None)
        stop = asyncio.Event()
        flusher = (
            asyncio.create_task(self._flush_loop(stop), name=f"progress-{self.job_id}")
            if self.store is not None else None
        )
        outcome: Optional[str] = None
        error: Optional[BaseException] = None
        try:
            yield self
            outcome = DONE
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            outcome, error = FAILED, exc
            raise
        finally:
            current_gauge.reset(token)
            if flusher is not None:
                if outcome is None:
                    flusher.cancel()
                else:
                    stop.set()  # let a flush in progress finish
                # wait() never raises the flusher's own exception or cancel.
                await asyncio.wait({flusher})
            if outcome is not None:
                self._finish(outcome, error)
                await self.flush(force=True)

    async def flush(self, *, force: bool = False) -> bool:
        """Send the snapshot and the buffered events to the store, if anything
        changed (or ``force``). Returns whether a write succeeded. Never
        raises, except a cancel — the events go back in the buffer first."""
        if self.store is None:
            return False
        with self._lock:
            if not (self._dirty or force):
                return False
            snapshot = self._snapshot_locked()
            events, self._events = self._events, []
            self._dirty = False
        try:
            await self.store.flush(self.job_id, snapshot, events)
        except asyncio.CancelledError:
            self._rebuffer(events)
            raise
        except Exception as exc:  # noqa: BLE001 — progress never fails a job
            self._rebuffer(events)
            if not self._store_failing:
                self.logger.warning(
                    "progress: could not store progress for job %s (keeps retrying): %s",
                    self.job_id, exc,
                )
            self._store_failing = True
            return False
        if self._store_failing:
            self.logger.info("progress: storing progress for job %s again", self.job_id)
            self._store_failing = False
        return True

    async def _flush_loop(self, stop: asyncio.Event) -> None:
        """Flush every ``flush_interval`` while dirty, until ``stop`` is set.
        The final flush is ``running()``'s, not this loop's."""
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.flush_interval)
            except asyncio.TimeoutError:
                pass
            if stop.is_set():
                return
            await self.flush()

    def _rebuffer(self, events: list[dict[str, Any]]) -> None:
        with self._lock:
            self._events = events + self._events
            over = len(self._events) - MAX_BUFFERED_EVENTS
            if over > 0:
                del self._events[:over]
                self._dropped += over
            self._dirty = True

    def _finish(self, outcome: str, error: Optional[BaseException]) -> None:
        with self._lock:
            self._state = outcome
            if outcome == DONE:
                for st in self._stages:
                    st.complete = True
                self._current = len(self._stages) - 1
                self._percent = 100.0
                self._label = "done"
            else:
                text = f"failed: {type(error).__name__}: {error}" if error else "failed"
                self._label = text[:_LABEL_MAX_CHARS]
            self._touch_locked()
            event = self._record_locked(self._stages[self._current] if self._current >= 0 else None)
        self._log_event(event, None, logging.INFO if outcome == DONE else logging.WARNING)

    # ── Internals (call with the lock held) ───────────────────────────────
    def _lookup_locked(self, stage: str) -> Optional[int]:
        position = self._position.get(stage)
        if position is None and stage not in self._warned:
            self._warned.add(stage)
            self.logger.warning(
                "progress: job %s has no stage %r (stages: %s) — ignoring it",
                self.job_id, stage, ", ".join(self._position),
            )
        return position

    def _enter_locked(self, position: int) -> None:
        if position > self._current:
            for st in self._stages[:position]:
                st.complete = True
            self._current = position

    def _recompute_locked(self) -> None:
        raw = sum(st.weight * st.fraction() for st in self._stages)
        if self._state == RUNNING:
            raw = min(raw, _RUNNING_CEILING)
        self._percent = max(self._percent, raw)

    def _touch_locked(self) -> None:
        self._updated_at = now_iso()
        self._dirty = True

    def _advance_locked(self, position: int, n: int, label: Optional[str]) -> dict[str, Any]:
        self._enter_locked(position)
        st = self._stages[position]
        st.index += max(0, int(n))
        if label is not None:
            self._label = label
        self._recompute_locked()
        self._touch_locked()
        return self._record_locked(st)

    def _record_locked(self, st: Optional[_StageState]) -> dict[str, Any]:
        """Append one event for the current state; return it."""
        self._seq += 1
        event = {
            "seq": self._seq,
            "at": self._updated_at,
            "stage": st.stage.name if st else None,
            "label": self._label,
            "index": st.index if st else 0,
            "length": st.length if st else 0,
            "percent": round(self._percent, 1),
            "state": self._state,
        }
        if self.store is not None:
            self._events.append(event)
            over = len(self._events) - MAX_BUFFERED_EVENTS
            if over > 0:
                del self._events[:over]
                self._dropped += over
        return event

    def _log_event(
        self, event: dict[str, Any], logger: Optional[logging.Logger], level: int
    ) -> None:
        """``[progress 43%] units 7/12: <label>`` (no counts for a stage with
        no planned length)."""
        where = event["stage"] or "job"
        if event["length"]:
            where = f"{where} {event['index']}/{event['length']}"
        (logger or self.logger).log(
            level, "%s", f"[progress {int(event['percent'])}%] {where}: {event['label']}"
        )


# ---------------------------------------------------------------------------
# The current gauge, and the helpers job code calls
# ---------------------------------------------------------------------------
# Same pattern as the classifier's llm.usage context vars: set once at the top
# of a job (``running()``), copied into every child task and to_thread worker,
# so code four modules down can checkpoint without a gauge being threaded
# through every signature — and two concurrent jobs never see each other's.

current_gauge: ContextVar[Optional[ProgressGauge]] = ContextVar(
    "common_jobs_progress_gauge", default=None
)
_current_unit: ContextVar[Optional[Unit]] = ContextVar(
    "common_jobs_progress_unit", default=None
)


def current() -> Optional[ProgressGauge]:
    """The gauge of the job this code is running in, or ``None``."""
    return current_gauge.get()


def checkpoint(
    label: str,
    *,
    stage: Optional[str] = None,
    n: int = 1,
    logger: Optional[logging.Logger] = None,
    level: int = logging.INFO,
) -> None:
    """:meth:`ProgressGauge.checkpoint` on the current gauge; a no-op without
    one. Never raises."""
    gauge = current_gauge.get()
    if gauge is None:
        return
    try:
        gauge.checkpoint(label, stage=stage, n=n, logger=logger, level=level)
    except Exception as exc:  # noqa: BLE001 — progress never fails a job
        _log.warning("progress: checkpoint %r failed: %s", label, exc)


def plan(stage: str, length: int) -> None:
    """:meth:`ProgressGauge.plan` on the current gauge; a no-op without one.
    Never raises."""
    gauge = current_gauge.get()
    if gauge is None:
        return
    try:
        gauge.plan(stage, length)
    except Exception as exc:  # noqa: BLE001
        _log.warning("progress: plan(%r, %r) failed: %s", stage, length, exc)


@contextmanager
def task(
    stage: str,
    steps: int = 1,
    label: Optional[str] = None,
    *,
    logger: Optional[logging.Logger] = None,
    level: int = logging.INFO,
) -> Iterator[Optional[Unit]]:
    """:meth:`ProgressGauge.task` on the current gauge; yields ``None`` and
    counts nothing without one."""
    gauge = current_gauge.get()
    if gauge is None:
        yield None
        return
    with gauge.task(stage, steps, label, logger=logger, level=level) as unit:
        yield unit


async def mark_queued(
    store: Optional[ProgressStore],
    job_id: str,
    *,
    stages: Sequence[Stage] = (),
    logger: Optional[logging.Logger] = None,
) -> None:
    """``store.queued(job_id)``, logged and swallowed on failure — a queue
    must be able to enqueue whether or not progress can be written."""
    if store is None:
        return
    try:
        await store.queued(job_id, stages=stages)
    except Exception as exc:  # noqa: BLE001
        (logger or _log).warning("progress: could not mark job %s queued: %s", job_id, exc)


async def next_attempt(
    store: Optional[ProgressStore],
    job_id: str,
    *,
    logger: Optional[logging.Logger] = None,
) -> int:
    """``store.next_attempt(job_id)``, or 1 when there is no store or it
    fails (logged) — a run must start whether or not history can be read."""
    if store is None:
        return 1
    try:
        return max(1, int(await store.next_attempt(job_id)))
    except Exception as exc:  # noqa: BLE001
        (logger or _log).warning(
            "progress: could not read the attempt of job %s (using 1): %s", job_id, exc
        )
        return 1
