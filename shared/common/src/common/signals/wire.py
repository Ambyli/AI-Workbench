"""The ``POST /signals`` wire format, as dataclasses that validate themselves.

One request carries every text the caller wants scanned (``texts``, each
with a caller-chosen id) and every task to run over some of them
(``tasks``, each naming its text ids, newest first, and its own params).
One response carries, per task, a status, the task's time, and per text
id the task's FEATURES -- counts, scores, ids, never text, never spans::

    request  {request_id?, deadline_ms?, texts: [{id, text}],
              tasks: [{task: pii,       texts: [ids]},
                      {task: nearest,   texts, max_tokens?, k?,
                                        examples: {fingerprint, items: [{id, route, text}]}},
                      {task: injection, texts, max_tokens?, windows?},
                      {task: language,  texts}]}
    response {request_id?, latency_ms?, models: {pii|embed|injection|language: "<repo>@<rev>"},
              results: {<task>: {status, ms?, error?, texts: {<id>: <features>}}}}

    features  pii        {entities: {<type>: {count, max_score?}}, complete, cached}
              nearest    {neighbours: [{id, route, similarity}],
                          routes: {<route>: {share, best, count}}, cached}
              injection  {score, windows, cached}
              language   {language, confidence, cached}

Results are keyed by task, so a request names each task at most once. The
entity types are :data:`common.pii.ENTITY_TYPES`; a sidecar maps its
model's labels onto them.

**Validation.** Every dataclass checks its own shape on construction (a
:class:`WireError`, a ``ValueError``), so a producer cannot build a
malformed message and ``to_json`` always emits a valid one. ``from_json``
parses the plain JSON value back:

* a REQUEST is parsed strictly -- an unknown key, a task param the task does
  not take, a text id no text has: :class:`WireError` (the sidecar answers
  422). Size limits are separate: :meth:`SignalsRequest.cap_problem` (the
  sidecar answers 413, the client refuses to send);
* a RESPONSE is parsed for a consumer that must keep going: unknown keys and
  unknown task names are ignored (a newer sidecar), and a known task whose
  result is malformed becomes ``TaskResult(status="error", error="malformed
  <task> result")`` instead of failing the others. Only a response that is
  not an object, or whose ``results`` / ``models`` are the wrong type, is a
  :class:`WireError`. :meth:`SignalsResponse.for_request` then keeps only what
  the request asked for.

**No text in errors.** A :class:`WireError` message names the field and the
rule, never a value from the input -- a key, a string, a number -- so it is
safe to log however hostile the input. A task's ``error`` is kept from the
wire only when it looks like an exception type name.

Stdlib only: the sidecar builds its responses with these, and route-policy
parses them; neither needs httpx for this module.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple, Type, Union

from ..pii import ENTITY_TYPES

TASKS: Tuple[str, ...] = ("pii", "nearest", "injection", "language")
# A task's status in a response. ok: every text done; partial: some (a text
# cut by the deadline, or one that failed -- then ``error`` names the first
# failure's type); deadline: still running at the deadline; skipped: queued
# past it, never started; warming: the example set is being embedded; error:
# it failed, no text has features; unavailable: its model is not loaded.
STATUSES: Tuple[str, ...] = ("ok", "partial", "deadline", "skipped", "warming", "error", "unavailable")
# The statuses whose ``texts`` may carry features.
USABLE_STATUSES: Tuple[str, ...] = ("ok", "partial")
# The key under ``models`` that names each task's model (nearest runs the
# embedding model).
TASK_MODELS: Mapping[str, str] = {"pii": "pii", "nearest": "embed", "injection": "injection",
                                  "language": "language"}
INJECTION_WINDOWS: Tuple[str, ...] = ("head_tail",)

# Request caps (the sidecar answers 413 past them; the client does not send).
MAX_TEXT_CHARS = 20_000
MAX_REQUEST_CHARS = 200_000      # over ``texts``; example sets are capped on their own
MAX_TEXTS = 64
MAX_EXAMPLES = 1_000
MAX_EXAMPLE_CHARS = 300
# Shape limits (a WireError past them).
MAX_TOKENS_CAP = 512
MAX_K = 50
MAX_DEADLINE_MS = 60_000
MAX_ID_CHARS = 128
MAX_MODEL_CHARS = 200

_PARAMS: Mapping[str, Tuple[str, ...]] = {
    "pii": (),
    "nearest": ("max_tokens", "k", "examples"),
    "injection": ("max_tokens", "windows"),
    "language": (),
}
_EPS = 1e-6
_LANGUAGE = re.compile(r"[A-Za-z]{2,8}(?:[-_][A-Za-z0-9]{1,8}){0,3}")
_ERROR_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.:-]{0,79}")


class WireError(ValueError):
    """A message that does not have the wire shape. The message names the
    field and the rule, never a value from the input."""


# ── checks ────────────────────────────────────────────────────────────────────
def _str(value: Any, where: str, *, max_chars: float = MAX_ID_CHARS, empty: bool = False) -> str:
    if not isinstance(value, str):
        raise WireError(f"{where}: must be a string")
    if not value and not empty:
        raise WireError(f"{where}: must not be empty")
    if len(value) > max_chars:
        raise WireError(f"{where}: longer than {max_chars} characters")
    return value


def _number(value: Any, where: str, *, lo: float = -math.inf, hi: float = math.inf) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise WireError(f"{where}: must be a finite number")
    if not lo - _EPS <= value <= hi + _EPS:
        raise WireError(f"{where}: out of range [{lo:g}, {hi:g}]")
    return float(value)


def _int(value: Any, where: str, *, lo: int = 0, hi: Optional[int] = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise WireError(f"{where}: must be an integer")
    if value < lo or (hi is not None and value > hi):
        raise WireError(f"{where}: out of range [{lo}, {hi if hi is not None else 'inf'}]")
    return value


def _bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise WireError(f"{where}: must be true or false")
    return value


def _object(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise WireError(f"{where}: must be an object")
    return value


def _list(value: Any, where: str) -> List[Any]:
    if not isinstance(value, (list, tuple)):
        raise WireError(f"{where}: must be a list")
    return list(value)


def _only(obj: Mapping[str, Any], allowed: Iterable[str], where: str) -> None:
    extra = set(obj) - set(allowed)
    if extra:
        raise WireError(f"{where}: {len(extra)} unknown key(s)")


def _set(instance: Any, name: str, value: Any) -> None:
    object.__setattr__(instance, name, value)


# ── request ───────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class TextItem:
    """One text to scan, under a caller-chosen id (route-policy: ``u<k>``
    for user message k, ``t<k>`` for the k-th tool result)."""

    id: str
    text: str

    def __post_init__(self) -> None:
        _str(self.id, "text.id")
        _str(self.text, "text.text", max_chars=math.inf, empty=True)

    def to_json(self) -> Dict[str, Any]:
        return {"id": self.id, "text": self.text}

    @classmethod
    def from_json(cls, obj: Any, where: str = "text") -> "TextItem":
        o = _object(obj, where)
        _only(o, ("id", "text"), where)
        return cls(_str(o.get("id"), f"{where}.id"),
                   _str(o.get("text"), f"{where}.text", max_chars=math.inf, empty=True))


@dataclass(frozen=True)
class Example:
    """One labelled example prompt of a nearest-example set."""

    id: str
    route: str
    text: str

    def __post_init__(self) -> None:
        _str(self.id, "example.id")
        _str(self.route, "example.route")
        _str(self.text, "example.text", max_chars=math.inf)

    def to_json(self) -> Dict[str, Any]:
        return {"id": self.id, "route": self.route, "text": self.text}

    @classmethod
    def from_json(cls, obj: Any, where: str = "example") -> "Example":
        o = _object(obj, where)
        _only(o, ("id", "route", "text"), where)
        return cls(_str(o.get("id"), f"{where}.id"), _str(o.get("route"), f"{where}.route"),
                   _str(o.get("text"), f"{where}.text", max_chars=math.inf))


def fingerprint_examples(items: Iterable[Example], model: str) -> str:
    """The example set's cache key: sha256 (hex) over the canonical JSON of
    ``{"model": model, "examples": sorted([id, route, text], ...)}`` (UTF-8,
    no whitespace). Order-independent; any edited example, or another
    model, is another fingerprint."""
    payload = json.dumps({"model": model, "examples": sorted([e.id, e.route, e.text] for e in items)},
                         ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()


@dataclass(frozen=True)
class ExampleSet:
    """A nearest-example set: its fingerprint (the sidecar's cache key for
    the embedded set) and its items, ids unique."""

    fingerprint: str
    items: Tuple[Example, ...]

    def __post_init__(self) -> None:
        _str(self.fingerprint, "examples.fingerprint")
        items = tuple(self.items)
        if not items:
            raise WireError("examples.items: must not be empty")
        if not all(isinstance(e, Example) for e in items):
            raise WireError("examples.items: must be Example values")
        if len({e.id for e in items}) != len(items):
            raise WireError("examples.items: ids must be unique")
        _set(self, "items", items)

    @classmethod
    def build(cls, items: Iterable[Example], *, model: str) -> "ExampleSet":
        """The set with its fingerprint computed (:func:`fingerprint_examples`)."""
        items = tuple(items)
        return cls(fingerprint_examples(items, model), items)

    def to_json(self) -> Dict[str, Any]:
        return {"fingerprint": self.fingerprint, "items": [e.to_json() for e in self.items]}

    @classmethod
    def from_json(cls, obj: Any, where: str = "examples") -> "ExampleSet":
        o = _object(obj, where)
        _only(o, ("fingerprint", "items"), where)
        items = _list(o.get("items"), f"{where}.items")
        return cls(_str(o.get("fingerprint"), f"{where}.fingerprint"),
                   tuple(Example.from_json(e, f"{where}.items[]") for e in items))


@dataclass(frozen=True)
class TaskRequest:
    """One task over some of the request's texts (ids, newest first), with
    the params that task takes: ``nearest`` -- ``examples`` (required),
    ``k``, ``max_tokens``; ``injection`` -- ``max_tokens``, ``windows``;
    ``pii`` and ``language`` -- none. An unset param is the sidecar's
    default."""

    task: str
    texts: Tuple[str, ...] = ()
    max_tokens: Optional[int] = None
    k: Optional[int] = None
    windows: Optional[str] = None
    examples: Optional[ExampleSet] = None

    def __post_init__(self) -> None:
        if self.task not in TASKS:
            raise WireError("task.task: not a known task")
        if isinstance(self.texts, str):
            raise WireError("task.texts: must be a list of text ids")
        texts = tuple(self.texts)
        for tid in texts:
            _str(tid, "task.texts[]")
        if len(set(texts)) != len(texts):
            raise WireError("task.texts: ids must be unique")
        _set(self, "texts", texts)
        allowed = _PARAMS[self.task]
        for name in ("max_tokens", "k", "windows", "examples"):
            if getattr(self, name) is not None and name not in allowed:
                raise WireError(f"task.{name}: the {self.task} task takes no {name}")
        if self.max_tokens is not None:
            _int(self.max_tokens, "task.max_tokens", lo=1, hi=MAX_TOKENS_CAP)
        if self.k is not None:
            _int(self.k, "task.k", lo=1, hi=MAX_K)
        if self.windows is not None and self.windows not in INJECTION_WINDOWS:
            raise WireError("task.windows: not a known window mode")
        if self.task == "nearest" and not isinstance(self.examples, ExampleSet):
            raise WireError("task.examples: the nearest task needs an example set")

    def to_json(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"task": self.task, "texts": list(self.texts)}
        for name in ("max_tokens", "k", "windows"):
            if getattr(self, name) is not None:
                out[name] = getattr(self, name)
        if self.examples is not None:
            out["examples"] = self.examples.to_json()
        return out

    @classmethod
    def from_json(cls, obj: Any, where: str = "task") -> "TaskRequest":
        o = _object(obj, where)
        task = o.get("task")
        if task not in TASKS:
            raise WireError(f"{where}.task: not a known task")
        _only(o, ("task", "texts") + _PARAMS[task], where)
        texts = tuple(_str(t, f"{where}.texts[]") for t in _list(o.get("texts", []), f"{where}.texts"))
        examples = o.get("examples")
        return cls(task, texts, max_tokens=o.get("max_tokens"), k=o.get("k"), windows=o.get("windows"),
                   examples=ExampleSet.from_json(examples, f"{where}.examples") if examples is not None else None)


@dataclass(frozen=True)
class SignalsRequest:
    """One ``POST /signals``: the texts, the tasks over them, a request id
    for the sidecar's logs (route-policy sends the decision id), and the
    deadline the sidecar answers by (None = no deadline: wait for every
    task)."""

    texts: Tuple[TextItem, ...]
    tasks: Tuple[TaskRequest, ...]
    request_id: Optional[str] = None
    deadline_ms: Optional[float] = None

    def __post_init__(self) -> None:
        texts, tasks = tuple(self.texts), tuple(self.tasks)
        if not all(isinstance(t, TextItem) for t in texts):
            raise WireError("texts: must be TextItem values")
        if not all(isinstance(t, TaskRequest) for t in tasks):
            raise WireError("tasks: must be TaskRequest values")
        ids = {t.id for t in texts}
        if len(ids) != len(texts):
            raise WireError("texts: ids must be unique")
        if len({t.task for t in tasks}) != len(tasks):
            raise WireError("tasks: each task at most once (results are keyed by task)")
        for task in tasks:
            if not set(task.texts) <= ids:
                raise WireError(f"tasks.{task.task}.texts: names a text id the request does not carry")
        if self.request_id is not None:
            _str(self.request_id, "request_id")
        if self.deadline_ms is not None:
            if _number(self.deadline_ms, "deadline_ms", lo=0, hi=MAX_DEADLINE_MS) <= 0:
                raise WireError("deadline_ms: must be positive")
        _set(self, "texts", texts)
        _set(self, "tasks", tasks)

    def cap_problem(self) -> Optional[str]:
        """Why the request is over the size caps (the sidecar's 413), or
        None. Names text ids and sizes, never text."""
        if len(self.texts) > MAX_TEXTS:
            return f"{len(self.texts)} texts, more than {MAX_TEXTS}"
        total = 0
        for item in self.texts:
            total += len(item.text)
            if len(item.text) > MAX_TEXT_CHARS:
                return f"text {item.id!r} has {len(item.text)} characters, more than {MAX_TEXT_CHARS}"
        if total > MAX_REQUEST_CHARS:
            return f"the texts hold {total} characters, more than {MAX_REQUEST_CHARS}"
        for task in self.tasks:
            if task.examples is None:
                continue
            if len(task.examples.items) > MAX_EXAMPLES:
                return f"{len(task.examples.items)} examples, more than {MAX_EXAMPLES}"
            for e in task.examples.items:
                if len(e.text) > MAX_EXAMPLE_CHARS:
                    return f"example {e.id!r} has {len(e.text)} characters, more than {MAX_EXAMPLE_CHARS}"
        return None

    def task(self, name: str) -> Optional[TaskRequest]:
        for task in self.tasks:
            if task.task == name:
                return task
        return None

    def to_json(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        if self.request_id is not None:
            out["request_id"] = self.request_id
        if self.deadline_ms is not None:
            out["deadline_ms"] = self.deadline_ms
        out["texts"] = [t.to_json() for t in self.texts]
        out["tasks"] = [t.to_json() for t in self.tasks]
        return out

    @classmethod
    def from_json(cls, obj: Any) -> "SignalsRequest":
        o = _object(obj, "request")
        _only(o, ("request_id", "deadline_ms", "texts", "tasks"), "request")
        texts = tuple(TextItem.from_json(t, "texts[]") for t in _list(o.get("texts", []), "texts"))
        tasks = tuple(TaskRequest.from_json(t, "tasks[]") for t in _list(o.get("tasks", []), "tasks"))
        return cls(texts, tasks, request_id=o.get("request_id"), deadline_ms=o.get("deadline_ms"))


# ── features ──────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class EntityStat:
    """How many entities of one type the NER found in a text, and its best
    score (None when the model gives none)."""

    count: int
    max_score: Optional[float] = None

    def __post_init__(self) -> None:
        _int(self.count, "entity.count")
        if self.max_score is not None:
            _number(self.max_score, "entity.max_score", lo=0, hi=1)

    def to_json(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"count": self.count}
        if self.max_score is not None:
            out["max_score"] = self.max_score
        return out

    @classmethod
    def from_json(cls, obj: Any, where: str) -> "EntityStat":
        o = _object(obj, where)
        score = o.get("max_score")
        return cls(_int(o.get("count"), f"{where}.count"),
                   _number(score, f"{where}.max_score", lo=0, hi=1) if score is not None else None)


@dataclass(frozen=True)
class PiiFeatures:
    """The ``pii`` task's features for one text: entity type -> stat (types
    from :data:`common.pii.ENTITY_TYPES`); ``complete`` false when only part
    of the text was scanned by the deadline; ``cached`` when the sidecar
    answered from its cache."""

    entities: Mapping[str, EntityStat] = field(default_factory=dict)
    complete: bool = True
    cached: bool = False

    def __post_init__(self) -> None:
        entities = dict(self.entities)
        for name, stat in entities.items():
            if name not in ENTITY_TYPES:
                raise WireError("pii.entities: a type outside the entity vocabulary")
            if not isinstance(stat, EntityStat):
                raise WireError("pii.entities: values must be EntityStat")
        _bool(self.complete, "pii.complete")
        _bool(self.cached, "pii.cached")
        _set(self, "entities", entities)

    def to_json(self) -> Dict[str, Any]:
        return {"entities": {k: v.to_json() for k, v in self.entities.items()},
                "complete": self.complete, "cached": self.cached}

    @classmethod
    def from_json(cls, obj: Any, where: str = "pii") -> "PiiFeatures":
        o = _object(obj, where)
        raw = _object(o.get("entities", {}), f"{where}.entities")
        entities = {}
        for name, stat in raw.items():
            if name not in ENTITY_TYPES:
                raise WireError(f"{where}.entities: a type outside the entity vocabulary")
            entities[name] = EntityStat.from_json(stat, f"{where}.entities[]")
        return cls(entities, complete=_bool(o.get("complete", True), f"{where}.complete"),
                   cached=_bool(o.get("cached", False), f"{where}.cached"))


@dataclass(frozen=True)
class Neighbour:
    """One of the k nearest examples: its id, route and cosine similarity."""

    id: str
    route: str
    similarity: float

    def __post_init__(self) -> None:
        _str(self.id, "neighbour.id")
        _str(self.route, "neighbour.route")
        _number(self.similarity, "neighbour.similarity", lo=-1, hi=1)

    def to_json(self) -> Dict[str, Any]:
        return {"id": self.id, "route": self.route, "similarity": self.similarity}

    @classmethod
    def from_json(cls, obj: Any, where: str) -> "Neighbour":
        o = _object(obj, where)
        return cls(_str(o.get("id"), f"{where}.id"), _str(o.get("route"), f"{where}.route"),
                   _number(o.get("similarity"), f"{where}.similarity", lo=-1, hi=1))


@dataclass(frozen=True)
class RouteShare:
    """One route among the k nearest: its share of the top-k similarity
    mass, its best similarity, and how many of the k it holds."""

    share: float
    best: float
    count: int

    def __post_init__(self) -> None:
        _number(self.share, "route.share", lo=0, hi=1)
        _number(self.best, "route.best", lo=-1, hi=1)
        _int(self.count, "route.count")

    def to_json(self) -> Dict[str, Any]:
        return {"share": self.share, "best": self.best, "count": self.count}

    @classmethod
    def from_json(cls, obj: Any, where: str) -> "RouteShare":
        o = _object(obj, where)
        return cls(_number(o.get("share"), f"{where}.share", lo=0, hi=1),
                   _number(o.get("best"), f"{where}.best", lo=-1, hi=1), _int(o.get("count"), f"{where}.count"))


@dataclass(frozen=True)
class NearestFeatures:
    """The ``nearest`` task's features for one text: the k nearest examples,
    most similar first, and the per-route summary of them."""

    neighbours: Tuple[Neighbour, ...] = ()
    routes: Mapping[str, RouteShare] = field(default_factory=dict)
    cached: bool = False

    def __post_init__(self) -> None:
        neighbours = tuple(self.neighbours)
        if not all(isinstance(nb, Neighbour) for nb in neighbours):
            raise WireError("nearest.neighbours: must be Neighbour values")
        if len(neighbours) > MAX_K:
            raise WireError(f"nearest.neighbours: more than {MAX_K}")
        routes = dict(self.routes)
        for name, share in routes.items():
            _str(name, "nearest.routes key")
            if not isinstance(share, RouteShare):
                raise WireError("nearest.routes: values must be RouteShare")
        _bool(self.cached, "nearest.cached")
        _set(self, "neighbours", neighbours)
        _set(self, "routes", routes)

    def to_json(self) -> Dict[str, Any]:
        return {"neighbours": [nb.to_json() for nb in self.neighbours],
                "routes": {k: v.to_json() for k, v in self.routes.items()}, "cached": self.cached}

    @classmethod
    def from_json(cls, obj: Any, where: str = "nearest") -> "NearestFeatures":
        o = _object(obj, where)
        neighbours = tuple(Neighbour.from_json(nb, f"{where}.neighbours[]")
                           for nb in _list(o.get("neighbours", []), f"{where}.neighbours"))
        raw = _object(o.get("routes", {}), f"{where}.routes")
        routes = {_str(k, f"{where}.routes key"): RouteShare.from_json(v, f"{where}.routes[]")
                  for k, v in raw.items()}
        return cls(neighbours, routes, cached=_bool(o.get("cached", False), f"{where}.cached"))


@dataclass(frozen=True)
class InjectionFeatures:
    """The ``injection`` task's features for one text: the highest
    injection probability over the windows scored, and how many were."""

    score: float
    windows: int = 1
    cached: bool = False

    def __post_init__(self) -> None:
        _number(self.score, "injection.score", lo=0, hi=1)
        _int(self.windows, "injection.windows", lo=1)
        _bool(self.cached, "injection.cached")

    def to_json(self) -> Dict[str, Any]:
        return {"score": self.score, "windows": self.windows, "cached": self.cached}

    @classmethod
    def from_json(cls, obj: Any, where: str = "injection") -> "InjectionFeatures":
        o = _object(obj, where)
        return cls(_number(o.get("score"), f"{where}.score", lo=0, hi=1),
                   _int(o.get("windows", 1), f"{where}.windows", lo=1),
                   cached=_bool(o.get("cached", False), f"{where}.cached"))


@dataclass(frozen=True)
class LanguageFeatures:
    """The ``language`` task's features for one text: a language code (as
    the model labels it: ``en``, ``es``, ``zh`` ...) and its confidence."""

    language: str
    confidence: float
    cached: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.language, str) or not _LANGUAGE.fullmatch(self.language):
            raise WireError("language.language: not a language code")
        _number(self.confidence, "language.confidence", lo=0, hi=1)
        _bool(self.cached, "language.cached")

    def to_json(self) -> Dict[str, Any]:
        return {"language": self.language, "confidence": self.confidence, "cached": self.cached}

    @classmethod
    def from_json(cls, obj: Any, where: str = "language") -> "LanguageFeatures":
        o = _object(obj, where)
        language = o.get("language")
        if not isinstance(language, str) or not _LANGUAGE.fullmatch(language):
            raise WireError(f"{where}.language: not a language code")
        return cls(language, _number(o.get("confidence"), f"{where}.confidence", lo=0, hi=1),
                   cached=_bool(o.get("cached", False), f"{where}.cached"))


Features = Union[PiiFeatures, NearestFeatures, InjectionFeatures, LanguageFeatures]
FEATURES: Mapping[str, Type[Any]] = {"pii": PiiFeatures, "nearest": NearestFeatures,
                                     "injection": InjectionFeatures, "language": LanguageFeatures}


# ── response ──────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class TaskResult:
    """One task's answer: its status (:data:`STATUSES`), its time in ms
    (None when it did not run, or is still running), the features per text
    id it finished, and -- for ``error`` / ``unavailable``, and for a
    ``partial`` some of whose texts failed -- a short reason (an exception
    type name from the sidecar, or the client's own ``malformed <task>
    result``; never text). A ``partial`` is usable either way: the texts in
    ``texts`` were scanned, the others were not."""

    status: str
    ms: Optional[float] = None
    texts: Mapping[str, Features] = field(default_factory=dict)
    error: Optional[str] = None

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise WireError("result.status: not a known status")
        if self.ms is not None:
            _number(self.ms, "result.ms", lo=0)
        texts = dict(self.texts)
        for tid in texts:
            _str(tid, "result.texts key")
        if self.error is not None:
            _str(self.error, "result.error", max_chars=200)
        _set(self, "texts", texts)

    @property
    def usable(self) -> bool:
        """Whether ``texts`` may carry features (``ok`` / ``partial``)."""
        return self.status in USABLE_STATUSES

    def to_json(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"status": self.status, "ms": self.ms,
                               "texts": {k: v.to_json() for k, v in self.texts.items()}}
        if self.error is not None:
            out["error"] = self.error
        return out

    @classmethod
    def from_json(cls, task: str, obj: Any, where: Optional[str] = None) -> "TaskResult":
        """Strict: a :class:`WireError` for any malformed part."""
        where = where or f"results.{task}"
        if task not in TASKS:
            raise WireError(f"{where}: not a known task")
        o = _object(obj, where)
        status = o.get("status")
        if status not in STATUSES:
            raise WireError(f"{where}.status: not a known status")
        ms = o.get("ms")
        features = FEATURES[task]
        texts = {}
        for tid, value in _object(o.get("texts", {}), f"{where}.texts").items():
            texts[_str(tid, f"{where}.texts key")] = features.from_json(value, f"{where}.texts[]")
        error = o.get("error")
        safe = error if isinstance(error, str) and _ERROR_NAME.fullmatch(error) else None
        return cls(status, _number(ms, f"{where}.ms", lo=0) if ms is not None else None, texts, safe)


def _check_result_types(results: Mapping[str, TaskResult]) -> None:
    for task, result in results.items():
        if task not in TASKS:
            raise WireError("results: a task outside the task list")
        if not isinstance(result, TaskResult):
            raise WireError("results: values must be TaskResult")
        if not all(isinstance(f, FEATURES[task]) for f in result.texts.values()):
            raise WireError(f"results.{task}.texts: features of another task")


@dataclass(frozen=True)
class SignalsResponse:
    """The sidecar's answer: per task, a :class:`TaskResult`; the model id
    behind each task (``models``, keyed per :data:`TASK_MODELS`); the
    request id echoed; and the sidecar's own time."""

    results: Mapping[str, TaskResult]
    request_id: Optional[str] = None
    latency_ms: Optional[float] = None
    models: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        results = dict(self.results)
        _check_result_types(results)
        models = dict(self.models)
        for key, value in models.items():
            _str(key, "models key", max_chars=64)
            _str(value, "models value", max_chars=MAX_MODEL_CHARS)
        if self.request_id is not None:
            _str(self.request_id, "request_id")
        if self.latency_ms is not None:
            _number(self.latency_ms, "latency_ms", lo=0)
        _set(self, "results", results)
        _set(self, "models", models)

    def result(self, task: str) -> Optional[TaskResult]:
        return self.results.get(task)

    def model_for(self, task: str) -> Optional[str]:
        """The model id that answered ``task`` (``models[TASK_MODELS[task]]``)."""
        key = TASK_MODELS.get(task)
        return self.models.get(key) if key is not None else None

    def for_request(self, request: SignalsRequest) -> "SignalsResponse":
        """This response cut to what ``request`` asked: one result per
        requested task (``error`` / ``missing from the response`` where the
        sidecar gave none), only the text ids that task named, and -- for
        ``nearest`` -- only answers whose every neighbour and route is in the
        request's example set (anything else is ``error`` / ``neighbour not
        in the example set``): nothing a confused sidecar invents reaches the
        caller. The request id is the request's."""
        results: Dict[str, TaskResult] = {}
        for task in request.tasks:
            result = self.results.get(task.task)
            if result is None:
                results[task.task] = TaskResult("error", error="missing from the response")
                continue
            wanted = set(task.texts)
            texts = {tid: f for tid, f in result.texts.items() if tid in wanted}
            if task.task == "nearest" and task.examples is not None:
                known = {e.id: e.route for e in task.examples.items}
                routes = set(known.values())
                for f in texts.values():
                    if (any(known.get(nb.id) != nb.route for nb in f.neighbours)  # type: ignore[union-attr]
                            or not set(f.routes) <= routes):  # type: ignore[union-attr]
                        result = TaskResult("error", result.ms, {}, "neighbour not in the example set")
                        texts = {}
                        break
            results[task.task] = TaskResult(result.status, result.ms, texts, result.error)
        return SignalsResponse(results, request_id=request.request_id, latency_ms=self.latency_ms,
                               models=self.models)

    def to_json(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        if self.request_id is not None:
            out["request_id"] = self.request_id
        out["latency_ms"] = self.latency_ms
        out["models"] = dict(self.models)
        out["results"] = {k: v.to_json() for k, v in self.results.items()}
        return out

    @classmethod
    def from_json(cls, obj: Any) -> "SignalsResponse":
        """Lenient per task (see the module docstring): unknown keys and tasks
        are ignored, a malformed known task becomes an ``error`` result."""
        o = _object(obj, "response")
        raw = _object(o.get("results"), "results")
        results: Dict[str, TaskResult] = {}
        for task in TASKS:
            if task not in raw:
                continue
            try:
                results[task] = TaskResult.from_json(task, raw[task])
            except WireError:
                results[task] = TaskResult("error", error=f"malformed {task} result")
        models_raw = _object(o.get("models", {}), "models")
        models = {_str(k, "models key", max_chars=64): _str(v, "models value", max_chars=MAX_MODEL_CHARS)
                  for k, v in models_raw.items()}
        request_id = o.get("request_id")
        latency = o.get("latency_ms")
        return cls(results,
                   request_id=_str(request_id, "request_id") if request_id is not None else None,
                   latency_ms=_number(latency, "latency_ms", lo=0) if latency is not None else None,
                   models=models)
