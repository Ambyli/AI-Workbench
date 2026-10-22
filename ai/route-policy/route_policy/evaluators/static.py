"""static -- a fixed vote, for tests and overrides.

Always votes for vote.route (with ``confidence``), or always abstains
(``abstain: true``). Useful to pin a draft ruleset's outcome while testing a
binding, as a high-priority override, or as a ``veto`` that takes a route
out of play (``role: veto``).
"""

from __future__ import annotations

from pydantic import Field

from .base import Evaluator, EvaluatorConfig, Outcome, OutputSpec, declares


class StaticConfig(EvaluatorConfig):
    abstain: bool = False
    confidence: float = Field(1.0, ge=0.0, le=1.0)
    reason: str = Field("fixed vote", min_length=1, max_length=500)


@declares(OutputSpec(
    type="static",
    summary="A fixed vote (or a fixed abstain).",
    votes="vote.route with `confidence`, every time -- unless `abstain: true`.",
    fields={},
))
class Static(Evaluator):
    Config = StaticConfig

    async def evaluate(self, ctx) -> Outcome:
        if self.config.abstain:
            return Outcome.abstain(self.config.reason, {})
        return Outcome.vote(self.config.reason, {}, confidence=self.config.confidence)
