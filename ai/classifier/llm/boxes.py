"""The LLM bounding-box enforcement loop — ask, validate, verify, retry.

A vision model asked "where is the solar array?" will answer. It will also
answer when there is no array, when it cannot tell, and when the honest answer
is "somewhere in the top half" — and in every one of those cases the answer is
four numbers that look exactly like a correct one. A box from a model is a
claim, not a measurement, so nothing here trusts one:

    ask      one criterion, the ONE page image the scoring call used — with a
             labelled 0-1000 coordinate grid drawn on it (LLM_BBOX_GRIDLINES,
             common.vision.draw_grid_overlay) so the model reads a position
             off a ruler instead of estimating a fraction of the frame — and
             a box on that grid.                 (llm.prompts.build_bbox_prompt)
    validate four finite numbers, x1 < x2, y1 < y2, inside the grid, area
             between LLM_BBOX_MIN_AREA and LLM_BBOX_MAX_AREA. A full-frame box
             is a refusal dressed as an answer, and it is the single most
             common failure.
    refine   (LLM_BBOX_REFINE) crop the ORIGINAL page around the coarse box —
             LLM_BBOX_REFINE_ZOOM × the box on each axis, at least
             LLM_BBOX_REFINE_MIN_SPAN of the page — draw the grid on the crop,
             ask again, and map the answer back into the page frame. Measured
             on photographed bills, the coarse box put a text line 25-100 grid
             units off on one axis and the refined one lands within a few;
             the coarse box is kept when the second answer is unusable, and
             both are recorded (``coarse_bbox_grid``, ``refined``).
             MARKS MODE (``refine_mode="marks"``, a criterion's
             ``options.refine``): the same crop, but
             instead of the grid every OCR text line inside the window
             (``text_lines``, the criterion's own text layer, ORIGINAL page
             pixels) is outlined and numbered (common.vision
             .draw_marks_overlay), and the model says which mark(s) are the
             feature (llm.prompts.build_marks_prompt); the refined box is
             the union of those lines' boxes, so it fits the text exactly —
             a placement the model cannot get a few pixels wrong. The grid
             refine runs instead when the page has no line boxes, the
             window holds none (a logo) or more than LLM_BBOX_MARKS_MAX, or
             the answer is unusable or ``null``; the attempt records which
             (``refine_mode``, ``marks``, ``marks_shown``,
             ``marks_fallback``).
    verify    crop that box out of the ORIGINAL page (+LLM_BBOX_CROP_PAD on
             each side, grown to at least ``verify_min_span`` of the page
             on each axis when that is set), send the crop ALONE, and
             ask whether the feature is visible in it. Accept at
             LLM_BBOX_VERIFY_PASS or better. With ``verify_partial``
             the answer also says whether the crop cuts the feature off
             (``verify_partial``) — never moving the score.
                                                (llm.prompts.build_verify_prompt)
    cross-check  IoU against the detector's best box for the same criterion,
             when phase 2's detector produced one. Informational: it is
             recorded as ``attrs.detector_iou`` and never accepts or rejects.
    retry     re-ask with the rejection as feedback ("your previous box …
             covered 100% of the image …; try again, smaller"), up to
             LLM_BBOX_MAX_ATTEMPTS. A verify rejection whose crop was
             ``partial`` says the box was on the right thing but cut it off
             (config.VERIFY_PARTIAL_REJECT) instead of "look elsewhere".

**The coordinate frames**, each conversion one named function: the page's
0-1000 GRID (what the ask answers on; ``grid_to_pixels`` / ``pixels_to_grid``
to ORIGINAL page pixels, the frame regions and OCR lines live in); the
refine WINDOW in grid units (``refine_window``) → its rect in original
pixels (``window_rect``, the rect ``crop_window`` cuts) → the CROP's pixels
after it is fitted to the working size (``to_crop_px``); a box on the crop's
own grid back to the page grid (``window_to_full``). The ≤1000-px WORKING
copy only ever appears as the ask image — its 0-1000 grid is the page's.

**Per criterion, not per service.** The opt-in settings — the marks refine
(``refine_mode``), the cut-off verify (``verify_partial``), the verify
crop's floor (``verify_min_span``) and which steps see reference examples
(``guide_ask`` / ``guide_refine`` / ``guide_verify``) — are keyword
arguments. The service passes each criterion's own (its ``options.refine``,
``options.verify_partial``, ``options.verify_min_span``,
``options.reference.boxes``), resolved against the service defaults by
``api.criterion_options`` and handed over by ``analysis.llm_eval``; the
module-level defaults below (LLM_BBOX_REFINE_MODE, LLM_BBOX_VERIFY_PARTIAL,
LLM_BBOX_VERIFY_MIN_SPAN, REFERENCE_BOX_ASK / _REFINE / _VERIFY) are read
here only when a DIRECT caller leaves a keyword out —
``bin/grounding_experiment.py``. LLM_BBOX_MARKS_MAX stays service-wide.

**Reference examples** (ZT-527), when the request lists references that
guide the criterion, ride along in the same calls — never extra calls —
each step behind its own flag (``guide_*``; for a direct caller left out,
its knob):

    ask      REFERENCE_BOX_ASK: up to one PASS-side and one FAIL example
             (``ask_examples``) captioned BEFORE the gridded candidate —
             composites, the reference page with the feature outlined, or
             region crops under the position check — so the model knows WHAT
             to box. Identification is the loop's documented weakness: asked
             for a logo, it boxes the heading that names the company.
    refine   REFERENCE_BOX_REFINE: the examples' region crops
             (``crop_examples``, cut by ``example_crop``) before the zoomed
             candidate crop, so a look-alike in the window is not boxed.
    verify   REFERENCE_BOX_VERIFY: the same crops before the candidate crop —
             "is it in the CANDIDATE crop?", still 1-10, reason first. A
             caller may show it other examples instead (``verify_examples``;
             only ``bin/grounding_experiment.py --verify-images composite``
             does — the service passes none).

The examples are chosen and loaded by ``analysis.references`` once per unit
and are the same on every attempt; validation, the gate, the area checks, the
rejection wording and the labels are untouched. With every step off (or no
examples) every prompt is byte-for-byte the unguided one. The loop never
imports ``analysis``: it returns what carried examples
(``Localization.guided`` / ``guided_calls``) and ``analysis.llm_eval``
records it.

**Every attempt comes back**, accepted or not. A rejected box is evidence
about the model, and ``?criterion=<slug>&attempt=2`` on the artifact file
endpoint renders it on its own so it can be looked at. The accepted one is
additionally the criterion's ``source="llm"`` region.

**The loop never changes a score or a verdict.** It runs AFTER the scoring
call, reads that call's score to decide whether there is anything to locate at
all (below LLM_BBOX_PRESENCE_MIN the model just said the feature is absent, so
there is not), and only ever adds keys. A criterion whose every attempt was
rejected keeps its score and comes back with ``accepted_attempt: null``.

Cost: one ask per attempt, plus one refine and one verify per attempt whose
box validated — so at most ``3 × max_attempts`` small calls per located
criterion, on top of that criterion's own scoring call (examples add images
to those calls, never calls; in marks mode a marks answer that falls back to
the grid adds the grid refine, so at most ``4 × max_attempts``). That is why the loop
is opt-in per criterion (``options.boxes``), capped per criterion
(``options.max_attempts``, never above LLM_BBOX_MAX_ATTEMPTS) and gated on the
presence score. Every call goes to the criterion's model (``model``, the
same registry entry its scoring call used — a vision model: the submit
refuses boxes on a text-only one) through ``llm.client``, so that model's
``max_concurrent`` limit applies to each one.

Process flow position: called by the ``llm`` evaluator
(``analysis.llm_eval``) right after that criterion's scoring call.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

from common.vision import (
    PageGeometry,
    Region,
    draw_grid_overlay,
    draw_marks_overlay,
    grid_to_pixels,
    iou,
    pixels_to_grid,
)

from config import (
    LLM_BBOX_CROP_PAD,
    LLM_BBOX_GRID,
    LLM_BBOX_GRID_STEP,
    LLM_BBOX_GRIDLINES,
    LLM_BBOX_MARKS_MAX,
    LLM_BBOX_MARKS_MIN_INSIDE,
    LLM_BBOX_MAX_AREA,
    LLM_BBOX_MAX_ATTEMPTS,
    LLM_BBOX_MIN_AREA,
    LLM_BBOX_PRESENCE_MIN,
    LLM_BBOX_REFINE,
    LLM_BBOX_REFINE_MIN_SPAN,
    LLM_BBOX_REFINE_MODE,
    LLM_BBOX_REFINE_ZOOM,
    LLM_BBOX_VERIFY_MIN_SPAN,
    LLM_BBOX_VERIFY_PARTIAL,
    LLM_BBOX_VERIFY_PASS,
    MAX_WORKING_DIMENSION,
    REFERENCE_BOX_ASK,
    REFERENCE_BOX_REFINE,
    REFERENCE_BOX_VERIFY,
    VERIFY_PARTIAL_REJECT,
)
# Imported as modules, not by name, so a test can script the model by
# replacing `llm.client.call_model_json` — the same reason the pipeline imports
# `detector.client` as a module.
from llm import client, prompts
from llm.models import ModelSpec
from logger import logger
from metrics import llm_bbox_attempts


# ---------------------------------------------------------------------------
# The record of one attempt
# ---------------------------------------------------------------------------


@dataclass
class BoxAttempt:
    """One pass round the loop, whatever became of it.

    Attributes:
        attempt:       1-based attempt number.
        bbox_grid:     What the model actually said, on the 0-1000 grid, or
                       None when it answered ``bbox: null`` or unusably.
        bbox_px:       The same box in ORIGINAL page pixels, clamped to the
                       page — None when there was no usable box to convert.
        valid:         Whether it survived validation.
        reject:        One human sentence saying why not. None when valid.
        verify_score:  The crop check's 1-10, or None when it never ran.
        verify_reason: What the model said it saw in the crop.
        detector_iou:  Overlap with the detector's best box for the same
                       criterion, or None when there was none to compare to.
        accepted:      valid AND verify_score >= LLM_BBOX_VERIFY_PASS.
        coarse_bbox_grid:   The first answer, before the refine pass moved it.
                            None when no refine pass ran; then ``bbox_grid``
                            IS the first answer.
        refine_window_grid: The page-frame window the refine crop covered.
        refined:       True when ``bbox_grid`` came from the refine pass.
        refine_reject: Why the refine answer was not used, when it was not.
        refine_mode:   Marks mode only (``refine_mode="marks"``): which
                       pass refined this attempt — ``"marks"``, or
                       ``"grid"`` when it fell back. None in grid mode.
        marks:         The mark numbers the model chose (1-based, as shown),
                       when the marks pass produced the box. Ids only — the
                       lines' text is never recorded here.
        marks_shown:   How many marks the marks call drew (none made: None).
        marks_fallback: Why the grid refine ran instead of the marks.
        verify_partial: The verify's ``partial`` answer (the crop cut the
                       feature off) — only with ``verify_partial``, and
                       None when the verify did not say.

    In marks mode ``bbox_grid`` of a marks-refined attempt is the union box
    converted to the page grid (``pixels_to_grid``), not a number the model
    wrote.
    """

    attempt: int
    bbox_grid: Optional[list[float]] = None
    bbox_px: Optional[list[float]] = None
    valid: bool = False
    reject: Optional[str] = None
    verify_score: Optional[int] = None
    verify_reason: Optional[str] = None
    detector_iou: Optional[float] = None
    accepted: bool = False
    coarse_bbox_grid: Optional[list[float]] = None
    refine_window_grid: Optional[list[float]] = None
    refined: bool = False
    refine_reject: Optional[str] = None
    refine_mode: Optional[str] = None
    marks: Optional[list[int]] = None
    marks_shown: Optional[int] = None
    marks_fallback: Optional[str] = None
    verify_partial: Optional[bool] = None

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe view for ``localization.attempts`` and regions.json.

        Keys that never applied are omitted rather than sent as null: an
        attempt the validator rejected has no ``verify_score`` because no
        verify call was made, which is a different fact from a verify call
        that scored nothing. The marks keys and ``verify_partial`` exist only
        behind their settings, so with those off the record is exactly what
        it always was.
        """
        out: dict[str, Any] = {
            "attempt": self.attempt,
            "bbox_grid": [round(v, 1) for v in self.bbox_grid] if self.bbox_grid else None,
            "bbox_px": [round(v, 2) for v in self.bbox_px] if self.bbox_px else None,
            "valid": self.valid,
            "accepted": self.accepted,
        }
        if self.reject:
            out["reject"] = self.reject
        if self.verify_score is not None:
            out["verify_score"] = self.verify_score
        if self.verify_reason:
            out["verify_reason"] = self.verify_reason
        if self.verify_partial is not None:
            out["verify_partial"] = self.verify_partial
        if self.detector_iou is not None:
            out["detector_iou"] = round(self.detector_iou, 4)
        if self.coarse_bbox_grid is not None:
            out["coarse_bbox_grid"] = [round(v, 1) for v in self.coarse_bbox_grid]
            out["refine_window_grid"] = (
                [round(v, 1) for v in self.refine_window_grid] if self.refine_window_grid else None
            )
            out["refined"] = self.refined
            if self.refine_reject:
                out["refine_reject"] = self.refine_reject
            if self.refine_mode is not None:
                out["refine_mode"] = self.refine_mode
                if self.marks_shown is not None:
                    out["marks_shown"] = self.marks_shown
                if self.marks:
                    out["marks"] = list(self.marks)
                if self.marks_fallback:
                    out["marks_fallback"] = self.marks_fallback
        return out

    def as_region(self, name: str, geometry: PageGeometry) -> Optional[Region]:
        """This attempt as a drawable Region, or None when it has no box.

        A rejected attempt still becomes a region (with
        ``attrs.accepted = False``) whenever there were four numbers to draw —
        which is exactly the case worth looking at, since the commonest
        rejection is a box covering the whole frame. An attempt the model
        answered ``bbox: null`` to has nothing to draw and returns None; it
        is still listed in ``localization.attempts``.
        """
        if not self.bbox_px:
            return None
        x1, y1, x2, y2 = self.bbox_px
        attrs: dict[str, Any] = {"attempt": self.attempt, "accepted": self.accepted}
        if self.verify_score is not None:
            attrs["verify_score"] = self.verify_score
        if self.detector_iou is not None:
            attrs["detector_iou"] = round(self.detector_iou, 4)
        if self.coarse_bbox_grid is not None:
            attrs["refined"] = self.refined
        if self.reject:
            attrs["reject"] = self.reject
        return Region(
            page=geometry.page,
            kind="box",
            points=[(min(x1, x2), min(y1, y2)), (max(x1, x2), max(y1, y2))],
            label=name,
            score=self.verify_score,
            source="llm",
            attrs=attrs,
        )


