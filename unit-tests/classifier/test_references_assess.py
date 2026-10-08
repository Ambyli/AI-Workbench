"""/assess with `references`: resolution, guided scoring, the position check.

Same harness as test_references_api.py — the app in-process through
TestClient, its real queue and stores on the session temp directory, the
model scripted at ``llm.client._send`` (so every call takes a real
CLASSIFIER_MAX_LLM_CALLS slot). References are created fully supplied (a
breakdown and regions for every criterion, and a description), so building
one costs no model call and the fake model only ever sees /assess traffic.

What is pinned here:

  * every refusal at submit — the static ones on the request model (no
    references, a non-llm criterion, position without boxes, bad / duplicate
    / too many ids, a one-image model) and the store-checked ones (unknown
    ids, not ready, an inheritance conflict, auto without criteria, an
    explicit `criterion` nobody has, the per-criterion cap, position against
    a whole-page example); auto WITH criteria is refused as not yet built;
  * inheritance — criteria omitted means the references' criteria, merged by
    name; name matching is case-insensitive and `options.reference.criterion`
    redirects it;
  * the images per call — 2 for one example, 3 for a contrastive PASS + FAIL
    call, one example per call when the model takes only two; the box loop
    and an unguided criterion still send one;
  * `combine` any / all / mean, a partial failure (noted) and a total one
    (the unit errors), a reference deleted after submit (the unit errors);
  * the position check — hit, miss (capped), unlocated (not capped), and a
    cap that would RAISE a score does nothing; `score: false` reports only;
  * the cv → llm fallback carries the reference;
  * the result: `detail.reference`, top-level `references`, `request`, the
    `references.json` artifact and its label, and the job's metadata.

Run with::

    UV_LINK_MODE=copy uv run pytest unit-tests/classifier/test_references_assess.py -q
"""

from __future__ import annotations

import asyncio
import base64
import json
import time

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

HOUSE = "has a house"
GOOD_BOX = [100, 200, 300, 400]     # grid; on 400×300 → x 40-120, y 60-120
FAR_BOX = [700, 700, 900, 900]


@pytest.fixture(scope="module")
def client(need_postgres):
    import main

    with TestClient(main.app) as c:
        yield c


class FakeModel:
    """Answers by call kind; scoring answers by what the prompt contains."""

    def __init__(self):
        self.scores: dict[str, object] = {}   # substring of the prompt → score | Exception
        self.default = 9
        self.bbox = GOOD_BOX
        self.verify = 9
        self.calls: list[tuple[str, dict]] = []
        # The selection call: every catalogue id at `select_default`, unless
        # `select_conf` names it; `select_error` makes the call fail.
        self.select_default = 90
        self.select_conf: dict[str, int] = {}
        self.select_error: Exception | None = None

    @staticmethod
    def kind_of(prompt: dict) -> str:
        system = prompt["messages"][0]["content"]
        if "catalogue descriptions" in system:
            return "describe"
        if "You locate features" in system:
            return "bbox"
        if "image crop" in system:
            return "verify"
        if "stored reference examples" in system:
            return "select"
        return "score"

    @staticmethod
    def images(prompt: dict) -> int:
        return sum(1 for p in prompt["messages"][1]["content"] if p["type"] == "image_url")

    @staticmethod
    def texts(prompt: dict) -> list[str]:
        return [p["text"] for p in prompt["messages"][1]["content"] if p["type"] == "text"]

    def scoring(self) -> list[dict]:
        return [p for k, p in self.calls if k == "score"]

    def selections(self) -> list[dict]:
        return [p for k, p in self.calls if k == "select"]

    async def __call__(self, prompt):
        kind = self.kind_of(prompt)
        self.calls.append((kind, prompt))
        if kind == "bbox":
            body = {"bbox": self.bbox, "confidence": 90, "reason": "it"}
        elif kind == "verify":
            body = {"score": self.verify, "reason": "it"}
        elif kind == "describe":
            body = {"description": "a page"}
        elif kind == "select":
            if self.select_error is not None:
                raise self.select_error
            import re

            text = FakeModel.texts(prompt)[0]
            ids = re.findall(r"- id: (r[0-9a-f]{12})", text)
            body = {"matches": [
                {"id": i, "confidence": self.select_conf.get(i, self.select_default),
                 "reason": "same kind of thing"}
                for i in ids if self.select_conf.get(i, self.select_default) is not None
            ]}
        else:
            text = json.dumps(prompt)
            score = self.default
            for needle, value in self.scores.items():
                if needle in text:
                    score = value
                    break
            if isinstance(score, Exception):
                raise score
            body = {"score": score, "verdict": "PASS", "confidence": 70,
                    "reason": f"scored {score}"}
        return {"choices": [{"message": {"content": json.dumps(body)}}]}


