"""POST /references and the rest of the routes, end to end, with a scripted model.

Same harness as test_classifier_endpoint.py: the app runs in-process through
FastAPI's TestClient with its real worker pool, Postgres queue, artifact store
and reference store (all on the session temp directory conftest.py sets up),
and the vision model is replaced at the transport (``llm.client._send``) so
the process-wide call limit stays in play. The fake model answers by the kind
of call — scoring, box ask, verify, describe — read off the prompt.

What is pinned here:

  * the lifecycle — 202 pending, the creation job, ready, the record and the
    files (page, working copy, one composite per llm-guiding criterion);
  * the merge — the caller's breakdown and regions beat the pipeline, the
    pipeline's answer is kept as ``observed``, ``[]`` is the whole page, a
    fully supplied criterion costs no model call, grid → px, a PDF page;
  * the refusals at submit — .txt, a page that is not there, a region off
    the page, a verdict that disagrees with its score, the count cap;
  * the description — generated only when none was sent;
  * ``from_job`` — 404 / 409 / 410, the job's answers and accepted regions,
    and the legacy rebuild for a job without ``result.request``;
  * PATCH, DELETE (409 while a live job uses it, ``?force=true``), the list
    filters, the files route's name grammar;
  * failure — nothing answered → ``failed``; a pending reference whose job is
    gone is reconciled to ``failed``;
  * retention — after the sweeper runs with a TTL of 0 the creation job is
    gone, and the reference is not.

Run with::

    UV_LINK_MODE=copy uv run pytest unit-tests/classifier/test_references_api.py -q
"""

from __future__ import annotations

import asyncio
import base64
import json
import pathlib
import time

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

HERE = pathlib.Path(__file__).resolve().parent
DOCS = HERE / "documents"

HOUSE = "has a house"
SHARP = "sharpness"
GOOD_BOX = [100, 200, 300, 400]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client(need_postgres):
    import main

    with TestClient(main.app) as c:
        yield c


class FakeModel:
    """A scripted vision model, answering by call kind; records every call."""

    def __init__(self, scores=None, *, describe="A grey test card with nothing on it.",
                 bbox=GOOD_BOX, verify=9, fail_scoring=False):
        self.scores = dict(scores or {})
        self.describe = describe
        self.bbox = bbox
        self.verify = verify
        self.fail_scoring = fail_scoring
        self.calls: list[tuple[str, dict]] = []

    @staticmethod
    def kind_of(prompt: dict) -> str:
        system = prompt["messages"][0]["content"]
        if "catalogue descriptions" in system:
            return "describe"
        if "You locate features" in system:
            return "bbox"
        if "image crop" in system:
            return "verify"
        return "score"

    def kinds(self) -> list[str]:
        return [k for k, _ in self.calls]

    def scored(self) -> list[str]:
        """The criterion names the scoring calls were about, in order."""
        out = []
        for kind, prompt in self.calls:
            if kind == "score":
                text = next(p["text"] for p in prompt["messages"][1]["content"]
                            if p["type"] == "text")
                out.append(text.split("CRITERION: ", 1)[1].split("\n", 1)[0])
        return out

    async def __call__(self, prompt):
        kind = self.kind_of(prompt)
        self.calls.append((kind, prompt))
        if kind == "describe":
            if self.describe is None:
                raise httpx.ConnectError("describe is down")
            body = {"description": self.describe}
        elif kind == "bbox":
            body = {"bbox": self.bbox, "confidence": 90, "reason": "a house"}
        elif kind == "verify":
            body = {"score": self.verify, "reason": "a house"}
        else:
            if self.fail_scoring:
                raise httpx.ConnectError("model down")
            name = self.scored()[-1]
            score = self.scores.get(name, 9)
            body = {"score": score, "verdict": "PASS", "confidence": 80,
                    "reason": f"model says {score}"}
        return {"choices": [{"message": {"content": json.dumps(body)}}]}


