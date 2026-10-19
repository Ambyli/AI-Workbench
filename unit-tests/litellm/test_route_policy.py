"""Unit tests for the route-policy client, single pass --
ai/litellm/hooks/routing/policy_client.py, driven through hooks.handler.Handler.

On every `auto` request the pre-call hook AWAITS ``POST /decide`` (bounded by
ROUTE_POLICY_DECIDE_TIMEOUT_S), rewrites ``data["model"]`` to the chain the
engine chose when the answer says ``apply: true`` and the hook's own
invariants allow it -- and to ``local-private`` in every other case -- and
stashes what happened in ``data["metadata"]``. The success,
streaming-iterator and failure hooks read the stash and fire ``POST
/observe`` in the background. Requests for any other model are never touched.

* Pure helpers (always run): header ids, tool-call names, response outcomes
  (OpenAI and Anthropic), reply parsing, the backend report.
* The hooks with a fake transport (always run): the exact /decide body and
  the rewrite, every fallback reason (timeout, transport error, non-2xx, bad
  JSON, another decision's id, apply false, a route that is not a chain, the
  client off, no messages), the exact /observe bodies for non-streaming,
  streaming and failed calls, exactly-once, hook latency against a hung
  service, the /observe in-flight cap, never-raises. Most of these need
  litellm's response types.
* The real HTTP client: httpx.MockTransport (timeouts, 5xx, the wire format)
  and a stdlib HTTP server.
* Against real LiteLLM: a Router with this repo's `auto` alias serves the
  chain the hook chose, and the openai provider never forwards the stash
  upstream. The proxy plumbing (skipped unless litellm[proxy]==1.95.0):
  ProxyLogging really dispatches every path to Handler -- /v1/messages
  (anthropic_messages) included -- and the stash survives them.
* Repo invariants: the defaults agree across settings.py, the compose file
  and .env.example.

    python -m pytest unit-tests/litellm -q -p no:cacheprovider
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
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
from hooks.log import logger
from hooks.overflow.state import STALE
from hooks.overflow.vllm import metrics_url
from hooks.routing.policy_client import (
    DISCONNECTED,
    STASH_KEY,
    HttpxPost,
    parse_decision,
    request_ids,
    response_outcome,
    split_reply,
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
CHAINS = ("local-general", "local-code", "local-reasoning", "local-private")

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
    """A fake Post standing in for the route-policy service: records (url,
    decoded JSON body); /decide answers a decision for `route` / `apply`
    (or `raw` bytes, or `status`), /observe answers `observe_status`. Paths
    in `hang` never answer; `exc` is raised for the paths in `exc_paths`."""

    def __init__(self, route="local-code", apply=True, mode="enforce", status=200, raw=None, hang=(), exc=None,
                 exc_paths=("decide", "observe"), observe_status=202):
        self.route, self.apply, self.mode, self.status, self.raw = route, apply, mode, status, raw
        self.hang, self.exc, self.exc_paths, self.observe_status = set(hang), exc, set(exc_paths), observe_status
        self.calls = []

    async def __call__(self, url, body):
        doc = json.loads(body.decode("utf-8"))
        self.calls.append((url, doc))
        path = url.rsplit("/", 1)[-1]
        if path in self.hang:
            await asyncio.sleep(3600)
        if self.exc is not None and path in self.exc_paths:
            raise self.exc
        if path == "observe":
            return self.observe_status, b""
        if self.raw is not None:
            return self.status, self.raw
        answer = {"decision_id": doc["decision_id"], "route": self.route, "mode": self.mode, "apply": self.apply,
                  "ruleset": {"id": "default", "version": 2}, "votes": [], "scores": {self.route: 1.0},
                  "latency_ms": 0.3}
        return self.status, json.dumps(answer).encode()

    def bodies(self, path):
        return [b for u, b in self.calls if u.endswith(path)]


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def make_handler(post=None, enabled=True, clock=None, pages=None, **kw) -> Handler:
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
                   route_policy=enabled, route_policy_url=kw.pop("url", SERVICE),
                   route_policy_timeout=kw.pop("timeout", 1.0),
                   route_policy_decide_timeout=kw.pop("decide_timeout", 0.5),
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


def deployment(chain="local-code", model="openai/muse-glimmer"):
    """data["deployment"] as the proxy stores it after the call (1707)."""
    return {"model_name": chain, "litellm_params": {"model": model}, "model_info": {"id": f"id-{chain}"}}


def general_chain():
    return [
        {"model_name": "local-general", "litellm_params": {"model": "openai/qwen3.8-solo", "order": 1, "api_base": QWEN}},
        {"model_name": "local-general", "litellm_params": {"model": "openai/muse-glimmer", "order": 2, "api_base": MUSE}},
        {"model_name": "local-general", "litellm_params": {"model": "anthropic/claude-sonnet-5", "order": 3}},
    ]


def metrics(waiting, running=1):
    return f'vllm:num_requests_waiting{{engine="0"}} {waiting}\nvllm:num_requests_running{{engine="0"}} {running}\n'


async def prime_backends(h, pages, clock, qwen, muse):
    """Teach the poller the chain's backends (what serving a request does) and poll twice."""
    h.overflow.filter_deployments("local-general", general_chain())
    pages[metrics_url(QWEN)] = qwen
    pages[metrics_url(MUSE)] = muse
    await h.overflow.poller.poll_once()
    clock.advance(2.5)
    await h.overflow.poller.poll_once()


