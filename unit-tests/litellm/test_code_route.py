"""local-code: the code-tool remap (ai/litellm/hooks/routing/code_route.py,
run from hooks.handler.Handler's async_pre_call_hook) and the local-code
chain's coding settings in ai/litellm/litellm_config.yaml.

Three layers, like the other files here:

* Pure logic (always runs): reading the tools a history CALLED (not the ones
  merely offered), CODE_ROUTE_TOOL_REGEX parsing (unset -> default, empty ->
  off, invalid -> logged and off), the remap in async_pre_call_hook (only
  local-general, only after a matching tool was called, never raises, one
  `[route]` log line), and how it coexists with the `auto` footer strip.
* Config invariants (need PyYAML): every local-code deployment carries the
  same coding system prompt (one YAML anchor), qwen gets reasoning_effort
  high and muse reasoning_strength xhigh through chat_template_kwargs, the
  prompt never says "reasoning strength" (muse's template would let it win
  over the kwarg), 32768-token answers fit each backend's max-model-len,
  local-general & co. are untouched, `auto` sets no max_tokens, and the
  regex default agrees across code_route.py, the compose file and .env.example.
* Against real LiteLLM (skipped unless importable): the proxy's own
  pre_call_hook dispatch applies the remap, and a Router built from the
  config really sends local-code's prompt, kwargs and max_tokens.

    python -m pytest unit-tests/litellm -q -p no:cacheprovider
"""

from __future__ import annotations

import asyncio
import copy
import logging
import pathlib
import re

import pytest

from common.conversation import called_tool_names, split_exchanges
from common.keywordmatch import KeywordRule, compile_keyword_rule
from hooks.handler import Handler
from hooks.log import logger
from hooks.routing.code_route import (
    DEFAULT_CODE_ROUTE_COOLDOWN,
    DEFAULT_CODE_ROUTE_ROUTER_CONFIG,
    DEFAULT_CODE_ROUTE_TOOL_REGEX,
    code_route_decision,
    code_tool_match,
    compile_code_route_regex,
    load_code_text_signal,
    parse_code_route_cooldown,
)

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_LITELLM_DIR = _ROOT / "ai" / "litellm"

ROUTER_CONFIG = _ROOT / "ai" / "semantic-router" / "config.yaml"

CONFIG = _LITELLM_DIR / "litellm_config.yaml"
COMPOSE = _LITELLM_DIR / "docker-compose.litellm.yml"
VLLM_COMPOSE = _ROOT / "ai" / "vllm" / "docker-compose.vllm.yml"
ENV_EXAMPLE = _ROOT / ".env.example"


def fn(name: str) -> dict:
    return {"type": "function", "function": {"name": name, "parameters": {"type": "object", "properties": {}}}}


# Tool names as Open WebUI v0.11.4 sends them. The sandbox MCP tools are
# server-id prefixed (seen in a real chat); Open Terminal's are its bare
# OpenAPI operationIds (utils/tools.py::get_terminal_tools, open-terminal
# 0.13.0 main.py).
SANDBOX_TOOLS = [fn("sandbox-mcp_run"), fn("sandbox-mcp_write_files"), fn("sandbox-mcp_exec")]
TERMINAL_TOOLS = [fn(n) for n in (
    "list_files", "read_file", "display_file", "write_file", "replace_file_content", "grep_search",
    "match_files", "search_files", "glob_search", "list_processes", "run_command", "get_process_status",
    "send_process_input", "kill_process")]
WEB_TOOLS = [fn("search_web"), fn("fetch_url"), fn("get_current_date")]


def make_handler(**kw) -> Handler:
    """Tool-call checks only and the default cooldown unless a test says
    otherwise, so no test depends on /app/semantic-router-config.yaml."""
    async def fetch(url):
        raise ConnectionError("no network in unit tests")

    kw.setdefault("code_text_signal", None)
    kw.setdefault("code_route_cooldown", DEFAULT_CODE_ROUTE_COOLDOWN)
    return Handler(fetch=fetch, start_poller=False, auto_footer=kw.pop("auto_footer", True), **kw)


def pre_call(h: Handler, data):
    return asyncio.run(h.async_pre_call_hook(user_api_key_dict=None, cache=None, data=data, call_type="acompletion"))


