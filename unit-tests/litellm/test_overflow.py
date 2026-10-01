"""Unit tests for ai/litellm/overflow.py -- the chain-alias overflow hook.

Two layers:

* Pure logic (always runs): /metrics parsing, the busy/idle hysteresis, the
  down / stale / unknown verdicts, the per-request filter rule, the
  concurrent poller and the never-throw guarantee. The HTTP probe is a stub;
  time is a fake clock.
* Against real LiteLLM (skipped unless `litellm` is importable): the chain
  aliases are loaded straight from ai/litellm/litellm_config.yaml into a
  `litellm.Router`, and `async_get_healthy_deployments` / `acompletion` are
  driven with the hook registered -- proving the hook runs before the order
  filter, that a long prompt skips qwen3.8-solo, and that a failed order-1
  call is retried at order 2.

Run from the repo root in any venv with pytest (+ litellm==1.95.0 and PyYAML
for the second layer):

    python -m pytest unit-tests/litellm -q -p no:cacheprovider
"""

from __future__ import annotations

import asyncio
import copy
import os
import pathlib
import sys
import time

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_LITELLM_DIR = _ROOT / "ai" / "litellm"
if str(_LITELLM_DIR) not in sys.path:
    sys.path.insert(0, str(_LITELLM_DIR))

import overflow  # noqa: E402
from overflow import (  # noqa: E402
    BUSY,
    DOWN,
    OK,
    UNKNOWN,
    BackendState,
    OverflowHandler,
    metrics_url,
    parse_queue_metrics,
)

QWEN = "http://qwen3.8-solo:8000/v1"
MUSE = "http://muse-glimmer:8000/v1"


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def dep(model: str, order, api_base=None) -> dict:
    params = {"model": model}
    if order is not None:
        params["order"] = order
    if api_base is not None:
        params["api_base"] = api_base
    return {"model_name": "chain", "litellm_params": params, "model_info": {"id": f"{model}-{order}"}}


def general_chain() -> list:
    return [
        dep("openai/qwen3.8-solo", 1, QWEN),
        dep("openai/muse-glimmer", 2, MUSE),
        dep("anthropic/claude-sonnet-5", 3),
    ]


def private_chain() -> list:
    return [dep("openai/qwen3.8-solo", 1, QWEN), dep("openai/muse-glimmer", 2, MUSE)]


def metrics(waiting: float, running: float = 0) -> str:
    return (
        "# HELP vllm:num_requests_waiting Number of requests waiting.\n"
        "# TYPE vllm:num_requests_waiting gauge\n"
        f'vllm:num_requests_waiting{{engine="0",model_name="m"}} {waiting}\n'
        f'vllm:num_requests_running{{engine="0",model_name="m"}} {running}\n'
    )


def make_handler(clock: Clock, pages: dict | None = None) -> OverflowHandler:
    """A handler whose fetch serves `pages[url]` (str) or raises it (Exception)."""
    pages = pages if pages is not None else {}

    async def fetch(url: str) -> str:
        page = pages.get(url)
        if isinstance(page, BaseException):
            raise page
        if page is None:
            raise ConnectionError("no route")
        return page

    return OverflowHandler(
        fetch=fetch, clock=clock, busy_after=2.0, idle_after=5.0,
        poll_interval=1.0, probe_timeout=0.5, start_poller=False,
    )


def poll(h: OverflowHandler) -> None:
    asyncio.run(h.poll_once())


def models(deployments: list) -> list:
    return [d["litellm_params"]["model"] for d in deployments]


# ── metrics_url / parse_queue_metrics ──────────────────────────────────────
@pytest.mark.parametrize(
    "base,url",
    [
        ("http://qwen3.8-solo:8000/v1", "http://qwen3.8-solo:8000/metrics"),
        ("http://qwen3.8-solo:8000/v1/", "http://qwen3.8-solo:8000/metrics"),
        ("http://host:8000", "http://host:8000/metrics"),
    ],
)
def test_metrics_url_strips_v1(base, url):
    assert metrics_url(base) == url


def test_parse_sums_across_label_sets_and_ignores_lookalikes():
    text = (
        "# HELP vllm:num_requests_waiting x\n"
        'vllm:num_requests_waiting{engine="0"} 2.0\n'
        'vllm:num_requests_waiting{engine="1"} 3.0\n'
        'vllm:num_requests_waiting_by_reason{reason="capacity"} 99.0\n'
        'vllm:num_requests_running{engine="0"} 1.0\n'
        "vllm:num_requests_running 2\n"
    )
    assert parse_queue_metrics(text) == (5.0, 3.0)