@dataclass
class Localization:
    """Every attempt for one criterion, and which (if any) was accepted.

    ``guided`` and ``guided_calls`` say what the reference examples reached,
    for the caller (``analysis.llm_eval``) to turn into
    ``localization.references``, ``references.json`` entries and metrics —
    the loop itself never imports ``analysis``. Neither is in ``as_dict()``:
    without references the record is exactly what it always was.

    Attributes:
        guided:       ``{"ask", "refine", "verify"}`` — whether that step's
                      calls carry examples (its ``guide_*`` flag on and
                      examples given),
                      fixed for the unit; a step that never ran still says
                      what it would have carried.
        guided_calls: One ``{kind: box_ask | box_refine | box_verify,
                      attempt, images, ok}`` per call that carried examples;
                      ``images`` counts the candidate, ``ok`` is False when
                      ``call_model_json`` returned None.
    """

    attempts: list[BoxAttempt] = field(default_factory=list)
    accepted_attempt: Optional[int] = None
    calls: int = 0
    guided: dict[str, bool] = field(
        default_factory=lambda: {"ask": False, "refine": False, "verify": False}
    )
    guided_calls: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "attempts": [a.as_dict() for a in self.attempts],
            "accepted_attempt": self.accepted_attempt,
            "calls": self.calls,
        }


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------


