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
  * ``options.boxes`` is off by default, ``options.max_attempts`` bounds the
    loop and can never exceed CLASSIFIER_LLM_BBOX_MAX_ATTEMPTS, and
    ``score: false`` keeps the geometry while dropping the judgement;
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
    "hint, entry, expected",
    [
        ("presence", {"score": 10}, True),
        ("auto", {"score": 7}, True),
        ("presence", {"score": 6}, False),
        ("quality", {"score": 10}, False),
        ("presence", {"score": None}, False),
        ("presence", None, False),
    ],
)
def test_wants_boxes(hint, entry, expected):
    assert llm_boxes.wants_boxes(hint, entry) is expected


def test_max_attempts_is_honoured_and_clamped_to_the_cap(monkeypatch, page):
    """A criterion's max_attempts lowers the loop's budget; nothing raises it."""
    rejected = [{"bbox": FULL_FRAME}] * 5
    model = ScriptedModel(rejected)
    _, loc = _run(model, monkeypatch, page, max_attempts=2)
    assert len(loc.attempts) == 2

    model = ScriptedModel(rejected)
    _, loc = _run(model, monkeypatch, page, max_attempts=99)
    assert len(loc.attempts) == llm_boxes.LLM_BBOX_MAX_ATTEMPTS


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
# The loop inside the pipeline: options.boxes, max_attempts, score: false
# ---------------------------------------------------------------------------


def _png_bytes(width=400, height=600):
    """A small PNG for the pipeline tests — content is irrelevant, the model
    is scripted; only the pixel frame matters."""
    import cv2

    array = np.full((height, width, 3), 180, dtype=np.uint8)
    return cv2.imencode(".png", array)[1].tobytes()


def _scoring_call(score: int, calls: list | None = None):
    """A stand-in for ``llm.client.call_vllm``: one flat answer, run through
    the validator the evaluator passes, exactly as the real call does."""

    async def _call(prompt, *, label="", validator=None):
        if calls is not None:
            calls.append(label)
        raw = {
            "score": score, "verdict": "PASS", "confidence": 80,
            "reason": "I observe panels. Therefore they are present.",
        }
        return validator(raw) if validator else raw

    return _call


def _criterion(**options):
    return CriterionInput(
        name="has solar panels", type="llm",
        options={"hint": "presence", **options},
    )


def _analyze(monkeypatch, criterion, *, answers, score=10, job_id=None, scoring_calls=None):
    """Run the whole pipeline on a synthetic PNG with a scripted model."""
    import analysis

    monkeypatch.setattr(llm_client, "call_vllm", _scoring_call(score, scoring_calls))
    model = ScriptedModel(answers)
    monkeypatch.setattr(llm_client, "call_vllm_json", model)
    # These tests script ask/verify pairs; the refine pass would consume the
    # verify answers. The grid changes no call sequence but is off for parity.
    monkeypatch.setattr(llm_boxes, "LLM_BBOX_REFINE", False)
    monkeypatch.setattr(llm_boxes, "LLM_BBOX_GRIDLINES", False)
    doc = analysis.load_document_bytes(_png_bytes(), "page.png", "image/png", keep_source=True)
    result = asyncio.run(analysis.analyze_document(doc, [criterion], job_id=job_id))
    return result, model


def _entry(result):
    return result["assessment"]["per_criterion_scores"]["has solar panels"]


def test_boxes_default_off_means_no_loop_calls(monkeypatch):
    """options.boxes defaults to false: a presence 10 is scored, never boxed."""
    result, model = _analyze(monkeypatch, _criterion(), answers=[{"bbox": GOOD_BOX}])
    entry = _entry(result)
    assert model.calls == []
    assert entry["options_used"]["boxes"] is False
    assert entry["localization"] == {"attempts": [], "accepted_attempt": None, "calls": 0}
    assert entry["regions"] == []


def test_low_presence_score_never_calls_the_loop(monkeypatch):
    """The model said the feature is absent — there is nothing to locate."""
    result, model = _analyze(
        monkeypatch, _criterion(boxes=True), answers=[{"bbox": GOOD_BOX}], score=3
    )
    assert model.calls == []
    assert _entry(result)["localization"]["calls"] == 0


def test_quality_hint_is_never_boxed(monkeypatch):
    result, model = _analyze(
        monkeypatch,
        CriterionInput(name="has solar panels", type="llm",
                       options={"hint": "quality", "boxes": True}),
        answers=[{"bbox": GOOD_BOX}],
    )
    assert model.calls == []