@pytest.fixture
def model(monkeypatch):
    from llm import boxes as llm_boxes
    from llm import client as llm_client

    fake = FakeModel()
    monkeypatch.setattr(llm_client, "_send", fake)
    monkeypatch.setattr(llm_boxes, "LLM_BBOX_REFINE", False)
    monkeypatch.setattr(llm_boxes, "LLM_BBOX_GRIDLINES", False)
    return fake


def _png(width=400, height=300, value=180) -> bytes:
    import cv2

    return cv2.imencode(".png", np.full((height, width, 3), value, dtype=np.uint8))[1].tobytes()


def _doc(raw: bytes | None = None) -> dict:
    return {"type": "base64", "data": base64.b64encode(raw or _png()).decode(), "filename": "p.png"}


def _house(**options) -> dict:
    return {"name": HOUSE, "type": "llm", "options": {"hint": "presence", **options}}


def _wait(client, path: str, done, timeout=20.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(path).json()
        if done(body):
            return body
        time.sleep(0.05)
    raise AssertionError(f"{path} did not finish: {body}")


def _reference(client, *, criteria=None, score=10, reason="a two-storey house",
               regions=None, tags=None) -> str:
    """A ready reference, fully supplied — no model call."""
    criteria = criteria or [_house()]
    body = {
        "document": _doc(),
        "criteria": criteria,
        "breakdown": {c["name"]: {"score": score, "reason": reason}
                      for c in criteria if c.get("score", True)},
        "regions": {c["name"]: ([{"box": [40, 60, 120, 120]}] if regions is None else regions)
                    for c in criteria},
        "description": "given",
        "tags": tags or [],
    }
    r = client.post("/references", json=body)
    assert r.status_code == 202, r.text
    rid = r.json()["reference_id"]
    ref = _wait(client, f"/references/{rid}", lambda b: b.get("status") != "pending")
    assert ref["status"] == "ready", ref
    _wait(client, f"/jobs/{ref['job_id']}", lambda b: b.get("phase") == "completed")
    return rid


@pytest.fixture(scope="module")
def refs(client):
    """The module's shared references (built once, before any test scripts a model)."""
    return {
        "pass": _reference(client, reason="alpha house"),
        "pass2": _reference(client, reason="beta house"),
        "fail": _reference(client, score=2, reason="a barn, not a house"),
        "whole": _reference(client, reason="whole page house", regions=[]),
        "roof": _reference(client, criteria=[
            {"name": "a roof", "type": "llm", "options": {"hint": "presence"}},
            {"name": "Net 30", "type": "text"},
        ], reason="a tiled roof"),
        "roof_conflict": _reference(client, criteria=[
            {"name": "a roof", "type": "llm", "options": {"hint": "quality"}},
        ], reason="a tiled roof"),
    }


def _assess(client, body: dict, *, phase="completed") -> dict:
    r = client.post("/assess", json={"document": _doc(), **body})
    assert r.status_code == 202, r.text
    job = _wait(client, f"/jobs/{r.json()['job_id']}",
                lambda b: b.get("phase") in ("completed", "failed"))
    assert job["phase"] == phase, job
    return job


def _refuse(client, body: dict, status=400) -> str:
    r = client.post("/assess", json={"document": _doc(), **body})
    assert r.status_code == status, r.text
    return r.json()["detail"]


def _entry(job, name=HOUSE) -> dict:
    return job["result"]["assessment"]["per_criterion_scores"][name]


# ---------------------------------------------------------------------------
# Refusals at submit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("body, needle", [
    ({"criteria": [_house(reference={})]}, "the request lists no `references`"),
    ({"criteria": [{"name": "Net 30", "type": "text", "options": {"reference": {}}}],
      "references": ["r000000000000"]}, "references guide the vision model"),
    ({"criteria": [{"name": "has bicycle", "type": "detector", "options": {"reference": {}}}],
      "references": ["r000000000000"]}, "references guide the vision model"),
    ({"criteria": [{"name": "sharpness", "type": "cv", "options": {"reference": {}}}],
      "references": ["r000000000000"]}, "answered by an OpenCV detector"),
    ({"criteria": [_house(reference={"position": "check"})],
      "references": ["r000000000000"]}, "needs options.boxes: true"),
    ({"criteria": [{"name": HOUSE, "type": "llm", "options": {
        "hint": "quality", "boxes": True, "reference": {"position": "check"}}}],
      "references": ["r000000000000"]}, "needs options.boxes: true"),
    ({"criteria": [_house()], "references": []}, "empty list"),
    ({"criteria": [_house()], "references": ["r000000000000", "r000000000000"]},
     "duplicate reference ids"),
    ({"criteria": [_house()], "references": ["nope"]}, "not reference ids"),
    ({"criteria": [_house()], "references": [f"r{i:012x}" for i in range(11)]},
     "CLASSIFIER_REFERENCE_MAX_PER_REQUEST"),
    ({"criteria": [_house(reference={"combine": "best"})], "references": ["r000000000000"]},
     "combine"),
])
def test_static_refusals(client, model, body, needle):
    assert needle in _refuse(client, body)
    assert model.calls == []


