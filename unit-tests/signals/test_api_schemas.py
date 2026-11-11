"""``signals.schemas.json`` against the code (root ``CLAUDE.md`` § Postman
collections; the file-wide checks -- coverage, valid schemas, every collection
body, the rendered copies -- are ``unit-tests/apidocs``).

Every schema of the file is hand-written from ``common.signals.wire`` and
``signals/api.py``: nothing in the service generates one, so there is no
``API_SCHEMAS_UPDATE`` step. This module checks the hand-written schemas
against what the code really produces, on fake models (``sig_helpers``):

* **/health** in all three states (loading 503, failed 503, ok 200), **/models**
  (ready and with a failed lane), **/metrics** on an instrumented app;
* **POST /signals**: every collection body, a request with no ``request_id``,
  and answers carrying every task status -- ``ok``, ``warming``,
  ``unavailable`` (loading and failed), ``partial`` (a failed text, a PII text
  cut mid-scan), ``error``, ``deadline``, ``skipped``; the 413 / 422 bodies
  are the generic ``{"detail": "<str>"}``;
* the wire tests' own messages (``shared/common/tests/test_signals_wire.py``):
  their request validates, their responses -- re-serialised by the wire
  types, with the sidecar's own model ids -- validate;
* the request schema is not looser than it can be: every malformed request
  the app answers 422 for a reason a schema can express fails it too.

The last test checks that every declared status was produced. Runs in the
signals suite:

    UV_LINK_MODE=copy uv run --no-sync python -m pytest unit-tests/signals -q -p no:cacheprovider
"""

from __future__ import annotations

import copy
import importlib.util
import json
import pathlib
import threading
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

from common.apidocs import postman, schemas
from common.signals import SignalsRequest, SignalsResponse, TaskResult
from sig_helpers import (FakeEmbed, FakeInjection, FakeLanguage, FakePii, app_client, examples_json, fakes,
                         wait_until)

from signals.runtime import load_specs

ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVICE_DIR = ROOT / "ai" / "signals"
SCHEMAS_PATH = SERVICE_DIR / "signals.schemas.json"
COLLECTION_PATH = SERVICE_DIR / "signals.postman_collection.json"
WIRE_TESTS = ROOT / "shared" / "common" / "tests" / "test_signals_wire.py"

# What a status with no entry must answer (the collection's generic error body).
GENERIC_ERROR = {"type": "object", "additionalProperties": False, "required": ["detail"],
                 "properties": {"detail": {"type": "string"}}}

SEEN: set = set()          # (endpoint key, status) produced by a real response
STATUSES_SEEN: set = set()  # task statuses answered by POST /signals
RAN: set = set()
PRODUCERS = {"health", "signals", "statuses", "errors"}


@pytest.fixture(scope="module")
def schema_file():
    return schemas.load_schemas(SCHEMAS_PATH)


def check(schema_file, method: str, path: str, response) -> Any:
    """Validate ``response`` against the entry of ``method path``. Returns
    the parsed JSON body, or None for a body that is not JSON."""
    key = f"{method} {path}"
    endpoint = schema_file.endpoints[key]
    status = str(response.status_code)
    spec = endpoint.responses().get(status)
    if spec is None:
        assert response.status_code >= 400, f"{key}: {status} has no entry in {SCHEMAS_PATH.name}"
        errors = schemas.validate(GENERIC_ERROR, response.json())
        assert errors == [], f"{key}: {status} is not the generic error body, so it needs an entry: {errors}"
        return response.json()
    SEEN.add((key, status))
    media = response.headers.get("content-type", "").split(";")[0].strip().lower()
    assert media == spec.content_type, (key, status, media)
    if spec.schema is None:
        return None
    body = response.json()
    errors = schemas.validate(endpoint.standalone(spec.schema), body)
    assert errors == [], f"{key} {status}: {errors}"
    return body


def post(client, payload: Any):
    return client.post("/signals", content=json.dumps(payload), headers={"content-type": "application/json"})


def answer(schema_file, client, payload: Any) -> Dict[str, Any]:
    """POST /signals, checked; the task statuses noted."""
    response = post(client, payload)
    assert response.status_code == 200, (response.status_code, response.text[:200])
    body = check(schema_file, "POST", "/signals", response)
    STATUSES_SEEN.update(r["status"] for r in body["results"].values())
    return body


def request_schema(schema_file):
    endpoint = schema_file.endpoints["POST /signals"]
    return endpoint.standalone(endpoint.requests()[0].schema)


def response_schema(schema_file):
    endpoint = schema_file.endpoints["POST /signals"]
    return endpoint.standalone(endpoint.responses()["200"].schema)


