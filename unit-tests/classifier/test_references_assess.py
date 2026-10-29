"""/assess with `references`: resolution, guided scoring, the position check.

Same harness as test_references_api.py — the app in-process through
TestClient, its real queue and stores on the session temp directory, the
model scripted at ``llm.client._send`` (so every call takes a real slot of
its model's ``max_concurrent`` limit). References are created fully supplied (a
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
  * matching by `key` — the order criterion → key → name: one key guides
    differently named criteria, a keyed criterion never falls back to its
    name (unguided, noted — not a 400), a key-less one still matches by
    name, `options.reference.criterion` beats the key, an inheritance
    conflict on keys, inherited keys, a reference keyed by PATCH, the `auto`
    pool and pick by key, and an old `auto` plan without `matched_by`;
  * the images per call — 2 for one example, 3 for a contrastive PASS + FAIL
    call, one example per call when the model takes only two; the box loop
    (knobs off) and an unguided criterion still send one;
  * the box loop WITH examples (the CLASSIFIER_REFERENCE_BOX_* knobs patched
    on): a guided `score: false` locate job, `localization.references` and
    the `references.json` / metric kinds with a failed box call, one entry
    per page on a two-page criterion, the notes when the loop ran unguided,
    a missing `page.jpg` or a reference deleted before the loop (dropped,
    the unit `ok`), a FAIL not shown unless its reference is `presence` (an
    old plan without `hint` included), the position check's ask crops, and a
    position check unchanged by what the loop was shown;
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
            if isinstance(self.verify, Exception):
                raise self.verify
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
    from llm.models import get_registry

    monkeypatch.setattr(get_registry().default, "max_images_per_prompt", 1)
    detail = _refuse(client, {"criteria": [_house()], "references": ["r000000000000"]})
    assert "takes 1 image per prompt" in detail and "example cannot be shown" in detail


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
    block = job["result"]["references"]["criteria"]
    assert (block["HAS A HOUSE"]["matched_by"], block["HAS A HOUSE"]["matched"]) == (
        "name", "HAS A HOUSE")
    assert (block["is a dwelling"]["matched_by"], block["is a dwelling"]["matched"]) == (
        "criterion", HOUSE)


def test_use_false_scores_without_examples(client, model, refs):
    job = _assess(client, {"criteria": [_house(reference={"use": False})],
                           "references": [refs["pass"]]})
    ref = _entry(job)["detail"]["reference"]
    assert ref["applied"] is False and "use is false" in ref["note"]
    assert [FakeModel.images(p) for p in model.scoring()] == [1]


# ---------------------------------------------------------------------------
# Matching by key — the order criterion → key → name
# ---------------------------------------------------------------------------

SQUARE = "is this square"
RECTANGLE = "is this rectangle"
SHAPE_KEY = "shape-key-7f"   # distinctive, so a prompt can be searched for it


def _shape(name=SQUARE, key=None, **options) -> dict:
    out = {"name": name, "type": "llm", "options": {"hint": "presence", **options}}
    if key is not None:
        out["key"] = key
    return out


@pytest.fixture(scope="module")
def keyed(client):
    """Keyed references (no tags, so no `auto` pool here ever sees them)."""
    return {
        "square": _reference(client, criteria=[_shape(key=SHAPE_KEY)], reason="a square tile"),
        "square_upper": _reference(client, criteria=[_shape(key=SHAPE_KEY.upper())],
                                   reason="another square tile"),
        "square_other": _reference(client, criteria=[_shape(key="colour-key-3c")],
                                   reason="a red square"),
        "square_unkeyed": _reference(client, criteria=[_shape()], reason="an unkeyed square"),
    }


def test_one_key_guides_differently_named_criteria(client, model, keyed):
    job = _assess(client, {
        "criteria": [_shape(RECTANGLE, SHAPE_KEY.upper()), _shape(SQUARE, SHAPE_KEY)],
        "references": [keyed["square"]],
    })
    for name in (RECTANGLE, SQUARE):
        ref = _entry(job, name)["detail"]["reference"]
        assert ref["applied"] is True, name
        assert [(e["reference_id"], e["criterion"]) for e in ref["examples"]] == [
            (keyed["square"], SQUARE)]
    # Each entry echoes its own key, as sent.
    assert _entry(job, RECTANGLE)["key"] == SHAPE_KEY.upper()
    assert _entry(job, SQUARE)["key"] == SHAPE_KEY
    block = job["result"]["references"]["criteria"]
    assert (block[RECTANGLE]["matched_by"], block[RECTANGLE]["matched"]) == (
        "key", SHAPE_KEY.upper())
    assert block[SQUARE]["matched_by"] == "key"
    # The model is told what the example ANSWERED (the reference criterion's
    # name) and asked its own question; the key is never in a prompt.
    prompts = model.scoring()
    assert len(prompts) == 2 and all(FakeModel.images(p) == 2 for p in prompts)
    asked = set()
    for prompt in prompts:
        text = json.dumps(prompt)
        assert SHAPE_KEY not in text.lower()
        assert "in this reference 'is this square' was PASS (10)" in FakeModel.texts(prompt)[0]
        asked.add(FakeModel.texts(prompt)[-1].split("CRITERION: ", 1)[1].split("\n", 1)[0])
    assert asked == {RECTANGLE, SQUARE}


def test_a_keyed_criterion_never_falls_back_to_its_name(client, model, keyed):
    # The name matches the reference's criterion; the key does not. No 400:
    # it scores unguided, and the note names the key.
    job = _assess(client, {
        "criteria": [_shape(SQUARE, "no-such-key")],
        "references": [keyed["square"], keyed["square_unkeyed"]],
    })
    ref = _entry(job, SQUARE)["detail"]["reference"]
    assert ref["applied"] is False
    assert ref["note"] == "no listed reference has a usable criterion with key 'no-such-key'"
    assert job["result"]["references"]["criteria"][SQUARE]["matched_by"] == "key"
    assert [FakeModel.images(p) for p in model.scoring()] == [1]


def test_a_keyless_criterion_still_matches_by_name(client, model, keyed):
    # The reference criterion HAS a key; a key-less /assess criterion still
    # finds it by name, case-insensitively.
    job = _assess(client, {"criteria": [_shape(SQUARE.upper())],
                           "references": [keyed["square"]]})
    ref = _entry(job, SQUARE.upper())["detail"]["reference"]
    assert ref["applied"] is True and ref["examples"][0]["reference_id"] == keyed["square"]
    assert _entry(job, SQUARE.upper())["key"] is None
    block = job["result"]["references"]["criteria"][SQUARE.upper()]
    assert (block["matched_by"], block["matched"]) == ("name", SQUARE.upper())


def test_options_reference_criterion_beats_the_key(client, model, refs, keyed):
    # The key would match `square`; the explicit criterion name wins.
    job = _assess(client, {
        "criteria": [_shape(RECTANGLE, SHAPE_KEY, reference={"criterion": HOUSE})],
        "references": [keyed["square"], refs["pass"]],
    })
    ref = _entry(job, RECTANGLE)["detail"]["reference"]
    assert [(e["reference_id"], e["criterion"]) for e in ref["examples"]] == [
        (refs["pass"], HOUSE)]
    block = job["result"]["references"]["criteria"][RECTANGLE]
    assert (block["matched_by"], block["matched"]) == ("criterion", HOUSE)
    # An unmatched explicit criterion is still the one 400.
    assert "is not a usable criterion" in _refuse(client, {
        "criteria": [_shape(RECTANGLE, SHAPE_KEY, reference={"criterion": "a chimney"})],
        "references": [keyed["square"]],
    })


def test_inherited_criteria_keep_their_keys_and_a_key_conflict_is_a_400(
    client, model, keyed
):
    job = _assess(client, {"references": [keyed["square"], keyed["square_upper"]]})
    # One criterion: the keys differ only in case, so the two agree.
    (criterion,) = job["result"]["request"]["criteria"]
    assert (criterion["name"], criterion["key"]) == (SQUARE, SHAPE_KEY)
    assert _entry(job, SQUARE)["key"] == SHAPE_KEY
    ref = _entry(job, SQUARE)["detail"]["reference"]
    assert {e["reference_id"] for e in ref["examples"]} == {
        keyed["square"], keyed["square_upper"]}
    assert job["result"]["references"]["criteria"][SQUARE]["matched_by"] == "key"

    for other in ("square_other", "square_unkeyed"):
        detail = _refuse(client, {"references": [keyed["square"], keyed[other]]})
        assert keyed["square"] in detail and keyed[other] in detail
        assert SQUARE in detail and "key" in detail


def test_a_reference_keyed_by_patch_guides_by_key(client, model):
    rid = _reference(client, criteria=[_shape("is it round")], reason="a round table")
    body = {"criteria": [_shape("is the table circular", "roundness")], "references": [rid]}
    assert _entry(_assess(client, body), "is the table circular")["detail"]["reference"][
        "applied"] is False
    r = client.patch(f"/references/{rid}", json={"criteria_keys": {"is it round": "Roundness"}})
    assert r.status_code == 200, r.text
    ref = _entry(_assess(client, body), "is the table circular")["detail"]["reference"]
    assert ref["applied"] is True and ref["examples"][0]["criterion"] == "is it round"


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
    from llm.models import get_registry

    monkeypatch.setattr(get_registry().default, "max_images_per_prompt", 2)
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
# Reference examples in the box loop (ZT-527) — the CLASSIFIER_REFERENCE_BOX_*
# knobs on. The test above is the knobs-off pin.
# ---------------------------------------------------------------------------


@pytest.fixture
def knobs(monkeypatch):
    """Set the three knobs where they are read (``llm.boxes``)."""
    from llm import boxes as llm_boxes

    def set_knobs(ask=True, refine=True, verify=True):
        monkeypatch.setattr(llm_boxes, "REFERENCE_BOX_ASK", ask)
        monkeypatch.setattr(llm_boxes, "REFERENCE_BOX_REFINE", refine)
        monkeypatch.setattr(llm_boxes, "REFERENCE_BOX_VERIFY", verify)

    return set_knobs


def _box_prompts(model: FakeModel, step: str) -> list[dict]:
    """The loop's prompts for one step: ``ask``, ``refine`` (the zoomed ask)
    or ``verify``."""
    if step == "verify":
        return [p for k, p in model.calls if k == "verify"]
    zoomed = step == "refine"
    return [p for k, p in model.calls
            if k == "bbox" and ("zoomed" in FakeModel.texts(p)[-1]) == zoomed]


def _calls_total(kind: str, outcome: str) -> float:
    from prometheus_client import REGISTRY

    return REGISTRY.get_sample_value(
        "classifier_reference_calls_total", {"kind": kind, "outcome": outcome}) or 0.0


def _record(rid: str, polarity: str) -> dict:
    from references.model import composite_name

    return {"reference_id": rid, "criterion": HOUSE, "polarity": polarity,
            "composite": composite_name(HOUSE)}


def test_a_guided_locate_job_runs_the_guided_loop(client, model, refs, knobs, monkeypatch):
    """A ZT-526 locate job — `score: false`, presence, boxes — with a PASS and
    a presence FAIL example: every step carries them, before the candidate."""
    from llm import boxes as llm_boxes
    from references.model import composite_name

    knobs()
    monkeypatch.setattr(llm_boxes, "LLM_BBOX_REFINE", True)
    job = _assess(client, {
        "criteria": [{"name": HOUSE, "type": "llm", "score": False,
                      "options": {"hint": "presence", "boxes": True}}],
        "references": [refs["pass"], refs["fail"]],
    })
    entry = _entry(job)
    assert entry["status"] == "ok" and entry["score"] is None
    loc = entry["localization"]
    assert loc["accepted_attempt"] == 1 and loc["calls"] == 3

    (ask,) = _box_prompts(model, "ask")
    assert FakeModel.images(ask) == 3
    texts = FakeModel.texts(ask)
    assert texts[0] == ("REFERENCE EXAMPLE — 'has a house' was PASS here; the coloured box "
                        "outlines it.")
    assert texts[1] == ("REFERENCE EXAMPLE — 'has a house' was FAIL here — the outlined thing "
                        "is NOT has a house; do not box it.")
    assert texts[2].endswith("CANDIDATE — locate the feature only in this image:")
    # The ask's example IS the stored composite — what production renders once.
    stored = client.get(f"/references/{refs['pass']}/files/{composite_name(HOUSE)}").content
    assert ask["messages"][1]["content"][1]["image_url"]["url"].endswith(
        base64.b64encode(stored).decode())
    for step in ("refine", "verify"):
        (prompt,) = _box_prompts(model, step)
        assert FakeModel.images(prompt) == 3
        assert FakeModel.texts(prompt)[0] == (
            "REFERENCE EXAMPLE — a crop showing 'has a house' (PASS here).")

    assert loc["references"] == [{
        "item": 0, "applied": True, "ask_images": "composite",
        "examples": [_record(refs["pass"], "pass"), _record(refs["fail"], "fail")],
        "ask": True, "refine": True, "verify": True, "note": None,
    }]
    assert entry["items"][0]["localization"]["references"] == loc["references"]


def test_box_calls_are_recorded_with_their_kind_including_a_failed_one(
    client, model, refs, knobs
):
    knobs(refine=False)
    model.verify = httpx.ConnectError("verify down")   # every verify call fails
    before = {(k, o): _calls_total(k, o) for k in ("scoring", "box_ask", "box_verify")
              for o in ("ok", "failed")}
    job = _assess(client, {"criteria": [_house(boxes=True)], "references": [refs["pass"]]})
    entry = _entry(job)
    # Nothing accepted, the score untouched — the loop never moves it.
    assert entry["status"] == "ok" and entry["score"] == 9
    assert entry["localization"]["accepted_attempt"] is None
    assert entry["localization"]["references"][0]["applied"] is True

    delta = {key: _calls_total(*key) - value for key, value in before.items()}
    assert delta == {("scoring", "ok"): 1, ("scoring", "failed"): 0,
                     ("box_ask", "ok"): 3, ("box_ask", "failed"): 0,
                     ("box_verify", "ok"): 0, ("box_verify", "failed"): 3}

    stored = client.get(f"/jobs/{job['job_id']}/artifacts/references.json").json()
    assert [c["kind"] for c in stored["calls"]] == ["scoring"] + ["box_ask", "box_verify"] * 3
    scoring, ask, verify = stored["calls"][:3]
    assert scoring["images"] == 2 and "attempt" not in scoring
    assert ask == {"criterion": HOUSE, "item": 0, "kind": "box_ask", "attempt": 1,
                   "images": 2, "example_images": "composite",
                   "examples": [_record(refs["pass"], "pass")], "ok": True}
    assert (verify["kind"], verify["example_images"], verify["ok"]) == ("box_verify", "crop", False)
    # The top-level count stays the guided SCORING calls.
    assert job["result"]["references"]["calls"] == 1


def test_a_two_page_criterion_merges_one_entry_per_page(client, model, refs, knobs):
    knobs()
    r = client.post("/assess", json={
        "documents": [_doc(), _doc()], "criteria": [_house(boxes=True)],
        "references": [refs["pass"]],
    })
    assert r.status_code == 202, r.text
    job = _wait(client, f"/jobs/{r.json()['job_id']}", lambda b: b.get("phase") == "completed")
    entry = _entry(job)
    merged = entry["localization"]["references"]
    assert [(e["item"], e["applied"]) for e in merged] == [(0, True), (1, True)]
    assert [u["localization"]["references"] for u in entry["items"]] == [[merged[0]], [merged[1]]]


def test_unguided_loops_say_why(client, model, refs, knobs):
    """The loop runs as today — one image — and the entry says why: a
    whole-page-only guide, the knobs off, or a guide that never applied."""
    knobs()
    job = _assess(client, {"criteria": [_house(boxes=True)], "references": [refs["whole"]]})
    assert {FakeModel.images(p) for k, p in model.calls if k in ("bbox", "verify")} == {1}
    assert _entry(job)["localization"]["references"] == [{
        "item": 0, "applied": False, "ask_images": None, "examples": [],
        "ask": False, "refine": False, "verify": False,
        "note": "no example has a region to show the box loop (a whole-page example "
                "teaches no location)",
    }]

    knobs(ask=False, refine=False, verify=False)
    job = _assess(client, {"criteria": [_house(boxes=True)], "references": [refs["pass"]]})
    (entry,) = _entry(job)["localization"]["references"]
    assert entry["applied"] is False
    assert entry["note"] == "the CLASSIFIER_REFERENCE_BOX_* knobs are off"

    knobs()
    job = _assess(client, {"criteria": [{"name": "a barn", "type": "llm", "options": {
        "hint": "presence", "boxes": True}}], "references": [refs["pass"]]})
    (entry,) = _entry(job, "a barn")["localization"]["references"]
    assert entry["applied"] is False and "no listed reference" in entry["note"]


def test_a_missing_page_jpg_drops_the_example_and_keeps_the_unit(client, model, knobs):
    from references.store import reference_files

    rid = _reference(client, reason="its page goes missing")
    assert reference_files.delete_file(rid, "page.jpg")
    knobs()
    job = _assess(client, {"criteria": [_house(boxes=True)], "references": [rid]})
    entry = _entry(job)
    assert entry["status"] == "ok" and entry["score"] == 9
    assert entry["localization"]["accepted_attempt"] == 1
    (ref,) = entry["localization"]["references"]
    assert ref["applied"] is False and ref["examples"] == []
    assert f"{rid}: its page image (page.jpg) is missing" in ref["note"]
    assert {FakeModel.images(p) for k, p in model.calls if k in ("bbox", "verify")} == {1}
    assert client.delete(f"/references/{rid}").status_code == 204


def test_a_reference_deleted_before_the_loop_drops_the_example_and_keeps_the_unit(
    client, model, knobs, monkeypatch
):
    """Deleted after the scoring call read its composite and before the loop
    loaded anything: the scoring answer stands and the loop runs unguided."""
    import analysis
    from analysis.references import JobReferences
    from api.schemas import AssessRequest
    from references.resolve import resolve_references
    from references.store import reference_files, reference_registry

    rid = _reference(client, reason="deleted mid-job")
    request = AssessRequest.model_validate({
        "document": {"type": "text", "data": "x"},
        "criteria": [_house(boxes=True)], "references": [rid],
    })
    plan = asyncio.run(resolve_references(request, reference_registry)).plan
    original = JobReferences.box_examples

    async def deleted_first(self, guide, spec, *, position_check):
        reference_files.delete(rid)
        self._images.clear()        # as if the scoring call had never cached it
        return await original(self, guide, spec, position_check=position_check)

    monkeypatch.setattr(JobReferences, "box_examples", deleted_first)
    knobs()
    doc = analysis.load_document_bytes(_png(), "p.png", "image/png", keep_source=True)
    result = asyncio.run(analysis.analyze_document(doc, request.criteria, references_plan=plan))
    entry = result["assessment"]["per_criterion_scores"][HOUSE]
    assert entry["status"] == "ok" and entry["score"] == 9
    (ref,) = entry["localization"]["references"]
    assert ref["applied"] is False
    assert "was deleted after this job was submitted" in ref["note"]
    assert client.delete(f"/references/{rid}").status_code == 204


def test_a_fail_is_shown_to_the_loop_only_under_a_presence_hint(
    client, model, refs, knobs
):
    import analysis
    from api.schemas import AssessRequest
    from references.resolve import resolve_references
    from references.store import reference_registry

    auto_fail = _reference(client, criteria=[{"name": HOUSE, "type": "llm",
                                              "options": {"hint": "auto"}}],
                           score=2, reason="an auto-hint barn")
    knobs()
    job = _assess(client, {"criteria": [_house(boxes=True)],
                           "references": [refs["pass"], auto_fail]})
    # The scoring call is still contrastive; the loop sees the PASS alone.
    assert [FakeModel.images(p) for p in model.scoring()] == [3]
    (ask,) = _box_prompts(model, "ask")
    assert FakeModel.images(ask) == 2 and "was PASS here" in FakeModel.texts(ask)[0]
    (ref,) = _entry(job)["localization"]["references"]
    assert ref["examples"] == [_record(refs["pass"], "pass")]

    # A plan queued before `hint` existed: its FAIL is not presence either.
    request = AssessRequest.model_validate({
        "document": {"type": "text", "data": "x"},
        "criteria": [_house(boxes=True)], "references": [refs["pass"], refs["fail"]],
    })
    plan = asyncio.run(resolve_references(request, reference_registry)).plan
    for example in plan["criteria"][HOUSE]["examples"]:
        assert example.pop("hint") == "presence"
    model.calls.clear()
    doc = analysis.load_document_bytes(_png(), "p.png", "image/png", keep_source=True)
    result = asyncio.run(analysis.analyze_document(doc, request.criteria, references_plan=plan))
    (ask,) = _box_prompts(model, "ask")
    assert FakeModel.images(ask) == 2
    (ref,) = result["assessment"]["per_criterion_scores"][HOUSE]["localization"]["references"]
    assert [e["polarity"] for e in ref["examples"]] == ["pass"]


def test_the_position_check_shows_the_ask_crops(client, model, refs, knobs):
    knobs()
    entry = _entry(_positioned(client, refs))
    (ask,) = _box_prompts(model, "ask")
    assert FakeModel.images(ask) == 2
    assert FakeModel.texts(ask)[0] == (
        "REFERENCE EXAMPLE — a crop showing 'has a house' (PASS here).")
    (ref,) = entry["localization"]["references"]
    assert ref["ask_images"] == "crop" and ref["applied"] is True


def test_the_position_check_is_unchanged_by_what_the_loop_was_shown(
    client, model, refs, knobs
):
    """The check compares the accepted box with the examples' OWN regions;
    showing the loop those examples changes neither its inputs nor its
    verdict."""
    model.bbox = FAR_BOX
    knobs(ask=False, refine=False, verify=False)
    unguided = _entry(_positioned(client, refs))
    knobs()
    guided = _entry(_positioned(client, refs))
    assert guided["localization"]["references"][0]["applied"] is True
    assert guided["detail"]["reference"]["position"] == unguided["detail"]["reference"]["position"]
    assert guided["detail"]["reference"]["position"]["status"] == "miss"
    assert (guided["score"], guided["verdict"]) == (unguided["score"], unguided["verdict"])


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
    # No `references` key — `progress` is the queue's gauge, on every job.
    assert set(job["metadata"]) == {"type", "request_id", "progress"}
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


def test_auto_admits_and_picks_by_key(client, model):
    """The pool admits a reference by key, and never by the name of a keyed
    criterion; the per-item pick looks the key up (``examples_by_key``)."""
    by_key = _reference(client, tags=["auto-key"], criteria=[_shape(SQUARE, "auto-shape")],
                        reason="auto keyed square")
    by_name = _reference(client, tags=["auto-key"], criteria=[_shape(RECTANGLE)],
                         reason="auto rectangle, no key")
    job = _assess(client, {
        "criteria": [_shape(RECTANGLE, "AUTO-SHAPE")], "references": _auto("auto-key"),
    })
    block = job["result"]["references"]
    assert block["pool"] == [by_key]          # by_name matches only the NAME: not admitted
    assert (block["criteria"][RECTANGLE]["matched_by"],
            block["criteria"][RECTANGLE]["matched"]) == ("key", "AUTO-SHAPE")
    ref = _entry(job, RECTANGLE)["detail"]["reference"]
    assert ref["applied"] is True and ref["mode"] == "auto"
    assert [(e["reference_id"], e["criterion"]) for e in ref["examples"]] == [(by_key, SQUARE)]
    assert by_name not in json.dumps(model.selections())

    # A key that no pool reference carries scores unguided, and says which key.
    model.calls.clear()
    job = _assess(client, {
        "criteria": [_shape(RECTANGLE, "auto-shape"), _shape("is this a kite", "kite-key")],
        "references": _auto("auto-key"),
    })
    ref = _entry(job, "is this a kite")["detail"]["reference"]
    assert ref["applied"] is False
    assert "usable criterion with key 'kite-key' at confidence" in ref["note"]
    assert _entry(job, RECTANGLE)["detail"]["reference"]["applied"] is True


def test_an_old_auto_plan_still_guides_by_name(client, model, auto_refs):
    """A plan queued before keys — no ``matched_by``, no ``examples_by_key`` —
    picks by name exactly as it always did."""
    import analysis
    from api.schemas import AssessRequest
    from references.resolve import resolve_references
    from references.store import reference_registry

    request = AssessRequest.model_validate({
        "document": {"type": "text", "data": "x"},
        "criteria": [_house()], "references": _auto("auto-a"),
    })
    plan = asyncio.run(resolve_references(request, reference_registry)).plan
    plan.pop("examples_by_key")
    for entry in plan["criteria"].values():
        entry.pop("matched_by")
    assert set(plan["criteria"][HOUSE]) == {"matched", "options"}   # the old shape

    doc = analysis.load_document_bytes(_png(), "p.png", "image/png", keep_source=True)
    result = asyncio.run(analysis.analyze_document(doc, request.criteria, references_plan=plan))
    ref = result["assessment"]["per_criterion_scores"][HOUSE]["detail"]["reference"]
    assert ref["applied"] is True
    assert {e["reference_id"] for e in ref["examples"]} == {auto_refs["pass"], auto_refs["fail"]}
    assert result["references"]["criteria"][HOUSE]["matched_by"] == "name"


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
