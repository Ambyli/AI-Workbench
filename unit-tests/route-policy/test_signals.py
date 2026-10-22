"""The signals prefetch (phase 5): one batched POST /signals per decision.

Through the service and the real ``common.signals.SignalsClient`` on a fake
sidecar (``rp_helpers.FakeSidecar`` behind an ``httpx.MockTransport``;
never the real sidecar):

* :func:`merge`: texts deduplicated by id across evaluators and tasks,
  newest first; equal requests merged, a conflicting one left out; the
  request caps (cut, dropped, an older short text still fitting);
* the text ids: ``u<k>`` per user message (Open WebUI's RAG block
  stripped), ``t<k>`` per tool result;
* ONE call per /decide for a ruleset that asks, none for one that does not
  (the live ``default``); the decision id as ``request_id``, ``deadline_ms``
  = the budget minus 20 ms;
* the budget: a slow sidecar is cut at ROUTE_POLICY_SIGNALS_BUDGET_S, the
  signal evaluators abstain, ``pii`` still forces from its patterns, and
  /decide stays in time; the primary's answer is unchanged by a shadow's
  signals;
* shadows prefetch their own signals after the answer, with the same
  budget; /evaluate waits (10 s, no deadline cut);
* every failure mode is an abstain: no client, not local (nothing sent),
  down, 5xx, 413;
* the startup warm-up of the bound example sets;
* the metrics; and no request text in any log line or stored row.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from rp_helpers import (
    RULESETS_DIR,
    FakeSidecar,
    GatedDatabase,
    assistant,
    compile_doc,
    context,
    memory_tables,
    seed,
    signals_ruleset,
    strip_shadows,
    tool_result_round,
    user,
    wait_for,
)

from route_policy.app import create_app
from route_policy.engine import pipeline
from route_policy.rulesets.loader import Binding
from route_policy.rulesets.store import RulesetStore
from route_policy.service import Service
from route_policy.signals.model import MAX_REQUEST_CHARS, MAX_TEXT_CHARS, MAX_TEXTS, SignalRequest, SignalText, merge

SENTINEL = "zebra-quartz-7731"


@pytest.fixture
def tables(monkeypatch):
    return memory_tables(monkeypatch)


def _sample(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _enforced_signals():
    """`default-signals` as an ENFORCED primary (a future `default` that
    adopts it): its signals are fetched on the request path."""
    doc = {**copy.deepcopy(signals_ruleset().document), "id": "enforced-signals", "mode": "enforce"}
    return compile_doc(doc)


def _service(*, primary=None, shadows=True, sidecar=None, client=None, database=None, **kw) -> Service:
    s = seed()
    store = RulesetStore(None)
    for compiled in s.compiled:
        store.add(compiled)
    if primary is not None:
        store.add(primary)
        store.set_bindings([Binding("auto", None, primary.id, primary.version)])
    else:
        store.set_bindings(s.bindings if shadows else strip_shadows(s.bindings))
    if client is None and sidecar is not None:
        client = sidecar.client()
    return Service(store=store, database=database, rulesets_dir=RULESETS_DIR, signals_client=client, **kw)


def _body(messages, decision_id=None, **kw) -> dict:
    return {"decision_id": decision_id or str(uuid.uuid4()), "requested_model": "auto", "chat_id": "chat-sig",
            "message_id": "msg-1", "messages": messages, **kw}


def _vote(response: dict, evaluator_id: str) -> dict:
    (vote,) = [v for v in response["votes"] if v["evaluator"] == evaluator_id]
    return vote


# -- merging -----------------------------------------------------------------------------------


def _text(k: int, text: str, position: int) -> SignalText:
    return SignalText(f"u{k}", text, (position, k))


def test_merge_dedupes_texts_and_orders_them_newest_first():
    old, new, tool = _text(1, "old", 0), _text(2, "new", 2), SignalText("t1", "rows", (1, 1))
    batch = merge([
        SignalRequest("pii", (old, new)),
        SignalRequest("pii", (tool, new)),                                  # merged: same task, same params
        SignalRequest("injection", (new,), {"max_tokens": 256, "windows": "head_tail"}),
        SignalRequest("injection", (old,), {"max_tokens": 128, "windows": "head_tail"}),   # a conflict
        SignalRequest("language", (new, _text(3, "", 3))),                  # an empty text is never sent
    ])
    assert [t.id for t in batch.texts] == ["u2", "t1", "u1"]
    assert {p.task: p.text_ids for p in batch.tasks} == {"pii": ("u2", "t1", "u1"), "injection": ("u2",),
                                                         "language": ("u2",)}
    assert batch.conflicts == ("injection",) and batch.not_sent == () and batch.dropped == frozenset()


def test_merge_applies_the_request_caps():
    long = _text(1, "a" * (MAX_TEXT_CHARS + 50), 1)
    batch = merge([SignalRequest("pii", (long,))])
    assert batch.truncated == {"u1"} and len(batch.texts[0].text) == MAX_TEXT_CHARS
    many = merge([SignalRequest("pii", tuple(_text(k, f"text {k}", k) for k in range(1, MAX_TEXTS + 11)))])
    assert len(many.texts) == MAX_TEXTS and many.texts[0].id == f"u{MAX_TEXTS + 10}"
    assert many.dropped == {f"u{k}" for k in range(1, 11)}                   # the oldest left out
    # Past the request total, a newer big text is left out and an older short one still fits.
    texts = [_text(k, "b" * (MAX_REQUEST_CHARS // 10 - 1000), 10 + k) for k in range(1, 12)] + [
        _text(99, "short", 0)]
    capped = merge([SignalRequest("pii", tuple(texts))])
    assert capped.chars <= MAX_REQUEST_CHARS and "u99" in {t.id for t in capped.texts}
    assert len(capped.dropped) == 1


def test_text_ids_and_the_rag_block():
    rag = ("### Task:\nRespond to the user query using the provided context.\n<context>\n<source id=\"1\">a page"
           "</source>\n</context>\n\nwhat about this?")
    ctx = context([user("first"), *tool_result_round("phoenix_run_query", "row", "a"), user(rag),
                   *tool_result_round("search_web", "page", "b")])
    assert [(i.id, i.text) for i in ctx.user_items] == [("u1", "first"), ("u2", "what about this?")]
    assert [(i.id, name, i.text) for i, name in ctx.tool_items] == [("t1", "phoenix_run_query", "row"),
                                                                     ("t2", "search_web", "page")]
    assert ctx.latest_user_position == 3 and ctx.tool_items[1][0].position[0] > ctx.latest_user_position


def test_a_ruleset_without_signal_evaluators_asks_for_nothing():
    from rp_helpers import default_ruleset

    assert pipeline.signal_requests(default_ruleset(), context([user("my SSN is 523-45-6789")])) is None


# -- one call per decision -------------------------------------------------------------------------


def test_one_batched_call_per_decide(tables):
    sidecar = FakeSidecar(ner=lambda t: {"street_address": (1, 0.95)} if "Willow" in t else {})
    service = _service(primary=_enforced_signals(), sidecar=sidecar)
    body = _body([user("the blue house at the end of Willow Bend"), assistant(), user("draft them a letter")])
    with TestClient(create_app(service=service, instrument=False)) as client:
        out = client.post("/decide", json=body).json()
    (request,) = sidecar.requests
    assert request["request_id"] == body["decision_id"] and request["deadline_ms"] == 80
    assert {t["task"]: t["texts"] for t in request["tasks"]} == {
        "pii": ["u2", "u1"], "injection": ["u2"], "nearest": ["u2"], "language": ["u2"]}
    assert [t["id"] for t in request["texts"]] == ["u2", "u1"]
    assert (out["route"], out["apply"]) == ("local-private", True)
    assert _vote(out, "pii")["reason"] == "sensitive data: street_address"


def test_the_live_default_makes_no_call(tables):
    sidecar = FakeSidecar()
    service = _service(shadows=False, sidecar=sidecar)
    with TestClient(create_app(service=service, instrument=False)) as client:
        assert client.post("/decide", json=_body([user("hello")])).status_code == 200
    assert sidecar.requests == []


def test_a_slow_sidecar_is_cut_at_the_budget_and_decide_stays_in_time(tables):
    sidecar = FakeSidecar(delay_s=1.0)
    service = _service(primary=_enforced_signals(), sidecar=sidecar, signals_budget_s=0.1)
    timeouts = _sample("route_policy_signals_total", outcome="timeout")
    with TestClient(create_app(service=service, instrument=False)) as client:
        client.post("/decide", json=_body([user("warm up the locality cache")]))   # not timed: first call
        started = time.monotonic()
        out = client.post("/decide", json=_body([user("send it to jane.doe@example.com")])).json()
        elapsed = time.monotonic() - started
    assert elapsed < 0.25, elapsed      # the hook's own 0.25 s, with room for the test client
    assert out["route"] == "local-private"                                   # pii, from the patterns
    assert _vote(out, "pii")["reason"] == "sensitive data: email (patterns only: signals timed out)"
    for evaluator_id in ("injection", "topic", "language"):
        assert _vote(out, evaluator_id)["reason"] == "signals timed out", evaluator_id
    assert _sample("route_policy_signals_total", outcome="timeout") >= timeouts + 2


def test_the_primary_is_unchanged_by_a_shadows_signals(tables):
    messages = [user("my name is Sam"), assistant(), user("write me a python script")]
    with TestClient(create_app(service=_service(sidecar=FakeSidecar(), database=GatedDatabase()),
                               instrument=False)) as client:
        with_signals = client.post("/decide", json=_body(messages)).json()
    with TestClient(create_app(service=_service(database=GatedDatabase()), instrument=False)) as client:
        without = client.post("/decide", json=_body(messages)).json()
    for out in (with_signals, without):
        for vote in out["votes"]:
            vote["latency_ms"] = 0
        out["latency_ms"] = out["decision_id"] = None
    assert with_signals == without and with_signals["ruleset"] == {"id": "default", "version": 3}


# -- shadows and /evaluate ---------------------------------------------------------------------------


def test_a_shadow_prefetches_its_own_signals_after_the_answer(tables):
    sidecar = FakeSidecar(ner=lambda t: {"person": (1, 0.97)})
    service = _service(sidecar=sidecar, database=GatedDatabase())
    body = _body([user("Customer Dana Brooks, DOB 04/12/1979, wants a quote")])
    shadow_id = uuid.uuid5(uuid.UUID(body["decision_id"]), "default-signals@1")
    shadows = _sample("route_policy_signals_seconds_count", kind="shadow")
    with TestClient(create_app(service=service, instrument=False)) as client:
        out = client.post("/decide", json=body).json()
        wait_for(lambda: shadow_id in tables.decisions)
        wait_for(lambda: service.pending_writes == 0)
    assert out["ruleset"] == {"id": "default", "version": 3}
    (request,) = sidecar.requests                       # the shadow's own call, keyed by ITS decision id
    assert request["request_id"] == str(shadow_id)
    shadow = tables.decisions[shadow_id]
    assert shadow.chosen_route == "local-private" and shadow.shadow_of == uuid.UUID(body["decision_id"])
    pii = next(e for e in tables.evaluations[shadow_id] if e.evaluator_id == "pii")
    assert pii.detail["forced_by"] == ["person+date_of_birth"] and pii.detail["signals_ms"] is not None
    observe = [e for e in tables.evaluations[shadow_id] if e.role == "observe"]
    assert {e.evaluator_id for e in observe} == {"injection", "code-shape", "language", "context-size"}
    assert _sample("route_policy_signals_seconds_count", kind="shadow") == shadows + 1


def test_a_shadow_gets_the_same_budget(tables):
    sidecar = FakeSidecar(delay_s=1.0)
    service = _service(sidecar=sidecar, database=GatedDatabase(), signals_budget_s=0.05)
    body = _body([user("hello")])
    shadow_id = uuid.uuid5(uuid.UUID(body["decision_id"]), "default-signals@1")
    with TestClient(create_app(service=service, instrument=False)) as client:
        client.post("/decide", json=body)
        wait_for(lambda: shadow_id in tables.decisions)
    topic = next(e for e in tables.evaluations[shadow_id] if e.evaluator_id == "topic")
    assert topic.abstained and topic.reason == "signals timed out"


def test_evaluate_waits_for_complete_signals():
    sidecar = FakeSidecar(delay_s=0.3, nearest=lambda t: ("local-reasoning", 1.0))
    service = _service(sidecar=sidecar, signals_budget_s=0.05)
    with TestClient(create_app(service=service, instrument=False)) as client:
        out = client.post("/evaluate", json={"ruleset": {"id": "default-signals"},
                                             "messages": [user("hello there")]}).json()
    (request,) = sidecar.requests
    assert "deadline_ms" not in request and request.get("request_id") is None
    assert (out["route"], out["mode"], out["apply"]) == ("local-reasoning", "shadow", False)
    topic = _vote(out, "topic")
    assert topic["detail"]["status"] == "ok" and topic["detail"]["signals_ms"] >= 300


# -- failure modes --------------------------------------------------------------------------------


@pytest.mark.parametrize("make, reason", [
    (lambda: None, "signals unavailable"),                                             # no client
    (lambda: FakeSidecar().client("http://signals.example.com:8000"), "signals not local"),
    (lambda: FakeSidecar(down=True).client(), "signals unavailable"),
    (lambda: FakeSidecar(http_status=503).client(), "signals unavailable"),
    (lambda: FakeSidecar(http_status=413).client(), "signals failed"),
])
def test_every_failure_is_an_abstain_and_pii_still_forces(make, reason):
    service = _service(primary=_enforced_signals(), client=make())
    with TestClient(create_app(service=service, instrument=False)) as client:
        out = client.post("/evaluate", json={"ruleset": {"id": "enforced-signals"},
                                             "messages": [user("my SSN is 523-45-6789")]}).json()
        clean = client.post("/evaluate", json={"ruleset": {"id": "enforced-signals"},
                                               "messages": [user("hello")]}).json()
    assert out["route"] == "local-private" and _vote(out, "pii")["reason"].endswith(f"(patterns only: {reason})")
    assert clean["route"] == "local-general"
    for evaluator_id in ("injection", "topic", "language"):
        assert _vote(clean, evaluator_id)["reason"] == reason, evaluator_id


@pytest.mark.parametrize("where", ["signal_requests", "apply", "metrics"])
def test_load_signals_never_raises(where, monkeypatch, caplog):
    """Documented "never raises": a bug anywhere around the client call --
    merging the requests, applying the answer, counting it -- leaves "signals
    failed" (the signal evaluators abstain, pii votes from its patterns), is
    counted and is logged by type only."""
    from route_policy import metrics
    from route_policy.signals import wire

    def boom(*args, **kwargs):
        raise KeyError(f"quoting the text: {SENTINEL}")

    target = {"signal_requests": (pipeline, "signal_requests"), "apply": (wire, "apply"),
              "metrics": (metrics.signal_tasks_total, "labels")}[where]
    monkeypatch.setattr(*target, boom)
    service = _service(primary=_enforced_signals(), sidecar=FakeSidecar())
    errors = _sample("route_policy_errors_total", kind="signals")
    caplog.set_level(logging.DEBUG)
    ctx = context([user(f"{SENTINEL} send it to jane.doe@example.com")])

    async def go():
        await service.load_signals(_enforced_signals(), ctx, uuid.uuid4(), kind="primary", budget_s=1.0)
        await service.signals_client.aclose()
        return await pipeline.run(_enforced_signals(), ctx)

    decision = asyncio.run(go())
    assert ctx.signals_error == "failed"
    assert _sample("route_policy_errors_total", kind="signals") == errors + 1
    assert decision.route == "local-private"
    pii = next(v for v in decision.votes if v.evaluator == "pii")
    assert pii.reason == "sensitive data: email (patterns only: signals failed)"
    assert any("KeyError" in r.getMessage() for r in caplog.records)
    assert not any(SENTINEL in r.getMessage() for r in caplog.records)


def test_the_budget_floor_leaves_the_sidecar_time_to_answer(monkeypatch):
    """At a floor equal to the 20 ms margin the sidecar was told deadline_ms 1
    and skipped every task; the floor is the margin + 20 ms."""
    import importlib.util

    from route_policy import config

    monkeypatch.setenv("ROUTE_POLICY_SIGNALS_BUDGET_S", "0.001")
    spec = importlib.util.spec_from_file_location("_route_policy_config_floor", config.__file__)
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    assert fresh.SIGNALS_BUDGET_S == fresh.SIGNALS_BUDGET_FLOOR_S == pytest.approx(0.04)
    assert fresh.SIGNALS_BUDGET_S - fresh.SIGNALS_DEADLINE_MARGIN_S == pytest.approx(0.02)
    sidecar = FakeSidecar()
    service = _service(primary=_enforced_signals(), sidecar=sidecar, signals_budget_s=fresh.SIGNALS_BUDGET_S)
    ctx = context([user("hello")])

    async def go():
        await service.load_signals(_enforced_signals(), ctx, uuid.uuid4(), kind="primary",
                                   budget_s=fresh.SIGNALS_BUDGET_S)
        await service.signals_client.aclose()

    asyncio.run(go())
    assert [r["deadline_ms"] for r in sidecar.requests] == [20]


def test_nothing_is_sent_to_a_sidecar_that_is_not_local():
    sidecar = FakeSidecar()
    service = _service(primary=_enforced_signals(), client=sidecar.client("http://signals.example.com:8000"))
    with TestClient(create_app(service=service, instrument=False)) as client:
        client.post("/evaluate", json={"ruleset": {"id": "enforced-signals"}, "messages": [user(SENTINEL)]})
    assert sidecar.requests == []


# -- the warm-up --------------------------------------------------------------------------------------


def test_startup_warms_the_bound_example_sets():
    sidecar = FakeSidecar(status={"nearest": "warming"})
    service = _service(sidecar=sidecar)
    assert asyncio.run(service.warm_signals()) == 1
    (request,) = sidecar.requests
    (task,) = request["tasks"]
    topic = signals_ruleset().entries[7].evaluator
    assert task["task"] == "nearest" and task["texts"] == [] and request["texts"] == []
    assert task["examples"]["fingerprint"] == topic.fingerprint
    assert len(task["examples"]["items"]) == len(topic.config.examples.examples)


def test_a_warm_up_that_fails_never_stops_startup():
    assert asyncio.run(_service(sidecar=FakeSidecar(down=True)).warm_signals()) == 0
    assert asyncio.run(_service(client=None).warm_signals()) == 0
    # A binding with no signal evaluators warms nothing.
    sidecar = FakeSidecar()
    assert asyncio.run(_service(shadows=False, sidecar=sidecar).warm_signals()) == 0 and sidecar.requests == []


# -- metrics and privacy -------------------------------------------------------------------------------


def test_metrics_count_calls_and_tasks(tables):
    sidecar = FakeSidecar(status={"nearest": "warming"})
    service = _service(primary=_enforced_signals(), sidecar=sidecar)
    before = {k: _sample("route_policy_signal_tasks_total", task=k[0], status=k[1])
              for k in (("pii", "ok"), ("nearest", "warming"), ("injection", "ok"), ("language", "ok"))}
    ok = _sample("route_policy_signals_total", outcome="ok")
    primary = _sample("route_policy_signals_seconds_count", kind="primary")
    with TestClient(create_app(service=service, instrument=False)) as client:
        client.post("/decide", json=_body([user("hello")]))
    assert _sample("route_policy_signals_total", outcome="ok") == ok + 1
    assert _sample("route_policy_signals_seconds_count", kind="primary") == primary + 1
    for key, value in before.items():
        assert _sample("route_policy_signal_tasks_total", task=key[0], status=key[1]) == value + 1, key


def test_no_request_text_in_any_log_line_or_stored_row(tables, caplog):
    """Invariant 1 (ROUTE_POLICY_PHASE5_PLAN § 6), at the unit level: a
    sentinel in every text the sidecar sees -- user messages and a Phoenix
    result -- appears in no log record and no decisions / evaluations row."""
    sidecar = FakeSidecar(ner=lambda t: {"person": (1, 0.9)}, injection=lambda t: 0.95,
                          nearest=lambda t: ("local-code", 1.0))
    service = _service(sidecar=sidecar, database=GatedDatabase())
    messages = [user(f"{SENTINEL} my SSN is 523-45-6789"), assistant(f"ok {SENTINEL}"),
                user(f"look up {SENTINEL}"),
                *tool_result_round("phoenix_run_query", f'[{{"note": "{SENTINEL}", "email": "a@example.com"}}]')]
    body = _body(messages)
    shadow_id = uuid.uuid5(uuid.UUID(body["decision_id"]), "default-signals@1")
    caplog.set_level(logging.DEBUG)
    with TestClient(create_app(service=service, instrument=False)) as client:
        out = client.post("/decide", json=body).json()
        wait_for(lambda: shadow_id in tables.decisions)
        wait_for(lambda: service.pending_writes == 0)
    assert any(SENTINEL in t["text"] for t in sidecar.requests[0]["texts"])    # it was really sent
    assert SENTINEL not in json.dumps(out)
    for record in caplog.records:
        assert SENTINEL not in record.getMessage(), record.name
    for decision_id, record in tables.decisions.items():
        assert SENTINEL not in json.dumps(record.response, default=str) and SENTINEL not in (record.reason or "")
        for e in tables.evaluations[decision_id]:
            assert SENTINEL not in json.dumps([e.reason, e.detail, e.error], default=str), e.evaluator_id
