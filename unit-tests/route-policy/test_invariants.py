"""The two invariants (engine/invariants.py), alone and through the pipeline."""

from __future__ import annotations

import asyncio

from rp_helpers import compile_doc, context, default_ruleset, run, user

from route_policy.engine.invariants import PRIVATE_ROUTE, enforce, fail_safe_route
from route_policy.evaluators.base import Evaluator, Outcome
from route_policy.engine.pipeline import run as run_pipeline

ROUTES = ["local-general", "local-code", "local-reasoning", "local-private"]


def test_requested_private_stays_private():
    checked = enforce("local-private", "local-code", ROUTES, "local-general")
    assert checked.route == PRIVATE_ROUTE and checked.allowed and checked.overrides == ("private",)
    assert enforce("local-private", "local-private", ROUTES, "local-general").overrides == ()
    # Even when the ruleset does not list it: it is the safe place regardless.
    assert enforce("local-private", "x", ["local-general"], "local-general").route == PRIVATE_ROUTE


def test_route_must_be_a_chain_alias_of_the_ruleset():
    checked = enforce("auto", "claude-opus-5-5", ROUTES, "local-general")
    assert checked.route == "local-private" and not checked.allowed and checked.overrides == ("not_a_chain",)
    assert fail_safe_route(["local-general", "local-code"], "local-general") == "local-general"
    assert enforce("auto", "local-code", ROUTES, "local-general") == enforce("auto", "local-code", ROUTES,
                                                                              "local-general")
    assert enforce("auto", "local-code", ROUTES, "local-general").allowed


def test_pipeline_applies_the_private_invariant():
    d = run(default_ruleset(), [user("write a python script")], requested_model="local-private")
    assert d.route == "local-private" and d.overrides == ("private",)
    assert "requested local-private stays local-private" in d.reason
    assert d.apply is False  # shadow


def test_enforce_mode_applies_only_when_allowed():
    doc = {**default_ruleset().document, "id": "enforced", "mode": "enforce"}
    enforced = compile_doc(doc)
    assert run(enforced, [user("hello")]).apply is True
    assert run(enforced, [user("write python")], requested_model="local-private").apply is True


class _Rogue(Evaluator):
    """Votes for a route its ruleset does not list (as a phase-4 evaluator could)."""

    type_name = "rogue"

    async def evaluate(self, ctx):
        return Outcome.vote("rogue", {}, route="claude-opus-5-5")


def test_a_vote_outside_the_routes_is_an_error_not_a_route():
    ruleset = default_ruleset()
    entry = ruleset.entries[1]
    rogue = type(entry)(entry.id, entry.type, entry.role, entry.route, entry.weight, _Rogue("x", None))
    hacked = type(ruleset)(**{**ruleset.__dict__, "entries": [rogue]})
    d = asyncio.run(run_pipeline(hacked, context([user("hi")])))
    assert d.route == "local-general" and d.votes[0].abstained
    assert "not one of this ruleset's routes" in d.votes[0].error
