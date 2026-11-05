"""The box loop's opt-in aids, driven by a scripted fake model.

Four settings, each OFF by default and each byte-identical to the loop
without it when off:

  * ``CLASSIFIER_LLM_BBOX_REFINE_MODE=marks`` — the refine crop shows the OCR
    text lines in its window, outlined and numbered, and the model picks
    mark(s); the box is the union of those lines. Pinned: the coordinate
    path (grid window → original pixels → the fitted crop, and back), the
    reading order, the drawing landing on the real pixels, the union, the
    answer parsing, every fallback to the grid refine and what each records;
  * ``CLASSIFIER_REFERENCE_BOX_TEXT`` — a reference example's region text,
    OCR'd once per job (``analysis.references.region_text`` / ``text_of``),
    on its ``BoxExample`` and record; never failing the unit;
  * ``CLASSIFIER_LLM_BBOX_VERIFY_PARTIAL`` — the verify's ``partial`` field
    and the "cut off" retry wording, never moving the score;
  * ``CLASSIFIER_LLM_BBOX_VERIFY_MIN_SPAN`` — the verify crop grown, centred,
    inside the page; the box itself unchanged;

and that ``analysis.llm_eval`` hands the loop the criterion's own text layer.
The prompts themselves are pinned in ``test_reference_prompts.py``. Each
setting is also a per-criterion option (``options.refine``,
``options.reference.text``, ``options.verify_partial``,
``options.verify_min_span``) whose service default is the env var named
above; the options, their precedence and their plumbing are pinned in
``test_box_loop_options.py`` — here the loop takes them as keywords.

Run with::

    UV_LINK_MODE=copy uv run --no-sync --with pytest --package classifier \\
        python -m pytest unit-tests/classifier/test_llm_box_marks.py -q -p no:cacheprovider
"""

from __future__ import annotations

import asyncio
import base64

import cv2
import numpy as np
import pytest

from common.vision import MARK_COLOURS, PageGeometry, pixels_to_grid

from llm import boxes as llm_boxes
from llm import client as llm_client
from llm.prompts import BoxExample
from test_llm_boxes import GEOMETRY, GOOD_BOX, ScriptedModel

NAME = "has solar panels"


def _poly(x1, y1, x2, y2):
    """An OCR line's box as RapidOCR gives it: four corner points."""
    return [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]


# GOOD_BOX [100, 200, 300, 400] on the 800×1200 page → the refine window
# [0, 50, 450, 550] grid → x 0-360 px, y 60-660 px. B and C share a row (C's
# top is a pixel lower); D is outside; E is only 30% inside.
LINE_A = [40, 100, 300, 130]
LINE_B = [40, 140, 200, 170]
LINE_C = [210, 141, 330, 171]
LINE_D = [500, 900, 700, 930]
LINE_E = [300, 300, 500, 330]
LINES = [
    {"text": t, "confidence": 0.9, "box": _poly(*b)}
    for t, b in (("C", LINE_C), ("D", LINE_D), ("A", LINE_A), ("E", LINE_E), ("B", LINE_B))
]
WINDOW = [0.0, 50.0, 450.0, 550.0]
RECT = (0, 60, 360, 660)


@pytest.fixture
def page():
    return np.full((GEOMETRY.height, GEOMETRY.width, 3), 200, dtype=np.uint8)


def _run(model, monkeypatch, page, **kwargs):
    """The loop with the refine pass ON (it is the subject here) and no grid
    on the ask; marks mode and the verify settings only when a test asks."""
    monkeypatch.setattr(llm_client, "call_model_json", model)
    kwargs.setdefault("gridlines", False)
    kwargs.setdefault("refine", True)
    return asyncio.run(
        llm_boxes.locate_criterion(
            NAME, image_b64="ZmFrZQ==", original_image=page, geometry=GEOMETRY, **kwargs,
        )
    )


def _marks_run(model, monkeypatch, page, **kwargs):
    kwargs.setdefault("refine_mode", "marks")
    kwargs.setdefault("text_lines", LINES)
    return _run(model, monkeypatch, page, **kwargs)


def _decode(b64: str) -> np.ndarray:
    return cv2.imdecode(np.frombuffer(base64.b64decode(b64), np.uint8), cv2.IMREAD_COLOR)


# ---------------------------------------------------------------------------
# The coordinate path
# ---------------------------------------------------------------------------


