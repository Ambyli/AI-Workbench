"""Each evaluator's behaviour: votes, abstains, edge cases, config validation.

The cooldown's verdicts and reasons are pinned on a table of histories (what
the `default` ruleset's `sticky-code` reports in every decision log).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from rp_helpers import ROOT, assistant, evaluate, system, tool_round, user

from route_policy.evaluators.backend_load import BackendLoad, BackendLoadConfig
from route_policy.evaluators.cooldown import Cooldown, CooldownConfig
from route_policy.evaluators.regex_text import RegexText, RegexTextConfig
from route_policy.evaluators.static import Static, StaticConfig
from route_policy.evaluators.tool_calls import ToolCalls, ToolCallsConfig

import yaml

CODE_RULE = yaml.safe_load((ROOT / "ai/route-policy/rulesets/patterns/code.yaml").read_text(encoding="utf-8"))
TOOL_REGEX = ("sandbox-mcp_.+|run_command|get_process_status|send_process_input|kill_process"
              "|replace_file_content|read_user_terminal|send_user_terminal_input")


def regex(rule: dict, **kw) -> RegexText:
    return RegexText("t", RegexTextConfig(rule=rule, **kw))


def tools(pattern: str = TOOL_REGEX, scope: str = "current_exchange") -> ToolCalls:
    return ToolCalls("code-tools", ToolCallsConfig(pattern=pattern, scope=scope))


def cooldown(exchanges: int = 3, judges=None) -> Cooldown:
    judges = judges if judges is not None else [tools(), RegexText("code-text", RegexTextConfig(rule=CODE_RULE))]
    cd = Cooldown("sticky", CooldownConfig(exchanges=exchanges, relevant_if=[j.id for j in judges]))
    cd.bind({**{j.id: j for j in judges}, "sticky": cd})
    return cd


# -- regex_text -----------------------------------------------------------------


def test_regex_text_literal_words_are_whole_words():
    ev = regex({"name": "w", "keywords": ["function"]})
    assert evaluate(ev, [user("write a function")]).voted
    assert not evaluate(ev, [user("these functions")]).voted   # a literal is a whole word


def test_regex_text_regex_method_and_case():
    ev = regex(CODE_RULE)
    out = evaluate(ev, [user("Make me a scientific calculator!")])
    assert out.voted and out.reason == "latest user message matches code_kw"
    assert out.detail["method"] == "regex" and out.detail["matched_patterns"] == [5]
    sensitive = regex({"name": "c", "keywords": ["SQL"], "case_sensitive": True})
    assert evaluate(sensitive, [user("SQL please")]).voted
    assert not evaluate(sensitive, [user("sql please")]).voted


def test_regex_text_operators():
    both = regex({"name": "a", "operator": "AND", "keywords": ["alpha", "beta"]})
    assert evaluate(both, [user("alpha and beta")]).voted
    assert not evaluate(both, [user("alpha only")]).voted
    none = regex({"name": "n", "operator": "NOR", "keywords": ["alpha"]})
    assert evaluate(none, [user("gamma")]).voted
    assert not evaluate(none, [user("alpha")]).voted


def test_regex_text_reads_only_the_latest_user_message():
    ev = regex(CODE_RULE)
    history = [system(), user("write a python script"), assistant(), user("thanks!")]
    out = evaluate(ev, history)
    assert not out.voted and out.detail["matched"] is False
    # The latest user message counts even when tool rounds follow it.
    assert evaluate(ev, [user("fix app.py"), *tool_round("sandbox-mcp_exec")]).voted


def test_regex_text_multimodal_content_and_no_user_message():
    ev = regex(CODE_RULE)
    content = [{"type": "image_url", "image_url": {"url": "data:,"}}, {"type": "text", "text": "debug this"}]
    assert evaluate(ev, [user(content)]).voted
    out = evaluate(ev, [system("write python")])
    assert not out.voted and out.reason == "no user message"


def test_regex_text_detail_never_carries_the_text():
    out = evaluate(regex(CODE_RULE), [user("secret project: app.py")])
    assert "secret" not in repr(out.detail) and out.detail["searched_chars"] == len("secret project: app.py")


def test_regex_text_relevance_reason():
    assert regex(CODE_RULE).exchange_relevance("fix the bug", []) == "message matches code_kw"
    assert regex(CODE_RULE).exchange_relevance("hello", []) is None


@pytest.mark.parametrize("rule", [
    {"name": "x", "keywords": []},                                  # no keywords
    {"name": "x", "keywords": ["(unclosed"], "method": "regex"},    # does not compile
    {"name": "x", "keywords": ["a"], "method": "bm25"},             # not implemented here
    {"name": "x", "keywords": ["a"], "fuzzy_match": True},          # a Go-only option
    {"name": "x", "keywords": ["a"], "operator": "XOR"},
])
def test_regex_text_bad_rules_are_refused(rule):
    with pytest.raises(ValidationError):
        RegexTextConfig(rule=rule)


# -- tool_calls ------------------------------------------------------------------


def test_tool_calls_current_exchange_only():
    ev = tools()
    called_before = [user("go"), *tool_round("sandbox-mcp_exec"), user("looks the same")]
    out = evaluate(ev, called_before)
    assert not out.voted and out.detail["called"] == []
    out = evaluate(ev, [user("go"), *tool_round("run_command")])
    assert out.voted and out.reason == "called run_command in this exchange"
    assert out.detail == {"scope": "current_exchange", "pattern": TOOL_REGEX,
                          "called": ["run_command"], "matched": "run_command"}


def test_tool_calls_full_name_match():
    ev = tools()
    assert evaluate(ev, [user("x"), *tool_round("sandbox-mcp_write_files")]).voted
    for name in ("my-sandbox-mcp_exec", "run_command_x", "search_web", "list_files"):
        assert not evaluate(ev, [user("x"), *tool_round(name)]).voted, name


def test_tool_calls_offered_tools_never_count():
    offered = [{"type": "function", "function": {"name": "sandbox-mcp_exec", "parameters": {}}}]
    assert not evaluate(tools(), [user("hi")], tools=offered).voted


def test_tool_calls_conversation_scope_and_anthropic_blocks():
    ev = tools(scope="conversation")
    out = evaluate(ev, [user("go"), *tool_round("kill_process"), user("and now?")])
    assert out.voted and out.reason == "called kill_process in this conversation"
    anthropic = [user("go"), {"role": "assistant", "content": [
        {"type": "tool_use", "id": "t", "name": "sandbox-mcp_run", "input": {}}]}]
    assert evaluate(tools(), anthropic).voted


def test_tool_calls_bad_pattern_refused():
    with pytest.raises(ValidationError):
        ToolCallsConfig(pattern="(")


# -- cooldown --------------------------------------------------------------------------


def _histories():
    code = [user("go"), *tool_round("sandbox-mcp_exec")]
    plain = [user("what's the weather"), assistant("sunny")]
    return {
        "empty": [],
        "system only": [system()],
        "never code": plain + [user("and tomorrow?")],
        "tool now": code,
        "code text now": [user("fix app.py")],
        "tool 1 ago": code + [user("looks the same")],
        "tool 2 ago": code + plain + [user("ok")],
        "tool 3 ago": code + plain + plain + [user("ok")],
        "tool 4 ago (expired)": code + plain + plain + plain + [user("ok")],
        "text 2 ago": [user("refactor this class"), assistant()] + plain + [user("thanks")],
        "text then expired": [user("write python"), assistant()] + plain * 3 + [user("hm")],
        "other tool": [user("go"), *tool_round("search_web"), user("next")],
    }


TOOL = "called sandbox-mcp_exec"
TEXT = "message matches code_kw"


def _held(why: str, ago: int, since: int, n: int):
    return True, f"{why} {ago} exchange(s) ago; {since}/{n} irrelevant since"


def _expired(why: str, since: int, n: int):
    return False, f"cooldown: {since}/{n} irrelevant exchanges since the last one that {why}"


def _expected(name: str, n: int):
    """(voted, reason) of a `cooldown(n)` judged by code-tools then code-text."""
    if name in ("empty", "system only"):
        return False, "no user message"
    if name in ("never code", "other tool"):
        return False, "no exchange has been relevant"
    if name == "tool now":
        return True, f"{TOOL} in this exchange"
    if name == "code text now":
        return True, f"{TEXT} in this exchange"
    why, ago, since = {
        "tool 1 ago": (TOOL, 1, 0),
        "tool 2 ago": (TOOL, 2, 1),
        "tool 3 ago": (TOOL, 3, 2),
        "tool 4 ago (expired)": (TOOL, 4, 3),
        "text 2 ago": (TEXT, 2, 1),
        "text then expired": (TEXT, 4, 3),
    }[name]
    # Held while fewer than n irrelevant exchanges followed the relevant one.
    return _held(why, ago, since, n) if since < n else _expired(why, since, n)


@pytest.mark.parametrize("name", list(_histories()))
@pytest.mark.parametrize("n", [0, 1, 3])
def test_cooldown_outcomes(name, n):
    out = evaluate(cooldown(n), _histories()[name])
    assert (out.voted, out.reason) == _expected(name, n), (name, n)


def test_cooldown_expected_table_spot_checks():
    # The table above, spelled out for the cases the ruleset relies on.
    assert _expected("tool 1 ago", 3) == (True, "called sandbox-mcp_exec 1 exchange(s) ago; 0/3 irrelevant since")
    assert _expected("tool 1 ago", 0) == (
        False, "cooldown: 0/0 irrelevant exchanges since the last one that called sandbox-mcp_exec")
    assert _expected("tool 3 ago", 3)[0] is True and _expected("tool 4 ago (expired)", 3)[0] is False
    assert _expected("text 2 ago", 3) == (True, "message matches code_kw 2 exchange(s) ago; 1/3 irrelevant since")


def test_cooldown_detail():
    out = evaluate(cooldown(3), _histories()["tool 2 ago"])
    assert out.voted and out.detail["last_relevant_ago"] == 2 and out.detail["irrelevant_since"] == 1
    assert out.detail["why"] == "called sandbox-mcp_exec" and out.detail["expired"] is False
    out = evaluate(cooldown(3), _histories()["tool 4 ago (expired)"])
    assert not out.voted and out.detail["expired"] is True and out.reason.startswith("cooldown: 3/3")


def test_cooldown_bind_errors():
    cd = Cooldown("sticky", CooldownConfig(exchanges=3, relevant_if=["missing"]))
    with pytest.raises(ValueError, match="not an evaluator"):
        cd.bind({"sticky": cd})
    cd = Cooldown("sticky", CooldownConfig(exchanges=3, relevant_if=["sticky"]))
    with pytest.raises(ValueError, match="itself"):
        cd.bind({"sticky": cd})
    st = Static("fixed", StaticConfig())
    cd = Cooldown("sticky", CooldownConfig(exchanges=3, relevant_if=["fixed"]))
    with pytest.raises(ValueError, match="does not judge"):
        cd.bind({"sticky": cd, "fixed": st})
    with pytest.raises(ValidationError):
        CooldownConfig(exchanges=-1, relevant_if=["a"])
    with pytest.raises(ValidationError):
        CooldownConfig(exchanges=1, relevant_if=["a", "a"])


# -- backend_load -------------------------------------------------------------------

SOLO = "http://qwen3.8-solo:8000/v1"
MUSE = "http://muse-glimmer:8000/v1"


def load(**kw) -> BackendLoad:
    return BackendLoad("load", BackendLoadConfig(**kw))


def test_backend_load_votes_on_verdict_or_queue():
    ev = load(backends=[r"qwen3\.8-solo"])
    out = evaluate(ev, [user("x")], backends={SOLO: {"verdict": "busy", "waiting": 2.0}})
    assert out.voted and out.reason == f"{SOLO} is busy (waiting 2)"
    assert out.detail == {"require": "any", "watched": {SOLO: "busy"}, "qualifying": [SOLO]}
    assert not evaluate(ev, [user("x")], backends={SOLO: {"verdict": "ok", "waiting": 0.0}}).voted
    queued = load(backends=["solo"], verdicts=[], min_waiting=1)
    assert evaluate(queued, [user("x")], backends={SOLO: {"verdict": "ok", "waiting": 1.0}}).voted


def test_backend_load_require_all_and_abstains_without_a_snapshot():
    ev = load(backends=["solo", "muse"], require="all")
    both = {SOLO: {"verdict": "down"}, MUSE: {"verdict": "busy"}}
    assert evaluate(ev, [user("x")], backends=both).voted
    one = {SOLO: {"verdict": "down"}, MUSE: {"verdict": "ok"}}
    assert not evaluate(ev, [user("x")], backends=one).voted
    out = evaluate(ev, [user("x")])
    assert not out.voted and out.reason == "no backend in the snapshot matches"


def test_backend_load_config_validation():
    with pytest.raises(ValidationError):
        BackendLoadConfig(backends=["("])
    with pytest.raises(ValidationError):
        BackendLoadConfig(backends=["x"], verdicts=["melting"])
    with pytest.raises(ValidationError):
        BackendLoadConfig(backends=[])


# -- static -------------------------------------------------------------------------


def test_static():
    out = evaluate(Static("s", StaticConfig(confidence=0.4, reason="pinned")), [])
    assert out.voted and out.confidence == 0.4 and out.reason == "pinned" and out.detail == {}
    assert not evaluate(Static("s", StaticConfig(abstain=True)), [user("x")]).voted
    with pytest.raises(ValidationError):
        StaticConfig(confidence=2)