def wants_boxes(hint: str, entry: Any) -> bool:
    """Is this criterion worth spending the loop's calls on?

    Called only for an ``llm`` criterion with ``options.boxes`` set (a `cv` /
    `text` / `detector` criterion is localised by a path that measures rather
    than claims). Two further conditions, both about not paying for a
    question with no answer:

      * the hint is ``presence`` or ``auto`` — "image sharpness" is a
        property of the whole page and boxing it would be a fabrication;
      * the scoring call gave it LLM_BBOX_PRESENCE_MIN or more — below that
        line the model has just said the feature is absent, and asking the
        same model where the thing it cannot see is produces a guess.

    Args:
        hint:  The criterion's ``options.hint``.
        entry: Its scoring-call answer (``{"score", ...}``).
    """
    if hint not in ("presence", "auto"):
        return False
    if not isinstance(entry, dict):
        return False
    score = entry.get("score")
    if not isinstance(score, (int, float)):
        return False
    return score >= LLM_BBOX_PRESENCE_MIN


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _numbers(raw: Any) -> Optional[list[float]]:
    """``raw`` as four finite floats, or None.

    Accepts a list or tuple of four numbers (or numeric strings — a model in
    JSON mode occasionally quotes them). Anything else, including the
    ``null`` that means "not visible here", is None.
    """
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    out: list[float] = []
    for value in raw:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number):
            return None
        out.append(number)
    return out


def validate_bbox(
    raw: Any, *, present: bool = True, grid: float = LLM_BBOX_GRID
) -> tuple[Optional[list[float]], Optional[str]]:
    """Check one model answer's ``bbox``.

    Returns ``(bbox, reject)``. ``bbox`` is the four numbers as the model gave
    them whenever there were four numbers — even for a rejected box, so the
    caller can still draw it and the operator can see what the model did. A
    non-None ``reject`` is one human sentence naming the problem; it goes
    straight into the retry's feedback and into the attempt record.

    The checks, in order:
      1. four finite numbers at all (``bbox: null`` fails here, deliberately —
         it is a legitimate answer, not a malformed one, and the CALLER
         distinguishes the two: an explicit null ends the loop, a malformed
         answer retries);
      2. inside the 0-``grid`` square, within a one-unit tolerance for a model
         that writes 1000.4;
      3. positive width and height;
      4. area within LLM_BBOX_MIN_AREA … LLM_BBOX_MAX_AREA of the frame.

    Args:
        raw:     The answer's ``bbox`` value.
        present: False when the key was missing entirely, so the rejection
                 says that instead of claiming the model answered ``null``.
        grid:    Grid span the box was asked for on.
    """
    bbox = _numbers(raw)
    if bbox is None:
        if not present:
            return None, "your answer had no 'bbox' key at all"
        if raw is None:
            return None, (
                "you answered bbox: null — you said the feature is not visible "
                "in this image"
            )
        return None, f"the answer's bbox was not four numbers ({raw!r})"

    x1, y1, x2, y2 = bbox
    tol = 1.0
    if min(bbox) < -tol or max(bbox) > grid + tol:
        return bbox, (
            f"your box {_fmt(bbox)} ran outside the 0-{int(grid)} grid — every "
            "coordinate must be inside the image"
        )
    if x2 - x1 <= 0 or y2 - y1 <= 0:
        return bbox, (
            f"your box {_fmt(bbox)} had no width or no height — it must be "
            "x1 < x2 and y1 < y2"
        )

    area = ((x2 - x1) * (y2 - y1)) / (grid * grid)
    if area > LLM_BBOX_MAX_AREA:
        return bbox, (
            f"your previous box {_fmt(bbox)} covered {area:.0%} of the image — "
            "a full-frame box is not a location; try again, smaller"
        )
    if area < LLM_BBOX_MIN_AREA:
        return bbox, (
            f"your previous box {_fmt(bbox)} covered {area:.2%} of the image, "
            f"under the {LLM_BBOX_MIN_AREA:.2%} floor — too small to be a "
            "location; try again, larger"
        )
    return bbox, None


def _fmt(bbox: list[float]) -> str:
    """A box as the model would have written it, for a feedback sentence."""
    return "[" + ", ".join(f"{v:g}" for v in bbox) + "]"


# ---------------------------------------------------------------------------
# The crop
# ---------------------------------------------------------------------------


