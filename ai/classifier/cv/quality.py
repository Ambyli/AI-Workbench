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

from config import BLUR_THRESHOLD, EXPOSURE_HIGH, EXPOSURE_LOW
from logger import logger


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
        Standard CV result dict.
    """
    logger.debug("check_blur: image shape=%s", image.shape)

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
    variance = float(cv2.Laplacian(gray, cv2.CV_64F).var())

    result = {
        "criterion": "sharpness",
        "score": min(10, int(10 * min(variance / (BLUR_THRESHOLD * 3), 1.0))),
        "verdict": "PASS" if variance >= BLUR_THRESHOLD else "FAIL",
        "confidence": 100,
        "detail": f"Laplacian variance: {variance:.1f} (threshold: {BLUR_THRESHOLD})",
        "method": "cv",
    }
    logger.debug("check_blur: returning score=%s verdict=%s variance=%.1f",
                 result["score"], result["verdict"], variance)
    return result


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
        Standard CV result dict.
    """
    logger.debug("check_exposure: image shape=%s", image.shape)

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
    mean = float(np.mean(gray))

    if mean < EXPOSURE_LOW:
        result = {"criterion": "exposure", "score": 2, "verdict": "FAIL", "confidence": 100,
                  "detail": f"Underexposed (mean: {mean:.1f}, min: {EXPOSURE_LOW})",
                  "method": "cv"}
    elif mean > EXPOSURE_HIGH:
        result = {"criterion": "exposure", "score": 2, "verdict": "FAIL", "confidence": 100,
                  "detail": f"Overexposed (mean: {mean:.1f}, max: {EXPOSURE_HIGH})",
                  "method": "cv"}
    else:
        score = int(1 + 9 * (mean - EXPOSURE_LOW) / (EXPOSURE_HIGH - EXPOSURE_LOW))
        result = {"criterion": "exposure", "score": score, "verdict": "PASS", "confidence": 100,
                  "detail": f"Normal exposure (mean: {mean:.1f})",
                  "method": "cv"}

    logger.debug("check_exposure: returning score=%s verdict=%s mean=%.1f",
                 result["score"], result["verdict"], mean)
    return result
