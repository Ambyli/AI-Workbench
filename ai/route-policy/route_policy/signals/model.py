"""What an evaluator asks the signals sidecar for, merged into one request,
and what it reads back. Pure: no I/O, no imports from the rest of the
service (``engine.context`` and ``evaluators.base`` import it); the wire
vocabulary and caps come from ``common.signals`` (stdlib only).

**Texts are addressed by id**, stable within one request, so two evaluators
naming the same text send it once:

    u<k>   the user text of the k-th user message (1-based, oldest first;
           ``common.conversation.user_texts``: Open WebUI's RAG block
           stripped)
    t<k>   the k-th tool result of the conversation (1-based, oldest first;
           ``common.conversation.tool_results``) -- sent only when an
           evaluator's scope selects it (``pii``'s data tools, a
           ``prompt_injection`` over the current exchange)

Each carries its ``position`` (message index, then k), so a request lists
texts NEWEST first -- the order the sidecar scans them in.

**One request per decision** (:func:`merge`): every evaluator's
:class:`SignalRequest` for the same task and the same parameters
(:attr:`SignalRequest.key`) becomes one task whose text list is the union;
a later request for the same task with DIFFERENT parameters is not sent
(the sidecar answers one result per task name) and its evaluator abstains
on the key mismatch. The request caps the sidecar enforces with a 413
(:data:`MAX_TEXT_CHARS` per text, :data:`MAX_REQUEST_CHARS` and
:data:`MAX_TEXTS` per request) are applied here, newest first: a longer
text is cut to its first :data:`MAX_TEXT_CHARS` characters, and a text
that no longer fits is left out (an older, shorter one may still fit).

**What comes back** is one :class:`SignalTask` per requested task on
``ctx.signals`` -- the sidecar's status, its per-text features, the model
id it answered with, and which texts were sent -- or, when the call did not
happen in time, ``ctx.signals_error``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional, Tuple

from common.signals import MAX_REQUEST_CHARS, MAX_TEXT_CHARS, MAX_TEXTS, STATUSES, TASK_MODELS, USABLE_STATUSES
from common.signals import TASKS as SIGNAL_TASKS

# The wire vocabulary is common.signals' (shared with the sidecar): the
# tasks, the key each reports its model id under in `models` (nearest ->
# "embed"), the per-task statuses (`ok` and `partial` carry features), and
# the request caps the sidecar enforces with a 413 -- applied before sending.
TASKS = SIGNAL_TASKS
MODEL_KEYS = TASK_MODELS
TASK_STATUSES = STATUSES
RESULT_STATUSES = USABLE_STATUSES
# Route-policy's own, for a task it asked for but got nothing back for.
NOT_SENT = "not_sent"   # no text of it fit the request caps
MISSING = "missing"     # sent, but the answer carried no result for it


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def model_matches(pin: Optional[str], reported: Optional[str]) -> bool:
    """Whether the model the sidecar answered with is the one an evaluator
    was calibrated for. No pin: any model. A pin without ``@<revision>``
    matches every revision of that repo; with one, only that revision."""
    if not pin:
        return True
    if not reported:
        return False
    return reported == pin or ("@" not in pin and reported.partition("@")[0] == pin)


@dataclass(frozen=True)
class SignalText:
    id: str                            # u<k> | t<k>
    text: str = field(repr=False)      # memory only: never logged or stored
    position: Tuple[int, int] = (0, 0)  # (message index, k): larger = newer


@dataclass(frozen=True)
class SignalRequest:
    """What one evaluator will read: one sidecar ``task`` over ``texts``,
    with the task's wire parameters -- ``common.signals.TaskRequest``'s
    keyword arguments (``k``, ``max_tokens``, ``windows``, ``examples`` as a
    ``common.signals.ExampleSet``). ``identity`` is what makes two requests
    the same task (default: the parameters as canonical JSON;
    ``nearest_examples`` uses its example fingerprint instead of the whole
    example set)."""

    task: str
    texts: Tuple[SignalText, ...] = ()
    params: Mapping[str, Any] = field(default_factory=dict)
    identity: Optional[str] = None

    @property
    def key(self) -> str:
        return f"{self.task}:{self.identity if self.identity is not None else canonical(dict(self.params))}"


@dataclass(frozen=True)
class TaskPlan:
    """One task of a merged request."""

    task: str
    key: str
    params: Mapping[str, Any]
    text_ids: Tuple[str, ...]          # newest first


@dataclass(frozen=True)
class SignalBatch:
    """One ``POST /signals`` worth of work, caps applied."""

    texts: Tuple[SignalText, ...]      # every text sent, newest first, each cut to MAX_TEXT_CHARS
    tasks: Tuple[TaskPlan, ...]        # the tasks sent
    not_sent: Tuple[TaskPlan, ...] = ()  # requested, but none of their texts fit the caps
    truncated: FrozenSet[str] = frozenset()   # ids cut to MAX_TEXT_CHARS
    dropped: FrozenSet[str] = frozenset()     # ids left out by MAX_TEXTS / MAX_REQUEST_CHARS
    conflicts: Tuple[str, ...] = ()    # tasks a later evaluator asked for with other parameters

    @property
    def chars(self) -> int:
        return sum(len(t.text) for t in self.texts)


@dataclass(frozen=True)
class SignalTask:
    """One task's result as an evaluator reads it (``ctx.signals[task]``)."""

    task: str
    status: str                        # TASK_STATUSES, or NOT_SENT / MISSING
    key: str                           # the request that was sent for this task
    ms: Optional[float] = None
    model: Optional[str] = None        # the model id the sidecar answered with
    texts: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)   # id -> features
    sent: FrozenSet[str] = frozenset()       # the text ids this task was sent
    truncated: FrozenSet[str] = frozenset()  # of those, the ones cut to MAX_TEXT_CHARS

    @property
    def usable(self) -> bool:
        return self.status in RESULT_STATUSES