def crop_for(
    image: Any, bbox_px: list[float], pad: float = LLM_BBOX_CROP_PAD, *, min_span: float = 0.0,
) -> Any:
    """Cut ``bbox_px`` out of the ORIGINAL page image, with padding.

    The original rather than the ≤1000-px working copy: a 200-px box on a
    working image is 200 soft pixels, and the crop check has to be able to see
    what is in it. Padding by ``pad`` of the box on each side because a model
    that boxes a feature slightly tight is right, and a crop that clips the
    thing being verified fails a check it should have passed.

    ``min_span`` (LLM_BBOX_VERIFY_MIN_SPAN, the verify crop only) grows the
    padded crop to at least that fraction of the page on each axis — see
    ``crop_rect``. 0 is exactly the crop this always cut.

    Args:
        image:    BGR numpy array of the original page.
        bbox_px:  ``[x1, y1, x2, y2]`` in that image's pixels.
        pad:      Fraction of the box width/height added on EACH side.
        min_span: Smallest crop, as a fraction of the page's width / height.

    Returns:
        A BGR sub-array, never empty (a degenerate box yields at least one
        pixel), or None when the image has no pixels at all.
    """
    if image is None or getattr(image, "size", 0) == 0:
        return None
    left, top, right, bottom = crop_rect(image.shape, bbox_px, pad, min_span=min_span)
    return image[top:bottom, left:right]


def crop_rect(
    shape: Any, bbox_px: list[float], pad: float = LLM_BBOX_CROP_PAD, *, min_span: float = 0.0,
) -> tuple[int, int, int, int]:
    """``(left, top, right, bottom)`` — the pixels ``crop_for`` cuts from an
    image of ``shape``: ``bbox_px`` padded by ``pad`` of its size on each
    side, clamped to the image, never empty.

    With ``min_span`` > 0 each axis is first clamped to the page, then — if
    shorter than ``min_span`` × that page dimension — grown to it, CENTRED on
    the box, and slid back inside the page when the growth ran over an edge
    (so a field at the top of the page still gets the full span below it);
    a span larger than the page is the whole axis. Both axes, as the refine
    window's minimum is: a wide text line is already wider than the width
    floor, so only its height grows, while a small square feature gets
    context on both. Used by ``analysis.references.region_text`` too, to
    know where the OCR'd crop sits on its page.
    """
    height, width = shape[:2]
    x1, y1, x2, y2 = bbox_px
    dx = (x2 - x1) * pad
    dy = (y2 - y1) * pad
    lo_x, hi_x, lo_y, hi_y = x1 - dx, x2 + dx, y1 - dy, y2 + dy
    if min_span > 0:
        lo_x, hi_x = _at_least(lo_x, hi_x, min_span * width, width)
        lo_y, hi_y = _at_least(lo_y, hi_y, min_span * height, height)
    left = max(0, int(math.floor(lo_x)))
    top = max(0, int(math.floor(lo_y)))
    right = min(width, int(math.ceil(hi_x)))
    bottom = min(height, int(math.ceil(hi_y)))
    # A box the clamp collapsed (entirely off-page after rounding) still has
    # to produce something the encoder can handle.
    right = max(right, left + 1)
    bottom = max(bottom, top + 1)
    return left, top, right, bottom


def _at_least(lo: float, hi: float, span: float, limit: float) -> tuple[float, float]:
    """``[lo, hi]`` clamped to ``[0, limit]``, then grown centred to ``span``
    (at most ``limit``) and slid back inside ``[0, limit]``."""
    lo, hi = max(0.0, lo), min(float(limit), hi)
    span = min(float(span), float(limit))
    if hi - lo >= span:
        return lo, hi
    centre = (lo + hi) / 2
    lo, hi = centre - span / 2, centre + span / 2
    if lo < 0:
        lo, hi = 0.0, span
    elif hi > limit:
        lo, hi = float(limit) - span, float(limit)
    return lo, hi


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