def test_a_one_image_model_refuses_references(client, model, monkeypatch):
    from api import schemas

    monkeypatch.setattr(schemas, "VISION_LLM_MAX_IMAGES_PER_PROMPT", 1)
    assert "one image per request" in _refuse(
        client, {"criteria": [_house()], "references": ["r000000000000"]}
    )


def test_store_checked_refusals(client, model, refs, monkeypatch):
    from references import resolve
    from references.model import new_reference_id
    from references.store import reference_registry

    unknown = ["r00000000000a", "r00000000000b"]
    detail = _refuse(client, {"criteria": [_house()], "references": [refs["pass"], *unknown]})
    assert str(unknown) in detail

    pending = new_reference_id()
    asyncio.run(reference_registry.create(pending, source_kind="document", job_id=None))
    assert "is pending" in _refuse(
        client, {"criteria": [_house()], "references": [pending]}, status=409
    )
    asyncio.run(reference_registry.delete(pending))

    detail = _refuse(client, {"references": [refs["roof"], refs["roof_conflict"]]})
    assert refs["roof"] in detail and refs["roof_conflict"] in detail and "a roof" in detail

    assert "needs `criteria`" in _refuse(client, {"references": "auto"})

    assert "is not a usable criterion" in _refuse(client, {
        "criteria": [_house(reference={"criterion": "a chimney"})],
        "references": [refs["pass"]],
    })

    monkeypatch.setattr(resolve, "REFERENCE_MAX_PER_CRITERION", 1)
    assert "CLASSIFIER_REFERENCE_MAX_PER_CRITERION" in _refuse(
        client, {"criteria": [_house()], "references": [refs["pass"], refs["pass2"]]}
    )
    monkeypatch.undo()

    assert "is the whole page" in _refuse(client, {
        "criteria": [_house(boxes=True, reference={"position": "check"})],
        "references": [refs["whole"]],
    })
    assert model.calls == []


def test_multipart_takes_references_and_omitted_criteria_still_default(client, model, refs):
    r = client.post(
        "/assess",
        files={"file": ("p.png", _png(), "image/png")},
        data={"criteria": json.dumps([_house()]), "references": json.dumps([refs["pass"]])},
    )
    assert r.status_code == 202, r.text
    job = _wait(client, f"/jobs/{r.json()['job_id']}", lambda b: b.get("phase") == "completed")
    assert _entry(job)["detail"]["reference"]["applied"] is True

    r = client.post("/assess", files={"file": ("p.png", _png(), "image/png")},
                    data={"references": "auto"})
    assert r.status_code == 400 and "needs `criteria`" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Inheritance and matching
# ---------------------------------------------------------------------------


def test_omitted_criteria_are_inherited_from_the_references(client, model, refs):
    job = _assess(client, {"references": [refs["roof"]]})
    names = list(job["result"]["assessment"]["per_criterion_scores"])
    assert names == ["a roof", "Net 30"]
    assert job["result"]["references"]["inherited"] is True
    assert [c["name"] for c in job["result"]["request"]["criteria"]] == ["a roof", "Net 30"]
    # The llm criterion is guided by its own reference; the text one is not.
    assert _entry(job, "a roof")["detail"]["reference"]["applied"] is True
    assert "reference" not in _entry(job, "Net 30")["detail"]


def test_names_match_case_insensitively_and_criterion_redirects(client, model, refs):
    job = _assess(client, {
        "criteria": [{"name": "HAS A HOUSE", "type": "llm"},
                     {"name": "is a dwelling", "type": "llm",
                      "options": {"reference": {"criterion": "has a house"}}},
                     {"name": "unrelated", "type": "llm"}],
        "references": [refs["pass"]],
    })
    assert _entry(job, "HAS A HOUSE")["detail"]["reference"]["applied"] is True
    redirected = _entry(job, "is a dwelling")["detail"]["reference"]
    assert redirected["applied"] is True and redirected["examples"][0]["criterion"] == HOUSE
    unguided = _entry(job, "unrelated")["detail"]["reference"]
    assert unguided["applied"] is False and "no listed reference" in unguided["note"]
    images = sorted(FakeModel.images(p) for p in model.scoring())
    assert images == [1, 2, 2]


def test_use_false_scores_without_examples(client, model, refs):
    job = _assess(client, {"criteria": [_house(reference={"use": False})],
                           "references": [refs["pass"]]})
    ref = _entry(job)["detail"]["reference"]
    assert ref["applied"] is False and "use is false" in ref["note"]
    assert [FakeModel.images(p) for p in model.scoring()] == [1]


