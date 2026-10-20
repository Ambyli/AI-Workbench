"""The llm_classifier evaluator (route_policy/evaluators/llm_classifier.py).

* config validation (criteria texts, otherwise, bounds, unknown keys) and
  the ruleset-level checks (criteria / otherwise are routes, no vote.route);
* the fingerprint covers what changes a verdict, and only that;
* verdict keys: one per prefix k = n ... n - max_age, stable as the
  conversation continues and across tool rounds of one message, different
  as soon as an earlier message is edited;
* the vote: the newest usable verdict, its age in the reason, the
  route chosen by the verdict, every abstain reason;
* analysis_requests: one request for the latest prefix, carrying the
  rendered transcript, unless that verdict is already known.
"""

from __future__ import annotations

import asyncio
import copy

import pytest
from pydantic import ValidationError

from rp_helpers import RULESETS_DIR, assistant, compile_doc, context, seed, tool_round, user

from common.conversation import render_transcript
from route_policy.analysis.model import Verdict
from route_policy.evaluators.llm_classifier import (
    LlmClassifier,
    LlmClassifierConfig,
    fingerprint,
    input_key,
)
from route_policy.rulesets.validate import RulesetInvalid

CRITERIA = {
    "local-code": "The latest user request is about code.",
    "local-reasoning": "The latest user request needs careful multi-step reasoning.",
    "local-private": "The conversation contains personal data about a real person.",
}


def config(**kw) -> LlmClassifierConfig:
    base = {"model": "qwen3.5-4b", "criteria": dict(CRITERIA), "otherwise": "local-general"}
    base.update(kw)
    return LlmClassifierConfig.model_validate(base)


def evaluator(**kw) -> LlmClassifier:
    return LlmClassifier("topic-llm", config(**kw))


def conversation(n: int) -> list:
    """n user messages with an assistant answer after each but the last."""
    out = []
    for i in range(1, n + 1):
        out.append(user(f"message number {i}"))
        if i < n:
            out.append(assistant(f"answer {i}"))
    return out


def key_for(ev: LlmClassifier, messages: list, k: int) -> str:
    h = ev.config.history
    return input_key(ev.fingerprint, render_transcript(messages, exchanges=h.exchanges, max_chars=h.max_chars,
                                                       upto_user=k))


def verdict(key: str, k: int, route="local-code", status="ok", confidence=0.9, scores=None) -> Verdict:
    return Verdict(key, status, route, confidence,
                   scores if scores is not None else {"local-code": 9, "local-reasoning": 2, "local-private": 1},
                   k, "qwen3.5-4b")


def run(ev: LlmClassifier, messages: list, analyses=None, lookup_error=None):
    ctx = context(messages)
    ctx.analyses = analyses or {}
    ctx.analysis_lookup_error = lookup_error
    return asyncio.run(ev.evaluate(ctx))


# -- config ----------------------------------------------------------------------------


def test_defaults_and_bounds():
    c = config()
    assert (c.min_score, c.history.exchanges, c.history.max_chars, c.max_age_exchanges) == (7, 4, 6000, 2)
    assert list(c.criteria) == ["local-code", "local-reasoning", "local-private"]   # the tie order


@pytest.mark.parametrize("change, expected", [
    ({"criteria": {}}, "criteria"),
    ({"criteria": {"local-code": "  "}}, "is empty"),
    ({"criteria": {"local-code": "x" * 201}}, "at most 200"),
    ({"criteria": {"local-code": "same", "local-reasoning": "same"}}, "same criterion text"),
    ({"otherwise": "local-code"}, "also a criterion route"),
    ({"min_score": 0}, "min_score"),
    ({"min_score": 11}, "min_score"),
    ({"history": {"exchanges": 0}}, "exchanges"),
    ({"history": {"max_chars": 100}}, "max_chars"),
    ({"history": {"exchanges": 2, "surprise": 1}}, "surprise"),
    ({"max_age_exchanges": -1}, "max_age_exchanges"),
    ({"mode": "sync"}, "mode"),
    ({"model": ""}, "model"),
])
def test_config_validation(change, expected):
    with pytest.raises(ValidationError) as info:
        config(**change)
    assert expected in str(info.value)