def test_window_rect_is_the_rect_crop_window_cuts(page):
    assert llm_boxes.window_rect(WINDOW, page.shape) == RECT
    crop = llm_boxes.crop_window(page, WINDOW)
    assert crop.shape[:2] == (RECT[3] - RECT[1], RECT[2] - RECT[0])
    # Each axis by its own dimension; floor near, ceil far; clamped; never empty.
    assert llm_boxes.window_rect([100.4, 0, 100.6, 1000], (1200, 800)) == (80, 0, 81, 1200)
    assert llm_boxes.window_rect([0, 0, 1000, 1000], (1200, 800)) == (0, 0, 800, 1200)
    assert llm_boxes.window_rect([999.9, 999.9, 1000, 1000], (1200, 800)) == (799, 1199, 800, 1200)


def test_line_bounds_reads_a_polygon_a_flat_box_and_nothing_else():
    assert llm_boxes.line_bounds({"box": _poly(10, 20, 30, 40)}) == [10, 20, 30, 40]
    # A rotated quadrilateral: its bounds.
    assert llm_boxes.line_bounds({"box": [[12, 20], [30, 22], [28, 40], [10, 38]]}) == [
        10, 20, 30, 40]
    assert llm_boxes.line_bounds({"box": [10, 20, 30, 40]}) == [10, 20, 30, 40]
    for bad in ({"box": None}, {}, None, {"box": "x"}, {"box": _poly(10, 20, 10, 40)},
                {"box": [[1, "a"], [2, 3]]}):
        assert llm_boxes.line_bounds(bad) is None


def test_lines_in_window_keeps_mostly_inside_lines_in_reading_order():
    bounds = [llm_boxes.line_bounds(line) for line in LINES]
    inside = llm_boxes.lines_in_window(bounds, RECT)
    # A (its row), then B before C — C's top is a pixel lower, but they are
    # one row and B is left; D outside, E only 30% inside.
    assert inside == [LINE_A, LINE_B, LINE_C]
    # The share is a parameter: at 0.3 E is in, on its own row.
    assert llm_boxes.lines_in_window(bounds, RECT, min_inside=0.3) == [
        LINE_A, LINE_B, LINE_C, LINE_E]
    assert llm_boxes.lines_in_window([], RECT) == []


def test_to_crop_px_shifts_then_scales_each_axis():
    rect = (1000, 500, 3000, 1500)                      # 2000 × 1000 px
    fitted = (500, 1000, 3)                             # fitted to 1000 wide
    assert llm_boxes.to_crop_px([[1100, 600, 1500, 700]], rect, fitted) == [
        [50.0, 50.0, 250.0, 100.0]]
    # And back: crop px × (rect size / crop size) + origin.
    (x1, y1, x2, y2), = llm_boxes.to_crop_px([[1100, 600, 1500, 700]], rect, fitted)
    sx, sy = 2000 / 1000, 1000 / 500
    assert [x1 * sx + 1000, y1 * sy + 500, x2 * sx + 1000, y2 * sy + 500] == [
        1100, 600, 1500, 700]


def test_the_marks_land_on_the_real_pixels_of_a_shrunk_crop():
    """The full path on a 4000×3000 page whose window is shrunk to the
    working size: dark bars at known ORIGINAL pixels, mapped and drawn — each
    outline hugs its bar in the crop the model is shown."""
    big = np.full((3000, 4000, 3), 255, dtype=np.uint8)
    bars = [[400, 500, 1900, 620], [2100, 505, 3500, 640], [600, 1300, 2600, 1420]]
    for x1, y1, x2, y2 in bars:
        big[y1:y2, x1:x2] = 0
    window = [0.0, 0.0, 1000.0, 600.0]
    rect = llm_boxes.window_rect(window, big.shape)
    crop = llm_boxes.crop_window(big, window)
    assert max(crop.shape[:2]) == 1000                  # it WAS shrunk
    inside = llm_boxes.lines_in_window(bars, rect)
    mapped = llm_boxes.to_crop_px(inside, rect, crop.shape)
    for (x1, y1, x2, y2), bar in zip(mapped, inside):
        ys, xs = np.nonzero(crop[:, :, 0] < 128)
        mine = (xs >= x1 - 2) & (xs <= x2 + 2) & (ys >= y1 - 2) & (ys <= y2 + 2)
        assert abs(xs[mine].min() - x1) <= 1.5 and abs(xs[mine].max() - x2) <= 1.5
        assert abs(ys[mine].min() - y1) <= 1.5 and abs(ys[mine].max() - y2) <= 1.5
    marked = llm_boxes._with_marks(crop, mapped)
    assert marked.shape == crop.shape
    # The first outline's top edge, in BGR, at its mapped row.
    x1, y1, x2, _ = mapped[0]
    b, g, r = marked[int(round(y1)), int((x1 + x2) / 2)]
    assert (r, g, b) == MARK_COLOURS[0]


