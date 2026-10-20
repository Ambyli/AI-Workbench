"""analysis.verdict.choose: per-route 1-10 scores -> (route, confidence).

The rule the analysis queue stores and the benchmark scores by: the highest
score at or above min_score wins (a tie goes to the route listed first),
confidence score/10; nothing reaches it -> `otherwise` with confidence
(10 - best)/9, or no route when there is no `otherwise`; no score at all ->
no route.
"""

from __future__ import annotations

import pytest

from route_policy.analysis.verdict import choose

ORDER = ["local-code", "local-reasoning", "local-private"]


@pytest.mark.parametrize("scores, otherwise, expected", [
    # A clear winner.
    ({"local-code": 9, "local-reasoning": 3, "local-private": 1}, "local-general", ("local-code", 0.9)),
    # The highest at or above min_score, not the first.
    ({"local-code": 7, "local-reasoning": 10, "local-private": 1}, "local-general", ("local-reasoning", 1.0)),
    # Exactly min_score passes.
    ({"local-code": 7, "local-reasoning": 1, "local-private": 1}, None, ("local-code", 0.7)),
    # A tie goes to the earlier route in `order`, whatever the dict order.
    ({"local-private": 8, "local-reasoning": 8, "local-code": 2}, "local-general", ("local-reasoning", 0.8)),
    # Nothing reaches min_score: otherwise, confidence from the best miss.
    ({"local-code": 1, "local-reasoning": 1, "local-private": 1}, "local-general", ("local-general", 1.0)),
    ({"local-code": 6, "local-reasoning": 2, "local-private": 1}, "local-general", ("local-general", 4 / 9)),
    # ... or no route without an otherwise.
    ({"local-code": 6, "local-reasoning": 2, "local-private": 1}, None, (None, None)),
    # An errored criterion (None) is ignored; the rest still decide.
    ({"local-code": None, "local-reasoning": 8, "local-private": None}, "local-general", ("local-reasoning", 0.8)),
    ({"local-code": None, "local-reasoning": 2, "local-private": None}, "local-general", ("local-general", 8 / 9)),
    # Every criterion errored: no route, even with an otherwise.
    ({"local-code": None, "local-reasoning": None, "local-private": None}, "local-general", (None, None)),
    ({}, "local-general", (None, None)),
])
def test_choose(scores, otherwise, expected):
    route, confidence = choose(scores, order=ORDER, min_score=7, otherwise=otherwise)
    assert route == expected[0]
    if expected[1] is None:
        assert confidence is None
    else:
        assert confidence == pytest.approx(expected[1])


def test_min_score_and_routes_outside_the_order():
    assert choose({"a": 5, "b": 4}, order=["a", "b"], min_score=4, otherwise=None) == ("a", 0.5)
    assert choose({"a": 5, "b": 4}, order=["a", "b"], min_score=6, otherwise="z")[0] == "z"
    # A route `order` does not list ranks after the listed ones on a tie.
    assert choose({"x": 9, "a": 9}, order=["a"], min_score=7, otherwise=None) == ("a", 0.9)
    assert choose({"x": 10, "a": 9}, order=["a"], min_score=7, otherwise=None) == ("x", 1.0)


def test_confidence_is_always_in_0_1():
    for best in range(1, 11):
        route, confidence = choose({"a": best}, order=["a"], min_score=7, otherwise="z")
        assert 0.0 <= confidence <= 1.0
        assert route == ("a" if best >= 7 else "z")
