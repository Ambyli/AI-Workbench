"""bench.points / bench.baseline: decision points from fabricated chats.

k indexing, the messages up to k, gaps, answer ids, the weak-label
precedence, ``rules_route`` from the REAL ``rulesets/default.yaml`` run
offline, trimming, the stratified sample, and the command end to end.
"""

from __future__ import annotations

import asyncio

import pytest

from bench_fixtures import Branch, chat_record, out_call, out_result, out_text, tool_answer

from bench import baseline as baseline_mod
from bench import points as points_mod
from bench.jsonl import read_jsonl, write_jsonl

from common.conversation import render_transcript


@pytest.fixture(scope="module")
def rules():
    return baseline_mod.RuleBaseline.from_file()


def build(chat, rules, decisions=None, **kw):
    index = points_mod.DecisionIndex.from_records(decisions or [])
    return asyncio.run(points_mod.chat_points(chat, baseline=rules, decisions=index, **kw))


# -- points ------------------------------------------------------------------------------


def test_one_point_per_user_message_with_k_messages_gap_and_answer(rules):
    b = Branch()
    b.user("hello")                 # T0 + 30
    b.assistant("hi")               # + 5
    b.user("how are you?")          # + 30
    b.assistant("fine")
    b.user("bye", gap=100.0)
    pts = build(b.chat(), rules)
    assert [p["k"] for p in pts] == [1, 2, 3]
    assert [p["user_index"] for p in pts] == [1, 2, 3]
    assert [p["user_message_id"] for p in pts] == ["m1", "m3", "m5"]
    assert [p["answer_message_id"] for p in pts] == ["m2", "m4", None]
    assert [p["point_id"] for p in pts] == ["chat-1:m1", "chat-1:m3", "chat-1:m5"]
    assert [p["gap_s"] for p in pts] == [None, 35.0, 105.0]
    for p in pts:
        assert p["messages"][-1]["role"] == "user"
        assert p["trimmed_exchanges"] == 0
    assert pts[1]["messages"] == [{"role": "user", "content": "hello"},
                                  {"role": "assistant", "content": "hi"},
                                  {"role": "user", "content": "how are you?"}]
    # What the evaluator renders for the point is the prefix itself.
    assert render_transcript(pts[2]["messages"], exchanges=4, max_chars=6000, upto_user=pts[2]["k"]) \
        == render_transcript(pts[2]["messages"], exchanges=4, max_chars=6000)


def test_k_counts_the_tool_image_user_message_user_index_does_not(rules):
    b = Branch()
    b.user("screenshot please")
    b.assistant("x", output=[out_call("c1", "sandbox-mcp_preview"), out_result("c1", "img", images=["u"]),
                             out_text("here")])
    b.user("thanks")
    pts = build(b.chat(), rules)
    assert [(p["user_index"], p["k"]) for p in pts] == [(1, 1), (2, 3)]
    assert pts[1]["messages"][-1] == {"role": "user", "content": "thanks"}


def test_point_flags(rules):
    b = Branch()
    b.user("one")
    b.assistant("", error=True)
    b.user("two")
    b.assistant("cited", sources=3)
    b.user("three", context_summary=True)
    pts = build(chat_record(b.messages, context_compaction=True), rules)
    assert [p["answer_error"] for p in pts] == [True, False, False]
    assert [p["answer_sources"] for p in pts] == [0, 3, 0]
    assert [p["compacted"] for p in pts] == [False, False, True]
    assert pts[2]["k"] == 1   # the checkpoint dropped the two earlier exchanges


def test_trim_keeps_the_last_exchanges_and_counts_the_cut(rules):
    b = Branch()
    b.user("build an app")
    tool_answer(b, "sandbox-mcp_run")
    for text in ("a", "b", "c"):
        b.user(text)
        b.assistant("ok")
    b.user("d")
    full = build(b.chat(), rules)
    trimmed = build(b.chat(), rules, keep_exchanges=2)
    last_full, last_trim = full[-1], trimmed[-1]
    assert last_full["k"] == 5 and last_trim["k"] == 2 and last_trim["trimmed_exchanges"] == 3
    assert [m["content"] for m in last_trim["messages"] if m["role"] == "user"] == ["c", "d"]
    # Labels and the rules' route come from the FULL history.
    assert last_trim["rules_route"] == last_full["rules_route"]
    assert points_mod.trim_exchanges([], 2) == ([], 0)


