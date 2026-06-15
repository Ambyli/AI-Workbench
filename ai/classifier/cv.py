"""All OpenCV-based detectors for the classifier service.

This module is the single home for every CV detection function, from the
always-run system checks (sharpness, exposure) to the opt-in feature
detectors used with the `cv_feature` criterion type.

System checks (always run in analyze_bgr, regardless of criteria)
------------------------------------------------------------------
    check_blur()       — Laplacian variance → sharpness score
    check_exposure()   — Mean pixel intensity → exposure score

Feature detectors (run only when a criterion has type="cv_feature")
--------------------------------------------------------------------
    detect_vegetation  — HSV green masking
    detect_sky         — upper-region blue/grey analysis
    detect_faces       — OpenCV Haar cascade (frontal faces)
    detect_water       — blue/teal hue + flat-texture validation
    detect_text        — Sobel edge-density per block

All functions share the same return dict shape:
    score      : int   1-10
    verdict    : str   PASS | MARGINAL | FAIL
    confidence : int   0-100
    detail     : str   human-readable measurement
    method     : str   always "cv"

Adding a new detector
---------------------
1. Write a function: def detect_X(image) -> dict
2. Add it to REGISTRY with one or more lowercase criterion name strings.
3. Rebuild the container — no other changes needed.

Process flow position: imported by analysis.py.
    check_blur / check_exposure  → called unconditionally inside analyze_bgr()
    get_detector()               → called for each cv_feature criterion
"""

import difflib
from typing import Callable

import cv2
import numpy as np

from config import BLUR_THRESHOLD, EXPOSURE_LOW, EXPOSURE_HIGH
from logger import logger


# ---------------------------------------------------------------------------
# System checks (always-run, not tied to any specific criterion)
# ---------------------------------------------------------------------------

