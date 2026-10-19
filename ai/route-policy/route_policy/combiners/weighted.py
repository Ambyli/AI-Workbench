"""weighted -- the best-scoring route wins if it reaches the threshold.

Score per route = Σ weight × confidence over the non-abstaining votes for it.
The top route wins when its score is > 0 and at or above ``threshold``;
otherwise the ruleset's ``default_route``. A tie between top routes goes to
the one listed first in the ruleset's ``routes``. The deciding evaluator
reported is the first (in ruleset order) that voted for the winner.

    combiner: {type: weighted, threshold: 0.7}
"""

from __future__ import annotations

from typing import Dict, List

from pydantic import Field

from .base import Ballot, Choice, Combiner, CombinerConfig, registers


class WeightedConfig(CombinerConfig):
    threshold: float = Field(0.5, ge=0.0)


@registers("weighted")
class Weighted(Combiner):
    summary = ("Σ weight × confidence per route; the top route wins at or above `threshold`, "
               "else default_route. Force and veto apply first.")
    Config = WeightedConfig

    def choose(self, votes: List[Ballot], routes: List[str]) -> Choice:
        totals: Dict[str, float] = {}
        for b in votes:
            totals[b.route] = totals.get(b.route, 0.0) + b.weight * b.confidence
        if not totals:
            return Choice(None, "no evaluator voted")
        order = {route: i for i, route in enumerate(routes)}
        best = min(totals, key=lambda r: (-totals[r], order.get(r, len(order))))
        score = round(totals[best], 6)
        threshold = self.config.threshold
        if score > 0 and score >= threshold:
            voters = [b.evaluator for b in votes if b.route == best]
            return Choice(best, f"{best} scored {score:g} (threshold {threshold:g}) from {', '.join(voters)}",
                          voters[0])
        return Choice(None, f"top score {best} {score:g} is below the threshold {threshold:g}")