@pytest.fixture
def model(monkeypatch):
    from llm import boxes as llm_boxes
    from llm import client as llm_client

    fake = FakeModel()
    monkeypatch.setattr(llm_client, "_send", fake)
    # One ask + one verify per attempt: the refine pass and the grid are
    # covered by test_llm_boxes.py and only add calls here.
    monkeypatch.setattr(llm_boxes, "LLM_BBOX_REFINE", False)
    monkeypatch.setattr(llm_boxes, "LLM_BBOX_GRIDLINES", False)
    return fake


def _png(width=400, height=300, value=180) -> bytes:
    import cv2

    return cv2.imencode(".png", np.full((height, width, 3), value, dtype=np.uint8))[1].tobytes()


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _doc(raw: bytes | None = None, filename="house.png") -> dict:
    return {"type": "base64", "data": _b64(raw or _png()), "filename": filename}


def _house(**options) -> dict:
    return {"name": HOUSE, "type": "llm", "options": {"hint": "presence", **options}}


def _wait_job(client, job_id: str, timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/jobs/{job_id}").json()
        if job.get("phase") in ("completed", "failed"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish: {job}")


def _wait_ref(client, reference_id: str, timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ref = client.get(f"/references/{reference_id}").json()
        if ref.get("status") in ("ready", "failed"):
            return ref
        time.sleep(0.05)
    raise AssertionError(f"reference {reference_id} did not finish: {ref}")


def _create(client, body: dict, *, expect="ready") -> dict:
    r = client.post("/references", json=body)
    assert r.status_code == 202, r.text
    accepted = r.json()
    assert accepted["status"] == "pending" and accepted["reference_id"].startswith("r")
    ref = _wait_ref(client, accepted["reference_id"])
    assert ref["status"] == expect, ref
    assert ref["job_id"] == accepted["job_id"]
    return ref


def _assess(client, criteria, document=None) -> dict:
    r = client.post("/assess", json={"document": document or _doc(), "criteria": criteria})
    assert r.status_code == 202, r.text
    job = _wait_job(client, r.json()["job_id"])
    assert job["phase"] == "completed", job
    return job


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# The lifecycle
# ---------------------------------------------------------------------------


def test_lifecycle_pending_then_ready_with_record_and_files(client, model):
    ref = _create(client, {
        "document": _doc(),
        "criteria": [_house(), {"name": SHARP, "type": "cv"}],
        "title": "A house",
        "tags": ["Houses", " exterior ", "houses"],
    })
    assert ref["title"] == "A house"
    assert ref["tags"] == ["houses", "exterior"]  # lowercased, stripped, de-duplicated
    assert ref["source"] == {"kind": "document", "job_id": None, "item": None}

    record = ref["record"]
    house = record["criteria"][HOUSE]
    assert house["expected"] == {"score": 9, "verdict": "PASS", "reason": "model says 9",
                                 "source": "pipeline"}
    assert house["region_source"] == "pipeline" and house["usable"] is True
    (region,) = house["regions"]
    # 0-1000 grid on a 400×300 page: [100, 200, 300, 400] → x 40-120, y 60-120.
    assert region["points"] == [[40.0, 60.0], [120.0, 120.0]]
    assert region["source"] == "llm" and region["page"] == 0
    # The cv criterion is stored (it is inherited) but is no example for the llm.
    sharp = record["criteria"][SHARP]
    assert sharp["guides_llm"] is False and sharp["composite"] is None
    assert sharp["observed"]["method"] == "cv"
    assert record["page"]["kind"] == "image" and record["page"]["geometry"]["width"] == 400

    names = {f["name"] for f in ref["files"]}
    assert names == {"page.jpg", "working.jpg", "regions.json", "record.json", house["composite"]}
    composite = client.get(f"/references/{ref['reference_id']}/files/{house['composite']}")
    assert composite.status_code == 200 and composite.headers["content-type"] == "image/jpeg"
    assert composite.content[:3] == b"\xff\xd8\xff"
    stored = client.get(f"/references/{ref['reference_id']}/files/record.json").json()
    assert stored["criteria"][HOUSE]["expected"]["score"] == 9

    # The creation job is an ordinary job with its own artifacts, typed "reference".
    job = _wait_job(client, ref["job_id"])
    assert job["phase"] == "completed"
    assert job["metadata"]["type"] == "reference"
    assert job["metadata"]["reference_id"] == ref["reference_id"]
    assert job["result"]["reference_id"] == ref["reference_id"]
    assert job["result"]["pipeline"]["schema_version"] == 3
    # Boxes were forced on for the presence criterion (no regions supplied).
    run = {c["name"]: c for c in job["result"]["pipeline"]["request"]["criteria"]}
    assert run[HOUSE]["options"]["boxes"] is True
    assert model.kinds().count("bbox") == 1 and model.kinds().count("verify") == 1


def test_the_assess_result_records_its_criteria(client, model):
    job = _assess(client, [_house(), {"name": "after", "type": "llm", "depends_on": HOUSE,
                                      "weight": 2.5}])
    criteria = job["result"]["request"]["criteria"]
    assert [c["name"] for c in criteria] == [HOUSE, "after"]
    assert criteria[1]["depends_on"] == HOUSE and criteria[1]["weight"] == 2.5
    # The assess job's metadata is exactly what it always was.
    assert set(job["metadata"]) == {"type", "request_id"}


def test_caller_answers_beat_the_pipeline_and_observed_is_kept(client, model):
    model.scores[HOUSE] = 3
    ref = _create(client, {
        "document": _doc(),
        "criteria": [_house()],
        "breakdown": {HOUSE: {"score": 10, "reason": "a two-storey house"}},
        "description": "a house photo",
    })
    house = ref["record"]["criteria"][HOUSE]
    assert house["expected"] == {"score": 10, "verdict": "PASS",
                                 "reason": "a two-storey house", "source": "caller"}
    assert house["observed"]["score"] == 3 and house["observed"]["verdict"] == "FAIL"
    # Scored 3, so the box loop never ran: nothing located, the whole page.
    assert house["region_source"] == "whole_page" and house["regions"] == []
    assert any("you said PASS, the model scored the example 3 and located nothing" in w
               for w in ref["record"]["warnings"])
    assert "describe" not in model.kinds()
    assert ref["description"] == "a house photo" and ref["description_source"] == "caller"


def test_fully_supplied_criteria_cost_no_model_call(client, model):
    ref = _create(client, {
        "document": _doc(),
        "criteria": [_house(), {"name": "a roof", "type": "llm",
                                "options": {"hint": "presence"}}],
        "breakdown": {HOUSE: {"score": 10, "verdict": "PASS"},
                      "a roof": {"score": 2, "reason": "flat, no roof visible"}},
        "regions": {HOUSE: [{"box": [10, 20, 110, 120]}], "a roof": []},
        "description": "given",
    })
    assert model.calls == []
    house = ref["record"]["criteria"][HOUSE]
    assert house["region_source"] == "caller" and house["observed"] is None
    (region,) = house["regions"]
    assert region["source"] == "manual" and region["points"] == [[10.0, 20.0], [110.0, 120.0]]
    roof = ref["record"]["criteria"]["a roof"]
    assert roof["region_source"] == "whole_page"
    assert roof["expected"]["verdict"] == "FAIL" and roof["usable"] is True
    job = _wait_job(client, ref["job_id"])
    assert job["result"]["pipeline"] is None and job["result"]["artifacts"] is None


def test_grid_regions_are_converted_to_page_pixels(client, model):
    ref = _create(client, {
        "document": _doc(_png(400, 300)),
        "criteria": [_house()],
        "breakdown": {HOUSE: {"score": 9}},
        "regions": {HOUSE: [{"box": [100, 100, 500, 500]},
                            {"polygon": [[0, 0], [1000, 0], [1000, 1000]]}]},
        "region_units": "grid",
        "description": "given",
    })
    box, poly = ref["record"]["criteria"][HOUSE]["regions"]
    assert box["points"] == [[40.0, 30.0], [200.0, 150.0]]
    assert poly["kind"] == "polygon" and poly["points"] == [[0.0, 0.0], [400.0, 0.0], [400.0, 300.0]]


def test_a_pdf_page_is_one_reference(client, model):
    raw = (DOCS / "invoice_two_page.pdf").read_bytes()
    ref = _create(client, {
        "document": {"type": "base64", "data": _b64(raw), "filename": "two.pdf"},
        "page": 1,
        "criteria": [{"name": "Net 30", "type": "text"}],
        "description": "given",
    })
    page = ref["record"]["page"]
    assert page["kind"] == "pdf" and page["page"] == 1
    assert page["geometry"]["pdf_points"] is not None
    # A text criterion is stored, located by its own path, but guides nothing.
    entry = ref["record"]["criteria"]["Net 30"]
    assert entry["guides_llm"] is False and entry["observed"]["method"] == "text"
    # The page that was not asked for is a 400.
    r = client.post("/references", json={
        "document": {"type": "base64", "data": _b64(raw)}, "page": 2,
        "criteria": [{"name": "Net 30", "type": "text"}],
    })
    assert r.status_code == 400 and "page 2 is not in" in r.json()["detail"]


def test_multipart_is_the_same_request(client, model):
    r = client.post(
        "/references",
        files={"file": ("house.png", _png(), "image/png")},
        data={
            "criteria": json.dumps([_house()]),
            "breakdown": json.dumps({HOUSE: {"score": 8}}),
            "regions": json.dumps({HOUSE: []}),
            "tags": ["one", "two"],
            "description": "given",
        },
    )
    assert r.status_code == 202, r.text
    ref = _wait_ref(client, r.json()["reference_id"])
    assert ref["status"] == "ready" and ref["tags"] == ["one", "two"]
    assert ref["record"]["criteria"][HOUSE]["expected"]["score"] == 8
    assert model.calls == []


# ---------------------------------------------------------------------------
# Refusals at submit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("body, needle", [
    ({"document": {"type": "text", "data": "just words"}, "criteria": [_house()]},
     "has no page image"),
    ({"document": _doc(), "criteria": [_house()],
      "regions": {HOUSE: [{"box": [10, 10, 500, 100]}]}},
     "falls outside the 400×300 page"),
    ({"document": _doc(), "criteria": [_house()],
      "regions": {HOUSE: [{"box": [10, 10, 1200, 100]}]}, "region_units": "grid"},
     "grid coordinates run 0-1000"),
    ({"document": _doc(), "criteria": [_house()],
      "breakdown": {HOUSE: {"score": 3, "verdict": "PASS"}}},
     "verdict PASS does not match score 3"),
    ({"document": _doc(), "criteria": [_house()], "breakdown": {"nope": {"score": 3}}},
     "breakdown names ['nope']"),
    ({"document": _doc(), "criteria": [_house()], "page": 1}, "an image has one page"),
    ({"document": _doc()}, "'criteria' is required"),
    ({"document": _doc(), "from_job": "abcdefabcdef", "criteria": [_house()]},
     "exactly one of 'document' or 'from_job'"),
    ({"document": _doc(), "criteria": [_house()],
      "regions": {HOUSE: [{"box": [30, 10, 20, 50]}]}}, "x1 < x2"),
])
def test_malformed_input_is_a_400_at_submit(client, model, body, needle):
    r = client.post("/references", json=body)
    assert r.status_code == 400, r.text
    assert needle in r.json()["detail"]
    assert model.calls == []


def test_the_count_cap_is_a_409(client, model, monkeypatch):
    from api import references as references_api
    from references.store import reference_registry

    existing = _run(reference_registry.count())
    monkeypatch.setattr(references_api, "REFERENCE_MAX_COUNT", existing)
    r = client.post("/references", json={"document": _doc(), "criteria": [_house()]})
    assert r.status_code == 409 and "CLASSIFIER_REFERENCE_MAX_COUNT" in r.json()["detail"]


# ---------------------------------------------------------------------------
# The description
# ---------------------------------------------------------------------------


def test_a_description_is_generated_only_when_none_was_sent(client, model):
    generated = _create(client, {"document": _doc(), "criteria": [_house()],
                                 "breakdown": {HOUSE: {"score": 9}}, "regions": {HOUSE: []}})
    assert model.kinds() == ["describe"]
    assert generated["description"] == "A grey test card with nothing on it."
    assert generated["description_source"] == "model"

    model.calls.clear()
    given = _create(client, {"document": _doc(), "criteria": [_house()],
                             "breakdown": {HOUSE: {"score": 9}}, "regions": {HOUSE: []},
                             "description": "mine"})
    assert model.calls == []
    assert (given["description"], given["description_source"]) == ("mine", "caller")


def test_a_failed_description_still_readies_the_reference(client, model):
    model.describe = None
    ref = _create(client, {"document": _doc(), "criteria": [_house()],
                           "breakdown": {HOUSE: {"score": 9}}, "regions": {HOUSE: []}})
    assert ref["description"] is None and ref["description_source"] is None
    assert any("could not be described" in w for w in ref["record"]["warnings"])


# ---------------------------------------------------------------------------
# from_job
# ---------------------------------------------------------------------------


def test_from_job_saves_a_reviewed_job_item(client, model):
    job = _assess(client, [_house(boxes=True)])
    model.calls.clear()
    ref = _create(client, {"from_job": job["job_id"], "description": "from a job"})
    assert ref["source"] == {"kind": "from_job", "job_id": job["job_id"], "item": 0}
    house = ref["record"]["criteria"][HOUSE]
    assert house["expected"]["source"] == "pipeline" and house["expected"]["score"] == 9
    assert house["observed"]["job_id"] == job["job_id"]
    assert house["region_source"] == "pipeline"
    assert house["regions"][0]["points"] == [[40.0, 60.0], [120.0, 120.0]]
    # The job's answer and its accepted box were enough: no model call at all.
    assert model.calls == []
    # The page is the job's base image, at its original size.
    assert ref["record"]["page"]["geometry"]["width"] == 400


def test_from_job_caller_breakdown_still_wins(client, model):
    job = _assess(client, [_house(boxes=True)])
    ref = _create(client, {
        "from_job": job["job_id"],
        "breakdown": {HOUSE: {"score": 4, "reason": "only a shed"}},
        "description": "x",
    })
    house = ref["record"]["criteria"][HOUSE]
    assert house["expected"]["source"] == "caller" and house["expected"]["verdict"] == "MARGINAL"
    assert house["observed"]["score"] == 9


def test_from_job_refusals(client, model):
    from jobs.queue import jobs_registry

    from api.schemas import ClassifierMetadata

    r = client.post("/references", json={"from_job": "000000000000"})
    assert r.status_code == 404

    staging = _run(jobs_registry.register(
        ClassifierMetadata(type="assess", request_id="t"), initial_phase="staging"))
    r = client.post("/references", json={"from_job": staging})
    assert r.status_code == 409 and "COMPLETED" in r.json()["detail"]

    job = _assess(client, [_house(boxes=True)])
    r = client.post("/references", json={"from_job": job["job_id"], "from_job_item": 3})
    assert r.status_code == 400

    assert client.delete(f"/jobs/{job['job_id']}/artifacts").status_code == 204
    r = client.post("/references", json={"from_job": job["job_id"]})
    assert r.status_code == 410 and "artifacts are gone" in r.json()["detail"]


def test_from_job_rebuilds_a_legacy_job_with_a_warning(client, model):
    from jobs.queue import jobs_registry

    job = _assess(client, [_house(boxes=True)])
    legacy = dict(job["result"])
    legacy.pop("request")
    _run(jobs_registry.set_result(job["job_id"], legacy))
    ref = _create(client, {"from_job": job["job_id"], "description": "x"})
    assert any("predates result.request" in w for w in ref["record"]["warnings"])
    house = ref["record"]["criteria"][HOUSE]
    assert house["input"]["options"]["boxes"] is True
    assert house["expected"]["source"] == "pipeline"


# ---------------------------------------------------------------------------
# PATCH, DELETE, list, files
# ---------------------------------------------------------------------------


def _quick(client, **extra) -> dict:
    return _create(client, {"document": _doc(), "criteria": [_house()],
                            "breakdown": {HOUSE: {"score": 9}}, "regions": {HOUSE: []},
                            "description": "q", **extra})


def test_patch_edits_metadata_only(client, model):
    ref = _quick(client, title="before")
    rid = ref["reference_id"]
    r = client.patch(f"/references/{rid}", json={"title": "after", "tags": ["New"]})
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "after" and r.json()["tags"] == ["new"]
    assert r.json()["record"] == ref["record"]  # the content is untouched

    r = client.patch(f"/references/{rid}", json={"description": None})
    assert r.status_code == 200 and r.json()["description"] is None
    assert r.json()["description_source"] is None

    r = client.patch(f"/references/{rid}", json={"criteria": []})
    assert r.status_code == 400 and "a new answer key is a new reference" in r.json()["detail"]
    assert client.patch(f"/references/{rid}", json={}).status_code == 400
    assert client.patch("/references/r000000000000", json={"title": "x"}).status_code == 404


def test_delete_refuses_while_a_live_job_uses_it_unless_forced(client, model):
    from jobs.queue import jobs_registry

    from api.schemas import ClassifierMetadata

    ref = _quick(client)
    rid = ref["reference_id"]
    user = _run(jobs_registry.register(
        ClassifierMetadata(type="assess", request_id="t", references=[rid]),
        initial_phase="staging",
    ))
    r = client.delete(f"/references/{rid}")
    assert r.status_code == 409 and user in r.json()["detail"]

    r = client.delete(f"/references/{rid}", params={"force": "true"})
    assert r.status_code == 204
    assert client.get(f"/references/{rid}").status_code == 404
    assert client.get(f"/references/{rid}/files/page.jpg").status_code == 404
    assert client.delete(f"/references/{rid}").status_code == 404
    _run(jobs_registry.delete(user))


def test_list_filters_by_status_and_tag(client, model):
    tagged = _quick(client, tags=["filter-me"])
    listing = client.get("/references", params={"tag": "FILTER-ME"}).json()
    assert [r["reference_id"] for r in listing["references"]] == [tagged["reference_id"]]
    assert listing["total"] == 1
    assert listing["references"][0]["criteria"][0]["name"] == HOUSE
    assert "record" not in listing["references"][0]

    ready = client.get("/references", params={"status": "ready", "limit": 200}).json()
    assert all(r["status"] == "ready" for r in ready["references"])
    assert tagged["reference_id"] in {r["reference_id"] for r in ready["references"]}
    assert client.get("/references", params={"status": "nope"}).status_code == 400
    page = client.get("/references", params={"limit": 1, "offset": 0}).json()
    assert len(page["references"]) == 1 and page["total"] >= 1


@pytest.fixture(scope="module")
def stored(client):
    """One ready reference for the read-only tests; fully supplied, so no model."""
    return _quick(client)


@pytest.mark.parametrize("name", [
    "manifest.json", "c.Foo.jpg", "page.png", ".record.json", "c..jpg", "record.json.tmp",
    "p0.base.jpg",
])
def test_the_files_route_refuses_any_other_name(client, stored, name):
    r = client.get(f"/references/{stored['reference_id']}/files/{name}")
    assert r.status_code == 400, r.text
    assert "is not a reference file name" in r.json()["detail"]


def test_a_traversal_never_reaches_the_disk(client, stored):
    for name in ("..%2Frecord.json", "..%2F..%2Fclassifier.db", "%2E%2E"):
        r = client.get(f"/references/{stored['reference_id']}/files/{name}")
        assert r.status_code in (400, 404), (name, r.status_code)
        assert r.headers["content-type"].startswith("application/json")


def test_unknown_or_malformed_ids_are_404(client):
    assert client.get("/references/r000000000000").status_code == 404
    assert client.get("/references/not-an-id").status_code == 404
    assert client.get("/references/not-an-id/files/page.jpg").status_code == 404


# ---------------------------------------------------------------------------
# Failure and reconcile
# ---------------------------------------------------------------------------


def test_nothing_answered_fails_the_reference(client, model):
    model.fail_scoring = True
    ref = _create(client, {"document": _doc(), "criteria": [_house()], "description": "x"},
                  expect="failed")
    assert "no criterion of this reference has an expected answer" in ref["error"]
    assert ref["files"] == []
    job = _wait_job(client, ref["job_id"])
    assert job["phase"] == "failed"


def test_a_pending_reference_whose_job_is_gone_is_reconciled_to_failed(client):
    from jobs.queue import jobs_registry
    from references.model import new_reference_id
    from references.store import reconcile, reference_registry

    rid = new_reference_id()
    _run(reference_registry.create(rid, source_kind="document", job_id="ffffffffffff"))
    assert _run(reconcile(jobs_registry)) >= 1
    ref = client.get(f"/references/{rid}").json()
    assert ref["status"] == "failed" and "no longer exists" in ref["error"]


# ---------------------------------------------------------------------------
# Retention — the reason references have their own root
# ---------------------------------------------------------------------------


def test_the_sweeper_takes_the_creation_job_and_leaves_the_reference(client, model, monkeypatch):
    from jobs.queue import sweeper
    from regions import sweeper as sweeper_module
    from regions.store import store as artifact_store

    ref = _create(client, {"document": _doc(), "criteria": [_house()], "description": "x"})
    rid, job_id = ref["reference_id"], ref["job_id"]
    # The reference is readied just BEFORE its job row completes; a job still
    # "processing" is never swept, so wait for the row.
    assert _wait_job(client, job_id)["phase"] == "completed"
    assert artifact_store.exists(job_id)  # the run wrote an ordinary job directory

    monkeypatch.setattr(sweeper_module, "JOB_TTL_HOURS", 0)
    time.sleep(0.01)  # created_at strictly before the cutoff
    out = _run(sweeper.sweep_once())
    assert out["jobs_removed"] >= 1

    assert client.get(f"/jobs/{job_id}").status_code == 404
    assert not artifact_store.exists(job_id)
    after = client.get(f"/references/{rid}")
    assert after.status_code == 200
    assert after.json()["status"] == "ready"
    assert after.json()["record"] == ref["record"]
    names = {f["name"] for f in after.json()["files"]}
    assert {"page.jpg", "working.jpg", "record.json", "regions.json"} <= names
    assert client.get(f"/references/{rid}/files/page.jpg").status_code == 200


def test_a_creation_job_that_never_reaches_its_runner_still_fails_the_reference(client):
    """A payload that never landed fails the job in handle_job, before the
    runner — the reference must not be left pending until the next restart."""
    from api.schemas import ClassifierMetadata
    from jobs.queue import jobs_registry, queue
    from references.model import new_reference_id
    from references.store import reference_registry

    rid = new_reference_id()
    job_id = _run(jobs_registry.register(
        ClassifierMetadata(type="reference", request_id="t", reference_id=rid),
        initial_phase="staging",
    ))
    _run(reference_registry.create(rid, source_kind="document", job_id=job_id))
    with pytest.raises(RuntimeError, match="payload missing"):
        _run(queue.handle_job(_run(jobs_registry.get(job_id))))
    ref = client.get(f"/references/{rid}").json()
    assert ref["status"] == "failed" and "payload missing" in ref["error"]
    _run(jobs_registry.delete(job_id))
