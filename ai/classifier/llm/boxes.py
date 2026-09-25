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
    verify    crop that box out of the ORIGINAL page (+LLM_BBOX_CROP_PAD on
             each side), send the crop ALONE, and ask whether the feature is
             visible in it. Accept at LLM_BBOX_VERIFY_PASS or better.
                                                (llm.prompts.build_verify_prompt)
    cross-check  IoU against the detector's best box for the same criterion,
             when phase 2's detector produced one. Informational: it is
             recorded as ``attrs.detector_iou`` and never accepts or rejects.
    retry     re-ask with the rejection as feedback ("your previous box …
             covered 100% of the image …; try again, smaller"), up to
             LLM_BBOX_MAX_ATTEMPTS.

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
box validated — so at most ``3 × LLM_BBOX_MAX_ATTEMPTS`` small calls per
located criterion, on top of the single scoring call the whole job shares.
That is why the loop is opt-in (``regions.llm_boxes``) and gated on the
presence score.

Process flow position: called from ``analysis.pipeline.analyze_document`` step
6.5, after the scoring call and before the regions are written.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

from common.vision import PageGeometry, Region, draw_grid_overlay, grid_to_pixels, iou

from api.schemas import CriterionInput
from config import (
    LLM_BBOX_CROP_PAD,
    LLM_BBOX_GRID,
    LLM_BBOX_GRID_STEP,
    LLM_BBOX_GRIDLINES,
    LLM_BBOX_MAX_AREA,
    LLM_BBOX_MAX_ATTEMPTS,
    LLM_BBOX_MIN_AREA,
    LLM_BBOX_PRESENCE_MIN,
    LLM_BBOX_REFINE,
    LLM_BBOX_REFINE_MIN_SPAN,
    LLM_BBOX_REFINE_ZOOM,
    LLM_BBOX_VERIFY_PASS,
    MAX_WORKING_DIMENSION,
)
# Imported as modules, not by name, so a test can script the model by
# replacing `llm.client.call_vllm_json` — the same reason the pipeline imports
# `detector.client` as a module.
from llm import client, prompts
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

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe view for ``localization.attempts`` and regions.json.

        Keys that never applied are omitted rather than sent as null: an
        attempt the validator rejected has no ``verify_score`` because no
        verify call was made, which is a different fact from a verify call
        that scored nothing.
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
    """Every attempt for one criterion, and which (if any) was accepted."""

    attempts: list[BoxAttempt] = field(default_factory=list)
    accepted_attempt: Optional[int] = None
    calls: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "attempts": [a.as_dict() for a in self.attempts],
            "accepted_attempt": self.accepted_attempt,
            "calls": self.calls,
        }


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------


def wants_boxes(c: CriterionInput, entry: Any) -> bool:
    """Is this criterion worth spending the loop's calls on?

    Three conditions, all of them about not paying for a question with no
    answer:

      * it is an ``llm`` criterion — a `cv` / `text` / `detector` criterion
        already localised through a path that measures rather than claims;
      * its hint is ``presence`` or ``auto`` — "image sharpness" is a
        property of the whole page and boxing it would be a fabrication;
      * the scoring call gave it LLM_BBOX_PRESENCE_MIN or more — below that
        line the model has just said the feature is absent, and asking the
        same model where the thing it cannot see is produces a guess.

    Args:
        c:     The criterion.
        entry: Its per-criterion result from the scoring call.
    """
    if c.type != "llm" or c.hint not in ("presence", "auto"):
        return False
    if not isinstance(entry, dict) or entry.get("verdict") == "SKIPPED":
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