def test_parse_reports_missing_metric_as_none_not_zero():
    assert parse_queue_metrics("process_cpu_seconds_total 12.0\n") == (None, None)
    assert parse_queue_metrics(metrics(0)) == (0.0, 0.0)


# ── hysteresis ─────────────────────────────────────────────────────────────
def test_busy_only_after_waiting_held_for_busy_after():
    st = BackendState(QWEN, metrics_url(QWEN))
    st.apply_sample(0.0, 1, 3, busy_after=2, idle_after=5)
    assert not st.busy
    st.apply_sample(1.9, 2, 3, busy_after=2, idle_after=5)
    assert not st.busy
    st.apply_sample(2.0, 1, 3, busy_after=2, idle_after=5)
    assert st.busy


def test_a_zero_sample_restarts_the_busy_timer():
    st = BackendState(QWEN, metrics_url(QWEN))
    st.apply_sample(0.0, 1, 3, 2, 5)
    st.apply_sample(1.5, 0, 3, 2, 5)
    st.apply_sample(2.5, 1, 3, 2, 5)
    assert not st.busy  # waiting has only held since 2.5
    st.apply_sample(4.5, 1, 3, 2, 5)
    assert st.busy


def test_busy_clears_only_after_idle_held_for_idle_after():
    st = BackendState(QWEN, metrics_url(QWEN))
    st.apply_sample(0.0, 1, 3, 2, 5)
    st.apply_sample(2.0, 1, 3, 2, 5)
    assert st.busy
    st.apply_sample(3.0, 0, 3, 2, 5)
    st.apply_sample(7.9, 0, 3, 2, 5)
    assert st.busy
    st.apply_sample(8.5, 1, 3, 2, 5)  # a blip of queue restarts the idle timer
    st.apply_sample(9.0, 0, 3, 2, 5)
    st.apply_sample(13.9, 0, 3, 2, 5)
    assert st.busy
    st.apply_sample(14.0, 0, 3, 2, 5)
    assert not st.busy


def test_failure_is_down_and_recovery_starts_clean():
    st = BackendState(QWEN, metrics_url(QWEN))
    st.apply_sample(0.0, 1, 3, 2, 5)
    st.apply_sample(2.0, 1, 3, 2, 5)
    assert st.verdict(2.0, 5) == BUSY
    st.apply_failure(3.0, "ConnectError")
    assert st.verdict(3.0, 5) == DOWN
    st.apply_sample(4.0, 0, 0, 2, 5)
    assert st.verdict(4.0, 5) == OK


def test_verdicts_unknown_when_never_probed_stale_or_metric_missing():
    st = BackendState(QWEN, metrics_url(QWEN))
    assert st.verdict(0.0, 5) == UNKNOWN
    st.apply_failure(0.0, "boom")
    assert st.verdict(4.9, 5) == DOWN
    assert st.verdict(5.1, 5) == UNKNOWN  # stale: the poller stopped -> keep
    st.apply_sample(10.0, None, None, 2, 5)
    assert st.verdict(10.0, 5) == UNKNOWN  # a 200 that is not vLLM -> keep


# ── the filter rule ────────────────────────────────────────────────────────
def busy(h: OverflowHandler, clock: Clock, url: str, pages: dict) -> None:
    pages[url] = metrics(4, 3)
    poll(h)
    clock.advance(2.5)
    poll(h)


def test_unknown_backends_keep_everything():
    clock = Clock()
    h = make_handler(clock)
    chain = general_chain()
    assert h.filter_deployments("local-general", chain) is chain


def test_idle_chain_is_untouched():
    clock = Clock()
    pages = {metrics_url(QWEN): metrics(0), metrics_url(MUSE): metrics(0)}
    h = make_handler(clock, pages)
    h.filter_deployments("local-general", general_chain())  # learn backends
    poll(h)
    chain = general_chain()
    assert h.filter_deployments("local-general", chain) is chain


def test_busy_order1_spills_to_order2_not_claude():
    clock = Clock()
    pages = {metrics_url(QWEN): metrics(0), metrics_url(MUSE): metrics(0)}
    h = make_handler(clock, pages)
    h.filter_deployments("local-general", general_chain())
    busy(h, clock, metrics_url(QWEN), pages)
    out = h.filter_deployments("local-general", general_chain())
    assert models(out) == ["openai/muse-glimmer", "anthropic/claude-sonnet-5"]
    assert h.stats["spills"] == 1


