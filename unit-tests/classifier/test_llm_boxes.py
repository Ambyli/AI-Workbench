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
    """Point the loop at ``model`` and run one criterion through it.

    The grid overlay and the refine pass are OFF here unless a test turns
    them on: these tests script the model call by call, and the two aids
    each change the call sequence — which the tests under "The grid and the
    refine pass" cover on their own.
    """
    monkeypatch.setattr(llm_client, "call_vllm_json", model)
    kwargs.setdefault("gridlines", False)
    kwargs.setdefault("refine", False)
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
# The grid and the refine pass
# ---------------------------------------------------------------------------
#
# Both exist because of a measurement (config.LLM_BBOX_GRIDLINES): the bare
# ask put a text line's box 25-100 grid units off on one axis; a labelled
# grid halved that, and a second ask on a zoomed crop of the original with
# its own grid brought it within a few units. What is pinned here is the
# MECHANICS — the call sequence, the window, the mapping back, what is
# recorded, and that a failed refine keeps the coarse box — not the model.


@pytest.fixture
def working():
    """The ≤1000-px page the scoring image came from: half the original."""
    return np.full((GEOMETRY.height // 2, GEOMETRY.width // 2, 3), 200, dtype=np.uint8)


def test_gridlines_change_the_ask_image_and_say_so(monkeypatch, page, working):
    """With a working image the ask sees a gridded copy, and the prompt tells
    the model to read the grid; the verify crop is untouched by either."""
    model = ScriptedModel([
        {"bbox": GOOD_BOX},
        {"score": 9, "reason": "panels"},
    ])
    _run(model, monkeypatch, page, working_image=working, gridlines=True)

    ask_url = model.calls[0][1]["messages"][1]["content"][0]["image_url"]["url"]
    assert ask_url != "data:image/jpeg;base64,ZmFrZQ=="
    assert "coordinate grid is drawn over the image" in model.user_text(0)
    assert "every 100 units" in model.user_text(0)
    verify_text = model.user_text(1)
    assert "coordinate grid" not in verify_text


def test_gridlines_without_a_working_image_ask_on_the_bare_page(monkeypatch, page):
    """No pixels to draw on → the scoring image goes out unchanged, and the
    prompt must not claim a grid that is not there."""
    model = ScriptedModel([
        {"bbox": GOOD_BOX},
        {"score": 9, "reason": "panels"},
    ])
    _run(model, monkeypatch, page, gridlines=True)
    ask_url = model.calls[0][1]["messages"][1]["content"][0]["image_url"]["url"]
    assert ask_url == "data:image/jpeg;base64,ZmFrZQ=="
    assert "coordinate grid" not in model.user_text(0)


def test_refine_window_zooms_clamps_and_honours_the_minimum():
    # 200×200 box × 2.5 = 500 wide, centred on (200, 300): x clamps at 0.
    assert llm_boxes.refine_window(GOOD_BOX, zoom=2.5, min_span=0.2) == [0.0, 50.0, 450.0, 550.0]
    # A thin line: 140×14 × 2.5 = 350×35 → the y span rises to the 200 floor.
    window = llm_boxes.refine_window([400, 500, 540, 514], zoom=2.5, min_span=0.2)
    assert window == pytest.approx([295.0, 407.0, 645.0, 607.0])
    # Never past the far edge either.
    assert llm_boxes.refine_window([900, 900, 1000, 1000], zoom=2.5, min_span=0.2)[2:] == [1000.0, 1000.0]


def test_window_to_full_maps_the_crop_frame_back():
    window = [0.0, 50.0, 450.0, 550.0]
    assert llm_boxes.window_to_full([400, 400, 600, 600], window) == pytest.approx(
        [180.0, 250.0, 270.0, 350.0]
    )


def test_crop_window_cuts_the_original_and_fits_the_working_size(page):
    crop = llm_boxes.crop_window(page, [0.0, 50.0, 450.0, 550.0])
    # 800×1200 page: x 0-360 px, y 60-660 px → 600 tall, 360 wide; under 1000.
    assert crop.shape[:2] == (600, 360)
    big = np.zeros((3000, 4000, 3), dtype=np.uint8)
    crop = llm_boxes.crop_window(big, [0.0, 0.0, 1000.0, 1000.0])
    assert max(crop.shape[:2]) == 1000
    assert llm_boxes.crop_window(None, [0, 0, 100, 100]) is None


def test_refine_asks_on_the_zoomed_crop_and_maps_back(monkeypatch, page, working):
    """ask → refine → verify: the refined box, in the page frame, is what is
    verified and stored; the coarse box and the window are kept beside it."""
    model = ScriptedModel([
        {"bbox": GOOD_BOX, "reason": "roughly there"},          # coarse, page frame
        {"bbox": [400, 400, 600, 600], "reason": "exactly there"},  # fine, crop frame
        {"score": 9, "reason": "a solar array fills the crop"},
    ])
    regions, loc = _run(model, monkeypatch, page, working_image=working, gridlines=True, refine=True)

    assert model.labels() == [
        "bbox/has solar panels#1",
        "refine/has solar panels#1",
        "verify/has solar panels#1",
    ]
    assert loc.calls == 3 and loc.accepted_attempt == 1
    attempt = loc.attempts[0]
    assert attempt.refined is True
    assert attempt.coarse_bbox_grid == [100.0, 200.0, 300.0, 400.0]
    assert attempt.refine_window_grid == [0.0, 50.0, 450.0, 550.0]
    assert attempt.bbox_grid == pytest.approx([180.0, 250.0, 270.0, 350.0])
    # 800×1200 page: x × 0.8, y × 1.2.
    assert attempt.bbox_px == pytest.approx([144.0, 300.0, 216.0, 420.0])
    assert regions[0].points == pytest.approx([(144.0, 300.0), (216.0, 420.0)])
    assert regions[0].attrs["refined"] is True

    # The refine prompt says it is a crop, and carries the grid sentence.
    refine_text = model.user_text(1)
    assert "zoomed-in crop" in refine_text and "coordinate grid" in refine_text

    data = attempt.as_dict()
    assert data["refined"] is True
    assert data["coarse_bbox_grid"] == [100.0, 200.0, 300.0, 400.0]
    assert data["refine_window_grid"] == [0.0, 50.0, 450.0, 550.0]
    assert "refine_reject" not in data


def test_refine_failure_keeps_the_coarse_box(monkeypatch, page, working):
    """An unusable second answer costs a call and changes nothing else."""
    model = ScriptedModel([
        {"bbox": GOOD_BOX},
        None,                                  # the refine call failed
        {"score": 8, "reason": "panels"},
    ])
    regions, loc = _run(model, monkeypatch, page, working_image=working, refine=True)
    attempt = loc.attempts[0]
    assert loc.calls == 3 and attempt.accepted is True
    assert attempt.refined is False
    assert attempt.bbox_grid == [100.0, 200.0, 300.0, 400.0]
    assert "usable answer for the refine pass" in attempt.refine_reject
    assert attempt.as_dict()["refine_reject"] == attempt.refine_reject
    assert regions[0].attrs["refined"] is False


def test_refine_does_not_run_for_a_rejected_coarse_box(monkeypatch, page, working):
    """Nothing to zoom into when the first answer was the whole frame."""
    model = ScriptedModel([
        {"bbox": FULL_FRAME},
        {"bbox": GOOD_BOX},
        {"bbox": [400, 400, 600, 600]},
        {"score": 9, "reason": "panels"},
    ])
    _, loc = _run(model, monkeypatch, page, working_image=working, refine=True)
    assert model.labels() == [
        "bbox/has solar panels#1",
        "bbox/has solar panels#2",
        "refine/has solar panels#2",
        "verify/has solar panels#2",
    ]
    assert loc.attempts[0].coarse_bbox_grid is None
    assert "coarse_bbox_grid" not in loc.attempts[0].as_dict()
    assert loc.attempts[1].refined is True


def test_refine_without_an_original_image_is_recorded_not_fatal(monkeypatch, working):
    """The page image is what gets cropped; without it the coarse box stands."""
    model = ScriptedModel([{"bbox": GOOD_BOX}])
    _, loc = _run(model, monkeypatch, None, working_image=working, refine=True)
    attempt = loc.attempts[0]
    assert attempt.refined is False
    assert "not available to crop for the refine pass" in attempt.refine_reject
    assert loc.calls == 1  # no refine call, and no verify call either (no page)


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
    # These tests script ask/verify pairs; the refine pass would consume the
    # verify answers. The grid changes no call sequence but is off for parity.
    monkeypatch.setattr(llm_boxes, "LLM_BBOX_REFINE", False)
    monkeypatch.setattr(llm_boxes, "LLM_BBOX_GRIDLINES", False)
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
