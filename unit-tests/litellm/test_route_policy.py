"""Unit tests for the route-policy client in shadow mode --
ai/litellm/hooks/routing/policy_client.py, driven through hooks.handler.Handler.

On every `auto` request the pre-call hook FIRES ``POST /decide`` (never
awaited) and stashes the decision id in ``data["metadata"]``; the success,
streaming-iterator and failure hooks read it back and fire ``POST /observe``.
Nothing changes routing, nothing raises, nothing waits.

* Pure helpers (always run): header ids, tool-call names, the backend report.
* The hooks with a fake transport (always run): the exact /decide and
  /observe bodies for non-streaming, streaming and failed `auto` calls, the
  decision-id round trip, exactly-once, nothing for other models or when
  disabled, hook latency with a hung or failing service, the in-flight cap,
  never-raises. Most of these need litellm's response types.
* The real HTTP client: httpx.MockTransport (timeouts, 5xx, the wire format)
  and a stdlib HTTP server.
* The proxy plumbing (skipped unless litellm[proxy]==1.95.0): ProxyLogging
  really dispatches all four paths to Handler and the stash survives them;
  the openai provider never forwards the stash upstream.
* Repo invariants: the defaults agree across settings.py, the compose file
  and .env.example.

    python -m pytest unit-tests/litellm -q -p no:cacheprovider
"""

from __future__ import annotations

import asyncio
import copy
import json
import pathlib
import re
import statistics
import threading
import time
import types
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hooks.footer.render import footer_text
from hooks.handler import Handler
from hooks.overflow.state import STALE
from hooks.overflow.vllm import metrics_url
from hooks.routing.policy_client import (
    DISCONNECTED,
    STASH_KEY,
    HttpxPost,
    request_ids,
    stash_of,
    tool_call_names,
)

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_LITELLM_DIR = _ROOT / "ai" / "litellm"
COMPOSE = _LITELLM_DIR / "docker-compose.litellm.yml"
ENV_EXAMPLE = _ROOT / ".env.example"

QWEN = "http://qwen3.8-solo:8000/v1"
MUSE = "http://muse-glimmer:8000/v1"
SERVICE = "http://route-policy:8000"
FOOTER_GENERAL = footer_text("qwen3.8-solo · local-general")

try:
    import litellm
    from litellm.types.utils import (
        ChatCompletionDeltaToolCall,
        ChatCompletionMessageToolCall,
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

try:
    import httpx
except ImportError:
    httpx = None

needs_httpx = pytest.mark.skipif(httpx is None, reason="needs httpx")

try:
    import yaml
except ImportError:
    yaml = None

needs_yaml = pytest.mark.skipif(yaml is None, reason="needs PyYAML")


# ── fakes ──────────────────────────────────────────────────────────────────
class Recorder:
    """A fake Post: records (url, decoded JSON body) and answers `status`."""

    def __init__(self, status=200, hang=False, exc=None):
        self.status = status
        self.hang = hang
        self.exc = exc
        self.calls = []

    async def __call__(self, url, body):
        self.calls.append((url, json.loads(body.decode("utf-8"))))
        if self.hang:
            await asyncio.sleep(3600)
        if self.exc is not None:
            raise self.exc
        return self.status

    def bodies(self, path):
        return [b for u, b in self.calls if u == SERVICE + path]


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


INNER = {
    "id-gen-qwen": {"model_name": "local-general", "litellm_params": {"model": "openai/qwen3.8-solo", "api_base": QWEN}},
    "id-gen-claude": {"model_name": "local-general", "litellm_params": {"model": "anthropic/claude-sonnet-5"}},
    "id-code-muse": {"model_name": "local-code", "litellm_params": {"model": "openai/muse-glimmer", "api_base": MUSE}},
}


class FakeRouter:
    def get_deployment(self, model_id):
        return copy.deepcopy(INNER.get(model_id))

    def get_model_list(self, model_name=None, team_id=None):
        return [copy.deepcopy(d) for d in INNER.values() if d["model_name"] == model_name]


def make_handler(post=None, enabled=True, clock=None, pages=None, router=FakeRouter(), **kw) -> Handler:
    pages = pages if pages is not None else {}

    async def fetch(url):
        page = pages.get(url)
        if isinstance(page, BaseException):
            raise page
        if page is None:
            raise ConnectionError("no route")
        return page

    return Handler(fetch=fetch, clock=clock or Clock(), busy_after=2.0, idle_after=5.0, poll_interval=1.0,
                   probe_timeout=0.5, start_poller=False, auto_footer=kw.pop("auto_footer", True),
                   router_getter=lambda: router, route_policy=enabled, route_policy_url=kw.pop("url", SERVICE),
                   route_policy_timeout=kw.pop("timeout", 1.0),
                   policy_post=post if post is not None else Recorder(), **kw)


OWUI_HEADERS = {
    # What add_litellm_data_to_request puts in metadata.headers: starlette's
    # lower-cased request headers, minus the auth ones.
    "content-type": "application/json",
    "x-openwebui-chat-id": "chat-9",
    "x-openwebui-message-id": "msg-3",
    "x-openwebui-user-id": "user-7",
    "x-openwebui-user-name": "Amber",
}
AUTO_DEPLOYMENT = {"model_name": "auto", "litellm_params": {"model": "openai/vllm-sr/auto"}, "model_info": {"id": "id-auto"}}
KEY = types.SimpleNamespace(key_alias="open-webui")
TOOLS = [{"type": "function", "function": {"name": "sandbox-mcp_exec", "parameters": {"type": "object"}}}]


def proxy_data(model="auto", stream=False, messages=None, headers=None, tools=None, **kw):
    """A request as the proxy hands it to the hooks (common_request_processing)."""
    data = {
        "model": model,
        "stream": stream,
        "messages": messages if messages is not None else [{"role": "user", "content": "hi"}],
        "litellm_call_id": "call-1",
        "metadata": {"headers": dict(OWUI_HEADERS if headers is None else headers), "user_api_key_alias": "open-webui"},
    }
    if tools is not None:
        data["tools"] = tools
    data.update(kw)
    return data


def routed_headers(model_id="id-gen-qwen", group="local-general", name="openai/qwen3.8-solo"):
    return {
        "x-litellm-model-group": "auto",
        "llm_provider-x-vsr-selected-model": group,
        "llm_provider-x-litellm-model-id": model_id,
        "llm_provider-x-litellm-model-name": name,
        "llm_provider-x-litellm-model-group": group,
    }


def general_chain():
    return [
        {"model_name": "local-general", "litellm_params": {"model": "openai/qwen3.8-solo", "order": 1, "api_base": QWEN}},
        {"model_name": "local-general", "litellm_params": {"model": "openai/muse-glimmer", "order": 2, "api_base": MUSE}},
        {"model_name": "local-general", "litellm_params": {"model": "anthropic/claude-sonnet-5", "order": 3}},
    ]


def metrics(waiting, running=1):
    return f'vllm:num_requests_waiting{{engine="0"}} {waiting}\nvllm:num_requests_running{{engine="0"}} {running}\n'


async def prime_backends(h, pages, clock, qwen, muse):
    """Teach the poller the chain's backends (what an inner request does) and poll twice."""
    h.overflow.filter_deployments("local-general", general_chain())
    pages[metrics_url(QWEN)] = qwen
    pages[metrics_url(MUSE)] = muse
    await h.overflow.poller.poll_once()
    clock.advance(2.5)
    await h.overflow.poller.poll_once()


async def drain(h):
    """Let every background call the client started finish."""
    for _ in range(5):
        tasks = list(h.policy._tasks)
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)


