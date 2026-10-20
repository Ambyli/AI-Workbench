"""One route per turn (``pin: turn``) and Open WebUI background tasks,
through /decide with the service's tables in memory
(``rp_helpers.memory_tables``):

* the calls of one turn (same chat id + message id) -- its first call, a
  tool round, Open WebUI's hidden auto-continue nudge -- all get the first
  call's route; every later call stores ``pinned_from`` = the first; the
  shadow ruleset pins too, from its OWN rows; only the turn's first call
  asks for a classification;
* privacy beats the pin, and a private turn stays private; no pin (and no
  lookup) without a message id; a new message id is a new turn;
* a pin read that is cut or fails leaves the call decided afresh, in time,
  and counted;
* a background task is stored with ``background_task``, is never classified
  (its topic-llm abstains, nothing is enqueued), and never pins a turn nor
  is pinned to one;
* route_policy_pinned_total.

The SQL side (the lookup query, the new columns, migration 0003) is
test_db.py's; the pin itself, as a pure function of the context, is
test_pipeline.py's and test_default_ruleset.py's.
"""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from rp_helpers import (
    DECIDE_KEYS,
    NUDGE,
    RULESETS_DIR,
    TITLE_TASK,
    GatedDatabase,
    RecordingQueue,
    assistant,
    memory_tables,
    seed,
    strip_shadows,
    tool_round,
    user,
    wait_for,
)

from route_policy.app import create_app
from route_policy.db.repositories import decisions as decisions_repo
from route_policy.rulesets.store import RulesetStore
from route_policy.service import Service, shadow_decision_id

# The 2026-10 chat: a calculator, a joke, then the weather. The weather
# turn's first call is still on sticky-code's 2-exchange cooldown
# (local-code); its tool round is the same exchange; the nudge is a new user
# message, so on its own it would expire the cooldown (local-general).
CALCULATOR = [user("Make me a scientific calculator!"), assistant("done"), user("tell me a joke"),
              assistant("ha")]
FIRST_CALL = CALCULATOR + [user("check the weather in Utah")]
TOOL_ROUND = FIRST_CALL + tool_round("search_web")
NUDGED = TOOL_ROUND + [assistant("I'll look it up again."), user(NUDGE)]


@pytest.fixture
def tables(monkeypatch):
    return memory_tables(monkeypatch)


def _llm():
    (llm,) = [r for r in seed().compiled if r.id == "default-llm"]
    return llm


def _service(database=None, *, shadows: bool = True, queue=None, **kw) -> Service:
    s = seed()
    store = RulesetStore(None)
    for compiled in s.compiled:
        store.add(compiled)
    store.set_bindings(s.bindings if shadows else strip_shadows(s.bindings))
    return Service(store=store, database=database, rulesets_dir=RULESETS_DIR, analysis_queue=queue, **kw)