def _draft() -> dict:
    (llm,) = [r for r in seed().compiled if r.id == "default-llm"]
    return copy.deepcopy(llm.document)


def test_ruleset_checks_routes_and_refuses_vote_route():
    doc = _draft()
    doc["evaluators"][-1]["config"]["criteria"]["local-claude"] = "Asks for Claude."
    with pytest.raises(RulesetInvalid, match=r"topic-llm\)\.config: criteria / otherwise name \['local-claude'\]"):
        compile_doc(doc)
    doc = _draft()
    doc["evaluators"][-1]["config"]["otherwise"] = "elsewhere"
    with pytest.raises(RulesetInvalid, match=r"name \['elsewhere'\]"):
        compile_doc(doc)
    doc = _draft()
    doc["evaluators"][-1]["vote"]["route"] = "local-code"
    with pytest.raises(RulesetInvalid, match="chooses its own route; remove vote.route"):
        compile_doc(doc)


# -- fingerprint and keys -----------------------------------------------------------------


def test_fingerprint_covers_what_changes_a_verdict():
    base = fingerprint(config())
    assert base == fingerprint(config()) and len(base) == 64
    for changed in (config(model="muse-glimmer"), config(criteria={**CRITERIA, "local-code": "Code."}),
                    config(criteria={"local-code": CRITERIA["local-code"]}), config(otherwise=None),
                    config(min_score=8), config(history={"exchanges": 3}), config(history={"max_chars": 5000})):
        assert fingerprint(changed) != base
    assert fingerprint(config(max_age_exchanges=5)) == base   # only which verdicts are read
    # The mapping's order never matters: jsonb does not keep it.
    assert fingerprint(config(criteria=dict(reversed(list(CRITERIA.items()))))) == base


def test_the_tie_order_is_the_rulesets_routes_not_the_mapping():
    doc = _draft()
    topic = doc["evaluators"][-1]["config"]
    topic["criteria"] = dict(reversed(list(topic["criteria"].items())))   # what a jsonb round trip may do
    compiled = compile_doc(doc).entries[-1].evaluator
    (original,) = [r for r in seed().compiled if r.id == "default-llm"]
    baked = original.entries[-1].evaluator
    assert compiled.order == baked.order == ["local-code", "local-reasoning", "local-private"]
    assert compiled.fingerprint == baked.fingerprint
    msgs = conversation(2)
    (a,) = compiled.analysis_requests(context(msgs))
    (b,) = baked.analysis_requests(context(msgs))
    assert list(a.criteria) == list(b.criteria) == ["local-code", "local-reasoning", "local-private"]
    assert a.input_key == b.input_key
    # Standalone (no ruleset), the mapping's own order.
    assert LlmClassifier("x", config(criteria=dict(reversed(list(CRITERIA.items()))))).order == [
        "local-private", "local-reasoning", "local-code"]


def test_lookups_are_the_prefixes_n_back_to_n_minus_max_age():
    ev = evaluator()
    msgs = conversation(4)
    ctx = context(msgs)
    keys = ev.analysis_lookups(ctx)
    assert keys == [key_for(ev, msgs, k) for k in (4, 3, 2)] and len(set(keys)) == 3
    assert evaluator(max_age_exchanges=0).analysis_lookups(context(msgs)) == [key_for(ev, msgs, 4)]
    assert len(evaluator(max_age_exchanges=10).analysis_lookups(context(msgs))) == 4   # never below k=1
    assert ev.analysis_lookups(context([])) == []
    assert ev.analysis_lookups(context([assistant("hello")])) == []


