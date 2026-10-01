"""The two region shapes every OpenCV detector builds its geometry with.

The detectors already compute masks and boxes to produce their scores; these
turn that work into the plain dicts the rest of the service understands,
instead of throwing it away::

    {"kind": "box" | "polygon",
     "points": [[x, y], ...],     # 2 corners for a box, >=3 vertices otherwise
     "score": float | None,       # coverage fraction, block count, ...
     "attrs": {...}}              # free-form detail

Coordinates are in the WORKING image the detector was handed — the <=1000-px
resize, not the original page. ``analysis.cv_eval`` rescales them.

    _mask_regions() — a binary mask -> simplified polygons, specks dropped and
                      the list bounded.
    _box_region()   — one OpenCV ``(x, y, w, h)`` rectangle -> a box region.

Process flow position: the bottom of the cv package; imported by
``cv.features``.
"""

import cv2

from config import (
    CV_REGION_MAX_PER_DETECTOR,
    CV_REGION_MIN_AREA_FRAC,
    CV_REGION_POLY_EPSILON_FRAC,
)


def _mask_regions(mask, image_shape, *, offset_y: int = 0) -> list[dict]:
    """Turn a binary mask into simplified polygon regions.

    The detectors already build these masks to measure coverage; this walks
    the same mask's external contours, drops the specks
    (``CV_REGION_MIN_AREA_FRAC`` of the image), simplifies each with
    ``approxPolyDP`` so a hedge is a dozen vertices rather than a thousand,
    and keeps the largest ``CV_REGION_MAX_PER_DETECTOR``.

    Args:
        mask:        Single-channel binary mask, same width as the image.
        image_shape: The working image's ``(h, w, …)`` — sets the area floor.
        offset_y:    Added to every y, for a detector that masked a crop
                     (``detect_sky`` looks only at the top of the page).

    Returns:
        ``[{"kind": "polygon", "points": [[x, y], ...], "score": area_frac}]``
        in working-image coordinates, largest first.
    """
    total = float(image_shape[0] * image_shape[1]) or 1.0
    min_area = total * CV_REGION_MIN_AREA_FRAC

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    scored: list[tuple[float, dict]] = []
    for contour in contours:
        area = float(cv2.contourArea(contour))
        if area < min_area:
            continue
        epsilon = CV_REGION_POLY_EPSILON_FRAC * cv2.arcLength(contour, True)
        simplified = cv2.approxPolyDP(contour, epsilon, True)
        points = [[float(p[0][0]), float(p[0][1]) + offset_y] for p in simplified]
        if len(points) < 3:
            continue
        scored.append(
            (
                area,
                {
                    "kind": "polygon",
                    "points": points,
                    "score": round(area / total, 5),
                    "attrs": {"area_px": int(area)},
                },
            )
        )

    scored.sort(key=lambda item: -item[0])
    return [region for _, region in scored[:CV_REGION_MAX_PER_DETECTOR]]


def _box_region(x, y, w, h, *, score=None, **attrs) -> dict:
    """One ``kind="box"`` region from an OpenCV ``(x, y, w, h)`` rectangle."""
    return {
        "kind": "box",
        "points": [[float(x), float(y)], [float(x + w), float(y + h)]],
        "score": score,
        "attrs": attrs,
    }
