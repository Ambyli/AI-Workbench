"""prompt_injection -- a jailbreak / prompt-injection score from the signals
sidecar (the ``injection`` task: a classifier over a head and a tail window
of each text).

Config::

    model: NeuralTrust/prompt-guard-oss-small   # optional pin: the model `threshold` was chosen for
    threshold: 0.9                              # vote when the highest score is at least this
    scope: current_message                      # or current_exchange: + this exchange's tool results
    max_tokens: 256                             # each window's length

``current_exchange`` adds every tool result after the latest user message (or
inside it: an Anthropic ``tool_result`` block) -- a fetched page or a file
that carries instructions (indirect injection).

**Log only, for now.** The ``default-signals`` draft runs it with role
``observe``: the score lands in ``evaluations.detail.score`` and routes
nothing. What a hit should do -- ``force`` local-private, ``veto`` a chain,
a different threshold -- is a later ruleset version, chosen from that data
(ROUTE_POLICY.md § The default-signals draft). Route-policy can only pick a
chain; refusing a request is not an engine action.

On a long message the injection model is the slowest of the sidecar's
(~340 ms at 2,000 characters on the box), so within the 0.1 s budget it is
often cut (``deadline`` / ``skipped``) -- an abstain, harmless while it only
observes.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import Field

from ..signals.model import SignalRequest, SignalText
from ._signals import feature, lookup
from .base import Evaluator, EvaluatorConfig, FieldSpec, Outcome, OutputSpec, declares

TASK = "injection"
WINDOWS = "head_tail"


class PromptInjectionConfig(EvaluatorConfig):
    model: Optional[str] = Field(None, min_length=1, max_length=256)
    threshold: float = Field(0.9, ge=0.0, le=1.0)
    scope: Literal["current_message", "current_exchange"] = "current_message"
    max_tokens: int = Field(256, ge=16, le=512)


@declares(OutputSpec(
    type="prompt_injection",
    summary="A jailbreak / prompt-injection score (the signals sidecar's `injection` task, a head and a tail "
            "window per text) on the latest user message, or on it and this exchange's tool results.",
    votes="vote.route when the highest score of the texts in scope is >= threshold; abstains below it, "
          "when nothing was scored (a timeout, a deadline cut, a missing answer), and on an answer from "
          "another model than the `model` pin.",
    fields={
        "model": FieldSpec("The model the sidecar answered with (null when it did not)", "string",
                           nullable=True),
        "status": FieldSpec("The injection task's status (evaluators/_signals.py)", "string"),
        "score": FieldSpec("The highest score, 0-1; null when no text was scored", "number", nullable=True),
        "scored_texts": FieldSpec("Texts that got a score", "integer"),
        "windows": FieldSpec("Windows scored across the texts (head + tail of max_tokens each; 1 for a "
                             "short text)", "integer"),
        "signals_ms": FieldSpec("The signals call's time; null when there was none", "number", nullable=True),
    },
))
class PromptInjection(Evaluator):
    Config = PromptInjectionConfig

    def _texts(self, ctx) -> List[SignalText]:
        latest = ctx.user_items[-1] if ctx.user_items else None
        out = [latest] if latest is not None and latest.text else []
        if self.config.scope == "current_exchange" and ctx.latest_user_position is not None:
            # >=: an Anthropic tool_result rides inside the latest user message itself.
            out += [item for item, _ in reversed(ctx.tool_items)
                    if item.text and item.position[0] >= ctx.latest_user_position]
        return out

    def _params(self) -> Dict[str, Any]:
        return {"max_tokens": self.config.max_tokens, "windows": WINDOWS}

    def _request(self, ctx) -> Optional[SignalRequest]:
        texts = self._texts(ctx)
        if not texts:
            return None
        return SignalRequest(TASK, tuple(texts), self._params())

    def signal_tasks(self) -> Dict[str, str]:
        return {TASK: SignalRequest(TASK, (), self._params()).key}

    def signal_requests(self, ctx) -> List[SignalRequest]:
        request = self._request(ctx)
        return [request] if request is not None else []

    async def evaluate(self, ctx) -> Outcome:
        request = self._request(ctx)
        detail = {"model": None, "status": "not_sent", "score": None, "scored_texts": 0, "windows": 0,
                  "signals_ms": ctx.signals_ms}
        if request is None:
            return Outcome.abstain("no text to score", detail)
        found = lookup(ctx, request, pin=self.config.model)
        detail.update(model=found.model, status=found.status)
        if not found.usable:
            return Outcome.abstain(found.why(), detail)
        scored = [t.id for t in request.texts if feature(found.task, t.id, "score") is not None]
        if not scored:
            return Outcome.abstain("no text was scored", detail)
        score = round(max(feature(found.task, i, "score") for i in scored), 6)
        windows = sum(int(feature(found.task, i, "windows") or 1) for i in scored)
        detail.update(score=score, scored_texts=len(scored), windows=windows)
        if score >= self.config.threshold:
            return Outcome.vote(f"injection score {score:.2f} >= {self.config.threshold:g}", detail,
                                confidence=score)
        return Outcome.abstain(f"injection score {score:.2f} < {self.config.threshold:g}", detail)
