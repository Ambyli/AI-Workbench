"""Unit tests for the `auto` footer -- ai/litellm/hooks/footer, driven through
hooks.handler.Handler's three hooks.

When a client calls model `auto`, the route-policy hook rewrites it to a chain
alias and LiteLLM serves that chain in the same pass; the assistant text ends
with a markdown footer naming what answered:

    <answer>

    ---
    *qwen3.8-solo · local-general*

Everything the footer names comes from that one request: data["deployment"]
(the proxy sets it from the response's model id before the success and
streaming hooks run) and the route-policy stash in data["metadata"].

Three layers:

* Pure helpers (always run): footer regex / stripping, Open WebUI task
  detection, the env toggle parser.
* The hooks against real litellm types (skipped unless `litellm` is
  importable): non-stream and stream, scope (only requests the client sent
  as `auto`), overflow busy/down/plain, the route-policy fallback tag, tool
  calls / response_format / Open WebUI tasks / non-chat responses skipped,
  n > 1, idempotence, reasoning untouched, exception safety.
* Against real LiteLLM: a Router with this repo's `auto` alias serves the
  rewritten chain (or local-private when nothing rewrote it) and the footer
  names what data["deployment"] resolves to; the proxy (litellm[proxy]==
  1.95.0) really activates and dispatches these hooks on Handler.

    python -m pytest unit-tests/litellm -q -p no:cacheprovider
"""

from __future__ import annotations

import asyncio
import copy
import pathlib
import socket

import pytest

from hooks.footer.render import footer_text, is_openwebui_task, strip_footer, strip_footers_from_messages
from hooks.handler import Handler
from hooks.overflow.vllm import metrics_url
from hooks.routing.stash import STASH_KEY
from hooks.settings import _env_bool

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_LITELLM_DIR = _ROOT / "ai" / "litellm"

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
        ("hi" + footer_text("qwen3.8-solo · local-private · route-policy fallback"), "hi"),
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
# The default RAG template, PREPENDED to the user's own message when a model
# with knowledge retrieves sources (RAG_SYSTEM_CONTEXT unset ->
# add_or_update_user_message(..., append=False)). Starts "### Task:" too.
OWUI_RAG = (
    "### Task:\nRespond to the user query using the provided context, incorporating inline citations in the format [id] "
    "**only when the <source> tag includes an explicit id attribute** (e.g., <source id=\"1\">).\n...\n"
    "<context>\n<source id=\"1\">...</source>\n</context>\n"
    "\nHow long did projects sit in the 'Audit and Submit M1' queue?"
)


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
    # Open WebUI sends every task with stream=False; a streamed request is a chat answer.
    data["stream"] = True
    assert not is_openwebui_task(data)


def test_streamed_answer_with_rag_template_is_not_a_task():
    """Regression (chat ed3ef662, 2026-10-14): a knowledge-backed model's answer
    lost its footer because the "### Task:" RAG template is prepended to the
    user's message -- for every request of the turn, tool loop included."""
    data = {"model": "auto", "stream": True, "messages": [
        {"role": "system", "content": "You are the office assistant."},
        {"role": "user", "content": OWUI_RAG},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "t"}]},
        {"role": "tool", "tool_call_id": "t", "content": "[]"},
    ]}
    assert not is_openwebui_task(data)
    data["messages"][1] = {"role": "user", "content": [{"type": "text", "text": OWUI_RAG}]}
    assert not is_openwebui_task(data)


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


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def make_handler(pages=None, clock=None, **kw) -> Handler:
    """The route-policy client is off (conftest), so these tests drive the
    post-call hooks with data shaped like a request the client DID route."""
    pages = pages if pages is not None else {}

    async def fetch(url):
        page = pages.get(url)
        if isinstance(page, BaseException):
            raise page
        if page is None:
            raise ConnectionError("no route")
        return page

    return Handler(fetch=fetch, clock=clock or Clock(), busy_after=2.0, idle_after=5.0,
                   poll_interval=1.0, probe_timeout=0.5, start_poller=False,
                   auto_footer=kw.pop("auto_footer", True), **kw)


def deployment(chain="local-general", model="openai/qwen3.8-solo", **params):
    """data["deployment"] as the proxy stores it (a Deployment's dict form)."""
    return {"model_name": chain, "litellm_params": {"model": model, **params}, "model_info": {"id": f"id-{chain}"}}


