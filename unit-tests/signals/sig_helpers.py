"""Shared helpers for the signals tests: fake models, an engine and an app on them.

The fakes follow the real models' interfaces (signals/tasks/*): ``scan``
generators that yield between "windows", ``embed`` / ``embed_set``,
``detect``, ``warm_up``. What they find is decided by marker words, so a
test controls the answer; how long a step takes, and whether it waits on a
``threading.Event`` the test releases, is set per fake. A ``Recorder``
notes every step (lane, text, start time) and the most steps any one lane
ran at once -- the single-flight check.

Imported by the test modules (conftest.py puts this directory on sys.path).
"""

from __future__ import annotations

import asyncio
import math
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Sequence

from fastapi.testclient import TestClient

from common.signals import Example, ExampleSet, TaskRequest, TaskResult

from signals.cache import ExampleSets, FeatureCache
from signals.runtime import Models, load_specs
from signals.scheduler import Engine

SENTINEL = "SENTINEL-7f3a9c-never-echo-this"
GATE_TIMEOUT_S = 5.0


class Recorder:
    """Every model step, and the peak number running at once per lane."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.steps: List[tuple] = []          # (lane, text, perf_counter at start)
        self.active: Dict[str, int] = {}
        self.peak: Dict[str, int] = {}

    def enter(self, lane: str, text: str) -> None:
        with self._lock:
            self.steps.append((lane, text, time.perf_counter()))
            self.active[lane] = self.active.get(lane, 0) + 1
            self.peak[lane] = max(self.peak.get(lane, 0), self.active[lane])

    def leave(self, lane: str) -> None:
        with self._lock:
            self.active[lane] -= 1

    def texts(self, lane: str) -> List[str]:
        with self._lock:
            return [t for l, t, _ in self.steps if l == lane]

    def starts(self, lane: str) -> List[float]:
        with self._lock:
            return [s for l, _, s in self.steps if l == lane]


class _Fake:
    lane = ""

    def __init__(self, rec: Optional[Recorder] = None, *, step_s: float = 0.0, gate: Optional[threading.Event] = None,
                 gate_from: int = 0, gate_on: Optional[str] = None, fail: Optional[str] = None) -> None:
        self.rec = rec or Recorder()
        self.step_s = step_s
        self.gate = gate            # steps from `gate_from` on (of texts containing `gate_on`) wait for it
        self.gate_from = gate_from
        self.gate_on = gate_on
        self.fail = fail            # a text containing this raises, quoting the text
        self.warmed = False

    def _step(self, text: str, index: int) -> None:
        self.rec.enter(self.lane, text)
        try:
            if self.fail and self.fail in text:
                raise ValueError(f"tokenizer choked on {text!r}")
            gated = self.gate is not None and index >= self.gate_from
            if gated and (self.gate_on is None or self.gate_on in text):
                if not self.gate.wait(GATE_TIMEOUT_S):
                    raise TimeoutError("test gate never opened")
            if self.step_s:
                time.sleep(self.step_s)
        finally:
            self.rec.leave(self.lane)

    def warm_up(self) -> None:
        self.warmed = True


class FakePii(_Fake):
    """One window per ``window_chars`` characters; NAME -> person, ADDRESS ->
    street_address, EMAIL -> email, counted per window."""

    lane = "pii"
    WORDS = (("NAME", "person"), ("ADDRESS", "street_address"), ("EMAIL", "email"))

    def __init__(self, rec: Optional[Recorder] = None, *, window_chars: int = 100, **kw: Any) -> None:
        super().__init__(rec, **kw)
        self.window_chars = window_chars

    def scan(self, text: str):
        found: Dict[str, Dict[str, Any]] = {}
        windows = max(1, math.ceil(len(text) / self.window_chars))
        for i in range(windows):
            self._step(text, i)
            chunk = text[i * self.window_chars:(i + 1) * self.window_chars]
            for word, kind in self.WORDS:
                if word in chunk:
                    entry = found.setdefault(kind, {"count": 0, "max_score": 0.9})
                    entry["count"] += chunk.count(word)
            yield {k: dict(v) for k, v in found.items()}
        return {k: dict(v) for k, v in found.items()}


class FakeInjection(_Fake):
    """Two windows (head + tail) for a text over 100 characters; a score of
    0.97 when the text says "ignore previous", else 0.02."""

    lane = "injection"

    def scan(self, text: str, max_tokens: int):
        windows = 2 if len(text) > 100 else 1
        for i in range(windows):
            self._step(text, i)
            yield None
        return {"score": 0.97 if "ignore previous" in text.lower() else 0.02, "windows": windows}


def unit(values: Sequence[float]):
    import numpy as np

    v = np.asarray(values, dtype=np.float32)
    return v / np.linalg.norm(v)


class FakeEmbed(_Fake):
    """4-d vectors by marker word: code / reason / anything else, plus a small
    common component so every similarity is positive."""

    lane = "embed"

    def __init__(self, rec: Optional[Recorder] = None, *, batch_s: float = 0.0,
                 batch_gate: Optional[threading.Event] = None, fail_sets: bool = False, **kw: Any) -> None:
        super().__init__(rec, **kw)
        self.batch_s = batch_s
        self.batch_gate = batch_gate
        self.fail_sets = fail_sets
        self.sets_embedded = 0

    @staticmethod
    def vector(text: str):
        t = text.lower()
        if "code" in t:
            return unit([1, 0, 0, 0.2])
        if "reason" in t:
            return unit([0, 1, 0, 0.2])
        return unit([0, 0, 1, 0.2])

    def embed(self, texts: Sequence[str], max_tokens: int):
        import numpy as np

        return np.stack([self.vector(t) for t in texts]) if texts else np.zeros((0, 4), dtype=np.float32)

    def scan(self, text: str, max_tokens: int):
        self._step(text, 0)
        yield from ()
        return self.vector(text)

    def embed_set(self, texts: Sequence[str], batch: int = 16):
        import numpy as np

        parts = []
        for i in range(0, len(texts), batch):
            self.rec.enter("embed-set", "")
            try:
                if self.batch_gate is not None and not self.batch_gate.wait(GATE_TIMEOUT_S):
                    raise TimeoutError("test gate never opened")
                if self.batch_s:
                    time.sleep(self.batch_s)
                if self.fail_sets:
                    raise RuntimeError(f"could not embed {texts[i]!r}")
            finally:
                self.rec.leave("embed-set")
            parts.append(self.embed(texts[i:i + batch], 512))
            yield None
        self.sets_embedded += 1
        return np.concatenate(parts) if parts else np.zeros((0, 4), dtype=np.float32)


class FakeLanguage(_Fake):
    lane = "language"

    def detect(self, text: str) -> Dict[str, Any]:
        if self.fail and self.fail in text:
            raise ValueError(f"fasttext choked on {text!r}")
        return {"language": "es" if "hola" in text.lower() else "en", "confidence": 0.9}


def fakes(rec: Optional[Recorder] = None, **per_lane: Any) -> Dict[str, Any]:
    """All four fakes on one recorder; ``pii=FakePii(...)`` etc. override one."""
    rec = rec or Recorder()
    out = {"pii": FakePii(rec), "embed": FakeEmbed(rec), "injection": FakeInjection(rec), "language": FakeLanguage(rec)}
    out.update(per_lane)
    return out


def ready_models(models_by_lane: Dict[str, Any]) -> Models:
    """The real specs (models.yaml), with these fakes ready; other lanes stay loading."""
    models = Models(load_specs())
    for lane, model in models_by_lane.items():
        models.set_ready(lane, model, 0.0)
    return models


@contextmanager
def running_engine(models_by_lane: Dict[str, Any], *, cache_entries: int = 1000, max_sets: int = 8) -> Iterator[Engine]:
    engine = Engine(ready_models(models_by_lane), FeatureCache(cache_entries), ExampleSets(max_sets))
    engine.start()
    try:
        yield engine
    finally:
        engine.stop(2.0)


def run(engine: Engine, texts: Dict[str, str], tasks: Sequence[TaskRequest],
        deadline_ms: Optional[float] = 1000) -> Dict[str, TaskResult]:
    return asyncio.run(engine.run(texts, tasks, deadline_ms))


def features(result: TaskResult) -> Dict[str, Any]:
    """A result's per-text features as wire JSON."""
    return {tid: f.to_json() for tid, f in result.texts.items()}


