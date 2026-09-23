"""The LLM bounding-box enforcement loop, driven by a scripted fake model.

No vLLM, no network: ``llm.client.call_vllm_json`` is replaced with a list of
answers, which is exactly what the loop is for — the interesting cases are
all "the model said something wrong", and a real model cannot be asked to say
something wrong on cue.

What is asserted here, in the order the loop does it:

  * a full-frame box is rejected, the REJECTION reaches the next prompt as
    feedback, and a good box on attempt 2 is accepted;
  * a box that validates but whose crop does not show the feature is rejected
    three times and the criterion ends ``exhausted`` — with every attempt
    still returned, ``accepted_attempt: null``, and the SCORE AND VERDICT
    untouched (the property the whole design rests on);
  * a presence score under the floor skips the loop entirely — no calls at all;
  * the 0-1000 grid converts through a NON-SQUARE page correctly (the axis
    bug that a square fixture would hide);
  * the detector cross-check lands in ``attrs.detector_iou`` when phase 2's
    boxes are there, and is absent when they are not.

Run with::

    UV_LINK_MODE=copy uv run --no-sync --with pytest --package classifier \\
        python -m pytest unit-tests/classifier/test_llm_boxes.py -q -p no:cacheprovider
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from common.vision import PageGeometry, Region

from api.schemas import CriterionInput
from llm import boxes as llm_boxes
from llm import client as llm_client

# A page that is NOT square, so an x/y axis mix-up in grid_to_pixels shows up
# as a wrong number rather than the same number twice.
GEOMETRY = PageGeometry(page=0, width=800, height=1200, working_scale=0.5)

# A box well inside both area bounds: 200×200 grid units is 4% of the frame.
GOOD_BOX = [100, 200, 300, 400]
FULL_FRAME = [0, 0, 1000, 1000]


class ScriptedModel:
    """A ``call_vllm_json`` stand-in that answers from a list, in order.

    Records every prompt it was given so a test can assert on the FEEDBACK —
    the retry carrying the previous rejection is half of what the loop does,
    and it is invisible in the result.
    """

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, prompt, *, label=""):
        self.calls.append((label, prompt))
        return self.answers.pop(0) if self.answers else None

    def user_text(self, index: int) -> str:
        """The text part of the nth call's user message."""
        content = self.calls[index][1]["messages"][-1]["content"]
        return next(part["text"] for part in content if part["type"] == "text")

    def labels(self) -> list[str]:
        return [label for label, _ in self.calls]


@pytest.fixture
def page():
    """A plain BGR page at the geometry's ORIGINAL size, for the verify crop."""
    return np.full((GEOMETRY.height, GEOMETRY.width, 3), 200, dtype=np.uint8)


def _run(model, monkeypatch, page, **kwargs):
    """Point the loop at ``model`` and run one criterion through it."""
    monkeypatch.setattr(llm_client, "call_vllm_json", model)
    return asyncio.run(
        llm_boxes.locate_criterion(
            "has solar panels",
            image_b64="ZmFrZQ==",
            original_image=page,
            geometry=GEOMETRY,
            **kwargs,
        )
    )


# ---------------------------------------------------------------------------
# The happy path, reached the hard way
# ---------------------------------------------------------------------------


def test_full_frame_rejected_then_feedback_then_accepted(monkeypatch, page):
    """Attempt 1 boxes the whole image; attempt 2, told why, boxes the thing."""
    model = ScriptedModel([
        {"bbox": FULL_FRAME, "confidence": 80, "reason": "it is in the photo"},
        {"bbox": GOOD_BOX, "confidence": 90, "reason": "panels on the roof"},
        {"score": 9, "reason": "a solar array fills the crop"},
    ])
    regions, loc = _run(model, monkeypatch, page)

    # Two asks and ONE verify: attempt 1 never got as far as a crop.
    assert model.labels() == [
        "bbox/has solar panels#1",
        "bbox/has solar panels#2",
        "verify/has solar panels#2",
    ]

    assert loc.accepted_attempt == 2
    assert loc.calls == 3
    assert [a.attempt for a in loc.attempts] == [1, 2]

    first, second = loc.attempts
    assert first.valid is False
    assert "covered 100% of the image" in first.reject
    assert "try again, smaller" in first.reject
    assert first.verify_score is None  # no crop call was ever made for it

    assert second.valid is True
    assert second.accepted is True
    assert second.verify_score == 9

    # The rejection reached the model on the retry — that is the "feedback"
    # half of the loop, and it is otherwise invisible.
    retry_text = model.user_text(1)
    assert "covered 100% of the image" in retry_text
    assert "Give a DIFFERENT box this time" in retry_text

    # Every attempt comes back as a region, accepted first.
    assert [r.attrs["attempt"] for r in regions] == [2, 1]
    assert regions[0].attrs["accepted"] is True
    assert regions[0].source == "llm"
    assert regions[0].score == 9
    assert regions[1].attrs["accepted"] is False