def history(*called: str) -> list:
    """An Open WebUI-style replayed history: one assistant turn that called
    each of `called` (OpenAI tool_calls) with its tool results, then the
    user's next message."""
    msgs = [{"role": "user", "content": "Make me a scientific calculator!"}]
    for i, name in enumerate(called):
        msgs.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"call_{i}", "type": "function", "function": {"name": name, "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"call_{i}", "content": "ok"})
    msgs.append({"role": "user", "content": "Looks the same"})
    return msgs


def request(model="local-general", tools=None, called=(), **kw):
    data = {"model": model, "messages": history(*called)}
    if tools is not None:
        data["tools"] = tools
    data.update(kw)
    return data


# ── pure helpers ─────────────────────────────────────────────────────────────
def test_called_tool_names_reads_every_call_shape_most_recent_first():
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "tool_calls": [{"function": {"name": "a"}}, {"function": {"name": "b"}}]},
        {"role": "tool", "tool_call_id": "x", "content": "ok"},           # no name: nothing to add
        {"role": "assistant", "function_call": {"name": "c", "arguments": "{}"}},
        {"role": "function", "name": "c", "content": "ok"},
        {"role": "assistant", "content": [{"type": "text", "text": "x"},
                                          {"type": "tool_use", "id": "t", "name": "d", "input": {}}]},
        {"role": "tool", "name": "e", "content": "ok"},
    ]
    assert called_tool_names(msgs) == ["e", "d", "c", "c", "b", "a"]


def test_called_tool_names_skips_junk():
    msgs = [None, "x", {"role": "assistant", "tool_calls": "nope"},
            {"role": "assistant", "tool_calls": [None, {"function": "nope"}, {"function": {"name": ""}}]},
            {"role": "assistant", "content": [{"type": "tool_use"}, {"type": "text", "name": "not-a-call"}]},
            {"role": "user", "name": "a-user-name", "content": "hi"}]
    assert called_tool_names(msgs) == []
    assert called_tool_names(None) == [] and called_tool_names("x") == [] and called_tool_names({}) == []


def test_offered_tools_are_not_calls():
    """`tools` definitions sit beside messages, never inside them."""
    assert called_tool_names(history()) == []
    assert called_tool_names(history("sandbox-mcp_exec")) == ["sandbox-mcp_exec"]


@pytest.mark.parametrize("name", [
    "sandbox-mcp_run", "sandbox-mcp_write_files", "sandbox-mcp_exec", "sandbox-mcp_edit_file",
    "run_command", "get_process_status", "send_process_input", "kill_process", "replace_file_content",
    "read_user_terminal", "send_user_terminal_input",
])
def test_default_regex_matches_sandbox_and_terminal_tools(name):
    assert code_tool_match([name], DEFAULT_CODE_ROUTE_TOOL_REGEX) == name


@pytest.mark.parametrize("name", [
    # Generic names another tool server could own: not enough on their own.
    "read_file", "write_file", "list_files", "search_files",
    # Not code tools at all.
    "search_web", "fetch_url", "generate_image", "query_database", "list_tables",
    # Full match only: a prefix / suffix / substring is not a hit.
    "sandbox-mcp_", "my_run_command", "run_command_v2", "xsandbox-mcp_run", "sandbox_run",
])
def test_default_regex_ignores_everything_else(name):
    assert code_tool_match([name], DEFAULT_CODE_ROUTE_TOOL_REGEX) is None


def test_first_matching_name_is_reported():
    assert code_tool_match(["search_web", "read_file", "replace_file_content", "run_command"],
                           DEFAULT_CODE_ROUTE_TOOL_REGEX) == "replace_file_content"
    assert code_tool_match(["search_web", "fetch_url"], DEFAULT_CODE_ROUTE_TOOL_REGEX) is None
    assert code_tool_match([], DEFAULT_CODE_ROUTE_TOOL_REGEX) is None
    assert code_tool_match(None, DEFAULT_CODE_ROUTE_TOOL_REGEX) is None
    assert code_tool_match(["sandbox-mcp_run", None, 3], DEFAULT_CODE_ROUTE_TOOL_REGEX) == "sandbox-mcp_run"
    assert code_tool_match(["sandbox-mcp_run"], None) is None
    assert code_tool_match(["sandbox-mcp_run"], "") is None
    assert code_tool_match(["sandbox-mcp_run", "sandbox-mcp_exec"], re.compile("sandbox-mcp_exec")) == "sandbox-mcp_exec"


@pytest.mark.parametrize("raw", [None, "", "   ", "\t\n"])
def test_empty_regex_disables(raw):
    assert compile_code_route_regex(raw) == (None, "")


def test_invalid_regex_disables_with_an_error():
    pattern, error = compile_code_route_regex("sandbox-mcp_(")
    assert pattern is None
    assert "invalid CODE_ROUTE_TOOL_REGEX" in error and "sandbox-mcp_(" in error


def _load_fresh(monkeypatch, fresh_hooks, load_shim, value):
    """Load overflow.py the way the proxy does (spec_from_file_location, not
    registered in sys.modules) against a FRESH import of the hooks package,
    with CODE_ROUTE_TOOL_REGEX set to `value` (None = unset), so the env read
    at import (hooks.settings) is what is tested."""
    if value is None:
        monkeypatch.delenv("CODE_ROUTE_TOOL_REGEX", raising=False)
    else:
        monkeypatch.setenv("CODE_ROUTE_TOOL_REGEX", value)
    with fresh_hooks():
        return load_shim()