def test_max_attempts_option_bounds_the_pipeline_loop(monkeypatch):
    result, model = _analyze(
        monkeypatch, _criterion(boxes=True, max_attempts=1),
        answers=[{"bbox": FULL_FRAME}, {"bbox": FULL_FRAME}, {"bbox": FULL_FRAME}],
    )
    entry = _entry(result)
    assert entry["options_used"]["max_attempts"] == 1
    assert len(entry["localization"]["attempts"]) == 1
    assert len(model.calls) == 1


def test_max_attempts_past_the_cap_is_refused_at_validation():
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as exc:
        _criterion(boxes=True, max_attempts=llm_boxes.LLM_BBOX_MAX_ATTEMPTS + 1)
    assert "exceeds this server's cap" in str(exc.value)


def test_pipeline_verdict_is_unchanged_by_an_exhausted_loop(monkeypatch):
    """The loop is additive: three rejected boxes, identical score and verdict."""
    without, _ = _analyze(monkeypatch, _criterion(), answers=[])
    with_loop, _ = _analyze(
        monkeypatch,
        _criterion(boxes=True),
        answers=[
            {"bbox": [100, 200, 300, 400]}, {"score": 3, "reason": "tiles"},
            {"bbox": [400, 200, 600, 400]}, {"score": 2, "reason": "a vent"},
            {"bbox": [100, 600, 300, 800]}, {"score": 1, "reason": "grass"},
        ],
    )

    assert with_loop["verdict"] == without["verdict"]
    assert with_loop["assessment"]["overall_score"] == without["assessment"]["overall_score"]
    entry, base = _entry(with_loop), _entry(without)
    assert entry["score"] == base["score"] and entry["verdict"] == base["verdict"]

    # …and every attempt is there, all rejected.
    assert entry["localization"]["accepted_attempt"] is None
    assert len(entry["localization"]["attempts"]) == 3
    assert entry["regions"] and all(r["attrs"]["accepted"] is False for r in entry["regions"])


def test_score_false_locates_without_judging(monkeypatch):
    """/locate's job: the presence call gates the loop, then is thrown away."""
    scoring = []
    result, model = _analyze(
        monkeypatch,
        CriterionInput(name="has solar panels", type="llm", score=False,
                       options={"hint": "presence", "boxes": True}),
        answers=[{"bbox": GOOD_BOX}, {"score": 9, "reason": "panels"}],
        scoring_calls=scoring,
    )
    entry = _entry(result)
    assert scoring == ["score/has solar panels"]  # the gate still ran
    assert entry["score"] is None and entry["verdict"] is None and entry["confidence"] is None
    assert entry["scored"] is False
    assert entry["localization"]["accepted_attempt"] == 1
    assert entry["regions"][0]["source"] == "llm"
    # Excluded from the weighting, so there is nothing to weigh at all.
    assert result["assessment"]["overall_score"] is None
    assert result["assessment"]["complete"] is True
    assert result["assessment"]["weighted_score_breakdown"] is None


def test_pipeline_writes_localization_and_attempt_urls(monkeypatch):
    """regions.json carries the record; the result carries per-attempt URLs."""
    from common.vision import ArtifactStore
    from config import ARTIFACT_DIR
    from regions.collect import visible_regions

    result, _ = _analyze(
        monkeypatch,
        _criterion(boxes=True),
        answers=[
            {"bbox": FULL_FRAME},
            {"bbox": [100, 200, 300, 400]},
            {"score": 9, "reason": "panels"},
        ],
        job_id="testjob1",
    )
    entry = _entry(result)
    slug = entry["artifacts"]["slug"]
    attempts = entry["artifacts"]["attempts"]
    assert [a["attempt"] for a in attempts] == [1, 2]
    assert attempts[0]["svg"] == f"/jobs/testjob1/artifacts/p0.svg?criterion={slug}&attempt=1"
    assert attempts[0]["accepted"] is False and attempts[1]["accepted"] is True
    assert entry["artifacts"]["layers"]["svg"] == f"/jobs/testjob1/artifacts/p0.svg?criterion={slug}"

    store = ArtifactStore(ARTIFACT_DIR)
    stored = store.read_json("testjob1", "regions.json")
    criterion = stored["criteria"]["has solar panels"]
    assert criterion["localization"]["accepted_attempt"] == 2
    assert criterion["sources"] == ["llm"]
    assert len(criterion["regions"]) == 2
    # Layers are NOT written at job time any more — only on first fetch.
    assert store.open("testjob1", "p0.svg") is None
    assert store.open("testjob1", "p0.base.jpg") is not None
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