def run(coro):
    return asyncio.run(coro)


def is_uuid4(value):
    try:
        return uuid.UUID(value).version == 4
    except Exception:
        return False


# ── pure helpers ───────────────────────────────────────────────────────────
def test_request_ids_any_case_and_absent():
    assert request_ids(proxy_data()) == {"chat_id": "chat-9", "message_id": "msg-3", "user_id": "user-7"}
    mixed = proxy_data(headers={"X-OpenWebUI-Chat-Id": " chat-1 ", "X-OPENWEBUI-USER-ID": "u"})
    assert request_ids(mixed) == {"chat_id": "chat-1", "message_id": None, "user_id": "u"}
    nothing = {"chat_id": None, "message_id": None, "user_id": None}
    assert request_ids(proxy_data(headers={})) == nothing
    assert request_ids(proxy_data(headers={"x-openwebui-chat-id": ""})) == nothing
    assert request_ids({"metadata": {"headers": "nope"}}) == nothing
    assert request_ids({"metadata": None}) == nothing
    assert request_ids(None) == nothing


def test_tool_call_names_message_delta_and_legacy():
    msg = {"tool_calls": [{"id": "a", "function": {"name": "sandbox-mcp_exec", "arguments": "{}"}},
                          {"id": "b", "function": {"name": "run_command"}},
                          {"id": "c", "function": {"arguments": "more"}}]}  # a later delta: no name
    assert tool_call_names(msg) == ["sandbox-mcp_exec", "run_command"]
    assert tool_call_names({"function_call": {"name": "legacy_fn"}}) == ["legacy_fn"]
    assert tool_call_names({"content": "hi"}) == [] and tool_call_names(None) == []
    assert tool_call_names({"tool_calls": "garbage"}) == []


def test_backend_report_splits_stale_from_unknown():
    async def go():
        clock, pages = Clock(), {}
        h = make_handler(clock=clock, pages=pages)
        await prime_backends(h, pages, clock, metrics(0, 2), metrics(3))
        report = h.overflow.poller.report(clock())
        assert report == {QWEN: {"verdict": "ok", "waiting": 0.0, "running": 2.0},
                          MUSE: {"verdict": "busy", "waiting": 3.0, "running": 1.0}}
        clock.advance(10)  # > 5 polls without a probe: the poller is dead
        assert {v["verdict"] for v in h.overflow.poller.report(clock()).values()} == {STALE}
        # The filter still treats it as unknown: nothing is dropped.
        assert h.overflow.filter_deployments("local-general", general_chain()) == general_chain()
        h.overflow.poller.watch("http://new:8000/v1")  # never probed
        assert h.overflow.poller.report(clock())["http://new:8000/v1"] == {
            "verdict": "unknown", "waiting": None, "running": None}

    run(go())


