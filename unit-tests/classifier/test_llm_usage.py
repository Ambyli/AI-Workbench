"""Per-call model usage records (llm/usage.py) and the routes that read them.

No vLLM: the model is scripted at the bare transport ``llm.client._send`` —
where every other suite scripts it — so ``_post`` (the slot, the timer, the
usage row) runs for real. Every test that writes rows swaps
``llm.usage.usage_store`` for a fresh store on the session's Postgres
database with ``llm_calls`` emptied first, so the counts here are exact. The
whole module needs that database (``TEST_POSTGRES_DSN``; see conftest.py).

Pinned:

  * an ok call writes one row with every token field, the finish reason, the
    models and the raw ``usage`` verbatim;
  * an error call writes ``outcome: "error"`` with ``http_status`` and the
    exception, and the call still raises;
  * a failed write (or a row that cannot even be built) never fails the
    call — ``classifier_llm_usage_write_errors_total`` counts it — and the
    write happens after the ``LLM_CALLS`` slot is released;
  * the context vars attribute job, criterion and item; two concurrent units
    never cross-attribute; the ``references: "auto"`` selection call carries
    the item and no criterion;
  * ``totals()`` sums, groups by kind / outcome / criterion; ``summary()``
    filters by time and job type and groups by day;
  * ``delete_job`` removes a job's rows; a TTL sweep of the expired job
    leaves them;
  * end to end through the app: ``result.usage`` equals the sum of
    ``GET /jobs/{id}/usage``'s calls, ``GET /usage`` aggregates, and
    ``DELETE /jobs/{id}`` empties it.

Run with::

    TEST_POSTGRES_DSN=postgresql://postgres@localhost:5432/postgres \\
    UV_LINK_MODE=copy uv run --no-sync --with pytest --package classifier \\
        python -m pytest unit-tests/classifier/test_llm_usage.py -q -p no:cacheprovider
"""

from __future__ import annotations

import asyncio
import base64
import json
import pathlib
import tempfile
import time

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

import llm.client as llm_client
from db import database
from llm import usage as llm_usage

pytestmark = pytest.mark.postgres

FULL_USAGE = {
    "prompt_tokens": 1200,
    "completion_tokens": 300,
    "total_tokens": 1500,
    "prompt_tokens_details": {"cached_tokens": 1024, "multimodal_tokens": {"image": 800}},
    "completion_tokens_details": {"reasoning_tokens": 250},
}


def _response(content: str = '{"score": 9}', usage=None, *, model="muse-glimmer",
              finish_reason="stop") -> dict:
    data = {"model": model,
            "choices": [{"message": {"content": content}, "finish_reason": finish_reason}]}
    if usage is not None:
        data["usage"] = usage
    return data


def _errors() -> float:
    return REGISTRY.get_sample_value("classifier_llm_usage_write_errors_total") or 0.0


def _run(coro):
    return asyncio.run(coro)


async def _sql(statement: str) -> None:
    async with database.acquire() as conn:
        await conn.execute(statement)


@pytest.fixture
def store(monkeypatch):
    """A fresh usage store on an EMPTY ``llm_calls``, swapped in for the
    process one. The table is the session database's (the one every other
    test's model calls also write to), emptied first so counts are exact."""
    fresh = llm_usage.UsageStore(database)
    _run(fresh.init())
    _run(_sql("TRUNCATE llm_calls"))
    monkeypatch.setattr(llm_usage, "usage_store", fresh)
    return fresh


async def _in_job(job_id: str, coro_fn, *, job_type: str = "assess"):
    """Run ``coro_fn()`` with the job context set, as handle_job does."""
    t1 = llm_usage.job_id_var.set(job_id)
    t2 = llm_usage.job_type_var.set(job_type)
    try:
        return await coro_fn()
    finally:
        llm_usage.job_type_var.reset(t2)
        llm_usage.job_id_var.reset(t1)


# ---------------------------------------------------------------------------
# One request → one row
# ---------------------------------------------------------------------------