# ---------------------------------------------------------------------------
# Images per call, and what each call shows
# ---------------------------------------------------------------------------


def test_one_example_is_a_two_image_call(client, model, refs):
    job = _assess(client, {"criteria": [_house()], "references": [refs["pass"]]})
    (prompt,) = model.scoring()
    assert FakeModel.images(prompt) == 2
    content = prompt["messages"][1]["content"]
    assert [p["type"] for p in content] == ["text", "image_url", "text", "image_url", "text"]
    assert "was PASS (10): alpha house; the coloured box outlines it" in content[0]["text"]
    assert "CANDIDATE" in content[2]["text"]
    assert "CRITERION: has a house" in content[4]["text"]
    assert "score ONLY the image marked CANDIDATE" in prompt["messages"][0]["content"]

    entry = _entry(job)
    ref = entry["detail"]["reference"]
    assert ref["applied"] is True and ref["mode"] == "explicit" and ref["combine"] == "any"
    assert ref["examples"] == [{
        "reference_id": refs["pass"], "criterion": HOUSE,
        "expected": {"score": 10, "verdict": "PASS"}, "polarity": "pass",
        "region": "caller", "call": 0,
    }]
    assert ref["calls"][0]["images"] == 2 and ref["calls"][0]["chosen"] is True
    assert ref["position"] is None and ref["note"] is None
    # Unit and criterion agree on a one-item job.
    assert entry["items"][0]["detail"]["reference"] == ref


def test_pass_and_fail_share_one_contrastive_call(client, model, refs):
    job = _assess(client, {"criteria": [_house()], "references": [refs["fail"], refs["pass"]]})
    (prompt,) = model.scoring()
    assert FakeModel.images(prompt) == 3
    texts = FakeModel.texts(prompt)
    # PASS-side example first, then the counter-example, then the candidate.
    assert "was PASS" in texts[0] and "was FAIL (2)" in texts[1]
    assert "does NOT satisfy the criterion" in texts[1]
    ref = _entry(job)["detail"]["reference"]
    assert [e["polarity"] for e in ref["examples"]] == ["fail", "pass"]
    assert [e["call"] for e in ref["examples"]] == [0, 0]
    assert len(ref["calls"]) == 1 and ref["calls"][0]["images"] == 3


def test_a_two_image_model_sends_one_example_per_call(client, model, refs, monkeypatch):
    from analysis import llm_eval

    monkeypatch.setattr(llm_eval, "VISION_LLM_MAX_IMAGES_PER_PROMPT", 2)
    job = _assess(client, {"criteria": [_house()], "references": [refs["pass"], refs["fail"]]})
    assert sorted(FakeModel.images(p) for p in model.scoring()) == [2, 2]
    ref = _entry(job)["detail"]["reference"]
    assert [e["call"] for e in ref["examples"]] == [0, 1]


def test_the_box_loop_still_sends_one_image(client, model, refs):
    _assess(client, {"criteria": [_house(boxes=True)], "references": [refs["pass"]]})
    kinds = {}
    for kind, prompt in model.calls:
        kinds.setdefault(kind, set()).add(FakeModel.images(prompt))
    assert kinds["score"] == {2} and kinds["bbox"] == {1} and kinds["verify"] == {1}


# ---------------------------------------------------------------------------
# Combining, failing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rule, score, verdict, reason", [
    ("any", 9, "PASS", "scored 9"),
    ("all", 4, "MARGINAL", "scored 4"),
    ("mean", 6, "MARGINAL", None),   # round(6.5) — banker's rounding — is 6
])
def test_combine(client, model, refs, rule, score, verdict, reason):
    model.scores = {"alpha house": 9, "beta house": 4}
    job = _assess(client, {"criteria": [_house(reference={"combine": rule})],
                           "references": [refs["pass"], refs["pass2"]]})
    entry = _entry(job)
    assert (entry["score"], entry["verdict"]) == (score, verdict)
    assert entry["detail"]["value"] == score
    if reason:
        assert entry["reason"] == reason
    ref = entry["detail"]["reference"]
    assert ref["combine"] == rule and len(ref["calls"]) == 2
    assert sorted(c["score"] for c in ref["calls"]) == [4, 9]


def test_a_partial_failure_combines_the_rest_and_says_so(client, model, refs):
    model.scores = {"beta house": httpx.ConnectError("down")}
    job = _assess(client, {"criteria": [_house()], "references": [refs["pass"], refs["pass2"]]})
    entry = _entry(job)
    assert entry["status"] == "ok" and entry["score"] == 9
    ref = entry["detail"]["reference"]
    assert "1 of 2 reference-guided calls failed" in ref["note"]
    failed = [c for c in ref["calls"] if c["error"]]
    assert len(failed) == 1 and "down" in failed[0]["error"] and failed[0]["score"] is None


