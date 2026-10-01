"""Regression tests for the `auto` timeout budget.

Each layer must sit strictly inside the one outside it:

    Open WebUI 900  (AIOHTTP_CLIENT_TIMEOUT -- a hard total incl. streaming)
      > LiteLLM `auto` 870  (stream_timeout + timeout, ai/litellm/litellm_config.yaml)
        > Envoy listener 840  (listeners[0].timeout, ai/semantic-router/config.yaml)
          > chain orders 1/2: 240, Claude order 3: 300  (per-read)

plus: a timeout inside a chain goes straight to the next `order`
(router_settings.model_group_retry_policy, TimeoutErrorRetries: 0).
See ai/semantic-router/SEMANTIC_ROUTER.md > Timeout budget.

Two layers, like test_overflow.py:

* Config invariants (need only PyYAML): parse the checked-in files. Open
  WebUI's values come from .env.example, NEVER from .env -- the test must not
  depend on, or read, the secrets file.
* Against real LiteLLM (skipped unless `litellm` is importable): build a
  v1.95.0 Router from the parsed config, prove it picks up the retry policy
  and resolves the intended timeouts, and drive real timeouts against local
  stubs: a pre-header timeout must fail over to order 2 without retrying
  order 1, and a stall after headers must take the mid-stream fallback to
  order 2.

    python -m pytest unit-tests/litellm -q -p no:cacheprovider
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import pathlib
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

yaml = pytest.importorskip("yaml")

_ROOT = pathlib.Path(__file__).resolve().parents[2]
LITELLM_CONFIG = _ROOT / "ai" / "litellm" / "litellm_config.yaml"
ROUTER_CONFIG = _ROOT / "ai" / "semantic-router" / "config.yaml"
ENVOY_RENDER = _ROOT / "ai" / "semantic-router" / "envoy.yaml"
ENV_EXAMPLE = _ROOT / ".env.example"

CHAINS = ("local-general", "local-code", "local-reasoning", "local-private")
# Template-hard-coded in vllm-sr 0.3.0 (cli/templates/envoy.template.yaml:
# route idleTimeout, ext_proc grpc_service.timeout, connect_timeout) -- the
# listener timeout must stay below it to mean anything.
ENVOY_HARD_CAP_S = 1200


def _litellm_cfg() -> dict:
    return yaml.safe_load(LITELLM_CONFIG.read_text(encoding="utf-8"))


def _env_example(key: str) -> int:
    pattern = re.compile(rf"^{re.escape(key)}=(.*)$")
    values = [
        m.group(1).strip()
        for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
        if (m := pattern.match(line))
    ]
    assert len(values) == 1, f"{key} must appear exactly once in .env.example"
    return int(values[0])


def _seconds(value: str) -> int:
    m = re.fullmatch(r"(\d+)s", str(value).strip())
    assert m, f"expected '<n>s', got {value!r}"
    return int(m.group(1))


def _owui() -> tuple[int, int]:
    return (
        _env_example("OPENWEBUI_AIOHTTP_CLIENT_TIMEOUT"),
        _env_example("OPENWEBUI_AIOHTTP_CLIENT_STREAM_IDLE_TIMEOUT"),
    )


def _auto_params() -> dict:
    (auto,) = [m for m in _litellm_cfg()["model_list"] if m["model_name"] == "auto"]
    return auto["litellm_params"]


def _envoy_listener_s() -> int:
    cfg = yaml.safe_load(ROUTER_CONFIG.read_text(encoding="utf-8"))
    return _seconds(cfg["listeners"][0]["timeout"])


def _chain_deployments() -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for m in _litellm_cfg()["model_list"]:
        if m["model_name"] in CHAINS:
            out.setdefault(m["model_name"], []).append(m["litellm_params"])
    return out


# ── config invariants ──────────────────────────────────────────────────────
def test_env_example_open_webui_values_are_900():
    assert _owui() == (900, 900)


def test_layers_are_strictly_nested():
    total, idle = _owui()
    auto = _auto_params()
    envoy = _envoy_listener_s()
    assert auto["stream_timeout"] == auto["timeout"], "auto: stream and non-stream must agree"
    assert total > auto["stream_timeout"] > envoy, (total, auto["stream_timeout"], envoy)
    # On `auto` the response is silent until complete (Envoy buffers it), so
    # Open WebUI's idle timer is a total too -- it must not undercut auto.
    assert idle > auto["stream_timeout"]
    assert envoy < ENVOY_HARD_CAP_S


def test_request_timeout_matches_open_webui_total():
    assert _litellm_cfg()["litellm_settings"]["request_timeout"] == _owui()[0]


def test_chain_deployments_carry_both_timeouts_inside_auto():
    auto = _auto_params()["stream_timeout"]
    envoy = _envoy_listener_s()
    chains = _chain_deployments()
    assert set(chains) == set(CHAINS)
    for alias, deployments in chains.items():
        by_order = {d["order"]: d for d in deployments}
        assert by_order[1]["stream_timeout"] < auto, alias
        for d in deployments:
            assert d["stream_timeout"] == d["timeout"], (alias, d["order"])
            assert d["stream_timeout"] < envoy, (alias, d["order"])
            expected = 300 if d["model"].startswith("anthropic/") else 240
            assert d["stream_timeout"] == expected, (alias, d["order"], d["model"])


def test_retry_policy_sends_chain_timeouts_to_the_next_order():
    rs = _litellm_cfg().get("router_settings") or {}
    policy = rs.get("model_group_retry_policy") or {}
    for alias in CHAINS + ("auto",):
        assert policy.get(alias) == {"TimeoutErrorRetries": 0}, alias
    # allowed_fails_policy switches every deployment to legacy cooldown.
    assert "allowed_fails_policy" not in rs


def test_checked_in_envoy_render_carries_the_listener_timeout():
    """prepare.py's compare() is the real gate; this catches a config.yaml
    edit committed without its re-render before the box does."""
    text = ENVOY_RENDER.read_text(encoding="utf-8")
    body = text[text.index("\nadmin:") + 1:]
    want = f"{_envoy_listener_s()}s"
    route_timeouts = re.findall(r"^\s+timeout: (\S+)$", body, re.M)
    message_timeouts = re.findall(r"^\s+message_timeout: (\S+)$", body, re.M)
    assert message_timeouts == [want]
    # Every route timeout is the listener value; the only other `timeout:`
    # is the template-hard-coded ext_proc grpc_service timeout.
    assert route_timeouts.count(f"{ENVOY_HARD_CAP_S}s") == 1
    assert [t for t in route_timeouts if t != f"{ENVOY_HARD_CAP_S}s"] == [want] * (len(route_timeouts) - 1)
    assert len(route_timeouts) > 1


# ── against real LiteLLM 1.95.0 ────────────────────────────────────────────
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
try:
    import litellm  # noqa: E402
except ImportError:
    litellm = None

needs_litellm = pytest.mark.skipif(litellm is None, reason="needs litellm==1.95.0")


def _resolved_model_list() -> list:
    out = []
    for m in _litellm_cfg()["model_list"]:
        m = copy.deepcopy(m)
        if str(m["model_name"]).startswith("os.environ/"):
            m["model_name"] = "default-model"
        for k, v in list(m["litellm_params"].items()):
            if isinstance(v, str) and v.startswith("os.environ/"):
                m["litellm_params"][k] = {
                    "api_key": "sk-test-not-used",
                    "model": "openai/default-model",
                }.get(k, "http://unused.invalid/v1")
        out.append(m)
    return out


def _router_settings() -> dict:
    return copy.deepcopy(_litellm_cfg().get("router_settings") or {})


@needs_litellm
def test_router_settings_are_router_args_the_proxy_will_pass():
    """proxy_server.py:5012-5016 copies only keys that are Router args and
    silently drops the rest -- a misspelt key would be a no-op."""
    valid = set(litellm.Router.get_valid_args())
    assert set(_router_settings()) <= valid


@needs_litellm
def test_v1_95_router_picks_up_policy_and_timeouts():
    from litellm.router_utils.get_retry_from_policy import get_num_retries_from_retry_policy

    settings = _litellm_cfg()["litellm_settings"]
    router = litellm.Router(
        model_list=_resolved_model_list(),
        num_retries=settings["num_retries"],
        enable_pre_call_checks=True,
        **_router_settings(),
    )
    # The four chains, plus `auto`: a turn that used its whole budget must not
    # be started again with seconds left before Open WebUI's cap.
    assert set(router.model_group_retry_policy) == set(CHAINS) | {"auto"}
    timeout_exc = litellm.Timeout(message="t", model="m", llm_provider="openai")
    other_exc = litellm.InternalServerError(message="x", model="m", llm_provider="openai")
    for alias in CHAINS + ("auto",):
        assert get_num_retries_from_retry_policy(
            exception=timeout_exc, model_group=alias,
            model_group_retry_policy=router.model_group_retry_policy) == 0
        # Only timeouts are overridden; everything else keeps num_retries.
        assert get_num_retries_from_retry_policy(
            exception=other_exc, model_group=alias,
            model_group_retry_policy=router.model_group_retry_policy) is None

    def effective(name, order=None, stream=True):
        (d,) = [m for m in router.model_list if m["model_name"] == name
                and m["litellm_params"].get("order") == order]
        return router._get_timeout(kwargs={"stream": stream}, data=d["litellm_params"])

    for stream in (True, False):
        assert effective("auto", stream=stream) == 870
        assert effective("local-general", 1, stream) == 240
        assert effective("local-general", 3, stream) == 300
        assert effective("local-private", 2, stream) == 240


class _Stub:
    """A tiny OpenAI-compatible upstream on 127.0.0.1.

    mode "slow":  `delay` seconds before ANY response bytes, so a client
                  timeout fires BEFORE headers, then a normal answer.
    mode "stall": 200 + SSE headers at once, then silence -- a backend that
                  accepted the request and never produced a chunk.
    mode "ok":    answers at once; SSE when the request asks to stream.
    """

    def __init__(self, mode: str, reply: str = "", delay: float = 5) -> None:
        self.hits = 0
        self._release = threading.Event()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # quiet
                pass

            def _send(self, ctype: str, body: bytes) -> None:
                self.send_response(200)
                self.send_header("content-type", ctype)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):  # noqa: N802
                stub.hits += 1
                req = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
                try:
                    if mode == "slow" and stub._release.wait(delay):
                        return
                    if mode == "stall":
                        self.send_response(200)
                        self.send_header("content-type", "text/event-stream")
                        self.send_header("transfer-encoding", "chunked")
                        self.end_headers()
                        self.wfile.flush()
                        stub._release.wait(delay)
                        return
                    if req.get("stream"):
                        chunk = {"id": "x", "object": "chat.completion.chunk", "created": 0, "model": reply,
                                 "choices": [{"index": 0, "finish_reason": None,
                                              "delta": {"role": "assistant", "content": reply}}]}
                        done = dict(chunk, choices=[{"index": 0, "finish_reason": "stop", "delta": {}}])
                        sse = f"data: {json.dumps(chunk)}\n\ndata: {json.dumps(done)}\n\ndata: [DONE]\n\n"
                        self._send("text/event-stream", sse.encode())
                        return
                    self._send("application/json", json.dumps({
                        "id": "x", "object": "chat.completion", "created": 0, "model": reply,
                        "choices": [{"index": 0, "finish_reason": "stop",
                                     "message": {"role": "assistant", "content": reply}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    }).encode())
                except OSError:
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._release.set()
        self.server.shutdown()
        self.server.server_close()


def _stub_chain(order1: _Stub, order2: _Stub):
    """A two-order `local-general` with a 1 s timeout, and this repo's
    num_retries + router_settings."""
    def dep(order, base):
        return {"model_name": "local-general", "litellm_params": {
            "model": "openai/stub", "api_base": base, "api_key": "none",
            "order": order, "timeout": 1, "stream_timeout": 1}}

    return litellm.Router(
        model_list=[dep(1, order1.base), dep(2, order2.base)],
        num_retries=_litellm_cfg()["litellm_settings"]["num_retries"],
        **_router_settings(),
    )


@needs_litellm
def test_pre_header_timeout_fails_over_to_order_2_without_retrying_order_1():
    slow, fast = _Stub("slow", "order-1"), _Stub("ok", "order-2")
    try:
        resp = asyncio.run(_stub_chain(slow, fast).acompletion(
            model="local-general", messages=[{"role": "user", "content": "hi"}]))
        assert resp.choices[0].message.content == "order-2"
        # Without model_group_retry_policy this is 2: num_retries: 1 re-picks
        # order 1 before the order fallback runs.
        assert slow.hits == 1, "order 1 was retried after a timeout"
        assert fast.hits == 1
    finally:
        slow.close()
        fast.close()


@needs_litellm
def test_stall_after_headers_falls_over_mid_stream_at_v1_95():
    """Headers, then no chunk: the read timeout fires inside the stream. At
    v1.95.0, for an openai-compatible deployment, it maps to
    APIConnectionError (no status code -> exception_mapping_utils.py:482-490),
    which is NOT a 4xx, so the stream wrapper raises MidStreamFallbackError
    (streaming_handler.py:2153-2217) and the Router's mid-stream fallback
    (router.py:2066-2150) moves to order 2. Only a mid-stream error that maps
    to a 4xx (streaming_handler.py:2202-2206) is raised without fallback. If
    this test starts failing after a LiteLLM bump, SEMANTIC_ROUTER.md >
    Timeout budget is out of date."""
    stall, fast = _Stub("stall"), _Stub("ok", "order-2")

    async def run():
        stream = await _stub_chain(stall, fast).acompletion(
            model="local-general", stream=True, messages=[{"role": "user", "content": "hi"}])
        return "".join([c.choices[0].delta.content or "" async for c in stream if c and c.choices])

    try:
        assert asyncio.run(run()) == "order-2"
        assert stall.hits == 1
        assert fast.hits == 1
    finally:
        stall.close()
        fast.close()