def test_verify_call_label_names_its_attempt(monkeypatch, page):
    """The verify call is labelled with the attempt whose box it checks."""
    model = ScriptedModel([
        {"bbox": FULL_FRAME},
        {"bbox": GOOD_BOX},
        {"score": 8, "reason": "panels"},
    ])
    _run(model, monkeypatch, page)
    assert model.labels()[2] == "verify/has solar panels#2"


# ---------------------------------------------------------------------------
# Never accepted
# ---------------------------------------------------------------------------


def test_exhausted_keeps_every_attempt_and_accepts_none(monkeypatch, page):
    """Three usable boxes, three crops that show nothing → nothing accepted."""
    model = ScriptedModel([
        {"bbox": [100, 200, 300, 400]},
        {"score": 3, "reason": "just roof tiles"},
        {"bbox": [400, 200, 600, 400]},
        {"score": 2, "reason": "a chimney"},
        {"bbox": [100, 600, 300, 800]},
        {"score": 3, "reason": "grass"},
    ])
    regions, loc = _run(model, monkeypatch, page)

    assert loc.accepted_attempt is None
    assert len(loc.attempts) == 3
    assert loc.calls == 6  # one ask + one verify per attempt
    assert all(a.valid and not a.accepted for a in loc.attempts)
    assert all(a.verify_score is not None for a in loc.attempts)
    assert "did not show has solar panels (verify score 3)" in loc.attempts[0].reject
    assert "look elsewhere" in loc.attempts[0].reject

    # All three boxes are still returned, all marked rejected, so `?attempt=n`
    # can render any of them.
    assert len(regions) == 3
    assert all(r.attrs["accepted"] is False for r in regions)
    assert {r.attrs["attempt"] for r in regions} == {1, 2, 3}

    # Each retry carried the PREVIOUS rejection.
    assert "verify score 3" in model.user_text(2)
    assert "verify score 2" in model.user_text(4)


def test_bbox_null_stops_the_loop(monkeypatch, page):
    """"Not visible here" is an answer; re-asking it buys a guess."""
    model = ScriptedModel([
        {"bbox": None, "confidence": 10, "reason": "no array on this roof"},
        {"bbox": GOOD_BOX},  # never reached
    ])
    regions, loc = _run(model, monkeypatch, page)

    assert len(loc.attempts) == 1
    assert loc.calls == 1
    assert loc.accepted_attempt is None
    assert "not visible" in loc.attempts[0].reject
    assert regions == []  # nothing to draw


def test_model_failure_is_a_rejected_attempt_not_an_exception(monkeypatch, page):
    """A None from call_vllm_json (HTTP error, unparseable) is survivable."""
    model = ScriptedModel([None, None, None])
    regions, loc = _run(model, monkeypatch, page)
    assert len(loc.attempts) == 3
    assert loc.accepted_attempt is None
    assert regions == []


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "criterion, entry, expected",
    [
        (dict(type="llm", hint="presence"), {"score": 10}, True),
        (dict(type="llm", hint="auto"), {"score": 7}, True),
        (dict(type="llm", hint="presence"), {"score": 6}, False),
        (dict(type="llm", hint="quality"), {"score": 10}, False),
        (dict(type="cv"), {"score": 10}, False),
        (dict(type="text"), {"score": 10}, False),
        (dict(type="llm", hint="presence"), {"verdict": "SKIPPED", "score": None}, False),
        (dict(type="llm", hint="presence"), None, False),
    ],
)
def test_wants_boxes(criterion, entry, expected):
    c = CriterionInput(name="has solar panels", **criterion)
    assert llm_boxes.wants_boxes(c, entry) is expected


