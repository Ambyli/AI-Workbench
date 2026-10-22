"""Regression tests for the chat timeout budget.

`auto` is served in ONE pass: the route-policy hook rewrites it to a chain
alias and LiteLLM streams that chain's answer straight back. So there is one
layer inside Open WebUI's total:

    Open WebUI 900  (AIOHTTP_CLIENT_TIMEOUT -- a hard total incl. streaming)
      > chain deployments, per read: local orders 1/2: 240, Claude order 3: 300

and the three pre-header timeouts a chain can burn in a row
(240 + 240 + 300 = 780 s) still fit inside it, so a stalled local backend
fails over while the turn has budget left.

plus: a timeout inside a chain goes straight to the next `order`
(router_settings.model_group_retry_policy, TimeoutErrorRetries: 0).

plus: the ai/edge HAProxy's `listen litellm` (900 s client/server inactivity)
sits OUTSIDE the chains for LAN clients on PORT_LITELLM / PORT_LITELLM_TLS --
a non-streamed answer is silent until it is complete, so the edge must outlast
the longest silence a chain can produce.

plus: `auto` has no timeout of its own -- it is a model_group_alias, not a
deployment -- so nothing sits between Open WebUI and the chain any more.
See ai/litellm/LITELLM.md > Chain timeouts.

Two layers, like test_overflow.py:

* Config invariants (need only PyYAML): parse the checked-in files. Open
  WebUI's values come from .env.example, NEVER from .env -- the test must not
  depend on, or read, the secrets file.
* Against real LiteLLM (skipped unless `litellm` is importable): build a
  v1.95.0 Router from the parsed config, prove it picks up the retry policy,
  the `auto` alias and the intended timeouts, and drive real timeouts against
  local stubs: a pre-header timeout must fail over to order 2 without
  retrying order 1, and a stall after headers must take the mid-stream
  fallback to order 2.

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
EDGE_CFG = _ROOT / "ai" / "edge" / "haproxy.cfg"
ENV_EXAMPLE = _ROOT / ".env.example"

CHAINS = ("local-general", "local-code", "local-reasoning", "local-private")


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


def _chain_deployments() -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for m in _litellm_cfg()["model_list"]:
        if m["model_name"] in CHAINS:
            out.setdefault(m["model_name"], []).append(m["litellm_params"])
    return out


def _worst_chain_silence(deployments: list[dict]) -> int:
    """The longest a chain can keep a caller waiting without a byte: every
    order's per-read timeout firing before headers, one after the other."""
    return sum(d["stream_timeout"] for d in deployments)


def _edge_litellm_timeouts() -> dict[str, int]:
    """`timeout client|server` of haproxy.cfg's `listen litellm`, in seconds."""
    out, inside = {}, False
    for raw in EDGE_CFG.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if not raw.startswith((" ", "\t")):
            inside = line == "listen litellm"
            continue
        if inside and line.startswith("timeout "):
            _, which, value = line.split()
            out[which] = _seconds(value)
    return out


# ── config invariants ──────────────────────────────────────────────────────
def test_env_example_open_webui_values_are_900():
    assert _owui() == (900, 900)


def test_request_timeout_matches_open_webui_total():
    assert _litellm_cfg()["litellm_settings"]["request_timeout"] == _owui()[0]


def test_chain_deployments_carry_both_timeouts_inside_open_webui():
    total, idle = _owui()
    chains = _chain_deployments()
    assert set(chains) == set(CHAINS)
    for alias, deployments in chains.items():
        for d in deployments:
            assert d["stream_timeout"] == d["timeout"], (alias, d["order"])
            expected = 300 if d["model"].startswith("anthropic/") else 240
            assert d["stream_timeout"] == expected, (alias, d["order"], d["model"])
            # A per-read limit: Open WebUI's stream-idle timer must not undercut it.
            assert d["stream_timeout"] < idle, (alias, d["order"])
        # Every order timing out before headers still leaves Open WebUI's total
        # time to show the error instead of cutting the turn itself.
        assert _worst_chain_silence(deployments) < total, alias