async def drain(h):
    """Let every background /observe the client started finish."""
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


def test_response_outcome_openai_and_anthropic():
    openai = {"choices": [{"index": 1, "finish_reason": "length", "message": {"content": "x"}},
                          {"index": 0, "finish_reason": "tool_calls",
                           "message": {"tool_calls": [{"function": {"name": "run_command"}}]}}]}
    assert response_outcome(openai) == ("tool_calls", ["run_command"])
    anthropic = {"id": "msg_1", "type": "message", "stop_reason": "tool_use", "content": [
        {"type": "text", "text": "let me"}, {"type": "tool_use", "id": "t", "name": "sandbox-mcp_exec", "input": {}}]}
    assert response_outcome(anthropic) == ("tool_use", ["sandbox-mcp_exec"])
    assert response_outcome(None) == (None, []) and response_outcome(object()) == (None, [])


def test_split_reply_and_parse_decision():
    assert split_reply((200, b"{}")) == (200, b"{}")
    assert split_reply(204) == (204, b"")
    assert split_reply((500, None)) == (500, b"")
    assert parse_decision(b'{"route": "local-code"}') == {"route": "local-code"}
    for raw in (b"not json", b"[1, 2]", b"", b"null", None, b'"local-code"'):
        assert parse_decision(raw) is None, raw


B64 = "iVBORw0KGgo" + "A" * 3_000_000  # ~3 MB of base64


def image_conversation():
    """Every shape of inline payload a /decide may meet, next to text and tool calls."""
    return [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": [
            {"type": "text", "text": "what is in this picture? data:image/png;base64," + "x" * 300},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + B64, "detail": "high"}},
            {"type": "image_url", "image_url": "data:image/jpeg;base64," + B64},
            {"type": "image_url", "image_url": {"url": "https://example.com/cat.png"}},
            {"type": "input_image", "image_url": "data:image/webp;base64," + B64},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": B64}},
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": B64}},
            {"type": "input_audio", "input_audio": {"data": B64, "format": "wav"}},
            {"type": "file", "file": {"filename": "a.pdf", "file_data": "data:application/pdf;base64," + B64}},
        ]},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "t1", "type": "function", "function": {"name": "sandbox-mcp_exec", "arguments": '{"cmd": "ls"}'}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "a.txt"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "tu", "name": "run_command",
                                           "input": {"blob": "data:image/png;base64," + "y" * 500}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu", "content": [
                {"type": "text", "text": "the screenshot:"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": B64}}]},
            {"type": "text", "text": "Looks the same"}]},
    ]


def test_decide_body_drops_inline_payloads_and_keeps_the_rest():
    async def go():
        post = Recorder()
        h = make_handler(post)
        data = proxy_data(messages=image_conversation(), tools=TOOLS)
        original = copy.deepcopy(data["messages"])
        assert len(json.dumps(original)) > 20_000_000
        await h.async_pre_call_hook(KEY, None, data, "acompletion")
        return data, original, post

    data, original, post = run(go())
    assert data["model"] == "local-code"
    assert data["messages"] == original  # the request itself is untouched
    raw_size = len(json.dumps(post.calls[0][1]))
    assert raw_size < 5_000, raw_size  # was > 20 MB
    sent = post.bodies("/decide")[0]["messages"]
    parts = sent[1]["content"]
    # Every part keeps its type (and its other keys); only the payload is gone.
    assert [p["type"] for p in parts] == [p["type"] for p in original[1]["content"]]
    assert parts[0] == original[1]["content"][0]  # text is verbatim, data: URI and all
    assert parts[1]["image_url"] == {"url": "data:image/png;base64,[omitted: 3000033 chars]", "detail": "high"}
    assert parts[2]["image_url"] == "data:image/jpeg;base64,[omitted: 3000034 chars]"
    assert parts[3] == original[1]["content"][3]  # a plain URL is no payload
    assert parts[4]["image_url"].startswith("data:image/webp;base64,[omitted: ")
    assert parts[5]["source"] == {"type": "base64", "media_type": "image/png", "data": "[omitted: 3000011 chars]"}
    assert parts[6]["source"]["media_type"] == "application/pdf" and parts[6]["source"]["data"].startswith("[omitted")
    assert parts[7]["input_audio"] == {"data": "[omitted: 3000011 chars]", "format": "wav"}
    assert parts[8]["file"]["filename"] == "a.pdf"
    assert parts[8]["file"]["file_data"].startswith("data:application/pdf;base64,[omitted: ")
    # Tool calls, tool results' text, the latest user text: verbatim.
    assert sent[0] == original[0] and sent[2] == original[2] and sent[3] == original[3]
    assert sent[4] == original[4]  # a tool_use block is a tool call: never touched
    nested = sent[5]["content"][0]["content"]
    assert nested[0] == {"type": "text", "text": "the screenshot:"}
    assert nested[1]["type"] == "image" and nested[1]["source"]["data"].startswith("[omitted")
    assert sent[5]["content"][1] == {"type": "text", "text": "Looks the same"}