def test_an_ok_call_writes_one_row_with_every_field(store, monkeypatch):
    async def send(prompt):
        return _response(usage=FULL_USAGE)

    monkeypatch.setattr(llm_client, "_send", send)
    prompt = {"model": "muse-glimmer", "max_tokens": 8192, "messages": []}

    async def call():
        with llm_usage.unit_scope(criterion="has a house", item=2, document=1):
            return await llm_client.call_vllm(prompt, label="score/has a house")

    assert _run(_in_job("job-ok", call)) == {"score": 9}
    (row,) = _run(store.calls("job-ok"))
    assert row["job_id"] == "job-ok" and row["job_type"] == "assess"
    assert (row["criterion"], row["item"], row["document"]) == ("has a house", 2, 1)
    assert row["label"] == "score/has a house" and row["kind"] == "score"
    assert row["attempt"] == 1 and row["outcome"] == "ok"
    assert row["model_requested"] == "muse-glimmer" and row["model_reported"] == "muse-glimmer"
    assert row["api_url"] == llm_client.VISION_LLM_API
    assert row["max_tokens"] == 8192 and row["finish_reason"] == "stop"
    assert row["http_status"] is None and row["error"] is None
    assert row["seconds"] >= 0 and row["started_at"].endswith("+00:00")
    assert (row["prompt_tokens"], row["cached_tokens"], row["completion_tokens"],
            row["reasoning_tokens"], row["total_tokens"]) == (1200, 1024, 300, 250, 1500)
    # The raw usage object, verbatim — the multimodal count has no column.
    assert row["usage"] == FULL_USAGE


def test_every_parse_retry_is_its_own_row(store, monkeypatch):
    answers = iter([_response("not json", {"prompt_tokens": 5, "completion_tokens": 7}),
                    _response('{"score": 3}', {"prompt_tokens": 5, "completion_tokens": 2})])

    async def send(prompt):
        return next(answers)

    monkeypatch.setattr(llm_client, "_send", send)
    _run(_in_job("job-retry", lambda: llm_client.call_vllm_json({}, label="verify/x#1")))
    rows = _run(store.calls("job-retry"))
    assert [(r["attempt"], r["completion_tokens"]) for r in rows] == [(1, 7), (2, 2)]
    assert {r["kind"] for r in rows} == {"verify"}


def test_an_http_error_is_recorded_and_still_raises(store, monkeypatch):
    request = httpx.Request("POST", "http://muse-glimmer:8000/v1/chat/completions")

    async def overloaded(prompt):
        raise httpx.HTTPStatusError(
            "503 Service Unavailable", request=request,
            response=httpx.Response(503, request=request),
        )

    monkeypatch.setattr(llm_client, "_send", overloaded)
    with pytest.raises(llm_client.LLMCallError):
        _run(_in_job("job-503", lambda: llm_client.call_vllm(
            {"model": "muse-glimmer"}, label="score/x")))
    (row,) = _run(store.calls("job-503"))
    assert row["outcome"] == "error" and row["http_status"] == 503
    assert row["error"].startswith("HTTPStatusError: 503")
    assert row["prompt_tokens"] is None and row["usage"] is None
    assert row["model_requested"] == "muse-glimmer" and row["model_reported"] is None


def test_a_transport_error_has_no_status_but_is_recorded(store, monkeypatch):
    async def down(prompt):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(llm_client, "_send", down)
    assert _run(_in_job("job-down", lambda: llm_client.call_vllm_json(
        {}, label="bbox/x#1"))) is None
    (row,) = _run(store.calls("job-down"))
    assert row["outcome"] == "error" and row["http_status"] is None
    assert row["error"] == "ConnectError: connection refused" and row["kind"] == "ask"


