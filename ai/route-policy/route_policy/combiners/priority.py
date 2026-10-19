"""priority -- the first non-abstaining vote wins, in ruleset order.

The `default` ruleset uses it to reproduce today's routing: the router's
decision priorities (privacy 300 > code 200 > reasoning 150 > general 10)
become the evaluator order, and the code-tool remap and cooldown, which only
ever turn a ``local-general`` turn into ``local-code``, sit after the three
keyword evaluators. Weights and confidences are reported in ``scores`` but
never change the outcome.
"""

from __future__ import annotations

from typing import List

from .base import Ballot, Choice, Combiner, registers


@registers("priority")
class Priority(Combiner):
    summary = "The first non-abstaining vote in ruleset order wins; force and veto apply first."

    def choose(self, votes: List[Ballot], routes: List[str]) -> Choice:
        if votes:
            first = votes[0]
            return Choice(first.route, f"{first.evaluator}: {first.reason}", first.evaluator)
        return Choice(None, "no evaluator voted")
