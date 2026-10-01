"""Unit tests for the `auto` footer in ai/litellm/overflow.py.

When a client calls model `auto` (the semantic router), the assistant text
ends with a markdown footer naming the backend that answered:

    <answer>

    ---
    *qwen3.8-solo · local-general*

Three layers:

* Pure helpers (always run): footer regex / stripping, Open WebUI task
  detection, the env toggle parser.
* The hooks against real litellm types (skipped unless `litellm` is
  importable): non-stream and stream, scope, overflow busy/down/plain,
  router bypassed, tool calls / response_format / Open WebUI tasks skipped,
  n > 1, idempotence, reasoning untouched, exception safety.
* The proxy plumbing (skipped unless litellm[proxy]==1.95.0 is importable):
  the proxy really activates these hooks on OverflowHandler, really dispatches
  them, and the outer openai provider really surfaces the inner LiteLLM's /
  router's headers as `llm_provider-*` for BOTH a JSON and an SSE response
  (a stdlib HTTP server stands in for Envoy). Plus the cross-group fallback
  (`auto` -> local-private) and the in-group order fallback, driven through
  a real `litellm.Router`.

    python -m pytest unit-tests/litellm -q -p no:cacheprovider
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import pathlib
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_LITELLM_DIR = _ROOT / "ai" / "litellm"
if str(_LITELLM_DIR) not in sys.path:
    sys.path.insert(0, str(_LITELLM_DIR))

import overflow  # noqa: E402
from overflow import (  # noqa: E402
    OverflowHandler,
    _env_bool,
    footer_text,
    is_openwebui_task,
    metrics_url,
    strip_footer,
    strip_footers_from_messages,
)

QWEN = "http://qwen3.8-solo:8000/v1"
MUSE = "http://muse-glimmer:8000/v1"
FOOTER_GENERAL = "\n\n---\n*qwen3.8-solo · local-general*"


# ── pure helpers ───────────────────────────────────────────────────────────
def test_footer_text_is_the_approved_format():
    assert footer_text("qwen3.8-solo · local-general") == FOOTER_GENERAL


@pytest.mark.parametrize(
    "text,expected",
    [
        ("hi" + FOOTER_GENERAL, "hi"),
        ("hi" + FOOTER_GENERAL + "\n", "hi"),
        ("hi\r\n\r\n---\r\n*qwen3.8-solo · local-general*", "hi"),
        ("hi" + footer_text("claude-sonnet-5 · local-general · overflow (local busy)"), "hi"),
        ("hi" + footer_text("qwen3.8-solo · local-private · router bypassed"), "hi"),
        ("hi" + FOOTER_GENERAL + FOOTER_GENERAL, "hi"),  # a doubled footer goes too
        # Not ours: a rule + italic line without the " · " separator, or not at the end.
        ("hi\n\n---\n*Note: this is an aside*", "hi\n\n---\n*Note: this is an aside*"),
        ("hi" + FOOTER_GENERAL + "\nmore", "hi" + FOOTER_GENERAL + "\nmore"),
        ("no footer", "no footer"),
    ],
)
def test_strip_footer_matches_exactly_what_we_emit(text, expected):
    assert strip_footer(text) == expected
    assert strip_footer(strip_footer(text)) == expected  # idempotent


def test_strip_footers_only_touches_assistant_messages():
    messages = [
        {"role": "system", "content": "sys" + FOOTER_GENERAL},
        {"role": "user", "content": "q" + FOOTER_GENERAL},
        {"role": "assistant", "content": "a1" + FOOTER_GENERAL},
        {"role": "assistant", "content": [
            {"type": "text", "text": "a2 first part"},
            {"type": "image_url", "image_url": {"url": "data:x"}},
            {"type": "text", "text": "a2 last" + FOOTER_GENERAL},
        ]},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "t"}]},
    ]
    assert strip_footers_from_messages(messages) == 2
    assert messages[0]["content"] == "sys" + FOOTER_GENERAL
    assert messages[1]["content"] == "q" + FOOTER_GENERAL
    assert messages[2]["content"] == "a1"
    assert messages[3]["content"][0]["text"] == "a2 first part"
    assert messages[3]["content"][2]["text"] == "a2 last"
    assert messages[4]["content"] is None
    assert strip_footers_from_messages(messages) == 0  # idempotent


# Open WebUI v0.11.4 default task prompts (backend/open_webui/config.py), as
# rendered: the first line of each template survives rendering unchanged.
OWUI_TITLE = "### Task:\nGenerate a concise title summarizing the chat history.\n### Guidelines:\n...\n<chat_history>\nUSER: hi\n</chat_history>"
OWUI_TAGS = "### Task:\nGenerate 1-3 broad tags categorizing the main themes of the chat history, along with 1-3 more specific subtopic tags.\n\n### Guidelines:\n..."
OWUI_FOLLOW_UP = "### Task:\nSuggest 3-5 relevant follow-up questions or prompts that the user might naturally ask next ..."
OWUI_QUERY = "### Task:\nAnalyze the chat history to determine the necessity of generating search queries, in the given language. ..."
OWUI_AUTOCOMPLETE = "### Task:\nYou are an autocompletion system. Continue the text in `<text>` ..."
OWUI_EMOJI = "Your task is to reflect the speaker's likely facial expression through a fitting emoji. ...\n\nMessage: ```hi```"


@pytest.mark.parametrize("prompt", [OWUI_TITLE, OWUI_TAGS, OWUI_FOLLOW_UP, OWUI_QUERY, OWUI_AUTOCOMPLETE, OWUI_EMOJI])
def test_openwebui_task_prompts_are_detected(prompt):
    data = {"model": "auto", "stream": False, "messages": [{"role": "user", "content": prompt}]}
    assert is_openwebui_task(data)
    # The task model may carry its own system prompt in front.
    data["messages"].insert(0, {"role": "system", "content": "You are helpful."})
    assert is_openwebui_task(data)
    # List-of-parts content too.
    data["messages"][-1] = {"role": "user", "content": [{"type": "text", "text": prompt}]}
    assert is_openwebui_task(data)


def test_openwebui_legacy_function_calling_is_detected():
    data = {"model": "auto", "stream": False, "messages": [
        {"role": "system", "content": "Available Tools: [{...}]\n\nYour task is to choose and return the correct tool(s)..."},
        {"role": "user", "content": "Query: what is the weather"},
    ]}
    assert is_openwebui_task(data)


def test_custom_task_template_with_chat_history_block_is_detected_when_not_streaming():
    data = {"model": "auto", "stream": False, "messages": [
        {"role": "user", "content": "Make a title.\n<chat_history>\nUSER: hi\n</chat_history>"}]}
    assert is_openwebui_task(data)
    data["stream"] = True  # a real chat turn streams; this heuristic only covers tasks
    assert not is_openwebui_task(data)


def test_ordinary_chat_is_not_a_task():
    data = {"model": "auto", "stream": True, "messages": [
        {"role": "user", "content": "### Task:\nold"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "Write a haiku about rain."},
    ]}
    assert not is_openwebui_task(data)  # only the LAST user message counts
    assert not is_openwebui_task({"model": "auto"})
    assert not is_openwebui_task(None)


@pytest.mark.parametrize("raw,default,expected", [
    ("", True, True), ("true", False, True), ("1", False, True), ("TRUE", False, True),
    ("false", True, False), ("0", True, False), (" no ", True, False), ("maybe", True, True),
])
def test_env_bool(monkeypatch, raw, default, expected):
    monkeypatch.setenv("ZEO_TEST_BOOL", raw)
    assert _env_bool("ZEO_TEST_BOOL", default) is expected


# ── the hooks, against real litellm types ──────────────────────────────────
try:
    import litellm  # noqa: E402
    from litellm.types.utils import (  # noqa: E402
        ChatCompletionDeltaToolCall,
        Choices,
        Delta,
        Function,
        Message,
        ModelResponse,
        ModelResponseStream,
        StreamingChoices,
    )
except ImportError:
    litellm = None

needs_litellm = pytest.mark.skipif(litellm is None, reason="needs litellm==1.95.0")

AUTO_DEPLOYMENT = {"model_name": "auto", "litellm_params": {"model": "openai/vllm-sr/auto"}, "model_info": {"id": "id-auto"}}

INNER = {
    "id-gen-qwen": {"model_name": "local-general", "litellm_params": {"model": "openai/qwen3.8-solo", "api_base": QWEN}},
    "id-gen-muse": {"model_name": "local-general", "litellm_params": {"model": "openai/muse-glimmer", "api_base": MUSE}},
    "id-gen-claude": {"model_name": "local-general", "litellm_params": {"model": "anthropic/claude-sonnet-5"}},
    "id-rea-opus": {"model_name": "local-reasoning", "litellm_params": {"model": "anthropic/claude-opus-5-5"}},
}


class FakeRouter:
    def get_deployment(self, model_id):
        return copy.deepcopy(INNER.get(model_id))

    def get_model_list(self, model_name=None, team_id=None):
        return [copy.deepcopy(d) for d in INNER.values() if d["model_name"] == model_name]


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def make_handler(pages=None, clock=None, router=FakeRouter(), **kw) -> OverflowHandler:
    pages = pages if pages is not None else {}

    async def fetch(url):
        page = pages.get(url)
        if isinstance(page, BaseException):
            raise page
        if page is None:
            raise ConnectionError("no route")
        return page

    return OverflowHandler(fetch=fetch, clock=clock or Clock(), busy_after=2.0, idle_after=5.0,
                           poll_interval=1.0, probe_timeout=0.5, start_poller=False,
                           auto_footer=kw.pop("auto_footer", True), router_getter=lambda: router, **kw)


def routed_headers(model_id="id-gen-qwen", group="local-general", name="openai/qwen3.8-solo"):
    """What the outer openai provider keeps from Envoy + the inner LiteLLM."""
    return {
        "x-litellm-model-group": "auto",  # set by the OUTER router (router.py:9388)
        "llm_provider-x-vsr-selected-decision": "general",
        "llm_provider-x-vsr-selected-model": group,
        "llm_provider-x-litellm-model-id": model_id,
        "llm_provider-x-litellm-model-name": name,
        "llm_provider-x-litellm-model-group": group,
    }


def auto_data(**kw):
    data = {"model": "auto", "stream": False, "messages": [{"role": "user", "content": "hi"}],
            "deployment": copy.deepcopy(AUTO_DEPLOYMENT)}
    data.update(kw)
    return data


def response(contents=("hello",), headers=None, tool_calls=False, reasoning=None):
    choices = []
    for i, content in enumerate(contents):
        msg = {"role": "assistant", "content": content}
        if reasoning is not None:
            msg["reasoning_content"] = reasoning
        if tool_calls:
            msg["tool_calls"] = [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]
        choices.append(Choices(index=i, message=Message(**msg), finish_reason="tool_calls" if tool_calls else "stop"))
    r = ModelResponse(model="auto", choices=choices)
    r._hidden_params = {"additional_headers": dict(headers if headers is not None else routed_headers())}
    return r


def run(coro):
    return asyncio.run(coro)


def success(h, data, resp):
    out = run(h.async_post_call_success_hook(data=data, user_api_key_dict=None, response=resp))
    return out if out is not None else resp


def contents(resp):
    return [c.message.content for c in resp.choices]


@needs_litellm
def test_non_stream_footer():
    h = make_handler()
    out = success(h, auto_data(), response())
    assert contents(out) == ["hello" + FOOTER_GENERAL]
    assert h.stats["footers"] == 1


@needs_litellm
def test_non_auto_is_untouched():
    h = make_handler()
    for model in ("local-general", "local-private", "qwen3.8-solo", "claude-sonnet-5"):
        resp = response()
        assert run(h.async_post_call_success_hook(data=auto_data(model=model), user_api_key_dict=None, response=resp)) is None
        assert contents(resp) == ["hello"]
        msgs = [{"role": "assistant", "content": "a" + FOOTER_GENERAL}]
        assert run(h.async_pre_call_hook(None, None, {"model": model, "messages": msgs}, "acompletion")) is None
        assert msgs[0]["content"] == "a" + FOOTER_GENERAL


@needs_litellm
def test_model_id_beats_model_name_header():
    """The deployment is mapped from x-litellm-model-id through the proxy's router,
    so a name header that disagrees (e.g. stale after an order fallback) loses."""
    h = make_handler()
    out = success(h, auto_data(), response(headers=routed_headers(model_id="id-gen-muse", name="openai/qwen3.8-solo")))
    assert contents(out) == ["hello\n\n---\n*muse-glimmer · local-general*"]


@needs_litellm
def test_without_router_falls_back_to_name_and_group_headers():
    h = make_handler(router=None)
    out = success(h, auto_data(), response(headers=routed_headers(model_id="unknown-id")))
    assert contents(out) == ["hello" + FOOTER_GENERAL]
    # Only the router's own header: chain known, model unknown -> no footer at all.
    resp = response(headers={"llm_provider-x-vsr-selected-model": "local-general"})
    assert run(h.async_post_call_success_hook(data=auto_data(), user_api_key_dict=None, response=resp)) is None


def general_chain():
    return [
        {"model_name": "local-general", "litellm_params": {"model": "openai/qwen3.8-solo", "order": 1, "api_base": QWEN}},
        {"model_name": "local-general", "litellm_params": {"model": "openai/muse-glimmer", "order": 2, "api_base": MUSE}},
        {"model_name": "local-general", "litellm_params": {"model": "anthropic/claude-sonnet-5", "order": 3}},
    ]


def _learn_and_poll(h, pages, clock, qwen, muse):
    h.filter_deployments("local-general", general_chain())  # what the INNER request does
    pages[metrics_url(QWEN)] = qwen
    pages[metrics_url(MUSE)] = muse
    run(h.poll_once())
    clock.advance(2.5)
    run(h.poll_once())


def _metrics(waiting):
    return f'vllm:num_requests_waiting{{engine="0"}} {waiting}\nvllm:num_requests_running{{engine="0"}} 1\n'


CLAUDE = routed_headers(model_id="id-gen-claude", name="anthropic/claude-sonnet-5")


@needs_litellm
def test_overflow_local_busy():
    clock, pages = Clock(), {}
    h = make_handler(pages, clock)
    _learn_and_poll(h, pages, clock, _metrics(3), _metrics(3))
    out = success(h, auto_data(), response(headers=CLAUDE))
    assert contents(out) == ["hello\n\n---\n*claude-sonnet-5 · local-general · overflow (local busy)*"]


@needs_litellm
def test_overflow_local_down():
    clock, pages = Clock(), {}
    h = make_handler(pages, clock)
    _learn_and_poll(h, pages, clock, ConnectionError("refused"), ConnectionError("refused"))
    out = success(h, auto_data(), response(headers=CLAUDE))
    assert contents(out) == ["hello\n\n---\n*claude-sonnet-5 · local-general · overflow (local down)*"]


@needs_litellm
def test_overflow_busy_wins_over_down_and_unknown_is_plain():
    clock, pages = Clock(), {}
    h = make_handler(pages, clock)
    _learn_and_poll(h, pages, clock, _metrics(3), ConnectionError("refused"))
    assert "overflow (local busy)" in contents(success(h, auto_data(), response(headers=CLAUDE)))[0]
    # A fresh handler that never saw the chain (another worker) and has no data: plain.
    h2 = make_handler()
    assert contents(success(h2, auto_data(), response(headers=CLAUDE)))[0].endswith(
        "*claude-sonnet-5 · local-general · overflow*")
    # Opus via local-reasoning, chain backends found through the router's model list.
    out = success(h2, auto_data(), response(headers=routed_headers(
        model_id="id-rea-opus", group="local-reasoning", name="anthropic/claude-opus-5-5")))
    assert contents(out)[0].endswith("*claude-opus-5-5 · local-reasoning · overflow*")


BYPASS_HEADERS = {
    # The outer router answered from local-private after `auto` failed:
    "x-litellm-model-group": "local-private",
    "x-litellm-attempted-fallbacks": 1,
    # ...and the backend is vLLM itself, so no x-vsr-* / x-litellm-* upstream headers.
    "llm_provider-content-type": "application/json",
}


@needs_litellm
def test_router_bypassed():
    h = make_handler()
    data = auto_data(deployment={"model_name": "local-private", "litellm_params": {"model": "openai/qwen3.8-solo"}})
    out = success(h, data, response(headers=BYPASS_HEADERS))
    assert contents(out) == ["hello\n\n---\n*qwen3.8-solo · local-private · router bypassed*"]
    # Without data["deployment"] (it is resolved from the model id and could be
    # missing) the outer group header alone is not enough to name the model.
    resp = response(headers=BYPASS_HEADERS)
    assert run(h.async_post_call_success_hook(data=auto_data(deployment=None), user_api_key_dict=None, response=resp)) is None


@needs_litellm
def test_tool_calls_response_format_and_tasks_are_skipped():
    h = make_handler()
    resp = response(contents=(None,), tool_calls=True)
    assert run(h.async_post_call_success_hook(data=auto_data(), user_api_key_dict=None, response=resp)) is None
    resp = response(contents=("calling a tool",), tool_calls=True)  # content AND tool calls
    assert run(h.async_post_call_success_hook(data=auto_data(), user_api_key_dict=None, response=resp)) is None
    assert contents(resp) == ["calling a tool"]
    resp = response(contents=('{"a": 1}',))
    data = auto_data(response_format={"type": "json_object"})
    assert run(h.async_post_call_success_hook(data=data, user_api_key_dict=None, response=resp)) is None
    for prompt in (OWUI_TITLE, OWUI_TAGS, OWUI_EMOJI):
        resp = response(contents=('{"title": "Rain"}',))
        data = auto_data(messages=[{"role": "user", "content": prompt}])
        assert run(h.async_post_call_success_hook(data=data, user_api_key_dict=None, response=resp)) is None
        assert contents(resp) == ['{"title": "Rain"}']


@needs_litellm
def test_n_greater_than_one_and_never_twice():
    h = make_handler()
    resp = response(contents=("one", "two", "", None))
    out = success(h, auto_data(), resp)
    assert contents(out) == ["one" + FOOTER_GENERAL, "two" + FOOTER_GENERAL, "", None]
    again = success(h, auto_data(), out)  # a second dispatch changes nothing
    assert contents(again) == ["one" + FOOTER_GENERAL, "two" + FOOTER_GENERAL, "", None]


@needs_litellm
def test_imitated_footer_is_replaced_not_doubled():
    h = make_handler()
    resp = response(contents=("answer\n\n---\n*muse-glimmer · local-code*",))
    assert contents(success(h, auto_data(), resp)) == ["answer" + FOOTER_GENERAL]


@needs_litellm
def test_reasoning_content_is_untouched():
    h = make_handler()
    out = success(h, auto_data(), response(reasoning="thinking..."))
    assert out.choices[0].message.reasoning_content == "thinking..."
    assert out.choices[0].message.content == "hello" + FOOTER_GENERAL


@needs_litellm
def test_toggle_off():
    h = make_handler(auto_footer=False)
    resp = response()
    assert run(h.async_post_call_success_hook(data=auto_data(), user_api_key_dict=None, response=resp)) is None
    assert contents(resp) == ["hello"]
    msgs = [{"role": "assistant", "content": "a" + FOOTER_GENERAL}]
    assert run(h.async_pre_call_hook(None, None, {"model": "auto", "messages": msgs}, "acompletion")) is None
    assert msgs[0]["content"] == "a" + FOOTER_GENERAL
    chunks = stream_chunks(["a", "b"])
    assert collect(h, chunks) == chunks


@needs_litellm
def test_pre_call_strips_on_auto():
    h = make_handler()
    data = {"model": "auto", "messages": [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1" + FOOTER_GENERAL},
        {"role": "assistant", "content": [{"type": "text", "text": "a2" + FOOTER_GENERAL}]},
        {"role": "user", "content": "q2" + FOOTER_GENERAL},
    ]}
    out = run(h.async_pre_call_hook(None, None, data, "acompletion"))
    assert out is data
    assert data["messages"][1]["content"] == "a1"
    assert data["messages"][2]["content"][0]["text"] == "a2"
    assert data["messages"][3]["content"] == "q2" + FOOTER_GENERAL
    assert h.stats["footers_stripped"] == 2
    assert run(h.async_pre_call_hook(None, None, data, "acompletion")) is None  # idempotent


@needs_litellm
def test_exceptions_never_escape():
    h = make_handler()

    def boom(*_a, **_k):
        raise RuntimeError("bug")

    h.footer_label = boom  # type: ignore[assignment]
    resp = response()
    assert run(h.async_post_call_success_hook(data=auto_data(), user_api_key_dict=None, response=resp)) is None
    assert contents(resp) == ["hello"]
    chunks = stream_chunks(["a", "b"])
    assert [_text(c) for c in collect(h, chunks)] == [_text(c) for c in stream_chunks(["a", "b"])]
    assert h.stats["footer_errors"] >= 2

    # A router that raises: the name header is used instead.
    class BadRouter:
        def get_deployment(self, model_id):
            raise RuntimeError("router bug")

    h2 = make_handler(router=BadRouter())
    assert contents(success(h2, auto_data(), response())) == ["hello" + FOOTER_GENERAL]
    # Garbage in, nothing out -- and no exception.
    assert run(h2.async_post_call_success_hook(data=None, user_api_key_dict=None, response=None)) is None
    assert run(h2.async_post_call_success_hook(data=auto_data(), user_api_key_dict=None, response=object())) is None
    assert run(h2.async_pre_call_hook(None, None, {"model": "auto", "messages": "nope"}, "acompletion")) is None
    assert run(h2.async_pre_call_hook(None, None, None, "acompletion")) is None


# ── streaming ──────────────────────────────────────────────────────────────
def stream_chunks(texts, finish="stop", finish_text=None, headers=None, n=1, reasoning=None, usage_chunk=True):
    hidden = {"additional_headers": dict(headers if headers is not None else routed_headers())}
    out = []
    if reasoning:
        for i in range(n):
            out.append(ModelResponseStream(id="c", created=1, model="auto",
                                           choices=[StreamingChoices(index=i, delta=Delta(reasoning_content=reasoning))]))
    for t in texts:
        for i in range(n):
            out.append(ModelResponseStream(id="c", created=1, model="auto",
                                           choices=[StreamingChoices(index=i, delta=Delta(content=t))]))
    if finish:
        for i in range(n):
            out.append(ModelResponseStream(id="c", created=1, model="auto", choices=[
                StreamingChoices(index=i, delta=Delta(content=finish_text), finish_reason=finish)]))
    if usage_chunk:
        out.append(ModelResponseStream(id="c", created=1, model="auto", choices=[]))
    for c in out:
        c._hidden_params = copy.deepcopy(hidden)
    return out


class Wrapper:
    """Stands in for CustomStreamWrapper: async-iterable with _hidden_params."""

    def __init__(self, chunks, headers=None):
        self._chunks = chunks
        self._hidden_params = {"additional_headers": dict(headers if headers is not None else routed_headers())}

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for c in self._chunks:
            yield c


def collect(h, chunks, data=None, headers=None):
    async def go():
        out = []
        async for c in h.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None, response=Wrapper(chunks, headers), request_data=data or auto_data(stream=True)):
            out.append(c)
        return out

    return run(go())


def _text(chunk):
    return "".join((c.delta.content or "") for c in chunk.choices)


def joined(chunks, index=0):
    return "".join((c.delta.content or "") for ch in chunks for c in ch.choices if c.index == index)


def finish_position(chunks, index=0):
    return next(k for k, ch in enumerate(chunks) for c in ch.choices if c.index == index and c.finish_reason)


@needs_litellm
def test_stream_footer_goes_before_the_finish_chunk():
    chunks = stream_chunks(["hel", "lo"])
    out = collect(make_handler(), chunks)
    assert len(out) == len(chunks) + 1
    assert joined(out) == "hello" + FOOTER_GENERAL
    k = finish_position(out)
    assert _text(out[k - 1]) == FOOTER_GENERAL  # the extra chunk is right before finish
    assert out[k - 1].choices[0].finish_reason is None
    assert out[k - 1].id == "c" and out[k - 1].model == "auto"
    assert out[-1].choices == []  # the usage chunk still comes last


@needs_litellm
def test_stream_finish_chunk_with_text_gets_the_footer_appended():
    chunks = stream_chunks(["hel"], finish_text="lo")
    out = collect(make_handler(), chunks)
    assert len(out) == len(chunks)
    assert joined(out) == "hello" + FOOTER_GENERAL
    assert _text(out[finish_position(out)]) == "lo" + FOOTER_GENERAL


@needs_litellm
def test_stream_without_finish_chunk_gets_footer_at_the_end():
    out = collect(make_handler(), stream_chunks(["hello"], finish=None, usage_chunk=False))
    assert joined(out) == "hello" + FOOTER_GENERAL


@needs_litellm
def test_stream_scope_tools_tasks_and_reasoning():
    h = make_handler()
    # non-auto: identical objects, nothing added
    chunks = stream_chunks(["a"])
    assert collect(h, chunks, data=auto_data(model="local-general", stream=True)) == chunks
    # tool call streamed: skipped
    tool = ModelResponseStream(id="c", created=1, model="auto", choices=[StreamingChoices(index=0, delta=Delta(
        tool_calls=[ChatCompletionDeltaToolCall(id="t", index=0, type="function", function=Function(name="f", arguments="{}"))]))])
    chunks = stream_chunks(["thinking out loud"], finish="tool_calls")
    chunks.insert(1, tool)
    assert FOOTER_GENERAL not in joined(collect(h, chunks))
    # response_format / Open WebUI task: skipped
    assert collect(h, stream_chunks(["{}"]), data=auto_data(stream=True, response_format={"type": "json_object"}))
    assert FOOTER_GENERAL not in joined(collect(
        h, stream_chunks(["Rain"]), data=auto_data(stream=True, messages=[{"role": "user", "content": OWUI_TITLE}])))
    # reasoning-only stream: nothing to put a footer on
    assert FOOTER_GENERAL not in joined(collect(h, stream_chunks([], reasoning="hmm")))
    # reasoning + content: reasoning untouched, footer on content
    out = collect(h, stream_chunks(["hi"], reasoning="hmm"))
    assert out[0].choices[0].delta.reasoning_content == "hmm"
    assert joined(out) == "hi" + FOOTER_GENERAL


@needs_litellm
def test_stream_n_greater_than_one():
    out = collect(make_handler(), stream_chunks(["x", "y"], n=2))
    assert joined(out, 0) == "xy" + FOOTER_GENERAL
    assert joined(out, 1) == "xy" + FOOTER_GENERAL
    assert sum(_text(c).count("---") for c in out) == 2  # once per choice, never twice


@needs_litellm
def test_stream_claude_and_bypass_labels():
    clock, pages = Clock(), {}
    h = make_handler(pages, clock)
    _learn_and_poll(h, pages, clock, ConnectionError("x"), ConnectionError("x"))
    out = collect(h, stream_chunks(["hi"], headers=CLAUDE), headers=CLAUDE)
    assert joined(out).endswith("*claude-sonnet-5 · local-general · overflow (local down)*")
    data = auto_data(stream=True, deployment={"model_name": "local-private", "litellm_params": {"model": "openai/muse-glimmer"}})
    out = collect(h, stream_chunks(["hi"], headers=BYPASS_HEADERS), data=data, headers=BYPASS_HEADERS)
    assert joined(out) == "hi\n\n---\n*muse-glimmer · local-private · router bypassed*"


@needs_litellm
def test_stream_upstream_errors_still_propagate():
    """Never-throw covers OUR code; an upstream stream error is a real error."""

    class Broken:
        _hidden_params = {}

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise ValueError("upstream died")

    async def go():
        async for _ in make_handler().async_post_call_streaming_iterator_hook(
                user_api_key_dict=None, response=Broken(), request_data=auto_data(stream=True)):
            pass

    with pytest.raises(ValueError):
        run(go())


# ── proxy plumbing (litellm[proxy]==1.95.0) ────────────────────────────────
try:
    from litellm.caching.dual_cache import DualCache  # noqa: E402
    from litellm.proxy._types import UserAPIKeyAuth  # noqa: E402
    from litellm.proxy.utils import ProxyLogging  # noqa: E402
except Exception:  # proxy extras (backoff, fastapi, ...) not installed
    ProxyLogging = None

needs_proxy = pytest.mark.skipif(ProxyLogging is None, reason="needs litellm[proxy]==1.95.0")


@pytest.fixture
def registered():
    h = make_handler()
    old = litellm.callbacks
    litellm.callbacks = [h]
    try:
        yield h
    finally:
        litellm.callbacks = old


@needs_proxy
def test_proxy_activates_the_hooks_defined_on_the_class(registered):
    caps = ProxyLogging._callback_capabilities()
    assert caps.has_iterator_override and caps.has_pre_call_override
    assert (registered, "override") in caps.iterator_overrides
    assert ProxyLogging(user_api_key_cache=DualCache()).needs_iterator_wrap()


@needs_proxy
def test_proxy_dispatches_all_three_hooks(registered):
    pl = ProxyLogging(user_api_key_cache=DualCache())
    key = UserAPIKeyAuth()

    data = {"model": "auto", "messages": [{"role": "assistant", "content": "a" + FOOTER_GENERAL}]}
    data = run(pl.pre_call_hook(user_api_key_dict=key, data=data, call_type="acompletion"))
    assert data["messages"][0]["content"] == "a"

    out = run(pl.post_call_success_hook(data=auto_data(), response=response(), user_api_key_dict=key))
    assert contents(out) == ["hello" + FOOTER_GENERAL]

    async def stream():
        res = []
        async for c in pl.async_post_call_streaming_iterator_hook(
                response=Wrapper(stream_chunks(["hi"])), user_api_key_dict=key, request_data=auto_data(stream=True)):
            res.append(c)
        return res

    assert joined(run(stream())) == "hi" + FOOTER_GENERAL


# A stand-in for Envoy + the inner LiteLLM: answers /v1/chat/completions with
# the response headers the real ones add (x-vsr-* from the router,
# x-litellm-model-* from the inner proxy).
UPSTREAM_HEADERS = {
    "x-vsr-selected-decision": "general",
    "x-vsr-selected-model": "local-general",
    "x-litellm-model-id": "id-gen-qwen",
    "x-litellm-model-name": "openai/qwen3.8-solo",
    "x-litellm-model-group": "local-general",
}


class _Upstream(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
        self.send_response(200)
        for k, v in UPSTREAM_HEADERS.items():
            self.send_header(k, v)
        if body.get("stream"):
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            for delta, fin in (({"role": "assistant", "content": "hel"}, None), ({"content": "lo"}, None), ({}, "stop")):
                chunk = {"id": "u", "object": "chat.completion.chunk", "created": 1, "model": "local-general",
                         "choices": [{"index": 0, "delta": delta, "finish_reason": fin}]}
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            payload = json.dumps({"id": "u", "object": "chat.completion", "created": 1, "model": "local-general",
                                  "choices": [{"index": 0, "message": {"role": "assistant", "content": "hello"},
                                               "finish_reason": "stop"}],
                                  "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}).encode()
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)


@pytest.fixture
def upstream(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        server.shutdown()
        server.server_close()


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@needs_litellm
def test_outer_provider_surfaces_upstream_headers_json_and_sse(upstream):
    """The UNVERIFIED-in-the-plan bit: the outer openai provider keeps the
    router's and the inner LiteLLM's headers as llm_provider-* -- on the
    response for JSON, and on the CustomStreamWrapper for SSE."""
    router = litellm.Router(model_list=[{"model_name": "auto", "litellm_params": {
        "model": "openai/vllm-sr/auto", "api_base": upstream, "api_key": "sk-test"}}])
    h = make_handler()

    resp = run(router.acompletion(model="auto", messages=[{"role": "user", "content": "hi"}]))
    hdrs = resp._hidden_params["additional_headers"]
    assert hdrs["llm_provider-x-litellm-model-id"] == "id-gen-qwen"
    assert hdrs["llm_provider-x-vsr-selected-model"] == "local-general"
    assert hdrs["x-litellm-model-group"] == "auto"  # the outer group, bare
    data = auto_data(deployment=router.get_deployment(model_id=resp._hidden_params["model_id"]))
    assert contents(success(h, data, resp)) == ["hello" + FOOTER_GENERAL]

    async def stream():
        wrapper = await router.acompletion(model="auto", messages=[{"role": "user", "content": "hi"}], stream=True)
        assert wrapper._hidden_params["additional_headers"]["llm_provider-x-litellm-model-id"] == "id-gen-qwen"
        out = []
        async for c in h.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None, response=wrapper, request_data=auto_data(stream=True, deployment=data["deployment"])):
            out.append(c)
        return out

    out = run(stream())
    assert joined(out) == "hello" + FOOTER_GENERAL


@needs_litellm
def test_cross_group_fallback_to_local_private_is_router_bypassed():
    """`auto` down -> fallbacks: auto -> [local-private]. The proxy's own data
    dict keeps model="auto" (route_request unpacks it, route_llm_request.py:404),
    and data["deployment"] -- what the proxy resolves from the response's
    model_id -- is the local-private deployment."""
    port = _closed_port()
    router = litellm.Router(
        model_list=[
            {"model_name": "auto", "litellm_params": {"model": "openai/vllm-sr/auto",
                                                      "api_base": f"http://127.0.0.1:{port}/v1", "api_key": "x"}},
            {"model_name": "local-private", "litellm_params": {"model": "openai/qwen3.8-solo",
                                                               "api_base": QWEN, "api_key": "none",
                                                               "mock_response": "answered locally"}},
        ],
        fallbacks=[{"auto": ["local-private"]}],
        num_retries=0,
    )
    data = {"model": "auto", "messages": [{"role": "user", "content": "hi"}], "timeout": 5}
    resp = run(router.acompletion(**data))
    assert data["model"] == "auto"
    hidden = resp._hidden_params
    assert hidden["additional_headers"]["x-litellm-model-group"] == "local-private"
    assert int(hidden["additional_headers"]["x-litellm-attempted-fallbacks"]) == 1
    data["deployment"] = router.get_deployment(model_id=hidden["model_id"])
    assert data["deployment"].model_name == "local-private"
    out = success(make_handler(), data, resp)
    assert contents(out) == ["answered locally\n\n---\n*qwen3.8-solo · local-private · router bypassed*"]


@needs_litellm
def test_in_group_order_fallback_reports_the_final_deployment_id():
    """What the inner proxy sends as x-litellm-model-id is the response's
    _hidden_params["model_id"]; after an order-1 failure it is order 2's."""
    port = _closed_port()
    router = litellm.Router(model_list=[
        {"model_name": "local-private", "litellm_params": {"model": "openai/qwen3.8-solo", "order": 1,
                                                           "api_base": f"http://127.0.0.1:{port}/v1", "api_key": "x"},
         "model_info": {"id": "id-qwen"}},
        {"model_name": "local-private", "litellm_params": {"model": "openai/muse-glimmer", "order": 2,
                                                           "api_base": MUSE, "api_key": "x", "mock_response": "two"},
         "model_info": {"id": "id-muse"}},
    ], num_retries=0)
    resp = run(router.acompletion(model="local-private", messages=[{"role": "user", "content": "hi"}], timeout=5))
    assert resp._hidden_params["model_id"] == "id-muse"
    assert resp._hidden_params["additional_headers"]["x-litellm-model-group"] == "local-private"


@needs_litellm
def test_env_toggle_reaches_the_instance_litellm_loads(monkeypatch):
    from litellm.proxy.types_utils.utils import get_instance_fn

    monkeypatch.setenv("AUTO_FOOTER_ENABLED", "false")
    h = get_instance_fn("overflow.handler", config_file_path=str(_LITELLM_DIR / "litellm_config.yaml"))
    assert h.auto_footer is False
    monkeypatch.setenv("AUTO_FOOTER_ENABLED", "1")
    h = get_instance_fn("overflow.handler", config_file_path=str(_LITELLM_DIR / "litellm_config.yaml"))
    assert h.auto_footer is True
    monkeypatch.delenv("AUTO_FOOTER_ENABLED")
    assert overflow._env_bool("AUTO_FOOTER_ENABLED", True) is True  # default on