# ── /decide ────────────────────────────────────────────────────────────────
def test_decide_body_exact_and_fired_in_the_background():
    async def go():
        clock, pages, post = Clock(), {}, Recorder()
        h = make_handler(post, clock=clock, pages=pages)
        await prime_backends(h, pages, clock, metrics(0, 2), metrics(3))
        messages = [
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "build a calculator"},
            {"role": "assistant", "content": "done" + FOOTER_GENERAL},  # stripped before /decide
            {"role": "user", "content": "Looks the same"},
        ]
        data = proxy_data(messages=messages, tools=TOOLS)
        out = await h.async_pre_call_hook(KEY, None, data, "acompletion")
        assert out is data  # the footer strip changed it; the stash alone would not return it
        stash = data["metadata"][STASH_KEY]
        assert post.calls == []  # fired, not awaited: nothing has run yet
        assert h.policy.in_flight == 1
        await drain(h)
        [body] = post.bodies("/decide")
        assert is_uuid4(body["decision_id"]) and body["decision_id"] == stash["decision_id"]
        assert body == {
            "decision_id": stash["decision_id"],
            "requested_model": "auto",
            "litellm_call_id": "call-1",
            "chat_id": "chat-9",
            "message_id": "msg-3",
            "user_id": "user-7",
            "key_alias": "open-webui",
            "messages": [
                {"role": "system", "content": "be brief"},
                {"role": "user", "content": "build a calculator"},
                {"role": "assistant", "content": "done"},
                {"role": "user", "content": "Looks the same"},
            ],
            "tools": TOOLS,
            "backends": {QWEN: {"verdict": "ok", "waiting": 0.0, "running": 2.0},
                         MUSE: {"verdict": "busy", "waiting": 3.0, "running": 1.0}},
        }
        assert stash == {"decision_id": body["decision_id"], "litellm_call_id": "call-1", "observed": False}
        assert data["model"] == "auto"  # routing untouched
        assert h.stats["route_policy_decides"] == 1 and h.stats["route_policy_errors"] == 0

    run(go())


def test_decide_snapshot_is_taken_in_the_hook():
    """A key replaced in a message after the hook returned does not change what was sent."""
    async def go():
        post = Recorder()
        h = make_handler(post)
        data = proxy_data(messages=[{"role": "user", "content": "original"}])
        assert await h.async_pre_call_hook(KEY, None, data, "acompletion") is None  # stash only
        data["messages"][0]["content"] = "rewritten later"
        data["messages"].append({"role": "assistant", "content": "x"})
        await drain(h)
        assert post.bodies("/decide")[0]["messages"] == [{"role": "user", "content": "original"}]

    run(go())


def test_decide_minimal_request_sends_nulls():
    async def go():
        post = Recorder()
        h = make_handler(post)
        data = {"model": "auto", "messages": [{"role": "user", "content": "hi"}]}  # no metadata, ids or key
        await h.async_pre_call_hook(None, None, data, "acompletion")
        await drain(h)
        body = post.bodies("/decide")[0]
        assert {k: body[k] for k in ("litellm_call_id", "chat_id", "message_id", "user_id", "key_alias", "tools")} == {
            "litellm_call_id": None, "chat_id": None, "message_id": None, "user_id": None,
            "key_alias": None, "tools": None}
        assert body["backends"] == {}  # nothing learnt yet (a fresh proxy)
        assert data["metadata"][STASH_KEY]["decision_id"] == body["decision_id"]

    run(go())


def test_a_client_sent_stash_is_replaced():
    async def go():
        post = Recorder()
        h = make_handler(post)
        data = proxy_data()
        data["metadata"][STASH_KEY] = {"decision_id": "client-chosen", "observed": True}
        await h.async_pre_call_hook(KEY, None, data, "acompletion")
        await drain(h)
        decided = post.bodies("/decide")[0]["decision_id"]
        assert decided != "client-chosen" and data["metadata"][STASH_KEY]["decision_id"] == decided
        assert data["metadata"][STASH_KEY]["observed"] is False

    run(go())


@pytest.mark.parametrize("model", ["local-general", "local-private", "local-code", "qwen3.8-solo", "claude-sonnet-5"])
def test_nothing_for_other_models(model):
    async def go():
        post = Recorder()
        h = make_handler(post)
        data = proxy_data(model=model)
        before = copy.deepcopy(data)
        await h.async_pre_call_hook(KEY, None, data, "acompletion")
        if model == "local-general":
            before["model"] = data["model"]  # the code remap may act; it is not ours
        assert data == before and stash_of(data) is None
        await h.async_post_call_failure_hook(data, RuntimeError("x"), KEY)
        await drain(h)
        assert post.calls == [] and h.policy.in_flight == 0

    run(go())


@pytest.mark.parametrize("handler_kw", [{"enabled": False}, {"url": ""}, {"url": "   "}])
def test_disabled_does_nothing_at_all(handler_kw):
    async def go():
        post = Recorder()
        h = make_handler(post, **handler_kw)
        assert h.policy.enabled is False
        data = proxy_data()
        before = copy.deepcopy(data)
        assert await h.async_pre_call_hook(KEY, None, data, "acompletion") is None
        assert data == before  # not even a stash
        # A stash that arrived from elsewhere is ignored too.
        data["metadata"][STASH_KEY] = {"decision_id": "d", "observed": False}
        await h.async_post_call_failure_hook(data, RuntimeError("x"), KEY)
        assert h.policy.watch_stream(data, None) is None
        await drain(h)
        assert post.calls == [] and h.policy.in_flight == 0
        assert h.state()["config"]["route_policy_enabled"] is False

    run(go())


def test_non_chat_auto_request_is_not_decided():
    async def go():
        post = Recorder()
        h = make_handler(post)
        data = {"model": "auto", "input": "hi", "metadata": {}}  # e.g. the responses API
        await h.async_pre_call_hook(KEY, None, data, "aresponses")
        await drain(h)
        assert post.calls == [] and data == {"model": "auto", "input": "hi", "metadata": {}}

    run(go())


