"""The engine pipeline: the /decide body is exactly the contract, every
evaluator runs, a broken evaluator never breaks a decision; a ``pin: turn``
ruleset keeps the turn's first route (privacy and the invariants still win),
and a call that is not its turn's first asks for no classification."""

from __future__ import annotations

import asyncio
import copy
import uuid

import pytest

from rp_helpers import DECIDE_KEYS, VOTE_KEYS, assistant, compile_doc, context, default_ruleset, run, seed, \
    tool_round, user

from route_policy.engine.context import TurnPin
from route_policy.engine.pipeline import analysis_requests
from route_policy.engine.pipeline import run as run_pipeline
from route_policy.evaluators.base import Evaluator

FIRST = uuid.UUID("0b1c2d3e-0000-4000-8000-000000000001")


def pinned(messages, route, ruleset=None, **kw):
    """The decision for a LATER call of a turn whose first decision chose ``route``."""
    ctx = context(messages, **kw)
    ctx.turn_pin = TurnPin(FIRST, route)
    return asyncio.run(run_pipeline(ruleset or default_ruleset(), ctx))


def test_response_is_exactly_the_contract():
    d = run(default_ruleset(), [user("go"), *tool_round("sandbox-mcp_exec")])
    body = d.response("6f0e8c1e-8f7e-4c55-9a39-0f3f7ab5d001")
    assert set(body) == DECIDE_KEYS
    assert body["route"] == "local-code" and body["mode"] == "enforce" and body["apply"] is True
    assert body["ruleset"] == {"id": "default", "version": 3}
    assert [v["evaluator"] for v in body["votes"]] == ["privacy", "code-text", "reasoning-text", "code-tools",
                                                       "sticky-code"]
    for vote in body["votes"]:
        assert set(vote) == VOTE_KEYS
    tools = body["votes"][3]
    assert tools == {**tools, "type": "tool_calls", "route": "local-code", "weight": 1.0, "confidence": 1.0,
                     "role": "vote", "abstained": False, "reason": "called sandbox-mcp_exec in this exchange"}
    assert body["scores"] == {"local-code": 2.0}  # code-tools + sticky-code
    assert isinstance(body["latency_ms"], float)


def test_evaluate_body_adds_detail_and_error():
    body = run(default_ruleset(), [user("hi")]).response(None, with_detail=True)
    assert body["decision_id"] is None
    for vote in body["votes"]:
        assert set(vote) == VOTE_KEYS | {"detail", "error"}
    assert body["votes"][0]["detail"]["rule"] == "privacy_kw"


def test_abstains_report_null_route_and_confidence():
    vote = run(default_ruleset(), [user("hi")]).votes[0]
    assert vote.abstained and vote.route is None and vote.confidence is None and vote.weight == 1.0


class _Boom(Evaluator):
    type_name = "boom"

    async def evaluate(self, ctx):
        raise RuntimeError("kaput")


def test_a_raising_evaluator_is_an_abstain_with_its_error():
    ruleset = default_ruleset()
    first = ruleset.entries[0]
    boom = type(first)("boom", "boom", "force", "local-private", 1.0, _Boom("boom", None))
    hacked = type(ruleset)(**{**ruleset.__dict__, "entries": [boom, *ruleset.entries[1:]]})
    d = asyncio.run(run_pipeline(hacked, context([user("write python")])))
    assert d.route == "local-code"           # the rest still decided
    assert d.votes[0].abstained and d.votes[0].error == "RuntimeError: kaput"
    assert d.votes[0].reason == "error: RuntimeError: kaput"
    assert d.errors == ["boom: RuntimeError: kaput"]


def test_every_evaluator_runs_even_after_a_force():
    d = run(default_ruleset(), [user("my ssn and a python script")])
    assert d.route == "local-private"
    assert [v.abstained for v in d.votes] == [False, False, True, True, False]
    assert d.decided_by == "privacy"


# -- the turn pin ------------------------------------------------------------------------


def test_a_pinned_call_keeps_the_contract_and_every_vote():
    fresh = run(default_ruleset(), [user("go"), *tool_round("sandbox-mcp_exec")])
    d = pinned([user("go"), *tool_round("sandbox-mcp_exec")], "local-general")
    body = d.response("6f0e8c1e-8f7e-4c55-9a39-0f3f7ab5d001")
    assert set(body) == DECIDE_KEYS and all(set(v) == VOTE_KEYS for v in body["votes"])
    assert (body["route"], body["apply"]) == ("local-general", True)
    # The votes and scores are the fresh decision's: only the route is held.
    assert [v["route"] for v in body["votes"]] == [v["route"] for v in fresh.response(None)["votes"]]
    assert body["scores"] == fresh.scores == {"local-code": 2.0}
    assert (d.decided_by, d.pin_outcome, d.pinned_from) == ("pin", "changed", FIRST)
    assert d.reason == (f"pinned to this turn's first decision (local-general, {FIRST}); "
                        f"fresh: local-code — {fresh.reason}")


def test_privacy_and_a_requested_private_beat_the_pin():
    d = pinned([user("my SSN is 123-45-6789")], "local-code")
    assert (d.route, d.apply, d.pin_outcome, d.pinned_from) == ("local-private", True, "private", FIRST)
    assert d.decided_by == "privacy"
    assert d.reason.endswith(f"; local-private beats this turn's first decision (local-code, {FIRST})")
    d = pinned([user("write a python script")], "local-code", requested_model="local-private")
    assert (d.route, d.pin_outcome, d.overrides) == ("local-private", "private", ("private",))


def test_a_private_turn_stays_private():
    d = pinned([user("my SSN is 1"), assistant(), user("continue")], "local-private")
    assert (d.route, d.apply, d.pin_outcome, d.decided_by) == ("local-private", True, "changed", "pin")


def test_the_invariants_run_again_on_the_pinned_route():
    # A first decision made under another version whose routes this one lacks.
    d = pinned([user("hello")], "local-claude")
    assert (d.route, d.apply, d.pin_outcome) == ("local-private", False, "changed")
    assert d.overrides == ("not_a_chain",) and "'local-claude' is not one of this ruleset's chain aliases" in d.reason
    assert "local-private instead, not applied" in d.errors[-1]


def test_a_ruleset_without_pin_ignores_the_turns_first_decision():
    doc = copy.deepcopy(default_ruleset().document)
    del doc["pin"]
    unpinned = compile_doc(doc)
    assert unpinned.pin is None
    d = pinned([user("write a python script")], "local-general", ruleset=unpinned)
    assert (d.route, d.pinned_from, d.pin_outcome) == ("local-code", None, None)


@pytest.mark.parametrize("pin", [True, False])
def test_a_later_call_of_a_turn_asks_for_no_classification(pin):
    (llm,) = [r for r in seed().compiled if r.id == "default-llm"]
    assert llm.pin == "turn"
    ctx = context([user("hello"), assistant(), user("tell me about the moon")])
    if pin:
        ctx.turn_pin = TurnPin(FIRST, "local-general")
    requests = analysis_requests(llm, ctx)
    assert len(requests) == (0 if pin else 1)