def stash(route="local-general", source="engine", decision_id="d-1"):
    return {"requested_model": "auto", "route": route, "source": source, "decision_id": decision_id,
            "litellm_call_id": "call-1", "observed": False}


def auto_data(chain="local-general", model="openai/qwen3.8-solo", source="engine", **kw):
    """An `auto` request as the post-call hooks see it: the pre-call hook
    rewrote `model` to the chain and stashed the route; the proxy stored the
    deployment that answered."""
    data = {"model": chain, "stream": False, "messages": [{"role": "user", "content": "hi"}],
            "metadata": {STASH_KEY: stash(chain, source)}, "deployment": deployment(chain, model)}
    data.update(kw)
    return data


def direct_data(model="local-general", **kw):
    """A direct chain / alias call: no stash."""
    data = {"model": model, "stream": False, "messages": [{"role": "user", "content": "hi"}],
            "metadata": {}, "deployment": deployment(model if model.startswith("local-") else "x")}
    data.update(kw)
    return data


def group_headers(chain="local-general"):
    """What the Router puts in _hidden_params["additional_headers"] (router.py:9388)."""
    return {"x-litellm-model-group": chain}


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
    r._hidden_params = {"additional_headers": dict(headers if headers is not None else group_headers())}
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
def test_the_footer_names_the_served_deployment_and_chain():
    """Both parts come from data["deployment"]: model_name is the chain,
    litellm_params.model the deployment (an order-2 answer names order 2)."""
    h = make_handler()
    out = success(h, auto_data("local-code", "openai/muse-glimmer"), response(headers=group_headers("local-code")))
    assert contents(out) == ["hello\n\n---\n*muse-glimmer · local-code*"]
    # The deployment wins over a disagreeing group header.
    out = success(h, auto_data(), response(headers=group_headers("local-reasoning")))
    assert contents(out) == ["hello" + FOOTER_GENERAL]


@needs_litellm
def test_without_a_deployment_there_is_no_model_name_and_no_footer():
    """x-litellm-model-group can name the chain, never the deployment."""
    h = make_handler()
    resp = response()
    assert run(h.async_post_call_success_hook(data=auto_data(deployment=None), user_api_key_dict=None,
                                              response=resp)) is None
    assert contents(resp) == ["hello"]


@needs_litellm
def test_non_auto_is_untouched():
    """A direct chain or alias call -- no stash -- gets no footer, and its
    history is not stripped."""
    h = make_handler()
    for model in ("local-general", "local-private", "qwen3.8-solo", "claude-sonnet-5"):
        resp = response()
        assert run(h.async_post_call_success_hook(data=direct_data(model), user_api_key_dict=None,
                                                  response=resp)) is None
        assert contents(resp) == ["hello"]
        msgs = [{"role": "assistant", "content": "a" + FOOTER_GENERAL}]
        assert run(h.async_pre_call_hook(None, None, {"model": model, "messages": msgs}, "acompletion")) is None
        assert msgs[0]["content"] == "a" + FOOTER_GENERAL


@needs_litellm
def test_a_client_sent_stash_on_a_direct_call_gets_no_footer():
    """The pre-call hook drops a stash the request body carried, so a client
    cannot make a direct chain call look like `auto`."""
    h = make_handler()
    data = direct_data("local-general")
    data["metadata"][STASH_KEY] = stash()
    assert run(h.async_pre_call_hook(None, None, data, "acompletion")) is None
    assert STASH_KEY not in data["metadata"]
    resp = response()
    assert run(h.async_post_call_success_hook(data=data, user_api_key_dict=None, response=resp)) is None


def general_chain():
    return [
        {"model_name": "local-general", "litellm_params": {"model": "openai/qwen3.8-solo", "order": 1, "api_base": QWEN}},
        {"model_name": "local-general", "litellm_params": {"model": "openai/muse-glimmer", "order": 2, "api_base": MUSE}},
        {"model_name": "local-general", "litellm_params": {"model": "anthropic/claude-sonnet-5", "order": 3}},
    ]


def _learn_and_poll(h, pages, clock, qwen, muse):
    h.overflow.filter_deployments("local-general", general_chain())  # what serving the request does
    pages[metrics_url(QWEN)] = qwen
    pages[metrics_url(MUSE)] = muse
    run(h.overflow.poller.poll_once())
    clock.advance(2.5)
    run(h.overflow.poller.poll_once())


def _metrics(waiting):
    return f'vllm:num_requests_waiting{{engine="0"}} {waiting}\nvllm:num_requests_running{{engine="0"}} 1\n'