def test_auto_is_an_alias_with_no_timeout_layer_of_its_own():
    cfg = _litellm_cfg()
    assert not [m for m in cfg["model_list"] if m["model_name"] == "auto"]
    assert cfg["router_settings"]["model_group_alias"] == {"auto": "local-private"}


def test_edge_litellm_listener_outlasts_every_chain_silence():
    """LAN clients reach LiteLLM through the edge. A non-streamed answer is
    silent until complete -- up to a chain's worst-case pre-header failover --
    so the edge's inactivity timers must outlast that, and match Open WebUI's
    total (the edge is outside the budget, never the layer that cuts a turn)."""
    edge = _edge_litellm_timeouts()
    total, _ = _owui()
    assert edge["client"] == edge["server"], edge
    assert edge["server"] >= total, (edge, total)
    worst = max(_worst_chain_silence(d) for d in _chain_deployments().values())
    assert edge["server"] > worst, (edge, worst)


def test_retry_policy_sends_chain_timeouts_to_the_next_order():
    rs = _litellm_cfg().get("router_settings") or {}
    policy = rs.get("model_group_retry_policy") or {}
    assert set(policy) == set(CHAINS), "the chains, and nothing else (`auto` is rewritten before routing)"
    for alias in CHAINS:
        assert policy[alias] == {"TimeoutErrorRetries": 0}, alias
    # allowed_fails_policy switches every deployment to legacy cooldown.
    assert "allowed_fails_policy" not in rs


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
def test_v1_95_router_picks_up_policy_alias_and_timeouts():
    from litellm.router_utils.get_retry_from_policy import get_num_retries_from_retry_policy

    settings = _litellm_cfg()["litellm_settings"]
    router = litellm.Router(
        model_list=_resolved_model_list(),
        num_retries=settings["num_retries"],
        enable_pre_call_checks=True,
        **_router_settings(),
    )
    assert set(router.model_group_retry_policy) == set(CHAINS)
    timeout_exc = litellm.Timeout(message="t", model="m", llm_provider="openai")
    other_exc = litellm.InternalServerError(message="x", model="m", llm_provider="openai")
    for alias in CHAINS:
        assert get_num_retries_from_retry_policy(
            exception=timeout_exc, model_group=alias,
            model_group_retry_policy=router.model_group_retry_policy) == 0
        # Only timeouts are overridden; everything else keeps num_retries.
        assert get_num_retries_from_retry_policy(
            exception=other_exc, model_group=alias,
            model_group_retry_policy=router.model_group_retry_policy) is None
    # `auto` resolves to local-private's deployments when nothing rewrote it,
    # and is listed like a model (what /v1/models shows Open WebUI).
    assert router._get_model_from_alias("auto") == "local-private"
    listed = router.get_model_list_from_model_alias(model_name="auto")
    assert {d["litellm_params"]["model"] for d in listed} == {"openai/qwen3.8-solo", "openai/muse-glimmer"}
    assert {d["model_name"] for d in listed} == {"auto"}

    def effective(name, order=None, stream=True):
        (d,) = [m for m in router.model_list if m["model_name"] == name
                and m["litellm_params"].get("order") == order]
        return router._get_timeout(kwargs={"stream": stream}, data=d["litellm_params"])

    for stream in (True, False):
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

    settings = _router_settings()
    settings.pop("model_group_alias", None)  # its target (local-private) is not in this stub list
    return litellm.Router(
        model_list=[dep(1, order1.base), dep(2, order2.base)],
        num_retries=_litellm_cfg()["litellm_settings"]["num_retries"],
        **settings,
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
    this test starts failing after a LiteLLM bump, LITELLM.md > Chain
    timeouts is out of date."""
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