def crop_for(image: Any, bbox_px: list[float], pad: float = LLM_BBOX_CROP_PAD) -> Any:
    """Cut ``bbox_px`` out of the ORIGINAL page image, with padding.

    The original rather than the ≤1000-px working copy: a 200-px box on a
    working image is 200 soft pixels, and the crop check has to be able to see
    what is in it. Padding by ``pad`` of the box on each side because a model
    that boxes a feature slightly tight is right, and a crop that clips the
    thing being verified fails a check it should have passed.

    Args:
        image:   BGR numpy array of the original page.
        bbox_px: ``[x1, y1, x2, y2]`` in that image's pixels.
        pad:     Fraction of the box width/height added on EACH side.

    Returns:
        A BGR sub-array, never empty (a degenerate box yields at least one
        pixel), or None when the image has no pixels at all.
    """
    if image is None or getattr(image, "size", 0) == 0:
        return None
    height, width = image.shape[:2]
    x1, y1, x2, y2 = bbox_px
    dx = (x2 - x1) * pad
    dy = (y2 - y1) * pad
    left = max(0, int(math.floor(x1 - dx)))
    top = max(0, int(math.floor(y1 - dy)))
    right = min(width, int(math.ceil(x2 + dx)))
    bottom = min(height, int(math.ceil(y2 + dy)))
    # A box the clamp collapsed (entirely off-page after rounding) still has
    # to produce something the encoder can handle.
    right = max(right, left + 1)
    bottom = max(bottom, top + 1)
    return image[top:bottom, left:right]


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
        max_attempts:     Override for LLM_BBOX_MAX_ATTEMPTS (tests).
        working_image:    BGR array of the ≤MAX_WORKING_DIMENSION page the
                          scoring call's image was encoded from — the same
                          pixels, so drawing the grid on it changes nothing
                          the model was scored on. None → no grid on the ask.
        gridlines:        Draw the grid (default LLM_BBOX_GRIDLINES).
        refine:           Run the zoomed second pass (default LLM_BBOX_REFINE).

    Returns:
        ``(regions, localization)``. ``regions`` holds one Region per attempt
        that had a drawable box, accepted first; ``localization`` records
        every attempt including the undrawable ones.
    """
    if gridlines is None:
        gridlines = LLM_BBOX_GRIDLINES
    if refine is None:
        refine = LLM_BBOX_REFINE
    loc = Localization()
    feedback: list[str] = []
    best_detector = _best_detector_region(detector_regions)

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
        answer = await client.call_vllm_json(
            prompts.build_bbox_prompt(
                ask_b64, name, feedback=feedback,
                gridlines=grid_on_ask, grid_step=LLM_BBOX_GRID_STEP,
            ),
            label=f"bbox/{name}#{number}",
        )
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
        # be a thin text line the coarse box overshot).
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
                loc.calls += 1
                fine = await client.call_vllm_json(
                    prompts.build_bbox_prompt(
                        client.encode_image_to_base64(
                            _with_grid(crop) if gridlines else crop
                        ),
                        name, gridlines=gridlines, grid_step=LLM_BBOX_GRID_STEP,
                        zoomed=True,
                    ),
                    label=f"refine/{name}#{number}",
                )
                fine_bbox, fine_reject = (
                    validate_bbox(fine.get("bbox"), present="bbox" in fine)
                    if isinstance(fine, dict)
                    else (None, "the model did not return a usable answer for the refine pass")
                )
                if fine_bbox is not None and fine_reject is None:
                    bbox = window_to_full(fine_bbox, window)
                    attempt.refined = True
                    attempt.bbox_grid = bbox
                    attempt.bbox_px = _clamped_box(
                        grid_to_pixels(bbox, geometry, LLM_BBOX_GRID), geometry
                    )
                    logger.info(
                        "locate_criterion: '%s' attempt %d refined %s → %s (window %s)",
                        name, number, _fmt(attempt.coarse_bbox_grid), _fmt(bbox), _fmt(window),
                    )
                else:
                    attempt.refine_reject = fine_reject or "unusable refine answer"
                    logger.info(
                        "locate_criterion: '%s' attempt %d kept the coarse box %s — "
                        "refine pass: %s",
                        name, number, _fmt(bbox), attempt.refine_reject,
                    )
            _cross_check(attempt, name, geometry, best_detector)

        # Step 3 — verify by crop. The ONE image in this call is the crop.
        crop = crop_for(original_image, attempt.bbox_px or [])
        if crop is None:
            attempt.reject = (
                "the page image was not available to crop, so the box could "
                "not be verified"
            )
            llm_bbox_attempts.labels(outcome="rejected_verify").inc()
            break
        loc.calls += 1
        verdict = await client.call_vllm_json(
            prompts.build_verify_prompt(client.encode_image_to_base64(crop), name),
            label=f"verify/{name}#{number}",
        )
        attempt.verify_score = _score_of(verdict)
        attempt.verify_reason = (
            str(verdict.get("reason"))[:400] if isinstance(verdict, dict) else None
        )

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


async def locate_criteria(
    criteria: list[CriterionInput],
    per_criterion_scores: dict,
    *,
    image_b64: Optional[str],
    original_image: Any,
    geometry: Optional[PageGeometry],
    detector_regions: Optional[dict[str, list[Region]]] = None,
    working_image: Any = None,
) -> tuple[dict[str, list[Region]], dict[str, dict]]:
    """Run the loop for every criterion that qualifies.

    Sequentially, not with ``asyncio.gather``: the calls are small but they
    land on the same vLLM instance the scoring call just used, and a job with
    eight presence criteria would otherwise open sixteen concurrent requests
    against a server running ``--max-num-seqs 4``. Several jobs already run in
    parallel (CLASSIFIER_MAX_CONCURRENT) — the concurrency belongs there.

    Args:
        criteria:             Every criterion in the request; the gate picks.
        per_criterion_scores: The scoring call's results, read for the gate
                              only and never written to.
        image_b64:            The page image the scoring call attached, or
                              None (a text-only document — nothing to locate).
        original_image:       BGR array of that page at original size.
        geometry:             That page's frame, or None.
        detector_regions:     ``{criterion: [Region, ...]}`` from phase 2.
        working_image:        BGR array of the ≤MAX_WORKING_DIMENSION page
                              ``image_b64`` was encoded from, for the grid
                              overlay. None → the ask sees the bare image.

    Returns:
        ``({criterion: [Region, ...]}, {criterion: localization dict})`` —
        entries only for the criteria that actually ran.
    """
    if not image_b64 or geometry is None:
        return {}, {}

    wanted = [c for c in criteria if wants_boxes(c, per_criterion_scores.get(c.name))]
    if not wanted:
        logger.info(
            "locate_criteria: regions.llm_boxes is on but no criterion qualifies "
            "(needs type=llm, hint presence/auto, and a presence score >= %d)",
            LLM_BBOX_PRESENCE_MIN,
        )
        return {}, {}

    logger.info(
        "locate_criteria: running the enforcement loop for %d criterion/criteria "
        "on page %d: %s",
        len(wanted), geometry.page, [c.name for c in wanted],
    )
    regions: dict[str, list[Region]] = {}
    localizations: dict[str, dict] = {}
    for c in wanted:
        found, loc = await locate_criterion(
            c.name,
            image_b64=image_b64,
            original_image=original_image,
            geometry=geometry,
            detector_regions=(detector_regions or {}).get(c.name),
            working_image=working_image,
        )
        regions[c.name] = found
        localizations[c.name] = loc.as_dict()
    return regions, localizations


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
    height, width = image.shape[:2]
    x1, y1, x2, y2 = window
    left = max(0, int(math.floor(x1 * width / grid)))
    top = max(0, int(math.floor(y1 * height / grid)))
    right = min(width, int(math.ceil(x2 * width / grid)))
    bottom = min(height, int(math.ceil(y2 * height / grid)))
    right = max(right, left + 1)
    bottom = max(bottom, top + 1)
    crop = image[top:bottom, left:right]
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
# Small helpers
# ---------------------------------------------------------------------------


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