def test_union_box():
    assert llm_boxes.union_box([LINE_B, LINE_C]) == [40, 140, 330, 171]
    assert llm_boxes.union_box([LINE_A]) == LINE_A


@pytest.mark.parametrize(
    "answer, chosen, why",
    [
        ({"marks": [3, 1, 3]}, [1, 3], None),
        ({"marks": 2}, [2], None),
        ({"marks": ["2", 3.0]}, [2, 3], None),
        ({"marks": None}, None, "marks: null"),
        ({"marks": []}, None, "marks: null"),
        ({"marks": [0]}, None, "mark 0 is not one of the 3 marks shown"),
        ({"marks": [4]}, None, "mark 4 is not one of the 3 marks shown"),
        ({"marks": [1.5]}, None, "not a list of mark numbers"),
        ({"marks": [True]}, None, "not a list of mark numbers"),
        ({"marks": "line two"}, None, "not a list of mark numbers"),
        ({"reason": "x"}, None, "no 'marks' key"),
        (None, None, "usable answer for the marks pass"),
    ],
)
def test_parse_marks(answer, chosen, why):
    got, reason = llm_boxes.parse_marks(answer, 3)
    assert got == chosen
    assert (reason is None) if why is None else (why in reason)


# ---------------------------------------------------------------------------
# The marks refine in the loop
# ---------------------------------------------------------------------------


def test_marks_refine_unions_the_chosen_lines(monkeypatch, page):
    model = ScriptedModel([
        {"bbox": GOOD_BOX},
        {"reason": "lines two and three", "marks": [2, 3]},
        {"score": 9, "reason": "panels"},
    ])
    regions, loc = _marks_run(model, monkeypatch, page)
    assert model.labels() == [
        f"bbox/{NAME}#1", f"refine/marks/{NAME}#1", f"verify/{NAME}#1"]
    assert loc.calls == 3 and loc.accepted_attempt == 1
    attempt = loc.attempts[0]
    assert attempt.refined is True and attempt.marks == [2, 3]
    # B ∪ C in original pixels, and the same box on the page grid.
    assert attempt.bbox_px == [40.0, 140.0, 330.0, 171.0]
    assert attempt.bbox_grid == [round(v, 1) for v in pixels_to_grid(
        [(40, 140), (330, 171)], GEOMETRY)] == [50.0, 116.7, 412.5, 142.5]
    assert regions[0].points == [(40.0, 140.0), (330.0, 171.0)]
    data = attempt.as_dict()
    assert data["refine_mode"] == "marks" and data["marks"] == [2, 3]
    assert data["marks_shown"] == 3 and "marks_fallback" not in data
    assert data["coarse_bbox_grid"] == [100.0, 200.0, 300.0, 400.0]
    assert data["refine_window_grid"] == WINDOW
    # The marks call shows ONE image, the marked crop at the window's size,
    # and says how many marks it holds.
    (image,) = model.images(1)
    assert _decode(image).shape[:2] == (RECT[3] - RECT[1], RECT[2] - RECT[0])
    assert "There are 3 marks, numbered 1 to 3." in model.last_text(1)


def test_the_marks_prompt_carries_the_earlier_rejections(monkeypatch, page):
    model = ScriptedModel([
        {"bbox": [0, 0, 1000, 1000]},                       # attempt 1 rejected
        {"bbox": GOOD_BOX},
        {"marks": [1]},
        {"score": 9, "reason": "panels"},
    ])
    _, loc = _marks_run(model, monkeypatch, page)
    text = model.last_text(2)
    assert "Earlier answers for this feature were rejected" in text
    assert "covered 100% of the image" in text
    assert loc.attempts[1].marks == [1]


def test_marks_with_examples_show_them_first(monkeypatch, page):
    crops = [BoxExample(NAME, "PASS", "Q1JPUA==", crop=True)]
    model = ScriptedModel([{"bbox": GOOD_BOX}, {"marks": [1]}, {"score": 9}])
    _, loc = _marks_run(model, monkeypatch, page, crop_examples=crops, guide_refine=True)
    assert model.images(1)[0] == "Q1JPUA==" and len(model.images(1)) == 2
    assert model.last_text(1).startswith("Find this feature in the zoomed CANDIDATE image")
    assert loc.guided_calls == [{"kind": "box_refine", "attempt": 1, "images": 2, "ok": True}]