def merge(requests: Iterable[SignalRequest], *, keep_empty: bool = False) -> SignalBatch:
    """The requests as one batch (module docstring). Empty texts are never
    sent. A task left with no text is moved to ``not_sent`` -- unless
    ``keep_empty`` (the startup warm-up, whose tasks carry no text on
    purpose)."""
    plans: Dict[str, Dict[str, Any]] = {}
    texts: Dict[str, SignalText] = {}
    conflicts: List[str] = []
    for request in requests:
        plan = plans.get(request.task)
        if plan is None:
            plan = plans[request.task] = {"key": request.key, "params": dict(request.params), "ids": {}}
        elif plan["key"] != request.key:
            if request.task not in conflicts:
                conflicts.append(request.task)
            continue
        for item in request.texts:
            if not item.text:
                continue
            texts.setdefault(item.id, item)
            plan["ids"][item.id] = None

    newest_first = sorted(texts.values(), key=lambda t: t.position, reverse=True)
    kept: List[SignalText] = []
    truncated, dropped = set(), set()
    total = 0
    for item in newest_first:
        text = item.text
        if len(text) > MAX_TEXT_CHARS:
            text = text[:MAX_TEXT_CHARS]
        if len(kept) >= MAX_TEXTS or total + len(text) > MAX_REQUEST_CHARS:
            dropped.add(item.id)
            continue
        if text is not item.text:
            truncated.add(item.id)
            item = SignalText(item.id, text, item.position)
        kept.append(item)
        total += len(text)

    order = {t.id: i for i, t in enumerate(kept)}
    sent: List[TaskPlan] = []
    unsent: List[TaskPlan] = []
    for task, plan in plans.items():
        ids = tuple(sorted((i for i in plan["ids"] if i in order), key=order.__getitem__))
        out = TaskPlan(task, plan["key"], plan["params"], ids)
        if ids or keep_empty:
            sent.append(out)
        else:
            unsent.append(out)
    used = {i for p in sent for i in p.text_ids}
    return SignalBatch(
        texts=tuple(t for t in kept if t.id in used),
        tasks=tuple(sent),
        not_sent=tuple(unsent),
        truncated=frozenset(truncated & used),
        dropped=frozenset(dropped),
        conflicts=tuple(conflicts),
    )
