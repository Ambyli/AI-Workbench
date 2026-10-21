"""Against a real Postgres (TEST_POSTGRES_DSN): startup (migrations, seeding,
bindings and their shadow slots), idempotent /decide and /observe in either
order, shadow decisions and the views over them (decision_outcomes over
primaries, shadow_comparisons), the analyses table, the whole classifier
loop on a fake classifier, the read endpoints, the immutability guards, and
0003: one route per turn (the lookup on real rows, ``pinned_from``, one
classification per turn), ``background_task``, the upgrade from phase 4.

Every test here is marked ``postgres`` and skipped without the DSN.
"""

from __future__ import annotations

import asyncio
import pathlib
import shutil
import uuid

import asyncpg
import pytest
from fastapi.testclient import TestClient

from rp_helpers import (
    DECIDE_KEYS,
    NUDGE,
    RULESETS_DIR,
    TITLE_TASK,
    FakeClassifier,
    assistant,
    tool_round,
    user,
    wait_for,
)

from route_policy import config
from route_policy.app import create_app
from route_policy.db import migrations as migrations_mod
from route_policy.db.pool import Database, _init_connection
from route_policy.db.repositories import analyses as analyses_repo
from route_policy.db.repositories.rulesets import SeedConflict

pytestmark = pytest.mark.postgres


def _client(dsn: str = None, rulesets_dir: pathlib.Path = RULESETS_DIR, classifier: FakeClassifier = None,
            **client_kw) -> TestClient:
    database = Database(dsn or config.DB_DSN, max_size=4)
    client = classifier.client(**client_kw) if classifier is not None else None
    return TestClient(create_app(database=database, rulesets_dir=rulesets_dir, instrument=False,
                                 analysis_client=client))


def _shadow_id(primary_id, ruleset: str = "default-llm@3") -> uuid.UUID:
    return uuid.uuid5(uuid.UUID(str(primary_id)), ruleset)


# `auto`'s shadow slots, in order: the phase-4 and phase-5 drafts.
SHADOWS = [{"ruleset_id": "default-llm", "version": 3}, {"ruleset_id": "default-signals", "version": 1}]


def _wait_rows(dsn: str, sql: str, *args, n: int = 1):
    """Poll until the query returns at least ``n`` rows (a background write)."""
    return wait_for(lambda: (lambda rows: rows if len(rows) >= n else None)(_fetch(dsn, sql, *args)))


def _fetch(dsn: str, sql: str, *args):
    async def go():
        conn = await asyncpg.connect(dsn)
        try:
            await _init_connection(conn)  # json / jsonb as Python objects, like the service
            return [dict(r) for r in await conn.fetch(sql, *args)]
        finally:
            await conn.close()

    return asyncio.run(go())


def _decide(client, chat="chat-db", decision_id=None, messages=None, **kw):
    body = {"decision_id": decision_id or str(uuid.uuid4()), "requested_model": "auto",
            "litellm_call_id": f"call-{uuid.uuid4().hex[:6]}", "chat_id": chat, "user_id": "u1",
            "messages": messages if messages is not None else [user("go"), *tool_round("sandbox-mcp_exec")],
            "backends": {"http://qwen3.8-solo:8000/v1": {"verdict": "busy", "waiting": 2.0, "running": 3.0}}}
    body.update(kw)
    r = client.post("/decide", json=body)
    assert r.status_code == 200, r.text
    return body, r.json()


def _copy_rulesets(tmp_path: pathlib.Path, name: str = "rulesets") -> pathlib.Path:
    target = tmp_path / name
    shutil.copytree(RULESETS_DIR, target)
    return target


def _rewrite(path: pathlib.Path, *pairs) -> None:
    text = path.read_text(encoding="utf-8")
    for old, new in pairs:
        assert old in text, old
        text = text.replace(old, new)
    path.write_text(text, encoding="utf-8")


# -- startup ----------------------------------------------------------------------


def test_startup_migrates_seeds_and_binds(fresh_database):
    with _client(fresh_database) as c:
        assert c.get("/ready").json() == {"status": "ready", "db": "up"}
    tables = {r["table_name"] for r in _fetch(
        fresh_database, "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")}
    assert {"rulesets", "bindings", "binding_shadows", "decisions", "evaluations", "outcomes", "analyses",
            "decision_outcomes", "shadow_comparisons", "schema_migrations"} <= tables
    rows = _fetch(fresh_database, "SELECT id, version, created_by, source, active_from, active_to FROM rulesets "
                                  "ORDER BY id")
    assert [(r["id"], r["version"], r["created_by"], r["source"]) for r in rows] == [
        ("default", 3, "seed", "rulesets/default.yaml"), ("default-llm", 3, "seed", "rulesets/default-llm.yaml"),
        ("default-signals", 1, "seed", "rulesets/default-signals.yaml")]
    # All three are active: one as the primary, two in shadow slots.
    assert all(r["active_from"] is not None and r["active_to"] is None for r in rows)
    (b,) = _fetch(fresh_database, "SELECT id, scope_model, scope_key_alias, ruleset_id, version FROM bindings")
    assert {k: v for k, v in b.items() if k != "id"} == {"scope_model": "auto", "scope_key_alias": None,
                                                         "ruleset_id": "default", "version": 3}
    slots = _fetch(fresh_database, "SELECT binding_id, position, ruleset_id, version FROM binding_shadows "
                                   "ORDER BY position")
    assert slots == [{"binding_id": b["id"], "position": 0, "ruleset_id": "default-llm", "version": 3},
                     {"binding_id": b["id"], "position": 1, "ruleset_id": "default-signals", "version": 1}]
    # The example set is inlined into the stored document, never read from the file again.
    (signals,) = _fetch(fresh_database, "SELECT document FROM rulesets WHERE id = 'default-signals'")
    (topic,) = [e for e in signals["document"]["evaluators"] if e["id"] == "topic"]
    assert topic["config"]["examples"]["name"] == "routes_v1"
    # A second start changes nothing.
    with _client(fresh_database) as c:
        assert c.get("/health").status_code == 200
    assert len(_fetch(fresh_database, "SELECT * FROM rulesets")) == 3
    assert len(_fetch(fresh_database, "SELECT * FROM binding_shadows")) == 2
    assert len(_fetch(fresh_database, "SELECT * FROM schema_migrations")) == len(migrations_mod.migrations())


