"""Shadow rulesets and /decide's after-the-answer work (service.py), with no
real database:

* the store resolves the primary and the shadow refs; shadows load from the
  cache, and one that cannot load is skipped and counted;
* the primary's /decide response is the same whether or not a shadow is
  bound -- shadows run after it, in one background task;
* a shadow decision is stored as its own row: id uuid5(primary, ruleset@version),
  shadow_of = the primary, its own evaluations; a repeated /decide writes
  nothing twice;
* the analysis requests of the primary and of each shadow reach the queue
  with their provenance;
* a shadow that raises, a hung database, a full task cap -- none of them
  changes, delays or fails the answer;
* /evaluate reads stored verdicts (read-only) and never enqueues.
"""

from __future__ import annotations

import asyncio
import copy
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from rp_helpers import (
    DECIDE_KEYS,
    RULESETS_DIR,
    GatedDatabase as FakeDatabase,
    RecordingQueue,
    assistant,
    compile_doc,
    memory_tables,
    seed,
    strip_shadows,
    tool_round,
    user,
    wait_for,
)

from common.conversation import render_transcript
from route_policy.analysis.model import Verdict
from route_policy.app import create_app
from route_policy.evaluators.llm_classifier import input_key
from route_policy.rulesets.loader import Binding
from route_policy.rulesets.store import Resolved, RulesetStore
from route_policy.service import Service, shadow_decision_id

PRIMARY = ("default", 3)
SHADOW = ("default-llm", 3)


@pytest.fixture
def tables(monkeypatch):
    """decisions (+ evaluations) and analyses, in memory."""
    return memory_tables(monkeypatch)


def _store(shadows: bool = True) -> RulesetStore:
    s = seed()
    store = RulesetStore(None)
    for compiled in s.compiled:
        store.add(compiled)
    store.set_bindings(s.bindings if shadows else strip_shadows(s.bindings))
    return store


def _service(database=None, *, shadows: bool = True, queue=None, **kw) -> Service:
    return Service(store=_store(shadows), database=database, rulesets_dir=RULESETS_DIR, analysis_queue=queue, **kw)


def _body(decision_id=None, messages=None, **kw) -> dict:
    body = {"decision_id": decision_id or str(uuid.uuid4()), "requested_model": "auto", "litellm_call_id": "call-1",
            "chat_id": "chat-s", "message_id": "msg-2", "user_id": "u1",
            "messages": messages if messages is not None else [user("hello"), assistant("hi"),
                                                              user("tell me about the moon")]}
    body.update(kw)
    return body


