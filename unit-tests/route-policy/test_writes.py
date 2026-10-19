"""No database wait may hold /decide for long (service.py, rulesets/store.py).

LiteLLM awaits /decide for every `auto` turn within 0.25 s and falls back to
local-private past it, so:

* a decision write that outlasts ``write_budget_s`` is answered at once and
  finishes in the background (route_policy_writes_deferred_total), and it
  still lands;
* a write that finishes in time behaves as before -- a duplicate
  decision_id replays the first stored response;
* at most ``max_in_flight`` background writes; past that a write is dropped
  and counted;
* shutdown waits for pending writes, then cancels the rest, cleanly;
* /observe defers the same way (202 ``{"stored": null, "deferred": true}``);
* the binding refresh and an uncached version fetch on the /decide path are
  bounded by the store's ``read_budget_s``.

No real database: a fake whose ``acquire`` sleeps, waits on a gate or fails,
and the repositories' writes replaced by an in-memory table.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from rp_helpers import RULESETS_DIR, default_ruleset, seed, tool_round, user

from route_policy.api.schemas import DecideRequest, ObserveRequest
from route_policy.app import create_app
from route_policy.db.repositories import decisions as decisions_repo
from route_policy.db.repositories import outcomes as outcomes_repo
from route_policy.db.repositories import rulesets as rulesets_repo
from route_policy.engine.pipeline import run as run_pipeline
from route_policy.rulesets.loader import Binding
from route_policy.rulesets.model import content_sha256
from route_policy.rulesets.store import RulesetStore
from route_policy.service import DEFERRED, DROPPED, DUPLICATE, OutcomeNotStored, Service


class FakeDatabase:
    """``acquire`` waits on ``gate`` (when given), sleeps ``delay``, then
    fails (``fail``) or hands out a dummy connection."""

    def __init__(self, delay: float = 0.0, fail: bool = False, gate=None) -> None:
        self.delay, self.fail, self.gate = delay, fail, gate
        self.acquired = 0

    @asynccontextmanager
    async def acquire(self):
        self.acquired += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise ConnectionError("route-policy-db is down")
        yield SimpleNamespace()


@pytest.fixture
def table(monkeypatch):
    """The decision / outcome inserts against an in-memory table."""
    rows = SimpleNamespace(decisions={}, outcomes={})

    async def insert_decision(conn, record, evaluations):
        if record.decision_id in rows.decisions:
            return False
        rows.decisions[record.decision_id] = record.response
        return True

    async def get_response(conn, decision_id):
        return rows.decisions.get(decision_id)

    async def insert_outcome(conn, record):
        if record.decision_id in rows.outcomes:
            return False
        rows.outcomes[record.decision_id] = record.status
        return True

    monkeypatch.setattr(decisions_repo, "insert", insert_decision)
    monkeypatch.setattr(decisions_repo, "get_response", get_response)
    monkeypatch.setattr(outcomes_repo, "insert", insert_outcome)
    return rows


def _sample(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _store() -> RulesetStore:
    s = seed()
    store = RulesetStore(None)
    for compiled in s.compiled:
        store.add(compiled)
    store.set_bindings(s.bindings)
    return store


def _service(database, **kw) -> Service:
    return Service(store=_store(), database=database, rulesets_dir=RULESETS_DIR, **kw)


def _decide_body(decision_id=None) -> dict:
    return {"decision_id": decision_id or str(uuid.uuid4()), "requested_model": "auto",
            "messages": [user("go"), *tool_round("sandbox-mcp_exec")]}


async def _record(service: Service, decision_id=None):
    body = DecideRequest.model_validate(_decide_body(decision_id))
    decision = await run_pipeline(default_ruleset(), body.context())
    return await service.record_decision(body, decision, decision.response(str(body.decision_id)))


# -- /decide over HTTP: a hung write ----------------------------------------------------------


def test_a_hung_write_does_not_hold_decide_and_lands_later(table):
    database = FakeDatabase(delay=0.6)
    service = _service(database, write_budget_s=0.05)
    deferred = _sample("route_policy_writes_deferred_total", write="decision")
    body = _decide_body()
    with TestClient(create_app(service=service, instrument=False)) as client:
        started = time.monotonic()
        r = client.post("/decide", json=body)
        elapsed = time.monotonic() - started
        assert r.status_code == 200 and r.json()["route"] == "local-code" and r.json()["apply"] is True
        # The budget, not the 0.6 s the write takes (generous margin for a slow CI box).
        assert elapsed < 0.4, elapsed
        assert _sample("route_policy_writes_deferred_total", write="decision") == deferred + 1
        assert service.pending_writes == 1 and uuid.UUID(body["decision_id"]) not in table.decisions
    # Shutdown drained it: the write landed after the answer.
    assert uuid.UUID(body["decision_id"]) in table.decisions
    assert service.pending_writes == 0


def test_a_write_within_budget_behaves_as_before(table):
    service = _service(FakeDatabase(), write_budget_s=5)
    body = _decide_body()
    with TestClient(create_app(service=service, instrument=False)) as client:
        first = client.post("/decide", json=body).json()
        assert uuid.UUID(body["decision_id"]) in table.decisions and service.pending_writes == 0
        # A duplicate id, different conversation: the FIRST stored decision comes back.
        second = client.post("/decide", json={**body, "messages": [user("prove it")]}).json()
    assert second == first and second["route"] == "local-code"


def test_duplicate_within_budget_replays_at_the_service(monkeypatch):
    decision_id = str(uuid.uuid4())
    stored = {"decision_id": decision_id, "route": "local-reasoning", "first": True}

    async def insert(conn, record, evaluations):
        return False  # the id is already there

    async def get_response(conn, did):
        return stored

    monkeypatch.setattr(decisions_repo, "insert", insert)
    monkeypatch.setattr(decisions_repo, "get_response", get_response)
    service = _service(FakeDatabase(), write_budget_s=5)
    status, response = asyncio.run(_record(service, decision_id))
    assert status == DUPLICATE and response == stored


def test_a_background_write_that_fails_is_counted_like_any_write_error(table):
    async def go():
        service = _service(FakeDatabase(delay=0.2, fail=True), write_budget_s=0.01)
        errors = _sample("route_policy_errors_total", kind="decision_write")
        status, response = await _record(service)
        assert status == DEFERRED and response["route"] == "local-code"
        assert await service.drain(timeout_s=5) == 0  # finished (failed) on its own
        return errors

    errors = asyncio.run(go())
    assert _sample("route_policy_errors_total", kind="decision_write") == errors + 1
    assert table.decisions == {}


# -- the in-flight cap ----------------------------------------------------------------------------


def test_past_the_cap_a_write_is_dropped_and_counted(table):
    async def go():
        gate = asyncio.Event()
        service = _service(FakeDatabase(gate=gate), write_budget_s=0, max_in_flight=2)
        dropped = _sample("route_policy_errors_total", kind="decision_write_dropped")
        statuses = [(await _record(service))[0] for _ in range(3)]
        assert statuses == [DEFERRED, DEFERRED, DROPPED]
        assert service.pending_writes == 2
        assert _sample("route_policy_errors_total", kind="decision_write_dropped") == dropped + 1
        assert _sample("route_policy_writes_in_flight") == 2
        gate.set()
        assert await service.drain(timeout_s=5) == 0
        assert service.pending_writes == 0 and _sample("route_policy_writes_in_flight") == 0

    asyncio.run(go())
    assert len(table.decisions) == 2  # the dropped one was never written


# -- shutdown ---------------------------------------------------------------------------------------


def test_shutdown_with_writes_that_never_finish_is_clean(table, recwarn):
    async def go():
        never = asyncio.Event()
        service = _service(FakeDatabase(gate=never), write_budget_s=0)
        for _ in range(2):
            assert (await _record(service))[0] == DEFERRED
        assert service.pending_writes == 2
        started = time.monotonic()
        assert await service.drain(timeout_s=0.1) == 2  # waited, then cancelled both
        assert time.monotonic() - started < 2
        assert service.pending_writes == 0

    asyncio.run(go())
    assert table.decisions == {}
    assert not [w for w in recwarn if "never awaited" in str(w.message) or "destroyed" in str(w.message)]


def test_app_shutdown_drains_pending_writes(table):
    never = asyncio.Event()
    service = _service(FakeDatabase(gate=never), write_budget_s=0, drain_s=0.1)
    with TestClient(create_app(service=service, instrument=False)) as client:
        assert client.post("/decide", json=_decide_body()).status_code == 200
        assert service.pending_writes == 1
    assert service.pending_writes == 0  # cancelled by the lifespan, not left behind


# -- /observe ------------------------------------------------------------------------------------------


def test_observe_defers_a_hung_write_and_503s_a_failure_in_time(table):
    hung = _service(FakeDatabase(delay=0.5), write_budget_s=0.05)
    deferred = _sample("route_policy_writes_deferred_total", write="outcome")
    did = str(uuid.uuid4())
    with TestClient(create_app(service=hung, instrument=False)) as client:
        started = time.monotonic()
        r = client.post("/observe", json={"decision_id": did, "status": "success"})
        assert time.monotonic() - started < 0.4
        assert r.status_code == 202 and r.json() == {"decision_id": did, "stored": None, "deferred": True}
    assert _sample("route_policy_writes_deferred_total", write="outcome") == deferred + 1
    assert table.outcomes == {uuid.UUID(did): "success"}  # landed during shutdown

    failing = _service(FakeDatabase(fail=True), write_budget_s=5)
    with TestClient(create_app(service=failing, instrument=False)) as client:
        assert client.post("/observe", json={"decision_id": did, "status": "success"}).status_code == 503
    fast = _service(FakeDatabase(), write_budget_s=5)
    with TestClient(create_app(service=fast, instrument=False)) as client:
        r = client.post("/observe", json={"decision_id": str(uuid.uuid4()), "status": "success"})
        assert r.status_code == 202 and r.json()["stored"] is True


def test_observe_past_the_cap_is_503(table):
    async def go():
        gate = asyncio.Event()
        service = _service(FakeDatabase(gate=gate), write_budget_s=0, max_in_flight=1)
        body = ObserveRequest.model_validate({"decision_id": str(uuid.uuid4()), "status": "success"})
        assert await service.record_outcome(body) is None  # deferred
        dropped = _sample("route_policy_errors_total", kind="outcome_write_dropped")
        with pytest.raises(OutcomeNotStored):
            await service.record_outcome(body)
        assert _sample("route_policy_errors_total", kind="outcome_write_dropped") == dropped + 1
        gate.set()
        await service.drain(timeout_s=5)

    asyncio.run(go())


# -- reads on the /decide path ------------------------------------------------------------------------


def test_a_hung_binding_refresh_decides_from_the_snapshot(monkeypatch):
    async def go():
        gate = asyncio.Event()
        store = RulesetStore(FakeDatabase(gate=gate), refresh_s=0, read_budget_s=0.05)
        for compiled in seed().compiled:
            store.add(compiled)
        store.set_bindings([Binding("auto", None, "default", 2)])

        async def list_bindings(conn):
            return [{"scope_model": "auto", "scope_key_alias": None, "ruleset_id": "default", "version": 2}]

        monkeypatch.setattr(rulesets_repo, "list_bindings", list_bindings)
        cut = _sample("route_policy_db_reads_cut_total", read="binding_refresh")
        started = time.monotonic()
        assert (await store.resolve("auto", None)).version == 2
        assert time.monotonic() - started < 0.3
        assert _sample("route_policy_db_reads_cut_total", read="binding_refresh") == cut + 1
        # Next request: the refresh is still running -- no wait at all, no second refresh.
        started = time.monotonic()
        assert (await store.resolve("auto", None)).version == 2
        assert time.monotonic() - started < 0.04
        assert store.database.acquired == 1
        gate.set()
        await asyncio.wait_for(store._refresh_task, 5)
        await store.cancel_background()

    asyncio.run(go())


def test_a_hung_version_fetch_is_cut_and_lands_in_the_cache(monkeypatch):
    async def go():
        gate = asyncio.Event()
        store = RulesetStore(FakeDatabase(gate=gate), refresh_s=3600, read_budget_s=0.05)
        doc = {**default_ruleset().document, "version": 7}

        async def get(conn, ruleset_id, version=None):
            return {"id": "default", "version": 7, "document": doc, "content_sha256": content_sha256(doc)}

        monkeypatch.setattr(rulesets_repo, "get", get)
        store.set_bindings([Binding("auto", None, "default", 7)])
        cut = _sample("route_policy_db_reads_cut_total", read="ruleset_fetch")
        started = time.monotonic()
        assert await store.resolve("auto", None) is None  # not in time: /decide answers 503
        assert time.monotonic() - started < 0.3
        assert _sample("route_policy_db_reads_cut_total", read="ruleset_fetch") == cut + 1
        assert await store.resolve("auto", None) is None and store.database.acquired == 1  # shared fetch
        gate.set()
        await asyncio.wait_for(asyncio.gather(*store._fetches.values()), 5)
        assert (await store.resolve("auto", None)).version == 7  # from the cache now
        # Without a budget (POST /evaluate, the read endpoints) get() waits as before.
        assert (await store.get("default", 7)).version == 7

    asyncio.run(go())


def test_the_app_bounds_its_reads_and_writes_by_config():
    from route_policy import config

    assert config.DB_READ_BUDGET_S > 0 and config.WRITES_MAX_IN_FLIGHT == 256 and config.WRITES_DRAIN_S > 0
    service = Service(store=RulesetStore(None), database=None, rulesets_dir=RULESETS_DIR)
    assert service.write_budget_s == config.DECISION_WRITE_BUDGET_S
    assert service.max_in_flight == config.WRITES_MAX_IN_FLIGHT and service.drain_s == config.WRITES_DRAIN_S
