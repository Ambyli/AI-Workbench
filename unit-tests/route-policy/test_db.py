"""Against a real Postgres (TEST_POSTGRES_DSN): startup (migrations, seeding,
bindings), idempotent /decide and /observe in either order, the
decision_outcomes view, the read endpoints, and the immutability guards.

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

from rp_helpers import DECIDE_KEYS, RULESETS_DIR, assistant, tool_round, user

from route_policy import config
from route_policy.app import create_app
from route_policy.db import migrations as migrations_mod
from route_policy.db.pool import Database, _init_connection
from route_policy.db.repositories.rulesets import SeedConflict

pytestmark = pytest.mark.postgres


def _client(dsn: str = None, rulesets_dir: pathlib.Path = RULESETS_DIR) -> TestClient:
    database = Database(dsn or config.DB_DSN, max_size=4)
    return TestClient(create_app(database=database, rulesets_dir=rulesets_dir, instrument=False))


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
    assert {"rulesets", "bindings", "decisions", "evaluations", "outcomes", "decision_outcomes",
            "schema_migrations"} <= tables
    assert "analyses" not in tables  # phase 4
    (rs,) = _fetch(fresh_database, "SELECT id, version, created_by, source, active_from, active_to FROM rulesets")
    assert (rs["id"], rs["version"], rs["created_by"], rs["source"]) == ("default", 2, "seed", "rulesets/default.yaml")
    assert rs["active_from"] is not None and rs["active_to"] is None
    (b,) = _fetch(fresh_database, "SELECT scope_model, scope_key_alias, ruleset_id, version FROM bindings")
    assert b == {"scope_model": "auto", "scope_key_alias": None, "ruleset_id": "default", "version": 2}
    # A second start changes nothing.
    with _client(fresh_database) as c:
        assert c.get("/health").status_code == 200
    assert len(_fetch(fresh_database, "SELECT * FROM rulesets")) == 1
    assert len(_fetch(fresh_database, "SELECT * FROM schema_migrations")) == len(migrations_mod.migrations())


def test_a_changed_ruleset_without_a_version_bump_stops_startup(fresh_database, tmp_path):
    with _client(fresh_database):
        pass
    edited = _copy_rulesets(tmp_path)
    _rewrite(edited / "default.yaml", ("exchanges: 3", "exchanges: 4"))
    with pytest.raises(SeedConflict, match="default@2"):
        with _client(fresh_database, edited):
            pass


def test_a_version_bump_moves_the_binding(fresh_database, tmp_path):
    with _client(fresh_database):
        pass
    edited = _copy_rulesets(tmp_path)
    _rewrite(edited / "default.yaml", ("version: 2", "version: 3"), ("exchanges: 3", "exchanges: 4"))
    with _client(fresh_database, edited) as c:
        _, out = _decide(c)
        assert out["ruleset"] == {"id": "default", "version": 3}
        versions = [v["version"] for v in c.get("/rulesets/default").json()["versions"]]
        assert versions == [2, 3]
    marks = {r["version"]: r for r in _fetch(fresh_database, "SELECT version, active_from, active_to FROM rulesets")}
    assert marks[2]["active_to"] is not None and marks[3]["active_to"] is None


def test_the_phase_3_upgrade_keeps_the_old_version_readable(fresh_database, tmp_path):
    """The box's path: a database seeded with a shadow `default@1` (phase 2),
    then this image, whose only baked-in `default` is version 2 (enforce).
    The binding moves to 2; version 1 stays stored with its decisions, and
    is still readable and evaluable although no file holds it any more."""
    phase2 = _copy_rulesets(tmp_path, "phase2")
    _rewrite(phase2 / "default.yaml", ("version: 2", "version: 1"), ("mode: enforce", "mode: shadow"))
    with _client(fresh_database, phase2) as c:
        _, out = _decide(c, chat="chat-upgrade")
        assert (out["ruleset"], out["mode"], out["apply"]) == ({"id": "default", "version": 1}, "shadow", False)
    with _client(fresh_database) as c:
        _, out = _decide(c, chat="chat-upgrade")
        assert (out["ruleset"], out["mode"], out["apply"]) == ({"id": "default", "version": 2}, "enforce", True)
        assert c.get("/health").json()["rulesets"] == [{"id": "default", "version": 2}]
        assert [v["version"] for v in c.get("/rulesets/default").json()["versions"]] == [1, 2]
        assert c.get("/rulesets/default/1").json()["document"]["mode"] == "shadow"
        old = c.post("/evaluate", json={"ruleset": {"id": "default", "version": 1},
                                        "messages": [user("fix app.py")]})
        assert old.status_code == 200 and (old.json()["mode"], old.json()["route"]) == ("shadow", "local-code")
        (b,) = c.get("/bindings").json()["bindings"]
        assert b["version"] == 2
        decisions = c.get("/chats/chat-upgrade/decisions").json()["decisions"]
        assert [(d["ruleset"]["version"], d["applied"]) for d in decisions] == [(1, False), (2, True)]


def test_migrations_are_recorded_and_immutable(fresh_database, tmp_path):
    async def go(directory):
        conn = await asyncpg.connect(fresh_database)
        try:
            return await migrations_mod.migrate(conn, directory)
        finally:
            await conn.close()

    assert asyncio.run(go(migrations_mod.MIGRATIONS_DIR)) == ["0001_initial.sql"]
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
    assert (d["ruleset_id"], d["ruleset_version"], d["message_count"], d["tool_count"]) == ("default", 2, 3, 0)
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
        assert len(out["decisions"][1]["votes"]) == 5
        limited = c.get(f"/chats/{chat}/decisions", params={"limit": 1}).json()["decisions"]
        assert [d["decision_id"] for d in limited] == [second["decision_id"]]
        assert c.get("/chats/nobody/decisions").json() == {"chat_id": "nobody", "decisions": []}
        assert c.get(f"/chats/{chat}/decisions", params={"limit": 0}).status_code == 422


def test_ruleset_and_binding_reads():
    with _client() as c:
        listed = c.get("/rulesets").json()["rulesets"]
        assert [(r["id"], r["version"], r["mode"]) for r in listed] == [("default", 2, "enforce")]
        assert "document" not in listed[0] and len(listed[0]["sha256"]) == 64
        latest = c.get("/rulesets/default").json()
        assert latest["version"] == 2 and latest["document"]["id"] == "default"
        assert [v["version"] for v in latest["versions"]] == [2]
        assert c.get("/rulesets/default/2").json()["document"]["evaluators"][0]["id"] == "privacy"
        assert c.get("/rulesets/default/1").status_code == 404
        assert c.get("/rulesets/default/3").status_code == 404
        assert c.get("/rulesets/nope").status_code == 404
        assert c.get("/rulesets/default/0").status_code == 422
        (b,) = c.get("/bindings").json()["bindings"]
        assert (b["scope_model"], b["scope_key_alias"], b["ruleset_id"], b["version"]) == ("auto", None, "default", 2)
        # A stored document posts straight back to /evaluate.
        doc = c.get("/rulesets/default/2").json()["document"]
        assert c.post("/evaluate", json={"document": doc, "messages": [user("prove it")]}).json()["route"] == \
            "local-reasoning"


def test_a_binding_changed_in_the_database_is_picked_up(fresh_database):
    with _client(fresh_database) as c:
        assert c.post("/decide", json={"decision_id": str(uuid.uuid4()), "requested_model": "tester",
                                       "messages": [user("x")]}).status_code == 404
        _fetch(fresh_database, "INSERT INTO bindings (scope_model, ruleset_id, version, updated_by) "
                               "VALUES ('tester', 'default', 2, 'test') RETURNING id")
        r = c.post("/decide", json={"decision_id": str(uuid.uuid4()), "requested_model": "tester",
                                    "messages": [user("x")]})
        assert r.status_code == 200 and r.json()["ruleset"] == {"id": "default", "version": 2}