# ── /observe: non-streaming ────────────────────────────────────────────────
def response(content="hello", headers=None, tool_calls=None, finish="stop", rid="chatcmpl-abc"):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = [ChatCompletionMessageToolCall(id=f"t{i}", type="function",
                                                           function=Function(name=n, arguments="{}"))
                             for i, n in enumerate(tool_calls)]
    r = ModelResponse(id=rid, model="auto", choices=[Choices(index=0, message=Message(**msg), finish_reason=finish)])
    r._hidden_params = {"additional_headers": dict(headers if headers is not None else routed_headers())}
    return r


async def decided(h, **kw):
    data = proxy_data(**kw)
    await h.async_pre_call_hook(KEY, None, data, "acompletion")
    data["deployment"] = copy.deepcopy(AUTO_DEPLOYMENT)  # what the proxy sets after the call (1707)
    return data


@needs_litellm
def test_observe_non_streaming_exact_body():
    async def go():
        post = Recorder()
        h = make_handler(post)
        data = await decided(h)
        out = await h.async_post_call_success_hook(data, KEY, response())
        assert out.choices[0].message.content == "hello" + FOOTER_GENERAL  # the footer still works
        await drain(h)
        [decide], [observe] = post.bodies("/decide"), post.bodies("/observe")
        assert observe == {
            "decision_id": decide["decision_id"],
            "litellm_call_id": "call-1",
            "response_id": "chatcmpl-abc",
            "served_model_group": "local-general",
            "served_deployment": "openai/qwen3.8-solo",
            "overflow_reason": None,
            "finish_reason": "stop",
            "tool_calls": [],
            "status": "success",
            "error": None,
        }
        assert data["metadata"][STASH_KEY]["observed"] is True
        assert h.stats["route_policy_observes"] == 1

    run(go())


@needs_litellm
def test_observe_tool_calls_and_exactly_once():
    async def go():
        post = Recorder()
        h = make_handler(post)
        data = await decided(h)
        resp = response(content=None, tool_calls=["sandbox-mcp_exec", "run_command"], finish="tool_calls",
                        headers=routed_headers(model_id="id-code-muse", group="local-code", name="openai/muse-glimmer"))
        assert await h.async_post_call_success_hook(data, KEY, resp) is None  # no footer on a tool call
        await h.async_post_call_success_hook(data, KEY, resp)  # a second dispatch
        await h.async_post_call_failure_hook(data, RuntimeError("late"), KEY)  # and a failure after it
        await drain(h)
        [observe] = post.bodies("/observe")
        assert observe["tool_calls"] == ["sandbox-mcp_exec", "run_command"]
        assert observe["finish_reason"] == "tool_calls"
        assert (observe["served_model_group"], observe["served_deployment"]) == ("local-code", "openai/muse-glimmer")

    run(go())


@needs_litellm
def test_observe_claude_overflow_reason_and_router_bypass():
    async def go():
        clock, pages, post = Clock(), {}, Recorder()
        h = make_handler(post, clock=clock, pages=pages)
        await prime_backends(h, pages, clock, metrics(3), metrics(3))
        data = await decided(h)
        await h.async_post_call_success_hook(data, KEY, response(
            headers=routed_headers(model_id="id-gen-claude", name="anthropic/claude-sonnet-5")))
        # Router down: `auto` fell back to local-private (outer deployment, no inner headers).
        bypass = await decided(h)
        bypass["deployment"] = {"model_name": "local-private", "litellm_params": {"model": "openai/qwen3.8-solo"}}
        await h.async_post_call_success_hook(bypass, KEY, response(
            headers={"x-litellm-model-group": "local-private", "llm_provider-content-type": "application/json"}))
        await drain(h)
        claude, local = post.bodies("/observe")
        assert (claude["served_model_group"], claude["served_deployment"], claude["overflow_reason"]) == (
            "local-general", "anthropic/claude-sonnet-5", "busy")
        assert (local["served_model_group"], local["served_deployment"], local["overflow_reason"]) == (
            "local-private", "openai/qwen3.8-solo", None)

    run(go())


def test_observe_failure_exact_body_and_none_returned():
    async def go():
        post = Recorder()
        h = make_handler(post)
        data = await decided(h)
        out = await h.async_post_call_failure_hook(request_data=data, original_exception=RuntimeError("boom"),
                                                   user_api_key_dict=KEY, traceback_str="tb")
        assert out is None  # never transforms the client's error
        await drain(h)
        [decide], [observe] = post.bodies("/decide"), post.bodies("/observe")
        assert observe == {
            "decision_id": decide["decision_id"], "litellm_call_id": "call-1", "response_id": None,
            "served_model_group": None, "served_deployment": None, "overflow_reason": None,
            "finish_reason": None, "tool_calls": [], "status": "failure", "error": "RuntimeError: boom",
        }
        assert len(observe["error"]) <= 500

    run(go())