async def locate_criterion(
    name: str,
    *,
    image_b64: str,
    original_image: Any,
    geometry: PageGeometry,
    detector_regions: Optional[list[Region]] = None,
    max_attempts: int = LLM_BBOX_MAX_ATTEMPTS,
    working_image: Any = None,
    gridlines: Optional[bool] = None,
    refine: Optional[bool] = None,
    ask_b64: Optional[str] = None,
    model: Optional[ModelSpec] = None,
    ask_examples: Optional[list[prompts.BoxExample]] = None,
    crop_examples: Optional[list[prompts.BoxExample]] = None,
    verify_examples: Optional[list[prompts.BoxExample]] = None,
    guide_ask: Optional[bool] = None,
    guide_refine: Optional[bool] = None,
    guide_verify: Optional[bool] = None,
    text_lines: Optional[list[dict]] = None,
    refine_mode: Optional[str] = None,
    marks_max: Optional[int] = None,
    verify_partial: Optional[bool] = None,
    verify_min_span: Optional[float] = None,
) -> tuple[list[Region], Localization]:
    """Run the full ask → validate → refine → verify → retry loop for one criterion.

    Args:
        name:             The criterion to locate.
        image_b64:        Base64 JPEG of the ONE page image the scoring call
                          used — the model has to answer about the picture it
                          was scored on. This is what the ask sees when there
                          is no ``working_image`` to draw the grid on.
        original_image:   BGR array of that page at ORIGINAL size, for crops.
        geometry:         That page's frame; the 0-1000 grid converts through
                          it straight into original pixels.
        detector_regions: Phase 2's ``source="detector"`` boxes for the same
                          criterion, for the IoU cross-check. Informational.
        max_attempts:     The criterion's resolved ``options.max_attempts``
                          (at most LLM_BBOX_MAX_ATTEMPTS — the options model
                          refuses more at submit, and this clamps again).
        working_image:    BGR array of the ≤MAX_WORKING_DIMENSION page the
                          scoring call's image was encoded from — the same
                          pixels, so drawing the grid on it changes nothing
                          the model was scored on. None → no grid on the ask.
        gridlines:        Draw the grid (default LLM_BBOX_GRIDLINES).
        refine:           Run the zoomed second pass (default LLM_BBOX_REFINE).
        ask_b64:          The ask image already built by ``ask_image_b64``
                          (``DocumentContext.ask_image_b64`` memoises it per
                          item, so N located criteria on a page draw the grid
                          once). None → built here from ``gridlines``.
        model:            The registry entry every ask / refine / verify call
                          goes to (its ``max_tokens.box`` budget); None → the
                          registry default.
        ask_examples:     Reference examples for the ASK (composites, or
                          region crops under the position check), built once
                          per unit by ``analysis.references`` and the same on
                          every attempt.
        crop_examples:    Region crops of the same examples, for REFINE and
                          (unless ``verify_examples``) VERIFY.
        verify_examples:  What VERIFY shows instead of ``crop_examples`` —
                          ``bin/grounding_experiment.py --verify-images
                          composite`` passes the ask's composites. None (the
                          service: ``analysis.llm_eval`` passes nothing) →
                          ``crop_examples``, exactly as before. Refine always
                          shows ``crop_examples``.
        guide_ask / guide_refine / guide_verify:
                          Show that step its examples. The service passes the
                          criterion's resolved ``options.reference.boxes``;
                          left out (a direct caller), the REFERENCE_BOX_ASK /
                          _REFINE / _VERIFY knobs, read here at call time. A
                          step shows examples only when its flag is on AND
                          its list is non-empty; otherwise its prompt is the
                          unguided one, byte for byte. The labels never
                          change (``llm.client`` maps their prefixes to the
                          call kind); what carried examples is ``guided`` /
                          ``guided_calls`` on the Localization.
        text_lines:       The page's OCR text lines — ``common.documents
                          .TextLayer.lines``, ``{"text", "confidence",
                          "box"}`` with ``box`` a 4-point polygon in ORIGINAL
                          page pixels — for the marks refine. ``analysis
                          .llm_eval`` passes the layer the criterion's
                          ``options.ocr`` produced (no extra OCR pass). None
                          → no marks (the grid refine, noted in marks mode).
        refine_mode:      ``"grid"`` | ``"marks"`` — the service passes the
                          criterion's resolved ``options.refine`` (default,
                          for a direct caller, LLM_BBOX_REFINE_MODE; anything
                          else is grid).
        marks_max:        Most marks one marks call may show (default
                          LLM_BBOX_MARKS_MAX, service-wide); more → the grid
                          refine.
        verify_partial:   Ask the verify whether the crop cuts the feature
                          off — ``options.verify_partial`` (direct-caller
                          default LLM_BBOX_VERIFY_PARTIAL).
        verify_min_span:  Grow the verify crop to this share of the page on
                          each axis — ``options.verify_min_span``
                          (direct-caller default LLM_BBOX_VERIFY_MIN_SPAN; 0
                          = the padded box as always).

    Returns:
        ``(regions, localization)``. ``regions`` holds one Region per attempt
        that had a drawable box, accepted first; ``localization`` records
        every attempt including the undrawable ones.
    """
    if gridlines is None:
        gridlines = LLM_BBOX_GRIDLINES
    if refine is None:
        refine = LLM_BBOX_REFINE
    if guide_ask is None:
        guide_ask = REFERENCE_BOX_ASK
    if guide_refine is None:
        guide_refine = REFERENCE_BOX_REFINE
    if guide_verify is None:
        guide_verify = REFERENCE_BOX_VERIFY
    if refine_mode is None:
        refine_mode = LLM_BBOX_REFINE_MODE
    refine_mode = "marks" if str(refine_mode).strip().lower() == "marks" else "grid"
    if marks_max is None:
        marks_max = LLM_BBOX_MARKS_MAX
    if verify_partial is None:
        verify_partial = LLM_BBOX_VERIFY_PARTIAL
    if verify_min_span is None:
        verify_min_span = LLM_BBOX_VERIFY_MIN_SPAN
    max_attempts = max(1, min(int(max_attempts), LLM_BBOX_MAX_ATTEMPTS))
    loc = Localization()
    feedback: list[str] = []
    best_detector = _best_detector_region(detector_regions)
    # What each step shows, fixed for the unit: the same examples on every
    # attempt, so attempts differ only by their feedback.
    shown_ask = list(ask_examples or []) if guide_ask else []
    shown_refine = list(crop_examples or []) if guide_refine else []
    verify_source = crop_examples if verify_examples is None else verify_examples
    shown_verify = list(verify_source or []) if guide_verify else []
    loc.guided = {
        "ask": bool(shown_ask), "refine": bool(shown_refine), "verify": bool(shown_verify),
    }

    if ask_b64 is None:
        ask_b64 = ask_image_b64(image_b64, working_image, gridlines=gridlines)
    grid_on_ask = ask_b64 is not image_b64
    if gridlines and not grid_on_ask:
        logger.debug(
            "locate_criterion: '%s' — no working image to draw the grid on; "
            "asking on the bare page image", name,
        )

    for number in range(1, max_attempts + 1):
        attempt = BoxAttempt(attempt=number)
        loc.attempts.append(attempt)

        # Step 1 — ask.
        loc.calls += 1
        answer = await client.call_model_json(
            prompts.build_bbox_prompt(
                ask_b64, name, feedback=feedback,
                gridlines=grid_on_ask, grid_step=LLM_BBOX_GRID_STEP,
                references=shown_ask, model=model,
            ),
            label=f"bbox/{name}#{number}",
            model=model,
        )
        _log_guided(loc, "box_ask", number, shown_ask, answer)
        if answer is None:
            attempt.reject = (
                "the model did not return a usable answer for this attempt"
            )
            llm_bbox_attempts.labels(outcome="rejected_invalid").inc()
            feedback.append(attempt.reject)
            continue

        # Step 2 — validate.
        raw_bbox = answer.get("bbox")
        bbox, reject = validate_bbox(raw_bbox, present="bbox" in answer)
        attempt.bbox_grid = bbox
        if bbox is not None:
            points = grid_to_pixels(bbox, geometry, LLM_BBOX_GRID)
            attempt.bbox_px = _clamped_box(points, geometry)
            _cross_check(attempt, name, geometry, best_detector)
        if reject:
            attempt.reject = reject
            llm_bbox_attempts.labels(outcome="rejected_invalid").inc()
            logger.info("locate_criterion: '%s' attempt %d rejected — %s",
                        name, number, reject)
            if bbox is None and "bbox" in answer and raw_bbox is None:
                # The model said the feature is not visible in this image.
                # That is an answer, not a malformed one — re-asking the same
                # model the same question about the same picture buys a guess,
                # so the loop stops here.
                logger.info(
                    "locate_criterion: '%s' — model reports the feature is not "
                    "visible on page %d; not retrying",
                    name, geometry.page,
                )
                break
            feedback.append(reject)
            continue
        attempt.valid = True

        # Step 2.5 — refine on a zoomed crop of the ORIGINAL page. The coarse
        # box already validated; the refined one only has to be a usable box
        # inside the window (no area floor — the whole point is that it may
        # be a thin text line the coarse box overshot). Marks mode tries the
        # numbered OCR lines first; the grid pass is the fallback.
        if refine and bbox is not None:
            window = refine_window(
                bbox, zoom=LLM_BBOX_REFINE_ZOOM, min_span=LLM_BBOX_REFINE_MIN_SPAN
            )
            attempt.coarse_bbox_grid = list(bbox)
            attempt.refine_window_grid = window
            crop = crop_window(original_image, window)
            if crop is None:
                attempt.refine_reject = (
                    "the page image was not available to crop for the refine pass"
                )
            else:
                marked = False
                if refine_mode == "marks":
                    marked = await _refine_by_marks(
                        attempt, loc, name, number, crop=crop, window=window,
                        image_shape=original_image.shape, geometry=geometry,
                        text_lines=text_lines, marks_max=int(marks_max),
                        feedback=list(feedback), shown=shown_refine, model=model,
                    )
                if not marked:
                    await _refine_by_grid(
                        attempt, loc, name, number, crop=crop, window=window,
                        geometry=geometry, gridlines=gridlines, shown=shown_refine,
                        model=model,
                    )
            _cross_check(attempt, name, geometry, best_detector)

        # Step 3 — verify by crop. The candidate's only image in this call is
        # the crop (example crops, when shown, are of other pages).
        crop = crop_for(original_image, attempt.bbox_px or [], min_span=float(verify_min_span))
        if crop is None:
            attempt.reject = (
                "the page image was not available to crop, so the box could "
                "not be verified"
            )
            llm_bbox_attempts.labels(outcome="rejected_verify").inc()
            break
        loc.calls += 1
        verdict = await client.call_model_json(
            prompts.build_verify_prompt(
                client.encode_image_to_base64(crop), name,
                references=shown_verify, model=model, partial=bool(verify_partial),
            ),
            label=f"verify/{name}#{number}",
            model=model,
        )
        _log_guided(loc, "box_verify", number, shown_verify, verdict)
        attempt.verify_score = _score_of(verdict)
        attempt.verify_reason = (
            str(verdict.get("reason"))[:400] if isinstance(verdict, dict) else None
        )
        if verify_partial:
            attempt.verify_partial = _partial_of(verdict)

        if attempt.verify_score is not None and attempt.verify_score >= LLM_BBOX_VERIFY_PASS:
            attempt.accepted = True
            loc.accepted_attempt = number
            llm_bbox_attempts.labels(outcome="accepted").inc()
            logger.info(
                "locate_criterion: '%s' accepted attempt %d (verify %d, iou %s)",
                name, number, attempt.verify_score,
                f"{attempt.detector_iou:.2f}" if attempt.detector_iou is not None else "n/a",
            )
            break

        shown = attempt.verify_score if attempt.verify_score is not None else "unreadable"
        if verify_partial and attempt.verify_partial:
            # On the right thing, cut off: the retry is told to widen around
            # the same spot, not to move away from it.
            attempt.reject = VERIFY_PARTIAL_REJECT.format(
                box=_fmt(attempt.bbox_grid or []), name=name, score=shown,
            )
        else:
            attempt.reject = (
                f"the crop of your previous box {_fmt(attempt.bbox_grid or [])} did "
                f"not show {name} (verify score {shown}); look elsewhere"
            )
        llm_bbox_attempts.labels(outcome="rejected_verify").inc()
        feedback.append(attempt.reject)

    if loc.accepted_attempt is None and loc.attempts:
        llm_bbox_attempts.labels(outcome="exhausted").inc()
        logger.info(
            "locate_criterion: '%s' — %d attempt(s), none accepted; the score "
            "is unchanged and regions carry accepted=false",
            name, len(loc.attempts),
        )

    # Accepted first so a consumer reading `regions[0]` gets the answer rather
    # than the first thing that was tried.
    regions = [r for r in (a.as_region(name, geometry) for a in loc.attempts) if r]
    regions.sort(key=lambda r: not r.attrs.get("accepted"))
    return regions, loc