@pytest.mark.parametrize(
    "kwargs, answers, why",
    [
        ({"text_lines": None}, [], "no text lines were given"),
        ({"text_lines": []}, [], "has no line boxes"),
        ({"text_lines": [{"text": "x", "box": None}]}, [], "has no line boxes"),
        ({"text_lines": [LINES[1]]}, [], "no OCR text line lies inside the refine window"),
        ({"marks_max": 2}, [], "3 OCR text lines lie inside the refine window, over the cap of 2"),
        ({}, [None], "usable answer for the marks pass"),
        ({}, [{"reason": "a picture", "marks": None}], "marks: null"),
        ({}, [{"marks": [7]}], "mark 7 is not one of the 3 marks shown"),
    ],
)
def test_every_fallback_runs_the_grid_refine_and_says_why(
    monkeypatch, page, kwargs, answers, why
):
    model = ScriptedModel([
        {"bbox": GOOD_BOX},
        *answers,                                           # the marks call, if any
        {"bbox": [400, 400, 600, 600]},                     # the grid refine
        {"score": 9, "reason": "panels"},
    ])
    _, loc = _marks_run(model, monkeypatch, page, **kwargs)
    marks_called = bool(answers)
    assert model.labels() == (
        [f"bbox/{NAME}#1"]
        + ([f"refine/marks/{NAME}#1"] if marks_called else [])
        + [f"refine/{NAME}#1", f"verify/{NAME}#1"]
    )
    assert loc.calls == 3 + marks_called
    attempt = loc.attempts[0]
    assert attempt.refined is True and attempt.marks is None
    assert attempt.bbox_grid == pytest.approx([180.0, 250.0, 270.0, 350.0])  # the grid's box
    data = attempt.as_dict()
    assert data["refine_mode"] == "grid" and why in data["marks_fallback"]
    assert "marks" not in data
    assert ("marks_shown" in data) is marks_called
    # The grid prompt is today's refine prompt, untouched by the attempt.
    assert "zoomed-in crop" in model.last_text(len(answers) + 1)


def test_a_failed_grid_fallback_keeps_the_coarse_box(monkeypatch, page):
    model = ScriptedModel([{"bbox": GOOD_BOX}, {"marks": None}, None, {"score": 8}])
    _, loc = _marks_run(model, monkeypatch, page)
    attempt = loc.attempts[0]
    assert attempt.refined is False and attempt.bbox_grid == [100.0, 200.0, 300.0, 400.0]
    assert "marks: null" in attempt.marks_fallback
    assert "usable answer for the refine pass" in attempt.refine_reject


def test_no_page_image_means_no_refine_of_either_kind(monkeypatch):
    working = np.full((600, 400, 3), 200, dtype=np.uint8)
    model = ScriptedModel([{"bbox": GOOD_BOX}])
    _, loc = _marks_run(model, monkeypatch, None, working_image=working)
    data = loc.attempts[0].as_dict()
    assert "not available to crop for the refine pass" in data["refine_reject"]
    assert "refine_mode" not in data and loc.calls == 1


def test_refine_off_turns_marks_off_too(monkeypatch, page):
    model = ScriptedModel([{"bbox": GOOD_BOX}, {"score": 9}])
    _, loc = _marks_run(model, monkeypatch, page, refine=False)
    assert model.labels() == [f"bbox/{NAME}#1", f"verify/{NAME}#1"]
    assert "refine_mode" not in loc.attempts[0].as_dict()


def test_the_mode_defaults_to_the_setting_and_reads_anything_else_as_grid(monkeypatch, page):
    monkeypatch.setattr(llm_boxes, "LLM_BBOX_REFINE_MODE", "marks")
    model = ScriptedModel([{"bbox": GOOD_BOX}, {"marks": [1]}, {"score": 9}])
    _, loc = _run(model, monkeypatch, page, text_lines=LINES)
    assert loc.attempts[0].marks == [1]
    model = ScriptedModel([{"bbox": GOOD_BOX}, {"bbox": [400, 400, 600, 600]}, {"score": 9}])
    _, loc = _run(model, monkeypatch, page, text_lines=LINES, refine_mode="Grid-ish")
    assert model.labels()[1] == f"refine/{NAME}#1"
    assert "refine_mode" not in loc.attempts[0].as_dict()


# ---------------------------------------------------------------------------
# The verify's "cut off" field
# ---------------------------------------------------------------------------