# -- the weak labels -----------------------------------------------------------------------


@pytest.mark.parametrize("privacy, tool, decision, expected", [
    (True, "sandbox-mcp_run", "local-general", ("local-private", "privacy_rule")),
    (False, "sandbox-mcp_run", "local-general", ("local-code", "code_tool")),
    (False, None, "local-reasoning", ("local-reasoning", "decision")),
    (False, None, None, (None, None)),
])
def test_weak_label_precedence(privacy, tool, decision, expected):
    assert baseline_mod.weak_label(privacy, tool, decision) == expected


def test_weak_labels_from_a_chat(rules):
    b = Branch()
    b.user("my ssn is 000-00-0000, fix my script")      # privacy beats the code tool
    tool_answer(b, "sandbox-mcp_run")
    b.user("now make it faster")
    tool_answer(b, "run_command", call_id="call-2")      # code tool beats the decision
    b.user("what is the weather like?")
    b.assistant("sunny")                                  # decision
    b.user("and tomorrow?")
    b.assistant("rain")                                   # nothing logged
    decisions = [
        {"chat_id": "chat-1", "message_id": "m4", "chosen_route": "local-general",
         "created_at": "2026-10-01T10:00:00+00:00", "shadow_of": None},
        {"chat_id": "chat-1", "message_id": "m6", "chosen_route": "local-reasoning",
         "created_at": "2026-10-01T10:05:00+00:00", "shadow_of": None},
        {"chat_id": "chat-1", "message_id": "m6", "chosen_route": "local-code",   # a later tool round
         "created_at": "2026-10-01T10:05:09+00:00", "shadow_of": None},
        {"chat_id": "chat-1", "message_id": "m6", "chosen_route": "local-private",  # a shadow ruleset
         "created_at": "2026-10-01T10:04:00+00:00", "shadow_of": "d-primary"},
    ]
    pts = build(b.chat(), rules, decisions)
    assert [(p["weak_label"], p["weak_source"]) for p in pts] == [
        ("local-private", "privacy_rule"),
        ("local-code", "code_tool"),
        ("local-reasoning", "decision"),
        (None, None),
    ]
    assert [p["decision_route"] for p in pts] == [None, "local-general", "local-reasoning", None]
    assert pts[0]["answer_tools"] == ["sandbox-mcp_run"]
    assert pts[0]["privacy_kw_route"] == "local-private"
    assert all(p["privacy_kw_route"] is None for p in pts[1:])


def test_decision_index_ignores_rows_without_ids():
    index = points_mod.DecisionIndex.from_records([
        {"chat_id": "c", "message_id": None, "chosen_route": "local-code"},
        {"chat_id": "c", "message_id": "a", "chosen_route": None},
        {"chat_id": "c", "message_id": "a2", "chosen_route": "local-code"},
    ])
    assert len(index) == 1
    assert index.route("c", "a2") == "local-code"
    assert index.route("c", None) is None and index.route(None, "a2") is None


# -- rules_route: the real default ruleset, offline ---------------------------------------


@pytest.mark.parametrize("text, route, decided_by", [
    ("write a python script that renames files", "local-code", "code-text"),
    ("prove that the square root of 2 is irrational", "local-reasoning", "reasoning-text"),
    ("what is the capital of France?", "local-general", None),
    ("here is the customer address and bank account number", "local-private", "privacy"),
])
def test_rules_route_on_one_message(rules, text, route, decided_by):
    b = Branch()
    b.user(text)
    (point,) = build(b.chat(), rules)
    assert (point["rules_route"], point["rules_decided_by"]) == (route, decided_by)
    assert point["rules_ruleset"] == f"default@{rules.ruleset.version}"


def test_rules_route_sticks_to_code_after_a_sandbox_call(rules):
    b = Branch()
    b.user("hi")
    tool_answer(b, "sandbox-mcp_run")
    b.user("great, what is the capital of France?")
    b.assistant("Paris")
    pts = build(b.chat(), rules)
    assert (pts[1]["rules_route"], pts[1]["rules_decided_by"]) == ("local-code", "sticky-code")
    assert pts[1]["weak_label"] is None