def test_keys_are_stable_as_the_conversation_continues_and_across_tool_rounds():
    ev = evaluator()
    two = conversation(2)
    later = [*two, *tool_round("sandbox-mcp_exec"), assistant("done"), user("next one")]
    assert key_for(ev, later, 2) == key_for(ev, two, 2)
    # Tool rounds of message 2 ask for the same latest-prefix key.
    rounds = [*two, *tool_round("sandbox-mcp_exec")]
    assert ev.analysis_lookups(context(rounds))[0] == ev.analysis_lookups(context(two))[0]
    (a,) = ev.analysis_requests(context(two))
    (b,) = ev.analysis_requests(context(rounds))
    assert a.input_key == b.input_key and a.text == b.text


def test_an_edited_earlier_message_changes_every_later_key():
    ev = evaluator()
    original = conversation(3)
    edited = copy.deepcopy(original)
    edited[0]["content"] = "message number 1, edited"
    assert key_for(ev, edited, 1) != key_for(ev, original, 1)
    assert key_for(ev, edited, 2) != key_for(ev, original, 2)
    # A verdict stored for the original message 2 is not read for the edit.
    known = {key_for(ev, original, 2): verdict(key_for(ev, original, 2), 2)}
    assert run(ev, original, known).voted
    out = run(ev, edited, known)
    assert not out.voted and out.reason == "no verdict in the last 3 message(s)"


# -- the vote --------------------------------------------------------------------------------


def test_votes_the_newest_usable_verdict_with_its_age():
    ev = evaluator()
    msgs = conversation(3)
    k2, k1 = key_for(ev, msgs, 2), key_for(ev, msgs, 1)
    out = run(ev, msgs, {k2: verdict(k2, 2, "local-reasoning", confidence=0.8,
                                     scores={"local-code": 2, "local-reasoning": 8, "local-private": 1}),
                         k1: verdict(k1, 1, "local-code")})
    assert out.voted and out.route == "local-reasoning" and out.confidence == 0.8
    assert out.reason == "local-reasoning scored 8/10 (qwen3.5-4b, 1 message(s) ago)"
    assert out.detail == {"model": "qwen3.5-4b", "user_messages": 3, "looked_up": 3, "lookup_error": None,
                          "input_key": k2, "exchange_index": 2, "age_exchanges": 1, "status": "ok",
                          "scores": {"local-code": 2, "local-reasoning": 8, "local-private": 1}}
    k3 = key_for(ev, msgs, 3)
    out = run(ev, msgs, {k3: verdict(k3, 3, "local-code")})
    assert out.reason == "local-code scored 9/10 (qwen3.5-4b, this message)" and out.detail["age_exchanges"] == 0


def test_otherwise_and_routeless_verdicts():
    ev = evaluator()
    msgs = conversation(2)
    k1 = key_for(ev, msgs, 1)
    low = {"local-code": 3, "local-reasoning": 1, "local-private": 1}
    out = run(ev, msgs, {k1: verdict(k1, 1, "local-general", confidence=7 / 9, scores=low)})
    assert out.voted and out.route == "local-general"
    assert out.reason == "no criterion reached 7/10, otherwise local-general (qwen3.5-4b, 1 message(s) ago)"
    out = run(ev, msgs, {k1: verdict(k1, 1, None, confidence=None, scores=low)})
    assert not out.voted and out.reason == "verdict 1 message(s) ago: no criterion reached 7/10"
    assert out.detail["input_key"] == k1


def test_a_failed_newer_verdict_falls_back_to_an_older_usable_one():
    ev = evaluator()
    msgs = conversation(3)
    k2, k1 = key_for(ev, msgs, 2), key_for(ev, msgs, 1)
    failed = verdict(k2, 2, None, status="timeout", confidence=None, scores={})
    out = run(ev, msgs, {k2: failed, k1: verdict(k1, 1, "local-code")})
    assert out.voted and out.route == "local-code" and out.detail["exchange_index"] == 1
    out = run(ev, msgs, {k2: failed})
    assert not out.voted and out.reason == "verdict failed: timeout" and out.detail["status"] == "timeout"
    incomplete = verdict(k2, 2, "local-code", status="incomplete",
                         scores={"local-code": 8, "local-reasoning": None, "local-private": None})
    assert run(ev, msgs, {k2: incomplete}).voted