def test_a_failed_write_never_fails_the_call(store, monkeypatch):
    async def send(prompt):
        return _response(usage=FULL_USAGE)

    async def broken_insert(row):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(llm_client, "_send", send)
    monkeypatch.setattr(store, "_insert", broken_insert)
    before = _errors()
    assert _run(llm_client.call_vllm({}, label="score/x")) == {"score": 9}
    assert _errors() - before == 1


def test_a_row_that_cannot_be_built_never_fails_the_call(store, monkeypatch):
    async def send(prompt):
        return _response()

    def broken_build(**_fields):
        raise ValueError("odd prompt")

    monkeypatch.setattr(llm_client, "_send", send)
    monkeypatch.setattr(llm_usage, "build_row", broken_build)
    before = _errors()
    assert _run(llm_client.call_vllm({}, label="score/x")) == {"score": 9}
    assert _errors() - before == 1


def test_the_write_happens_after_the_slot_is_released(store, monkeypatch):
    seen: list[int] = []

    async def send(prompt):
        return _response()

    original = store.record

    async def record(row):
        seen.append(llm_client.LLM_CALLS.in_flight)
        return await original(row)

    monkeypatch.setattr(llm_client, "_send", send)
    monkeypatch.setattr(store, "record", record)
    _run(llm_client.call_vllm({}, label="score/x"))
    assert seen == [0]


def test_a_call_outside_a_job_is_recorded_with_no_job(store, monkeypatch):
    async def send(prompt):
        return _response(usage={"prompt_tokens": 1, "completion_tokens": 1})

    monkeypatch.setattr(llm_client, "_send", send)
    _run(llm_client.call_vllm({}, label="reference/describe"))
    summary = _run(store.summary())
    assert summary["totals"]["calls"] == 1 and summary["totals"]["jobs"] == 0


# ---------------------------------------------------------------------------
# Attribution through the scheduler
# ---------------------------------------------------------------------------


def test_concurrent_units_never_cross_attribute(store, monkeypatch):
    """Two units in flight at once, each its own task — as under gather."""
    gate = asyncio.Event()
    arrived: list[str] = []

    async def send(prompt):
        arrived.append(prompt["who"])
        if len(arrived) == 2:
            gate.set()
        await gate.wait()  # both requests are in flight before either returns
        return _response()

    monkeypatch.setattr(llm_client, "_send", send)

    async def unit(name: str, item: int):
        with llm_usage.unit_scope(criterion=name, item=item, document=0):
            await asyncio.sleep(0)  # let the other unit set its own scope
            return await llm_client.call_vllm({"who": name}, label=f"score/{name}")

    async def job():
        await asyncio.gather(unit("a", 0), unit("b", 1))

    _run(_in_job("job-two", job))
    rows = _run(store.calls("job-two"))
    assert sorted((r["label"], r["criterion"], r["item"]) for r in rows) == [
        ("score/a", "a", 0), ("score/b", "b", 1),
    ]


def test_the_scheduler_attributes_criterion_item_and_the_selection_call(store, monkeypatch):
    """run_units with stand-in evaluators and a stand-in references plan: the
    selection call carries its item and no criterion, every unit's call its
    criterion and item."""
    from types import SimpleNamespace

    from analysis import scheduler
    from analysis.outcome import Outcome
    from api.schemas import CriterionInput

    async def send(prompt):
        return _response(usage={"prompt_tokens": 10, "completion_tokens": 1})

    monkeypatch.setattr(llm_client, "_send", send)

    async def fake_llm(c, ctx):
        await llm_client.call_vllm({}, label=f"score/{c.name}")
        return Outcome(status="ok", method="llm", score=9, verdict="PASS",
                       confidence=80, reason="ok")

    monkeypatch.setitem(scheduler.EVALUATORS, "llm", fake_llm)

    class Refs:
        def __init__(self):
            self.selected: dict[int, asyncio.Future] = {}

        def needs_selection(self, c):
            return True

        async def select(self, ctx):
            if ctx.item not in self.selected:
                self.selected[ctx.item] = asyncio.get_running_loop().create_future()
                await llm_client.call_vllm({}, label=f"reference/select#{ctx.item}")
                self.selected[ctx.item].set_result(None)
            return await self.selected[ctx.item]

        def finish(self, c, ctx, outcome):
            return outcome

    refs = Refs()
    items = [SimpleNamespace(item=n, document=0, references=refs) for n in (0, 1)]
    groups = [SimpleNamespace(index=0, items=items)]
    criteria = [CriterionInput.model_validate({"name": n, "type": "llm"}) for n in ("roof", "door")]

    _run(_in_job("job-sched", lambda: scheduler.run_units(criteria, items, groups)))
    rows = _run(store.calls("job-sched"))
    got = sorted((r["kind"], r["criterion"] or "", r["item"]) for r in rows)
    assert got == [
        ("score", "door", 0), ("score", "door", 1),
        ("score", "roof", 0), ("score", "roof", 1),
        ("select", "", 0), ("select", "", 1),
    ]
    assert {r["document"] for r in rows} == {0}
    assert {r["job_id"] for r in rows} == {"job-sched"}