def test_every_call_failing_fails_the_unit(client, model, refs):
    model.scores = {"house": httpx.ConnectError("down")}
    job = _assess(client, {"criteria": [_house()], "references": [refs["pass"], refs["pass2"]]})
    entry = _entry(job)
    assert entry["status"] == "error" and "down" in entry["error"]


def test_a_reference_deleted_after_submit_fails_that_criterion(client, model):
    import analysis
    from api.schemas import AssessRequest
    from references.resolve import resolve_references
    from references.store import reference_registry

    rid = _reference(client, reason="soon gone")
    request = AssessRequest.model_validate({
        "document": {"type": "text", "data": "x"},
        "criteria": [_house(), {"name": "other", "type": "llm"}],
        "references": [rid],
    })
    plan = asyncio.run(resolve_references(request, reference_registry)).plan
    assert client.delete(f"/references/{rid}").status_code == 204

    doc = analysis.load_document_bytes(_png(), "p.png", "image/png", keep_source=True)
    result = asyncio.run(analysis.analyze_document(doc, request.criteria, references_plan=plan))
    entries = result["assessment"]["per_criterion_scores"]
    assert entries[HOUSE]["status"] == "error"
    assert "was deleted after this job was submitted" in entries[HOUSE]["error"]
    assert entries["other"]["status"] == "ok"


# ---------------------------------------------------------------------------
# The position check
# ---------------------------------------------------------------------------


def _positioned(client, refs, **options):
    return _assess(client, {
        "criteria": [_house(boxes=True, reference={"position": "check", **options})],
        "references": [refs["pass"]],
    })


def test_position_hit_keeps_the_score(client, model, refs):
    entry = _entry(_positioned(client, refs))
    position = entry["detail"]["reference"]["position"]
    assert position["status"] == "hit" and position["iou"] == pytest.approx(1.0)
    assert position["reference_id"] == refs["pass"] and position["capped_from"] is None
    assert entry["score"] == 9


def test_position_miss_caps_the_score(client, model, refs):
    from config import REFERENCE_POSITION_CAP

    model.bbox = FAR_BOX
    entry = _entry(_positioned(client, refs))
    position = entry["detail"]["reference"]["position"]
    assert position["status"] == "miss" and position["capped_from"] == 9
    assert entry["score"] == REFERENCE_POSITION_CAP and entry["verdict"] == "MARGINAL"
    assert entry["detail"]["value"] == REFERENCE_POSITION_CAP
    assert "score capped at" in entry["reason"]


def test_a_cap_never_raises_a_score(client, model, refs, monkeypatch):
    from analysis import references as analysis_references

    monkeypatch.setattr(analysis_references, "REFERENCE_POSITION_CAP", 10)
    model.bbox = FAR_BOX
    entry = _entry(_positioned(client, refs))
    position = entry["detail"]["reference"]["position"]
    assert position["status"] == "miss" and position["capped_from"] is None
    assert entry["score"] == 9


def test_position_unlocated_is_not_capped(client, model, refs):
    model.verify = 2   # every box is rejected by the crop check
    entry = _entry(_positioned(client, refs))
    assert entry["detail"]["reference"]["position"]["status"] == "unlocated"
    assert entry["score"] == 9


def test_min_iou_and_the_offset_decide_a_hit(client, model, refs, monkeypatch):
    from analysis import references as analysis_references

    # A quarter of the example's box, same corner: IoU 0.25, centres 0.05 apart
    # (centre distance / sqrt 2, each box a fraction of its own page).
    model.bbox = [100, 200, 200, 300]
    near = _entry(_positioned(client, refs, min_iou=0.9))["detail"]["reference"]["position"]
    assert near["min_iou"] == 0.9 and near["iou"] == pytest.approx(0.25)
    assert near["center_offset"] == pytest.approx(0.05, abs=1e-3)
    assert near["status"] == "hit"   # by the offset (<= 0.15), not by the overlap

    monkeypatch.setattr(analysis_references, "REFERENCE_POSITION_MAX_OFFSET", 0.01)
    by_iou = _entry(_positioned(client, refs, min_iou=0.2))["detail"]["reference"]["position"]
    assert by_iou["status"] == "hit" and by_iou["max_offset"] == 0.01
    strict = _entry(_positioned(client, refs, min_iou=0.9))
    assert strict["detail"]["reference"]["position"]["status"] == "miss"
    assert strict["score"] == 5


def test_position_on_score_false_reports_only(client, model, refs):
    model.bbox = FAR_BOX
    job = _assess(client, {
        "criteria": [{"name": HOUSE, "type": "llm", "score": False, "options": {
            "hint": "presence", "boxes": True, "reference": {"position": "check"}}}],
        "references": [refs["pass"]],
    })
    entry = _entry(job)
    assert entry["score"] is None
    position = entry["detail"]["reference"]["position"]
    assert position["status"] == "miss" and position["capped_from"] is None


