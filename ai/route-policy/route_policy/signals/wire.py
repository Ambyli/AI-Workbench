"""The adapter between route-policy's signal batches and ``common.signals``,
the sidecar's wire format and client.

* :func:`build_request` -- a :class:`~route_policy.signals.model.SignalBatch`
  as a ``common.signals.SignalsRequest`` (every text once, each task with its
  text ids newest first and its parameters);
* :func:`apply` -- the client's ``SignalsOutcome`` onto the request view:
  ``ctx.signals`` (task -> :class:`~route_policy.signals.model.SignalTask`,
  one per task asked for), ``ctx.signals_error``, ``ctx.signals_ms``;
  returns the ``route_policy_signals_total`` outcome label;
* :func:`task_labels` -- one task's bounded ``route_policy_signal_tasks_total``
  labels.

The only module that turns ``common.signals``'s answer into what evaluators
read: each text's features as the plain JSON object the wire format defines
(``PiiFeatures.to_json()`` and so on), so an evaluator reads keys, never a
wire class. Only the text ids this request sent are kept (the client has
already cut the answer to the request, ``SignalsResponse.for_request``).
Nothing here logs or returns text.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from common.signals import SignalsOutcome, SignalsRequest, TaskRequest, TextItem

from .model import MISSING, NOT_SENT, TASK_STATUSES, TASKS, SignalBatch, SignalTask

# Outcomes that are not the client's: the call was never made, or
# route-policy's own guard around it fired.
NOTHING_SENT = "nothing_sent"   # every task was not_sent: no call
TIMED_OUT = "timeout"           # the guard around the client's own budget
FAILED = "error"                # building the request or the client raised (it should never)

# The client's outcome status -> (route_policy_signals_total label, ctx.signals_error).
_OUTCOMES = {
    "ok": ("ok", None),
    "timeout": ("timeout", "timed out"),
    "error": ("error", "failed"),
    "unavailable": ("unavailable", "unavailable"),
    "not_local": ("not_local", "not local"),
}


def build_request(batch: SignalBatch, *, request_id: Optional[str], deadline_ms: Optional[float]) -> SignalsRequest:
    """The batch as one ``POST /signals``. Raises ``common.signals.WireError``
    (a ``ValueError``) on a parameter the wire format refuses -- the caller
    treats it as a failed call."""
    texts = tuple(TextItem(t.id, t.text) for t in batch.texts)
    tasks = tuple(TaskRequest(plan.task, plan.text_ids, **dict(plan.params)) for plan in batch.tasks)
    return SignalsRequest(texts, tasks, request_id=request_id, deadline_ms=deadline_ms)


def _ms(value: Any) -> Optional[float]:
    return round(float(value), 3) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def apply(ctx: Any, batch: SignalBatch, outcome: Any) -> str:
    """Put ``outcome`` (a ``common.signals.SignalsOutcome``; None for "no
    client"; or one of the sentinels above) on ``ctx`` for ``batch``.
    Returns the outcome label."""
    signals: Dict[str, SignalTask] = {
        plan.task: SignalTask(plan.task, NOT_SENT, plan.key) for plan in batch.not_sent}
    if isinstance(outcome, str) and outcome == NOTHING_SENT:
        ctx.signals = signals
        return "ok"
    if outcome is None:
        ctx.signals, ctx.signals_error = signals, "unavailable"
        return "unavailable"
    if isinstance(outcome, str):
        label, error = _OUTCOMES.get(outcome, ("error", "failed"))
        ctx.signals, ctx.signals_error = signals, error
        return label
    if not isinstance(outcome, SignalsOutcome):
        ctx.signals, ctx.signals_error = signals, "failed"
        return "error"
    label, error = _OUTCOMES.get(outcome.status, ("error", "failed"))
    ctx.signals_ms = _ms(outcome.ms)
    response = outcome.response
    if error is not None or response is None:
        ctx.signals, ctx.signals_error = signals, error or "failed"
        return label if error is not None else "error"
    for plan in batch.tasks:
        sent = frozenset(plan.text_ids)
        truncated = frozenset(i for i in batch.truncated if i in sent)
        model = response.model_for(plan.task)
        result = response.result(plan.task)
        if result is None:
            signals[plan.task] = SignalTask(plan.task, MISSING, plan.key, model=model, sent=sent,
                                            truncated=truncated)
            continue
        signals[plan.task] = SignalTask(
            task=plan.task,
            status=result.status,
            key=plan.key,
            ms=_ms(result.ms),
            model=model,
            texts={i: f.to_json() for i, f in result.texts.items() if i in sent},
            sent=sent,
            truncated=truncated,
        )
    ctx.signals, ctx.signals_error = signals, None
    return "ok"


def task_labels(task: SignalTask) -> Tuple[str, str]:
    """(task, status) with both bounded to known values."""
    name = task.task if task.task in TASKS else "other"
    status = task.status if task.status in TASK_STATUSES + (MISSING, NOT_SENT) else "other"
    return name, status
