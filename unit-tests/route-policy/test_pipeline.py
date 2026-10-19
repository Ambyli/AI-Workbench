"""The engine pipeline: the /decide body is exactly the contract, every
evaluator runs, a broken evaluator never breaks a decision."""

from __future__ import annotations

import asyncio

from rp_helpers import DECIDE_KEYS, VOTE_KEYS, context, default_ruleset, run, tool_round, user

from route_policy.engine.pipeline import run as run_pipeline
from route_policy.evaluators.base import Evaluator


def test_response_is_exactly_the_contract():
    d = run(default_ruleset(), [user("go"), *tool_round("sandbox-mcp_exec")])
    body = d.response("6f0e8c1e-8f7e-4c55-9a39-0f3f7ab5d001")
    assert set(body) == DECIDE_KEYS
    assert body["route"] == "local-code" and body["mode"] == "enforce" and body["apply"] is True
    assert body["ruleset"] == {"id": "default", "version": 2}
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