# ---------------------------------------------------------------------------
# The cv → llm fallback, and the result blocks
# ---------------------------------------------------------------------------


def test_cv_llm_fallback_carries_the_reference(client, model, refs):
    job = _assess(client, {"criteria": [{"name": HOUSE, "type": "cv",
                                         "options": {"reference": {"combine": "all"}}}],
                           "references": [refs["pass"]]})
    entry = _entry(job)
    assert entry["method"] == "llm"
    assert entry["detail"]["reference"]["applied"] is True
    assert entry["detail"]["reference"]["combine"] == "all"
    assert entry["options_used"]["reference"]["combine"] == "all"
    assert [FakeModel.images(p) for p in model.scoring()] == [2]


def test_the_result_blocks_metadata_and_artifact(client, model, refs):
    job = _assess(client, {"criteria": [_house(), {"name": "Net 30", "type": "text"}],
                           "references": [refs["pass"], refs["fail"]]})
    assert job["metadata"]["references"] == [refs["pass"], refs["fail"]]
    block = job["result"]["references"]
    assert block["mode"] == "explicit" and block["requested"] == [refs["pass"], refs["fail"]]
    assert block["resolved"] == [refs["pass"], refs["fail"]]
    assert block["pool"] is None and block["pool_truncated"] is False
    assert block["selection"] == [] and block["calls"] == 1 and block["inherited"] is False
    assert [e["reference_id"] for e in block["criteria"][HOUSE]["examples"]] == [
        refs["pass"], refs["fail"]]
    assert "Net 30" not in block["criteria"]
    assert [c["name"] for c in job["result"]["request"]["criteria"]] == [HOUSE, "Net 30"]

    files = {f["name"]: f for f in job["result"]["artifacts"]["files"]}
    assert files["references.json"]["kind"] == "references"
    stored = client.get(f"/jobs/{job['job_id']}/artifacts/references.json").json()
    (call,) = stored["calls"]
    assert call["criterion"] == HOUSE and call["images"] == 3
    from references.model import composite_name

    assert [(e["reference_id"], e["composite"]) for e in call["examples"]] == [
        (refs["pass"], composite_name(HOUSE)), (refs["fail"], composite_name(HOUSE))]
    manifest = client.get(f"/jobs/{job['job_id']}/artifacts").json()
    assert "references.json" in {f["name"] for f in manifest["files"]}


def test_without_references_the_result_says_null_and_nothing_changes(client, model):
    job = _assess(client, {"criteria": [_house()]})
    assert job["result"]["references"] is None
    assert "reference" not in _entry(job)["detail"]
    assert "reference" not in _entry(job)["options_used"]
    assert set(job["metadata"]) == {"type", "request_id"}
    assert [FakeModel.images(p) for p in model.scoring()] == [1]


def test_delete_is_refused_while_an_assess_job_using_it_is_live(client, refs):
    from api.schemas import ClassifierMetadata
    from jobs.queue import jobs_registry

    job_id = asyncio.run(jobs_registry.register(
        ClassifierMetadata(type="assess", request_id="t", references=[refs["pass2"]]),
        initial_phase="staging",
    ))
    try:
        assert client.delete(f"/references/{refs['pass2']}").status_code == 409
    finally:
        asyncio.run(jobs_registry.delete(job_id))


def test_the_auto_pool_is_fixed_at_submit(client, monkeypatch):
    """The selection CALL is a later change; the pool it will rank is
    resolved here: ready references with a usable criterion matching one of
    the request's llm-answered criteria, tag-filtered, newest first, capped."""
    from api.schemas import AssessRequest
    from references import resolve
    from references.store import reference_registry

    both = _reference(client, tags=["t-alpha", "t-beta"], reason="tagged both")
    one = _reference(client, tags=["t-alpha"], reason="tagged one")
    other = _reference(client, tags=["t-alpha"], criteria=[
        {"name": "a chimney", "type": "llm", "options": {"hint": "presence"}}])

    def pool(tags, match, criteria=None):
        request = AssessRequest.model_validate({
            "document": {"type": "text", "data": "x"},
            "criteria": criteria or [_house(), {"name": "Net 30", "type": "text"}],
            "references": {"auto": True, "tags": tags, "tags_match": match},
        })
        refs, truncated = asyncio.run(resolve.auto_pool(request, reference_registry))
        return [r.id for r in refs], truncated

    assert pool(["T-Alpha", "t-beta"], "all") == ([both], False)
    assert pool(["t-alpha"], "any") == ([one, both], False)   # newest first; no chimney
    assert pool(["t-alpha"], "any", [{"name": "A Chimney", "type": "llm"}]) == ([other], False)
    monkeypatch.setattr(resolve, "REFERENCE_AUTO_POOL_MAX", 1)
    assert pool(["t-alpha"], "any") == ([one], True)
    resolved = asyncio.run(resolve.resolve_references(AssessRequest.model_validate({
        "document": {"type": "text", "data": "x"}, "criteria": [_house()],
        "references": {"auto": True, "tags": ["t-alpha"]},
    }), reference_registry))
    assert resolved.plan["mode"] == "auto" and resolved.plan["pool_truncated"] is True
    assert resolved.reference_ids == [one]