def test_a_conversation_without_payloads_is_sent_as_is():
    messages = [{"role": "user", "content": [{"type": "text", "text": "hi"}]},
                {"role": "user", "content": "data:image/png;base64," + "z" * 500}]  # a string content is text
    from hooks.routing.policy_client import slim_messages

    out = slim_messages(messages)
    assert out == messages and all(a is b for a, b in zip(out, messages))
    small = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:,x"}}]}]
    assert slim_messages(small)[0] is small[0]  # too short to be a payload


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


# ── /decide and the rewrite ────────────────────────────────────────────────
FOOTER_GENERAL = footer_text("qwen3.8-solo · local-general")


def test_decide_is_awaited_and_its_route_applied():
    async def go():
        clock, pages, post = Clock(), {}, Recorder(route="local-code")
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
        assert out is data
        # Awaited: the answer is in before the hook returned, and applied.
        [body] = post.bodies("/decide")
        assert data["model"] == "local-code"
        stash = data["metadata"][STASH_KEY]
        assert is_uuid4(body["decision_id"])
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
        assert stash == {"requested_model": "auto", "route": "local-code", "source": "engine",
                         "decision_id": body["decision_id"], "litellm_call_id": "call-1", "observed": False}
        assert h.policy.in_flight == 0  # nothing left running in the background
        stats = h.stats
        assert (stats["route_policy_decides"], stats["route_policy_applied"], stats["route_policy_fallbacks"],
                stats["route_policy_errors"]) == (1, 1, 0, 0)

    run(go())


@pytest.mark.parametrize("route", CHAINS)
def test_every_chain_route_is_applied(route):
    async def go():
        h = make_handler(Recorder(route=route))
        data = proxy_data()
        await h.async_pre_call_hook(KEY, None, data, "acompletion")
        assert data["model"] == route and stash_of(data)["source"] == "engine"

    run(go())


def test_decide_minimal_request_sends_nulls():
    async def go():
        post = Recorder()
        h = make_handler(post)
        data = {"model": "auto", "messages": [{"role": "user", "content": "hi"}]}  # no metadata, ids or key
        await h.async_pre_call_hook(None, None, data, "acompletion")
        body = post.bodies("/decide")[0]
        assert {k: body[k] for k in ("litellm_call_id", "chat_id", "message_id", "user_id", "key_alias", "tools")} == {
            "litellm_call_id": None, "chat_id": None, "message_id": None, "user_id": None,
            "key_alias": None, "tools": None}
        assert body["backends"] == {}  # nothing learnt yet (a fresh proxy)
        assert data["metadata"][STASH_KEY]["decision_id"] == body["decision_id"]
        assert data["model"] == "local-code"

    run(go())


def test_a_client_sent_stash_is_replaced():
    async def go():
        post = Recorder()
        h = make_handler(post)
        data = proxy_data()
        data["metadata"][STASH_KEY] = {"requested_model": "auto", "route": "claude-sonnet-5", "source": "engine",
                                       "decision_id": "client-chosen", "observed": True}
        await h.async_pre_call_hook(KEY, None, data, "acompletion")
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
        data["metadata"][STASH_KEY] = {"requested_model": "auto", "route": model, "source": "engine",
                                       "decision_id": "forged", "observed": False}
        before = copy.deepcopy(data)
        assert await h.async_pre_call_hook(KEY, None, data, "acompletion") is None
        del before["metadata"][STASH_KEY]  # a stash the body carried is dropped -- the only edit
        assert data == before and stash_of(data) is None
        await h.async_post_call_success_hook(data, KEY, None)
        await h.async_post_call_failure_hook(data, RuntimeError("x"), KEY)
        await drain(h)
        assert post.calls == [] and h.policy.in_flight == 0

    run(go())


@pytest.mark.parametrize("handler_kw", [{"enabled": False}, {"url": ""}, {"url": "   "}])
def test_client_off_routes_auto_to_local_private_without_calling(handler_kw):
    async def go():
        post = Recorder()
        h = make_handler(post, **handler_kw)
        assert h.policy.enabled is False
        data = proxy_data()
        assert await h.async_pre_call_hook(KEY, None, data, "acompletion") is data
        assert data["model"] == "local-private"
        assert data["metadata"][STASH_KEY] == {"requested_model": "auto", "route": "local-private",
                                               "source": "fallback:disabled", "decision_id": None,
                                               "litellm_call_id": "call-1", "observed": False}
        # No decision was asked for, so none is observed.
        await h.async_post_call_failure_hook(data, RuntimeError("x"), KEY)
        assert h.policy.watch_stream(data, None) is None
        await drain(h)
        assert post.calls == [] and h.policy.in_flight == 0
        state = h.state()
        assert state["config"]["route_policy_enabled"] is False
        assert state["route_policy_fallbacks_by_reason"] == {"disabled": 1}
        assert state["stats"]["route_policy_fallbacks"] == 1 and state["stats"]["route_policy_errors"] == 0

    run(go())


