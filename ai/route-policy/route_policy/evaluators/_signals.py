"""What the signal evaluators share: reading their task off ``ctx.signals``.

Not a declaring module (no evaluator type lives here). Every signal
evaluator asks for ONE task (``Evaluator.signal_requests``) and reads it
back with :func:`lookup`, which says either "here is the result" or why it
is not there -- the ``status`` its ``detail`` reports:

    <task status>    the sidecar's: ok | partial | deadline | skipped |
                     warming | error | unavailable
    not_sent         no text of the task fit the request caps
    missing          sent, but the answer carried no result for it
    timed out | failed | unavailable | not local
                     the call itself did not answer (``ctx.signals_error``)
    not fetched      no call was made for this request (a pipeline run
                     without the service)
    conflict         another evaluator of the ruleset asked for the same
                     task with other parameters, and its request was sent
    model mismatch   the sidecar answered with another model than the
                     evaluator's ``model`` pin

Only ``ok`` and ``partial`` carry features.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from ..signals.model import SignalRequest, SignalTask, model_matches

NOT_FETCHED, CONFLICT, MODEL_MISMATCH = "not fetched", "conflict", "model mismatch"


@dataclass(frozen=True)
class Lookup:
    name: str                    # the task asked for
    task: Optional[SignalTask]   # set when usable: features to read
    status: str                  # see the module docstring
    model: Optional[str]         # the model id the sidecar answered with, when it answered
    call: bool = False           # the status is the CALL's (ctx.signals_error / no call), not the task's

    @property
    def usable(self) -> bool:
        return self.task is not None

    def why(self) -> str:
        """An abstain reason: ``signals timed out`` (the call), ``nearest
        warming`` / ``pii unavailable`` (the task), ``injection model
        mismatch``."""
        return f"signals {self.status}" if self.call else f"{self.name} {self.status}"


def lookup(ctx: Any, request: SignalRequest, *, pin: Optional[str] = None) -> Lookup:
    """The result of ``request`` on ``ctx.signals``, or why there is none."""
    name = request.task
    if ctx.signals_error is not None:
        return Lookup(name, None, ctx.signals_error, None, call=True)
    task = ctx.signals.get(name)
    if task is None:
        return Lookup(name, None, NOT_FETCHED, None, call=True)
    if task.key != request.key:
        return Lookup(name, None, CONFLICT, task.model)
    if not task.usable:
        return Lookup(name, None, task.status, task.model)
    if not model_matches(pin, task.model):
        return Lookup(name, None, MODEL_MISMATCH, task.model)
    return Lookup(name, task, task.status, task.model)


def number(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def feature(task: SignalTask, text_id: str, key: str) -> Optional[float]:
    """One numeric feature of one text, or None (not scored, or junk)."""
    features = task.texts.get(text_id)
    return number(features.get(key)) if isinstance(features, dict) else None