def test_a_rejected_partial_crop_asks_for_the_same_place_larger(monkeypatch, page):
    model = ScriptedModel([
        {"bbox": GOOD_BOX},
        {"score": 4, "reason": "only the digits", "partial": True},
        {"bbox": [100, 180, 320, 420]},
        {"score": 9, "reason": "label and amount", "partial": False},
    ])
    _, loc = _run(model, monkeypatch, page, refine=False, verify_partial=True)
    first, second = loc.attempts
    assert first.verify_partial is True and second.verify_partial is False
    assert first.reject == (
        f"the crop of your previous box [100, 200, 300, 400] showed {NAME} but cut part "
        "of it off (verify score 4); box the same place again, larger, so that all of it "
        "is inside — its label and every digit")
    assert "look elsewhere" not in model.last_text(2)
    assert first.reject in model.last_text(2)                 # the retry's feedback
    assert second.accepted and loc.accepted_attempt == 2
    assert first.as_dict()["verify_partial"] is True
    # The verify prompt asked for it.
    assert '"partial": false' in model.last_text(1)


def test_partial_never_moves_the_score(monkeypatch, page):
    """An accepted crop stays accepted, partial or not; a rejected one that is
    not partial gets today's sentence."""
    model = ScriptedModel([{"bbox": GOOD_BOX}, {"score": 8, "partial": True}])
    _, loc = _run(model, monkeypatch, page, refine=False, verify_partial=True)
    assert loc.accepted_attempt == 1 and loc.attempts[0].verify_partial is True
    model = ScriptedModel([{"bbox": GOOD_BOX}, {"score": 2, "partial": "false"}])
    _, loc = _run(model, monkeypatch, page, refine=False, verify_partial=True, max_attempts=1)
    assert loc.attempts[0].verify_partial is False
    assert loc.attempts[0].reject.endswith("look elsewhere")


@pytest.mark.parametrize(
    "value, read",
    [(True, True), (False, False), (1, True), (0, False), ("TRUE", True), ("no", False),
     ("maybe", None), (None, None), (2, None)],
)
def test_partial_of(value, read):
    assert llm_boxes._partial_of({"score": 3, "partial": value}) is read
    assert llm_boxes._partial_of(None) is None


def test_partial_off_ignores_the_field(monkeypatch, page):
    model = ScriptedModel([{"bbox": GOOD_BOX}, {"score": 2, "partial": True}])
    _, loc = _run(model, monkeypatch, page, refine=False, verify_partial=False, max_attempts=1)
    assert loc.attempts[0].verify_partial is None
    assert "verify_partial" not in loc.attempts[0].as_dict()
    assert loc.attempts[0].reject.endswith("look elsewhere")
    assert "partial" not in model.last_text(1)


# ---------------------------------------------------------------------------
# The verify crop's minimum span
# ---------------------------------------------------------------------------


def test_min_span_grows_the_crop_centred():
    shape = (1200, 800)
    # A 100×20 line in the middle; 25% pad → 150×30; 10% of 1200 = 120 tall,
    # 10% of 800 = 80 wide (already wider).
    assert llm_boxes.crop_rect(shape, [350, 590, 450, 610], 0.25, min_span=0.1) == (
        325, 540, 475, 660)


def test_min_span_slides_inside_the_page_at_an_edge():
    shape = (1200, 800)
    assert llm_boxes.crop_rect(shape, [10, 2, 60, 12], 0.0, min_span=0.1) == (0, 0, 80, 120)
    assert llm_boxes.crop_rect(shape, [740, 1190, 790, 1199], 0.0, min_span=0.1) == (
        720, 1080, 800, 1200)
    # Over the page: the whole axis.
    assert llm_boxes.crop_rect(shape, [100, 100, 200, 120], 0.0, min_span=1.0) == (
        0, 0, 800, 1200)


def test_min_span_zero_is_todays_crop():
    shape = (1200, 800)
    for box in ([350, 590, 450, 610], [0, 0, 100, 100], [790.5, 1190.2, 805, 1210]):
        assert llm_boxes.crop_rect(shape, box, 0.25) == llm_boxes.crop_rect(
            shape, box, 0.25, min_span=0.0)
    # And crop_for cuts exactly that rect (the existing padding test pins it).
    page = np.zeros((1200, 800, 3), dtype=np.uint8)
    assert llm_boxes.crop_for(page, [400.0, 600.0, 500.0, 700.0], pad=0.10).shape[:2] == (120, 120)


