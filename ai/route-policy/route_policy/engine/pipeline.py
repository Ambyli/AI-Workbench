"""Run a compiled ruleset on one request: evaluators -> combiner -> invariants.

``await run(ruleset, ctx)`` -> :class:`Decision`:

1. every evaluator runs, in ruleset order (all of them, even after a force
   vote -- the log is the point of shadow mode); each one's time is measured,
   and an evaluator that raises is recorded as an abstain with its error;
2. a vote for a route the ruleset does not list is turned into an abstain
   with an error -- an evaluator can never smuggle a non-chain route past the
   combiner;
3. the ruleset's combiner picks one route (``combiners.base``);
4. the invariants (``engine.invariants``) have the last word;
5. ``apply`` is true only for an ``enforce`` ruleset whose invariants held.

Pure: no database, no metrics. The API layer stores and counts the result.
:meth:`Decision.response` is the POST /decide body (ROUTE_POLICY_PLAN § 7,
the API contract), and with ``with_detail`` the POST /evaluate body.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..combiners.base import Ballot
from ..evaluators.base import Outcome
from ..rulesets.model import CompiledRuleset
from . import invariants
from .context import RequestContext

logger = logging.getLogger("route_policy.engine")

ERROR_MAX_CHARS = 500


def _ms(seconds: float) -> float:
    return round(seconds * 1000.0, 3)


@dataclass(frozen=True)
class Vote:
    """One evaluator's part in a decision -- the contract's ``votes[]`` item."""

    evaluator: str
    type: str
    role: str
    route: Optional[str]           # None when abstained
    weight: float
    confidence: Optional[float]    # None when abstained
    abstained: bool
    reason: str
    latency_ms: float
    detail: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    def public(self, with_detail: bool = False) -> Dict[str, Any]:
        out = {
            "evaluator": self.evaluator,
            "type": self.type,
            "route": self.route,
            "weight": self.weight,
            "confidence": self.confidence,
            "role": self.role,
            "abstained": self.abstained,
            "reason": self.reason,
            "latency_ms": self.latency_ms,
        }
        if with_detail:
            out["detail"] = self.detail
            out["error"] = self.error
        return out


@dataclass(frozen=True)
class Decision:
    ruleset: CompiledRuleset
    requested_model: str
    route: str
    apply: bool
    votes: List[Vote]
    scores: Dict[str, float]
    reason: str                       # the combiner's explanation (+ invariant notes)
    decided_by: Optional[str]
    overrides: Tuple[str, ...]        # invariants that changed the route
    latency_ms: float

    @property
    def mode(self) -> str:
        return self.ruleset.mode

    @property
    def errors(self) -> List[str]:
        out = [f"{v.evaluator}: {v.error}" for v in self.votes if v.error]
        if "not_a_chain" in self.overrides:
            out.append(self.reason)
        return out

    def response(self, decision_id: Optional[str], with_detail: bool = False) -> Dict[str, Any]:
        return {
            "decision_id": decision_id,
            "route": self.route,
            "mode": self.mode,
            "apply": self.apply,
            "ruleset": self.ruleset.ref(),
            "votes": [v.public(with_detail) for v in self.votes],
            "scores": self.scores,
            "latency_ms": self.latency_ms,
        }


def _error_text(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text if len(text) <= ERROR_MAX_CHARS else text[: ERROR_MAX_CHARS - 1] + "…"


async def run(ruleset: CompiledRuleset, ctx: RequestContext) -> Decision:
    started = time.perf_counter()
    votes: List[Vote] = []
    ballots: List[Ballot] = []
    for entry in ruleset.entries:
        t0 = time.perf_counter()
        error: Optional[str] = None
        try:
            outcome = await entry.evaluator.evaluate(ctx)
            if not isinstance(outcome, Outcome):
                raise TypeError(f"evaluate() returned {type(outcome).__name__}, not an Outcome")
        except Exception as exc:  # noqa: BLE001 -- one broken evaluator never breaks the decision
            error = _error_text(exc)
            logger.warning("engine: %s@%d evaluator %s raised: %s", ruleset.id, ruleset.version, entry.id, error)
            outcome = Outcome.abstain(f"error: {error}", {})
        elapsed = _ms(time.perf_counter() - t0)

        route: Optional[str] = None
        if outcome.voted:
            route = outcome.route or entry.route
            if route not in ruleset.routes:
                error = f"voted for {route!r}, which is not one of this ruleset's routes"
                outcome = Outcome.abstain(f"error: {error}", outcome.detail)
                route = None
        abstained = not outcome.voted
        confidence = None if abstained else float(outcome.confidence)
        votes.append(Vote(entry.id, entry.type, entry.role, route, entry.weight, confidence, abstained,
                          outcome.reason, elapsed, dict(outcome.detail), error))
        ballots.append(Ballot(entry.id, entry.type, entry.role, route, entry.weight,
                              confidence if confidence is not None else 0.0, abstained, outcome.reason))

    combination = ruleset.combiner.combine(ballots, ruleset.routes, ruleset.default_route)
    checked = invariants.enforce(ctx.requested_model, combination.route, ruleset.routes, ruleset.default_route)
    reason = combination.reason if not checked.notes else f"{combination.reason}; " + "; ".join(checked.notes)
    return Decision(
        ruleset=ruleset,
        requested_model=ctx.requested_model,
        route=checked.route,
        apply=ruleset.mode == "enforce" and checked.allowed,
        votes=votes,
        scores=combination.scores,
        reason=reason,
        decided_by=combination.decided_by,
        overrides=checked.overrides,
        latency_ms=_ms(time.perf_counter() - started),
    )