# ── /observe: streaming ────────────────────────────────────────────────────
def stream_chunks(texts=("hel", "lo"), tool=None, finish="stop", rid="chatcmpl-s1", headers=None):
    hidden = {"additional_headers": dict(headers if headers is not None else routed_headers())}
    out = [ModelResponseStream(id=rid, created=1, model="auto",
                               choices=[StreamingChoices(index=0, delta=Delta(content=t))]) for t in texts]
    if tool:
        out.append(ModelResponseStream(id=rid, created=1, model="auto", choices=[StreamingChoices(index=0, delta=Delta(
            tool_calls=[ChatCompletionDeltaToolCall(id="t", index=0, type="function",
                                                    function=Function(name=tool, arguments=""))]))]))
        out.append(ModelResponseStream(id=rid, created=1, model="auto", choices=[StreamingChoices(index=0, delta=Delta(
            tool_calls=[ChatCompletionDeltaToolCall(index=0, function=Function(arguments="{}"))]))]))
    if finish:
        out.append(ModelResponseStream(id=rid, created=1, model="auto",
                                       choices=[StreamingChoices(index=0, delta=Delta(), finish_reason=finish)]))
    out.append(ModelResponseStream(id=rid, created=1, model="auto", choices=[]))  # usage chunk
    for c in out:
        c._hidden_params = copy.deepcopy(hidden)
    return out


class Wrapper:
    """Stands in for CustomStreamWrapper: async-iterable with _hidden_params."""

    def __init__(self, chunks, headers=None, fail_after=None):
        self._chunks = chunks
        self._fail_after = fail_after
        self._hidden_params = {"additional_headers": dict(headers if headers is not None else routed_headers())}

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for i, c in enumerate(self._chunks):
            if self._fail_after is not None and i == self._fail_after:
                raise ValueError("upstream died")
            yield c


async def collect(h, data, wrapper):
    return [c async for c in h.async_post_call_streaming_iterator_hook(
        user_api_key_dict=KEY, response=wrapper, request_data=data)]


def joined(chunks):
    return "".join((c.delta.content or "") for ch in chunks for c in ch.choices)


@needs_litellm
def test_observe_streaming_exact_body_and_the_stream_is_wrapped_once():
    async def go():
        post = Recorder()
        h = make_handler(post)
        data = await decided(h, stream=True)
        chunks = stream_chunks()
        out = await collect(h, data, Wrapper(chunks))
        # Exactly what the footer alone produces: one extra footer chunk, nothing else.
        assert len(out) == len(chunks) + 1 and joined(out) == "hello" + FOOTER_GENERAL
        await drain(h)
        [decide], [observe] = post.bodies("/decide"), post.bodies("/observe")
        assert observe == {
            "decision_id": decide["decision_id"], "litellm_call_id": "call-1", "response_id": "chatcmpl-s1",
            "served_model_group": "local-general", "served_deployment": "openai/qwen3.8-solo",
            "overflow_reason": None, "finish_reason": "stop", "tool_calls": [], "status": "success", "error": None,
        }

    run(go())


@needs_litellm
def test_observe_streamed_tool_call_without_footer():
    """A streamed tool call: the footer stays out, the stream is still watched,
    and the chunks pass through unchanged (same objects)."""
    async def go():
        post = Recorder()
        h = make_handler(post)
        data = await decided(h, stream=True)
        chunks = stream_chunks(texts=(), tool="sandbox-mcp_exec", finish="tool_calls")
        out = await collect(h, data, Wrapper(chunks))
        assert out == chunks and all(a is b for a, b in zip(out, chunks))
        await drain(h)
        [observe] = post.bodies("/observe")
        assert observe["tool_calls"] == ["sandbox-mcp_exec"] and observe["finish_reason"] == "tool_calls"
        assert observe["status"] == "success"

    run(go())


@needs_litellm
def test_streaming_with_footer_off_still_observes():
    async def go():
        post = Recorder()
        h = make_handler(post, auto_footer=False)
        data = await decided(h, stream=True)
        chunks = stream_chunks()
        out = await collect(h, data, Wrapper(chunks))
        assert all(a is b for a, b in zip(out, chunks)) and len(out) == len(chunks)
        await drain(h)
        assert post.bodies("/observe")[0]["status"] == "success"

    run(go())


@needs_litellm
def test_stream_upstream_error_observes_failure_once():
    async def go():
        post = Recorder()
        h = make_handler(post)
        data = await decided(h, stream=True)
        with pytest.raises(ValueError, match="upstream died"):
            await collect(h, data, Wrapper(stream_chunks(), fail_after=1))
        # The proxy's async_data_generator then calls post_call_failure_hook (proxy_server.py:7540).
        await h.async_post_call_failure_hook(data, ValueError("upstream died"), KEY)
        await drain(h)
        [observe] = post.bodies("/observe")
        assert observe["status"] == "failure" and observe["error"] == "ValueError: upstream died"
        assert observe["response_id"] == "chatcmpl-s1"  # the first chunk did arrive
        assert observe["served_model_group"] == "local-general"

    run(go())


@needs_litellm
def test_client_disconnect_observes_failure():
    async def go():
        post = Recorder()
        h = make_handler(post)
        data = await decided(h, stream=True)
        gen = h.async_post_call_streaming_iterator_hook(user_api_key_dict=KEY, response=Wrapper(stream_chunks()),
                                                        request_data=data)
        await gen.__anext__()
        await gen.aclose()  # what the proxy's GeneratorExit does to it
        await drain(h)
        [observe] = post.bodies("/observe")
        assert observe["status"] == "failure" and observe["error"] == DISCONNECTED
        assert observe["response_id"] == "chatcmpl-s1"

    run(go())


@needs_litellm
def test_stream_without_a_decision_is_not_watched():
    async def go():
        post = Recorder()
        h = make_handler(post)
        data = proxy_data(model="local-general", stream=True)  # no stash
        chunks = stream_chunks()
        out = await collect(h, data, Wrapper(chunks))
        assert out == chunks and post.calls == []

    run(go())