def test_the_verify_sees_the_grown_crop_and_the_box_is_unchanged(monkeypatch, page):
    # A one-line box: 40% × 1% of the page, just over the area floor.
    model = ScriptedModel([{"bbox": [300, 495, 700, 505]}, {"score": 9}])
    _, loc = _run(model, monkeypatch, page, refine=False, verify_min_span=0.2)
    attempt = loc.attempts[0]
    assert attempt.bbox_px == pytest.approx([240.0, 594.0, 560.0, 606.0])
    crop = _decode(model.images(1)[0])
    assert crop.shape[0] == 240                              # 20% of 1200
    assert crop.shape[1] == 480                              # 320 + 25% each side ≥ 160
    assert "verify_min_span" not in attempt.as_dict()        # nothing new recorded


# ---------------------------------------------------------------------------
# Off is today
# ---------------------------------------------------------------------------


def _today(monkeypatch, page, **kwargs):
    model = ScriptedModel([
        {"bbox": [0, 0, 1000, 1000]},
        {"bbox": GOOD_BOX},
        {"bbox": [400, 400, 600, 600]},
        {"score": 3, "reason": "tiles", "partial": True},
        {"bbox": GOOD_BOX},
        {"bbox": [400, 400, 600, 600]},
        {"score": 9, "reason": "panels"},
    ])
    _, loc = _run(model, monkeypatch, page, **kwargs)
    return model, loc


def test_every_setting_off_is_byte_identical_to_the_loop_without_them(monkeypatch, page):
    plain_model, plain = _today(monkeypatch, page)
    off_model, off = _today(
        monkeypatch, page, text_lines=LINES, refine_mode="grid", marks_max=1,
        verify_partial=False, verify_min_span=0.0,
    )
    assert off_model.calls == plain_model.calls              # every prompt, in order
    assert off.as_dict() == plain.as_dict()
    keys = {k for a in off.as_dict()["attempts"] for k in a}
    assert not keys & {"refine_mode", "marks", "marks_shown", "marks_fallback",
                       "verify_partial"}


def test_the_settings_default_off(monkeypatch, page):
    assert llm_boxes.LLM_BBOX_REFINE_MODE == "grid"
    assert llm_boxes.LLM_BBOX_VERIFY_PARTIAL is False
    assert llm_boxes.LLM_BBOX_VERIFY_MIN_SPAN == 0.0
    from analysis import references

    assert references.REFERENCE_BOX_TEXT is False
    plain_model, plain = _today(monkeypatch, page)
    defaults_model, defaults = _today(monkeypatch, page, text_lines=LINES)
    assert defaults_model.calls == plain_model.calls and defaults.as_dict() == plain.as_dict()


# ---------------------------------------------------------------------------
# An example's region text
# ---------------------------------------------------------------------------


class FakeEngine:
    """An OCR engine answering from a list of ``(text, [x1, y1, x2, y2])`` in
    the CROP's pixels, recording the crop it was given."""

    def __init__(self, lines):
        self.lines = lines
        self.seen = []

    def recognize(self, image):
        from common.documents import OCRResult

        self.seen.append(image.shape[:2])
        return OCRResult(
            text="\n".join(t for t, _ in self.lines), confidence=0.9,
            lines=[{"text": t, "confidence": 0.9, "box": None if b is None else _poly(*b)}
                   for t, b in self.lines],
        )


def test_region_text_keeps_the_lines_centred_inside_the_region():
    from analysis.references import region_text

    page = np.zeros((1000, 1000, 3), dtype=np.uint8)
    # Region 200-400 × 300-340; padded 10% → crop origin (180, 296), 240 × 48,
    # the region at (20, 4)-(220, 44) in the crop (no minimum span here).
    engine = FakeEngine([
        ("Meter #:", (25, 8, 110, 40)),          # centre inside the region
        ('4471 "0923"', (120, 8, 215, 40)),      # inside; quotes made single
        ("neighbour", (0, 44, 240, 48)),         # in the padding only
    ])
    assert region_text(page, [200, 300, 400, 340], engine, min_span=0.0) == (
        "Meter #: 4471 '0923'")
    assert engine.seen == [(48, 240)]


def test_region_text_reads_with_context_around_a_thin_line():
    """The default grows the OCR'd crop to REFERENCE_BOX_TEXT_MIN_SPAN of the
    page (the detector misses a lone thin strip) — and the centre rule still
    keeps only the region's own line."""
    from analysis.references import region_text
    from config import REFERENCE_BOX_TEXT_MIN_SPAN

    page = np.zeros((2000, 1000, 3), dtype=np.uint8)
    tall = round(REFERENCE_BOX_TEXT_MIN_SPAN * 2000)
    # Region 200-400 × 980-1020 (40 tall, padded to 48); grown to `tall`
    # rows centred on y 1000, so the region sits at rows (tall/2 - 20, +20).
    top = tall / 2 - 20
    engine = FakeEngine([
        ("above", (20, top - 30, 200, top - 5)),
        ("Account #078-360-268-2-8", (25, top + 5, 215, top + 35)),
        ("below", (20, top + 45, 200, top + 70)),
    ])
    assert region_text(page, [200, 980, 400, 1020], engine) == "Account #078-360-268-2-8"
    assert engine.seen == [(tall, 240)]