# ---------------------------------------------------------------------------
# The store's reads, delete, and retention
# ---------------------------------------------------------------------------


def _row(job_id, *, kind="score", criterion="c", outcome="ok", seconds=1.0, prompt=10,
         cached=None, completion=5, reasoning=None, started_at=None, job_type="assess",
         model="muse-glimmer"):
    return {
        "job_id": job_id, "job_type": job_type, "started_at": started_at or llm_usage.now_iso(),
        "label": f"{kind}/{criterion}", "kind": kind, "criterion": criterion, "item": 0,
        "document": 0, "attempt": 1, "model_requested": model, "model_reported": model,
        "outcome": outcome, "seconds": seconds, "prompt_tokens": prompt,
        "cached_tokens": cached, "completion_tokens": completion,
        "reasoning_tokens": reasoning,
        "total_tokens": None if prompt is None else prompt + (completion or 0),
    }


def test_totals_sum_and_group(store):
    for row in (
        _row("j", kind="score", criterion="a", seconds=1.5, prompt=100, completion=20, cached=64),
        _row("j", kind="score", criterion="b", seconds=2.0, prompt=200, completion=30),
        _row("j", kind="verify", criterion="a", seconds=0.5, prompt=50, completion=5),
        _row("j", kind="select", criterion=None, outcome="error", seconds=0.25,
             prompt=None, completion=None),
        _row("other", prompt=9999),
    ):
        assert _run(store.record(row))

    out = _run(store.totals("j"))
    t = out["totals"]
    assert (t["calls"], t["errors"], t["seconds"]) == (4, 1, 4.25)
    assert (t["prompt_tokens"], t["completion_tokens"], t["total_tokens"]) == (350, 55, 405)
    assert t["cached_tokens"] == 64          # only one call reported it
    assert t["reasoning_tokens"] is None     # none did — null, not 0
    assert t["models"] == ["muse-glimmer"]
    assert t["first_call_at"] <= t["last_call_at"]
    assert set(out["by_kind"]) == {"score", "verify", "select"}
    assert out["by_kind"]["score"]["calls"] == 2 and out["by_kind"]["score"]["prompt_tokens"] == 300
    assert out["by_outcome"]["error"]["calls"] == 1 and out["by_outcome"]["ok"]["calls"] == 3
    by_criterion = {b["criterion"]: b for b in out["by_criterion"]}
    assert set(by_criterion) == {None, "a", "b"}
    assert by_criterion["a"]["calls"] == 2 and by_criterion["a"]["prompt_tokens"] == 150
    assert sum(b["calls"] for b in out["by_criterion"]) == t["calls"]

    usage = _run(store.job_usage("j"))
    assert usage["usage_url"] == "/jobs/j/usage" and usage["calls"] == 4


def test_totals_of_a_job_with_no_calls(store):
    t = _run(store.totals("nothing"))["totals"]
    assert (t["calls"], t["errors"], t["seconds"], t["prompt_tokens"], t["models"]) == (
        0, 0, 0.0, None, [])