# ---------------------------------------------------------------------------
# references: "auto" — the per-item selection call
# ---------------------------------------------------------------------------


def _auto(tag: str, match: str = "any") -> dict:
    return {"auto": True, "tags": [tag], "tags_match": match}


@pytest.fixture(scope="module")
def auto_refs(client):
    """References scoped by tags no other test uses, so each pool is exact."""
    return {
        "pass": _reference(client, tags=["auto-a"], reason="auto pass house"),
        "fail": _reference(client, tags=["auto-a"], score=2, reason="auto barn"),
        "roof": _reference(client, tags=["auto-a"], criteria=[
            {"name": "a roof", "type": "llm", "options": {"hint": "presence"}}],
            reason="auto roof"),
        "pos": _reference(client, tags=["auto-pos"], reason="auto positioned house"),
    }


def test_auto_selects_and_guides(client, model, auto_refs):
    job = _assess(client, {"criteria": [_house()], "references": _auto("auto-a")})
    # Only references with a matching criterion are in the pool: no roof.
    block = job["result"]["references"]
    assert block["mode"] == "auto" and block["pool_truncated"] is False
    assert set(block["pool"]) == {auto_refs["pass"], auto_refs["fail"]}
    assert set(job["metadata"]["references"]) == set(block["pool"])

    (selection,) = model.selections()
    assert FakeModel.images(selection) == 1
    text = FakeModel.texts(selection)[0]
    assert "'has a house' was PASS (10)" in text and "'has a house' was FAIL (2)" in text
    assert auto_refs["roof"] not in text

    (scoring,) = model.scoring()
    assert FakeModel.images(scoring) == 3          # the selected PASS + FAIL, contrastive
    ref = _entry(job)["detail"]["reference"]
    assert ref["applied"] is True and ref["mode"] == "auto"
    assert {e["reference_id"] for e in ref["examples"]} == {auto_refs["pass"], auto_refs["fail"]}

    (sel,) = block["selection"]
    assert sel["item"] == 0 and sel["status"] == "ok" and sel["error"] is None
    assert {m["id"] for m in sel["matches"]} == set(block["pool"])
    assert set(block["resolved"]) == set(block["pool"])
    assert block["selection_calls"] == 1 and block["calls"] == 1

    stored = client.get(f"/jobs/{job['job_id']}/artifacts/references.json").json()
    assert {e["reference_id"] for e in stored["catalogue"]} == set(block["pool"])
    assert stored["selection"][0]["raw"]["matches"]  # the model's own answer is kept


def test_auto_keeps_only_matches_at_or_above_the_threshold(client, model, auto_refs):
    from config import REFERENCE_AUTO_MIN_CONFIDENCE

    model.select_conf = {auto_refs["pass"]: REFERENCE_AUTO_MIN_CONFIDENCE - 1,
                         auto_refs["fail"]: REFERENCE_AUTO_MIN_CONFIDENCE}
    job = _assess(client, {"criteria": [_house()], "references": _auto("auto-a")})
    ref = _entry(job)["detail"]["reference"]
    assert [e["reference_id"] for e in ref["examples"]] == [auto_refs["fail"]]
    assert [FakeModel.images(p) for p in model.scoring()] == [2]


def test_auto_respects_the_per_criterion_cap(client, model, auto_refs, monkeypatch):
    from analysis import references as analysis_references

    monkeypatch.setattr(analysis_references, "REFERENCE_MAX_PER_CRITERION", 1)
    model.select_conf = {auto_refs["fail"]: 95, auto_refs["pass"]: 80}
    job = _assess(client, {"criteria": [_house()], "references": _auto("auto-a")})
    ref = _entry(job)["detail"]["reference"]
    assert [e["reference_id"] for e in ref["examples"]] == [auto_refs["fail"]]  # best ranked


def test_auto_zero_matches_scores_unguided(client, model, auto_refs):
    model.select_default = None   # the model answers {"matches": []}
    job = _assess(client, {"criteria": [_house()], "references": _auto("auto-a")})
    ref = _entry(job)["detail"]["reference"]
    assert ref["applied"] is False and "matched no reference" in ref["note"]
    assert [FakeModel.images(p) for p in model.scoring()] == [1]
    assert job["result"]["references"]["selection"][0]["status"] == "ok"