def test_waiting_shorter_than_busy_after_does_not_spill():
    clock = Clock()
    pages = {metrics_url(QWEN): metrics(4), metrics_url(MUSE): metrics(0)}
    h = make_handler(clock, pages)
    h.filter_deployments("local-general", general_chain())
    poll(h)
    clock.advance(1.0)
    poll(h)
    assert len(h.filter_deployments("local-general", general_chain())) == 3


def test_both_local_busy_or_down_leaves_only_claude():
    clock = Clock()
    pages = {metrics_url(QWEN): metrics(0), metrics_url(MUSE): ConnectionError("refused")}
    h = make_handler(clock, pages)
    h.filter_deployments("local-general", general_chain())
    busy(h, clock, metrics_url(QWEN), pages)
    out = h.filter_deployments("local-general", general_chain())
    assert models(out) == ["anthropic/claude-sonnet-5"]


def test_down_order1_spills_to_order2():
    clock = Clock()
    pages = {metrics_url(QWEN): ConnectionError("refused"), metrics_url(MUSE): metrics(0)}
    h = make_handler(clock, pages)
    h.filter_deployments("local-reasoning", general_chain())
    poll(h)
    out = h.filter_deployments("local-general", general_chain())
    assert models(out) == ["openai/muse-glimmer", "anthropic/claude-sonnet-5"]


def test_claude_is_never_dropped():
    clock = Clock()
    pages = {metrics_url(QWEN): ConnectionError("x"), metrics_url(MUSE): ConnectionError("x")}
    h = make_handler(clock, pages)
    h.filter_deployments("local-general", general_chain())
    poll(h)
    out = h.filter_deployments("local-general", general_chain())
    assert "anthropic/claude-sonnet-5" in models(out)


def test_local_private_both_busy_stays_local():
    clock = Clock()
    pages = {metrics_url(QWEN): metrics(0), metrics_url(MUSE): metrics(0)}
    h = make_handler(clock, pages)
    h.filter_deployments("local-private", private_chain())
    pages[metrics_url(QWEN)] = metrics(3)
    pages[metrics_url(MUSE)] = metrics(3)
    poll(h)
    clock.advance(2.5)
    poll(h)
    chain = private_chain()
    out = h.filter_deployments("local-private", chain)
    assert out == chain  # both busy -> both kept -> order 1 queues locally
    assert all(d["litellm_params"].get("api_base") for d in out)
    assert h.stats["kept_all_overloaded"] == 1


def test_local_private_prefers_busy_over_down():
    clock = Clock()
    pages = {metrics_url(QWEN): ConnectionError("refused"), metrics_url(MUSE): metrics(0)}
    h = make_handler(clock, pages)
    h.filter_deployments("local-private", private_chain())
    busy(h, clock, metrics_url(MUSE), pages)
    out = h.filter_deployments("local-private", private_chain())
    assert models(out) == ["openai/muse-glimmer"]


def test_local_private_all_down_returns_original():
    clock = Clock()
    h = make_handler(clock, {})
    h.filter_deployments("local-private", private_chain())
    poll(h)
    chain = private_chain()
    assert h.filter_deployments("local-private", chain) is chain


def test_groups_without_two_orders_are_never_touched():
    clock = Clock()
    h = make_handler(clock, {})
    single = [dep("openai/qwen3.8-solo", None, QWEN), dep("openai/qwen3.8-solo", None, QWEN)]
    poll(h)
    assert h.filter_deployments("qwen3.8-solo", single) is single
    same_order = [dep("openai/qwen3.8-solo", 1, QWEN), dep("openai/muse-glimmer", 1, MUSE)]
    assert h.filter_deployments("x", same_order) is same_order
    assert h.state()["backends"] == {}  # not a chain -> not even learnt


def test_stale_data_keeps_everything():
    clock = Clock()
    pages = {metrics_url(QWEN): ConnectionError("x"), metrics_url(MUSE): metrics(0)}
    h = make_handler(clock, pages)
    h.filter_deployments("local-general", general_chain())
    poll(h)
    assert len(h.filter_deployments("local-general", general_chain())) == 2
    clock.advance(5.1)  # > 5 x poll interval with no new probe
    assert len(h.filter_deployments("local-general", general_chain())) == 3


def test_hook_never_throws():
    h = make_handler(Clock())

    def boom(*_a, **_k):
        raise RuntimeError("bug")

    h.filter_deployments = boom  # type: ignore[assignment]
    chain = general_chain()
    out = asyncio.run(h.async_filter_deployments("local-general", chain, None))
    assert out is chain and h.stats["errors"] == 1
    assert asyncio.run(h.async_filter_deployments("m", None, None)) is None