# ── latency, errors, the in-flight cap ─────────────────────────────────────
@pytest.mark.parametrize("post", [Recorder(hang=True), Recorder(status=500), Recorder(exc=ConnectionError("refused"))],
                         ids=["hangs", "500", "refused"])
def test_hook_latency_is_unaffected_by_a_bad_service(post):
    async def go():
        h = make_handler(post, timeout=0.2)
        data = proxy_data(messages=[{"role": "user", "content": f"m{i}"} for i in range(200)])
        t0 = time.perf_counter()
        await h.async_pre_call_hook(KEY, None, data, "acompletion")
        elapsed = time.perf_counter() - t0
        task = next(iter(h.policy._tasks))
        assert not task.done()  # returned before the call even started
        assert elapsed < 0.05, elapsed
        t0 = time.perf_counter()
        await h.async_post_call_failure_hook(data, RuntimeError("x"), KEY)
        assert time.perf_counter() - t0 < 0.05
        await drain(h)
        assert h.policy.in_flight == 0
        assert h.stats["route_policy_errors"] == 2 and h.stats["route_policy_decides"] == 0

    run(go())


def test_hook_overhead_is_small_against_a_disabled_client():
    """Median pre-call time with the client on vs off, on a 400-message chat:
    the client adds a uuid, a shallow copy of the message list and a task."""
    async def timed(h):
        samples = []
        for _ in range(60):
            data = proxy_data(messages=[{"role": "user" if i % 2 else "assistant", "content": "x" * 400}
                                        for i in range(400)])
            t0 = time.perf_counter()
            await h.async_pre_call_hook(KEY, None, data, "acompletion")
            samples.append(time.perf_counter() - t0)
            await drain(h)
        return statistics.median(samples)

    async def go():
        off = await timed(make_handler(Recorder(), enabled=False))
        on = await timed(make_handler(Recorder(hang=False)))
        return off, on

    off, on = run(go())
    assert on - off < 0.005, (off, on)


def test_in_flight_cap_drops_and_counts():
    async def go():
        post = Recorder(hang=True)
        h = make_handler(post, timeout=5.0, policy_max_in_flight=3)
        datas = [proxy_data() for _ in range(5)]
        for d in datas:
            await h.async_pre_call_hook(KEY, None, d, "acompletion")
        assert h.policy.in_flight == 3 and h.stats["route_policy_dropped"] == 2
        assert [stash_of(d) is not None for d in datas] == [True, True, True, False, False]
        # A dropped decision is never observed.
        for d in datas[3:]:
            await h.async_post_call_failure_hook(d, RuntimeError("x"), KEY)
        assert h.policy.in_flight == 3 and h.stats["route_policy_dropped"] == 2
        assert h.state()["route_policy_in_flight"] == 3
        for t in list(h.policy._tasks):
            t.cancel()
        await asyncio.gather(*list(h.policy._tasks), return_exceptions=True)
        assert h.policy.in_flight == 0

    run(go())