def _sample(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _without_latency(response: dict) -> dict:
    out = copy.deepcopy(response)
    out["latency_ms"] = 0
    for vote in out["votes"]:
        vote["latency_ms"] = 0
    return out


# -- resolving ------------------------------------------------------------------------------


def test_resolve_returns_the_primary_and_the_shadow_refs():
    store = _store()
    resolved = asyncio.run(store.resolve("auto", None))
    assert (resolved.primary.id, resolved.primary.version) == PRIMARY and resolved.shadows == (SHADOW,)
    assert [(r.id, r.version, r.mode) for r in asyncio.run(store.shadow_rulesets(resolved))] == [
        ("default-llm", 3, "shadow")]


def test_a_shadow_that_cannot_load_is_skipped_and_counted():
    store = _store()
    resolved = Resolved(store._compiled[PRIMARY], (("ghost", 1), SHADOW))
    before = _sample("route_policy_errors_total", kind="shadow_load")
    assert [r.id for r in asyncio.run(store.shadow_rulesets(resolved))] == ["default-llm"]
    assert _sample("route_policy_errors_total", kind="shadow_load") == before + 1


def test_shadow_ids_are_derived():
    primary = uuid.UUID("6f0e8c1e-8f7e-4c55-9a39-0f3f7ab5d001")
    (llm,) = [r for r in seed().compiled if r.id == "default-llm"]
    assert shadow_decision_id(primary, llm) == uuid.uuid5(primary, "default-llm@3")
    assert shadow_decision_id(primary, llm) == shadow_decision_id(primary, llm)
    assert shadow_decision_id(uuid.uuid4(), llm) != shadow_decision_id(primary, llm)


# -- the primary is untouched -------------------------------------------------------------------


@pytest.mark.parametrize("messages", [
    [user("hello")],
    [user("go"), *tool_round("sandbox-mcp_exec")],
    [user("fix app.py"), assistant(), user("and the weather?")],
    [user("my SSN is 123-45-6789")],
])
def test_the_primary_response_is_the_same_with_and_without_a_shadow(tables, messages):
    body = _body(messages=messages)
    with TestClient(create_app(service=_service(FakeDatabase()), instrument=False)) as with_shadow:
        a = with_shadow.post("/decide", json=body)
    with TestClient(create_app(service=_service(FakeDatabase(), shadows=False), instrument=False)) as plain:
        b = plain.post("/decide", json={**body, "decision_id": str(uuid.uuid4())})
    assert a.status_code == b.status_code == 200
    a, b = a.json(), b.json()
    assert set(a) == set(b) == DECIDE_KEYS
    assert _without_latency({**a, "decision_id": None}) == _without_latency({**b, "decision_id": None})
    assert a["ruleset"] == {"id": "default", "version": 3} and a["mode"] == "enforce"


def test_the_shadow_decision_is_stored_beside_the_primary(tables):
    service = _service(FakeDatabase())
    body = _body()
    with TestClient(create_app(service=service, instrument=False)) as client:
        out = client.post("/decide", json=body).json()
        primary_id = uuid.UUID(body["decision_id"])
        shadow_id = uuid.uuid5(primary_id, "default-llm@3")
        wait_for(lambda: shadow_id in tables.decisions)
    primary, shadow = tables.decisions[primary_id], tables.decisions[shadow_id]
    assert primary.shadow_of is None and primary.response == out
    assert shadow.shadow_of == primary_id
    assert (shadow.ruleset_id, shadow.ruleset_version, shadow.mode, shadow.applied) == ("default-llm", 3, "shadow",
                                                                                       False)
    assert (shadow.chat_id, shadow.message_id, shadow.user_id, shadow.litellm_call_id) == (
        "chat-s", "msg-2", "u1", "call-1")
    assert shadow.response["decision_id"] == str(shadow_id) and shadow.response["ruleset"] == {
        "id": "default-llm", "version": 3}
    assert [e.evaluator_id for e in tables.evaluations[shadow_id]] == [
        "privacy", "code-text", "reasoning-text", "code-tools", "topic-llm"]
    topic = tables.evaluations[shadow_id][-1]
    assert topic.abstained and topic.reason == "no verdict in the last 2 message(s)"
    assert topic.detail["looked_up"] == 2 and topic.detail["lookup_error"] is None


def test_a_repeated_decide_writes_no_shadow_twice(tables):
    service = _service(FakeDatabase())
    body = _body()
    shadow_id = uuid.uuid5(uuid.UUID(body["decision_id"]), "default-llm@3")
    before = _sample("route_policy_shadow_decisions_total", ruleset="default-llm@3", agree="true")
    with TestClient(create_app(service=service, instrument=False)) as client:
        first = client.post("/decide", json=body).json()
        wait_for(lambda: shadow_id in tables.decisions)
        second = client.post("/decide", json=body).json()
        wait_for(lambda: service.pending_writes == 0)
    assert first == second
    assert len(tables.decisions) == 2
    assert _sample("route_policy_shadow_decisions_total", ruleset="default-llm@3", agree="true") == before + 1


# -- the analysis requests ------------------------------------------------------------------------


def test_requests_reach_the_queue_with_their_provenance(tables):
    queue = RecordingQueue()
    service = _service(FakeDatabase(), queue=queue)
    body = _body()
    with TestClient(create_app(service=service, instrument=False)) as client:
        client.post("/decide", json=body)
        wait_for(lambda: queue.submitted)
        wait_for(lambda: service.pending_writes == 0)
    (req,) = queue.submitted   # `default` has no llm_classifier; default-llm's topic-llm asks once
    shadow_id = uuid.uuid5(uuid.UUID(body["decision_id"]), "default-llm@3")
    assert (req.ruleset_id, req.ruleset_version, req.decision_id, req.evaluator_id) == (
        "default-llm", 3, shadow_id, "topic-llm")
    assert (req.chat_id, req.message_id, req.exchange_index, req.model) == ("chat-s", "msg-2", 2, "qwen3.5-4b")
    assert req.text == render_transcript(body["messages"], exchanges=1, max_chars=6000, upto_user=2)


def test_a_primary_with_an_llm_classifier_reads_its_verdicts_and_enqueues(tables):
    """A future `default` that enforces topic-llm: /decide reads the stored
    verdict (one query) and votes it; its own request goes to the queue."""
    (llm,) = [r for r in seed().compiled if r.id == "default-llm"]
    doc = {**copy.deepcopy(llm.document), "id": "enforced", "mode": "enforce"}
    enforced = compile_doc(doc)
    store = RulesetStore(None)
    store.add(enforced)
    store.set_bindings([Binding("auto", None, "enforced", enforced.version)])
    queue = RecordingQueue()
    service = Service(store=store, database=FakeDatabase(), rulesets_dir=RULESETS_DIR, analysis_queue=queue)
    messages = [user("hello"), assistant("hi"), user("tell me about the moon")]
    topic = enforced.entries[-1].evaluator
    k1 = input_key(topic.fingerprint, render_transcript(messages, exchanges=1, max_chars=6000, upto_user=1))
    tables.analyses[k1] = Verdict(k1, "ok", "local-reasoning", 0.8, {"local-reasoning": 8}, 1, "qwen3.5-4b")
    with TestClient(create_app(service=service, instrument=False)) as client:
        out = client.post("/decide", json=_body(messages=messages)).json()
        wait_for(lambda: queue.submitted)
    assert (out["route"], out["apply"]) == ("local-reasoning", True)
    assert out["votes"][-1]["reason"] == "local-reasoning scored 8/10 (qwen3.5-4b, 1 message(s) ago)"
    (req,) = queue.submitted
    assert req.ruleset_id == "enforced" and str(req.decision_id) == out["decision_id"]


# -- isolation ---------------------------------------------------------------------------------------


def test_a_failing_shadow_never_reaches_the_answer(tables, monkeypatch):
    service = _service(FakeDatabase())

    async def boom(resolved):
        raise RuntimeError("shadow loader bug")

    monkeypatch.setattr(service.store, "shadow_rulesets", boom)
    before = _sample("route_policy_errors_total", kind="shadow_error")
    with TestClient(create_app(service=service, instrument=False)) as client:
        r = client.post("/decide", json=_body())
        assert r.status_code == 200 and r.json()["route"] == "local-general"
        wait_for(lambda: service.pending_writes == 0)
    assert _sample("route_policy_errors_total", kind="shadow_error") == before + 1
    assert len(tables.decisions) == 1


def test_a_hung_database_delays_neither_the_answer_nor_shutdown_for_long(tables):
    never = asyncio.Event()
    service = _service(FakeDatabase(gate=never), write_budget_s=0.05, read_budget_s=0.05, drain_s=0.2)
    with TestClient(create_app(service=service, instrument=False)) as client:
        started = time.monotonic()
        r = client.post("/decide", json=_body())
        assert r.status_code == 200 and time.monotonic() - started < 0.4
        assert service.pending_writes == 2   # the primary's write + the after-the-answer task
    assert service.pending_writes == 0 and tables.decisions == {}


def test_past_the_task_cap_the_shadow_work_is_dropped(tables):
    async def go():
        never = asyncio.Event()
        service = _service(FakeDatabase(gate=never), write_budget_s=0, max_in_flight=1)
        from route_policy.api.schemas import DecideRequest
        from route_policy.engine import pipeline

        body = DecideRequest.model_validate(_body())
        resolved = await service.store.resolve("auto", None)
        ctx = body.context()
        decision = await pipeline.run(resolved.primary, ctx)
        await service.record_decision(body, decision, decision.response(str(body.decision_id)))  # takes the slot
        dropped = _sample("route_policy_errors_total", kind="shadow_dropped")
        assert service.after_decide(body, decision, resolved, ctx) is False
        assert _sample("route_policy_errors_total", kind="shadow_dropped") == dropped + 1
        await service.drain(timeout_s=0.05)

    asyncio.run(go())


def test_no_shadow_and_no_queue_means_no_background_task(tables):
    service = _service(FakeDatabase(), shadows=False)
    with TestClient(create_app(service=service, instrument=False)) as client:
        client.post("/decide", json=_body())
        assert service.pending_writes == 0


# -- /evaluate ------------------------------------------------------------------------------------------


def test_evaluate_reads_verdicts_and_never_enqueues(tables):
    queue = RecordingQueue()
    service = _service(FakeDatabase(), queue=queue)
    messages = [user("hello"), assistant("hi"), user("tell me about the moon")]
    (llm,) = [r for r in seed().compiled if r.id == "default-llm"]
    topic = llm.entries[-1].evaluator
    k1 = input_key(topic.fingerprint, render_transcript(messages, exchanges=1, max_chars=6000, upto_user=1))
    tables.analyses[k1] = Verdict(k1, "ok", "local-code", 0.9, {"local-code": 9}, 1, "qwen3.5-4b")
    with TestClient(create_app(service=service, instrument=False)) as client:
        out = client.post("/evaluate", json={"ruleset": {"id": "default-llm"}, "messages": messages}).json()
        unknown = client.post("/evaluate", json={"ruleset": {"id": "default-llm"},
                                                 "messages": [user("something else")]}).json()
    assert (out["route"], out["mode"], out["apply"]) == ("local-code", "shadow", False)
    vote = out["votes"][-1]
    assert vote["evaluator"] == "topic-llm" and vote["detail"]["input_key"] == k1
    assert unknown["route"] == "local-general" and unknown["votes"][-1]["abstained"]
    assert queue.submitted == []


def test_evaluate_without_a_database_abstains_with_the_reason():
    service = _service(None)
    with TestClient(create_app(service=service, instrument=False)) as client:
        out = client.post("/evaluate", json={"ruleset": {"id": "default-llm"}, "messages": [user("hi")]}).json()
    assert out["votes"][-1]["reason"] == "lookup unavailable (no database)"