# ---------------------------------------------------------------------------
# The grid, and the refine window
# ---------------------------------------------------------------------------


def ask_image_b64(image_b64: str, working_image: Any, *, gridlines: bool) -> str:
    """The image the ASK sees: the working page with the grid drawn on it
    when ``gridlines`` is on and the page is available, else ``image_b64``
    itself (identity, so a caller can tell which happened with ``is``)."""
    if not gridlines or working_image is None or getattr(working_image, "size", 0) == 0:
        return image_b64
    return client.encode_image_to_base64(_with_grid(working_image))


def _with_grid(image_bgr: Any) -> Any:
    """A BGR array → the same array with the labelled grid burned in, BGR."""
    import numpy as np

    rgb = np.ascontiguousarray(np.asarray(image_bgr)[:, :, ::-1])
    gridded = draw_grid_overlay(rgb, grid=LLM_BBOX_GRID, step=LLM_BBOX_GRID_STEP)
    return np.ascontiguousarray(np.asarray(gridded)[:, :, ::-1])


def refine_window(
    bbox: list[float], *, zoom: float, min_span: float, grid: float = LLM_BBOX_GRID
) -> list[float]:
    """The page-frame window the refine crop covers, in grid units.

    ``zoom`` × the coarse box on each axis, centred on it, never narrower
    than ``min_span`` of the page on either axis, clamped to the page. Wide
    enough that a coarse box 25-100 units off still has the feature inside
    the crop; tight enough that the feature is large in the crop, which is
    what makes the second answer precise.
    """
    x1, y1, x2, y2 = bbox
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    w = max((x2 - x1) * zoom, grid * min_span)
    h = max((y2 - y1) * zoom, grid * min_span)
    return [
        max(0.0, cx - w / 2), max(0.0, cy - h / 2),
        min(grid, cx + w / 2), min(grid, cy + h / 2),
    ]


def crop_window(image: Any, window: list[float], *, grid: float = LLM_BBOX_GRID) -> Optional[Any]:
    """Cut a grid-unit ``window`` out of the ORIGINAL page and bring it to
    the working size, so the refine crop has the same pixel budget as the
    page the coarse answer came from. None when there is no image."""
    if image is None or getattr(image, "size", 0) == 0:
        return None
    left, top, right, bottom = window_rect(window, image.shape, grid=grid)
    return _fit_working(image[top:bottom, left:right])


def window_rect(
    window: list[float], shape: Any, *, grid: float = LLM_BBOX_GRID
) -> tuple[int, int, int, int]:
    """A grid-unit ``window`` → ``(left, top, right, bottom)`` in the
    ORIGINAL page's pixels (``shape`` is that image's): floor on the near
    edges, ceil on the far ones, clamped, never empty — exactly the pixels
    ``crop_window`` cuts before it fits them to the working size. Each axis
    scales by its own dimension, as the grid does."""
    height, width = shape[:2]
    x1, y1, x2, y2 = window
    left = max(0, int(math.floor(x1 * width / grid)))
    top = max(0, int(math.floor(y1 * height / grid)))
    right = min(width, int(math.ceil(x2 * width / grid)))
    bottom = min(height, int(math.ceil(y2 * height / grid)))
    right = max(right, left + 1)
    bottom = max(bottom, top + 1)
    return left, top, right, bottom


def example_crop(page_bgr: Any, bounds: list[float], pad: float = LLM_BBOX_CROP_PAD) -> Optional[Any]:
    """A reference example's region as the loop shows it: cut out of the
    reference's ORIGINAL page exactly as the verify crop is cut from the
    candidate's (``crop_for``, padded by ``pad``), then brought down to the
    working size — ``crop_for`` never shrinks, and a region of a 4000-px
    photo can be most of it. ``bounds`` is ``[x1, y1, x2, y2]`` in that
    page's pixels (a polygon's bounds will do). None when there is no image.

    Used by ``analysis.references`` (the refine / verify examples, and the
    ask's under the position check) and ``bin/grounding_experiment.py``."""
    crop = crop_for(page_bgr, list(bounds), pad)
    return None if crop is None else _fit_working(crop)


def _fit_working(crop: Any) -> Any:
    """``crop`` shrunk to at most MAX_WORKING_DIMENSION on its long side
    (never enlarged), so it costs the model no more than a working page."""
    h, w = crop.shape[:2]
    if max(h, w) > MAX_WORKING_DIMENSION:
        import cv2

        scale = MAX_WORKING_DIMENSION / max(h, w)
        crop = cv2.resize(
            crop, (max(1, int(w * scale)), max(1, int(h * scale))),
            interpolation=cv2.INTER_AREA,
        )
    return crop


