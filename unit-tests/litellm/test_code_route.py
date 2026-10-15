"""local-code: the code-tool remap in ai/litellm/overflow.py and the
local-code chain's coding settings in ai/litellm/litellm_config.yaml.

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
  regex default agrees across overflow.py, the compose file and .env.example.
* Against real LiteLLM (skipped unless importable): the proxy's own
  pre_call_hook dispatch applies the remap, and a Router built from the
  config really sends local-code's prompt, kwargs and max_tokens.

    python -m pytest unit-tests/litellm -q -p no:cacheprovider
"""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import logging
import pathlib
import re
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_LITELLM_DIR = _ROOT / "ai" / "litellm"
if str(_LITELLM_DIR) not in sys.path:
    sys.path.insert(0, str(_LITELLM_DIR))

import overflow  # noqa: E402
from overflow import (  # noqa: E402
    DEFAULT_CODE_ROUTE_TOOL_REGEX,
    OverflowHandler,
    called_tool_names,
    code_tool_match,
    compile_code_route_regex,
)

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


def make_handler(**kw) -> OverflowHandler:
    async def fetch(url):
        raise ConnectionError("no network in unit tests")

    return OverflowHandler(fetch=fetch, start_poller=False, auto_footer=kw.pop("auto_footer", True), **kw)


def pre_call(h: OverflowHandler, data):
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


def _load_fresh(monkeypatch, value):
    """Load overflow.py the way the proxy does (spec_from_file_location, not
    registered in sys.modules), with CODE_ROUTE_TOOL_REGEX set to `value`
    (None = unset), so the env read at import is what is tested."""
    if value is None:
        monkeypatch.delenv("CODE_ROUTE_TOOL_REGEX", raising=False)
    else:
        monkeypatch.setenv("CODE_ROUTE_TOOL_REGEX", value)
    spec = importlib.util.spec_from_file_location("overflow_fresh_for_test", _LITELLM_DIR / "overflow.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_env_unset_uses_the_default_empty_disables_invalid_disables(monkeypatch):
    mod = _load_fresh(monkeypatch, None)
    assert mod.handler.code_route_regex is not None
    assert mod.handler.code_route_regex.pattern == DEFAULT_CODE_ROUTE_TOOL_REGEX

    mod = _load_fresh(monkeypatch, "")
    assert mod.handler.code_route_regex is None

    mod = _load_fresh(monkeypatch, "only_this_tool")
    assert code_tool_match(["only_this_tool"], mod.handler.code_route_regex) == "only_this_tool"
    assert code_tool_match(["sandbox-mcp_run"], mod.handler.code_route_regex) is None

    records = []

    class Grab(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    grab = Grab()
    logging.getLogger("zeo.overflow").addHandler(grab)
    try:
        mod = _load_fresh(monkeypatch, "(unclosed")
    finally:
        logging.getLogger("zeo.overflow").removeHandler(grab)
    assert mod.handler.code_route_regex is None
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
    assert h.code_route_regex is None
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
    overflow.logger.addHandler(grab)
    try:
        h = make_handler()
        pre_call(h, request(called=("sandbox-mcp_exec",)))
    finally:
        overflow.logger.removeHandler(grab)
    assert records == ["[route] local-general -> local-code (tool: sandbox-mcp_exec)"]


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
        def __reversed__(self):
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