def test_low_presence_score_never_calls_the_model(monkeypatch, page):
    """The model said the feature is absent — there is nothing to locate."""
    model = ScriptedModel([{"bbox": GOOD_BOX}])
    monkeypatch.setattr(llm_client, "call_vllm_json", model)
    regions, locs = asyncio.run(
        llm_boxes.locate_criteria(
            [CriterionInput(name="has solar panels", type="llm", hint="presence")],
            {"has solar panels": {"score": 3, "verdict": "FAIL"}},
            image_b64="ZmFrZQ==",
            original_image=page,
            geometry=GEOMETRY,
        )
    )
    assert regions == {} and locs == {}
    assert model.calls == []


def test_no_page_image_never_calls_the_model(monkeypatch, page):
    """A .txt / .docx has no pixel space for a box to live in."""
    model = ScriptedModel([{"bbox": GOOD_BOX}])
    monkeypatch.setattr(llm_client, "call_vllm_json", model)
    regions, locs = asyncio.run(
        llm_boxes.locate_criteria(
            [CriterionInput(name="has solar panels", type="llm", hint="presence")],
            {"has solar panels": {"score": 10}},
            image_b64=None,
            original_image=None,
            geometry=None,
        )
    )
    assert regions == {} and locs == {} and model.calls == []


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, fragment",
    [
        (FULL_FRAME, "covered 100%"),
        ([0, 0, 5, 5], "under the 0.20% floor"),
        ([300, 200, 100, 400], "no width or no height"),
        ([100, 200, 100, 400], "no width or no height"),
        ([-40, 200, 300, 400], "ran outside the 0-1000 grid"),
        ([100, 200, 300, 1400], "ran outside the 0-1000 grid"),
        ([100, 200, 300], "was not four numbers"),
        ("somewhere on the roof", "was not four numbers"),
        (None, "not visible"),
    ],
)
def test_validate_bbox_rejections(raw, fragment):
    _, reject = llm_boxes.validate_bbox(raw)
    assert reject is not None and fragment in reject


def test_validate_bbox_accepts_a_reasonable_box():
    bbox, reject = llm_boxes.validate_bbox(GOOD_BOX)
    assert reject is None
    assert bbox == [100.0, 200.0, 300.0, 400.0]


def test_validate_bbox_missing_key_says_so():
    _, reject = llm_boxes.validate_bbox(None, present=False)
    assert "no 'bbox' key" in reject


def test_numeric_strings_are_accepted():
    """A model in JSON mode occasionally quotes its numbers."""
    bbox, reject = llm_boxes.validate_bbox(["100", "200", "300", "400"])
    assert reject is None and bbox == [100.0, 200.0, 300.0, 400.0]


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


def test_grid_converts_per_axis_on_a_non_square_page(monkeypatch, page):
    """800×1200 means x scales by 0.8 and y by 1.2 — not one factor for both."""
    model = ScriptedModel([
        {"bbox": GOOD_BOX},
        {"score": 10, "reason": "panels"},
    ])
    regions, loc = _run(model, monkeypatch, page)

    assert loc.attempts[0].bbox_grid == [100.0, 200.0, 300.0, 400.0]
    assert loc.attempts[0].bbox_px == pytest.approx([80.0, 240.0, 240.0, 480.0])
    assert regions[0].points == [(80.0, 240.0), (240.0, 480.0)]
    # Inside the page, which is the invariant every region source shares.
    for x, y in regions[0].points:
        assert 0 <= x <= GEOMETRY.width and 0 <= y <= GEOMETRY.height


def test_box_is_clamped_to_the_page(monkeypatch, page):
    """A box at the very edge of the grid must not land a pixel off-page."""
    model = ScriptedModel([
        {"bbox": [0, 0, 1000, 500]},  # 50% of the frame — inside MAX_AREA
        {"score": 8, "reason": "panels"},
    ])
    regions, loc = _run(model, monkeypatch, page)
    assert loc.attempts[0].bbox_px == pytest.approx([0.0, 0.0, 800.0, 600.0])
    assert regions[0].attrs["accepted"] is True


def test_crop_pads_and_clamps(page):
    """The verify crop is padded outward and never leaves the image."""
    crop = llm_boxes.crop_for(page, [0.0, 0.0, 100.0, 100.0], pad=0.10)
    assert crop.shape[0] == 110 and crop.shape[1] == 110  # clamped at 0, padded at 100
    inner = llm_boxes.crop_for(page, [400.0, 600.0, 500.0, 700.0], pad=0.10)
    assert inner.shape[0] == 120 and inner.shape[1] == 120


