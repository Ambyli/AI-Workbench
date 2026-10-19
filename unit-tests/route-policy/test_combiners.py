"""Combiners: the shared frame (force, veto, default) and each type's choice."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from route_policy.combiners.base import Ballot, combiner_registry, scores_of
from route_policy.combiners.priority import Priority
from route_policy.combiners.weighted import Weighted, WeightedConfig
from route_policy.combiners.base import CombinerConfig

ROUTES = ["local-general", "local-code", "local-reasoning", "local-private"]


def b(evaluator, route, role="vote", weight=1.0, confidence=1.0, abstained=False):
    return Ballot(evaluator, "static", role, None if abstained else route, weight,
                  0.0 if abstained else confidence, abstained, f"{evaluator} says {route}")


def priority():
    return Priority(CombinerConfig())


def weighted(threshold=0.5):
    return Weighted(WeightedConfig(threshold=threshold))


def test_registry():
    assert set(combiner_registry()) == {"priority", "weighted"}


def test_priority_first_non_abstaining_vote_wins():
    c = priority().combine([b("a", "local-code", abstained=True), b("b", "local-reasoning"), b("c", "local-code")],
                           ROUTES, "local-general")
    assert c.route == "local-reasoning" and c.decided_by == "b"
    assert c.reason == "b: b says local-reasoning"
    assert c.scores == {"local-reasoning": 1.0, "local-code": 1.0}


def test_priority_default_when_nobody_votes():
    c = priority().combine([b("a", "local-code", abstained=True)], ROUTES, "local-general")
    assert c.route == "local-general" and c.decided_by is None
    assert c.reason == "no evaluator voted; default route local-general" and c.scores == {}


def test_force_wins_wherever_it_sits():
    ballots = [b("code", "local-code"), b("pii", "local-private", role="force")]
    for combiner in (priority(), weighted(0.0)):
        c = combiner.combine(ballots, ROUTES, "local-general")
        assert c.route == "local-private" and c.decided_by == "pii"
        assert c.reason.startswith("forced by pii:")


def test_abstaining_force_does_nothing():
    c = priority().combine([b("pii", "local-private", role="force", abstained=True), b("code", "local-code")],
                           ROUTES, "local-general")
    assert c.route == "local-code"


def test_veto_removes_a_route_and_never_beats_force():
    c = priority().combine([b("no-code", "local-code", role="veto"), b("code", "local-code"),
                            b("reason", "local-reasoning")], ROUTES, "local-general")
    assert c.route == "local-reasoning" and c.vetoed == ("local-code",) and "vetoed local-code" in c.reason
    c = priority().combine([b("no-private", "local-private", role="veto"),
                            b("pii", "local-private", role="force")], ROUTES, "local-general")
    assert c.route == "local-private"


def test_vetoed_default_falls_to_the_first_route_in_play():
    c = priority().combine([b("v", "local-general", role="veto")], ROUTES, "local-general")
    assert c.route == "local-code"
    every = [b(f"v{i}", r, role="veto") for i, r in enumerate(ROUTES)]
    assert priority().combine(every, ROUTES, "local-general").route == "local-general"


def test_weighted_threshold_ties_and_scores():
    ballots = [b("a", "local-code", weight=0.4), b("b", "local-code", weight=0.4),
               b("c", "local-reasoning", weight=0.7, confidence=1.0)]
    c = weighted(0.5).combine(ballots, ROUTES, "local-general")
    assert c.route == "local-code" and c.decided_by == "a"
    assert c.scores == {"local-code": 0.8, "local-reasoning": 0.7}
    c = weighted(0.9).combine(ballots, ROUTES, "local-general")
    assert c.route == "local-general" and "below the threshold" in c.reason
    tie = [b("r", "local-reasoning", weight=0.6), b("c", "local-code", weight=0.6)]
    assert weighted(0.5).combine(tie, ROUTES, "local-general").route == "local-code"  # routes order
    zero = [b("z", "local-code", weight=0.0)]
    assert weighted(0.0).combine(zero, ROUTES, "local-general").route == "local-general"


def test_weighted_uses_confidence():
    c = weighted(0.5).combine([b("a", "local-code", confidence=0.3)], ROUTES, "local-general")
    assert c.route == "local-general"
    with pytest.raises(ValidationError):
        WeightedConfig(threshold=-1)
    with pytest.raises(ValidationError):
        WeightedConfig(threshold=0.5, extra=1)


def test_scores_skip_abstains_and_vetoes():
    assert scores_of([b("a", "local-code", abstained=True), b("v", "local-code", role="veto"),
                      b("f", "local-private", role="force", weight=2.0)]) == {"local-private": 2.0}