def collection_bodies():
    collection = postman.load(COLLECTION_PATH)
    values = postman.variables(collection)
    return [postman.body(r, values).value for r in postman.requests(collection)
            if postman.endpoint_key(r) == "POST /signals"]


# -- GET /health, GET /models, GET /metrics ---------------------------------------------------


def test_health_models_and_metrics(schema_file):
    gate = threading.Event()
    with app_client(fakes(), load_gate=gate) as client:
        loading = check(schema_file, "GET", "/health", client.get("/health"))
        assert loading["status"] == "loading" and client.get("/health").status_code == 503
        check(schema_file, "GET", "/models", client.get("/models"))
        body = answer(schema_file, client, {"texts": [{"id": "u1", "text": "NAME"}],
                                            "tasks": [{"task": "pii", "texts": ["u1"]}], "deadline_ms": 50})
        assert body["results"]["pii"]["status"] == "unavailable"
        gate.set()
        assert wait_until(lambda: client.get("/health").status_code == 200)
        assert check(schema_file, "GET", "/health", client.get("/health"))["status"] == "ok"
        models = check(schema_file, "GET", "/models", client.get("/models"))
        assert {m["state"] for m in models["models"].values()} == {"ready"}
    with app_client(failing=("injection",)) as client:
        assert wait_until(lambda: client.get("/health").json()["models"]["injection"]["state"] == "failed")
        failed = check(schema_file, "GET", "/health", client.get("/health"))
        assert failed["status"] == "failed" and failed["models"]["injection"]["error"] == "OSError"
        check(schema_file, "GET", "/models", client.get("/models"))
        body = answer(schema_file, client, {"texts": [{"id": "u1", "text": "hi"}],
                                            "tasks": [{"task": "injection", "texts": ["u1"]}], "deadline_ms": 200})
        assert body["results"]["injection"]["error"] == "OSError"
    from signals.app import create_app

    chosen = fakes()
    with TestClient(create_app(loader=lambda lane, spec: chosen[lane], instrument=True)) as client:
        check(schema_file, "GET", "/metrics", client.get("/metrics"))
    RAN.add("health")


# -- POST /signals -------------------------------------------------------------------------


def test_every_collection_body_and_more(schema_file):
    bodies = collection_bodies()
    assert len(bodies) == 6
    with app_client() as client:
        for body in bodies:
            first = answer(schema_file, client, body)
            if first["results"].get("nearest", {}).get("status") == "warming":
                assert wait_until(lambda: post(client, body).json()["results"]["nearest"]["status"] == "ok")
                answer(schema_file, client, body)
        no_id = {k: v for k, v in bodies[1].items() if k != "request_id"}
        out = answer(schema_file, client, {**no_id, "deadline_ms": None})
        assert "request_id" not in out
        answer(schema_file, client, {"request_id": None, "texts": [], "tasks": []})
    RAN.add("signals")


def test_every_task_status_is_answered_in_the_schema(schema_file):
    text = {"texts": [{"id": "u1", "text": "NAME and BOOM"}, {"id": "u2", "text": "NAME only"}]}
    # partial (one text failed) and error (every text failed)
    with app_client(fakes(pii=FakePii(fail="BOOM"), language=FakeLanguage(fail="BOOM"))) as client:
        out = answer(schema_file, client, {**text, "tasks": [{"task": "pii", "texts": ["u1", "u2"]},
                                                            {"task": "language", "texts": ["u1"]}],
                                           "deadline_ms": 1000})
        assert out["results"]["pii"]["status"] == "partial" and out["results"]["pii"]["error"] == "ValueError"
        assert out["results"]["language"]["status"] == "error"
        # skipped: a deadline spent before any job starts (a text not cached yet)
        out = answer(schema_file, client, {"texts": [{"id": "u3", "text": "NAME, not seen before"}],
                                           "tasks": [{"task": "pii", "texts": ["u3"]}], "deadline_ms": 0.001})
        assert out["results"]["pii"]["status"] == "skipped"
    # deadline (nothing finished yet), partial (a PII text cut mid-scan) and
    # warming (an example set still being embedded)
    gate = threading.Event()
    try:
        with app_client(fakes(injection=FakeInjection(gate=gate), embed=FakeEmbed(batch_gate=gate),
                              pii=FakePii(window_chars=10, gate=gate, gate_from=1))) as client:
            out = answer(schema_file, client, {"texts": [{"id": "u1", "text": "some code"}],
                                               "tasks": [{"task": "nearest", "texts": ["u1"],
                                                          "examples": examples_json()}],
                                               "deadline_ms": 50})
            assert out["results"]["nearest"]["status"] == "warming"
            out = answer(schema_file, client, {"texts": [{"id": "u1", "text": "NAME is ok, ADDRESS ok, and more"}],
                                               "tasks": [{"task": "injection", "texts": ["u1"]},
                                                         {"task": "pii", "texts": ["u1"]}],
                                               "deadline_ms": 150})
            assert out["results"]["injection"]["status"] == "deadline"
            assert out["results"]["pii"]["status"] == "partial"
            assert out["results"]["pii"]["texts"]["u1"]["complete"] is False
            gate.set()
    finally:
        gate.set()
    RAN.add("statuses")


