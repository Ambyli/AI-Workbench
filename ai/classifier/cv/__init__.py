"""OpenCV detectors: the registry, and the name -> detector lookup.

Every CV detection function in the service lives in this package, split by
what it answers:

    quality.py  whole-page measurements — ``check_blur``, ``check_exposure``.
                They emit no regions: a full-frame box is not an answer to
                "where".
    features.py "is X in this page?" — vegetation, sky, faces, water, text.
                Each emits ``regions`` alongside its score.
    regions.py  the two shared shapes those regions are built with.

All detectors share one return dict:
    score      : int   1-10
    verdict    : str   PASS | MARGINAL | FAIL
    confidence : int   0-100
    detail     : str   human-readable measurement
    method     : str   always "cv"
    regions    : list  optional — WHERE the detector found what it scored, in
                       WORKING-IMAGE coordinates (the <=1000-px resize, not the
                       original page). ``analysis.cv_eval._run_cv_criterion``
                       divides by that page's ``working_scale`` and stamps the
                       page index, which is what puts them in the original
                       space every stored Region lives in.

Adding a new detector
---------------------
1. Write a function in ``features.py``: def detect_X(image) -> dict
2. Emit ``regions`` when the detector knows where it looked; omit the key
   entirely when it does not.
3. Add it to REGISTRY below with one or more lowercase criterion name strings.
4. Rebuild the container — no other changes needed.

Process flow position: ``get_detector`` is called for each type="cv" criterion
in step 4 of ``analysis.pipeline.analyze_document``; the detector then runs on
EVERY page image of the document and the worst page's result is the one
reported. A document with no page images (.txt / .docx) skips cv criteria
entirely. ``REGISTRY`` is also read by ``api.introspection`` for
GET /cv-detectors.
"""

import difflib
from typing import Callable

from config import CV_NAME_FUZZY_CUTOFF
from cv.features import (
    detect_faces,
    detect_sky,
    detect_text,
    detect_vegetation,
    detect_water,
)
from cv.quality import check_blur, check_exposure
from logger import logger

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
      2. Fuzzy match via difflib at CV_NAME_FUZZY_CUTOFF (0.8) — typos and
         small variants only. The cutoff is deliberately high: at 0.6
         "has solar panels" resolved to the vegetation detector and
         "has meter" to the water detector, so an unrelated `cv` criterion
         was scored by the wrong detector with no warning.
      3. None — caller falls back to the detector service / LLM with a warning.

    Args:
        criterion_name: The criterion name as supplied by the caller.

    Returns:
        A detector callable, or None if no match is found.
    """
    lower = criterion_name.lower().strip()

    if lower in REGISTRY:
        logger.debug("get_detector: exact match '%s'", lower)
        return REGISTRY[lower]

    close = difflib.get_close_matches(
        lower, REGISTRY.keys(), n=1, cutoff=CV_NAME_FUZZY_CUTOFF
    )
    if close:
        logger.debug("get_detector: fuzzy match '%s' -> '%s'", lower, close[0])
        return REGISTRY[close[0]]

    logger.debug("get_detector: no detector found for '%s'", criterion_name)
    return None
