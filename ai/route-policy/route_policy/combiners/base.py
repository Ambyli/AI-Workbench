"""Combiners: turn the evaluators' votes into ONE route.

Every combiner runs the same frame (:meth:`Combiner.combine`); a type only
implements :meth:`Combiner.choose`, the choice among ordinary votes:

1. **force** -- the first non-abstaining ``role: force`` vote (in ruleset
   order) wins outright, whatever else voted. That is how ``privacy`` sends a
   PII prompt to ``local-private`` no matter what code or reasoning signals
   say. A force vote is never vetoed.
2. **veto** -- every non-abstaining ``role: veto`` vote takes its route out of
   play for this request.
3. **choose** -- the type's rule over the remaining ``role: vote`` votes
   (priority: the first; weighted: the best score at or above a threshold).
4. **default** -- nothing chosen: the ruleset's ``default_route``; if that is
   vetoed, the first route in ``routes`` that is not; if every route is
   vetoed, ``default_route`` anyway (a veto never leaves a request unrouted).

``role: observe`` is in none of the steps: an observe evaluator runs and is
logged with its would-be route (``evaluations.route``), and no combiner reads
it -- no score, no force, no veto. It is how a signal (the jailbreak score)
is measured without routing anything, in a shadow or an enforced ruleset.

``scores`` is the same for every type: Σ weight × confidence per route over
the non-abstaining vote and force votes (vetoes are not scores). It is what
/decide returns and what route-policy-db stores.

Registered by type name with ``@registers``; GET /evaluator-types lists them
under ``combiner_types``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, ClassVar, Dict, List, Optional, Sequence, Type, TypeVar

from pydantic import BaseModel, ConfigDict

ROLE_VOTE, ROLE_FORCE, ROLE_VETO, ROLE_OBSERVE = "vote", "force", "veto", "observe"
ROLES = (ROLE_VOTE, ROLE_FORCE, ROLE_VETO, ROLE_OBSERVE)


@dataclass(frozen=True)
class Ballot:
    """One evaluator's result as a combiner sees it."""

    evaluator: str
    type: str
    role: str
    route: Optional[str]      # None when abstained
    weight: float
    confidence: float
    abstained: bool
    reason: str

    @property
    def counts(self) -> bool:
        return not self.abstained and bool(self.route)


@dataclass(frozen=True)
class Choice:
    """What a combiner type picked among ordinary votes (route None = nothing)."""

    route: Optional[str]
    reason: str
    decided_by: Optional[str] = None


@dataclass(frozen=True)
class Combination:
    route: str
    scores: Dict[str, float]
    reason: str
    decided_by: Optional[str]          # the deciding evaluator id; None for the default
    vetoed: tuple = ()


class CombinerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def scores_of(ballots: Sequence[Ballot]) -> Dict[str, float]:
    totals: Dict[str, float] = {}
    for b in ballots:
        if b.counts and b.role in (ROLE_VOTE, ROLE_FORCE):
            totals[b.route] = totals.get(b.route, 0.0) + b.weight * b.confidence
    return {route: round(total, 6) for route, total in totals.items()}


class Combiner:
    type_name: ClassVar[str] = ""
    summary: ClassVar[str] = ""
    Config: ClassVar[Type[CombinerConfig]] = CombinerConfig

    def __init__(self, config: CombinerConfig) -> None:
        self.config = config

    def choose(self, votes: List[Ballot], routes: List[str]) -> Choice:
        """Pick among ordinary votes. ``votes`` holds only non-abstaining
        ``role: vote`` ballots for routes still in play, in ruleset order;
        ``routes`` is the routes still in play, in ruleset order."""
        raise NotImplementedError

    def combine(self, ballots: Sequence[Ballot], routes: Sequence[str], default_route: str) -> Combination:
        ballots = list(ballots)
        scores = scores_of(ballots)
        for b in ballots:
            if b.counts and b.role == ROLE_FORCE:
                return Combination(b.route, scores, f"forced by {b.evaluator}: {b.reason}", b.evaluator)
        vetoed = tuple(dict.fromkeys(b.route for b in ballots if b.counts and b.role == ROLE_VETO))
        in_play = [r for r in routes if r not in vetoed]
        votes = [b for b in ballots if b.counts and b.role == ROLE_VOTE and b.route in in_play]
        choice = self.choose(votes, in_play)
        note = f"; vetoed {', '.join(vetoed)}" if vetoed else ""
        if choice.route is not None:
            return Combination(choice.route, scores, choice.reason + note, choice.decided_by, vetoed)
        if default_route not in vetoed:
            fallback = default_route
        else:
            fallback = in_play[0] if in_play else default_route
        return Combination(fallback, scores, f"{choice.reason}; default route {fallback}{note}", None, vetoed)

    @classmethod
    def describe(cls) -> Dict[str, Any]:
        return {"summary": cls.summary, "config_schema": cls.Config.model_json_schema()}


_REGISTRY: Dict[str, Type[Combiner]] = {}
C = TypeVar("C", bound=Type[Combiner])


def registers(name: str) -> Callable[[C], C]:
    def attach(cls: C) -> C:
        existing = _REGISTRY.get(name)
        if existing is not None and existing is not cls:
            raise ValueError(f"a combiner type {name!r} is already registered")
        cls.type_name = name
        _REGISTRY[name] = cls
        return cls

    return attach


def combiner_registry() -> Dict[str, Type[Combiner]]:
    # Imported here, not at module top: the two modules import this one.
    from . import priority, weighted  # noqa: F401

    return dict(_REGISTRY)
