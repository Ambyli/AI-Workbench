"""Whole-page quality measurements: sharpness and exposure.

Two deterministic checks, both of them properties of the entire page rather
than of anything in it:

    check_blur()     — Laplacian variance -> sharpness score.
    check_exposure() — mean pixel intensity -> exposure score.

Neither emits ``regions``: a full-frame box would be a non-answer dressed up
as a location. Confidence is always 100 — these are measurements, not
opinions. The thresholds (BLUR_THRESHOLD, EXPOSURE_LOW / _HIGH) live in
config.py § OpenCV pre-check thresholds.

Process flow position: registered in ``cv/__init__.py``'s REGISTRY and run by
``analysis.cv_eval._run_cv_criterion``.
"""

import cv2
import numpy as np

from config import BLUR_FULL_SCORE_MULTIPLE, BLUR_THRESHOLD, EXPOSURE_HIGH, EXPOSURE_LOW
from cv.result import DetectorSpec, Measurement, cv_result, describes
from logger import logger


@describes(DetectorSpec(
    technique="Laplacian variance",
    metric="laplacian_variance",
    measurements={
        "laplacian_variance": Measurement(
            "variance", "Variance of the Laplacian of the greyscale page — higher is sharper"
        ),
    },
    thresholds={
        "pass_at_or_above": "PASS at or above this variance",
        "full_score_at": "the variance at which the score reaches 10",
    },
))
def check_blur(image) -> dict:
    """Measure image sharpness using the Laplacian operator.

    The Laplacian highlights rapid intensity changes (edges).  A sharp image
    has high variance in its Laplacian response; a blurry image has low
    variance because edges are smoothed out.

    Scoring: variance is linearly mapped to 1-10, capped at 10.
    FAIL threshold: BLUR_THRESHOLD (100.0 by default, set in config.py).
    Confidence is always 100 — this is a deterministic measurement.

    Emits no ``regions``: sharpness is a property of the whole page, and a
    full-frame box would be a non-answer dressed up as a location.

    Args:
        image: BGR numpy array (H×W×3) or grayscale (H×W).

    Returns:
        The ``cv.result`` shape: metric ``laplacian_variance``; thresholds
        ``pass_at_or_above`` (BLUR_THRESHOLD) and ``full_score_at`` (three
        times it, where the score reaches 10).
    """
    logger.debug("check_blur: image shape=%s", image.shape)

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
    variance = float(cv2.Laplacian(gray, cv2.CV_64F).var())

    result = cv_result(
        detector="check_blur",
        # Floored at 1: scores are 1-10 everywhere in this service, and a flat
        # or badly blurred page (variance under a tenth of full_score_at) used
        # to score 0 — which then entered the weighted average as a zero.
        score=max(1, min(10, int(
            10 * min(variance / (BLUR_THRESHOLD * BLUR_FULL_SCORE_MULTIPLE), 1.0)
        ))),
        verdict="PASS" if variance >= BLUR_THRESHOLD else "FAIL",
        confidence=100,
        reason=f"Laplacian variance: {variance:.1f} (threshold: {BLUR_THRESHOLD})",
        metric="laplacian_variance",
        measurements={"laplacian_variance": variance},
        thresholds={
            "pass_at_or_above": BLUR_THRESHOLD,
            "full_score_at": BLUR_THRESHOLD * BLUR_FULL_SCORE_MULTIPLE,
        },
    )
    logger.debug("check_blur: returning score=%s verdict=%s variance=%.1f",
                 result["score"], result["verdict"], variance)
    return result


@describes(DetectorSpec(
    technique="Mean pixel intensity",
    metric="mean_intensity",
    measurements={
        "mean_intensity": Measurement(
            "intensity", "Mean greyscale value of the page, 0 (black) to 255 (white)"
        ),
    },
    thresholds={
        "normal_min": "below this mean the page is underexposed (FAIL)",
        "normal_max": "above this mean the page is overexposed (FAIL)",
    },
    states={
        "underexposed": "mean below normal_min",
        "normal": "mean within normal_min..normal_max (PASS)",
        "overexposed": "mean above normal_max",
    },
))
def check_exposure(image) -> dict:
    """Check overall image exposure via mean pixel intensity.

    A correctly exposed image has mean intensity between EXPOSURE_LOW (30)
    and EXPOSURE_HIGH (220).  Images outside this range receive a fixed
    FAIL score of 2.  Within the normal range, mean intensity is linearly
    mapped to 1-10.
    Confidence is always 100 — this is a deterministic measurement.

    Emits no ``regions`` — like check_blur, the measurement is whole-page.

    Args:
        image: BGR numpy array (H×W×3) or grayscale (H×W).

    Returns:
        The ``cv.result`` shape: metric ``mean_intensity`` (0-255), thresholds
        ``normal_min`` / ``normal_max``, and ``state`` one of
        ``underexposed`` / ``normal`` / ``overexposed``.
    """
    logger.debug("check_exposure: image shape=%s", image.shape)

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
    mean = float(np.mean(gray))

    if mean < EXPOSURE_LOW:
        state, score, verdict = "underexposed", 2, "FAIL"
        reason = f"Underexposed (mean: {mean:.1f}, min: {EXPOSURE_LOW})"
    elif mean > EXPOSURE_HIGH:
        state, score, verdict = "overexposed", 2, "FAIL"
        reason = f"Overexposed (mean: {mean:.1f}, max: {EXPOSURE_HIGH})"
    else:
        state, verdict = "normal", "PASS"
        score = int(1 + 9 * (mean - EXPOSURE_LOW) / (EXPOSURE_HIGH - EXPOSURE_LOW))
        reason = f"Normal exposure (mean: {mean:.1f})"

    result = cv_result(
        detector="check_exposure",
        score=score,
        verdict=verdict,
        confidence=100,
        reason=reason,
        metric="mean_intensity",
        measurements={"mean_intensity": mean},
        thresholds={"normal_min": EXPOSURE_LOW, "normal_max": EXPOSURE_HIGH},
        state=state,
    )

    logger.debug("check_exposure: returning score=%s verdict=%s mean=%.1f",
                 result["score"], result["verdict"], mean)
    return result