def test_spill_logging_is_throttled():
    import logging

    records = []

    class ListHandler(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    clock = Clock()
    pages = {metrics_url(QWEN): ConnectionError("x"), metrics_url(MUSE): metrics(0)}
    h = make_handler(clock, pages)
    h.filter_deployments("local-general", general_chain())
    poll(h)
    lh = ListHandler()
    overflow.logger.addHandler(lh)
    try:
        for _ in range(5):
            h.filter_deployments("local-general", general_chain())
        clock.advance(1.0)
        poll(h)
        clock.advance(30.0)
        poll(h)
        h.filter_deployments("local-general", general_chain())
    finally:
        overflow.logger.removeHandler(lh)
    assert len(records) == 2
    assert "dropping openai/qwen3.8-solo@order1 (down" in records[0]
    assert "+4 similar" in records[1]


# ── poller ─────────────────────────────────────────────────────────────────
def test_poll_once_probes_concurrently():
    async def slow_fetch(url: str) -> str:
        await asyncio.sleep(0.3)
        return metrics(0)

    h = OverflowHandler(fetch=slow_fetch, poll_interval=1.0, probe_timeout=1.0, start_poller=False)
    h.filter_deployments("local-general", general_chain())
    start = time.perf_counter()
    asyncio.run(h.poll_once())
    assert time.perf_counter() - start < 0.55  # 2 x 0.3 s would be serial
    assert all(b["verdict"] == OK for b in h.state()["backends"].values())


def test_probe_timeout_counts_as_down():
    async def hang(url: str) -> str:
        await asyncio.sleep(5)
        return metrics(0)

    h = OverflowHandler(fetch=hang, poll_interval=1.0, probe_timeout=0.1, start_poller=False)
    h.filter_deployments("local-general", general_chain())
    asyncio.run(h.poll_once())
    assert {b["verdict"] for b in h.state()["backends"].values()} == {DOWN}


def test_poller_starts_lazily_and_restarts_when_dead():
    calls = []

    async def fetch(url: str) -> str:
        calls.append(url)
        return metrics(0)

    async def scenario():
        h = OverflowHandler(fetch=fetch, poll_interval=0.2, probe_timeout=0.1)
        assert h._task is None
        await h.async_filter_deployments("local-general", general_chain(), None)
        first = h._task
        assert first is not None and not first.done()
        await asyncio.sleep(0.05)
        first.cancel()
        try:
            await first
        except asyncio.CancelledError:
            pass

        async def died():
            raise RuntimeError("poller crashed")

        dead = asyncio.get_running_loop().create_task(died())
        await asyncio.sleep(0)
        h._task = dead
        await h.async_filter_deployments("local-general", general_chain(), None)
        assert h._task is not dead and not h._task.done()
        assert h.stats["poller_restarts"] == 1
        await asyncio.sleep(0.05)
        h._task.cancel()
        return h

    h = asyncio.run(scenario())
    assert set(calls) >= {metrics_url(QWEN), metrics_url(MUSE)}
    assert h.state()["backends"][QWEN]["verdict"] == OK


# ── against real LiteLLM 1.95.0 ────────────────────────────────────────────
try:
    import litellm  # noqa: E402
    import yaml  # noqa: E402
except ImportError:  # pure-logic tests above still run
    litellm = yaml = None

pytestmark_litellm = pytest.mark.skipif(litellm is None, reason="needs litellm==1.95.0 + PyYAML")

CONFIG = _LITELLM_DIR / "litellm_config.yaml"
CHAINS = ("local-general", "local-code", "local-reasoning", "local-private")


def chain_model_list() -> list:
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    out = []
    for m in cfg["model_list"]:
        if m["model_name"] in CHAINS:
            m = copy.deepcopy(m)
            if m["litellm_params"].get("api_key", "").startswith("os.environ/"):
                m["litellm_params"]["api_key"] = "sk-test-not-used"
            out.append(m)
    return out


@pytestmark_litellm
def test_config_chain_table_is_what_the_policy_says():
    by = {}
    for m in chain_model_list():
        p = m["litellm_params"]
        by.setdefault(m["model_name"], []).append((p["order"], p["model"]))
    assert sorted(by["local-general"]) == [(1, "openai/qwen3.8-solo"), (2, "openai/muse-glimmer"), (3, "anthropic/claude-sonnet-5")]
    assert sorted(by["local-code"]) == sorted(by["local-general"])
    assert sorted(by["local-reasoning"]) == [(1, "openai/muse-glimmer"), (2, "openai/qwen3.8-solo"), (3, "anthropic/claude-opus-5-5")]
    assert sorted(by["local-private"]) == [(1, "openai/qwen3.8-solo"), (2, "openai/muse-glimmer")]
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    for fb in cfg["litellm_settings"]["fallbacks"] + cfg["litellm_settings"]["context_window_fallbacks"]:
        assert not any(k in CHAINS for k in fb), fb
        assert not any("claude" in t for k, v in fb.items() if k == "auto" for t in v)
    assert cfg["litellm_settings"]["callbacks"] == ["overflow.handler"]


@pytestmark_litellm
def test_litellm_can_load_the_hook_the_way_the_proxy_does():
    """The proxy loads `overflow.handler` with spec_from_file_location +
    exec_module and never registers it in sys.modules
    (proxy/types_utils/utils.py:42-49). A @dataclass in the module crashed
    exactly there -- this is the regression test."""
    from litellm.integrations.custom_logger import CustomLogger
    from litellm.proxy.types_utils.utils import get_instance_fn

    h = get_instance_fn("overflow.handler", config_file_path=str(CONFIG))
    assert isinstance(h, CustomLogger)
    assert type(h).__name__ == "OverflowHandler"
    assert h.state()["config"]["busy_after_s"] == float(os.environ.get("OVERFLOW_BUSY_AFTER_S") or 2.0)


def make_router(handler: OverflowHandler, **kw):
    router = litellm.Router(model_list=chain_model_list(), enable_pre_call_checks=True, **kw)
    litellm.callbacks = [handler]
    return router


def healthy(router, model: str, content: str = "hi") -> list:
    return asyncio.run(router.async_get_healthy_deployments(
        model=model, request_kwargs={}, messages=[{"role": "user", "content": content}]))


@pytestmark_litellm
def test_router_order_filter_runs_after_the_hook():
    clock = Clock()
    pages = {metrics_url(QWEN): metrics(0), metrics_url(MUSE): metrics(0)}
    h = make_handler(clock, pages)
    router = make_router(h)
    try:
        assert models(healthy(router, "local-general")) == ["openai/qwen3.8-solo"]  # learns backends
        poll(h)
        assert models(healthy(router, "local-general")) == ["openai/qwen3.8-solo"]
        assert models(healthy(router, "local-reasoning")) == ["openai/muse-glimmer"]
        busy(h, clock, metrics_url(QWEN), pages)
        assert models(healthy(router, "local-general")) == ["openai/muse-glimmer"]
        assert models(healthy(router, "local-reasoning")) == ["openai/muse-glimmer"]
        busy(h, clock, metrics_url(MUSE), pages)
        assert models(healthy(router, "local-general")) == ["anthropic/claude-sonnet-5"]
        assert models(healthy(router, "local-reasoning")) == ["anthropic/claude-opus-5-5"]
        assert models(healthy(router, "local-private")) == ["openai/qwen3.8-solo"]  # queues locally
    finally:
        litellm.callbacks = []


@pytestmark_litellm
def test_router_long_prompt_skips_qwen_by_context_window():
    h = make_handler(Clock(), {})
    router = make_router(h)
    try:
        long_prompt = "word " * 120_000  # > 114688 tokens, < 245760
        assert models(healthy(router, "local-general", long_prompt)) == ["openai/muse-glimmer"]
        assert models(healthy(router, "local-private", long_prompt)) == ["openai/muse-glimmer"]
    finally:
        litellm.callbacks = []


@pytestmark_litellm
def test_router_failed_order1_call_retries_at_order2():
    """A deployment that FAILS at call time is retried at the next order level
    with no `fallbacks` entry (router.py:6132-6182). Order 1 points at a closed
    port; order 2 answers with a mock."""
    h = make_handler(Clock(), {})
    ml = chain_model_list()
    for m in ml:
        if m["model_name"] == "local-private":
            p = m["litellm_params"]
            if p["order"] == 1:
                p["api_base"] = "http://127.0.0.1:9/v1"
            else:
                p["mock_response"] = "answered by order 2"
    router = litellm.Router(model_list=ml, enable_pre_call_checks=True, num_retries=0)
    litellm.callbacks = [h]
    try:
        resp = asyncio.run(router.acompletion(
            model="local-private", messages=[{"role": "user", "content": "hi"}], timeout=5))
        assert resp.choices[0].message.content == "answered by order 2"
    finally:
        litellm.callbacks = []