def test_shadow_slots_follow_the_bindings_file(fresh_database, tmp_path):
    with _client(fresh_database):
        pass
    unshadowed = _copy_rulesets(tmp_path)
    _rewrite(unshadowed / "bindings.yaml", ("  shadow: [default-llm, default-signals]\n", ""))
    with _client(fresh_database, unshadowed) as c:
        (b,) = c.get("/bindings").json()["bindings"]
        assert b["shadows"] == []
        body, _ = _decide(c, chat="chat-unshadowed")
        assert c.get("/chats/chat-unshadowed/decisions").json()["decisions"][0]["shadows"] == []
    assert _fetch(fresh_database, "SELECT * FROM binding_shadows") == []
    (llm,) = _fetch(fresh_database, "SELECT active_to FROM rulesets WHERE id = 'default-llm'")
    assert llm["active_to"] is not None   # no slot points at it any more
    with _client(fresh_database) as c:
        (b,) = c.get("/bindings").json()["bindings"]
        assert b["shadows"] == SHADOWS
    (llm,) = _fetch(fresh_database, "SELECT active_to FROM rulesets WHERE id = 'default-llm'")
    assert llm["active_to"] is None


def test_a_changed_ruleset_without_a_version_bump_stops_startup(fresh_database, tmp_path):
    with _client(fresh_database):
        pass
    edited = _copy_rulesets(tmp_path)
    _rewrite(edited / "default.yaml", ("exchanges: 2", "exchanges: 4"))
    with pytest.raises(SeedConflict, match="default@3"):
        with _client(fresh_database, edited):
            pass


def test_a_version_bump_moves_the_binding(fresh_database, tmp_path):
    with _client(fresh_database):
        pass
    edited = _copy_rulesets(tmp_path)
    _rewrite(edited / "default.yaml", ("version: 3", "version: 4"), ("exchanges: 2", "exchanges: 4"))
    with _client(fresh_database, edited) as c:
        _, out = _decide(c)
        assert out["ruleset"] == {"id": "default", "version": 4}
        versions = [v["version"] for v in c.get("/rulesets/default").json()["versions"]]
        assert versions == [3, 4]
    marks = {(r["id"], r["version"]): r for r in _fetch(
        fresh_database, "SELECT id, version, active_from, active_to FROM rulesets")}
    assert marks[("default", 3)]["active_to"] is not None and marks[("default", 4)]["active_to"] is None


def test_the_phase_3_upgrade_keeps_the_old_version_readable(fresh_database, tmp_path):
    """The box's path: a database holding a shadow `default@1` bound to
    `auto` (phase 2), then this image, whose only baked-in `default` is
    version 2 (enforce). The binding moves to 2; version 1 stays stored with
    its decisions, and is still readable and evaluable although no file
    holds it any more. (The phase-2 state is made by hand: today's seed
    refuses a shadow ruleset as a binding's primary.)"""
    phase2 = _copy_rulesets(tmp_path, "phase2")
    _rewrite(phase2 / "default.yaml", ("version: 3", "version: 1"), ("mode: enforce", "mode: shadow"))
    (phase2 / "bindings.yaml").unlink()
    (phase2 / "default-llm.yaml").unlink()
    (phase2 / "default-signals.yaml").unlink()
    with _client(fresh_database, phase2) as c:
        _fetch(fresh_database, "INSERT INTO bindings (scope_model, ruleset_id, version) "
                               "VALUES ('auto', 'default', 1) RETURNING id")
        _, out = _decide(c, chat="chat-upgrade")
        assert (out["ruleset"], out["mode"], out["apply"]) == ({"id": "default", "version": 1}, "shadow", False)
    with _client(fresh_database) as c:
        _, out = _decide(c, chat="chat-upgrade")
        assert (out["ruleset"], out["mode"], out["apply"]) == ({"id": "default", "version": 3}, "enforce", True)
        assert c.get("/health").json()["rulesets"] == [{"id": "default", "version": 3},
                                                       {"id": "default-llm", "version": 3},
                                                       {"id": "default-signals", "version": 1}]
        assert [v["version"] for v in c.get("/rulesets/default").json()["versions"]] == [1, 3]
        assert c.get("/rulesets/default/1").json()["document"]["mode"] == "shadow"
        old = c.post("/evaluate", json={"ruleset": {"id": "default", "version": 1},
                                        "messages": [user("fix app.py")]})
        assert old.status_code == 200 and (old.json()["mode"], old.json()["route"]) == ("shadow", "local-code")
        (b,) = c.get("/bindings").json()["bindings"]
        assert b["version"] == 3 and b["shadows"] == SHADOWS
        decisions = c.get("/chats/chat-upgrade/decisions").json()["decisions"]
        assert [(d["ruleset"]["version"], d["applied"]) for d in decisions] == [(1, False), (3, True)]


