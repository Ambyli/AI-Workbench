"""The HTTP API with no database: the contract shapes, validation, the
read-only endpoints, and that a decision is returned even when it cannot be
logged. The database-backed behaviour is in test_db.py."""

from __future__ import annotations

import copy
import uuid

import pytest
from fastapi.testclient import TestClient

from rp_helpers import DECIDE_KEYS, RULESETS_DIR, VOTE_KEYS, dbless_app, default_ruleset, seed, tool_round, user

from route_policy.app import create_app
from route_policy.db.pool import Database
from route_policy.rulesets.store import RulesetStore
from route_policy.service import Service


@pytest.fixture
def client():
    with TestClient(dbless_app()) as c:
        yield c


def decide_body(**kw):
    body = {"decision_id": str(uuid.uuid4()), "requested_model": "auto", "litellm_call_id": "call-1",
            "chat_id": "chat-1", "message_id": None, "user_id": "u1", "key_alias": None,
            "messages": [user("go"), *tool_round("sandbox-mcp_exec")], "tools": None,
            "backends": {"http://qwen3.8-solo:8000/v1": {"verdict": "ok", "waiting": 0.0, "running": 1.0}}}
    body.update(kw)
    return body


# -- /decide -------------------------------------------------------------------------


def test_decide_returns_the_contract(client):
    body = decide_body()
    r = client.post("/decide", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert set(out) == DECIDE_KEYS and all(set(v) == VOTE_KEYS for v in out["votes"])
    assert out["decision_id"] == body["decision_id"]
    assert (out["route"], out["mode"], out["apply"]) == ("local-code", "shadow", False)
    assert out["ruleset"] == {"id": "default", "version": 1}


def test_decide_is_lenient_where_strictness_would_only_lose_data(client):
    body = decide_body(messages=None, tools=[{"type": "function"}], surprise="ignored",
                       backends={"http://x/v1": {"verdict": "melting", "waiting": None, "extra": 1}})
    r = client.post("/decide", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["route"] == "local-general"


@pytest.mark.parametrize("patch", [
    {"decision_id": "not-a-uuid"},
    {"decision_id": None},
    {"requested_model": ""},
    {"messages": "a string"},
    {"chat_id": "x" * 300},
])
def test_decide_rejects_what_breaks_the_contract(client, patch):
    assert client.post("/decide", json=decide_body(**patch)).status_code == 422


def test_decide_unbound_model_is_404(client):
    r = client.post("/decide", json=decide_body(requested_model="claude-opus-5-5"))
    assert r.status_code == 404 and "no ruleset is bound" in r.json()["detail"]


def test_decide_requested_private_stays_private(client):
    r = client.post("/decide", json=decide_body(requested_model="local-private"))
    # No binding for local-private in phase 2 -- the invariant is exercised
    # through /evaluate below and in test_invariants.py.
    assert r.status_code == 404


def test_decide_still_answers_when_the_database_is_down():
    s = seed()
    store = RulesetStore(None)
    for compiled in s.compiled:
        store.add(compiled)
    store.set_bindings(s.bindings)
    dead = Database("postgresql://nobody@127.0.0.1:1/none")
    app = create_app(service=Service(store=store, database=dead, rulesets_dir=RULESETS_DIR), instrument=False)
    with TestClient(app) as c:
        r = c.post("/decide", json=decide_body())
        assert r.status_code == 200 and r.json()["route"] == "local-code"
        assert c.post("/observe", json={"decision_id": str(uuid.uuid4()), "status": "success"}).status_code == 503
        assert c.get("/ready").status_code == 503
        assert c.get("/health").status_code == 200
        assert c.get("/rulesets").status_code == 503


# -- /observe ------------------------------------------------------------------------


def test_observe_without_a_database_is_503(client):
    r = client.post("/observe", json={"decision_id": str(uuid.uuid4()), "status": "success"})
    assert r.status_code == 503


def test_observe_validation(client):
    assert client.post("/observe", json={"decision_id": "nope"}).status_code == 422
    assert client.post("/observe", json={"decision_id": str(uuid.uuid4()), "status": ""}).status_code == 422


# -- /evaluate -----------------------------------------------------------------------


def test_evaluate_a_stored_ruleset(client):
    r = client.post("/evaluate", json={"ruleset": {"id": "default"}, "messages": [user("prove it")]})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["decision_id"] is None and out["route"] == "local-reasoning"
    assert all(set(v) == VOTE_KEYS | {"detail", "error"} for v in out["votes"])
    r = client.post("/evaluate", json={"ruleset": {"id": "default", "version": 1},
                                       "requested_model": "local-private", "messages": [user("prove it")]})
    assert r.json()["route"] == "local-private"


def test_evaluate_a_document(client):
    doc = copy.deepcopy(default_ruleset().document)
    doc["combiner"] = {"type": "weighted", "threshold": 1.5}
    r = client.post("/evaluate", json={"document": doc, "messages": [user("go"), *tool_round("run_command")]})
    assert r.status_code == 200, r.text
    assert r.json()["route"] == "local-code" and r.json()["scores"] == {"local-code": 2.0}
    # A document with refs resolves them inside the baked rulesets directory.
    raw = {"id": "draft", "version": 1, "mode": "shadow", "routes": ["local-general", "local-code"],
           "default_route": "local-general",
           "evaluators": [{"id": "code", "type": "regex_text", "config": {"patterns_ref": "patterns/code.yaml"},
                           "vote": {"route": "local-code"}}]}
    assert client.post("/evaluate", json={"document": raw, "messages": [user("fix app.py")]}).json()["route"] == \
        "local-code"


def test_evaluate_errors(client):
    assert client.post("/evaluate", json={"ruleset": {"id": "nope"}}).status_code == 404
    assert client.post("/evaluate", json={"ruleset": {"id": "default", "version": 9}}).status_code == 404
    assert client.post("/evaluate", json={"messages": []}).status_code == 422
    both = {"ruleset": {"id": "default"}, "document": {"id": "x"}}
    assert client.post("/evaluate", json=both).status_code == 422
    bad = client.post("/evaluate", json={"document": {"id": "x", "version": 1, "mode": "shadow",
                                                      "routes": ["a"], "default_route": "b"}})
    assert bad.status_code == 422 and "default_route" in bad.json()["detail"]["errors"][0]
    escape = {"id": "x", "version": 1, "mode": "shadow", "routes": ["a"], "default_route": "a",
              "evaluators": [{"id": "e", "type": "regex_text", "config": {"patterns_ref": "../docker-compose.route-policy.yml"},
                              "vote": {"route": "a"}}]}
    r = client.post("/evaluate", json={"document": escape})
    assert r.status_code == 422 and "inside the rulesets directory" in r.json()["detail"]["errors"][0]


# -- read-only endpoints -----------------------------------------------------------------


def test_health_ready_and_types(client):
    assert client.get("/health").json() == {"status": "ok", "rulesets": [{"id": "default", "version": 1}],
                                            "bindings": 1}
    assert client.get("/ready").status_code == 503
    types = client.get("/evaluator-types").json()
    assert set(types["evaluator_types"]) == {"regex_text", "tool_calls", "cooldown", "backend_load", "static"}
    assert set(types["combiner_types"]) == {"priority", "weighted"}
    assert types["roles"] == ["vote", "force", "veto"]
    tool_calls = types["evaluator_types"]["tool_calls"]
    assert tool_calls["config_schema"]["required"] == ["pattern"]
    assert tool_calls["output"]["relevance"]


def test_database_reads_without_a_database_are_503(client):
    for path in ("/rulesets", "/rulesets/default", "/rulesets/default/1", "/bindings", "/chats/c/decisions"):
        assert client.get(path).status_code == 503, path


def test_metrics_are_exposed():
    from rp_helpers import seed as load

    s = load()
    store = RulesetStore(None)
    for compiled in s.compiled:
        store.add(compiled)
    store.set_bindings(s.bindings)
    app = create_app(service=Service(store=store, database=None, rulesets_dir=RULESETS_DIR))
    with TestClient(app) as c:
        c.post("/decide", json=decide_body())
        text = c.get("/metrics").text
    for name in ("route_policy_decisions_total", "route_policy_decide_seconds", "route_policy_evaluator_seconds",
                 "route_policy_votes_total", "route_policy_errors_total"):
        assert name in text, name
    assert 'route_policy_decisions_total{applied="false",mode="shadow",route="local-code"}' in text