def test_the_stash_goes_in_the_proxys_own_metadata_dict():
    """On /v1/messages (and /v1/responses, batches, files) the proxy keeps its
    metadata -- the request headers -- in data["litellm_metadata"], because
    `metadata` is a parameter of the request itself there. The ids are read
    from, and the stash written to, that dict; the client's `metadata` is
    left exactly as sent."""
    async def go():
        post = Recorder(route="local-code")
        h = make_handler(post)
        data = {"model": "auto", "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}],
                "metadata": {"user_id": "claude-code-user"},
                "litellm_metadata": {"headers": dict(OWUI_HEADERS), "user_api_key_alias": "cc"},
                "litellm_call_id": "call-m"}
        await h.async_pre_call_hook(KEY, None, data, "anthropic_messages")
        assert data["model"] == "local-code"
        assert data["metadata"] == {"user_id": "claude-code-user"}
        assert data["litellm_metadata"][STASH_KEY]["route"] == "local-code"
        assert stash_of(data) is data["litellm_metadata"][STASH_KEY]
        assert post.bodies("/decide")[0]["chat_id"] == "chat-9"
        # A stash smuggled into either dict of a direct call is dropped.
        direct = {"model": "local-general", "messages": [], "metadata": {STASH_KEY: {"requested_model": "auto",
                                                                                     "route": "x"}},
                  "litellm_metadata": {"headers": {}, STASH_KEY: {"requested_model": "auto", "route": "x"}}}
        await h.async_pre_call_hook(KEY, None, direct, "anthropic_messages")
        assert STASH_KEY not in direct["metadata"] and STASH_KEY not in direct["litellm_metadata"]

    run(go())


def test_non_chat_auto_request_goes_to_local_private_without_calling():
    async def go():
        post = Recorder()
        h = make_handler(post)
        data = {"model": "auto", "input": "hi", "metadata": {}}  # e.g. the responses API
        assert await h.async_pre_call_hook(KEY, None, data, "aresponses") is data
        assert data["model"] == "local-private"
        assert data["metadata"][STASH_KEY]["source"] == "fallback:no-messages"
        assert post.calls == []

    run(go())


ANOTHER_ID = str(uuid.uuid4())


@pytest.mark.parametrize("post_kw,why,is_error", [
    ({"hang": {"decide"}}, "timeout", True),
    ({"exc": ConnectionError("refused")}, "error", True),
    ({"status": 500}, "http-500", True),
    ({"status": 404}, "http-404", True),
    ({"status": 422}, "http-422", True),
    ({"raw": b"not json"}, "bad-response", True),
    ({"raw": b"[1, 2]"}, "bad-response", True),
    ({"raw": json.dumps({"decision_id": ANOTHER_ID, "route": "local-code", "apply": True}).encode()},
     "bad-response", True),
    ({"apply": False, "mode": "shadow"}, "not-applied", False),
    ({"apply": "true"}, "not-applied", False),
    ({"apply": None}, "not-applied", False),
    ({"route": "claude-sonnet-5"}, "not-a-chain", True),
    ({"route": "auto"}, "not-a-chain", True),
    ({"route": None}, "not-a-chain", True),
    ({"route": 7}, "not-a-chain", True),
], ids=lambda v: v if isinstance(v, str) else None)
def test_every_fallback_goes_to_local_private_and_is_still_observed(post_kw, why, is_error):
    async def go():
        post = Recorder(exc_paths=("decide",), **post_kw)
        h = make_handler(post, decide_timeout=0.1)
        data = proxy_data()
        assert await h.async_pre_call_hook(KEY, None, data, "acompletion") is data
        assert data["model"] == "local-private"
        stash = data["metadata"][STASH_KEY]
        [decide] = post.bodies("/decide")
        assert stash == {"requested_model": "auto", "route": "local-private", "source": f"fallback:{why}",
                         "decision_id": decide["decision_id"], "litellm_call_id": "call-1", "observed": False}
        assert h.state()["route_policy_fallbacks_by_reason"] == {why: 1}
        assert h.stats["route_policy_fallbacks"] == 1 and h.stats["route_policy_applied"] == 0
        assert h.stats["route_policy_errors"] == (1 if is_error else 0)
        # The decision id still meets its outcome: local-private answered.
        data["deployment"] = deployment("local-private", "openai/qwen3.8-solo")
        await h.async_post_call_failure_hook(data, RuntimeError("x"), KEY) if why == "error" else None
        if why != "error":
            await h.async_post_call_success_hook(data, KEY, {"choices": []})
        await drain(h)
        [observe] = post.bodies("/observe")
        assert observe["decision_id"] == decide["decision_id"]
        if why != "error":
            assert (observe["served_model_group"], observe["served_deployment"]) == (
                "local-private", "openai/qwen3.8-solo")

    run(go())


def test_a_hung_service_costs_at_most_the_decide_timeout():
    async def go():
        h = make_handler(Recorder(hang={"decide"}), decide_timeout=0.2)
        data = proxy_data(messages=[{"role": "user", "content": f"m{i}"} for i in range(200)])
        t0 = time.perf_counter()
        await h.async_pre_call_hook(KEY, None, data, "acompletion")
        elapsed = time.perf_counter() - t0
        assert 0.15 <= elapsed < 0.6, elapsed
        assert data["model"] == "local-private" and stash_of(data)["source"] == "fallback:timeout"

    run(go())