def test_the_attached_files_block_reaches_the_rules(rules):
    """A file name in Open WebUI's <attached_files> block is what the code
    keywords match -- the converter is why the benchmark sees it."""
    b = Branch()
    b.user("what does this say?", files=[{"type": "file", "id": "f1", "url": "/api/v1/files/f1",
                                          "name": "settings.yaml"}])
    native, = build(b.chat(), rules)
    legacy, = build(b.chat(function_calling="legacy"), rules)
    assert native["rules_route"] == "local-code"
    assert legacy["rules_route"] == "local-general"


def test_baseline_needs_its_two_evaluators(rules):
    with pytest.raises(ValueError, match="regex_text"):
        baseline_mod.RuleBaseline(rules.ruleset, privacy_evaluator="nope")
    with pytest.raises(ValueError, match="tool_calls"):
        baseline_mod.RuleBaseline(rules.ruleset, code_tools_evaluator="privacy")
    assert rules.code_tool(["other", "sandbox-mcp_exec"]) == "sandbox-mcp_exec"
    assert rules.code_tool(["sandbox-mcp"]) is None   # the pattern must match the whole name


# -- the sample ---------------------------------------------------------------------------


def light(pid, chat, weak, rules_route):
    return {"point_id": pid, "chat_id": chat, "weak_label": weak, "rules_route": rules_route}


def test_sample_covers_rare_strata_first_and_is_deterministic():
    pts = [light(f"g{i}", f"c{i}", "local-general", "local-general") for i in range(50)]
    pts += [light("p1", "cp", "local-private", "local-private"), light("r1", "cr", None, "local-reasoning")]
    ids = points_mod.stratified_sample(pts, 5, seed=1)
    assert len(ids) == 5 and len(set(ids)) == 5
    assert {"p1", "r1"} <= set(ids)
    assert ids == points_mod.stratified_sample(pts, 5, seed=1)


def test_sample_caps_points_per_chat_and_stops_when_exhausted():
    pts = [light(f"a{i}", "same-chat", "local-general", "local-general") for i in range(10)]
    assert len(points_mod.stratified_sample(pts, 8, per_chat=3)) == 3
    assert len(points_mod.stratified_sample(pts, 8, per_chat=0)) == 8
    assert points_mod.stratified_sample([], 5) == []


# -- the command ---------------------------------------------------------------------------


def test_command_end_to_end(tmp_path):
    chats = []
    for n in range(3):
        b = Branch()
        b.user(f"write a python function number {n}")
        tool_answer(b, "sandbox-mcp_run")
        b.user("thanks, and what is the capital of France?")
        b.assistant("Paris")
        chats.append(b.chat(chat_id=f"chat-{n}"))
    write_jsonl(tmp_path / "chats.jsonl", chats)
    write_jsonl(tmp_path / "decisions.jsonl", [
        {"chat_id": "chat-0", "message_id": "m4", "chosen_route": "local-code", "shadow_of": None,
         "created_at": "2026-10-01T00:00:00+00:00"}])
    code = points_mod.main(["--chats", str(tmp_path / "chats.jsonl"), "--decisions",
                            str(tmp_path / "decisions.jsonl"), "--out", str(tmp_path / "points.jsonl"),
                            "--sample", "4", "--sample-out", str(tmp_path / "sample.jsonl"), "--seed", "7"])
    assert code == 0
    pts, bad = read_jsonl(tmp_path / "points.jsonl")
    assert bad == 0 and len(pts) == 6
    assert set(pts[0]) == {
        "point_id", "chat_id", "user_id", "user_message_id", "answer_message_id", "k", "user_index",
        "timestamp", "gap_s", "messages", "trimmed_exchanges", "answer_tools", "weak_label", "weak_source",
        "decision_route", "rules_ruleset", "rules_route", "rules_decided_by", "privacy_kw_route",
        "compacted", "answer_error", "answer_sources"}
    assert pts[1]["weak_label"] == "local-code" and pts[1]["weak_source"] == "decision"
    sample, _ = read_jsonl(tmp_path / "sample.jsonl")
    assert len(sample) == 4 and {p["point_id"] for p in sample} <= {p["point_id"] for p in pts}
    raw = (tmp_path / "points.jsonl").read_bytes()
    assert b"\r\n" not in raw and raw.isascii()


def test_command_refuses_a_sample_without_a_destination(tmp_path, capsys):
    with pytest.raises(SystemExit):
        points_mod.main(["--chats", "x", "--out", "y", "--sample", "3"])
