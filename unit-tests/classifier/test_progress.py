"""Job progress in the classifier: the gauge the queue runs every job under.

What is pinned:

  * the scheduler plans the "units" stage at Σ steps — one per unit, TWO for
    an llm criterion with ``options.boxes`` (scored, then located) — and every
    unit reaches its full count however it ended: located, short-circuited
    by a low score, or skipped by its dependency gate;
  * ``run_assess`` plans one "load" step per document, credited from the
    worker threads;
  * the pipeline enters "artifacts", and the job ends ``done`` at 100;
  * end to end (``postgres``-marked, through the app): the queued event at
    attempt 0, the run's history at attempt 1 from ``started`` to ``done``,
    the snapshot on ``GET /jobs/{id}`` as ``metadata.progress``, a reference
    whose criteria were all supplied reaching 100 through "finalize" alone,
    a failed job ending ``failed``, and ``DELETE /jobs/{id}`` taking the
    history with it.

The pure tests drive ``analysis.analyze_document`` / ``jobs.runners.run_assess``
under a ``ProgressGauge`` with a list-backed fake store, with the vision model
scripted at the transport (``llm.client._send``) like test_scheduler.py.

Run with::

    TEST_POSTGRES_DSN=postgresql://postgres@localhost:5432/postgres \\
    UV_LINK_MODE=copy uv run --no-sync --with pytest --package classifier \\
        python -m pytest unit-tests/classifier/test_progress.py -q -p no:cacheprovider
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
import uuid

import numpy as np
import pytest

import analysis
from api.schemas import AssessRequest, CriterionInput
from common.jobs.progress import ProgressGauge
from jobs import runners
from jobs.payloads import SubmittedDocument, build_assess_payload
from jobs.queue import STAGES
from llm import boxes as llm_boxes
from llm import client as llm_client


class FakeStore:
    """Records every flush the gauge sends."""

    def __init__(self) -> None:
        self.flushes: list[tuple[dict, list[dict]]] = []

    async def flush(self, job_id, snapshot, events):
        self.flushes.append((snapshot, list(events)))

    async def queued(self, job_id, *, stages=()):
        pass

    async def next_attempt(self, job_id):
        return 1

    def events(self) -> list[dict]:
        return [e for _, events in self.flushes for e in events]

    def last(self) -> dict:
        return self.flushes[-1][0]


class Model:
    """Every answer is a score AND a box, so it serves as a scoring, ask and
    verify answer alike; ``score_for`` sets a criterion's scoring answer."""

    def __init__(self, score_for=None):
        self.score_for = dict(score_for or {})
        self.prompts: list[str] = []

    async def __call__(self, prompt):
        text = json.dumps(prompt)
        self.prompts.append(text)
        # The prompt is JSON-dumped, so a line break reads as a backslash.
        match = re.search(r'CRITERION: ([^\\\n"]+)', text)
        score = self.score_for.get(match.group(1), 9) if match else 9
        body = {"score": score, "verdict": "x", "confidence": 80, "reason": "scripted",
                "bbox": [100, 100, 400, 400]}
        return {"choices": [{"message": {"content": json.dumps(body)}}]}

    def asked(self, name: str) -> bool:
        return any(f"CRITERION: {name}" in p for p in self.prompts)


@pytest.fixture
def model(monkeypatch):
    fake = Model(score_for={"absent thing": 3, "gate": 2})
    monkeypatch.setattr(llm_client, "_send", fake)
    monkeypatch.setattr(llm_boxes, "LLM_BBOX_REFINE", False)
    monkeypatch.setattr(llm_boxes, "LLM_BBOX_GRIDLINES", False)
    return fake


def _png(width=300, height=200) -> bytes:
    import cv2

    return cv2.imencode(".png", np.full((height, width, 3), 170, dtype=np.uint8))[1].tobytes()


def _llm(name, **kw):
    return CriterionInput(name=name, type="llm", **kw)


def _units(snapshot: dict) -> dict:
    return next(s for s in snapshot["stages"] if s["name"] == "units")


# ---------------------------------------------------------------------------
# Under a gauge, no database
# ---------------------------------------------------------------------------