def test_env_unset_uses_the_default_empty_disables_invalid_disables(monkeypatch, fresh_hooks, load_shim):
    def _load(value):
        return _load_fresh(monkeypatch, fresh_hooks, load_shim, value)

    mod = _load(None)
    assert mod.handler.code_route.regex is not None
    assert mod.handler.code_route.regex.pattern == DEFAULT_CODE_ROUTE_TOOL_REGEX

    mod = _load("")
    assert mod.handler.code_route.regex is None

    mod = _load("only_this_tool")
    assert code_tool_match(["only_this_tool"], mod.handler.code_route.regex) == "only_this_tool"
    assert code_tool_match(["sandbox-mcp_run"], mod.handler.code_route.regex) is None

    records = []

    class Grab(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    grab = Grab()
    logging.getLogger("zeo.overflow").addHandler(grab)
    try:
        mod = _load("(unclosed")
    finally:
        logging.getLogger("zeo.overflow").removeHandler(grab)
    assert mod.handler.code_route.regex is None
    assert any("invalid CODE_ROUTE_TOOL_REGEX" in r and "remap disabled" in r for r in records)


# ── the remap in async_pre_call_hook ─────────────────────────────────────────
@pytest.mark.parametrize("called,hit", [
    (("sandbox-mcp_run",), "sandbox-mcp_run"),
    (("run_command",), "run_command"),
    (("search_web", "sandbox-mcp_write_files", "search_web"), "sandbox-mcp_write_files"),
    (("sandbox-mcp_run", "sandbox-mcp_exec"), "sandbox-mcp_exec"),   # most recent call reported
])
def test_local_general_after_a_code_tool_call_goes_to_local_code(called, hit):
    h = make_handler()
    data = request(tools=copy.deepcopy(SANDBOX_TOOLS + WEB_TOOLS), called=called)
    before = copy.deepcopy(data)
    out = pre_call(h, data)
    assert out is data and data["model"] == "local-code"
    before["model"] = "local-code"
    assert data == before  # nothing else touched
    assert h.stats["code_routes"] == 1 and h.stats["code_route_errors"] == 0


def test_a_call_without_any_offered_tools_still_counts():
    """The history is what decides; this round's `tools` may be absent."""
    h = make_handler()
    data = request(called=("sandbox-mcp_exec",))
    assert pre_call(h, data) is data and data["model"] == "local-code"


@pytest.mark.parametrize("data", [
    request(),                                                           # no tools, no calls
    request(tools=copy.deepcopy(SANDBOX_TOOLS)),                         # sandbox attached, never used
    request(tools=copy.deepcopy(SANDBOX_TOOLS + TERMINAL_TOOLS)),        # both attached, never used
    request(tools=copy.deepcopy(WEB_TOOLS + SANDBOX_TOOLS), called=("search_web",)),  # only web search used
    request(tools=copy.deepcopy(TERMINAL_TOOLS), called=("read_file", "list_files")),  # generic names only
    request(messages="garbage"),
])
def test_local_general_without_a_code_tool_call_is_untouched(data):
    h = make_handler()
    before = copy.deepcopy(data)
    assert pre_call(h, data) is None
    assert data == before and h.stats["code_routes"] == 0


@pytest.mark.parametrize("model", [
    "local-private",      # PII routing is the router's decision -- never remapped
    "local-reasoning", "local-code", "qwen3.8-solo", "muse-glimmer", "claude-sonnet-5",
])
def test_other_models_are_never_remapped(model):
    h = make_handler()
    data = request(model=model, tools=copy.deepcopy(SANDBOX_TOOLS + TERMINAL_TOOLS),
                   called=("sandbox-mcp_exec", "run_command"))
    assert pre_call(h, data) is None
    assert data["model"] == model and h.stats["code_routes"] == 0


def test_auto_is_not_remapped_but_still_gets_its_footer_stripped():
    h = make_handler()
    data = request(model="auto", tools=copy.deepcopy(SANDBOX_TOOLS), called=("sandbox-mcp_exec",))
    data["messages"].insert(-1, {"role": "assistant", "content": "done\n\n---\n*qwen3.8-solo · local-code*"})
    assert pre_call(h, data) is data
    assert data["model"] == "auto"
    assert data["messages"][-2]["content"] == "done"
    assert h.stats["code_routes"] == 0 and h.stats["footers_stripped"] == 1


def test_remap_works_with_the_footer_turned_off():
    h = make_handler(auto_footer=False)
    data = request(called=("sandbox-mcp_exec",))
    assert pre_call(h, data) is data and data["model"] == "local-code"


@pytest.mark.parametrize("setting", ["", None])
def test_disabled_remap_changes_nothing(setting):
    h = make_handler(code_route_regex=setting)
    assert h.code_route.regex is None
    data = request(called=("sandbox-mcp_exec",))
    assert pre_call(h, data) is None and data["model"] == "local-general"


def test_custom_regex_on_the_handler():
    h = make_handler(code_route_regex="search_web")
    data = request(tools=copy.deepcopy(WEB_TOOLS), called=("search_web",))
    assert pre_call(h, data) is data and data["model"] == "local-code"


def test_remap_logs_one_route_line():
    records = []

    class Grab(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    grab = Grab()
    logger.addHandler(grab)
    try:
        h = make_handler()
        pre_call(h, request(called=("sandbox-mcp_exec",)))
    finally:
        logger.removeHandler(grab)
    assert records == ["[route] local-general -> local-code "
                       "(called sandbox-mcp_exec 1 exchange(s) ago; 0/3 irrelevant since)"]


def test_hook_never_raises():
    class ExplodingDict(dict):
        def get(self, *a, **k):
            raise RuntimeError("boom")

    h = make_handler()
    data = ExplodingDict(model="local-general", messages=history("sandbox-mcp_exec"))
    assert pre_call(h, data) is None
    assert dict.__getitem__(data, "model") == "local-general"
    assert h.stats["code_route_errors"] == 1

    class ExplodingMessages(list):
        def __iter__(self):
            raise RuntimeError("boom")

    data = request(messages=ExplodingMessages(history("sandbox-mcp_exec")))
    assert pre_call(h, data) is None and data["model"] == "local-general"
    assert h.stats["code_route_errors"] == 2

    class BadPattern:
        def fullmatch(self, name):
            raise RuntimeError("boom")

    h = make_handler(code_route_regex=BadPattern())
    data = request(called=("sandbox-mcp_exec",))
    assert pre_call(h, data) is None and data["model"] == "local-general"
    assert pre_call(h, None) is None and pre_call(h, "x") is None


# ── the code cooldown ─────────────────────────────────────────────────────────
def coding(text="Make me a scientific calculator!", tool="sandbox-mcp_write_files"):
    """One exchange: the user message and the rounds it started (a code-tool
    call when `tool` is set, then the final answer)."""
    msgs = [{"role": "user", "content": text}]
    if tool:
        msgs += [{"role": "assistant", "content": "", "tool_calls": [
                     {"id": "c1", "type": "function", "function": {"name": tool, "arguments": "{}"}}]},
                 {"role": "tool", "tool_call_id": "c1", "content": "ok"}]
    msgs.append({"role": "assistant", "content": "Done."})
    return msgs


def chatting(text):
    return [{"role": "user", "content": text}, {"role": "assistant", "content": "It's sunny."}]


def now(text):
    """The current exchange: just the user's new message, no rounds yet."""
    return [{"role": "user", "content": text}]


WEATHER = ["What's the weather in Denver?", "And tomorrow?", "Will it snow this week?", "What about Boulder?"]


def test_split_exchanges_one_per_user_message():
    msgs = [{"role": "system", "content": "sys"}, {"role": "assistant", "content": "orphan"},
            *coding(), *chatting("hi"),
            {"role": "user", "content": [{"type": "text", "text": "look"}, {"type": "image_url", "image_url": {}},
                                         {"type": "text", "text": "at this"}]}]
    ex = split_exchanges(msgs)
    assert [t for t, _ in ex] == ["Make me a scientific calculator!", "hi", "look\nat this"]
    assert len(ex[0][1]) == 3 and len(ex[1][1]) == 1 and ex[2][1] == []
    assert split_exchanges(None) == [] and split_exchanges("x") == []


def test_users_example_three_irrelevant_exchanges_then_it_drops():
    """Build a calculator (sandbox call), then four weather questions: the
    first three are still served by local-code, the fourth drops."""
    history = coding()
    served = []
    for question in WEATHER:
        remap, _ = code_route_decision(history + now(question), DEFAULT_CODE_ROUTE_TOOL_REGEX, None, 3)
        served.append(remap)
        history += chatting(question)
    assert served == [True, True, True, False]


def test_the_reasons_name_the_activity_and_the_count():
    tool = DEFAULT_CODE_ROUTE_TOOL_REGEX
    h = coding()
    assert code_route_decision(h + now(WEATHER[0]), tool, None, 3) == (
        True, "called sandbox-mcp_write_files 1 exchange(s) ago; 0/3 irrelevant since")
    h += chatting(WEATHER[0]) + chatting(WEATHER[1])
    assert code_route_decision(h + now(WEATHER[2]), tool, None, 3) == (
        True, "called sandbox-mcp_write_files 3 exchange(s) ago; 2/3 irrelevant since")
    h += chatting(WEATHER[2])
    assert code_route_decision(h + now(WEATHER[3]), tool, None, 3) == (
        False, "cooldown: 3/3 irrelevant exchanges since the last one that called sandbox-mcp_write_files")


def test_a_chat_that_never_looked_like_code_has_no_reason():
    h = chatting(WEATHER[0]) + chatting(WEATHER[1])
    assert code_route_decision(h + now(WEATHER[2]), DEFAULT_CODE_ROUTE_TOOL_REGEX, None, 3) == (False, "")
    assert code_route_decision([], DEFAULT_CODE_ROUTE_TOOL_REGEX, None, 3) == (False, "")


def test_code_activity_in_the_current_exchange_wins_and_resets_the_streak():
    tool = DEFAULT_CODE_ROUTE_TOOL_REGEX
    h = coding() + sum((chatting(q) for q in WEATHER), [])          # cooled down
    assert code_route_decision(h + now("Looks the same"), tool, None, 3)[0] is False
    # The model calls the sandbox mid-exchange: the next round is local-code.
    mid = h + coding("Looks the same", tool="sandbox-mcp_exec")[:-1]
    assert code_route_decision(mid, tool, None, 3) == (True, "called sandbox-mcp_exec in this exchange")
    # And the streak counts from there again.
    h = mid + [{"role": "assistant", "content": "Fixed."}]
    assert code_route_decision(h + now(WEATHER[0]), tool, None, 3)[0] is True


def test_cooldown_zero_keeps_local_code_only_for_the_current_exchange():
    tool = DEFAULT_CODE_ROUTE_TOOL_REGEX
    assert code_route_decision(coding() + now(WEATHER[0]), tool, None, 0)[0] is False
    assert code_route_decision(coding()[:-1], tool, None, 0)[0] is True


def test_irrelevant_tool_calls_do_not_count():
    tool = DEFAULT_CODE_ROUTE_TOOL_REGEX
    h = coding() + coding(WEATHER[0], tool="search_web") + coding(WEATHER[1], tool="search_web") \
        + coding(WEATHER[2], tool="read_file")
    assert code_route_decision(h + now(WEATHER[3]), tool, None, 3)[0] is False


# The router's own signal, as LiteLLM loads it from the mounted config.yaml.
@pytest.fixture(scope="module")
def router_signal() -> KeywordRule:
    signal, error = load_code_text_signal(str(ROUTER_CONFIG))
    assert signal is not None, error
    return signal


def test_the_real_code_kw_loads_and_keeps_the_router_semantics(router_signal):
    assert router_signal.operator == "OR" and len(router_signal.patterns) >= 5
    assert router_signal.matches("Make me a scientific calculator!")
    assert router_signal.matches("there should be a test for each function")
    assert router_signal.matches("The tests have to use exactly the same functions the calculator uses.")
    for question in WEATHER + ["How far is the nearest star?", "Looks the same"]:
        assert not router_signal.matches(question), question


def test_a_code_kw_message_is_relevant_without_any_tool_call(router_signal):
    tool = DEFAULT_CODE_ROUTE_TOOL_REGEX
    h = coding("Refactor this function please", tool=None)
    assert code_route_decision(h + now(WEATHER[0]), tool, router_signal, 3) == (
        True, "message matches code_kw 1 exchange(s) ago; 0/3 irrelevant since")
    assert code_route_decision(h + now(WEATHER[0]), tool, None, 3) == (False, "")   # tool checks only
    assert code_route_decision(now("fix the python script"), tool, router_signal, 3) == (
        True, "message matches code_kw in this exchange")


def test_compile_code_text_signal_mirrors_go_semantics():
    words = compile_keyword_rule({"operator": "OR", "case_sensitive": False, "keywords": ["function", "```"]})
    assert words.matches("a FUNCTION here") and not words.matches("functions")   # whole literal word
    assert words.matches("see```x")                       # no letters or digits -> no \b, a plain substring
    regex = compile_keyword_rule({"method": "regex", "case_sensitive": False, "keywords": [r"\bfunctions?\b"]})
    assert regex.matches("Functions") and not regex.matches("dysfunctional")
    cased = compile_keyword_rule({"method": "regex", "case_sensitive": True, "keywords": ["Go"]})
    assert cased.matches("Go") and not cased.matches("go")
    both = compile_keyword_rule({"operator": "AND", "keywords": ["docker", "compose"]})
    assert both.matches("docker compose up") and not both.matches("docker run")
    none = compile_keyword_rule({"operator": "NOR", "keywords": ["weather"]})
    assert none.matches("code") and not none.matches("weather")
    assert not words.matches("") and not words.matches(None)


@pytest.mark.parametrize("rule", [None, {}, {"keywords": []}, {"keywords": [""]}, {"keywords": [3]},
                                  {"operator": "XOR", "keywords": ["a"]},
                                  {"method": "regex", "keywords": ["(unclosed"]}])
def test_compile_code_text_signal_rejects_bad_rules(rule):
    with pytest.raises(ValueError):
        compile_keyword_rule(rule)


def test_load_code_text_signal_failures_return_an_error(tmp_path):
    assert load_code_text_signal(str(tmp_path / "missing.yaml"))[0] is None
    assert load_code_text_signal("")[1] == "CODE_ROUTE_ROUTER_CONFIG is empty"
    other = tmp_path / "other.yaml"
    other.write_text("routing:\n  signals:\n    keywords:\n      - name: privacy_kw\n        keywords: [ssn]\n",
                     encoding="utf-8")
    signal, error = load_code_text_signal(str(other))
    assert signal is None and "no keyword signal 'code_kw'" in error


@pytest.mark.parametrize("raw,expected", [(None, (3, "")), ("", (3, "")), (" 5 ", (5, "")), ("0", (0, "")), (2, (2, ""))])
def test_parse_code_route_cooldown(raw, expected):
    assert parse_code_route_cooldown(raw) == expected


@pytest.mark.parametrize("raw", ["-1", "three", "1.5"])
def test_parse_code_route_cooldown_rejects_junk(raw):
    value, error = parse_code_route_cooldown(raw)
    assert value == DEFAULT_CODE_ROUTE_COOLDOWN and "invalid CODE_ROUTE_COOLDOWN" in error


def test_the_hook_applies_the_cooldown_and_logs_the_drop():
    records = []

    class Grab(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    grab = Grab()
    logger.addHandler(grab)
    try:
        h = make_handler()
        history = coding()
        models = []
        for question in WEATHER:
            data = {"model": "local-general", "messages": copy.deepcopy(history + now(question))}
            pre_call(h, data)
            models.append(data["model"])
            history += chatting(question)
    finally:
        logger.removeHandler(grab)
    assert models == ["local-code", "local-code", "local-code", "local-general"]
    assert h.stats["code_routes"] == 3 and h.stats["code_route_cooldowns"] == 1
    assert records[-1] == ("[route] local-general stays (cooldown: 3/3 irrelevant exchanges since the last "
                           "one that called sandbox-mcp_write_files)")


def test_handler_loads_the_router_config_from_a_path():
    h = make_handler(code_text_signal=str(ROUTER_CONFIG))
    assert isinstance(h.code_route.text_signal, KeywordRule)
    data = {"model": "local-general", "messages": now("Make me a scientific calculator!")}
    assert pre_call(h, data) is data and data["model"] == "local-code"


# ── config invariants ─────────────────────────────────────────────────────────
try:
    import yaml  # noqa: E402
except ImportError:  # the pure tests above still run
    yaml = None

needs_yaml = pytest.mark.skipif(yaml is None, reason="needs PyYAML")

CODE_PROMPT_LINES = (
    "You are working on code with tools.",
    "Your turn ends as soon as you reply without a tool call.",
    "edit_file",
    "write_files only for new files or complete rewrites",
    "read_file (offset/limit)",
    "When a tool returns an error, diagnose it and retry in the same turn.",
    "Run the code or its tests to verify before saying it works.",
    "Reply without a tool call only when the task is done",
)


def _cfg() -> dict:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


def _group(name: str) -> dict:
    """{litellm_params.model: deployment} of one model group."""
    out = {}
    for m in _cfg()["model_list"]:
        if m["model_name"] == name:
            out[m["litellm_params"]["model"]] = m
    assert out, f"no deployments for {name}"
    return out


def _max_model_len(service: str) -> int:
    """--max-model-len of a vLLM service, read from its compose block."""
    text = VLLM_COMPOSE.read_text(encoding="utf-8")
    m = re.search(rf"(?ms)^  {re.escape(service)}:\n(.*?)(?=^  \S)", text)
    assert m, f"no service {service} in {VLLM_COMPOSE.name}"
    found = re.findall(r"--max-model-len\s+(\d+)", m.group(1))
    assert len(found) == 1, f"{service}: expected one --max-model-len, got {found}"
    return int(found[0])


@needs_yaml
def test_the_prompt_is_one_anchor_in_the_file():
    text = CONFIG.read_text(encoding="utf-8")
    assert len(re.findall(r"(?m)^\s+litellm_system_prompt: &code_system_prompt\b", text)) == 1
    assert len(re.findall(r"(?m)^\s+litellm_system_prompt: \*code_system_prompt\s*$", text)) == 2


@needs_yaml
def test_every_local_code_deployment_has_the_same_coding_prompt():
    group = _group("local-code")
    assert set(group) == {"openai/qwen3.8-solo", "openai/muse-glimmer", "anthropic/claude-sonnet-5"}
    prompts = {m["litellm_params"].get("litellm_system_prompt") for m in group.values()}
    assert len(prompts) == 1
    prompt = prompts.pop()
    assert isinstance(prompt, str) and prompt == prompt.strip()
    for line in CODE_PROMPT_LINES:
        assert line in prompt, line
    # muse-glimmer's template takes a "Reasoning strength:" line in the system
    # prompt over the reasoning_strength kwarg -- so the prompt must not have one.
    assert "reasoning strength" not in prompt.lower()
    assert "reasoning_strength" not in prompt.lower() and "reasoning effort" not in prompt.lower()


@needs_yaml
def test_local_code_reasoning_goes_through_each_models_own_template_kwarg():
    group = _group("local-code")
    qwen = group["openai/qwen3.8-solo"]["litellm_params"]
    muse = group["openai/muse-glimmer"]["litellm_params"]
    claude = group["anthropic/claude-sonnet-5"]["litellm_params"]
    # qwen_fixed_chat_template.jinja maps high -> xhigh; muse's stock template defaults to high.
    assert qwen["extra_body"]["chat_template_kwargs"] == {"reasoning_effort": "high"}
    assert muse["extra_body"]["chat_template_kwargs"] == {"reasoning_strength": "xhigh"}
    assert "extra_body" not in claude and "max_tokens" not in claude


@needs_yaml
def test_local_code_sampling_is_local_generals():
    general, code = _group("local-general"), _group("local-code")
    for model in ("openai/qwen3.8-solo", "openai/muse-glimmer"):
        g, c = general[model]["litellm_params"], code[model]["litellm_params"]
        for key in ("temperature", "top_p", "frequency_penalty", "presence_penalty", "api_base",
                    "order", "stream_timeout", "timeout"):
            assert c[key] == g[key], (model, key)
        g_extra = {k: v for k, v in g["extra_body"].items() if k != "chat_template_kwargs"}
        c_extra = {k: v for k, v in c["extra_body"].items() if k != "chat_template_kwargs"}
        assert c_extra == g_extra, model  # top_k / min_p kept next to the new kwarg


@needs_yaml
def test_local_code_answers_are_32768_tokens_and_fit_the_backend():
    group = _group("local-code")
    for model in ("openai/qwen3.8-solo", "openai/muse-glimmer"):
        m = group[model]
        assert m["litellm_params"]["max_tokens"] == 32768
        assert m["model_info"]["max_output_tokens"] == 32768
    # The context fit drops a deployment when count + max_tokens > max_model_len,
    # so max_input_tokens says the same thing: qwen 131072 - 32768 = 98304.
    assert group["openai/qwen3.8-solo"]["model_info"]["max_input_tokens"] == 98304


@needs_yaml
@pytest.mark.parametrize("chain", ["local-general", "local-code", "local-reasoning", "local-private"])
def test_every_local_chain_deployment_fits_input_plus_output_in_max_model_len(chain):
    for model, m in _group(chain).items():
        if not model.startswith("openai/"):
            continue
        service = model.split("/", 1)[1]
        assert m["model_info"]["max_input_tokens"] + m["litellm_params"]["max_tokens"] <= _max_model_len(service), (
            chain, model)


@needs_yaml
@pytest.mark.parametrize("chain", ["local-general", "local-reasoning", "local-private"])
def test_other_chains_are_unchanged(chain):
    for model, m in _group(chain).items():
        p = m["litellm_params"]
        assert "litellm_system_prompt" not in p, (chain, model)
        assert "chat_template_kwargs" not in (p.get("extra_body") or {}), (chain, model)
        if model.startswith("openai/"):
            assert p["max_tokens"] == 16384, (chain, model)


@needs_yaml
def test_auto_sets_no_max_tokens():
    """A deployment default on `auto` would reach the inner LiteLLM as a
    CLIENT max_tokens and override every chain deployment's own."""
    (auto,) = _group("auto").values()
    assert "max_tokens" not in auto["litellm_params"]
    assert "max_completion_tokens" not in auto["litellm_params"]


@needs_yaml
def test_regex_default_is_the_same_in_code_compose_and_env_example():
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    value = compose["services"]["litellm"]["environment"]["CODE_ROUTE_TOOL_REGEX"]
    m = re.fullmatch(r"\$\{CODE_ROUTE_TOOL_REGEX-(.*)\}", value)
    assert m, f"compose must use ${{CODE_ROUTE_TOOL_REGEX-<default>}} (unset -> default, empty -> off): {value!r}"
    assert m.group(1) == DEFAULT_CODE_ROUTE_TOOL_REGEX
    lines = [ln for ln in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
             if ln.startswith("CODE_ROUTE_TOOL_REGEX=")]
    assert lines == [f"CODE_ROUTE_TOOL_REGEX={DEFAULT_CODE_ROUTE_TOOL_REGEX}"]
    assert "$" not in DEFAULT_CODE_ROUTE_TOOL_REGEX  # .env would need $$


@needs_yaml
def test_cooldown_default_and_router_config_mount_agree():
    service = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]["litellm"]
    env = service["environment"]
    assert env["CODE_ROUTE_COOLDOWN"] == f"${{CODE_ROUTE_COOLDOWN:-{DEFAULT_CODE_ROUTE_COOLDOWN}}}"
    assert env["CODE_ROUTE_ROUTER_CONFIG"] == DEFAULT_CODE_ROUTE_ROUTER_CONFIG
    mount = f"../semantic-router/config.yaml:{DEFAULT_CODE_ROUTE_ROUTER_CONFIG}:ro"
    assert mount in service["volumes"]
    assert (COMPOSE.parent / "../semantic-router/config.yaml").resolve() == ROUTER_CONFIG.resolve()
    lines = [ln for ln in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines() if ln.startswith("CODE_ROUTE_COOLDOWN=")]
    assert lines == [f"CODE_ROUTE_COOLDOWN={DEFAULT_CODE_ROUTE_COOLDOWN}"]


# ── against real LiteLLM ──────────────────────────────────────────────────────
try:
    import litellm  # noqa: E402
    from litellm.integrations.custom_logger import CustomLogger  # noqa: E402
except ImportError:
    litellm = None

needs_litellm = pytest.mark.skipif(litellm is None, reason="needs litellm")

try:
    from litellm.caching.dual_cache import DualCache  # noqa: E402
    from litellm.proxy._types import UserAPIKeyAuth  # noqa: E402
    from litellm.proxy.utils import ProxyLogging  # noqa: E402
except Exception:  # proxy extras (backoff, fastapi, ...) not installed
    ProxyLogging = None

needs_proxy = pytest.mark.skipif(ProxyLogging is None, reason="needs litellm[proxy]==1.95.0")


@needs_proxy
def test_proxy_pre_call_hook_dispatch_applies_the_remap():
    h = make_handler()
    old = litellm.callbacks
    litellm.callbacks = [h]
    try:
        pl = ProxyLogging(user_api_key_cache=DualCache())
        data = request(tools=copy.deepcopy(SANDBOX_TOOLS), called=("sandbox-mcp_exec",))
        out = asyncio.run(pl.pre_call_hook(user_api_key_dict=UserAPIKeyAuth(), data=data, call_type="acompletion"))
        assert out["model"] == "local-code"
        data = request(tools=copy.deepcopy(SANDBOX_TOOLS))   # attached, never called
        out = asyncio.run(pl.pre_call_hook(user_api_key_dict=UserAPIKeyAuth(), data=data, call_type="acompletion"))
        assert out["model"] == "local-general"
        data = request(model="local-private", tools=copy.deepcopy(SANDBOX_TOOLS), called=("sandbox-mcp_exec",))
        out = asyncio.run(pl.pre_call_hook(user_api_key_dict=UserAPIKeyAuth(), data=data, call_type="acompletion"))
        assert out["model"] == "local-private"
    finally:
        litellm.callbacks = old


@needs_litellm
@needs_yaml
def test_router_sends_local_codes_prompt_kwargs_and_max_tokens():
    """A Router built from the real config: what leaves LiteLLM for each
    local-code order, captured before the HTTP call (order 1 and 2 point at
    a closed port; the capture is all that matters)."""
    captured = []

    class Capture(CustomLogger):
        def log_pre_api_call(self, model, messages, kwargs):
            captured.append((kwargs.get("additional_args") or {}).get("complete_input_dict") or {})

    model_list = []
    for m in _cfg()["model_list"]:
        if m["model_name"] in ("local-code", "local-general") and m["litellm_params"]["model"].startswith("openai/"):
            m = copy.deepcopy(m)
            m["litellm_params"]["api_base"] = "http://127.0.0.1:9/v1"
            model_list.append(m)
    router = litellm.Router(model_list=model_list, num_retries=0)
    old = litellm.callbacks
    litellm.callbacks = [Capture()]
    try:
        msgs = [{"role": "system", "content": "owui system"}, {"role": "user", "content": "hi"}]
        for group in ("local-code", "local-general"):
            try:
                asyncio.run(router.acompletion(model=group, messages=copy.deepcopy(msgs), timeout=2))
            except Exception:
                pass
    finally:
        litellm.callbacks = old
    bodies = [b for b in captured if b.get("messages")]
    by_model = {}
    for b in bodies:
        by_model.setdefault(b.get("model"), []).append(b)
    code_qwen = next(b for b in bodies if b["messages"][0]["content"].startswith("You are working on code")
                     and b.get("model") == "qwen3.8-solo")
    assert code_qwen["messages"][0]["content"].endswith("\n\nowui system")
    assert code_qwen["max_tokens"] == 32768
    assert code_qwen["extra_body"]["chat_template_kwargs"] == {"reasoning_effort": "high"}
    assert code_qwen["extra_body"]["top_k"] == 20
    general_qwen = [b for b in bodies if b.get("model") == "qwen3.8-solo"
                    and b["messages"][0]["content"] == "owui system"]
    assert general_qwen and general_qwen[0]["max_tokens"] == 16384
    assert "chat_template_kwargs" not in general_qwen[0].get("extra_body", {})