def window_to_full(
    bbox: list[float], window: list[float], *, grid: float = LLM_BBOX_GRID
) -> list[float]:
    """A box on the crop's own 0-``grid`` frame → the page's grid frame."""
    wx1, wy1, wx2, wy2 = window
    ww, wh = wx2 - wx1, wy2 - wy1
    x1, y1, x2, y2 = bbox
    return [
        wx1 + x1 / grid * ww, wy1 + y1 / grid * wh,
        wx1 + x2 / grid * ww, wy1 + y2 / grid * wh,
    ]


# ---------------------------------------------------------------------------
# The refine passes: the grid (today's), and the numbered marks
# ---------------------------------------------------------------------------


async def _refine_by_grid(
    attempt: BoxAttempt,
    loc: Localization,
    name: str,
    number: int,
    *,
    crop: Any,
    window: list[float],
    geometry: PageGeometry,
    gridlines: bool,
    shown: list[prompts.BoxExample],
    model: Optional[ModelSpec],
) -> None:
    """The grid refine: ``crop`` (the window, cut from the ORIGINAL page and
    fitted to the working size) with its own grid, a box on it, mapped back
    to the page grid (``window_to_full``). An unusable answer keeps the
    coarse box, with ``refine_reject`` saying why. One call."""
    coarse = attempt.coarse_bbox_grid or []
    loc.calls += 1
    fine = await client.call_model_json(
        prompts.build_bbox_prompt(
            client.encode_image_to_base64(_with_grid(crop) if gridlines else crop),
            name, gridlines=gridlines, grid_step=LLM_BBOX_GRID_STEP,
            zoomed=True, references=shown, model=model,
        ),
        label=f"refine/{name}#{number}",
        model=model,
    )
    _log_guided(loc, "box_refine", number, shown, fine)
    fine_bbox, fine_reject = (
        validate_bbox(fine.get("bbox"), present="bbox" in fine)
        if isinstance(fine, dict)
        else (None, "the model did not return a usable answer for the refine pass")
    )
    if fine_bbox is not None and fine_reject is None:
        bbox = window_to_full(fine_bbox, window)
        attempt.refined = True
        attempt.bbox_grid = bbox
        attempt.bbox_px = _clamped_box(grid_to_pixels(bbox, geometry, LLM_BBOX_GRID), geometry)
        logger.info(
            "locate_criterion: '%s' attempt %d refined %s → %s (window %s)",
            name, number, _fmt(coarse), _fmt(bbox), _fmt(window),
        )
    else:
        attempt.refine_reject = fine_reject or "unusable refine answer"
        logger.info(
            "locate_criterion: '%s' attempt %d kept the coarse box %s — refine pass: %s",
            name, number, _fmt(coarse), attempt.refine_reject,
        )


async def _refine_by_marks(
    attempt: BoxAttempt,
    loc: Localization,
    name: str,
    number: int,
    *,
    crop: Any,
    window: list[float],
    image_shape: Any,
    geometry: PageGeometry,
    text_lines: Optional[list[dict]],
    marks_max: int,
    feedback: list[str],
    shown: list[prompts.BoxExample],
    model: Optional[ModelSpec],
) -> bool:
    """The marks refine. True when it set the attempt's box; False when the
    grid refine must run instead, with ``marks_fallback`` saying why.

    The OCR lines mostly inside the window (``lines_in_window``, ORIGINAL
    pixels) are numbered in reading order, mapped onto ``crop``
    (``to_crop_px``) and drawn there (``draw_marks_overlay``); the model's
    chosen numbers (``parse_marks``) pick lines whose UNION, in original
    pixels, is the refined box — clamped to the page for ``bbox_px``, and
    converted to the page grid for ``bbox_grid``. One call, made only when
    there is something to mark and not too much of it.
    """
    attempt.refine_mode = "marks"
    why: Optional[str] = None
    if text_lines is None:
        why = "no text lines were given for the marks pass"
    else:
        lines = [b for b in (line_bounds(line) for line in text_lines) if b is not None]
        rect = window_rect(window, image_shape)
        inside = lines_in_window(lines, rect)
        if not lines:
            why = "the page's text layer has no line boxes (native text, or OCR found none)"
        elif not inside:
            why = "no OCR text line lies inside the refine window"
        elif len(inside) > marks_max:
            why = (
                f"{len(inside)} OCR text lines lie inside the refine window, over the cap "
                f"of {marks_max}"
            )
        else:
            loc.calls += 1
            attempt.marks_shown = len(inside)
            answer = await client.call_model_json(
                prompts.build_marks_prompt(
                    client.encode_image_to_base64(
                        _with_marks(crop, to_crop_px(inside, rect, crop.shape))
                    ),
                    name, marks=len(inside), feedback=feedback, references=shown,
                    model=model,
                ),
                label=f"refine/marks/{name}#{number}",
                model=model,
            )
            _log_guided(loc, "box_refine", number, shown, answer)
            chosen, why = parse_marks(answer, len(inside))
            if chosen:
                x1, y1, x2, y2 = union_box([inside[i - 1] for i in chosen])
                attempt.marks = chosen
                attempt.refined = True
                attempt.bbox_px = _clamped_box([(x1, y1), (x2, y2)], geometry)
                px = attempt.bbox_px
                # One decimal, as the record shows it: a retry's feedback
                # quotes this box, and the model writes grid numbers short.
                attempt.bbox_grid = [
                    round(v, 1) for v in pixels_to_grid(
                        [(px[0], px[1]), (px[2], px[3])], geometry, LLM_BBOX_GRID
                    )
                ]
                logger.info(
                    "locate_criterion: '%s' attempt %d refined by marks %s of %d → %s "
                    "(coarse %s)",
                    name, number, chosen, len(inside), _fmt(attempt.bbox_grid),
                    _fmt(attempt.coarse_bbox_grid or []),
                )
                return True
    attempt.refine_mode = "grid"
    attempt.marks_fallback = why
    logger.info(
        "locate_criterion: '%s' attempt %d — marks pass not used (%s); grid refine",
        name, number, why,
    )
    return False


def line_bounds(line: Any) -> Optional[list[float]]:
    """One OCR line's ``[x1, y1, x2, y2]`` — the bounds of its ``box`` (a
    4-point polygon ``[[x, y], …]``, as ``RapidOCREngine`` gives it, or
    already ``[x1, y1, x2, y2]``) in the same pixels as the box. None for a
    line with no usable box, or one with no width or height."""
    box = line.get("box") if isinstance(line, dict) else None
    try:
        if isinstance(box, (list, tuple)) and box and isinstance(box[0], (list, tuple)):
            xs = [float(p[0]) for p in box]
            ys = [float(p[1]) for p in box]
        elif isinstance(box, (list, tuple)) and len(box) == 4:
            xs = [float(box[0]), float(box[2])]
            ys = [float(box[1]), float(box[3])]
        else:
            return None
    except (TypeError, ValueError, IndexError):
        return None
    if not all(math.isfinite(v) for v in xs + ys):
        return None
    bounds = [min(xs), min(ys), max(xs), max(ys)]
    if bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
        return None
    return bounds


