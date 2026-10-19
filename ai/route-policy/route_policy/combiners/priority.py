"""priority -- the first non-abstaining vote wins, in ruleset order.

The `default` ruleset uses it: privacy (a force), then the code and
reasoning keywords, then the tool-call and cooldown rules -- so a turn that
asks for a proof stays on ``local-reasoning`` in a chat that has been coding,
and only an otherwise ``local-general`` turn is held on ``local-code``.
Weights and confidences are reported in ``scores`` but never change the
outcome.
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
