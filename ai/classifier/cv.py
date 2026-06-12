"""Deterministic OpenCV pre-checks run on every image before the LLM call.

These checks are fast, objective, and produce confidence=100 because they
measure concrete pixel statistics rather than relying on model interpretation.
Results live in the 'cv_pre_checks' field of every analysis response.

Two checks are performed:
  check_blur()     — Laplacian variance; low variance → blurry image
  check_exposure() — Mean pixel intensity; too dark or too bright → FAIL

CV results are intentionally kept separate from LLM per_criterion_scores.
They supplement the LLM assessment but do not override it.

Process flow position: called inside analysis.analyze_bgr() after the image
is decoded and resized, before the LLM prompt is sent.
"""

import numpy as np

from config import BLUR_THRESHOLD, EXPOSURE_LOW, EXPOSURE_HIGH
from logger import logger


def check_blur(image) -> dict:
    """Measure image sharpness using the Laplacian operator.

    The Laplacian highlights rapid intensity changes (edges).  A sharp image
    has high variance in its Laplacian response; a blurry image has low
    variance because edges are smoothed out.

    Scoring: variance is linearly mapped to 1-10, capped at 10.
    FAIL threshold: BLUR_THRESHOLD (100.0 by default).

    Args:
        image: BGR numpy array (H×W×3) or grayscale (H×W).

    Returns:
        dict with criterion, score (1-10), verdict, confidence (always 100),
        and a detail string showing the raw variance.
    """
    import cv2
    logger.debug("check_blur: image shape=%s", image.shape)

    # Convert to grayscale if needed — Laplacian operates on single-channel
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image

    # Compute Laplacian variance — higher = sharper
    variance = float(cv2.Laplacian(gray, cv2.CV_64F).var())

    result = {
        "criterion": "sharpness",
        # Scale variance to 1-10, capped so very sharp images don't exceed 10
        "score": min(10, int(10 * min(variance / (BLUR_THRESHOLD * 3), 1.0))),
        "verdict": "PASS" if variance >= BLUR_THRESHOLD else "FAIL",
        "confidence": 100,  # always 100 — this is a deterministic measurement
        "detail": f"Laplacian variance: {variance:.1f} (threshold: {BLUR_THRESHOLD})",
    }
    logger.debug("check_blur: returning score=%s verdict=%s variance=%.1f",
                 result["score"], result["verdict"], variance)
    return result


def check_exposure(image) -> dict:
    """Check overall image exposure via mean pixel intensity.

    A correctly exposed image has a mean intensity between EXPOSURE_LOW (30)
    and EXPOSURE_HIGH (220).  Images outside this range are underexposed or
    overexposed and receive a fixed FAIL score of 2.

    Scoring: within the normal range, mean intensity is linearly mapped to
    1-10 across the [EXPOSURE_LOW, EXPOSURE_HIGH] window.

    Args:
        image: BGR numpy array (H×W×3) or grayscale (H×W).

    Returns:
        dict with criterion, score (1-10), verdict, confidence (always 100),
        and a detail string showing the mean intensity.
    """
    import cv2
    logger.debug("check_exposure: image shape=%s", image.shape)

    # Convert to grayscale — exposure is a luminance measurement
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
    mean = float(np.mean(gray))

    if mean < EXPOSURE_LOW:
        # Too dark — underexposed
        result = {"criterion": "exposure", "score": 2, "verdict": "FAIL", "confidence": 100,
                  "detail": f"Underexposed (mean: {mean:.1f}, min: {EXPOSURE_LOW})"}
    elif mean > EXPOSURE_HIGH:
        # Too bright — overexposed
        result = {"criterion": "exposure", "score": 2, "verdict": "FAIL", "confidence": 100,
                  "detail": f"Overexposed (mean: {mean:.1f}, max: {EXPOSURE_HIGH})"}
    else:
        # Normal range — map linearly to 1-10
        score = int(1 + 9 * (mean - EXPOSURE_LOW) / (EXPOSURE_HIGH - EXPOSURE_LOW))
        result = {"criterion": "exposure", "score": score, "verdict": "PASS", "confidence": 100,
                  "detail": f"Normal exposure (mean: {mean:.1f})"}

    logger.debug("check_exposure: returning score=%s verdict=%s mean=%.1f",
                 result["score"], result["verdict"], mean)
    return result