def test_units_are_planned_and_counted_in_steps(model):
    criteria = [
        _llm("located thing", options={"hint": "presence", "boxes": True}),  # 2: scored, located
        _llm("absent thing", options={"hint": "presence", "boxes": True}),   # 2: loop short-circuits
        _llm("plain"),                                                       # 1
        _llm("gate"),                                                        # 1: FAILs
        _llm("after", depends_on="gate"),                                    # 1: skipped
    ]
    store = FakeStore()
    gauge = ProgressGauge("j", STAGES["assess"], store=store, flush_interval=0.01)
    job_id = uuid.uuid4().hex[:12]

    async def main():
        doc = analysis.load_document_bytes(_png(), "page.png", None, keep_source=True)
        async with gauge.running():
            return await analysis.analyze_document(doc, criteria, job_id=job_id)

    result = asyncio.run(main())
    entries = result["assessment"]["per_criterion_scores"]
    assert entries["located thing"]["localization"]["accepted_attempt"] == 1
    assert entries["absent thing"]["regions"] == []
    assert entries["after"]["status"] == "skipped" and not model.asked("after")

    final = store.last()
    assert final["state"] == "done" and final["percent"] == 100
    units = _units(final)
    assert (units["index"], units["length"]) == (7, 7)

    events = store.events()
    labels = [e["label"] for e in events]
    # The scoring checkpoint of each boxed unit is its own step...
    scored = [e for e in events if e["label"] == "located thing: scored"]
    assert len(scored) == 1 and scored[0]["stage"] == "units"
    # ...and every unit's exit is logged with its label, the skipped one too.
    for name in ("located thing", "absent thing", "plain", "gate", "after"):
        assert f"{name} · item 0" in labels
    # The unit counter only ever rises, and ends at the length.
    unit_index = [e["index"] for e in events if e["stage"] == "units"]
    assert unit_index == sorted(unit_index) and unit_index[-1] == 7
    assert "writing artifacts" in labels
    assert [e["percent"] for e in events] == sorted(e["percent"] for e in events)
    assert events[-1]["state"] == "done"


def test_a_one_step_unit_turns_the_scoring_checkpoint_into_a_label(model):
    store = FakeStore()
    gauge = ProgressGauge("j", STAGES["assess"], store=store)

    async def main():
        doc = analysis.load_document_bytes(_png(), "page.png", None)
        async with gauge.running():
            await analysis.analyze_document(doc, [_llm("plain"), _llm("other")])

    asyncio.run(main())
    scored = [e for e in store.events() if e["label"] == "plain: scored"]
    assert len(scored) == 1
    units = _units(store.last())
    assert units["length"] == 2 and units["index"] == 2


def test_run_assess_plans_one_load_step_per_document(model):
    request = AssessRequest.model_validate({
        "documents": [{"type": "text", "data": "Notice to Owner"},
                      {"type": "text", "data": "Second page"}],
        "criteria": [{"name": "Notice to Owner", "type": "text"}],
    })
    payload = build_assess_payload(request, [
        SubmittedDocument(raw=b"Notice to Owner", filename="a.txt", content_type=None,
                          kind="txt", pages=1),
        SubmittedDocument(raw=b"Second page", filename="b.txt", content_type=None,
                          kind="txt", pages=1),
    ])
    store = FakeStore()
    gauge = ProgressGauge("j", STAGES["assess"], store=store)

    async def main():
        async with gauge.running():
            return await runners.run_assess(payload)

    asyncio.run(main())
    load = next(s for s in store.last()["stages"] if s["name"] == "load")
    assert (load["index"], load["length"]) == (2, 2)
    load_labels = {e["label"] for e in store.events() if e["stage"] == "load"}
    assert load_labels == {"a.txt", "b.txt"}
    # A text criterion on two documents is two one-step units.
    assert _units(store.last())["length"] == 2


def test_without_a_gauge_nothing_changes(model):
    """A direct library call runs exactly as before — every hook is a no-op."""
    doc = analysis.load_document_bytes(_png(), "page.png", None)
    result = asyncio.run(analysis.analyze_document(
        doc, [_llm("thing", options={"hint": "presence", "boxes": True})]
    ))
    assert result["assessment"]["per_criterion_scores"]["thing"]["status"] == "ok"


# ---------------------------------------------------------------------------
# End to end, through the app (classifier-db)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client(need_postgres):
    from fastapi.testclient import TestClient

    import main

    with TestClient(main.app) as c:
        yield c