def check_blur(image) -> dict:
    """Measure image sharpness using the Laplacian operator.

    The Laplacian highlights rapid intensity changes (edges).  A sharp image
    has high variance in its Laplacian response; a blurry image has low
    variance because edges are smoothed out.

    Scoring: variance is linearly mapped to 1-10, capped at 10.
    FAIL threshold: BLUR_THRESHOLD (100.0 by default, set in config.py).
    Confidence is always 100 — this is a deterministic measurement.

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


# ---------------------------------------------------------------------------
# Feature detectors (opt-in via type="cv_feature")
# ---------------------------------------------------------------------------

def detect_vegetation(image) -> dict:
    """Detect green vegetation (trees, grass, shrubs) via HSV color masking.

    Converts to HSV and masks the typical green hue range (H 35-85).
    The coverage ratio of green pixels drives the score.

    Reliable for outdoor daylight images; may under-detect in poor lighting
    or over-detect artificial green objects (painted surfaces, signs).

    Score mapping:
        >15% green coverage → PASS  (score 7-10, scaled by coverage)
         5-15%              → MARGINAL (score 4-6)
        <5%                → FAIL  (score 1-3)
    """
    logger.debug("detect_vegetation: image shape=%s", image.shape)

    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    lower_green = np.array([35, 40, 40])
    upper_green = np.array([85, 255, 255])
    mask = cv2.inRange(hsv, lower_green, upper_green)

    kernel = np.ones((5, 5), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)

    total_pixels = image.shape[0] * image.shape[1]
    green_pixels = cv2.countNonZero(mask)
    ratio = green_pixels / total_pixels

    if ratio > 0.15:
        score = min(10, int(7 + (ratio - 0.15) / 0.05))
        verdict, confidence = "PASS", min(95, int(60 + ratio * 150))
    elif ratio >= 0.05:
        score = int(4 + (ratio - 0.05) / 0.01)
        verdict, confidence = "MARGINAL", 65
    else:
        score = max(1, int(ratio / 0.05 * 3))
        verdict, confidence = "FAIL", 75

    result = {
        "score": score, "verdict": verdict, "confidence": confidence,
        "detail": f"Green coverage: {ratio:.1%} of image ({green_pixels:,} px)",
        "method": "cv",
    }
    logger.debug("detect_vegetation: returning score=%s verdict=%s ratio=%.3f",
                 score, verdict, ratio)
    return result


def detect_sky(image) -> dict:
    """Detect sky in the upper portion of the image via blue/grey HSV analysis.

    Analyses the top 35% of the image for sky-like hues:
      - Clear blue sky:  H 100-130, moderate-high S, high V
      - Overcast/cloudy: any H, low S (<60), high V (>150)

    Score mapping:
        >60% sky coverage in upper region → PASS  (score 7-10)
        30-60%                            → MARGINAL (score 4-6)
        <30%                              → FAIL  (score 1-3)
    """
    logger.debug("detect_sky: image shape=%s", image.shape)

    h, w = image.shape[:2]
    sky_region = image[:int(h * 0.35), :]
    hsv = cv2.cvtColor(sky_region, cv2.COLOR_BGR2HSV)

    blue_mask = cv2.inRange(hsv, np.array([100, 30, 100]), np.array([130, 200, 255]))
    grey_mask  = cv2.inRange(hsv, np.array([0, 0, 150]),   np.array([179, 60, 255]))
    sky_mask   = cv2.bitwise_or(blue_mask, grey_mask)

    total = sky_region.shape[0] * sky_region.shape[1]
    sky_pixels = cv2.countNonZero(sky_mask)
    ratio = sky_pixels / total

    if ratio > 0.60:
        score = min(10, int(7 + (ratio - 0.60) / 0.10))
        verdict, confidence = "PASS", min(90, int(70 + ratio * 20))
    elif ratio >= 0.30:
        score = int(4 + (ratio - 0.30) / 0.10)
        verdict, confidence = "MARGINAL", 65
    else:
        score = max(1, int(ratio / 0.30 * 3))
        verdict, confidence = "FAIL", 70

    result = {
        "score": score, "verdict": verdict, "confidence": confidence,
        "detail": f"Sky coverage (upper 35%): {ratio:.1%} ({sky_pixels:,} px)",
        "method": "cv",
    }
    logger.debug("detect_sky: returning score=%s verdict=%s ratio=%.3f", score, verdict, ratio)
    return result


def detect_faces(image) -> dict:
    """Detect frontal human faces using OpenCV's Haar cascade classifier.

    Uses the built-in haarcascade_frontalface_default.xml which ships with
    cv2.  No external model download is required.

    Score mapping:
        ≥1 face (minNeighbors=5, high confidence) → PASS     (score 10)
        1 face  (minNeighbors=3, lower confidence) → MARGINAL (score 5)
        0 faces                                    → FAIL     (score 1)
    """
    logger.debug("detect_faces: image shape=%s", image.shape)

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    face_cascade = cv2.CascadeClassifier(cascade_path)

    if face_cascade.empty():
        logger.error("detect_faces: Haar cascade file not found at %s", cascade_path)
        return {"score": 1, "verdict": "FAIL", "confidence": 0,
                "detail": "Haar cascade file not found — face detection unavailable",
                "method": "cv"}

    faces_high = face_cascade.detectMultiScale(
        gray, scaleFactor=1.05, minNeighbors=5, minSize=(30, 30)
    )
    n_high = len(faces_high) if not isinstance(faces_high, tuple) else 0

    if n_high >= 1:
        score, verdict, confidence = 10, "PASS", 85
        detail = f"{n_high} face(s) detected (high confidence)"
    else:
        faces_low = face_cascade.detectMultiScale(
            gray, scaleFactor=1.05, minNeighbors=3, minSize=(30, 30)
        )
        n_low = len(faces_low) if not isinstance(faces_low, tuple) else 0
        if n_low >= 1:
            score, verdict, confidence = 5, "MARGINAL", 55
            detail = f"{n_low} possible face(s) detected (lower confidence)"
        else:
            score, verdict, confidence = 1, "FAIL", 80
            detail = "No faces detected"

    result = {"score": score, "verdict": verdict, "confidence": confidence,
              "detail": detail, "method": "cv"}
    logger.debug("detect_faces: returning score=%s verdict=%s", score, verdict)
    return result


def detect_water(image) -> dict:
    """Detect water or pools via blue/teal color masking + flat-texture validation.

    A blue region qualifies as water only if its Laplacian variance is below
    200 (flat, non-textured surface), rejecting blue cars, clothing, or signs.

    Score mapping:
        >15% qualifying blue+flat coverage → PASS  (score 7-10)
        5-15%                              → MARGINAL (score 4-6)
        <5%                                → FAIL  (score 1-3)
    """
    logger.debug("detect_water: image shape=%s", image.shape)

    hsv  = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    blue_mask = cv2.inRange(hsv, np.array([90, 40, 40]), np.array([130, 255, 255]))
    contours, _ = cv2.findContours(blue_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    qualifying_pixels = 0
    for contour in contours:
        area = cv2.contourArea(contour)
        if area < 500:
            continue
        x, y, w, h = cv2.boundingRect(contour)
        roi = gray[y:y + h, x:x + w]
        if roi.size == 0:
            continue
        if cv2.Laplacian(roi, cv2.CV_64F).var() < 200:
            qualifying_pixels += area

    total_pixels = image.shape[0] * image.shape[1]
    ratio = qualifying_pixels / total_pixels

    if ratio > 0.15:
        score = min(10, int(7 + (ratio - 0.15) / 0.05))
        verdict, confidence = "PASS", min(85, int(65 + ratio * 100))
    elif ratio >= 0.05:
        score = int(4 + (ratio - 0.05) / 0.033)
        verdict, confidence = "MARGINAL", 60
    else:
        score = max(1, int(ratio / 0.05 * 3))
        verdict, confidence = "FAIL", 70

    result = {
        "score": score, "verdict": verdict, "confidence": confidence,
        "detail": f"Qualifying water coverage: {ratio:.1%} ({int(qualifying_pixels):,} px)",
        "method": "cv",
    }
    logger.debug("detect_water: returning score=%s verdict=%s ratio=%.3f", score, verdict, ratio)
    return result


def detect_text(image) -> dict:
    """Detect text regions via Sobel edge density analysis.

    Text produces characteristically high edge density in small localised
    blocks (due to letter strokes).  Divides the image into 32×32 blocks
    and counts those with >35% edge density.

    Score mapping:
        >10% of blocks high-density → PASS  (score 7-10)
        3-10%                       → MARGINAL (score 4-6)
        <3%                         → FAIL  (score 1-3)
    """
    logger.debug("detect_text: image shape=%s", image.shape)

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    sx = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3)
    sy = cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3)
    magnitude = np.sqrt(sx ** 2 + sy ** 2)
    _, edges = cv2.threshold(magnitude, 50, 255, cv2.THRESH_BINARY)

    block = 32
    h, w = edges.shape
    total_blocks, high_density_blocks = 0, 0
    for y in range(0, h, block):
        for x in range(0, w, block):
            tile = edges[y:y + block, x:x + block]
            if tile.size == 0:
                continue
            total_blocks += 1
            if np.count_nonzero(tile) / tile.size > 0.35:
                high_density_blocks += 1

    ratio = high_density_blocks / total_blocks if total_blocks > 0 else 0.0

    if ratio > 0.10:
        score = min(10, int(7 + (ratio - 0.10) / 0.05))
        verdict, confidence = "PASS", min(85, int(65 + ratio * 150))
    elif ratio >= 0.03:
        score = int(4 + (ratio - 0.03) / 0.023)
        verdict, confidence = "MARGINAL", 60
    else:
        score = max(1, int(ratio / 0.03 * 3))
        verdict, confidence = "FAIL", 75

    result = {
        "score": score, "verdict": verdict, "confidence": confidence,
        "detail": (f"High-density edge blocks: {high_density_blocks}/{total_blocks} "
                   f"({ratio:.1%})"),
        "method": "cv",
    }
    logger.debug("detect_text: returning score=%s verdict=%s ratio=%.3f", score, verdict, ratio)
    return result


# ---------------------------------------------------------------------------
# Registry and lookup
# ---------------------------------------------------------------------------

REGISTRY: dict[str, Callable] = {
    # System checks — also usable as cv_feature criteria
    "sharpness":           check_blur,
    "is sharp":            check_blur,
    "is blurry":           check_blur,
    "exposure":            check_exposure,
    "proper exposure":     check_exposure,
    "is exposed":          check_exposure,
    # Feature detectors
    "has trees":           detect_vegetation,
    "has vegetation":      detect_vegetation,
    "has greenery":        detect_vegetation,
    "has plants":          detect_vegetation,
    "has sky":             detect_sky,
    "has faces":           detect_faces,
    "has people":          detect_faces,
    "has person":          detect_faces,
    "has water":           detect_water,
    "has pool":            detect_water,
    "has swimming pool":   detect_water,
    "has text":            detect_text,
    "has text regions":    detect_text,
    "has writing":         detect_text,
}


def get_detector(criterion_name: str) -> Callable | None:
    """Return the detector function for a criterion name, or None if unregistered.

    Resolution order:
      1. Exact match (case-insensitive, stripped).
      2. Fuzzy match via difflib (cutoff 0.6).
      3. None — caller falls back to LLM with a warning.

    Args:
        criterion_name: The criterion name as supplied by the caller.

    Returns:
        A detector callable, or None if no match is found.
    """
    lower = criterion_name.lower().strip()

    if lower in REGISTRY:
        logger.debug("get_detector: exact match '%s'", lower)
        return REGISTRY[lower]

    close = difflib.get_close_matches(lower, REGISTRY.keys(), n=1, cutoff=0.6)
    if close:
        logger.debug("get_detector: fuzzy match '%s' -> '%s'", lower, close[0])
        return REGISTRY[close[0]]

    logger.debug("get_detector: no detector found for '%s'", criterion_name)
    return None