def test_abstains_when_stale_unknown_or_unreadable():
    ev = evaluator()
    msgs = conversation(5)
    k2 = key_for(ev, msgs, 2)   # 3 messages old: past max_age_exchanges = 2
    out = run(ev, msgs, {k2: verdict(k2, 2)})
    assert not out.voted and out.reason == "no verdict in the last 3 message(s)"
    assert out.detail["input_key"] is None and out.detail["looked_up"] == 3
    out = run(ev, msgs, {}, lookup_error="timed out")
    assert not out.voted and out.reason == "lookup timed out" and out.detail["lookup_error"] == "timed out"
    out = run(ev, [assistant("hi")])
    assert not out.voted and out.reason == "no user message" and out.detail["user_messages"] == 0


# -- analysis requests ------------------------------------------------------------------------


def test_one_request_for_the_latest_prefix():
    ev = evaluator()
    msgs = [*conversation(2), *tool_round("search_web")]
    ctx = context(msgs)
    ctx.chat_id, ctx.message_id = "chat-1", "msg-2"
    (req,) = ev.analysis_requests(ctx)
    text = render_transcript(msgs, exchanges=4, max_chars=6000, upto_user=2)
    assert req.text == text and req.input_chars == len(text) and req.exchange_index == 2
    assert req.input_key == key_for(ev, msgs, 2) and req.fingerprint == ev.fingerprint
    assert (req.model, req.criteria, req.otherwise, req.min_score) == ("qwen3.5-4b", CRITERIA, "local-general", 7)
    assert (req.evaluator_id, req.chat_id, req.message_id) == ("topic-llm", "chat-1", "msg-2")
    assert (req.ruleset_id, req.ruleset_version, req.decision_id) == (None, None, None)  # the service adds them
    assert "message number 2" not in repr(req)   # the transcript never shows in a log line


def test_no_request_when_the_verdict_is_known_or_there_is_nothing_to_classify():
    ev = evaluator()
    msgs = conversation(2)
    ctx = context(msgs)
    k2 = key_for(ev, msgs, 2)
    ctx.analyses = {k2: verdict(k2, 2)}
    assert ev.analysis_requests(ctx) == []
    assert ev.analysis_requests(context([])) == []


def test_the_draft_ruleset_wiring():
    (llm,) = [r for r in seed().compiled if r.id == "default-llm"]
    (live,) = [r for r in seed().compiled if r.id == "default"]
    assert (llm.version, llm.mode, llm.routes, llm.default_route) == (
        1, "shadow", live.routes, live.default_route)
    assert [(e.id, e.type, e.role) for e in llm.entries] == [
        ("privacy", "regex_text", "force"), ("code-text", "regex_text", "vote"),
        ("reasoning-text", "regex_text", "vote"), ("code-tools", "tool_calls", "vote"),
        ("topic-llm", "llm_classifier", "vote")]
    # The first four are `default`'s, config for config.
    for mine, theirs in zip(llm.document["evaluators"][:4], live.document["evaluators"][:4]):
        assert mine["config"] == theirs["config"] and mine["vote"] == theirs["vote"], mine["id"]
    topic = llm.entries[-1].evaluator.config
    assert topic.model == "qwen3.5-4b" and list(topic.criteria) == ["local-code", "local-reasoning", "local-private"]
    assert (topic.otherwise, topic.min_score, topic.history.exchanges, topic.history.max_chars,
            topic.max_age_exchanges) == ("local-general", 7, 4, 6000, 2)
    assert all(len(text) <= 200 for text in topic.criteria.values())
    assert llm.combiner.type_name == "priority"
    assert (RULESETS_DIR / "default-llm.yaml").exists()