def test_hook_overhead_against_an_instant_service_is_small():
    """Median pre-call time with the client on (an instant fake service) vs
    off, on a 400-message chat: encoding the body and one await."""
    async def timed(h):
        samples = []
        for _ in range(40):
            data = proxy_data(messages=[{"role": "user" if i % 2 else "assistant", "content": "x" * 400}
                                        for i in range(400)])
            t0 = time.perf_counter()
            await h.async_pre_call_hook(KEY, None, data, "acompletion")
            samples.append(time.perf_counter() - t0)
        return statistics.median(samples)

    async def go():
        return await timed(make_handler(Recorder(), enabled=False)), await timed(make_handler(Recorder()))

    off, on = run(go())
    # The fake also decodes the body (as the service would); still well inside the 0.25 s budget.
    assert on - off < 0.05, (off, on)


def test_log_lines_live_once_and_fallbacks_throttled():
    lines = []

    class ListHandler(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    lh = ListHandler()
    logger.addHandler(lh)
    try:
        async def go():
            ok = make_handler(Recorder(route="local-reasoning"))
            for _ in range(3):
                await ok.async_pre_call_hook(KEY, None, proxy_data(), "acompletion")
            bad = make_handler(Recorder(status=500))
            for _ in range(3):
                await bad.async_pre_call_hook(KEY, None, proxy_data(), "acompletion")
            assert bad.stats["route_policy_errors"] == 3 and bad.stats["route_policy_fallbacks"] == 3
            shadow = make_handler(Recorder(apply=False, mode="shadow"))
            await shadow.async_pre_call_hook(KEY, None, proxy_data(), "acompletion")

        run(go())
    finally:
        logger.removeHandler(lh)
    live = [ln for ln in lines if "single pass is live" in ln]
    assert live == ["[route-policy] first decision applied: auto -> local-reasoning (ruleset default@2) "
                    "-- single pass is live"]
    fallbacks = [ln for ln in lines if "-> local-private" in ln]
    assert fallbacks == [
        "[route-policy] auto -> local-private (/decide answered HTTP 500)",
        "[route-policy] auto -> local-private (/decide chose 'local-code' but apply=False "
        "(ruleset default@2, mode 'shadow'))",
    ]


def test_never_raises():
    class BadKey:
        @property
        def key_alias(self):
            raise RuntimeError("bad key object")

    async def go():
        post = Recorder()
        h = make_handler(post)
        for data in (None, "string", {"model": "auto"}, {"model": "auto", "messages": "nope"}):
            await h.async_pre_call_hook(KEY, None, data, "acompletion")  # no exception
        weird_meta = {"model": "auto", "messages": [], "metadata": "not a dict"}
        await h.async_pre_call_hook(KEY, None, weird_meta, "acompletion")
        assert weird_meta["model"] == "local-code"  # routed; nowhere to stash it
        # A bad key object: a bug in our code path -> the safe fallback, counted.
        data = proxy_data()
        assert await h.async_pre_call_hook(BadKey(), None, data, "acompletion") is data
        assert data["model"] == "local-private" and stash_of(data)["source"] == "fallback:hook-error"
        assert h.stats["route_policy_errors"] == 1
        # Unserialisable message content: encoded with str(), still decided.
        weird = proxy_data(messages=[{"role": "user", "content": object()}])
        await h.async_pre_call_hook(KEY, None, weird, "acompletion")
        assert weird["model"] == "local-code"
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
        [observe] = [o for o in post.bodies("/observe") if o["decision_id"] == good["metadata"][STASH_KEY]["decision_id"]]
        assert observe["status"] == "success" and observe["served_model_group"] is None
        # A transport that returns garbage: a fallback, not an exception.
        h2 = make_handler(Recorder(status="weird"))
        d2 = proxy_data()
        await h2.async_pre_call_hook(KEY, None, d2, "acompletion")
        assert d2["model"] == "local-private" and h2.stats["route_policy_errors"] == 1

    run(go())


# ── /observe: non-streaming ────────────────────────────────────────────────
def response(content="hello", tool_calls=None, finish="stop", rid="chatcmpl-abc", group="local-code"):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = [ChatCompletionMessageToolCall(id=f"t{i}", type="function",
                                                           function=Function(name=n, arguments="{}"))
                             for i, n in enumerate(tool_calls)]
    r = ModelResponse(id=rid, model="auto", choices=[Choices(index=0, message=Message(**msg), finish_reason=finish)])
    r._hidden_params = {"additional_headers": {"x-litellm-model-group": group}}
    return r


async def decided(h, chain="local-code", model="openai/muse-glimmer", **kw):
    data = proxy_data(**kw)
    await h.async_pre_call_hook(KEY, None, data, "acompletion")
    data["deployment"] = deployment(chain, model)  # what the proxy sets after the call (1707)
    return data


@needs_litellm
def test_observe_non_streaming_exact_body():
    async def go():
        post = Recorder()
        h = make_handler(post)
        data = await decided(h)
        out = await h.async_post_call_success_hook(data, KEY, response())
        assert out.choices[0].message.content == "hello" + footer_text("muse-glimmer · local-code")
        await drain(h)
        [decide], [observe] = post.bodies("/decide"), post.bodies("/observe")
        assert observe == {
            "decision_id": decide["decision_id"],
            "litellm_call_id": "call-1",
            "response_id": "chatcmpl-abc",
            "served_model_group": "local-code",
            "served_deployment": "openai/muse-glimmer",
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
        resp = response(content=None, tool_calls=["sandbox-mcp_exec", "run_command"], finish="tool_calls")
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
def test_observe_claude_overflow_reason():
    async def go():
        clock, pages, post = Clock(), {}, Recorder(route="local-general")
        h = make_handler(post, clock=clock, pages=pages)
        await prime_backends(h, pages, clock, metrics(3), metrics(3))
        data = await decided(h, "local-general", "anthropic/claude-sonnet-5")
        out = await h.async_post_call_success_hook(data, KEY, response(group="local-general"))
        assert out.choices[0].message.content.endswith("*claude-sonnet-5 · local-general · overflow (local busy)*")
        await drain(h)
        [observe] = post.bodies("/observe")
        assert (observe["served_model_group"], observe["served_deployment"], observe["overflow_reason"]) == (
            "local-general", "anthropic/claude-sonnet-5", "busy")

    run(go())


def test_observe_anthropic_messages_response():
    """/v1/messages: the response is an Anthropic message dict."""
    async def go():
        post = Recorder()
        h = make_handler(post)
        data = await decided(h, messages=[{"role": "user", "content": [{"type": "text", "text": "fix it"}]}])
        resp = {"id": "msg_01", "type": "message", "role": "assistant", "model": "auto", "stop_reason": "tool_use",
                "content": [{"type": "tool_use", "id": "toolu_1", "name": "sandbox-mcp_exec", "input": {}}]}
        assert await h.async_post_call_success_hook(data, KEY, resp) is None  # no footer on this shape
        await drain(h)
        [observe] = post.bodies("/observe")
        assert (observe["response_id"], observe["finish_reason"], observe["tool_calls"]) == (
            "msg_01", "tool_use", ["sandbox-mcp_exec"])
        assert observe["served_model_group"] == "local-code"

    run(go())


def test_observe_failure_exact_body_and_none_returned():
    async def go():
        post = Recorder()
        h = make_handler(post)
        data = proxy_data()
        await h.async_pre_call_hook(KEY, None, data, "acompletion")  # no deployment: the call failed
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

    run(go())


# ── /observe: streaming ────────────────────────────────────────────────────
def stream_chunks(texts=("hel", "lo"), tool=None, finish="stop", rid="chatcmpl-s1", group="local-code"):
    hidden = {"additional_headers": {"x-litellm-model-group": group}}
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

    def __init__(self, chunks, fail_after=None, group="local-code"):
        self._chunks = chunks
        self._fail_after = fail_after
        self._hidden_params = {"additional_headers": {"x-litellm-model-group": group}}

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
        assert len(out) == len(chunks) + 1 and joined(out) == "hello" + footer_text("muse-glimmer · local-code")
        await drain(h)
        [decide], [observe] = post.bodies("/decide"), post.bodies("/observe")
        assert observe == {
            "decision_id": decide["decision_id"], "litellm_call_id": "call-1", "response_id": "chatcmpl-s1",
            "served_model_group": "local-code", "served_deployment": "openai/muse-glimmer",
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
        assert observe["served_model_group"] == "local-code"

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
        data = proxy_data(model="local-general", stream=True)  # a direct chain call: no stash
        await h.async_pre_call_hook(KEY, None, data, "acompletion")
        chunks = stream_chunks()
        out = await collect(h, data, Wrapper(chunks))
        assert out == chunks and post.calls == []

    run(go())


def test_observe_in_flight_cap_drops_and_counts():
    async def go():
        post = Recorder(hang={"observe"})
        h = make_handler(post, timeout=5.0, policy_max_in_flight=3)
        datas = [proxy_data() for _ in range(5)]
        for d in datas:
            await h.async_pre_call_hook(KEY, None, d, "acompletion")
        assert all(d["model"] == "local-code" for d in datas)  # /decide is awaited, never capped
        for d in datas:
            await h.async_post_call_failure_hook(d, RuntimeError("x"), KEY)
        assert h.policy.in_flight == 3 and h.stats["route_policy_dropped"] == 2
        assert h.state()["route_policy_in_flight"] == 3
        for t in list(h.policy._tasks):
            t.cancel()
        await asyncio.gather(*list(h.policy._tasks), return_exceptions=True)
        assert h.policy.in_flight == 0

    run(go())


# ── the real HTTP client ───────────────────────────────────────────────────
@needs_httpx
def test_httpx_post_wire_format_timeout_and_5xx():
    seen = []

    async def go():
        async def handler(request):
            body = json.loads(request.content)
            seen.append((request.method, str(request.url), request.headers.get("content-type"), body))
            if request.url.path == "/observe":
                return httpx.Response(500)
            if request.url.path.startswith("/slow/"):
                await asyncio.sleep(5)
            return httpx.Response(200, json={"decision_id": body["decision_id"], "route": "local-reasoning",
                                             "mode": "enforce", "apply": True})

        transport = httpx.MockTransport(handler)
        h = make_handler(HttpxPost(0.2, transport=transport), timeout=0.2, decide_timeout=0.2)
        data = await decided(h)
        assert data["model"] == "local-reasoning"
        await h.async_post_call_failure_hook(data, RuntimeError("x"), KEY)
        await drain(h)
        assert h.stats["route_policy_decides"] == 1 and h.stats["route_policy_errors"] == 1  # the 500
        h.policy.base_url = SERVICE + "/slow"  # a hung service: timed out, local-private
        slow = proxy_data()
        t0 = time.perf_counter()
        await h.async_pre_call_hook(KEY, None, slow, "acompletion")
        assert time.perf_counter() - t0 < 0.6
        assert slow["model"] == "local-private" and stash_of(slow)["source"] == "fallback:timeout"
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
        if self.path == "/decide":
            status, payload = 200, {"decision_id": body["decision_id"], "route": "local-code", "mode": "enforce",
                                    "apply": True, "ruleset": {"id": "default", "version": 2}}
        else:
            status, payload = 202, {"decision_id": body.get("decision_id")}
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


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
            h = make_handler(HttpxPost(2.0), url=url, timeout=2.0, decide_timeout=2.0)
            data = await decided(h, stream=True)
            assert data["model"] == "local-code"
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
ROUTE_POLICY_VARS = ("ROUTE_POLICY_ENABLED", "ROUTE_POLICY_URL", "ROUTE_POLICY_TIMEOUT_S",
                     "ROUTE_POLICY_DECIDE_TIMEOUT_S")


def test_settings_defaults_and_overrides(monkeypatch, fresh_hooks):
    for name in ROUTE_POLICY_VARS:
        monkeypatch.delenv(name, raising=False)
    with fresh_hooks():
        from hooks import settings
        assert (settings.ROUTE_POLICY_ENABLED, settings.ROUTE_POLICY_URL, settings.ROUTE_POLICY_TIMEOUT_S,
                settings.ROUTE_POLICY_DECIDE_TIMEOUT_S) == (True, "http://route-policy:8000", 2.0, 0.25)
    monkeypatch.setenv("ROUTE_POLICY_ENABLED", "false")
    monkeypatch.setenv("ROUTE_POLICY_URL", " http://elsewhere:9000/ ")
    monkeypatch.setenv("ROUTE_POLICY_TIMEOUT_S", "0.01")
    monkeypatch.setenv("ROUTE_POLICY_DECIDE_TIMEOUT_S", "0.001")
    with fresh_hooks():
        from hooks import settings
        from hooks.handler import Handler as FreshHandler
        assert (settings.ROUTE_POLICY_ENABLED, settings.ROUTE_POLICY_URL, settings.ROUTE_POLICY_TIMEOUT_S,
                settings.ROUTE_POLICY_DECIDE_TIMEOUT_S) == (False, "http://elsewhere:9000/", 0.1, 0.05)  # floors
        h = FreshHandler(start_poller=False)
        assert h.policy.enabled is False and h.policy.base_url == "http://elsewhere:9000"
        assert h.state()["config"]["route_policy_decide_timeout_s"] == 0.05
    monkeypatch.setenv("ROUTE_POLICY_ENABLED", "true")
    monkeypatch.setenv("ROUTE_POLICY_URL", "")
    monkeypatch.setenv("ROUTE_POLICY_DECIDE_TIMEOUT_S", "1.5")
    with fresh_hooks():
        from hooks.handler import Handler as FreshHandler
        h = FreshHandler(start_poller=False)
        assert h.policy.enabled is False and h.policy.decide_timeout == 1.5  # no URL, no client


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
    ("ROUTE_POLICY_DECIDE_TIMEOUT_S", "0.25"),
])
def test_defaults_agree_across_settings_compose_and_env_example(name, default):
    value = _compose_env()[name]
    m = re.fullmatch(r"\$\{" + name + r":-(.*)\}", str(value))
    assert m, f"compose must pass ${{{name}:-<default>}}: {value!r}"
    assert m.group(1) == default
    assert _env_example(name) == default


# ── against real LiteLLM ───────────────────────────────────────────────────
class _Upstream(BaseHTTPRequestHandler):
    """A stand-in OpenAI-compatible backend that records the bodies LiteLLM sends it."""

    bodies = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
        type(self).bodies.append(body)
        payload = json.dumps({"id": "chatcmpl-u", "object": "chat.completion", "created": 1, "model": body["model"],
                              "choices": [{"index": 0, "message": {"role": "assistant", "content": "hello"},
                                           "finish_reason": "stop"}],
                              "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def upstream(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    _Upstream.bodies = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        server.shutdown()
        server.server_close()


def _alias_router(base):
    """local-private and local-code on the stand-in, and this repo's `auto` alias."""
    def dep(chain, served, order):
        return {"model_name": chain, "litellm_params": {"model": f"openai/{served}", "api_base": base,
                                                        "api_key": "none", "order": order},
                "model_info": {"id": f"id-{chain}-{order}"}}

    return litellm.Router(model_list=[dep("local-private", "qwen3.8-solo", 1), dep("local-code", "muse-glimmer", 1)],
                          model_group_alias={"auto": "local-private"}, num_retries=0)


@needs_litellm
def test_the_routed_chain_answers_and_the_outcome_names_it(upstream):
    """Pre-call (the engine picks local-code) -> the Router serves the
    rewritten model -> the proxy's data["deployment"] -> footer + /observe."""
    async def go():
        router = _alias_router(upstream)
        post = Recorder(route="local-code")
        h = make_handler(post)
        data = proxy_data()
        await h.async_pre_call_hook(KEY, None, data, "acompletion")
        resp = await router.acompletion(model=data["model"], messages=data["messages"], metadata=data["metadata"])
        data["deployment"] = router.get_deployment(model_id=resp._hidden_params["model_id"])
        out = await h.async_post_call_success_hook(data, KEY, resp)
        await drain(h)
        return out, post

    out, post = run(go())
    assert [b["model"] for b in _Upstream.bodies] == ["muse-glimmer"]  # local-code's deployment, one call
    assert out.choices[0].message.content == "hello" + footer_text("muse-glimmer · local-code")
    [observe] = post.bodies("/observe")
    assert (observe["served_model_group"], observe["served_deployment"]) == ("local-code", "openai/muse-glimmer")


@needs_litellm
def test_the_stash_is_never_sent_upstream(upstream, monkeypatch):
    """data["metadata"] goes into Router.acompletion(**data); the openai
    provider must not put the stash (or metadata at all) in the upstream body
    -- also not with enable_preview_features, which forwards string values only."""
    router = _alias_router(upstream)
    stash = {"requested_model": "auto", "route": "local-code", "source": "engine",
             "decision_id": str(uuid.uuid4()), "litellm_call_id": "call-1", "observed": False}
    for preview in (False, True):
        monkeypatch.setattr(litellm, "enable_preview_features", preview)
        meta = {"headers": dict(OWUI_HEADERS), STASH_KEY: dict(stash)}
        run(router.acompletion(model="local-code", messages=[{"role": "user", "content": "hi"}], metadata=meta))
    assert len(_Upstream.bodies) == 2
    for body in _Upstream.bodies:
        assert STASH_KEY not in json.dumps(body)
        assert stash["decision_id"] not in json.dumps(body)


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
        post = Recorder(route="local-code")
        h = make_handler(post)
        old = litellm.callbacks
        litellm.callbacks = [h]
        try:
            pl = ProxyLogging(user_api_key_cache=DualCache())
            key = UserAPIKeyAuth(key_alias="open-webui")

            # Non-streaming.
            data = await pl.pre_call_hook(user_api_key_dict=key, data=proxy_data(), call_type="acompletion")
            assert data["model"] == "local-code"
            stash = data["metadata"][STASH_KEY]
            data["deployment"] = deployment()
            out = await pl.post_call_success_hook(data=data, response=response(), user_api_key_dict=key)
            assert out.choices[0].message.content == "hello" + footer_text("muse-glimmer · local-code")

            # Streaming, through the proxy's own iterator chain.
            sdata = await pl.pre_call_hook(user_api_key_dict=key, data=proxy_data(stream=True), call_type="acompletion")
            sdata["deployment"] = deployment()
            chunks = [c async for c in pl.async_post_call_streaming_iterator_hook(
                response=Wrapper(stream_chunks()), user_api_key_dict=key, request_data=sdata)]
            assert joined(chunks) == "hello" + footer_text("muse-glimmer · local-code")

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
def test_proxy_routes_anthropic_messages_too():
    """/v1/messages (Claude Code) runs the same pre-call pipeline with
    call_type anthropic_messages (anthropic_endpoints/endpoints.py:95): its
    `auto` is decided and rewritten like a chat completion's."""
    async def go():
        post = Recorder(route="local-reasoning")
        h = make_handler(post)
        old = litellm.callbacks
        litellm.callbacks = [h]
        try:
            pl = ProxyLogging(user_api_key_cache=DualCache())
            body = {"model": "auto", "max_tokens": 1024, "system": "be brief",
                    "messages": [{"role": "user", "content": [{"type": "text", "text": "prove it"}]}],
                    "tools": [{"name": "sandbox-mcp_exec", "input_schema": {"type": "object"}}],
                    "litellm_call_id": "call-m", "litellm_metadata": {"headers": {}}}
            data = await pl.pre_call_hook(user_api_key_dict=UserAPIKeyAuth(), data=body,
                                          call_type="anthropic_messages")
        finally:
            litellm.callbacks = old
        return data, post

    data, post = run(go())
    assert data["model"] == "local-reasoning" and stash_of(data)["source"] == "engine"
    assert STASH_KEY in data["litellm_metadata"] and "metadata" not in data
    [decide] = post.bodies("/decide")
    assert decide["messages"] == [{"role": "user", "content": [{"type": "text", "text": "prove it"}]}]
    assert decide["tools"] == [{"name": "sandbox-mcp_exec", "input_schema": {"type": "object"}}]


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