def test_crop_of_a_missing_image_is_none():
    assert llm_boxes.crop_for(None, [0, 0, 10, 10]) is None


# ---------------------------------------------------------------------------
# The detector cross-check
# ---------------------------------------------------------------------------


def _detector_region(points, score=0.6):
    return Region(
        page=0, kind="box", points=points, label="has solar panels",
        score=score, source="detector", attrs={"detector_score": score},
    )


def test_detector_iou_is_recorded_when_the_detector_found_something(monkeypatch, page):
    """The same box from both sources scores 1.0; it is informational only."""
    model = ScriptedModel([
        {"bbox": GOOD_BOX},
        {"score": 9, "reason": "panels"},
    ])
    regions, loc = _run(
        model, monkeypatch, page,
        detector_regions=[
            _detector_region([(80.0, 240.0), (240.0, 480.0)], 0.7),
            # A weaker box somewhere else: the cross-check uses the BEST one.
            _detector_region([(600.0, 900.0), (700.0, 1000.0)], 0.3),
        ],
    )
    assert loc.attempts[0].detector_iou == pytest.approx(1.0)
    assert regions[0].attrs["detector_iou"] == pytest.approx(1.0)


def test_detector_iou_of_a_disagreeing_box_is_zero(monkeypatch, page):
    model = ScriptedModel([
        {"bbox": GOOD_BOX},
        {"score": 9, "reason": "panels"},
    ])
    _, loc = _run(
        model, monkeypatch, page,
        detector_regions=[_detector_region([(600.0, 900.0), (700.0, 1000.0)])],
    )
    assert loc.attempts[0].detector_iou == 0.0


def test_no_detector_regions_means_no_iou_key(monkeypatch, page):
    """Absent, not zero: zero would mean "we compared and they disagreed"."""
    model = ScriptedModel([
        {"bbox": GOOD_BOX},
        {"score": 9, "reason": "panels"},
    ])
    regions, loc = _run(model, monkeypatch, page, detector_regions=[])
    assert loc.attempts[0].detector_iou is None
    assert "detector_iou" not in regions[0].attrs
    assert "detector_iou" not in loc.attempts[0].as_dict()


# ---------------------------------------------------------------------------
# The serialised shape callers actually read
# ---------------------------------------------------------------------------


def _png_bytes(width=400, height=600):
    """A small PNG for the pipeline tests — content is irrelevant, the model
    is scripted; only the pixel frame matters."""
    import cv2

    array = np.full((height, width, 3), 180, dtype=np.uint8)
    return cv2.imencode(".png", array)[1].tobytes()


def _scoring_call(score: int):
    """A stand-in for ``analysis.pipeline.call_vllm``: one presence criterion,
    one score."""

    async def _call(prompt):
        return {
            "assessment": {
                "overall_verdict": "PASS",
                "overall_score": score,
                "per_criterion_scores": {
                    "has solar panels": {
                        "score": score, "verdict": "PASS", "confidence": 80,
                        "reason": "I observe panels. Therefore they are present.",
                    }
                },
            }
        }

    return _call


def _analyze(monkeypatch, *, llm_boxes_on, answers, score=10, job_id=None):
    """Run the whole pipeline on a synthetic PNG with a scripted model."""
    import analysis
    from analysis import pipeline
    from api.schemas import RegionsOptions

    monkeypatch.setattr(pipeline, "call_vllm", _scoring_call(score))
    monkeypatch.setattr(llm_client, "call_vllm_json", ScriptedModel(answers))
    doc = analysis.load_document_bytes(_png_bytes(), "page.png", "image/png")
    return asyncio.run(
        analysis.analyze_document(
            doc,
            [CriterionInput(name="has solar panels", type="llm", hint="presence")],
            "never",
            regions=RegionsOptions(enabled=True, layers=["svg"], llm_boxes=llm_boxes_on),
            job_id=job_id,
        )
    )