def test_log_lines_live_once_and_failures_throttled():
    import logging

    from hooks.log import logger

    lines = []

    class ListHandler(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    lh = ListHandler()
    logger.addHandler(lh)
    try:
        async def go():
            ok = make_handler(Recorder())
            for _ in range(3):
                await ok.async_pre_call_hook(KEY, None, proxy_data(), "acompletion")
                await drain(ok)
            bad = make_handler(Recorder(exc=ConnectionError("refused")))
            for _ in range(3):
                await bad.async_pre_call_hook(KEY, None, proxy_data(), "acompletion")
                await drain(bad)
            assert bad.stats["route_policy_errors"] == 3

        run(go())
    finally:
        logger.removeHandler(lh)
    live = [ln for ln in lines if "shadow mode is live" in ln]
    assert live == ["[route-policy] first POST http://route-policy:8000/decide answered HTTP 200 -- shadow mode is live"]
    failed = [ln for ln in lines if "failed" in ln]
    assert failed == ["[route-policy] POST http://route-policy:8000/decide failed: ConnectionError: refused"]


def test_decide_outside_an_event_loop_is_dropped_not_raised():
    h = make_handler(Recorder())
    data = proxy_data()
    assert h.policy.decide(data, KEY) is False
    assert stash_of(data) is None and h.stats["route_policy_dropped"] == 1


def test_never_raises():
    class BadKey:
        @property
        def key_alias(self):
            raise RuntimeError("bad key object")

    async def go():
        post = Recorder()
        h = make_handler(post)
        for data in (None, "string", {"model": "auto"}, {"model": "auto", "messages": "nope"},
                     {"model": "auto", "messages": [], "metadata": "not a dict"}):
            assert await h.async_pre_call_hook(KEY, None, data, "acompletion") is None
        # A bad key object: counted, request untouched apart from nothing.
        data = proxy_data()
        await h.async_pre_call_hook(BadKey(), None, data, "acompletion")
        assert stash_of(data) is None and h.stats["route_policy_errors"] == 1
        # Unserialisable message content: encoded with str(), still sent.
        weird = proxy_data(messages=[{"role": "user", "content": object()}])
        await h.async_pre_call_hook(KEY, None, weird, "acompletion")
        # The footer's derivation blowing up: the observe goes out with nulls.
        good = proxy_data()
        await h.async_pre_call_hook(KEY, None, good, "acompletion")

        def boom(*_a, **_k):
            raise RuntimeError("served bug")

        h.policy._served = boom
        assert await h.async_post_call_success_hook(good, KEY, None) is None
        assert await h.async_post_call_success_hook(good, KEY, object()) is None
        assert await h.async_post_call_failure_hook(None, RuntimeError("x"), KEY) is None
        await drain(h)
        assert len(post.bodies("/decide")) == 2
        [observe] = post.bodies("/observe")
        assert observe["status"] == "success" and observe["served_model_group"] is None
        # A transport that returns garbage: an error, not an exception.
        h2 = make_handler(Recorder(status="weird"))
        await h2.async_pre_call_hook(KEY, None, proxy_data(), "acompletion")
        await drain(h2)
        assert h2.stats["route_policy_errors"] == 1

    run(go())


# ── the real HTTP client ───────────────────────────────────────────────────
@needs_httpx
def test_httpx_post_wire_format_timeout_and_5xx():
    seen = []

    async def go():
        async def handler(request):
            seen.append((request.method, str(request.url), request.headers.get("content-type"),
                         json.loads(request.content)))
            if request.url.path == "/observe":
                return httpx.Response(500)
            if request.url.path.startswith("/slow/"):
                await asyncio.sleep(5)
            return httpx.Response(200, json={"route": "local-general"})

        transport = httpx.MockTransport(handler)
        h = make_handler(HttpxPost(0.2, transport=transport), timeout=0.2)
        data = await decided(h)
        await h.async_post_call_failure_hook(data, RuntimeError("x"), KEY)
        await drain(h)
        assert h.stats["route_policy_decides"] == 1 and h.stats["route_policy_errors"] == 1  # the 500
        h.policy.base_url = SERVICE + "/slow"  # a hung service: timed out, counted
        t0 = time.perf_counter()
        await h.async_pre_call_hook(KEY, None, proxy_data(), "acompletion")
        assert time.perf_counter() - t0 < 0.05
        await drain(h)
        assert h.stats["route_policy_errors"] == 2

    run(go())
    by_url = {url: (method, ctype, body) for method, url, ctype, body in seen}
    assert set(by_url) == {SERVICE + "/decide", SERVICE + "/observe", SERVICE + "/slow/decide"}
    m1, ct1, body1 = by_url[SERVICE + "/decide"]
    m2, ct2, body2 = by_url[SERVICE + "/observe"]
    assert (m1, ct1, m2, ct2) == ("POST", "application/json", "POST", "application/json")
    assert body2["decision_id"] == body1["decision_id"] and body2["status"] == "failure"


class _Service(BaseHTTPRequestHandler):
    received = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)))
        type(self).received.append((self.path, body))
        status = 200 if self.path == "/decide" else 202
        payload = json.dumps({"decision_id": body.get("decision_id")}).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@needs_httpx
@needs_litellm
def test_against_a_real_http_server(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    _Service.received = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Service)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        async def go():
            h = make_handler(HttpxPost(2.0), url=url, timeout=2.0)
            data = await decided(h, stream=True)
            await collect(h, data, Wrapper(stream_chunks()))
            await drain(h)
            return h

        h = run(go())
    finally:
        server.shutdown()
        server.server_close()
    paths = sorted(p for p, _ in _Service.received)
    assert paths == ["/decide", "/observe"]
    bodies = dict(_Service.received)
    assert bodies["/decide"]["decision_id"] == bodies["/observe"]["decision_id"]
    assert h.stats["route_policy_decides"] == 1 and h.stats["route_policy_observes"] == 1  # 200 and 202


# ── settings and repo invariants ───────────────────────────────────────────
def test_settings_defaults_and_overrides(monkeypatch, fresh_hooks):
    for name in ("ROUTE_POLICY_ENABLED", "ROUTE_POLICY_URL", "ROUTE_POLICY_TIMEOUT_S"):
        monkeypatch.delenv(name, raising=False)
    with fresh_hooks():
        from hooks import settings
        assert (settings.ROUTE_POLICY_ENABLED, settings.ROUTE_POLICY_URL, settings.ROUTE_POLICY_TIMEOUT_S) == (
            True, "http://route-policy:8000", 2.0)
    monkeypatch.setenv("ROUTE_POLICY_ENABLED", "false")
    monkeypatch.setenv("ROUTE_POLICY_URL", " http://elsewhere:9000/ ")
    monkeypatch.setenv("ROUTE_POLICY_TIMEOUT_S", "0.01")
    with fresh_hooks():
        from hooks import settings
        from hooks.handler import Handler as FreshHandler
        assert (settings.ROUTE_POLICY_ENABLED, settings.ROUTE_POLICY_URL, settings.ROUTE_POLICY_TIMEOUT_S) == (
            False, "http://elsewhere:9000/", 0.1)
        h = FreshHandler(start_poller=False)
        assert h.policy.enabled is False and h.policy.base_url == "http://elsewhere:9000"
    monkeypatch.setenv("ROUTE_POLICY_ENABLED", "true")
    monkeypatch.setenv("ROUTE_POLICY_URL", "")
    with fresh_hooks():
        from hooks.handler import Handler as FreshHandler
        assert FreshHandler(start_poller=False).policy.enabled is False  # no URL, no client


def _compose_env():
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]["litellm"]["environment"]


def _env_example(name):
    lines = [ln for ln in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines() if ln.startswith(name + "=")]
    assert len(lines) == 1, f"{name} must appear exactly once in .env.example"
    return lines[0].split("=", 1)[1].strip()