def test_migrations_are_recorded_and_immutable(fresh_database, tmp_path):
    async def go(directory):
        conn = await asyncpg.connect(fresh_database)
        try:
            return await migrations_mod.migrate(conn, directory)
        finally:
            await conn.close()

    assert asyncio.run(go(migrations_mod.MIGRATIONS_DIR)) == ["0001_initial.sql", "0002_llm_classifier.sql",
                                                              "0003_turn_pins.sql"]
    assert asyncio.run(go(migrations_mod.MIGRATIONS_DIR)) == []
    changed = tmp_path / "migrations"
    changed.mkdir()
    (changed / "0001_initial.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(migrations_mod.MigrationChanged):
        asyncio.run(go(changed))


# -- /decide + /observe ---------------------------------------------------------------


def test_decide_stores_the_decision_and_its_evaluations():
    with _client() as c:
        body, out = _decide(c, chat="chat-store")
    assert set(out) == DECIDE_KEYS
    did = uuid.UUID(body["decision_id"])
    (d,) = _fetch(config.DB_DSN, "SELECT * FROM decisions WHERE decision_id = $1", did)
    assert (d["chat_id"], d["requested_model"], d["chosen_route"], d["applied"], d["mode"]) == \
        ("chat-store", "auto", "local-code", True, "enforce")
    assert (d["ruleset_id"], d["ruleset_version"], d["message_count"], d["tool_count"]) == ("default", 3, 3, 0)
    assert d["pinned_from"] is None and d["background_task"] is False
    assert d["scores"] == {"local-code": 2.0} and d["response"] == out and d["error"] is None
    assert d["backend_snapshot"] == {"http://qwen3.8-solo:8000/v1": {"verdict": "busy", "waiting": 2.0,
                                                                     "running": 3.0}}
    assert d["reason"] == "code-tools: called sandbox-mcp_exec in this exchange"
    rows = _fetch(config.DB_DSN, "SELECT * FROM evaluations WHERE decision_id = $1 ORDER BY position", did)
    assert [r["evaluator_id"] for r in rows] == ["privacy", "code-text", "reasoning-text", "code-tools",
                                                 "sticky-code"]
    assert rows[3]["vote_route"] == "local-code" and rows[3]["abstained"] is False
    assert rows[3]["detail"]["matched"] == "sandbox-mcp_exec"
    assert rows[0]["vote_route"] is None and rows[0]["abstained"] is True and rows[0]["confidence"] is None
    # No message text anywhere in the stored row.
    assert "go" not in str(d["response"]["votes"][0]["reason"])


def test_a_duplicate_decision_id_replays_the_first_decision():
    with _client() as c:
        body, first = _decide(c, chat="chat-dup")
        _, second = _decide(c, chat="chat-dup", decision_id=body["decision_id"], messages=[user("prove it")])
    assert second == first and second["route"] == "local-code"
    rows = _fetch(config.DB_DSN, "SELECT count(*) AS n FROM evaluations WHERE decision_id = $1",
                  uuid.UUID(body["decision_id"]))
    assert rows[0]["n"] == 5


def test_observe_before_decide_and_the_view():
    did = str(uuid.uuid4())
    with _client() as c:
        r = c.post("/observe", json={"decision_id": did, "litellm_call_id": "call-x", "response_id": "chatcmpl-1",
                                     "served_model_group": "local-code", "served_deployment": "openai/qwen3.8-solo",
                                     "overflow_reason": None, "finish_reason": "stop",
                                     "tool_calls": ["sandbox-mcp_exec"], "status": "success", "error": None})
        assert r.status_code == 202 and r.json() == {"decision_id": did, "stored": True}
        again = c.post("/observe", json={"decision_id": did, "status": "failure"})
        assert again.status_code == 202 and again.json()["stored"] is False
        (row,) = _fetch(config.DB_DSN, "SELECT * FROM decision_outcomes WHERE decision_id = $1", uuid.UUID(did))
        assert row["chosen_route"] is None and row["served_model_group"] == "local-code"  # outcome alone
        _decide(c, chat="chat-view", decision_id=did)
    (row,) = _fetch(config.DB_DSN, "SELECT * FROM decision_outcomes WHERE decision_id = $1", uuid.UUID(did))
    assert row["agree"] is True and row["response_id"] == "chatcmpl-1" and row["status"] == "success"
    assert row["tool_calls"] == ["sandbox-mcp_exec"] and row["chat_id"] == "chat-view"
    assert row["litellm_call_id"] != "call-x"  # the decision's own call id wins in the view


def test_success_query_shape():
    """The agreement query (ROUTE_POLICY_PLAN § 7) runs as written."""
    with _client() as c:
        body, _ = _decide(c, chat="chat-q")
        c.post("/observe", json={"decision_id": body["decision_id"], "served_model_group": "local-general",
                                 "status": "success"})
    rows = _fetch(config.DB_DSN, "SELECT d.chosen_route = o.served_model_group AS agree, count(*) AS n "
                                 "FROM decisions d JOIN outcomes o USING (decision_id) "
                                 "WHERE d.chat_id = 'chat-q' GROUP BY 1")
    assert rows == [{"agree": False, "n": 1}]


# -- read endpoints --------------------------------------------------------------------


def test_chat_decisions_oldest_first_with_outcomes():
    chat = f"chat-{uuid.uuid4().hex[:8]}"
    with _client() as c:
        first, _ = _decide(c, chat=chat, messages=[user("hello")])
        second, _ = _decide(c, chat=chat, messages=[user("hello"), assistant(), user("write python")])
        c.post("/observe", json={"decision_id": second["decision_id"], "served_model_group": "local-code",
                                 "status": "success"})
        out = c.get(f"/chats/{chat}/decisions").json()
        assert [d["decision_id"] for d in out["decisions"]] == [first["decision_id"], second["decision_id"]]
        assert out["decisions"][0]["outcome"] is None
        assert out["decisions"][1]["outcome"]["agree"] is True and out["decisions"][1]["route"] == "local-code"
        assert len(out["decisions"][1]["votes"]) == 5 and out["decisions"][1]["shadow_of"] is None
        # Each primary carries its shadows, written after the answer.
        _wait_rows(config.DB_DSN, "SELECT 1 FROM decisions WHERE shadow_of = $1",
                   uuid.UUID(second["decision_id"]), n=2)
        shadows = {d["ruleset"]["id"]: d for d in
                   c.get(f"/chats/{chat}/decisions").json()["decisions"][1]["shadows"]}
        assert set(shadows) == {"default-llm", "default-signals"}
        shadow = shadows["default-llm"]
        assert shadow["shadow_of"] == second["decision_id"] and shadow["outcome"] is None
        assert shadow["decision_id"] == str(_shadow_id(second["decision_id"])) and "shadows" not in shadow
        assert shadow["ruleset"] == {"id": "default-llm", "version": 3} and shadow["mode"] == "shadow"
        assert len(shadow["votes"]) == 5
        signals = shadows["default-signals"]
        assert signals["decision_id"] == str(_shadow_id(second["decision_id"], "default-signals@1"))
        assert len(signals["votes"]) == 11 and signals["route"] == "local-code"
        limited = c.get(f"/chats/{chat}/decisions", params={"limit": 1}).json()["decisions"]
        assert [d["decision_id"] for d in limited] == [second["decision_id"]]
        assert c.get("/chats/nobody/decisions").json() == {"chat_id": "nobody", "decisions": []}
        assert c.get(f"/chats/{chat}/decisions", params={"limit": 0}).status_code == 422


def test_ruleset_and_binding_reads():
    with _client() as c:
        listed = c.get("/rulesets").json()["rulesets"]
        assert [(r["id"], r["version"], r["mode"], r["pin"]) for r in listed] == [
            ("default", 3, "enforce", "turn"), ("default-llm", 3, "shadow", "turn"),
            ("default-signals", 1, "shadow", "turn")]
        assert "document" not in listed[0] and len(listed[0]["sha256"]) == 64
        latest = c.get("/rulesets/default").json()
        assert latest["version"] == 3 and latest["document"]["id"] == "default" and latest["pin"] == "turn"
        assert [v["version"] for v in latest["versions"]] == [3]
        assert c.get("/rulesets/default/3").json()["document"]["evaluators"][0]["id"] == "privacy"
        assert c.get("/rulesets/default/2").status_code == 404
        assert c.get("/rulesets/default/4").status_code == 404
        assert c.get("/rulesets/nope").status_code == 404
        assert c.get("/rulesets/default/0").status_code == 422
        (b,) = c.get("/bindings").json()["bindings"]
        assert (b["scope_model"], b["scope_key_alias"], b["ruleset_id"], b["version"]) == ("auto", None, "default", 3)
        assert b["shadows"] == SHADOWS
        # A stored document posts straight back to /evaluate.
        doc = c.get("/rulesets/default/3").json()["document"]
        assert c.post("/evaluate", json={"document": doc, "messages": [user("prove it")]}).json()["route"] == \
            "local-reasoning"


def test_startup_warms_the_bound_example_sets_and_shadows_reach_the_sidecar(fresh_database):
    """The real startup path (seed, bind, warm) with a signals client on a
    fake sidecar: one warm-up for `default-signals`' example set before the
    first request, then each /decide's shadow makes its own call."""
    from rp_helpers import FakeSidecar

    sidecar = FakeSidecar(status={"nearest": "warming"})
    database = Database(fresh_database, max_size=4)
    app = create_app(database=database, rulesets_dir=RULESETS_DIR, instrument=False,
                     signals_client=sidecar.client())
    with TestClient(app) as c:
        (warm,) = sidecar.requests
        assert warm["request_id"] == "warmup" and [t["task"] for t in warm["tasks"]] == ["nearest"]
        assert warm["tasks"][0]["texts"] == []
        body, _ = _decide(c, chat="chat-warm", messages=[user("my SSN is 523-45-6789")])
        shadow_id = _shadow_id(body["decision_id"], "default-signals@1")
        wait_for(lambda: len(sidecar.requests) == 2)
        wait_for(lambda: c.app.state.service.pending_writes == 0)
    assert sidecar.requests[1]["request_id"] == str(shadow_id)
    (row,) = _fetch(fresh_database, "SELECT chosen_route FROM decisions WHERE decision_id = $1", shadow_id)
    assert row["chosen_route"] == "local-private"
    topic = _fetch(fresh_database, "SELECT reason, detail FROM evaluations WHERE decision_id = $1 "
                                   "AND evaluator_id = 'topic'", shadow_id)
    assert topic[0]["reason"] == "nearest warming" and topic[0]["detail"]["status"] == "warming"


def test_a_binding_changed_in_the_database_is_picked_up(fresh_database):
    with _client(fresh_database) as c:
        assert c.post("/decide", json={"decision_id": str(uuid.uuid4()), "requested_model": "tester",
                                       "messages": [user("x")]}).status_code == 404
        _fetch(fresh_database, "INSERT INTO bindings (scope_model, ruleset_id, version, updated_by) "
                               "VALUES ('tester', 'default', 3, 'test') RETURNING id")
        r = c.post("/decide", json={"decision_id": str(uuid.uuid4()), "requested_model": "tester",
                                    "messages": [user("x")]})
        assert r.status_code == 200 and r.json()["ruleset"] == {"id": "default", "version": 3}


# -- shadow decisions and their views ---------------------------------------------------------


def test_shadow_decisions_and_the_views():
    chat = f"chat-{uuid.uuid4().hex[:8]}"
    with _client() as c:
        body, out = _decide(c, chat=chat, messages=[user("hello"), assistant(), user("tell me a story")])
        primary_id = uuid.UUID(body["decision_id"])
        rows = _wait_rows(config.DB_DSN, "SELECT * FROM decisions WHERE shadow_of = $1", primary_id, n=2)
        (shadow,) = [r for r in rows if r["ruleset_id"] == "default-llm"]
        (signals,) = [r for r in rows if r["ruleset_id"] == "default-signals"]
        c.post("/observe", json={"decision_id": body["decision_id"], "served_model_group": "local-general",
                                 "status": "success"})
        # A repeated /decide derives the same shadow id: nothing is written twice.
        _decide(c, chat=chat, decision_id=body["decision_id"])
        wait_for(lambda: c.app.state.service.pending_writes == 0)
    assert shadow["decision_id"] == _shadow_id(primary_id)
    assert (shadow["ruleset_id"], shadow["mode"], shadow["applied"], shadow["chat_id"]) == (
        "default-llm", "shadow", False, chat)
    assert len(_fetch(config.DB_DSN, "SELECT * FROM decisions WHERE shadow_of = $1", primary_id)) == 2
    assert signals["decision_id"] == _shadow_id(primary_id, "default-signals@1")
    # The draft's signal rows abstain without a sidecar (none in the tests); pii still ran its patterns.
    evaluations = {e["evaluator_id"]: e for e in _fetch(
        config.DB_DSN, "SELECT * FROM evaluations WHERE decision_id = $1", signals["decision_id"])}
    assert evaluations["topic"]["reason"] == "signals unavailable" and evaluations["topic"]["vote_route"] is None
    assert evaluations["pii"]["detail"]["texts_scanned"] == 2 and evaluations["pii"]["abstained"]
    assert (evaluations["injection"]["role"], evaluations["injection"]["abstained"]) == ("observe", True)
    topic = _fetch(config.DB_DSN, "SELECT * FROM evaluations WHERE decision_id = $1 AND evaluator_id = 'topic-llm'",
                   shadow["decision_id"])
    assert topic[0]["abstained"] and topic[0]["detail"]["looked_up"] == 2
    # decision_outcomes: the primary only.
    rows = _fetch(config.DB_DSN, "SELECT * FROM decision_outcomes WHERE chat_id = $1", chat)
    assert [r["decision_id"] for r in rows] == [primary_id] and rows[0]["agree"] is True
    # shadow_comparisons: the shadow beside its primary and what served the turn.
    comparisons = {r["shadow_ruleset_id"]: r for r in _fetch(
        config.DB_DSN, "SELECT * FROM shadow_comparisons WHERE chat_id = $1", chat)}
    assert set(comparisons) == {"default-llm", "default-signals"}
    assert comparisons["default-signals"]["agree_with_primary"] is True
    cmp = comparisons["default-llm"]
    assert (cmp["shadow_decision_id"], cmp["primary_decision_id"]) == (shadow["decision_id"], primary_id)
    assert (cmp["shadow_ruleset_id"], cmp["shadow_ruleset_version"], cmp["primary_ruleset_id"],
            cmp["primary_ruleset_version"]) == ("default-llm", 3, "default", 3)
    assert (cmp["shadow_route"], cmp["primary_route"], cmp["served_model_group"]) == (
        "local-general", "local-general", "local-general")
    assert cmp["agree_with_primary"] is True and cmp["agree_with_served"] is True
    assert cmp["primary_applied"] is True and cmp["observed_at"] is not None
    assert (cmp["shadow_pinned_from"], cmp["primary_pinned_from"], cmp["background_task"]) == (None, None, False)


def test_a_stored_draft_compiles_like_the_file():
    """jsonb reorders object keys (the criteria mapping included): the
    llm_classifier compiled from the STORED document must make the same
    verdict keys and the same tie order as the one compiled from the file."""
    from route_policy.rulesets.validate import compile_ruleset

    with _client() as c:
        stored = c.get("/rulesets/default-llm/3").json()["document"]
        baked = [r for r in c.app.state.service.store.compiled if r.id == "default-llm"][0]
    assert list(stored["evaluators"][-1]["config"]["criteria"]) != list(
        baked.document["evaluators"][-1]["config"]["criteria"])   # jsonb did reorder it
    again = compile_ruleset(stored).entries[-1].evaluator
    topic = baked.entries[-1].evaluator
    assert (again.fingerprint, again.order) == (topic.fingerprint, topic.order)


def test_decision_outcomes_keeps_its_columns():
    """0002 recreated the view over primaries only, with 0001's columns; 0003
    appended pinned_from and background_task."""
    cols = [r["column_name"] for r in _fetch(
        config.DB_DSN, "SELECT column_name FROM information_schema.columns WHERE table_name = 'decision_outcomes' "
                       "ORDER BY ordinal_position")]
    assert cols == ["decision_id", "decided_at", "observed_at", "chat_id", "message_id", "user_id", "key_alias",
                    "litellm_call_id", "response_id", "requested_model", "chosen_route", "applied", "mode",
                    "ruleset_id", "ruleset_version", "reason", "scores", "latency_ms", "decision_error",
                    "served_model_group", "served_deployment", "overflow_reason", "finish_reason", "tool_calls",
                    "status", "outcome_error", "agree", "pinned_from", "background_task"]


# -- analyses ------------------------------------------------------------------------------------------


def _analysis(key: str, **kw) -> analyses_repo.AnalysisRecord:
    base = dict(input_key=key, fingerprint="f" * 64, model_id="qwen3.5-4b", ruleset_id="default-llm",
                ruleset_version=1, evaluator_id="topic-llm", chat_id="c", message_id="m", decision_id=uuid.uuid4(),
                exchange_index=2, input_chars=120, route="local-code", confidence=0.9,
                scores={"local-code": 9, "local-reasoning": None}, reason="r" * 250, status="incomplete",
                error="e" * 400, classifier_job_id="job1", calls=3, prompt_tokens=2100, completion_tokens=120,
                model_seconds=4.5, queue_ms=1200.0, latency_ms=5300.0)
    base.update(kw)
    return analyses_repo.AnalysisRecord(**base)


def test_analyses_round_trip_first_write_wins_and_caps():
    key = uuid.uuid4().hex

    async def go():
        conn = await asyncpg.connect(config.DB_DSN)
        try:
            await _init_connection(conn)
            assert not await analyses_repo.exists(conn, key)
            assert await analyses_repo.insert(conn, _analysis(key)) is True
            assert await analyses_repo.insert(conn, _analysis(key, route="local-general")) is False
            assert await analyses_repo.exists(conn, key)
            got = await analyses_repo.get_many(conn, [key, "missing"])
            row = await conn.fetchrow("SELECT reason, error, scores FROM analyses WHERE input_key = $1", key)
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute("INSERT INTO analyses (input_key, fingerprint, model_id, exchange_index, "
                                   "input_chars, status, latency_ms, reason) "
                                   "VALUES ($1, 'f', 'm', 1, 1, 'ok', 1, $2)", key + "x", "r" * 201)
            return got, row
        finally:
            await conn.close()

    got, row = asyncio.run(go())
    (verdict,) = got.values()
    assert (verdict.input_key, verdict.status, verdict.route, verdict.confidence, verdict.exchange_index,
            verdict.model_id) == (key, "incomplete", "local-code", 0.9, 2, "qwen3.5-4b")
    assert verdict.scores == {"local-code": 9, "local-reasoning": None}
    assert len(row["reason"]) == 200 and len(row["error"]) == 300


def test_the_classifier_loop_end_to_end():
    """/decide for message 2 answers at once and enqueues; the fake
    classifier's job is submitted, read and DELETED; the analyses row lands;
    /decide for message 3 shows topic-llm voting that verdict in the SHADOW
    decision while the primary is unchanged; nothing stores the text."""
    marker = f"zebra{uuid.uuid4().hex[:10]}"
    chat = f"chat-{uuid.uuid4().hex[:8]}"
    fake = FakeClassifier(score={}, default_score=1)
    criteria = {}
    two = [user(f"hello {marker}"), assistant(), user(f"please debug my script, {marker}")]
    three = [*two, assistant("done"), user("thanks, and what next?")]
    with _client(classifier=fake) as c:
        (llm,) = [r for r in c.app.state.service.store.compiled if r.id == "default-llm"]
        criteria = llm.entries[-1].evaluator.config.criteria
        fake.score = {criteria["local-code"]: 9, criteria["local-reasoning"]: 3, criteria["local-private"]: 1}
        first, out1 = _decide(c, chat=chat, messages=two)
        (analysis,) = _wait_rows(config.DB_DSN, "SELECT * FROM analyses WHERE chat_id = $1", chat)
        second, out2 = _decide(c, chat=chat, messages=three)
        (shadow,) = _wait_rows(config.DB_DSN, "SELECT * FROM decisions WHERE shadow_of = $1 "
                                              "AND ruleset_id = 'default-llm'", uuid.UUID(second["decision_id"]))
        wait_for(lambda: c.app.state.service.pending_writes == 0)
    # The job: one text document, the three criteria, deleted after reading.
    (body,) = fake.submitted[:1]
    assert body["documents"][0]["type"] == "text" and marker in body["documents"][0]["data"]
    assert [x["name"] for x in body["criteria"]] == list(criteria.values()) and body["model"] == "qwen3.5-4b"
    assert fake.deleted == [f"job{i:04d}" for i in range(1, len(fake.submitted) + 1)] and fake.jobs == {}
    # The verdict row: scores, route, usage, provenance; no transcript.
    assert (analysis["status"], analysis["route"], analysis["confidence"]) == ("ok", "local-code", 0.9)
    assert analysis["scores"] == {"local-code": 9, "local-reasoning": 3, "local-private": 1}
    assert (analysis["exchange_index"], analysis["calls"], analysis["prompt_tokens"]) == (2, 3, 2100)
    assert analysis["decision_id"] == _shadow_id(first["decision_id"]) and analysis["ruleset_id"] == "default-llm"
    assert analysis["classifier_job_id"] == "job0001" and len(analysis["reason"]) <= 200
    # The primary is today's; the shadow's topic-llm votes the verdict.
    assert out2["ruleset"] == {"id": "default", "version": 3} and set(out2) == DECIDE_KEYS
    assert [v["evaluator"] for v in out2["votes"]][-1] == "sticky-code"
    shadow_votes = shadow["response"]["votes"]
    topic = shadow_votes[-1]
    assert topic["evaluator"] == "topic-llm" and topic["route"] == "local-code" and not topic["abstained"]
    assert topic["reason"] == "local-code scored 9/10 (qwen3.5-4b, 1 message(s) ago)"
    assert shadow["chosen_route"] == "local-code"
    # Nothing route-policy keeps holds the conversation text.
    for table in ("decisions", "evaluations", "analyses", "outcomes"):
        for row in _fetch(config.DB_DSN, f"SELECT * FROM {table}"):
            assert marker not in str(row), table


def test_an_edited_message_makes_the_old_verdict_unreachable():
    chat = f"chat-{uuid.uuid4().hex[:8]}"
    fake = FakeClassifier(default_score=9)
    original = [user("hello"), assistant(), user("write a haiku")]
    with _client(classifier=fake) as c:
        _decide(c, chat=chat, messages=original)
        _wait_rows(config.DB_DSN, "SELECT * FROM analyses WHERE chat_id = $1", chat)
        edited = [user("hello"), assistant(), user("write a limerick"), assistant(), user("and another")]
        second, _ = _decide(c, chat=chat, messages=edited)
        (shadow,) = _wait_rows(config.DB_DSN, "SELECT * FROM decisions WHERE shadow_of = $1 "
                                              "AND ruleset_id = 'default-llm'", uuid.UUID(second["decision_id"]))
        wait_for(lambda: c.app.state.service.pending_writes == 0)
    topic = shadow["response"]["votes"][-1]
    # default-llm reads only the latest user message: k=2's verdict was for
    # "write a haiku", and the edited k=2 renders differently, so it is gone.
    assert topic["abstained"] and topic["reason"] == "no verdict in the last 2 message(s)"


# -- 0003: one route per turn, background tasks -----------------------------------------------------

# A calculator, a joke, then the weather: the weather turn's first call is
# still on sticky-code's 2-exchange cooldown (local-code); the nudge, a new
# user message, would expire it (local-general) if the turn were not pinned.
CALCULATOR = [user("Make me a scientific calculator!"), assistant("done"), user("tell me a joke"), assistant("ha")]
FIRST_CALL = CALCULATOR + [user("check the weather in Utah")]
TOOL_ROUND = FIRST_CALL + tool_round("search_web")
NUDGED = TOOL_ROUND + [assistant("I'll look it up again."), user(NUDGE)]


def _turn(c, chat, message_id="m1"):
    """The turn's three calls, each waited for until its shadow row is in."""
    out = []
    for messages in (FIRST_CALL, TOOL_ROUND, NUDGED):
        body, answer = _decide(c, chat=chat, message_id=message_id, messages=messages)
        _wait_rows(config.DB_DSN, "SELECT 1 FROM decisions WHERE shadow_of = $1", uuid.UUID(body["decision_id"]),
                   n=2)
        out.append((uuid.UUID(body["decision_id"]), answer))
    return out


def test_the_calls_of_one_turn_on_real_rows():
    """The lookup on real rows: the earliest decision of the ruleset id for
    (chat, message) -- the primary's among the primaries, the shadow's among
    its own shadow rows; a background task never pins and is never pinned;
    another message id is another turn. And the columns, the views, the
    /chats read."""
    chat = f"chat-{uuid.uuid4().hex[:8]}"
    with _client() as c:
        task_body, _ = _decide(c, chat=chat, message_id="m1", messages=CALCULATOR + [user(TITLE_TASK)],
                               background_task=True)
        task = uuid.UUID(task_body["decision_id"])
        _wait_rows(config.DB_DSN, "SELECT 1 FROM decisions WHERE shadow_of = $1", task, n=2)
        calls = _turn(c, chat)
        other_body, other_out = _decide(c, chat=chat, message_id="m2", messages=NUDGED)
        other = uuid.UUID(other_body["decision_id"])
        wait_for(lambda: c.app.state.service.pending_writes == 0)
        listed = c.get(f"/chats/{chat}/decisions").json()["decisions"]
    ids = [did for did, _ in calls]
    assert [out["route"] for _, out in calls] == ["local-code"] * 3 and other_out["route"] == "local-general"
    rows = {r["decision_id"]: r for r in _fetch(
        config.DB_DSN, "SELECT decision_id, shadow_of, ruleset_id, pinned_from, background_task, chosen_route, "
                       "reason FROM decisions WHERE chat_id = $1", chat)}
    assert rows[task]["background_task"] is True and rows[task]["pinned_from"] is None
    assert rows[ids[0]]["pinned_from"] is None             # never pinned to the task before it
    assert rows[ids[1]]["pinned_from"] == rows[ids[2]]["pinned_from"] == ids[0]
    assert rows[ids[2]]["reason"].startswith(f"pinned to this turn's first decision (local-code, {ids[0]}); "
                                             "fresh: local-general")
    assert rows[other]["pinned_from"] is None and not rows[other]["background_task"]
    shadow_of = {r["shadow_of"]: r for r in rows.values() if r["ruleset_id"] == "default-llm"}
    assert shadow_of[task]["background_task"] is True
    first_shadow = _shadow_id(ids[0])
    assert shadow_of[ids[0]]["pinned_from"] is None
    assert shadow_of[ids[1]]["pinned_from"] == shadow_of[ids[2]]["pinned_from"] == first_shadow
    # Each shadow pins to ITS OWN first row: default-signals to its own, not default-llm's.
    signals_of = {r["shadow_of"]: r for r in rows.values() if r["ruleset_id"] == "default-signals"}
    first_signals = _shadow_id(ids[0], "default-signals@1")
    assert signals_of[ids[0]]["pinned_from"] is None
    assert signals_of[ids[1]]["pinned_from"] == signals_of[ids[2]]["pinned_from"] == first_signals
    # The views carry the new columns.
    outcomes = {r["decision_id"]: r for r in _fetch(
        config.DB_DSN, "SELECT decision_id, pinned_from, background_task FROM decision_outcomes WHERE chat_id = $1",
        chat)}
    assert outcomes[ids[2]]["pinned_from"] == ids[0] and outcomes[task]["background_task"] is True
    cmp = {r["primary_decision_id"]: r for r in _fetch(
        config.DB_DSN, "SELECT primary_decision_id, shadow_pinned_from, primary_pinned_from, background_task "
                       "FROM shadow_comparisons WHERE chat_id = $1 AND shadow_ruleset_id = 'default-llm'", chat)}
    assert (cmp[ids[2]]["shadow_pinned_from"], cmp[ids[2]]["primary_pinned_from"]) == (first_shadow, ids[0])
    assert cmp[task]["background_task"] is True and cmp[ids[0]]["background_task"] is False
    # GET /chats/{id}/decisions shows them too, the shadows' as well.
    assert [d["pinned_from"] for d in listed] == [None, None, str(ids[0]), str(ids[0]), None]
    assert [d["background_task"] for d in listed] == [True, False, False, False, False]
    assert {s["ruleset"]["id"]: s["pinned_from"] for s in listed[3]["shadows"]} == {
        "default-llm": str(first_shadow), "default-signals": str(first_signals)}


def test_one_classification_per_turn_end_to_end():
    """Three calls of one message id -- the third carrying Open WebUI's
    hidden auto-continue nudge -- share the first call's route, and the
    shadow's topic-llm asks the classifier ONCE for the turn; a background
    task asks for nothing."""
    chat = f"chat-{uuid.uuid4().hex[:8]}"
    fake = FakeClassifier(default_score=1)
    with _client(classifier=fake) as c:
        service = c.app.state.service
        calls = _turn(c, chat)
        _decide(c, chat=chat, message_id=None, messages=NUDGED + [assistant("ok"), user(TITLE_TASK)],
                background_task=True)
        wait_for(lambda: service.pending_writes == 0)
        (analysis,) = _wait_rows(config.DB_DSN, "SELECT * FROM analyses WHERE chat_id = $1", chat)
        wait_for(lambda: service.analysis_queue.depth == 0 and not service.analysis_queue._keys)
    assert [out["route"] for _, out in calls] == ["local-code"] * 3
    assert len(fake.submitted) == 1 and fake.deleted == ["job0001"]
    assert (analysis["exchange_index"], analysis["decision_id"]) == (3, _shadow_id(calls[0][0]))


def test_migration_0003_on_a_phase_4_database(fresh_database, tmp_path):
    """0003 on a database 0001 + 0002 made, with rows in it: the old rows
    read as not pinned, not background tasks; the index and the appended view
    columns are there; the file is idempotent."""
    old = tmp_path / "migrations"
    old.mkdir()
    for name in ("0001_initial.sql", "0002_llm_classifier.sql"):
        shutil.copy(migrations_mod.MIGRATIONS_DIR / name, old / name)
    did = uuid.uuid4()

    async def go():
        conn = await asyncpg.connect(fresh_database)
        try:
            assert await migrations_mod.migrate(conn, old) == ["0001_initial.sql", "0002_llm_classifier.sql"]
            await conn.execute("INSERT INTO rulesets (id, version, document, content_sha256) "
                               "VALUES ('default', 2, '{}', 'h')")
            await conn.execute("INSERT INTO decisions (decision_id, chat_id, message_id, requested_model, "
                               "chosen_route, applied, mode, ruleset_id, ruleset_version, scores, latency_ms, "
                               "response) VALUES ($1, 'c', 'm', 'auto', 'local-code', true, 'enforce', "
                               "'default', 2, '{}', 1, '{}')", did)
            assert await migrations_mod.migrate(conn) == ["0003_turn_pins.sql"]
            assert await migrations_mod.migrate(conn) == []
            (m3,) = [m for m in migrations_mod.migrations() if m.version == 3]
            await conn.execute(m3.sql)   # idempotent by hand, too
            row = await conn.fetchrow("SELECT pinned_from, background_task FROM decisions WHERE decision_id = $1",
                                      did)
            indexes = {r["indexname"] for r in await conn.fetch(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'decisions'")}
            views = {}
            for view in ("decision_outcomes", "shadow_comparisons"):
                views[view] = [r["column_name"] for r in await conn.fetch(
                    "SELECT column_name FROM information_schema.columns WHERE table_name = $1 "
                    "ORDER BY ordinal_position", view)]
            return dict(row), indexes, views
        finally:
            await conn.close()

    row, indexes, views = asyncio.run(go())
    assert row == {"pinned_from": None, "background_task": False}
    assert "decisions_turn_idx" in indexes
    assert views["decision_outcomes"][-3:] == ["agree", "pinned_from", "background_task"]
    assert views["shadow_comparisons"][-4:] == ["agree_with_served", "shadow_pinned_from", "primary_pinned_from",
                                                "background_task"]


def test_the_phase_4_upgrade_moves_both_bindings_and_pins_across_it(fresh_database, tmp_path):
    """The box's path: route-policy-db holds default@2 (3-exchange cooldown,
    no pin) and default-llm@1, bound; this image bakes default@3 and
    default-llm@3. Both slots move; the old versions stay readable with no
    file; their decisions read as unpinned chat turns; and a turn begun under
    default@2 keeps its route under default@3 (the pin is per ruleset id,
    any version)."""
    phase4 = _copy_rulesets(tmp_path, "phase4")
    _rewrite(phase4 / "default.yaml", ("version: 3", "version: 2"), ("pin: turn\n", ""),
             ("exchanges: 2", "exchanges: 3"))
    _rewrite(phase4 / "default-llm.yaml", ("version: 3", "version: 1"), ("pin: turn\n", ""))
    _rewrite(phase4 / "bindings.yaml", ("shadow: [default-llm, default-signals]", "shadow: [default-llm]"))
    (phase4 / "default-signals.yaml").unlink()
    chat = "chat-phase4"
    with _client(fresh_database, phase4) as c:
        old_body, old = _decide(c, chat=chat, message_id="m1", messages=FIRST_CALL)
        assert (old["ruleset"], old["route"]) == ({"id": "default", "version": 2}, "local-code")
    with _client(fresh_database) as c:
        new_body, new = _decide(c, chat=chat, message_id="m1", messages=NUDGED)
        assert (new["ruleset"], new["route"], new["apply"]) == ({"id": "default", "version": 3}, "local-code", True)
        (b,) = c.get("/bindings").json()["bindings"]
        assert b["version"] == 3 and b["shadows"] == SHADOWS
        assert [v["version"] for v in c.get("/rulesets/default").json()["versions"]] == [2, 3]
        assert [v["version"] for v in c.get("/rulesets/default-llm").json()["versions"]] == [1, 3]
        assert c.get("/rulesets/default/2").json()["pin"] is None
        wait_for(lambda: c.app.state.service.pending_writes == 0)
    rows = {r["decision_id"]: r for r in _fetch(
        fresh_database, "SELECT decision_id, pinned_from, background_task FROM decisions WHERE shadow_of IS NULL")}
    old_id, new_id = uuid.UUID(old_body["decision_id"]), uuid.UUID(new_body["decision_id"])
    assert rows[old_id] == {"decision_id": old_id, "pinned_from": None, "background_task": False}
    assert rows[new_id]["pinned_from"] == old_id
