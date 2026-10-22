"""Model lanes, and one /signals request across them.

**Lanes.** Each ONNX model (pii, embed, injection) has ONE ``Lane``: one
worker thread in front of one onnxruntime session, so a model never runs
two inferences at once and the sidecar's CPU use is bounded by
SIGNALS_THREADS_* (the detector's ``asyncio.Lock`` idea, per model). A slow
model never delays another: they are separate threads. ``language``
(fastText, microseconds) has no lane and runs inline.

**Jobs.** A request becomes at most one ``TextJob`` per task: the texts the
feature cache did not have, in the order the caller listed them (newest
first -- route-policy orders them). A job works in steps -- one token window
(PII, injection) or one text (embed) -- and the worker looks at the clock
between steps:

* a job that has not **started** by its deadline is ``skipped`` and never
  run late. This is the lesson of the plan's burst test (§ 2.5): jobs that
  ran late built a backlog without bound, and then every task, even the
  microsecond language one, missed its deadline;
* a running non-PII job stops between texts at its deadline (mid-text too,
  when another request is waiting for the model);
* a running PII job is the one exception: it finishes its texts after the
  response and caches them, so the next call -- the same conversation one
  turn later -- sees its whole history scanned. Past its deadline it yields
  the model to any waiting request between two windows ("parked" in the
  lane's background queue), and a lane keeps at most ONE parked PII job
  whose request has its answer: a newer one replaces it, so the backlog is
  bounded;
* waiting requests are served newest first: under a burst the request most
  likely to still meet its deadline goes first, and the stale ones are
  skipped, not run;
* a request with NO deadline (``deadline_ms: null``: route-policy's POST
  /evaluate, the bench) is *patient*: its jobs queue in the background, so
  they run only while no request with a deadline waits, and hand the model
  to one between two steps -- parked, then resumed where they stopped --
  until every text is done (the wire's MAX_DEADLINE_MS is their only
  bound). A parked patient job is never replaced while its request waits:
  each one is bounded by its own request;
* background work -- a parked PII job, a patient job, an example set being
  embedded (``WarmJob``) -- runs only while no request waits, oldest first,
  and hands the model back between two steps.

**One request** (``Engine.run``): cache lookups on the event loop, one job
per task submitted to its lane, language inline, then an ``asyncio.wait``
until the deadline. At the deadline each job is snapshotted (``take``): the
texts it finished, a PII text it was still scanning (``complete: false``),
and whether it started at all. The task's ``common.signals.TaskResult``
follows from that: ``ok`` | ``partial`` | ``deadline`` | ``skipped`` |
``warming`` | ``error`` | ``unavailable``, its ms since the request
arrived, its per-text features (the wire's typed ``PiiFeatures`` ...,
which validate themselves), and for ``error`` / ``unavailable`` the
exception type, or ``loading``. A text that fails costs only that text:
the task is ``partial`` with the first failure's type in ``error`` while
any other text has features, and ``error`` only when none has.

Nothing here logs or keeps text: a job holds its texts until it ends, the
cache keeps only digests and features, and an exception is reduced to its
type before it is logged or counted.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Deque, Dict, Generator, List, Mapping, Optional, Sequence, Tuple

from common.signals import (MAX_K, EntityStat, Example, InjectionFeatures, LanguageFeatures, NearestFeatures,
                            Neighbour, PiiFeatures, RouteShare, TaskRequest, TaskResult, fingerprint_examples)
from common.signals.wire import MAX_DEADLINE_MS

from . import config, metrics
from .cache import READY, ExampleSet, ExampleSets, FeatureCache, text_key
from .runtime import Models
from .tasks.embed import nearest

logger = logging.getLogger("signals")

Clock = Callable[[], float]
CLOCK: Clock = time.perf_counter

FINISHED = "finished"
STOPPED = "stopped"
SKIPPED = "skipped"
PARKED = "parked"
REPLACED = "replaced"

# A nearest task's k when it sends none.
DEFAULT_K = 5

# asyncio.wait calls per request (Engine.run): an early timer needs one more.
_MAX_WAITS = 3


@dataclass(frozen=True)
class Snapshot:
    started: bool
    finished: bool
    t_finished: Optional[float]
    texts: Dict[str, Any]
    errors: Tuple[str, ...]


def _resolve(future: "asyncio.Future") -> None:
    if not future.done():
        future.set_result(None)


class TextJob:
    """One task's uncached texts for one request, on one lane.

    ``work(text)`` is the model's scan generator (it yields between windows
    and returns the text's raw features, which are cached under the text's
    key); ``present(value, cached)`` turns raw features into the wire's
    features (``common.signals``: ``PiiFeatures`` ...); ``partial(yielded)``
    does the same for a text cut mid-scan (PII only, ``late=True``).
    ``patient``: its request has no deadline (the module docstring) -- it is
    queued in the background and yields to any waiting request.
    """

    def __init__(self, *, task: str, lane: str, items: Sequence[Tuple[str, str, bytes]],
                 work: Callable[[str], Generator[Any, None, Any]],
                 present: Callable[[Any, bool], Any], cache: FeatureCache, deadline: float,
                 late: bool = False, partial: Optional[Callable[[Any], Any]] = None,
                 patient: bool = False, clock: Clock = CLOCK) -> None:
        self.task = task
        self.lane = lane
        self.items = list(items)
        self.deadline = deadline
        self.late = late
        self.patient = patient
        self._work = work
        self._present = present
        self._partial = partial
        self._cache = cache
        self._clock = clock
        self._lock = threading.Lock()
        self._cursor = 0
        self._gen: Optional[Generator[Any, None, Any]] = None
        self._compute = 0.0
        self.started = False
        self.finished = False
        self.responded = False
        self.cancelled = False
        self.t_finished: Optional[float] = None
        self.features: Dict[str, Any] = {}
        self.partial_features: Optional[Tuple[str, Any]] = None
        self.errors: List[str] = []
        self.ended = threading.Event()
        self.future: Optional["asyncio.Future"] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # -- the event loop's side ------------------------------------------------------

    def attach(self, loop: asyncio.AbstractEventLoop) -> None:
        """Create the future ``Engine.run`` waits on; resolved when the job ends."""
        self._loop = loop
        self.future = loop.create_future()

    def take(self) -> Snapshot:
        """What the job has at the response. Marks it responded: from now on a
        non-PII job stops at the next text, and one that never started never will."""
        with self._lock:
            self.responded = True
            if not self.started:
                self.cancelled = True
            texts = dict(self.features)
            if self.partial_features is not None and self.partial_features[0] not in texts:
                texts[self.partial_features[0]] = self.partial_features[1]
            return Snapshot(started=self.started, finished=self.finished, t_finished=self.t_finished,
                            texts=texts, errors=tuple(self.errors))

    # -- the lane worker's side ---------------------------------------------------------

    def stale(self, now: float) -> bool:
        """Not started and never will be: answered, cancelled, or past its deadline."""
        return not self.started and (self.responded or self.cancelled or now >= self.deadline)

    def begin(self, now: float) -> bool:
        with self._lock:
            if self.cancelled or self.responded or now >= self.deadline:
                self.cancelled = True
                return False
            self.started = True
            return True

    def should_stop(self, now: float, waiting: bool) -> bool:
        if self.late:
            return False
        if not (self.responded or now >= self.deadline):
            return False
        return self._gen is None or waiting

    def preemptible(self, now: float) -> bool:
        """Whether it hands the model to a waiting request (and is parked):
        a patient job always, a PII job once its request has its answer."""
        return self.patient or (self.late and (self.responded or now >= self.deadline))

    def replaceable(self) -> bool:
        """A parked job a newer parked PII job may replace: a PII job whose
        request no longer waits for it -- never a patient one still waited for."""
        return self.late and (self.responded or not self.patient)

    def step(self) -> bool:
        """One unit of work: a cache hit, or one step of the model's scan.
        Returns whether there is more."""
        if self._cursor >= len(self.items):
            return False
        tid, text, key = self.items[self._cursor]
        try:
            if self._gen is None:
                hit = self._cache.get(key)
                if hit is not None:
                    self._record(tid, self._present(hit, True))
                    return self._advance()
                self._gen = self._work(text)
                self._compute = 0.0
            started = self._clock()
            try:
                yielded = next(self._gen)
            except StopIteration as stop:
                self._compute += self._clock() - started
                metrics.text_seconds.labels(self.lane).observe(self._compute)
                self._cache.put(key, stop.value)
                self._record(tid, self._present(stop.value, False))
                return self._advance()
            self._compute += self._clock() - started
            if yielded is not None and self._partial is not None:
                partial = self._partial(yielded)
                with self._lock:
                    self.partial_features = (tid, partial)
            return True
        except Exception as exc:  # noqa: BLE001 -- one text failed; the job goes on with the next
            name = type(exc).__name__
            with self._lock:
                self.errors.append(name)
                self.partial_features = None
            logger.warning("lane %s: %s text failed: %s", self.lane, self.task, name)
            return self._advance()

    def _record(self, tid: str, features: Any) -> None:
        with self._lock:
            self.features[tid] = features
            self.partial_features = None

    def _advance(self) -> bool:
        if self._gen is not None:
            self._gen.close()
        self._gen = None
        self._cursor += 1
        return self._cursor < len(self.items)

    def end(self, now: float, outcome: str) -> None:
        with self._lock:
            if outcome == FINISHED:
                self.finished = True
                self.t_finished = now
            if self._gen is not None:
                self._gen.close()
                self._gen = None
        self.ended.set()
        loop, future = self._loop, self.future
        if loop is not None and future is not None:
            try:
                loop.call_soon_threadsafe(_resolve, future)
            except RuntimeError:  # the loop is closed (shutdown)
                pass


class WarmJob:
    """Embed an example set in the background, one batch per step, then
    publish it to ``ExampleSets``. Hands the model to any waiting request
    between two batches; never stopped, never skipped."""

    late = False
    patient = False
    deadline = float("inf")

    def __init__(self, *, embedder: Any, entry: ExampleSet, examples: Sequence[Example],
                 sets: ExampleSets) -> None:
        self.lane = "embed"
        self.task = "warming"
        self.started = False
        self._embedder = embedder
        self._entry = entry
        self._ids = [e.id for e in examples]
        self._routes = [e.route for e in examples]
        self._texts = [e.text for e in examples]
        self._sets = sets
        self._gen: Optional[Generator[Any, None, Any]] = None
        self.ended = threading.Event()

    def stale(self, now: float) -> bool:
        return False

    def begin(self, now: float) -> bool:
        self.started = True
        return True

    def should_stop(self, now: float, waiting: bool) -> bool:
        return False

    def preemptible(self, now: float) -> bool:
        return True

    def replaceable(self) -> bool:
        return False

    def step(self) -> bool:
        try:
            if self._gen is None:
                self._gen = self._embedder.embed_set(self._texts)
            next(self._gen)
            return True
        except StopIteration as stop:
            published = self._sets.ready(self._entry, self._ids, self._routes, stop.value)
            metrics.warmings_total.labels("ok" if published else "evicted").inc()
            logger.info("warming: example set %s embedded (%d examples)%s", self._entry.fingerprint[:12],
                        len(self._ids), "" if published else ", but it was evicted meanwhile")
            return False
        except Exception as exc:  # noqa: BLE001 -- the next request naming the set tries again
            self._sets.discard(self._entry)
            metrics.warmings_total.labels("failed").inc()
            logger.warning("warming: example set %s failed: %s", self._entry.fingerprint[:12], type(exc).__name__)
            return False

    def end(self, now: float, outcome: str) -> None:
        if self._gen is not None:
            self._gen.close()
            self._gen = None
        self.ended.set()


class Lane:
    """One model's worker thread and its two queues (see the module docstring)."""

    def __init__(self, name: str, *, clock: Clock = CLOCK) -> None:
        self.name = name
        self._clock = clock
        self._cond = threading.Condition()
        self._fg: List[Any] = []
        self._bg: Deque[Any] = deque()
        self._stopping = False
        self._thread = threading.Thread(target=self._loop, name=f"signals-{name}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def request_stop(self) -> None:
        """Tell the worker to exit after its current step (jobs still queued are dropped)."""
        with self._cond:
            self._stopping = True
            self._cond.notify_all()

    def join(self, timeout: float) -> None:
        if self._thread.is_alive():
            self._thread.join(max(0.0, timeout))

    def submit(self, job: Any) -> None:
        """Queue a request's job: foreground, or background for a patient one."""
        if job.patient:
            self.submit_background(job)
            return
        with self._cond:
            self._fg.append(job)
            self._gauge()
            self._cond.notify()

    def submit_background(self, job: Any) -> None:
        with self._cond:
            self._bg.append(job)
            self._gauge()
            self._cond.notify()

    def withdraw(self, job: Any) -> bool:
        """Drop a queued job its request no longer waits for; True when it was still queued."""
        with self._cond:
            for queue in (self._fg, self._bg):
                if job in queue:
                    queue.remove(job)
                    self._gauge()
                    return True
            return False

    def depth(self) -> Tuple[int, int]:
        with self._cond:
            return len(self._fg), len(self._bg)

    def _gauge(self) -> None:
        metrics.queue_depth.labels(self.name, "foreground").set(len(self._fg))
        metrics.queue_depth.labels(self.name, "background").set(len(self._bg))

    def _waiting(self, now: float) -> bool:
        """A request's job is queued and can still meet its deadline (call
        under the lock). Patient jobs queue in the background, so they never
        count: a patient job waits for no one."""
        return any(not job.stale(now) for job in self._fg)

    def _next(self) -> Optional[Any]:
        while True:
            skipped: List[Any] = []
            job = None
            with self._cond:
                while not self._stopping and not self._fg and not self._bg:
                    self._cond.wait()
                if self._stopping:
                    return None
                now = self._clock()
                for stale in [j for j in self._fg if j.stale(now)]:
                    self._fg.remove(stale)
                    skipped.append(stale)
                if self._fg:
                    job = self._fg.pop()            # newest first
                elif self._bg:
                    job = self._bg.popleft()
                self._gauge()
            for stale in skipped:
                stale.begin(now)                    # marks it cancelled
                stale.end(now, SKIPPED)
                metrics.jobs_total.labels(self.name, SKIPPED).inc()
            if job is not None:
                return job

    def _loop(self) -> None:
        while True:
            job = self._next()
            if job is None:
                return
            try:
                self._run(job)
            except Exception as exc:  # noqa: BLE001 -- never let one job kill the lane
                logger.error("lane %s: job failed: %s", self.name, type(exc).__name__)
                job.end(self._clock(), STOPPED)

    def _run(self, job: Any) -> None:
        if not job.started:
            now = self._clock()
            if not job.begin(now):
                job.end(now, SKIPPED)
                metrics.jobs_total.labels(self.name, SKIPPED).inc()
                return
        while True:
            now = self._clock()
            with self._cond:
                waiting = self._waiting(now)
            if job.should_stop(now, waiting):
                job.end(now, STOPPED)
                metrics.jobs_total.labels(self.name, STOPPED).inc()
                return
            if waiting and job.preemptible(now):
                self._park(job, now)
                return
            if not job.step():
                job.end(self._clock(), FINISHED)
                metrics.jobs_total.labels(self.name, FINISHED).inc()
                return

    def _park(self, job: Any, now: float) -> None:
        """Put a preempted job at the back of the background queue. A PII job
        whose request has its answer replaces every older one like it (at most
        one per lane); a patient job still waited for replaces nothing and is
        never replaced."""
        replaced: List[Any] = []
        with self._cond:
            if job.replaceable():
                for old in [j for j in self._bg if j.replaceable()]:
                    self._bg.remove(old)
                    replaced.append(old)
            self._bg.append(job)
            self._gauge()
        metrics.jobs_total.labels(self.name, PARKED).inc()
        for old in replaced:
            old.end(now, REPLACED)
            metrics.jobs_total.labels(self.name, REPLACED).inc()


def _clamp(value: Optional[int], default: int, low: int, high: int) -> int:
    return max(low, min(int(default if value is None else value), high))


def _similarity(value: float) -> float:
    return round(min(1.0, max(-1.0, float(value))), 4)


def pii_features(entities: Mapping[str, Mapping[str, Any]], complete: bool, cached: bool) -> PiiFeatures:
    """A PII scan's ``{type: {count, max_score}}`` as the wire's features."""
    return PiiFeatures({kind: EntityStat(int(stat["count"]), stat.get("max_score")) for kind, stat in entities.items()},
                       complete=complete, cached=cached)


def nearest_features(found: Mapping[str, Any], cached: bool) -> NearestFeatures:
    """``tasks.embed.nearest``'s answer as the wire's features."""
    return NearestFeatures(
        tuple(Neighbour(nb["id"], nb["route"], _similarity(nb["similarity"])) for nb in found["neighbours"]),
        {route: RouteShare(min(1.0, max(0.0, s["share"])), _similarity(s["best"]), int(s["count"]))
         for route, s in found["routes"].items()},
        cached=cached)


def injection_features(value: Mapping[str, Any], cached: bool) -> InjectionFeatures:
    return InjectionFeatures(min(1.0, max(0.0, float(value["score"]))), int(value["windows"]), cached=cached)


def language_features(value: Mapping[str, Any], cached: bool) -> LanguageFeatures:
    return LanguageFeatures(value["language"], min(1.0, max(0.0, float(value["confidence"]))), cached=cached)


class Engine:
    """The lanes, the cache and the example sets, and ``run`` for one request."""

    def __init__(self, models: Models, cache: FeatureCache, sets: ExampleSets, *, clock: Clock = CLOCK) -> None:
        self.models = models
        self.cache = cache
        self.sets = sets
        self._clock = clock
        self.lanes: Dict[str, Lane] = {name: Lane(name, clock=clock) for name in config.ONNX_LANES}

    def start(self) -> None:
        for lane in self.lanes.values():
            lane.start()

    def stop(self, timeout: float = config.DRAIN_S) -> None:
        """Stop every lane, waiting at most ``timeout`` in all."""
        for lane in self.lanes.values():
            lane.request_stop()
        until = time.monotonic() + timeout
        for lane in self.lanes.values():
            lane.join(until - time.monotonic())

    def _ms(self, start: float, at: Optional[float] = None) -> float:
        return round(max(0.0, (self._clock() if at is None else at) - start) * 1000.0, 2)

    def _unavailable(self, lane: str) -> TaskResult:
        """The answer for a task whose model is not ready: ``error`` says
        ``loading``, or the exception type its load failed with."""
        state = self.models.snapshot()
        return TaskResult("unavailable", error=state["errors"].get(lane) or "loading")

    # -- planning: cache lookups and jobs ------------------------------------------------

    def _lookup(self, task: str, keys: Sequence[Tuple[str, str, bytes]],
                present: Callable[[Any, bool], Any]) -> Tuple[Dict[str, Any], list]:
        hits: Dict[str, Any] = {}
        misses = []
        for tid, text, key in keys:
            value = self.cache.get(key)
            if value is None:
                misses.append((tid, text, key))
            else:
                hits[tid] = present(value, True)
        metrics.cache_total.labels(task, "hit").inc(len(hits))
        metrics.cache_total.labels(task, "miss").inc(len(misses))
        return hits, misses

    def _plan_pii(self, task: TaskRequest, texts: Mapping[str, str], deadline: float, patient: bool):
        model = self.models.get("pii")
        if model is None:
            return self._unavailable("pii")
        mid = self.models.model_id("pii")
        present = lambda v, cached: pii_features(v, True, cached)  # noqa: E731
        keys = [(tid, texts[tid], text_key("pii", mid, texts[tid])) for tid in task.texts]
        hits, misses = self._lookup("pii", keys, present)
        if not misses:
            return hits, None
        return hits, TextJob(task="pii", lane="pii", items=misses, work=model.scan, present=present,
                             partial=lambda p: pii_features(p, False, False), cache=self.cache,
                             deadline=deadline, late=True, patient=patient, clock=self._clock)

    def _plan_injection(self, task: TaskRequest, texts: Mapping[str, str], deadline: float, patient: bool):
        model = self.models.get("injection")
        if model is None:
            return self._unavailable("injection")
        caps = self.models.specs["injection"].tokens
        max_tokens = _clamp(task.max_tokens, caps["default"], 8, caps["max"])
        mid = self.models.model_id("injection")
        keys = [(tid, texts[tid], text_key("injection", mid, str(max_tokens), texts[tid])) for tid in task.texts]
        hits, misses = self._lookup("injection", keys, injection_features)
        if not misses:
            return hits, None
        return hits, TextJob(task="injection", lane="injection", items=misses,
                             work=lambda text: model.scan(text, max_tokens), present=injection_features,
                             cache=self.cache, deadline=deadline, patient=patient, clock=self._clock)

    def _plan_nearest(self, task: TaskRequest, texts: Mapping[str, str], deadline: float, patient: bool):
        model = self.models.get("embed")
        if model is None:
            return self._unavailable("embed")
        mid = self.models.model_id("embed")
        examples = task.examples
        entry, warm = self.sets.lookup(examples.fingerprint, fingerprint_examples(examples.items, mid))
        if warm:
            self.lanes["embed"].submit_background(
                WarmJob(embedder=model, entry=entry, examples=examples.items, sets=self.sets))
            logger.info("warming: example set %s (%d examples) queued", examples.fingerprint[:12],
                        len(examples.items))
        self._gauge_sets()
        if entry.state != READY:
            return TaskResult("warming")
        caps = self.models.specs["embed"].tokens
        max_tokens = _clamp(task.max_tokens, caps["default"], 8, caps["max"])
        k = _clamp(task.k, DEFAULT_K, 1, MAX_K)
        vectors, ids, routes = entry.vectors, entry.ids, entry.routes
        present = lambda v, cached: nearest_features(nearest(vectors, ids, routes, v, k), cached)  # noqa: E731
        keys = [(tid, texts[tid], text_key("embed", mid, str(max_tokens), texts[tid])) for tid in task.texts]
        hits, misses = self._lookup("nearest", keys, present)
        if not misses:
            return hits, None
        return hits, TextJob(task="nearest", lane="embed", items=misses,
                             work=lambda text: model.scan(text, max_tokens), present=present,
                             cache=self.cache, deadline=deadline, patient=patient, clock=self._clock)

    def _language(self, task: TaskRequest, texts: Mapping[str, str], deadline: float, start: float) -> TaskResult:
        """Inline on the event loop (fastText takes microseconds), cached like
        the rest, and cut between texts at the deadline."""
        model = self.models.get("language")
        if model is None:
            return self._unavailable("language")
        mid = self.models.model_id("language")
        found: Dict[str, Any] = {}
        errors: List[str] = []
        hits = misses = 0
        for tid in task.texts:
            key = text_key("language", mid, texts[tid])
            value = self.cache.get(key)
            if value is not None:
                found[tid] = language_features(value, True)
                hits += 1
                continue
            if self._clock() >= deadline:
                break
            misses += 1
            try:
                value = model.detect(texts[tid])
                found[tid] = language_features(value, False)
            except Exception as exc:  # noqa: BLE001 -- reduced to its type: a message may quote the text
                errors.append(type(exc).__name__)
                logger.warning("language: a text failed: %s", errors[-1])
                continue
            self.cache.put(key, value)
        metrics.cache_total.labels("language", "hit").inc(hits)
        metrics.cache_total.labels("language", "miss").inc(misses)
        if errors and not found:
            status = "error"
        elif len(found) == len(task.texts):
            status = "ok"
        else:
            status = "partial" if found else "skipped"
        return TaskResult(status, self._ms(start) if found or status == "ok" else None, found,
                          errors[0] if errors else None)

    def _gauge_sets(self) -> None:
        for state, count in self.sets.states().items():
            metrics.example_sets.labels(state).set(count)

    # -- one request -----------------------------------------------------------------------

    async def run(self, texts: Mapping[str, str], tasks: Sequence[TaskRequest], deadline_ms: Optional[float],
                  start: Optional[float] = None) -> Dict[str, TaskResult]:
        """Every task of one request, answered by ``start + deadline_ms``
        (None: no deadline -- every task runs to the end, bounded by the
        wire's MAX_DEADLINE_MS, as patient jobs: in the background, handing
        the model to any request with a deadline between two steps)."""
        start = self._clock() if start is None else start
        patient = deadline_ms is None
        deadline = start + (MAX_DEADLINE_MS if patient else deadline_ms) / 1000.0
        loop = asyncio.get_running_loop()
        results: Dict[str, TaskResult] = {}
        waiting: List[Tuple[TaskRequest, Dict[str, Any], TextJob]] = []
        planners = {"pii": self._plan_pii, "injection": self._plan_injection, "nearest": self._plan_nearest}

        for task in tasks:
            if task.task not in planners:
                continue
            planned = planners[task.task](task, texts, deadline, patient)
            if isinstance(planned, TaskResult):
                results[task.task] = planned
                continue
            hits, job = planned
            if job is None:
                results[task.task] = TaskResult("ok", self._ms(start), hits)
                continue
            job.attach(loop)
            self.lanes[job.lane].submit(job)
            waiting.append((task, hits, job))

        for task in tasks:
            if task.task == "language":
                results[task.task] = self._language(task, texts, deadline, start)

        # asyncio's timer runs on the loop's clock and may fire up to one clock
        # resolution early -- 15.6 ms on Windows before Python 3.13, which
        # answered an 80 ms request at 63 ms (2026-10-20 end-to-end) -- so wait
        # again while this clock says the deadline has not passed. Bounded, so
        # an injected clock that never moves cannot spin.
        pending = {job.future for _task, _hits, job in waiting}
        for _ in range(_MAX_WAITS):
            remaining = deadline - self._clock()
            if not pending or remaining <= 0:
                break
            _done, pending = await asyncio.wait(pending, timeout=remaining)

        for task, hits, job in waiting:
            snap = job.take()
            if not snap.started and self.lanes[job.lane].withdraw(job):
                metrics.jobs_total.labels(job.lane, SKIPPED).inc()
            results[task.task] = self._combine(task, hits, snap, start)
        metrics.cache_entries.set(len(self.cache))

        ordered = {task.task: results[task.task] for task in tasks if task.task in results}
        for name, result in ordered.items():
            metrics.tasks_total.labels(name, result.status).inc()
            if result.ms is not None:
                metrics.task_seconds.labels(name).observe(result.ms / 1000.0)
        return ordered

    def _combine(self, task: TaskRequest, hits: Dict[str, Any], snap: Snapshot, start: float) -> TaskResult:
        found = {tid: hits[tid] if tid in hits else snap.texts[tid]
                 for tid in task.texts if tid in hits or tid in snap.texts}
        # A failed text is one text not scanned: the others still count
        # (partial, the first failure's type in `error`); error only when
        # nothing else has features.
        if snap.errors and not found:
            status = "error"
        elif snap.finished and not snap.errors:
            status = "ok"
        elif not found:
            status = "deadline" if snap.started else "skipped"
        else:
            status = "partial"
        if snap.finished and snap.t_finished is not None:
            ms: Optional[float] = self._ms(start, snap.t_finished)
        else:
            ms = self._ms(start) if found else None
        return TaskResult(status, ms, found, snap.errors[0] if snap.errors else None)
