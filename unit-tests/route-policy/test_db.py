"""Against a real Postgres (TEST_POSTGRES_DSN): startup (migrations, seeding,
bindings and their shadow slots), idempotent /decide and /observe in either
order, shadow decisions and the views over them (decision_outcomes over
primaries, shadow_comparisons), the analyses table, the whole classifier
loop on a fake classifier, the read endpoints, and the immutability guards.

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

from rp_helpers import DECIDE_KEYS, RULESETS_DIR, FakeClassifier, assistant, tool_round, user, wait_for

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


def _shadow_id(primary_id) -> uuid.UUID:
    return uuid.uuid5(uuid.UUID(str(primary_id)), "default-llm@1")


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
        ("default", 2, "seed", "rulesets/default.yaml"), ("default-llm", 1, "seed", "rulesets/default-llm.yaml")]
    # Both are active: one as the primary, one in a shadow slot.
    assert all(r["active_from"] is not None and r["active_to"] is None for r in rows)
    (b,) = _fetch(fresh_database, "SELECT id, scope_model, scope_key_alias, ruleset_id, version FROM bindings")
    assert {k: v for k, v in b.items() if k != "id"} == {"scope_model": "auto", "scope_key_alias": None,
                                                         "ruleset_id": "default", "version": 2}
    (s,) = _fetch(fresh_database, "SELECT binding_id, position, ruleset_id, version FROM binding_shadows")
    assert s == {"binding_id": b["id"], "position": 0, "ruleset_id": "default-llm", "version": 1}
    # A second start changes nothing.
    with _client(fresh_database) as c:
        assert c.get("/health").status_code == 200
    assert len(_fetch(fresh_database, "SELECT * FROM rulesets")) == 2
    assert len(_fetch(fresh_database, "SELECT * FROM binding_shadows")) == 1
    assert len(_fetch(fresh_database, "SELECT * FROM schema_migrations")) == len(migrations_mod.migrations())


def test_shadow_slots_follow_the_bindings_file(fresh_database, tmp_path):
    with _client(fresh_database):
        pass
    unshadowed = _copy_rulesets(tmp_path)
    _rewrite(unshadowed / "bindings.yaml", ("  shadow: [default-llm]\n", ""))
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
        assert b["shadows"] == [{"ruleset_id": "default-llm", "version": 1}]
    (llm,) = _fetch(fresh_database, "SELECT active_to FROM rulesets WHERE id = 'default-llm'")
    assert llm["active_to"] is None


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
    """The box's path: a database holding a shadow `default@1` bound to
    `auto` (phase 2), then this image, whose only baked-in `default` is
    version 2 (enforce). The binding moves to 2; version 1 stays stored with
    its decisions, and is still readable and evaluable although no file
    holds it any more. (The phase-2 state is made by hand: today's seed
    refuses a shadow ruleset as a binding's primary.)"""
    phase2 = _copy_rulesets(tmp_path, "phase2")
    _rewrite(phase2 / "default.yaml", ("version: 2", "version: 1"), ("mode: enforce", "mode: shadow"))
    (phase2 / "bindings.yaml").unlink()
    (phase2 / "default-llm.yaml").unlink()
    with _client(fresh_database, phase2) as c:
        _fetch(fresh_database, "INSERT INTO bindings (scope_model, ruleset_id, version) "
                               "VALUES ('auto', 'default', 1) RETURNING id")
        _, out = _decide(c, chat="chat-upgrade")
        assert (out["ruleset"], out["mode"], out["apply"]) == ({"id": "default", "version": 1}, "shadow", False)
    with _client(fresh_database) as c:
        _, out = _decide(c, chat="chat-upgrade")
        assert (out["ruleset"], out["mode"], out["apply"]) == ({"id": "default", "version": 2}, "enforce", True)
        assert c.get("/health").json()["rulesets"] == [{"id": "default", "version": 2},
                                                       {"id": "default-llm", "version": 1}]
        assert [v["version"] for v in c.get("/rulesets/default").json()["versions"]] == [1, 2]
        assert c.get("/rulesets/default/1").json()["document"]["mode"] == "shadow"
        old = c.post("/evaluate", json={"ruleset": {"id": "default", "version": 1},
                                        "messages": [user("fix app.py")]})
        assert old.status_code == 200 and (old.json()["mode"], old.json()["route"]) == ("shadow", "local-code")
        (b,) = c.get("/bindings").json()["bindings"]
        assert b["version"] == 2 and b["shadows"] == [{"ruleset_id": "default-llm", "version": 1}]
        decisions = c.get("/chats/chat-upgrade/decisions").json()["decisions"]
        assert [(d["ruleset"]["version"], d["applied"]) for d in decisions] == [(1, False), (2, True)]


def test_migrations_are_recorded_and_immutable(fresh_database, tmp_path):
    async def go(directory):
        conn = await asyncpg.connect(fresh_database)
        try:
            return await migrations_mod.migrate(conn, directory)
        finally:
            await conn.close()

    assert asyncio.run(go(migrations_mod.MIGRATIONS_DIR)) == ["0001_initial.sql", "0002_llm_classifier.sql"]
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
        assert len(out["decisions"][1]["votes"]) == 5 and out["decisions"][1]["shadow_of"] is None
        # Each primary carries its shadow, written after the answer.
        _wait_rows(config.DB_DSN, "SELECT 1 FROM decisions WHERE shadow_of = $1",
                   uuid.UUID(second["decision_id"]))
        (shadow,) = c.get(f"/chats/{chat}/decisions").json()["decisions"][1]["shadows"]
        assert shadow["shadow_of"] == second["decision_id"] and shadow["outcome"] is None
        assert shadow["decision_id"] == str(_shadow_id(second["decision_id"])) and "shadows" not in shadow
        assert shadow["ruleset"] == {"id": "default-llm", "version": 1} and shadow["mode"] == "shadow"
        assert len(shadow["votes"]) == 5
        limited = c.get(f"/chats/{chat}/decisions", params={"limit": 1}).json()["decisions"]
        assert [d["decision_id"] for d in limited] == [second["decision_id"]]
        assert c.get("/chats/nobody/decisions").json() == {"chat_id": "nobody", "decisions": []}
        assert c.get(f"/chats/{chat}/decisions", params={"limit": 0}).status_code == 422


def test_ruleset_and_binding_reads():
    with _client() as c:
        listed = c.get("/rulesets").json()["rulesets"]
        assert [(r["id"], r["version"], r["mode"]) for r in listed] == [("default", 2, "enforce"),
                                                                        ("default-llm", 1, "shadow")]
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
        assert b["shadows"] == [{"ruleset_id": "default-llm", "version": 1}]
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


# -- shadow decisions and their views ---------------------------------------------------------


def test_shadow_decisions_and_the_views():
    chat = f"chat-{uuid.uuid4().hex[:8]}"
    with _client() as c:
        body, out = _decide(c, chat=chat, messages=[user("hello"), assistant(), user("tell me a story")])
        primary_id = uuid.UUID(body["decision_id"])
        (shadow,) = _wait_rows(config.DB_DSN, "SELECT * FROM decisions WHERE shadow_of = $1", primary_id)
        c.post("/observe", json={"decision_id": body["decision_id"], "served_model_group": "local-general",
                                 "status": "success"})
        # A repeated /decide derives the same shadow id: nothing is written twice.
        _decide(c, chat=chat, decision_id=body["decision_id"])
        wait_for(lambda: c.app.state.service.pending_writes == 0)
    assert shadow["decision_id"] == _shadow_id(primary_id)
    assert (shadow["ruleset_id"], shadow["mode"], shadow["applied"], shadow["chat_id"]) == (
        "default-llm", "shadow", False, chat)
    assert len(_fetch(config.DB_DSN, "SELECT * FROM decisions WHERE shadow_of = $1", primary_id)) == 1
    topic = _fetch(config.DB_DSN, "SELECT * FROM evaluations WHERE decision_id = $1 AND evaluator_id = 'topic-llm'",
                   shadow["decision_id"])
    assert topic[0]["abstained"] and topic[0]["detail"]["looked_up"] == 2
    # decision_outcomes: the primary only.
    rows = _fetch(config.DB_DSN, "SELECT * FROM decision_outcomes WHERE chat_id = $1", chat)
    assert [r["decision_id"] for r in rows] == [primary_id] and rows[0]["agree"] is True
    # shadow_comparisons: the shadow beside its primary and what served the turn.
    (cmp,) = _fetch(config.DB_DSN, "SELECT * FROM shadow_comparisons WHERE chat_id = $1", chat)
    assert (cmp["shadow_decision_id"], cmp["primary_decision_id"]) == (shadow["decision_id"], primary_id)
    assert (cmp["shadow_ruleset_id"], cmp["shadow_ruleset_version"], cmp["primary_ruleset_id"],
            cmp["primary_ruleset_version"]) == ("default-llm", 1, "default", 2)
    assert (cmp["shadow_route"], cmp["primary_route"], cmp["served_model_group"]) == (
        "local-general", "local-general", "local-general")
    assert cmp["agree_with_primary"] is True and cmp["agree_with_served"] is True
    assert cmp["primary_applied"] is True and cmp["observed_at"] is not None


def test_a_stored_draft_compiles_like_the_file():
    """jsonb reorders object keys (the criteria mapping included): the
    llm_classifier compiled from the STORED document must make the same
    verdict keys and the same tie order as the one compiled from the file."""
    from route_policy.rulesets.validate import compile_ruleset

    with _client() as c:
        stored = c.get("/rulesets/default-llm/1").json()["document"]
        baked = [r for r in c.app.state.service.store.compiled if r.id == "default-llm"][0]
    assert list(stored["evaluators"][-1]["config"]["criteria"]) != list(
        baked.document["evaluators"][-1]["config"]["criteria"])   # jsonb did reorder it
    again = compile_ruleset(stored).entries[-1].evaluator
    topic = baked.entries[-1].evaluator
    assert (again.fingerprint, again.order) == (topic.fingerprint, topic.order)


def test_decision_outcomes_keeps_its_columns():
    """0002 recreated the view over primaries only, with 0001's columns."""
    cols = [r["column_name"] for r in _fetch(
        config.DB_DSN, "SELECT column_name FROM information_schema.columns WHERE table_name = 'decision_outcomes' "
                       "ORDER BY ordinal_position")]
    assert cols == ["decision_id", "decided_at", "observed_at", "chat_id", "message_id", "user_id", "key_alias",
                    "litellm_call_id", "response_id", "requested_model", "chosen_route", "applied", "mode",
                    "ruleset_id", "ruleset_version", "reason", "scores", "latency_ms", "decision_error",
                    "served_model_group", "served_deployment", "overflow_reason", "finish_reason", "tool_calls",
                    "status", "outcome_error", "agree"]


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
        (shadow,) = _wait_rows(config.DB_DSN, "SELECT * FROM decisions WHERE shadow_of = $1",
                               uuid.UUID(second["decision_id"]))
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
    assert out2["ruleset"] == {"id": "default", "version": 2} and set(out2) == DECIDE_KEYS
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
        (shadow,) = _wait_rows(config.DB_DSN, "SELECT * FROM decisions WHERE shadow_of = $1",
                               uuid.UUID(second["decision_id"]))
        wait_for(lambda: c.app.state.service.pending_writes == 0)
    topic = shadow["response"]["votes"][-1]
    assert topic["abstained"] and topic["reason"] == "no verdict in the last 3 message(s)"