def test_a_failed_selection_degrades_to_unguided_scoring(client, model, auto_refs):
    model.select_error = httpx.ConnectError("selector down")
    job = _assess(client, {"criteria": [_house()], "references": _auto("auto-a")})
    entry = _entry(job)
    assert entry["status"] == "ok" and entry["score"] == 9
    assert entry["detail"]["reference"]["applied"] is False
    assert "selection call failed" in entry["detail"]["reference"]["note"]
    (sel,) = job["result"]["references"]["selection"]
    assert sel["status"] == "failed" and "selector down" in sel["error"]


def test_an_empty_pool_makes_no_selection_call(client, model, auto_refs):
    job = _assess(client, {"criteria": [_house()], "references": _auto("no-such-tag")})
    assert model.selections() == []
    assert job["result"]["references"]["pool"] == []
    ref = _entry(job)["detail"]["reference"]
    assert ref["applied"] is False and "pool is empty" in ref["note"]


def test_one_selection_call_per_image_item_and_none_for_text(client, model, auto_refs):
    r = client.post("/assess", json={
        "documents": [_doc(), _doc(), {"type": "text", "data": "a house, in words"}],
        "criteria": [_house(), {"name": "is a house", "type": "llm",
                                "options": {"reference": {"criterion": HOUSE}}}],
        "references": _auto("auto-a"),
    })
    assert r.status_code == 202, r.text
    job = _wait(client, f"/jobs/{r.json()['job_id']}", lambda b: b.get("phase") == "completed")
    # Two image items → two calls, shared by both criteria; the .txt item → none.
    assert len(model.selections()) == 2
    assert all(FakeModel.images(p) == 1 for p in model.selections())
    selection = {s["item"]: s for s in job["result"]["references"]["selection"]}
    assert selection[0]["status"] == selection[1]["status"] == "ok"
    assert selection[2]["status"] == "skipped" and "no page image" in selection[2]["reason"]
    units = {u["item"]: u for u in _entry(job)["items"]}
    assert units[0]["detail"]["reference"]["applied"] is True
    assert units[2]["detail"]["reference"]["applied"] is False
    assert _entry(job, "is a house")["items"][0]["detail"]["reference"]["applied"] is True


def test_auto_pool_truncation_is_reported(client, model, auto_refs, monkeypatch):
    from references import resolve

    monkeypatch.setattr(resolve, "REFERENCE_AUTO_POOL_MAX", 1)
    job = _assess(client, {"criteria": [_house()], "references": _auto("auto-a")})
    block = job["result"]["references"]
    assert block["pool_truncated"] is True and len(block["pool"]) == 1
    stored = client.get(f"/jobs/{job['job_id']}/artifacts/references.json").json()
    assert len(stored["catalogue"]) == 1


def test_auto_tags_all_versus_any(client, model, auto_refs):
    job = _assess(client, {"criteria": [_house()],
                           "references": {"auto": True, "tags": ["auto-a", "auto-pos"],
                                          "tags_match": "all"}})
    assert job["result"]["references"]["pool"] == []
    job = _assess(client, {"criteria": [_house()],
                           "references": {"auto": True, "tags": ["auto-a", "auto-pos"],
                                          "tags_match": "any"}})
    assert set(job["result"]["references"]["pool"]) == {
        auto_refs["pass"], auto_refs["fail"], auto_refs["pos"]}


def test_auto_position_check_uses_the_selected_pass_example(client, model, auto_refs):
    from config import REFERENCE_POSITION_CAP

    model.bbox = FAR_BOX
    job = _assess(client, {
        "criteria": [_house(boxes=True, reference={"position": "check"})],
        "references": _auto("auto-pos"),
    })
    entry = _entry(job)
    position = entry["detail"]["reference"]["position"]
    assert position["status"] == "miss" and position["reference_id"] == auto_refs["pos"]
    assert entry["score"] == REFERENCE_POSITION_CAP and position["capped_from"] == 9


def test_delete_is_refused_while_an_auto_job_is_live(client, auto_refs):
    from api.schemas import AssessRequest, ClassifierMetadata
    from jobs.queue import jobs_registry
    from references.resolve import resolve_references
    from references.store import reference_registry

    request = AssessRequest.model_validate({
        "document": {"type": "text", "data": "x"}, "criteria": [_house()],
        "references": _auto("auto-pos"),
    })
    resolved = asyncio.run(resolve_references(request, reference_registry))
    assert resolved.reference_ids == [auto_refs["pos"]]
    job_id = asyncio.run(jobs_registry.register(
        ClassifierMetadata(type="assess", request_id="t", references=resolved.reference_ids),
        initial_phase="staging",
    ))
    try:
        r = client.delete(f"/references/{auto_refs['pos']}")
        assert r.status_code == 409 and job_id in r.json()["detail"]
    finally:
        asyncio.run(jobs_registry.delete(job_id))