@pytest.mark.parametrize("payload", [
    {"texts": [], "tasks": [], "oops": 1},                                            # unknown key
    {"texts": [{"id": "u1", "text": "x", "role": "user"}], "tasks": []},              # unknown text key
    {"texts": [{"id": "u1", "text": "x"}], "tasks": [{"task": "pii", "texts": ["u1"], "k": 5}]},  # param pii lacks
    {"texts": [], "tasks": [{"task": "summarise"}]},                                  # unknown task
    {"texts": [], "tasks": [{"task": "nearest", "texts": []}]},                       # nearest needs examples
    {"texts": [], "tasks": [{"task": "injection", "windows": "head"}]},               # head_tail only
    {"texts": [], "tasks": [{"task": "injection", "max_tokens": 513}]},
    {"texts": [], "tasks": [{"task": "pii", "texts": ["u1", "u1"]}]},
    {"texts": [], "tasks": [], "deadline_ms": 0},
    {"texts": [], "tasks": [], "deadline_ms": 10 ** 9},
    {"texts": [], "tasks": [], "request_id": ""},
    {"texts": [{"id": "u1", "text": 42}], "tasks": []},
    {"texts": None, "tasks": []},
    [],
])
def test_a_malformed_request_fails_the_schema_and_the_app(schema_file, payload):
    assert schemas.validate(request_schema(schema_file), payload) != []
    with app_client() as client:
        response = post(client, payload)
    assert response.status_code == 422
    check(schema_file, "POST", "/signals", response)


def test_the_error_bodies_are_generic(schema_file):
    over = {"texts": [{"id": f"u{i}", "text": "x"} for i in range(65)], "tasks": []}
    assert schemas.validate(request_schema(schema_file), over) != []
    with app_client() as client:
        assert post(client, over).status_code == 413
        check(schema_file, "POST", "/signals", post(client, over))
        not_json = client.post("/signals", content=b"\xff not json", headers={"content-type": "application/json"})
        assert not_json.status_code == 422
        check(schema_file, "POST", "/signals", not_json)
    RAN.add("errors")


# -- the wire tests' messages --------------------------------------------------------------------


def _wire_tests():
    spec = importlib.util.spec_from_file_location("signals_wire_tests_for_schemas", WIRE_TESTS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_wire_tests_messages_validate(schema_file):
    wire = _wire_tests()
    assert schemas.validate(request_schema(schema_file), wire.full_request().to_json()) == []
    assert schemas.validate(request_schema(schema_file),
                            SignalsRequest.from_json(wire.full_request().to_json()).to_json()) == []
    # The sidecar always reports its own ids (`<repo>@<12 hex>`); the wire
    # tests use short fake revisions, so the answer carries the real ones.
    models = {lane: spec.model_id for lane, spec in load_specs().items()}
    plan = copy.deepcopy(wire.PLAN_RESPONSE)
    partial = copy.deepcopy(plan)
    partial["results"]["pii"] = {"status": "partial", "ms": 4.0, "error": "OrtRuntimeError",
                                 "texts": {"u7": {"entities": {"phone": {"count": 1}}, "complete": True}}}
    errored = copy.deepcopy(plan)
    errored["results"]["injection"] = TaskResult("error", error="OrtRuntimeError").to_json()
    schema = response_schema(schema_file)
    for body in (plan, partial, errored):
        sent = SignalsResponse.from_json(body).to_json()
        sent["models"] = models
        assert schemas.validate(schema, sent) == [], body["results"]
        cut = SignalsResponse.from_json(body).for_request(wire.full_request()).to_json()
        cut["models"] = models
        assert schemas.validate(schema, cut) == []
    assert schemas.validate(schema, {**plan, "models": models}) != []          # language without `cached`


# -- last: every declared status was produced --------------------------------------------------


def test_every_declared_response_was_produced(schema_file):
    if RAN != PRODUCERS:
        pytest.skip(f"needs the whole module (ran: {sorted(RAN)})")
    declared = {(key, status) for key, e in schema_file.endpoints.items() for status in e.responses()}
    assert declared - SEEN == set(), "declared but never produced by a real response"
    assert STATUSES_SEEN == {"ok", "partial", "deadline", "skipped", "warming", "error", "unavailable"}