def test_summary_filters_by_time_and_type_and_groups_by_day(store):
    for row in (
        _row("a1", started_at="2026-10-06T23:59:59.000000+00:00", prompt=1),
        _row("a1", started_at="2026-10-07T00:00:00.000000+00:00", prompt=2),
        _row("r1", started_at="2026-10-07T12:00:00.000000+00:00", prompt=4,
             job_type="reference", kind="describe"),
        _row("a2", started_at="2026-10-08T08:00:00.000000+00:00", prompt=8),
    ):
        _run(store.record(row))

    everything = _run(store.summary())
    assert everything["totals"]["calls"] == 4 and everything["totals"]["jobs"] == 3
    assert [(d["day"], d["calls"]) for d in everything["by_day"]] == [
        ("2026-10-06", 1), ("2026-10-07", 2), ("2026-10-08", 1)]

    window = _run(store.summary(since="2026-10-07T00:00:00.000000+00:00",
                                until="2026-10-08T00:00:00.000000+00:00"))
    assert window["totals"]["prompt_tokens"] == 6  # since inclusive, until exclusive
    assert set(window["by_kind"]) == {"score", "describe"}

    refs = _run(store.summary(job_type="reference"))
    assert refs["totals"]["calls"] == 1 and refs["totals"]["prompt_tokens"] == 4


def test_delete_job_removes_only_that_jobs_rows(store):
    for job in ("keep", "drop", "drop"):
        _run(store.record(_row(job)))
    assert _run(store.delete_job("drop")) == 2
    assert _run(store.calls("drop")) == []
    assert len(_run(store.calls("keep"))) == 1


def test_a_ttl_sweep_of_the_job_leaves_its_usage(store, monkeypatch):
    """The artifact sweeper prunes the expired job row — and never the usage."""
    from common.jobs.postgres import PostgresRegistry
    from common.vision import ArtifactStore

    from api.schemas import ClassifierMetadata
    from regions import sweeper as sweeper_module

    # The sweeper's own artifact store, on a temp root: pass 1 deletes every
    # directory whose job is not in THIS registry, and the session's shared
    # artifact root holds other tests' jobs.
    root = pathlib.Path(tempfile.mkdtemp(prefix="classifier-usage-sweep-"))
    monkeypatch.setattr(sweeper_module, "store", ArtifactStore(str(root)))

    async def scenario():
        registry = PostgresRegistry(pool=database)  # the same database, as in the container
        await registry.init()
        job_id = await registry.register(ClassifierMetadata(type="assess", request_id="t"))
        await registry.set_result(job_id, {"ok": True})
        await store.record(_row(job_id))
        monkeypatch.setattr(sweeper_module, "JOB_TTL_HOURS", 0)
        # created_at is a TIMESTAMPTZ compared at microsecond resolution; a
        # short pause just keeps "created strictly before the cutoff" honest.
        await asyncio.sleep(0.05)
        out = await sweeper_module.ArtifactSweeper(registry).sweep_once()
        return job_id, out, await registry.get(job_id)

    job_id, out, job = _run(scenario())
    assert out["jobs_removed"] >= 1 and job is None
    assert len(_run(store.calls(job_id))) == 1


def test_record_creates_the_table_on_first_use():
    _run(_sql("DROP TABLE IF EXISTS llm_calls"))
    lazy = llm_usage.UsageStore(database)  # never init()-ed
    assert _run(lazy.record(_row("j")))
    assert len(_run(lazy.calls("j"))) == 1


# ---------------------------------------------------------------------------
# End to end through the app
# ---------------------------------------------------------------------------


class Model:
    """A scoring-only scripted model with usage on every answer."""

    def __init__(self):
        self.calls = 0

    async def __call__(self, prompt):
        self.calls += 1
        body = {"score": 8, "verdict": "PASS", "confidence": 80, "reason": "ok"}
        return _response(json.dumps(body), {
            "prompt_tokens": 1000 + self.calls, "completion_tokens": 40,
            "total_tokens": 1040 + self.calls,
            "completion_tokens_details": {"reasoning_tokens": 30},
        })