def test_region_text_falls_back_to_the_most_overlapping_line_and_cuts_long_text():
    from analysis.references import region_text

    page = np.zeros((1000, 1000, 3), dtype=np.uint8)
    engine = FakeEngine([("Amount due on or before 10/15 " + "x" * 120, (0, 0, 240, 48))])
    # The line's centre (119.5, 23.5) is outside a region that is its right end.
    text = region_text(page, [300, 300, 400, 340], FakeEngine([
        ("left", (0, 0, 10, 48)), ("Total $193.33", (0, 0, 239, 47))]), min_span=0.0)
    assert text == "Total $193.33"
    long = region_text(page, [180, 296, 420, 344], engine, pad=0.0, min_span=0.0)
    assert len(long) == 80 and long.endswith("…")
    # A text-less region (a logo) beside text: a neighbour clipping its
    # corner is not its line — the bill-2 logo once read "Non-Paymen".
    assert region_text(page, [300, 300, 400, 340], FakeEngine([
        ("Non-Payment notice", (80, 30, 300, 60))]), pad=0.0, min_span=0.0) is None
    assert region_text(page, [0, 0, 10, 10], FakeEngine([])) is None
    assert region_text(page, [0, 0, 10, 10], None) is None
    # Lines without boxes are all kept.
    assert region_text(page, [0, 0, 10, 10], FakeEngine([("a", None), ("b", None)])) == "a b"


def _example(rid):
    return {
        "reference_id": rid, "criterion": "the meter number",
        "expected": {"verdict": "PASS", "score": 9}, "polarity": "pass",
        "region_source": "caller",
        "regions": [{"page": 0, "kind": "box", "points": [[200, 300], [400, 340]]}],
        "composite": "c.the-meter-number-0a1b.jpg",
        "geometry": {"page": 0, "width": 1000, "height": 1000, "working_scale": 1.0},
        "hint": "presence",
    }


def test_text_of_reads_page_jpg_once_and_never_raises(monkeypatch):
    from analysis import references
    from references.store import reference_files

    rid = "r00000000beef"
    page = np.full((1000, 1000, 3), 255, dtype=np.uint8)
    reference_files.write(rid, "page.jpg", cv2.imencode(".jpg", page)[1].tobytes())
    engine = FakeEngine([("Meter #: 4471", (25, 8, 215, 40))])
    monkeypatch.setattr(references, "get_ocr_engine", lambda: engine)
    try:
        refs = references.JobReferences(plan={})
        assert asyncio.run(refs.text_of(_example(rid))) == "Meter #: 4471"
        assert asyncio.run(refs.text_of(_example(rid))) == "Meter #: 4471"
        assert len(engine.seen) == 1                                    # cached
        # No engine, no page, an engine that raises: None, never an error.
        monkeypatch.setattr(references, "get_ocr_engine", lambda: None)
        assert asyncio.run(references.JobReferences(plan={}).text_of(_example(rid))) is None

        class Broken:
            def recognize(self, image):
                raise RuntimeError("onnx fell over")

        monkeypatch.setattr(references, "get_ocr_engine", lambda: Broken())
        assert asyncio.run(references.JobReferences(plan={}).text_of(_example(rid))) is None
        monkeypatch.setattr(references, "get_ocr_engine", lambda: engine)
        assert asyncio.run(references.JobReferences(plan={}).text_of(
            _example("r00000000dead"))) is None
    finally:
        reference_files.delete(rid)