def claude_data(**kw):
    return auto_data("local-general", "anthropic/claude-sonnet-5", **kw)


@needs_litellm
def test_overflow_local_busy():
    clock, pages = Clock(), {}
    h = make_handler(pages, clock)
    _learn_and_poll(h, pages, clock, _metrics(3), _metrics(3))
    out = success(h, claude_data(), response())
    assert contents(out) == ["hello\n\n---\n*claude-sonnet-5 · local-general · overflow (local busy)*"]


@needs_litellm
def test_overflow_local_down():
    clock, pages = Clock(), {}
    h = make_handler(pages, clock)
    _learn_and_poll(h, pages, clock, ConnectionError("refused"), ConnectionError("refused"))
    out = success(h, claude_data(), response())
    assert contents(out) == ["hello\n\n---\n*claude-sonnet-5 · local-general · overflow (local down)*"]


@needs_litellm
def test_overflow_busy_wins_over_down_and_unknown_is_plain():
    clock, pages = Clock(), {}
    h = make_handler(pages, clock)
    _learn_and_poll(h, pages, clock, _metrics(3), ConnectionError("refused"))
    assert "overflow (local busy)" in contents(success(h, claude_data(), response()))[0]
    # A fresh handler that never filtered the chain (another worker) has no verdicts: plain.
    h2 = make_handler()
    assert contents(success(h2, claude_data(), response()))[0].endswith(
        "*claude-sonnet-5 · local-general · overflow*")
    out = success(h2, auto_data("local-reasoning", "anthropic/claude-opus-5-5"),
                  response(headers=group_headers("local-reasoning")))
    assert contents(out)[0].endswith("*claude-opus-5-5 · local-reasoning · overflow*")


@needs_litellm
@pytest.mark.parametrize("source", ["fallback:timeout", "fallback:disabled", "fallback:not-applied",
                                    "fallback:http-500"])
def test_route_policy_fallback(source):
    """The engine did not route the request: local-private answered, and the
    footer says so."""
    h = make_handler()
    data = auto_data("local-private", "openai/qwen3.8-solo", source=source)
    out = success(h, data, response(headers=group_headers("local-private")))
    assert contents(out) == ["hello\n\n---\n*qwen3.8-solo · local-private · route-policy fallback*"]


@needs_litellm
def test_an_engine_routed_local_private_is_not_a_fallback():
    h = make_handler()
    out = success(h, auto_data("local-private", "openai/muse-glimmer"), response())
    assert contents(out) == ["hello\n\n---\n*muse-glimmer · local-private*"]


