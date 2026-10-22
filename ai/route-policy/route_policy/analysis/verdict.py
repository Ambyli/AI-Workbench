"""Turn per-route scores into one route: the ``llm_classifier``'s verdict.

The document classifier scores each criterion 1-10 (``hint: presence``:
10 = clearly present, 5 = uncertain, 1 = clearly absent; 7+ is its PASS). An
``llm_classifier`` asks one criterion per route, so the scores arrive keyed
by ROUTE, and :func:`choose` picks:

* the highest score at or above ``min_score`` wins, a tie going to the route
  listed first in ``order`` (the ruleset's ``routes`` order, as the analysis
  request carries the criteria) -- confidence ``score / 10``;
* nothing reaches ``min_score``: ``otherwise``, with confidence
  ``(10 - best score) / 9`` -- 1.0 when every criterion was clearly absent
  (all 1s), 4/9 when the best one was a 6, just under the default 7 -- or no
  route at all when ``otherwise`` is None;
* no score at all (every criterion errored): no route.

Pure; used by the analysis queue when it writes a verdict, and by the
benchmark (``ai/route-policy/bench``) so it scores exactly as production.
"""

from __future__ import annotations

from typing import Mapping, Optional, Sequence, Tuple


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def choose(scores: Mapping[str, Optional[int]], *, order: Sequence[str], min_score: int,
           otherwise: Optional[str]) -> Tuple[Optional[str], Optional[float]]:
    """(route, confidence) from ``scores`` (route -> 1..10, None = errored).

    ``order`` is the tie order; a route in ``scores`` that ``order`` does
    not list ranks after every listed one, in ``scores``' own order."""
    known = {route: score for route, score in scores.items() if score is not None}
    if not known:
        return None, None
    ranked = [r for r in order if r in known] + [r for r in known if r not in order]
    best: Optional[Tuple[str, int]] = None
    for route in ranked:
        score = known[route]
        if score >= min_score and (best is None or score > best[1]):
            best = (route, score)
    if best is not None:
        return best[0], _clamp(best[1] / 10.0)
    if otherwise is None:
        return None, None
    return otherwise, _clamp((10 - max(known.values())) / 9.0)