def _sample(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _decide(client, tables, messages, *, message_id="msg-weather", shadow=True, **kw):
    """POST /decide; wait until its rows (and its shadow's) are in the table."""
    body = {"decision_id": str(uuid.uuid4()), "requested_model": "auto", "litellm_call_id": "call-1",
            "chat_id": "chat-t", "message_id": message_id, "user_id": "u1", "messages": messages}
    body.update(kw)
    r = client.post("/decide", json=body)
    assert r.status_code == 200, r.text
    did = uuid.UUID(body["decision_id"])
    wait_for(lambda: did in tables.decisions)
    if shadow:
        wait_for(lambda: shadow_decision_id(did, _llm()) in tables.decisions)
    return did, r.json()


# -- one route per turn -------------------------------------------------------------------------


def test_the_calls_of_one_turn_keep_its_first_route(tables):
    queue = RecordingQueue()
    service = _service(GatedDatabase(), queue=queue)
    kept = _sample("route_policy_pinned_total", ruleset="default@3", outcome="kept")
    changed = _sample("route_policy_pinned_total", ruleset="default@3", outcome="changed")
    shadow_kept = _sample("route_policy_pinned_total", ruleset="default-llm@2", outcome="kept")
    with TestClient(create_app(service=service, instrument=False)) as client:
        calls = [_decide(client, tables, messages) for messages in (FIRST_CALL, TOOL_ROUND, NUDGED)]
        wait_for(lambda: service.pending_writes == 0)
    ids = [did for did, _ in calls]
    for _, out in calls:
        assert set(out) == DECIDE_KEYS and (out["route"], out["apply"]) == ("local-code", True)
    first, tool, nudged = (tables.decisions[i] for i in ids)
    assert first.pinned_from is None and tool.pinned_from == nudged.pinned_from == ids[0]
    assert first.reason.startswith("sticky-code: ") and tool.reason == first.reason   # kept: the fresh reason
    assert nudged.reason.startswith(f"pinned to this turn's first decision (local-code, {ids[0]}); "
                                    f"fresh: local-general — ")
    assert [v["evaluator"] for v in calls[2][1]["votes"]][-1] == "sticky-code"   # every vote still logged
    assert calls[2][1]["votes"][-1]["abstained"] is True                        # ... the fresh one
    assert _sample("route_policy_pinned_total", ruleset="default@3", outcome="kept") == kept + 1
    assert _sample("route_policy_pinned_total", ruleset="default@3", outcome="changed") == changed + 1
    # The shadow pins from its OWN rows: held to the first shadow decision,
    # not to the primary's local-code.
    shadows = [tables.decisions[shadow_decision_id(i, _llm())] for i in ids]
    assert [s.chosen_route for s in shadows] == ["local-general"] * 3
    assert shadows[0].pinned_from is None
    assert shadows[1].pinned_from == shadows[2].pinned_from == shadows[0].decision_id
    assert _sample("route_policy_pinned_total", ruleset="default-llm@2", outcome="kept") == shadow_kept + 2
    # One classification for the turn: the first shadow decision's. The tool
    # round's prefix was the same; the nudge's (a new "user message") is
    # never asked for.
    assert [r.decision_id for r in queue.submitted] == [shadows[0].decision_id]
    assert queue.submitted[0].exchange_index == 3


def test_a_repeated_decide_is_not_pinned_to_itself(tables):
    service = _service(GatedDatabase(), shadows=False)
    body = {"decision_id": str(uuid.uuid4()), "requested_model": "auto", "chat_id": "chat-t",
            "message_id": "msg-repeat", "messages": FIRST_CALL}
    with TestClient(create_app(service=service, instrument=False)) as client:
        first = client.post("/decide", json=body).json()
        again = client.post("/decide", json=body).json()
    assert again == first
    assert tables.decisions[uuid.UUID(body["decision_id"])].pinned_from is None


def test_privacy_beats_the_pin_and_a_private_turn_stays_private(tables):
    private = _sample("route_policy_pinned_total", ruleset="default@3", outcome="private")
    with TestClient(create_app(service=_service(GatedDatabase(), shadows=False), instrument=False)) as client:
        first, out = _decide(client, tables, [user("hello")], message_id="msg-a", shadow=False)
        assert out["route"] == "local-general"
        pii, out = _decide(client, tables, [user("hello"), *tool_round("search_web"),
                                            user("my SSN is 123-45-6789")], message_id="msg-a", shadow=False)
        assert (out["route"], out["apply"]) == ("local-private", True)
        # A turn whose first call was private stays private through a nudge
        # that names no PII.
        start, out = _decide(client, tables, [user("my SSN is 123-45-6789, file my taxes")], message_id="msg-b",
                             shadow=False)
        assert out["route"] == "local-private"
        nudge, out = _decide(client, tables, [user("my SSN is 123-45-6789, file my taxes"), assistant("I'll file it."),
                                              user(NUDGE)], message_id="msg-b", shadow=False)
        assert out["route"] == "local-private"
    assert tables.decisions[pii].pinned_from == first
    assert tables.decisions[pii].reason.endswith(f"; local-private beats this turn's first decision "
                                                 f"(local-general, {first})")
    assert tables.decisions[nudge].pinned_from == start
    assert tables.decisions[nudge].reason.startswith(f"pinned to this turn's first decision (local-private, {start})")
    assert _sample("route_policy_pinned_total", ruleset="default@3", outcome="private") == private + 1


def test_no_pin_without_a_message_id_and_a_new_message_is_a_new_turn(tables, monkeypatch):
    lookups = []
    original = decisions_repo.first_of_turn

    async def counted(conn, **kw):
        lookups.append(kw["message_id"])
        return await original(conn, **kw)

    monkeypatch.setattr(decisions_repo, "first_of_turn", counted)
    with TestClient(create_app(service=_service(GatedDatabase(), shadows=False), instrument=False)) as client:
        a, out_a = _decide(client, tables, FIRST_CALL, message_id=None, shadow=False)
        b, out_b = _decide(client, tables, NUDGED, message_id=None, shadow=False)
        c, out_c = _decide(client, tables, FIRST_CALL, message_id="msg-1", shadow=False)
        d, out_d = _decide(client, tables, NUDGED, message_id="msg-2", shadow=False)
    assert (out_a["route"], out_b["route"], out_c["route"], out_d["route"]) == (
        "local-code", "local-general", "local-code", "local-general")
    assert all(tables.decisions[i].pinned_from is None for i in (a, b, c, d))
    assert lookups == ["msg-1", "msg-2"]   # none without a message id


def test_a_cut_or_failed_pin_read_decides_afresh_in_time(tables, monkeypatch):
    async def hangs(conn, **kw):
        await asyncio.Event().wait()

    monkeypatch.setattr(decisions_repo, "first_of_turn", hangs)
    service = _service(GatedDatabase(), shadows=False, read_budget_s=0.05)
    cut = _sample("route_policy_db_reads_cut_total", read="turn_pin")
    with TestClient(create_app(service=service, instrument=False)) as client:
        _decide(client, tables, FIRST_CALL, shadow=False)
        started = time.monotonic()
        nudged, out = _decide(client, tables, NUDGED, shadow=False)
        assert time.monotonic() - started < 0.4
    assert out["route"] == "local-general" and tables.decisions[nudged].pinned_from is None
    assert _sample("route_policy_db_reads_cut_total", read="turn_pin") == cut + 2

    async def fails(conn, **kw):
        raise ConnectionError("route-policy-db is down")

    monkeypatch.setattr(decisions_repo, "first_of_turn", fails)
    errors = _sample("route_policy_errors_total", kind="turn_pin")
    with TestClient(create_app(service=_service(GatedDatabase(), shadows=False), instrument=False)) as client:
        nudged, out = _decide(client, tables, NUDGED, message_id="msg-fail", shadow=False)
    assert out["route"] == "local-general" and tables.decisions[nudged].pinned_from is None
    assert _sample("route_policy_errors_total", kind="turn_pin") == errors + 1


# -- background tasks -------------------------------------------------------------------------------


def test_a_background_task_is_logged_never_classified_and_never_pins(tables):
    queue = RecordingQueue()
    service = _service(GatedDatabase(), queue=queue)
    with TestClient(create_app(service=service, instrument=False)) as client:
        # A task under the very message id of the chat turn that follows.
        task, _ = _decide(client, tables, CALCULATOR + [user(TITLE_TASK)], background_task=True)
        chat, out = _decide(client, tables, NUDGED)
        later_task, _ = _decide(client, tables, CALCULATOR + [user(TITLE_TASK)], background_task=True)
        wait_for(lambda: service.pending_writes == 0)
    # The chat call is not pinned to the task before it (fresh: the nudge
    # expired the cooldown), and the task after it is not pinned to the chat.
    assert out["route"] == "local-general" and tables.decisions[chat].pinned_from is None
    assert tables.decisions[later_task].pinned_from is None
    assert [tables.decisions[i].background_task for i in (task, chat, later_task)] == [True, False, True]
    llm = _llm()
    task_shadow = tables.decisions[shadow_decision_id(task, llm)]
    assert task_shadow.background_task is True
    topic = tables.evaluations[task_shadow.decision_id][-1]
    assert (topic.evaluator_id, topic.abstained, topic.reason) == ("topic-llm", True, "background task")
    assert topic.detail["looked_up"] == 0
    # Only the chat call asked for a classification.
    assert [r.decision_id for r in queue.submitted] == [shadow_decision_id(chat, llm)]


def test_background_task_defaults_to_false_and_accepts_null(tables):
    with TestClient(create_app(service=_service(GatedDatabase(), shadows=False), instrument=False)) as client:
        old_hook, _ = _decide(client, tables, [user("hi")], message_id=None, shadow=False)
        null, _ = _decide(client, tables, [user("hi")], message_id=None, shadow=False, background_task=None)
        r = client.post("/decide", json={"decision_id": str(uuid.uuid4()), "requested_model": "auto",
                                         "messages": [user("hi")], "background_task": "maybe"})
        assert r.status_code == 422
    assert tables.decisions[old_hook].background_task is False and tables.decisions[null].background_task is False