def test_pipeline_verdict_is_unchanged_by_an_exhausted_loop(monkeypatch):
    """The loop is additive: three rejected boxes, identical score and verdict."""
    without = _analyze(monkeypatch, llm_boxes_on=False, answers=[])
    with_loop = _analyze(
        monkeypatch,
        llm_boxes_on=True,
        answers=[
            {"bbox": [100, 200, 300, 400]}, {"score": 3, "reason": "tiles"},
            {"bbox": [400, 200, 600, 400]}, {"score": 2, "reason": "a vent"},
            {"bbox": [100, 600, 300, 800]}, {"score": 1, "reason": "grass"},
        ],
    )

    assert with_loop["verdict"] == without["verdict"]
    assert (
        with_loop["assessment"]["overall_score"]
        == without["assessment"]["overall_score"]
    )
    entry = with_loop["assessment"]["per_criterion_scores"]["has solar panels"]
    base = without["assessment"]["per_criterion_scores"]["has solar panels"]
    assert entry["score"] == base["score"] and entry["verdict"] == base["verdict"]

    # …and every attempt is there, all rejected.
    assert entry["localization"]["accepted_attempt"] is None
    assert len(entry["localization"]["attempts"]) == 3
    assert entry["regions"] and all(
        r["attrs"]["accepted"] is False for r in entry["regions"]
    )


def test_pipeline_without_llm_boxes_has_the_empty_localization_shape(monkeypatch):
    """A consumer can read localization.accepted_attempt without branching."""
    result = _analyze(monkeypatch, llm_boxes_on=False, answers=[])
    entry = result["assessment"]["per_criterion_scores"]["has solar panels"]
    assert entry["localization"] == {
        "attempts": [], "accepted_attempt": None, "calls": 0
    }
    assert entry["regions"] == [] and entry["artifacts"] is None


def test_pipeline_writes_localization_and_attempt_urls(monkeypatch):
    """regions.json carries the record; the result carries per-attempt URLs."""
    from common.vision import ArtifactStore
    from config import ARTIFACT_DIR
    from regions.collect import visible_regions

    result = _analyze(
        monkeypatch,
        llm_boxes_on=True,
        answers=[
            {"bbox": FULL_FRAME},
            {"bbox": [100, 200, 300, 400]},
            {"score": 9, "reason": "panels"},
        ],
        job_id="testjob1",
    )
    entry = result["assessment"]["per_criterion_scores"]["has solar panels"]
    slug = entry["artifacts"]["slug"]
    assert [a["attempt"] for a in entry["artifacts"]["attempts"]] == [1, 2]
    assert entry["artifacts"]["attempts"][0]["svg"] == (
        f"/jobs/testjob1/artifacts/p0.svg?criterion={slug}&attempt=1"
    )
    assert entry["artifacts"]["attempts"][0]["accepted"] is False
    assert entry["artifacts"]["attempts"][1]["accepted"] is True

    stored = ArtifactStore(ARTIFACT_DIR).read_json("testjob1", "regions.json")
    criterion = stored["criteria"]["has solar panels"]
    assert criterion["localization"]["accepted_attempt"] == 2
    assert criterion["sources"] == ["llm"]
    assert len(criterion["regions"]) == 2

    # The stored combined layer shows the ACCEPTED box only — one <rect> —
    # which is exactly what the default `?criterion=` filter re-renders.
    svg = ArtifactStore(ARTIFACT_DIR).open("testjob1", "p0.svg").decode()
    assert 'data-region-count="1"' in svg
    stored_regions = [Region.from_dict(r) for r in criterion["regions"]]
    assert len(visible_regions(stored_regions)) == 1


def test_localization_dict_shape(monkeypatch, page):
    model = ScriptedModel([
        {"bbox": FULL_FRAME},
        {"bbox": GOOD_BOX},
        {"score": 9, "reason": "a roof covered in panels"},
    ])
    _, loc = _run(model, monkeypatch, page)
    data = loc.as_dict()

    assert set(data) == {"attempts", "accepted_attempt", "calls"}
    assert data["accepted_attempt"] == 2 and data["calls"] == 3
    first, second = data["attempts"]
    assert first["valid"] is False and first["accepted"] is False
    assert "verify_score" not in first  # never ran one
    assert second["verify_score"] == 9 and second["accepted"] is True
    assert second["bbox_grid"] == [100.0, 200.0, 300.0, 400.0]
    assert second["bbox_px"] == [80.0, 240.0, 240.0, 480.0]
    assert second["verify_reason"] == "a roof covered in panels"