@needs_litellm
def test_an_auto_request_the_hook_never_rewrote_is_a_fallback():
    """No stash and model still `auto`: the `auto` alias served it from
    local-private (router_settings.model_group_alias)."""
    h = make_handler()
    data = {"model": "auto", "messages": [{"role": "user", "content": "hi"}],
            "deployment": deployment("local-private", "openai/qwen3.8-solo")}
    out = success(h, data, response())
    assert contents(out) == ["hello\n\n---\n*qwen3.8-solo · local-private · route-policy fallback*"]


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
def test_an_anthropic_messages_response_is_left_alone():
    """/v1/messages (Claude Code) routes `auto` the same way, but its response
    is an Anthropic message dict, not a chat completion: no footer, no error."""
    h = make_handler()
    resp = {"id": "msg_1", "type": "message", "role": "assistant", "model": "auto", "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "hello"}]}
    before = copy.deepcopy(resp)
    assert run(h.async_post_call_success_hook(data=auto_data(), user_api_key_dict=None, response=resp)) is None
    assert resp == before and h.stats["footer_errors"] == 0


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
    data = {"model": "auto", "messages": msgs}
    run(h.async_pre_call_hook(None, None, data, "acompletion"))
    assert msgs[0]["content"] == "a" + FOOTER_GENERAL  # not stripped...
    assert data["model"] == "local-private"  # ...but still routed (the client is off here)
    chunks = stream_chunks(["a", "b"])
    assert collect(h, chunks) == chunks


@needs_litellm
def test_pre_call_strips_on_auto_before_the_route():
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
    # Routed after the strip (the client is off in these tests: local-private).
    assert data["model"] == "local-private" and data["metadata"][STASH_KEY]["route"] == "local-private"
    # A second pass is a direct local-private call now: nothing to strip, nothing to route.
    assert run(h.async_pre_call_hook(None, None, data, "acompletion")) is None


@needs_litellm
def test_exceptions_never_escape():
    h = make_handler()

    def boom(*_a, **_k):
        raise RuntimeError("bug")

    h.footer.label = boom  # type: ignore[assignment]
    resp = response()
    assert run(h.async_post_call_success_hook(data=auto_data(), user_api_key_dict=None, response=resp)) is None
    assert contents(resp) == ["hello"]
    chunks = stream_chunks(["a", "b"])
    assert [_text(c) for c in collect(h, chunks)] == [_text(c) for c in stream_chunks(["a", "b"])]
    assert h.stats["footer_errors"] >= 2

    # A deployment object that raises on every read: no footer, no exception.
    class Hostile:
        def __getattr__(self, name):
            raise RuntimeError("deployment bug")

    h2 = make_handler()
    resp = response()
    assert run(h2.async_post_call_success_hook(data=auto_data(deployment=Hostile()), user_api_key_dict=None,
                                               response=resp)) is None
    assert contents(resp) == ["hello"]
    # Garbage in, nothing out -- and no exception.
    assert run(h2.async_post_call_success_hook(data=None, user_api_key_dict=None, response=None)) is None
    assert run(h2.async_post_call_success_hook(data=auto_data(), user_api_key_dict=None, response=object())) is None
    assert run(h2.async_pre_call_hook(None, None, None, "acompletion")) is None


# ── streaming ──────────────────────────────────────────────────────────────
def stream_chunks(texts, finish="stop", finish_text=None, headers=None, n=1, reasoning=None, usage_chunk=True):
    hidden = {"additional_headers": dict(headers if headers is not None else group_headers())}
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
        self._hidden_params = {"additional_headers": dict(headers if headers is not None else group_headers())}

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
    # a direct chain call: identical objects, nothing added
    chunks = stream_chunks(["a"])
    assert collect(h, chunks, data=direct_data("local-general", stream=True)) == chunks
    # tool call streamed: skipped
    tool = ModelResponseStream(id="c", created=1, model="auto", choices=[StreamingChoices(index=0, delta=Delta(
        tool_calls=[ChatCompletionDeltaToolCall(id="t", index=0, type="function", function=Function(name="f", arguments="{}"))]))])
    chunks = stream_chunks(["thinking out loud"], finish="tool_calls")
    chunks.insert(1, tool)
    assert FOOTER_GENERAL not in joined(collect(h, chunks))
    # response_format / Open WebUI legacy function calling: skipped
    assert collect(h, stream_chunks(["{}"]), data=auto_data(stream=True, response_format={"type": "json_object"}))
    assert FOOTER_GENERAL not in joined(collect(h, stream_chunks(["Rain"]), data=auto_data(stream=True, messages=[
        {"role": "system", "content": "Available Tools: [{...}]"}, {"role": "user", "content": "Query: rain?"}])))
    # A streamed answer under the "### Task:" RAG template is a chat answer: footer.
    assert joined(collect(h, stream_chunks(["Rain"]), data=auto_data(
        stream=True, messages=[{"role": "user", "content": OWUI_RAG}]))) == "Rain" + FOOTER_GENERAL
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
def test_stream_claude_and_fallback_labels():
    clock, pages = Clock(), {}
    h = make_handler(pages, clock)
    _learn_and_poll(h, pages, clock, ConnectionError("x"), ConnectionError("x"))
    out = collect(h, stream_chunks(["hi"]), data=claude_data(stream=True))
    assert joined(out).endswith("*claude-sonnet-5 · local-general · overflow (local down)*")
    data = auto_data("local-private", "openai/muse-glimmer", source="fallback:timeout", stream=True)
    out = collect(h, stream_chunks(["hi"], headers=group_headers("local-private")), data=data,
                  headers=group_headers("local-private"))
    assert joined(out) == "hi\n\n---\n*muse-glimmer · local-private · route-policy fallback*"


@needs_litellm
def test_stream_of_anthropic_events_passes_through():
    """A /v1/messages stream carries Anthropic events, not chat chunks: the
    footer adds nothing and every event passes through unchanged."""
    events = [b'event: message_start\ndata: {"type":"message_start"}\n\n',
              {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hi"}},
              b'event: message_stop\ndata: {"type":"message_stop"}\n\n']

    class Raw:
        _hidden_params = {}

        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            for e in events:
                yield e

    async def go():
        return [c async for c in make_handler().async_post_call_streaming_iterator_hook(
            user_api_key_dict=None, response=Raw(), request_data=auto_data(stream=True))]

    assert run(go()) == events


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


# ── against a real Router with this repo's `auto` alias ────────────────────
def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _alias_router(**kw):
    def dep(chain, model, order, reply):
        return {"model_name": chain, "litellm_params": {"model": model, "order": order, "api_key": "none",
                                                        "api_base": QWEN if "qwen" in model else MUSE,
                                                        "mock_response": reply},
                "model_info": {"id": f"id-{chain}-{order}"}}

    return litellm.Router(
        model_list=[dep("local-private", "openai/qwen3.8-solo", 1, "private answer"),
                    dep("local-private", "openai/muse-glimmer", 2, "private answer 2"),
                    dep("local-code", "openai/qwen3.8-solo", 1, "code answer")],
        model_group_alias={"auto": "local-private"}, num_retries=0, **kw)


@needs_litellm
def test_routed_auto_is_served_by_its_chain_and_the_footer_names_it():
    """What the proxy does after the pre-call hook rewrote `auto`: route on
    the chain, then store the deployment resolved from the response's model
    id in data["deployment"] (common_request_processing.py:1707)."""
    router = _alias_router()
    data = auto_data("local-code", "unused")
    resp = run(router.acompletion(model=data["model"], messages=data["messages"]))
    data["deployment"] = router.get_deployment(model_id=resp._hidden_params["model_id"])
    assert resp._hidden_params["additional_headers"]["x-litellm-model-group"] == "local-code"
    assert contents(success(make_handler(), data, resp)) == ["code answer\n\n---\n*qwen3.8-solo · local-code*"]


@needs_litellm
def test_unrewritten_auto_is_served_by_local_private_through_the_alias():
    """If nothing rewrote it, `auto` resolves to local-private at deployment
    selection; the request keeps model `auto`, and the footer flags it."""
    router = _alias_router()
    data = {"model": "auto", "messages": [{"role": "user", "content": "hi"}]}
    resp = run(router.acompletion(**data))
    assert data["model"] == "auto"
    data["deployment"] = router.get_deployment(model_id=resp._hidden_params["model_id"])
    assert data["deployment"].model_name == "local-private"
    assert contents(success(make_handler(), data, resp)) == [
        "private answer\n\n---\n*qwen3.8-solo · local-private · route-policy fallback*"]


@needs_litellm
def test_in_group_order_fallback_reports_the_final_deployment_id():
    """data["deployment"] is resolved from the response's
    _hidden_params["model_id"]; after an order-1 failure it is order 2's, so
    the footer names the deployment that really answered."""
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
    data = auto_data("local-private", "unused", deployment=router.get_deployment(model_id="id-muse"))
    assert contents(success(make_handler(), data, resp)) == ["two\n\n---\n*muse-glimmer · local-private*"]


@needs_litellm
def test_env_toggle_reaches_the_instance_litellm_loads(monkeypatch, fresh_hooks):
    """hooks.settings reads the environment once per process, at import -- so
    each load is against a fresh import of the package, as each proxy start is."""
    from litellm.proxy.types_utils.utils import get_instance_fn

    monkeypatch.setenv("AUTO_FOOTER_ENABLED", "false")
    with fresh_hooks():
        h = get_instance_fn("overflow.handler", config_file_path=str(_LITELLM_DIR / "litellm_config.yaml"))
    assert h.footer.enabled is False
    monkeypatch.setenv("AUTO_FOOTER_ENABLED", "1")
    with fresh_hooks():
        h = get_instance_fn("overflow.handler", config_file_path=str(_LITELLM_DIR / "litellm_config.yaml"))
    assert h.footer.enabled is True
    monkeypatch.delenv("AUTO_FOOTER_ENABLED")
    assert _env_bool("AUTO_FOOTER_ENABLED", True) is True  # default on


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
    assert data["model"] == "local-private"  # the client is off in these tests: the fallback

    out = run(pl.post_call_success_hook(data=auto_data(), response=response(), user_api_key_dict=key))
    assert contents(out) == ["hello" + FOOTER_GENERAL]

    async def stream():
        res = []
        async for c in pl.async_post_call_streaming_iterator_hook(
                response=Wrapper(stream_chunks(["hi"])), user_api_key_dict=key, request_data=auto_data(stream=True)):
            res.append(c)
        return res

    assert joined(run(stream())) == "hi" + FOOTER_GENERAL