def _wait_job(client, job_id: str, timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/jobs/{job_id}").json()
        if job.get("phase") in ("completed", "failed"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish: {job}")


def _doc() -> dict:
    return {"type": "base64", "data": base64.b64encode(_png()).decode("ascii"),
            "filename": "page.png"}


def _history_rows(job_id: str) -> int:
    from db import database

    async def count():
        async with database.acquire() as conn:
            return await conn.fetchval(
                "SELECT count(*) FROM job_progress WHERE job_id = $1", job_id
            )

    return asyncio.run(count())


@pytest.mark.postgres
def test_an_assess_job_is_queued_then_runs_to_done(client, model):
    r = client.post("/assess", json={
        "document": _doc(),
        "criteria": [{"name": "located thing", "type": "llm",
                      "options": {"hint": "presence", "boxes": True}},
                     {"name": "plain", "type": "llm"}],
    })
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    job = _wait_job(client, job_id)
    assert job["phase"] == "completed", job

    # The snapshot is on the job row itself.
    snap = job["metadata"]["progress"]
    assert snap["state"] == "done" and snap["percent"] == 100 and snap["attempt"] == 1
    assert [s["name"] for s in snap["stages"]] == ["load", "units", "artifacts"]
    assert _units(snap)["length"] == 3  # boxed llm 2 + plain 1

    body = client.get(f"/jobs/{job_id}/progress").json()
    assert body["job_id"] == job_id and body["phase"] == "completed"
    assert body["attempt"] == 1 and body["progress"] == snap
    events = body["events"]
    assert events[0]["label"] == "started" and events[0]["state"] == "running"
    assert events[-1]["label"] == "done" and events[-1]["state"] == "done"
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    stages_seen = [e["stage"] for e in events if e["stage"]]
    assert stages_seen.index("load") < stages_seen.index("units") < stages_seen.index("artifacts")
    assert "located thing: scored" in {e["label"] for e in events}
    assert body["next_after"] == events[-1]["seq"]

    queued = client.get(f"/jobs/{job_id}/progress", params={"attempt": 0}).json()
    assert [(e["seq"], e["state"]) for e in queued["events"]] == [(1, "queued")]

    page = client.get(f"/jobs/{job_id}/progress", params={"after": 1, "limit": 2}).json()
    assert [e["seq"] for e in page["events"]] == [2, 3]

    # DELETE takes the history with the row (ON DELETE CASCADE).
    assert _history_rows(job_id) > 0
    assert client.delete(f"/jobs/{job_id}").status_code == 204
    assert _history_rows(job_id) == 0
    assert client.get(f"/jobs/{job_id}/progress").status_code == 404


@pytest.mark.postgres
def test_a_fully_supplied_reference_reaches_100_through_finalize(client, model):
    house = {"name": "has a house", "type": "llm", "options": {"hint": "presence"}}
    r = client.post("/references", json={
        "document": _doc(),
        "criteria": [house],
        "breakdown": {"has a house": {"score": 10, "verdict": "PASS"}},
        "regions": {"has a house": [{"box": [10, 20, 110, 120]}]},
        "description": "given",
    })
    assert r.status_code == 202, r.text
    job = _wait_job(client, r.json()["job_id"])
    assert job["phase"] == "completed", job
    snap = job["metadata"]["progress"]
    assert snap["state"] == "done" and snap["percent"] == 100
    assert [s["name"] for s in snap["stages"]] == ["load", "units", "artifacts", "finalize"]
    assert not model.prompts  # nothing ran through the pipeline

    events = client.get(f"/jobs/{job['job_id']}/progress").json()["events"]
    assert not any(e["stage"] == "units" for e in events)
    finalize = [e["label"] for e in events if e["stage"] == "finalize"]
    assert finalize[:2] == ["description given", "reference files written"]


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_a_failed_job_ends_failed(monkeypatch):
    from common.jobs.postgres import PostgresRegistry

    from api.schemas import ClassifierMetadata
    from db import database
    from jobs import queue as queue_module

    registry = PostgresRegistry(pool=database)
    await registry.init()
    q = queue_module.ClassifierQueue(registry)
    await q.progress.init()

    async def explode(payload):
        raise RuntimeError("an undecodable page")

    monkeypatch.setattr(queue_module, "run_assess", explode)
    # Left in "staging" (enqueue's steps, minus the flip to "pending"): the
    # module's app may still be running workers on the same table, and one of
    # them must not claim this row first.
    job_id = await registry.register(
        ClassifierMetadata(type="assess", request_id="test"), initial_phase="staging"
    )
    await q.payloads.write(job_id, {"x": 1})
    await q.progress.queued(job_id, stages=queue_module.STAGES["assess"])
    queued = (await registry.get(job_id)).metadata["progress"]
    assert queued["state"] == "queued" and queued["attempt"] == 0
    with pytest.raises(RuntimeError):
        await q.handle_job(await registry.get(job_id))
    snap = (await registry.get(job_id)).metadata["progress"]
    assert snap["state"] == "failed" and snap["attempt"] == 1
    assert snap["label"] == "failed: RuntimeError: an undecodable page"
    assert await q.progress.next_attempt(job_id) == 2
    await registry.delete(job_id)