def _png() -> str:
    import cv2

    raw = cv2.imencode(".png", np.full((300, 400, 3), 180, dtype=np.uint8))[1].tobytes()
    return base64.b64encode(raw).decode("ascii")


def _wait_job(client, job_id: str, timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/jobs/{job_id}").json()
        if job.get("phase") in ("completed", "failed"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish: {job}")


def test_end_to_end_result_usage_endpoints_and_delete(store, monkeypatch):
    import main

    model = Model()
    monkeypatch.setattr(llm_client, "_send", model)
    since = llm_usage.now_iso()

    with TestClient(main.app) as client:
        r = client.post("/assess", json={
            "document": {"type": "base64", "data": _png(), "filename": "house.png"},
            "criteria": [
                {"name": "has a house", "type": "llm"},
                {"name": "has a roof", "type": "llm"},
                {"name": "Notice", "type": "text"},  # no model call
            ],
        })
        assert r.status_code == 202, r.text
        job_id = r.json()["job_id"]
        job = _wait_job(client, job_id)
        assert job["phase"] == "completed", job

        usage = job["result"]["usage"]
        assert usage["usage_url"] == f"/jobs/{job_id}/usage"
        assert usage["calls"] == model.calls == 2 and usage["errors"] == 0

        body = client.get(f"/jobs/{job_id}/usage").json()
        assert body["job_id"] == job_id and body["job_present"] is True
        assert body["job_type"] == "assess"
        calls = body["calls"]
        assert len(calls) == 2
        assert {c["criterion"] for c in calls} == {"has a house", "has a roof"}
        assert {(c["item"], c["document"], c["kind"]) for c in calls} == {(0, 0, "score")}
        for key in ("prompt_tokens", "completion_tokens", "reasoning_tokens", "total_tokens"):
            assert usage[key] == sum(c[key] for c in calls) == body["totals"][key], key
        assert usage["seconds"] == body["totals"]["seconds"]
        assert body["by_kind"]["score"]["calls"] == 2
        assert {b["criterion"] for b in body["by_criterion"]} == {"has a house", "has a roof"}

        summary = client.get("/usage", params={"since": since, "job_type": "assess"}).json()
        assert summary["filter"]["job_type"] == "assess"
        assert summary["totals"]["calls"] == 2 and summary["totals"]["jobs"] == 1
        assert summary["totals"]["prompt_tokens"] == usage["prompt_tokens"]
        assert summary["by_day"][0]["calls"] == 2
        assert client.get("/usage", params={"job_type": "reference"}).json()["totals"]["calls"] == 0
        assert client.get("/usage", params={"since": "yesterday"}).status_code == 400
        assert client.get("/usage", params={
            "since": "2026-10-08", "until": "2026-10-07"}).status_code == 400

        assert client.delete(f"/jobs/{job_id}").status_code == 204
        assert _run(store.calls(job_id)) == []
        assert client.get(f"/jobs/{job_id}/usage").status_code == 404
        assert client.get("/jobs/never-was/usage").status_code == 404


def test_an_expired_jobs_usage_stays_readable(store, monkeypatch):
    """The job row is gone (as after a TTL sweep); its usage still answers."""
    import main

    _run(store.record(_row("expired-job", prompt=7)))
    with TestClient(main.app) as client:
        body = client.get("/jobs/expired-job/usage").json()
        assert body["job_present"] is False and body["job_type"] == "assess"
        assert body["totals"]["prompt_tokens"] == 7 and len(body["calls"]) == 1
        # DELETE on the expired id still takes the usage rows, then 404s for the row.
        assert client.delete("/jobs/expired-job").status_code == 404
        assert client.get("/jobs/expired-job/usage").status_code == 404
