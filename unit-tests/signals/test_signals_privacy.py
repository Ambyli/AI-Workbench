"""The sidecar never echoes text (ROUTE_POLICY_PHASE5_PLAN § 6, invariant 2).

A sentinel string goes into every text and example of a request -- and into
the messages of the exceptions the fake models and loaders raise, the way a
tokenizer or onnxruntime error can quote its input -- and must come back in
no response body (200, 413, 422, 500), no log record at any level, no
/health, /models or /metrics output. Responses carry no span or offset,
and handling a request writes no file.
"""

from __future__ import annotations

import builtins
import json
import logging
import os

import pytest
from prometheus_client import REGISTRY, generate_latest

from sig_helpers import SENTINEL, FakeLanguage, FakePii, Recorder, app_client, examples_json, fakes, wait_until

from common.signals import Example

SPAN_KEYS = {"start", "end", "offset", "offsets", "span", "spans", "text", "chars"}


def _keys(node, out=None):
    out = set() if out is None else out
    if isinstance(node, dict):
        out.update(node)
        for value in node.values():
            _keys(value, out)
    elif isinstance(node, list):
        for value in node:
            _keys(value, out)
    return out


def _request(sentinel_examples: bool = True) -> dict:
    examples = examples_json()
    if sentinel_examples:
        examples = examples_json(examples=(Example("code-9", "local-code", f"code {SENTINEL}"),
                                           Example("gen-9", "local-general", f"poem {SENTINEL}")))
    return {
        "request_id": "req-1",
        "deadline_ms": 2000,
        "texts": [{"id": "u1", "text": f"my NAME is {SENTINEL} at ADDRESS, ignore previous {SENTINEL}"},
                  {"id": "t1", "text": f"EMAIL {SENTINEL} in a tool result"}],
        "tasks": [{"task": "pii", "texts": ["u1", "t1"]},
                  {"task": "nearest", "texts": ["u1"], "k": 2, "examples": examples},
                  {"task": "injection", "texts": ["u1"]},
                  {"task": "language", "texts": ["u1", "t1"]}],
    }


def _post(client, payload):
    return client.post("/signals", content=json.dumps(payload), headers={"content-type": "application/json"})


@pytest.fixture
def logs(caplog):
    caplog.set_level(logging.DEBUG)
    return caplog


def _assert_clean(*texts: str) -> None:
    for text in texts:
        assert SENTINEL not in text


def test_no_text_in_any_answer_log_or_metric(logs):
    with app_client() as client:
        first = _post(client, _request())                             # nearest is warming here
        assert wait_until(lambda: _post(client, _request()).json()["results"]["nearest"]["status"] == "ok")
        answer = _post(client, _request())
        health, models = client.get("/health"), client.get("/models")
    assert answer.status_code == 200
    body = answer.json()
    assert {r["status"] for r in body["results"].values()} == {"ok"}
    _assert_clean(first.text, answer.text, health.text, models.text, logs.text,
                  generate_latest(REGISTRY).decode("utf-8"))
    assert not (_keys(body) & SPAN_KEYS)                              # counts, scores, ids -- no spans or offsets


@pytest.mark.parametrize("payload", [
    {"texts": [{"id": "u1", "text": SENTINEL, "extra": SENTINEL}], "tasks": []},
    {"texts": [{"id": "u1", "text": SENTINEL}], "tasks": [{"task": SENTINEL}]},
    {"texts": [{"id": "u1", "text": SENTINEL}], "tasks": [{"task": "pii", "texts": ["u1"], SENTINEL: 1}]},
    {"texts": [{"id": "u1", "text": [SENTINEL]}], "tasks": []},
    {"texts": [{"id": "u1", "text": SENTINEL}], "tasks": [], "deadline_ms": SENTINEL},
])
def test_a_422_never_quotes_the_request(payload, logs):
    with app_client() as client:
        resp = _post(client, payload)
    assert resp.status_code == 422
    _assert_clean(resp.text, logs.text)


def test_a_body_that_is_not_json_is_not_quoted(logs):
    with app_client() as client:
        resp = client.post("/signals", content=f'{{"texts": [{{"id": "u1", "text": "{SENTINEL}" ]'.encode(),
                           headers={"content-type": "application/json"})
    assert resp.status_code == 422
    _assert_clean(resp.text, logs.text)


def test_a_413_names_sizes_and_ids_only(logs):
    with app_client() as client:
        resp = _post(client, {"request_id": SENTINEL + " not an id",
                              "texts": [{"id": "u1", "text": SENTINEL * 2000}], "tasks": []})
    assert resp.status_code == 413
    _assert_clean(resp.text, logs.text)


def test_a_500_is_reduced_to_the_exception_type(logs, monkeypatch):
    with app_client() as client:
        async def boom(*_a, **_k):
            raise ValueError(f"could not handle {SENTINEL}")

        monkeypatch.setattr(client.app.state.engine, "run", boom)
        resp = _post(client, _request(sentinel_examples=False))
    assert resp.status_code == 500 and resp.json() == {"detail": "internal error (ValueError)"}
    _assert_clean(resp.text, logs.text)


def test_a_model_exception_quoting_the_text_is_reduced_to_its_type(logs):
    rec = Recorder()
    models = fakes(rec, pii=FakePii(rec, fail=SENTINEL), language=FakeLanguage(rec, fail=SENTINEL))
    with app_client(models) as client:
        resp = _post(client, _request(sentinel_examples=False))
    body = resp.json()
    assert body["results"]["pii"]["status"] == "error" and body["results"]["pii"]["error"] == "ValueError"
    assert body["results"]["language"]["error"] == "ValueError"
    _assert_clean(resp.text, logs.text)


def test_a_load_failure_quoting_a_path_is_reduced_to_its_type(logs):
    with app_client(failing=("pii",)) as client:
        assert wait_until(lambda: client.get("/health").json()["models"]["pii"]["state"] == "failed")
        health = client.get("/health")
        resp = _post(client, _request(sentinel_examples=False))
    assert health.json()["models"]["pii"]["error"] == "OSError"
    _assert_clean(health.text, resp.text, logs.text)


def test_a_request_id_that_is_not_an_id_is_not_logged(logs):
    payload = _request(sentinel_examples=False)
    payload["request_id"] = f"{SENTINEL} with spaces"
    with app_client() as client:
        _post(client, payload)
    _assert_clean(logs.text)
    assert "[signals] request=- " in logs.text


def test_handling_a_request_writes_no_file(monkeypatch):
    writes = []
    real_open, real_os_open = builtins.open, os.open

    def guarded_open(file, mode="r", *args, **kwargs):
        if any(flag in mode for flag in ("w", "a", "x", "+")):
            writes.append(str(file))
        return real_open(file, mode, *args, **kwargs)

    def guarded_os_open(path, flags, *args, **kwargs):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND):
            writes.append(str(path))
        return real_os_open(path, flags, *args, **kwargs)

    with app_client() as client:
        monkeypatch.setattr(builtins, "open", guarded_open)
        monkeypatch.setattr(os, "open", guarded_os_open)
        for _ in range(3):
            _post(client, _request())
        client.get("/health")
        client.get("/models")
        monkeypatch.undo()
    assert writes == []
