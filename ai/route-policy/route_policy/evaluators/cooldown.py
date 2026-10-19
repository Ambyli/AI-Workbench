"""cooldown -- a vote that holds for a few exchanges after the last relevant one.

A port of the LiteLLM hook's code cooldown
(``ai/litellm/hooks/routing/code_route.py::code_route_decision`` and
``exchange_relevance``), configured by the ruleset instead of
CODE_ROUTE_COOLDOWN / CODE_ROUTE_TOOL_REGEX / the router-config mount:

* the history is split into EXCHANGES, one per user message (the message plus
  every round after it);
* an exchange is RELEVANT when one of the evaluators named in ``relevant_if``
  says so (``Evaluator.exchange_relevance``; asked in the order listed, the
  first reason wins -- the default ruleset lists ``code-tools`` before
  ``code-text``, the hook's order);
* the current exchange relevant -> vote;
* otherwise walk back: a relevant exchange with fewer than ``exchanges``
  irrelevant ones between it and the current one -> vote; ``exchanges`` or
  more -> abstain, the cooldown has run out; none relevant -> abstain.

Everything is recomputed from the request's history -- no per-chat state, so
two copies of the service and a restart agree. (Whether it should count from
stored decisions instead is ROUTE_POLICY_PLAN § 9's open question.)

The reasons are the hook's, word for word, so a shadow decision and a
``[route]`` log line can be compared directly.
"""

from __future__ import annotations

from typing import List, Mapping, Optional

from pydantic import Field, field_validator

from .base import Evaluator, EvaluatorConfig, FieldSpec, Outcome, OutputSpec, declares


class CooldownConfig(EvaluatorConfig):
    # Irrelevant exchanges in a row (after the last relevant one) before the
    # vote lapses. 0: only the current exchange counts.
    exchanges: int = Field(ge=0, le=1000)
    # Ids of evaluators in the same ruleset that judge one exchange.
    relevant_if: List[str] = Field(min_length=1)

    @field_validator("relevant_if")
    @classmethod
    def _unique(cls, value: List[str]) -> List[str]:
        if len(set(value)) != len(value):
            raise ValueError("relevant_if lists an evaluator twice")
        return value


@declares(OutputSpec(
    type="cooldown",
    summary="Keeps voting for vote.route while the conversation's recent exchanges are relevant, "
            "for `exchanges` irrelevant exchanges after the last relevant one.",
    votes="vote.route when the current exchange is relevant, or an earlier one is and fewer than "
          "`exchanges` irrelevant exchanges followed it; abstains once the cooldown has run out or "
          "when no exchange was ever relevant.",
    fields={
        "exchanges": FieldSpec("The cooldown: irrelevant exchanges allowed after the last relevant one", "integer"),
        "relevant_if": FieldSpec("The evaluators asked, in order", "array"),
        "relevant_now": FieldSpec("Whether the current exchange is relevant", "boolean"),
        "last_relevant_ago": FieldSpec("Exchanges back from the current one to the last relevant one "
                                       "(0 = the current one); null when none was", "integer", nullable=True),
        "irrelevant_since": FieldSpec("Irrelevant exchanges between that one and the current one; "
                                      "null when none was relevant", "integer", nullable=True),
        "why": FieldSpec("Why the last relevant exchange counted; null when none was", "string", nullable=True),
        "expired": FieldSpec("True when a relevant exchange exists but the cooldown has run out", "boolean"),
    },
    notes=("relevant_if must name evaluators whose type offers exchange relevance "
           "(GET /evaluator-types: output.relevance is not null).",),
))
class Cooldown(Evaluator):
    Config = CooldownConfig

    def __init__(self, evaluator_id: str, config: CooldownConfig) -> None:
        super().__init__(evaluator_id, config)
        self.judges: List[Evaluator] = []

    def bind(self, siblings: Mapping[str, Optional[Evaluator]]) -> None:
        judges = []
        for ref in self.config.relevant_if:
            if ref not in siblings:
                raise ValueError(f"relevant_if names {ref!r}, which is not an evaluator of this ruleset")
            other = siblings[ref]
            if other is None:  # declared, but invalid -- its own error says why
                raise ValueError(f"relevant_if names {ref!r}, which is invalid")
            if other is self:
                raise ValueError("relevant_if names the cooldown itself")
            if not other.supports_relevance():
                raise ValueError(f"relevant_if names {ref!r}, whose type {other.type_name!r} "
                                 f"does not judge single exchanges")
            judges.append(other)
        self.judges = judges

    def relevance(self, text: str, rounds: List[dict]) -> Optional[str]:
        """Why one exchange counts: the first judge that says it does."""
        for judge in self.judges:
            why = judge.exchange_relevance(text, rounds)
            if why is not None:
                return why
        return None

    def _detail(self, relevant_now: bool, ago: Optional[int], irrelevant: Optional[int],
                why: Optional[str], expired: bool) -> dict:
        return {
            "exchanges": self.config.exchanges,
            "relevant_if": list(self.config.relevant_if),
            "relevant_now": relevant_now,
            "last_relevant_ago": ago,
            "irrelevant_since": irrelevant,
            "why": why,
            "expired": expired,
        }

    async def evaluate(self, ctx) -> Outcome:
        cooldown = self.config.exchanges
        exchanges = ctx.exchanges
        if not exchanges:
            return Outcome.abstain("no user message", self._detail(False, None, None, None, False))
        current = self.relevance(*exchanges[-1])
        if current is not None:
            return Outcome.vote(f"{current} in this exchange", self._detail(True, 0, 0, current, False))
        irrelevant = 0
        for text, rounds in reversed(exchanges[:-1]):
            why = self.relevance(text, rounds)
            if why is not None:
                detail = self._detail(False, irrelevant + 1, irrelevant, why, irrelevant >= cooldown)
                if irrelevant < cooldown:
                    return Outcome.vote(
                        f"{why} {irrelevant + 1} exchange(s) ago; {irrelevant}/{cooldown} irrelevant since", detail)
                return Outcome.abstain(
                    f"cooldown: {irrelevant}/{cooldown} irrelevant exchanges since the last one that {why}", detail)
            irrelevant += 1
        return Outcome.abstain("no exchange has been relevant", self._detail(False, None, None, None, False))