def wait_until(predicate, timeout: float = GATE_TIMEOUT_S, every: float = 0.005) -> bool:
    until = time.perf_counter() + timeout
    while time.perf_counter() < until:
        if predicate():
            return True
        time.sleep(every)
    return predicate()


def lanes_idle(engine: Engine) -> bool:
    return all(lane.depth() == (0, 0) for lane in engine.lanes.values())


EXAMPLES = tuple(Example(*e) for e in (
    ("code-1", "local-code", "my code throws an error"),
    ("code-2", "local-code", "fix this code"),
    ("code-3", "local-code", "code review please"),
    ("reason-1", "local-reasoning", "reason about this proof"),
    ("reason-2", "local-reasoning", "reason step by step"),
    ("general-1", "local-general", "write a poem"),
    ("general-2", "local-general", "plan a trip"),
))


def example_set(fingerprint: str = "fp-1", examples: Sequence[Example] = EXAMPLES) -> ExampleSet:
    return ExampleSet(fingerprint, tuple(examples))


def nearest_task(texts: Sequence[str] = ("u1",), *, fingerprint: str = "fp-1", examples: Sequence[Example] = EXAMPLES,
                 k: int = 3, **kw: Any) -> TaskRequest:
    return TaskRequest("nearest", tuple(texts), k=k, examples=example_set(fingerprint, examples), **kw)


def examples_json(fingerprint: str = "fp-1", examples: Sequence[Example] = EXAMPLES) -> Dict[str, Any]:
    return example_set(fingerprint, examples).to_json()


@contextmanager
def app_client(models_by_lane: Optional[Dict[str, Any]] = None, *, load_gate: Optional[threading.Event] = None,
               failing: Sequence[str] = (), wait_ready: bool = True) -> Iterator[TestClient]:
    """A TestClient on create_app with fake models (all four by default).
    ``load_gate`` holds every load until set; a lane in ``failing`` raises
    on load (its message quotes the sentinel)."""
    from signals.app import create_app

    chosen = models_by_lane if models_by_lane is not None else fakes()

    def loader(lane: str, spec: Any) -> Any:
        if load_gate is not None and not load_gate.wait(GATE_TIMEOUT_S):
            raise TimeoutError("load gate never opened")
        if lane in failing or lane not in chosen:
            raise OSError(f"cannot open {SENTINEL}/{lane}")
        return chosen[lane]

    app = create_app(loader=loader, instrument=False)
    with TestClient(app) as client:
        if wait_ready and load_gate is None and not failing:
            assert wait_until(app.state.models.ready), app.state.models.snapshot()
        yield client
