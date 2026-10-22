"""Run a compiled ruleset on one request: evaluators -> combiner -> invariants.

``await run(ruleset, ctx)`` -> :class:`Decision`:

1. every evaluator runs, in ruleset order (all of them, even after a force
   vote -- every vote is logged, so a rule's hit rate can be read in Trino
   whatever decided the turn); each one's time is measured,
   and an evaluator that raises is recorded as an abstain with its error;
2. a vote for a route the ruleset does not list is turned into an abstain
   with an error -- an evaluator can never smuggle a non-chain route past the
   combiner;
3. the ruleset's combiner picks one route (``combiners.base``);
4. the invariants (``engine.invariants``) check it -- the FRESH decision;
5. a ``pin: turn`` ruleset whose turn already has a first decision
   (``ctx.turn_pin``, put there by the service) keeps that decision's route,
   unless the fresh route is local-private: a privacy force, or a request
   for local-private, always wins over a pin. The invariants run again on
   the pinned route, so they still have the last word;
6. ``apply`` is true only for an ``enforce`` ruleset whose invariants held.

**The pin** changes nothing but the route (and ``apply`` / the reason): every
evaluator still runs and every vote is logged, so the turn's later calls show
what the rules would have said. :attr:`Decision.pinned_from` names the turn's
first decision whenever there was one -- whether the pin changed the route
(``decided_by == "pin"``, reason ``pinned to this turn's first decision
(<route>, <id>); fresh: <route> — <reason>``), the fresh route already was the
turn's (the reason is the combiner's), or a fresh local-private beat it (the
reason says so). :attr:`Decision.pin_outcome` says which (``changed`` |
``kept`` | ``private``), for the metrics.

Pure: no database, no metrics. The API layer stores and counts the result.
:meth:`Decision.response` is the POST /decide body (ROUTE_POLICY_PLAN § 7,
the API contract), and with ``with_detail`` the POST /evaluate body -- the
pin adds no key to either.

Around a run, the service asks the same ruleset two more questions
(``Evaluator.analysis_lookups`` / ``analysis_requests``, gathered by
:func:`analysis_lookups` and :func:`analysis_requests` here): which stored
verdicts to read onto the context BEFORE the run, and what to classify in
the background AFTER the answer. An evaluator that raises in either is
skipped and logged -- the decision never depends on them. A call that is not
its turn's first asks for nothing: the turn's first call already asked for
its conversation prefix.

And a third (phase 5): what to fetch from the ``signals`` sidecar BEFORE the
run (``Evaluator.signal_requests``, merged by :func:`signal_requests` into
ONE request, ``signals.model.merge``). An evaluator that raises there is
skipped and logged too; its ``evaluate`` then abstains on the missing
answer. Every call asks, a turn's later calls included: the sidecar caches
by text, so a repeated text costs a lookup.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..analysis.model import AnalysisRequest
from ..combiners.base import Ballot
from ..evaluators.base import Outcome
from ..rulesets.model import CompiledRuleset
from ..signals.model import SignalBatch, SignalRequest, merge
from . import invariants
from .context import RequestContext, TurnPin

logger = logging.getLogger("route_policy.engine")

ERROR_MAX_CHARS = 500

# Decision.decided_by when the turn's pin replaced the fresh route.
PIN = "pin"
# Decision.pin_outcome (the route_policy_pinned_total{outcome} label).
PIN_CHANGED, PIN_KEPT, PIN_PRIVATE = "changed", "kept", "private"


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
    reason: str                       # the combiner's explanation (+ invariant notes, + the pin)
    decided_by: Optional[str]         # the deciding evaluator id; "pin"; None for the default
    overrides: Tuple[str, ...]        # invariants that changed the route
    latency_ms: float
    # The turn's first decision, when this one is not it (a `pin: turn`
    # ruleset); and what the pin did: changed | kept | private.
    pinned_from: Optional[uuid.UUID] = None
    pin_outcome: Optional[str] = None

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
    """An exception as it is logged and stored (``evaluations.error``, the
    vote's reason, ``decisions.error``): its TYPE name only. Evaluators read
    the conversation, and a message can quote it -- ``common.pii``'s grid
    lookup once raised ``KeyError('ſ')``, a character of the user's text."""
    text = type(exc).__name__
    return text if len(text) <= ERROR_MAX_CHARS else text[: ERROR_MAX_CHARS - 1] + "…"


def analysis_lookups(ruleset: CompiledRuleset, ctx: RequestContext) -> List[str]:
    """Every stored verdict the ruleset's evaluators will read, deduplicated,
    in ruleset order. Empty for a ruleset without analysis evaluators."""
    keys: List[str] = []
    for entry in ruleset.entries:
        try:
            wanted = entry.evaluator.analysis_lookups(ctx)
        except Exception as exc:  # noqa: BLE001 -- its evaluate() then abstains on an empty map
            logger.warning("engine: %s@%d evaluator %s analysis_lookups raised: %s",
                           ruleset.id, ruleset.version, entry.id, _error_text(exc))
            continue
        keys.extend(k for k in wanted if k not in keys)
    return keys


def _turn_pin(ruleset: CompiledRuleset, ctx: RequestContext) -> Optional[TurnPin]:
    """The turn's first decision when ``ruleset`` pins turns, else None."""
    return ctx.turn_pin if ruleset.pin == "turn" else None


def analysis_requests(ruleset: CompiledRuleset, ctx: RequestContext) -> List[AnalysisRequest]:
    """What the ruleset's evaluators want classified in the background, in
    ruleset order (provenance not filled in yet: the service adds the ruleset
    and the decision). Nothing for a call that is not its turn's first."""
    if _turn_pin(ruleset, ctx) is not None:
        return []
    out: List[AnalysisRequest] = []
    for entry in ruleset.entries:
        try:
            out.extend(entry.evaluator.analysis_requests(ctx))
        except Exception as exc:  # noqa: BLE001 -- nothing is classified for it this turn
            logger.warning("engine: %s@%d evaluator %s analysis_requests raised: %s",
                           ruleset.id, ruleset.version, entry.id, _error_text(exc))
    return out


def signal_requests(ruleset: CompiledRuleset, ctx: RequestContext) -> Optional[SignalBatch]:
    """The ruleset's sidecar requests as ONE batch (``signals.model.merge``),
    or None when no evaluator asked for anything -- then there is no call.
    A request for a task already asked for with other parameters is left
    out and logged; its evaluator abstains (``conflict``)."""
    requests: List[SignalRequest] = []
    for entry in ruleset.entries:
        try:
            requests.extend(entry.evaluator.signal_requests(ctx))
        except Exception as exc:  # noqa: BLE001 -- its evaluate() then abstains on the missing answer
            logger.warning("engine: %s@%d evaluator %s signal_requests raised: %s",
                           ruleset.id, ruleset.version, entry.id, _error_text(exc))
    if not requests:
        return None
    batch = merge(requests)
    if batch.conflicts and (ruleset.id, ruleset.version) not in _CONFLICTS_LOGGED:
        _CONFLICTS_LOGGED.add((ruleset.id, ruleset.version))   # a ruleset property: once per process
        logger.warning("engine: %s@%d asks the signals sidecar for %s twice with different parameters; "
                       "the first request is sent", ruleset.id, ruleset.version, ", ".join(batch.conflicts))
    return batch


_CONFLICTS_LOGGED: set = set()


def signal_warmups(rulesets: List[CompiledRuleset]) -> List[SignalBatch]:
    """What to send the sidecar at startup for these rulesets: one batch per
    distinct warm-up (a task name can appear once per request), texts
    empty."""
    seen: Dict[str, SignalRequest] = {}
    for ruleset in rulesets:
        for entry in ruleset.entries:
            try:
                for request in entry.evaluator.signal_warmups():
                    seen.setdefault(request.key, request)
            except Exception as exc:  # noqa: BLE001 -- it just starts cold
                logger.warning("engine: %s@%d evaluator %s signal_warmups raised: %s",
                               ruleset.id, ruleset.version, entry.id, _error_text(exc))
    return [merge([request], keep_empty=True) for request in seen.values()]


async def run(ruleset: CompiledRuleset, ctx: RequestContext) -> Decision:
    started = time.perf_counter()
    votes: List[Vote] = []
    ballots: List[Ballot] = []
    for entry in ruleset.entries:
        t0 = time.perf_counter()
        error: Optional[str] = None
        try:
            outcome = await entry.evaluator.evaluate(ctx)
        except Exception as exc:  # noqa: BLE001 -- one broken evaluator never breaks the decision
            error = _error_text(exc)
            logger.warning("engine: %s@%d evaluator %s raised: %s", ruleset.id, ruleset.version, entry.id, error)
            outcome = Outcome.abstain(f"error: {error}", {})
        else:
            if not isinstance(outcome, Outcome):
                error = f"evaluate() returned {type(outcome).__name__}, not an Outcome"
                logger.warning("engine: %s@%d evaluator %s: %s", ruleset.id, ruleset.version, entry.id, error)
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
    route, allowed, overrides, decided_by = checked.route, checked.allowed, checked.overrides, combination.decided_by
    pinned_from: Optional[uuid.UUID] = None
    pin_outcome: Optional[str] = None
    pin = _turn_pin(ruleset, ctx)
    if pin is not None:
        pinned_from = pin.decision_id
        first = f"this turn's first decision ({pin.route}, {pin.decision_id})"
        if route == pin.route:
            pin_outcome = PIN_KEPT
        elif route == invariants.PRIVATE_ROUTE:
            # Privacy always wins: a pin never moves a turn off local-private.
            pin_outcome = PIN_PRIVATE
            reason = f"{reason}; {invariants.PRIVATE_ROUTE} beats {first}"
        else:
            held = invariants.enforce(ctx.requested_model, pin.route, ruleset.routes, ruleset.default_route)
            pin_outcome, decided_by = PIN_CHANGED, PIN
            reason = f"pinned to {first}; fresh: {route} — {reason}"
            if held.notes:
                reason = f"{reason}; " + "; ".join(held.notes)
            route, allowed = held.route, held.allowed
            overrides = tuple(dict.fromkeys(overrides + held.overrides))
    return Decision(
        ruleset=ruleset,
        requested_model=ctx.requested_model,
        route=route,
        apply=ruleset.mode == "enforce" and allowed,
        votes=votes,
        scores=combination.scores,
        reason=reason,
        decided_by=decided_by,
        overrides=overrides,
        latency_ms=_ms(time.perf_counter() - started),
        pinned_from=pinned_from,
        pin_outcome=pin_outcome,
    )