@needs_yaml
@pytest.mark.parametrize("name,default", [
    ("ROUTE_POLICY_ENABLED", "true"),
    ("ROUTE_POLICY_URL", "http://route-policy:8000"),
    ("ROUTE_POLICY_TIMEOUT_S", "2"),
])
def test_defaults_agree_across_settings_compose_and_env_example(name, default):
    value = _compose_env()[name]
    m = re.fullmatch(r"\$\{" + name + r":-(.*)\}", str(value))
    assert m, f"compose must pass ${{{name}:-<default>}}: {value!r}"
    assert m.group(1) == default
    assert _env_example(name) == default


# ── proxy plumbing (litellm[proxy]==1.95.0) ────────────────────────────────
try:
    from litellm.caching.dual_cache import DualCache
    from litellm.proxy._types import UserAPIKeyAuth
    from litellm.proxy.utils import ProxyLogging
except Exception:  # proxy extras (backoff, fastapi, ...) not installed
    ProxyLogging = None

needs_proxy = pytest.mark.skipif(ProxyLogging is None, reason="needs litellm[proxy]==1.95.0")


@needs_proxy
def test_proxy_dispatches_every_path_and_the_stash_survives():
    async def go():
        post = Recorder()
        h = make_handler(post)
        old = litellm.callbacks
        litellm.callbacks = [h]
        try:
            pl = ProxyLogging(user_api_key_cache=DualCache())
            key = UserAPIKeyAuth(key_alias="open-webui")

            # Non-streaming.
            data = await pl.pre_call_hook(user_api_key_dict=key, data=proxy_data(), call_type="acompletion")
            stash = data["metadata"][STASH_KEY]
            data["deployment"] = copy.deepcopy(AUTO_DEPLOYMENT)
            out = await pl.post_call_success_hook(data=data, response=response(), user_api_key_dict=key)
            assert out.choices[0].message.content == "hello" + FOOTER_GENERAL

            # Streaming, through the proxy's own iterator chain.
            sdata = await pl.pre_call_hook(user_api_key_dict=key, data=proxy_data(stream=True), call_type="acompletion")
            sdata["deployment"] = copy.deepcopy(AUTO_DEPLOYMENT)
            chunks = [c async for c in pl.async_post_call_streaming_iterator_hook(
                response=Wrapper(stream_chunks()), user_api_key_dict=key, request_data=sdata)]
            assert joined(chunks) == "hello" + FOOTER_GENERAL

            # Failure: post_call_failure_hook returns no transformed exception.
            fdata = await pl.pre_call_hook(user_api_key_dict=key, data=proxy_data(), call_type="acompletion")
            transformed = await pl.post_call_failure_hook(request_data=fdata, original_exception=RuntimeError("boom"),
                                                          user_api_key_dict=key)
            assert transformed is None
            await drain(h)
        finally:
            litellm.callbacks = old

        decides, observes = post.bodies("/decide"), post.bodies("/observe")
        assert len(decides) == 3 and len(observes) == 3
        assert [d["decision_id"] for d in decides] == [o["decision_id"] for o in observes]
        assert decides[0]["decision_id"] == stash["decision_id"]
        assert all(d["key_alias"] == "open-webui" for d in decides)
        assert [o["status"] for o in observes] == ["success", "success", "failure"]
        assert [o["response_id"] for o in observes] == ["chatcmpl-abc", "chatcmpl-s1", None]

    run(go())


@needs_proxy
def test_proxy_still_activates_the_class_body_hooks():
    h = make_handler(Recorder())
    old = litellm.callbacks
    litellm.callbacks = [h]
    try:
        caps = ProxyLogging._callback_capabilities()
        assert caps.has_pre_call_override and caps.has_iterator_override
        assert (h, "override") in caps.iterator_overrides
    finally:
        litellm.callbacks = old


# A stand-in for Envoy + the inner LiteLLM that records the request bodies the
# outer openai provider sends it.
class _Upstream(BaseHTTPRequestHandler):
    bodies = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
        type(self).bodies.append(body)
        payload = json.dumps({"id": "chatcmpl-u", "object": "chat.completion", "created": 1, "model": "local-general",
                              "choices": [{"index": 0, "message": {"role": "assistant", "content": "hello"},
                                           "finish_reason": "stop"}],
                              "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@needs_litellm
def test_the_stash_is_never_sent_upstream(monkeypatch):
    """data["metadata"] goes into Router.acompletion(**data); the openai
    provider must not put the stash (or metadata at all) in the upstream body
    -- also not with enable_preview_features, which forwards string values only."""
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    _Upstream.bodies = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}/v1"
    try:
        router = litellm.Router(model_list=[{"model_name": "auto", "litellm_params": {
            "model": "openai/vllm-sr/auto", "api_base": base, "api_key": "sk-test"}}])
        stash = {"decision_id": str(uuid.uuid4()), "litellm_call_id": "call-1", "observed": False}
        for preview in (False, True):
            monkeypatch.setattr(litellm, "enable_preview_features", preview)
            meta = {"headers": dict(OWUI_HEADERS), STASH_KEY: dict(stash)}
            run(router.acompletion(model="auto", messages=[{"role": "user", "content": "hi"}], metadata=meta))
    finally:
        server.shutdown()
        server.server_close()
    assert len(_Upstream.bodies) == 2
    for body in _Upstream.bodies:
        assert STASH_KEY not in json.dumps(body)
        assert stash["decision_id"] not in json.dumps(body)