def test_box_examples_carry_the_text_only_when_asked(monkeypatch):
    from types import SimpleNamespace

    from analysis import references

    refs = references.JobReferences(plan={})

    async def image_b64(example):
        return "COMP"

    async def crop_b64(example):
        return "CROP"

    async def text_of(example):
        return "Meter #: 4471"

    monkeypatch.setattr(refs, "image_b64", image_b64)
    monkeypatch.setattr(refs, "crop_b64", crop_b64)
    monkeypatch.setattr(refs, "text_of", text_of)
    guide = references.CriterionGuide("the meter number", "explicit", {}, [_example("r1")])
    spec = SimpleNamespace(max_images_per_prompt=3)

    off = asyncio.run(refs.box_examples(guide, spec, position_check=False))
    assert [e.text for e in off.ask + off.crops] == [None, None]
    assert "text" not in off.records[0]

    on = asyncio.run(refs.box_examples(guide, spec, position_check=False, with_text=True))
    assert [e.text for e in on.ask + on.crops] == ["Meter #: 4471"] * 2
    assert on.records[0]["text"] == "Meter #: 4471"
    assert on.ask[0].caption().endswith(' The outlined area reads: "Meter #: 4471".')
    assert on.crops[0].caption().endswith(' The text at its centre reads: "Meter #: 4471".')

    monkeypatch.setattr(references, "REFERENCE_BOX_TEXT", True)          # the default
    assert asyncio.run(refs.box_examples(guide, spec, position_check=False)).records[0][
        "text"] == "Meter #: 4471"


# ---------------------------------------------------------------------------
# The pipeline hands the loop the criterion's own text layer
# ---------------------------------------------------------------------------


def _analyze_marks(monkeypatch, criterion, *, answers):
    """The whole pipeline on ``test_llm_boxes``' synthetic 400×600 PNG, the
    refine pass ON in marks mode, the model scripted. Marks is the SERVICE
    default here (``CLASSIFIER_LLM_BBOX_REFINE_MODE``, patched where the
    service resolves it, ``api.criterion_options``); a criterion's own
    ``options.refine`` is test_box_loop_options.py's."""
    import analysis
    from api import criterion_options

    from test_llm_boxes import _png_bytes, _scoring_call

    monkeypatch.setattr(llm_client, "call_model", _scoring_call(10))
    model = ScriptedModel(answers)
    monkeypatch.setattr(llm_client, "call_model_json", model)
    monkeypatch.setattr(llm_boxes, "LLM_BBOX_REFINE", True)
    monkeypatch.setattr(llm_boxes, "LLM_BBOX_GRIDLINES", False)
    monkeypatch.setattr(criterion_options, "LLM_BBOX_REFINE_MODE", "marks")
    doc = analysis.load_document_bytes(_png_bytes(), "page.png", "image/png", keep_source=True)
    result = asyncio.run(analysis.analyze_document(doc, [criterion]))
    return result, model


def test_llm_eval_passes_its_text_layer_to_the_marks_refine(monkeypatch):
    """ocr "always" on a synthetic page with a scripted OCR engine: the lines
    the scoring call's layer holds are the ones numbered — no second pass."""
    from analysis import context

    from test_llm_boxes import _criterion, _entry

    engine = FakeEngine([("Amount due", (40, 100, 200, 130)), ("$193.33", (40, 140, 160, 170))])
    monkeypatch.setattr(context, "get_ocr_engine", lambda: engine)
    # The ask [50, 100, 450, 350] → px 20-180 × 60-210; its window holds both lines.
    result, model = _analyze_marks(
        monkeypatch, _criterion(boxes=True, ocr="always"),
        answers=[{"bbox": [50, 100, 450, 350]}, {"marks": [1, 2]}, {"score": 9}],
    )
    assert len(engine.seen) == 1                                        # one OCR pass
    assert model.labels() == [f"bbox/{NAME}#1", f"refine/marks/{NAME}#1", f"verify/{NAME}#1"]
    attempt = _entry(result)["localization"]["attempts"][0]
    assert attempt["refine_mode"] == "marks" and attempt["marks"] == [1, 2]
    assert attempt["marks_shown"] == 2
    assert attempt["bbox_px"] == [40.0, 100.0, 200.0, 170.0]           # the two lines' union


def test_a_page_without_line_boxes_falls_back_in_the_pipeline(monkeypatch):
    """The test session runs with CLASSIFIER_OCR_ENGINE=none: the layer has no
    lines, so the grid refines and the attempt says why."""
    from test_llm_boxes import _criterion, _entry

    result, model = _analyze_marks(
        monkeypatch, _criterion(boxes=True),
        answers=[{"bbox": [50, 100, 450, 350]}, {"bbox": [400, 400, 600, 600]}, {"score": 9}],
    )
    assert model.labels() == [f"bbox/{NAME}#1", f"refine/{NAME}#1", f"verify/{NAME}#1"]
    attempt = _entry(result)["localization"]["attempts"][0]
    assert attempt["refine_mode"] == "grid"
    assert attempt["marks_fallback"] == (
        "the page's text layer has no line boxes (native text, or OCR found none)")