def lines_in_window(
    lines: list[list[float]],
    rect: tuple[int, int, int, int],
    *,
    min_inside: float = LLM_BBOX_MARKS_MIN_INSIDE,
) -> list[list[float]]:
    """The line bounds (ORIGINAL pixels) with at least ``min_inside`` of
    their area inside ``rect`` (``window_rect``), in reading order: rows top
    to bottom, left to right within a row. A row is the lines whose vertical
    centres lie within half the median line height of the row's first —
    sorting on the top edge alone would put a right-hand column first
    whenever its line sat a pixel higher. The order is the mark numbers."""
    left, top, right, bottom = rect
    kept = []
    for b in lines:
        x1, y1, x2, y2 = b
        area = (x2 - x1) * (y2 - y1)
        if area <= 0:
            continue
        ix = max(0.0, min(x2, right) - max(x1, left))
        iy = max(0.0, min(y2, bottom) - max(y1, top))
        if ix * iy / area >= min_inside:
            kept.append(list(b))
    if not kept:
        return []
    heights = sorted(b[3] - b[1] for b in kept)
    tolerance = heights[len(heights) // 2] / 2
    kept.sort(key=lambda b: (b[1] + b[3]) / 2)
    rows: list[list[list[float]]] = []
    for b in kept:
        centre = (b[1] + b[3]) / 2
        if rows and centre - (rows[-1][0][1] + rows[-1][0][3]) / 2 <= tolerance:
            rows[-1].append(b)
        else:
            rows.append([b])
    return [b for row in rows for b in sorted(row, key=lambda b: b[0])]


def to_crop_px(
    bounds: list[list[float]], rect: tuple[int, int, int, int], crop_shape: Any
) -> list[list[float]]:
    """Boxes in ORIGINAL pixels → the pixels of the crop ``crop_window`` cut
    at ``rect`` and fitted to ``crop_shape``: shift by the rect's origin,
    then scale each axis by its own factor (``_fit_working`` rounds each
    side, so the two can differ by a hair). Parts outside the crop stay
    outside; the overlay clips them."""
    left, top, right, bottom = rect
    sx = crop_shape[1] / (right - left)
    sy = crop_shape[0] / (bottom - top)
    return [
        [(x1 - left) * sx, (y1 - top) * sy, (x2 - left) * sx, (y2 - top) * sy]
        for x1, y1, x2, y2 in bounds
    ]


def union_box(bounds: list[list[float]]) -> list[float]:
    """The smallest ``[x1, y1, x2, y2]`` holding every box — several marks,
    one feature (a label line and its value line)."""
    return [
        min(b[0] for b in bounds), min(b[1] for b in bounds),
        max(b[2] for b in bounds), max(b[3] for b in bounds),
    ]


def parse_marks(answer: Any, count: int) -> tuple[Optional[list[int]], Optional[str]]:
    """The marks answer (``prompts.MARKS_SCAFFOLD``) → ``(numbers, None)``,
    sorted and de-duplicated, or ``(None, why)`` when it cannot be used:
    no answer, no ``marks`` key, ``null`` / ``[]`` (none of the lines is the
    feature — the grid may still box it), something that is not whole
    numbers, or a number outside 1..``count``. A bare number, numeric
    strings and ``3.0`` are read as numbers; ``true`` is not."""
    if not isinstance(answer, dict):
        return None, "the model did not return a usable answer for the marks pass"
    if "marks" not in answer:
        return None, "the marks answer had no 'marks' key"
    raw = answer["marks"]
    if raw is None or (isinstance(raw, (list, tuple, str)) and len(raw) == 0):
        return None, (
            "the model answered marks: null — none of the marked lines is the feature"
        )
    if not isinstance(raw, (list, tuple)):
        raw = [raw]
    chosen: list[int] = []
    for value in raw:
        number: Optional[float] = None
        if not isinstance(value, bool):
            try:
                number = float(value)
            except (TypeError, ValueError):
                number = None
        if number is None or not math.isfinite(number) or not number.is_integer():
            shown = repr(answer["marks"])[:80]
            return None, f"'marks' was not a list of mark numbers ({shown})"
        if not 1 <= int(number) <= count:
            return None, f"mark {int(number)} is not one of the {count} marks shown"
        if int(number) not in chosen:
            chosen.append(int(number))
    return sorted(chosen), None


def _with_marks(image_bgr: Any, boxes: list[list[float]]) -> Any:
    """A BGR crop → the same crop with ``boxes`` (its own pixels) outlined
    and numbered 1..N (``common.vision.draw_marks_overlay``), BGR."""
    import numpy as np

    rgb = np.ascontiguousarray(np.asarray(image_bgr)[:, :, ::-1])
    marked = draw_marks_overlay(rgb, boxes)
    return np.ascontiguousarray(np.asarray(marked)[:, :, ::-1])


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _log_guided(
    loc: Localization, kind: str, attempt: int, shown: list[Any], answer: Any
) -> None:
    """Record one call that carried examples (``Localization.guided_calls``);
    a call with none shown is not a guided call and is not logged."""
    if shown:
        loc.guided_calls.append({
            "kind": kind, "attempt": attempt, "images": len(shown) + 1,
            "ok": answer is not None,
        })


def _cross_check(
    attempt: BoxAttempt, name: str, geometry: PageGeometry, best_detector: Optional[Region]
) -> None:
    """Record the IoU against the detector's best box, when there is one."""
    if best_detector is None or not attempt.bbox_px:
        return
    region = attempt.as_region(name, geometry)
    if region is not None:
        attempt.detector_iou = iou(region, best_detector)


def _clamped_box(points: list[tuple[float, float]], geometry: PageGeometry) -> list[float]:
    """Two corner points → ``[x1, y1, x2, y2]`` inside the page."""
    xs = [max(0.0, min(float(geometry.width), p[0])) for p in points]
    ys = [max(0.0, min(float(geometry.height), p[1])) for p in points]
    return [min(xs), min(ys), max(xs), max(ys)]


def _best_detector_region(regions: Optional[list[Region]]) -> Optional[Region]:
    """The detector's highest-scoring box for a criterion, for the IoU check.

    The best one rather than all of them: the cross-check answers "did the two
    sources point at the same thing", and a model box that overlaps the
    detector's ninth-best guess has not.
    """
    usable = [r for r in (regions or []) if r.source == "detector"]
    if not usable:
        return None
    return max(usable, key=lambda r: r.score or 0.0)


def _score_of(verdict: Any) -> Optional[int]:
    """The verify call's 1-10, clamped, or None when it did not answer one."""
    if not isinstance(verdict, dict):
        return None
    try:
        return max(1, min(10, int(float(verdict.get("score")))))
    except (TypeError, ValueError):
        return None


def _partial_of(verdict: Any) -> Optional[bool]:
    """The verify call's ``partial`` (``verify_partial``): a JSON
    boolean, ``0`` / ``1``, or ``"true"`` / ``"false"`` / ``"yes"`` /
    ``"no"`` in any case — a model in JSON mode occasionally quotes it; None
    when it did not say."""
    if not isinstance(verdict, dict):
        return None
    value = verdict.get("partial")
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in ("true", "false", "yes", "no"):
        return value.strip().lower() in ("true", "yes")
    return None
