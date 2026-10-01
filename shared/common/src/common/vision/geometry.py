"""Coordinate transforms between the four spaces a region can be expressed in.

    working image   what detectors and vision models actually see — the page
                    downscaled to ≤1000 px on the long side.
    original page   where every stored Region lives (see model.py).
    PDF points      1/72 inch, the coordinate system inside a PDF file.
    normalised grid the 0–1000 square a vision model is asked to answer on.

Everything here is pure arithmetic on plain tuples — no numpy, no OpenCV — so
the transforms are importable anywhere and cheap enough to call per region.

Rounding: nothing is rounded here. ``Region.as_dict`` rounds at the
serialisation boundary; rounding mid-transform would compound across a
working → original → points chain.

Process flow position: called by every region producer before it constructs a
Region, and by the artifact endpoints when re-deriving PDF rectangles.
"""

from __future__ import annotations

from typing import Iterable, Optional, Sequence

from .model import PageGeometry, Region

Point = tuple[float, float]

# The grid a vision model is asked to place boxes on. 0–1000 rather than 0–1
# because models are markedly better at emitting integers than decimals.
DEFAULT_GRID = 1000.0


def scale_points(points: Iterable[Point], factor: float) -> list[Point]:
    """Multiply every coordinate by ``factor``."""
    return [(x * factor, y * factor) for x, y in points]


def to_original(points: Iterable[Point], working_scale: float) -> list[Point]:
    """Working-image coordinates → original page pixels.

    ``working_scale`` is the factor the page was multiplied by to produce the
    working image (≤ 1.0), so the inverse is a division. A zero or missing
    scale is treated as 1.0 rather than raising: a detector result is not
    worth losing over a geometry bookkeeping slip.
    """
    scale = working_scale if working_scale else 1.0
    return [(x / scale, y / scale) for x, y in points]


def to_working(points: Iterable[Point], working_scale: float) -> list[Point]:
    """Original page pixels → working-image coordinates (the inverse of
    :func:`to_original`). Used when a stored region has to be drawn on, or
    cropped from, the downscaled image again."""
    scale = working_scale if working_scale else 1.0
    return [(x * scale, y * scale) for x, y in points]


def pixels_to_points(
    points: Iterable[Point], geometry: PageGeometry
) -> Optional[list[Point]]:
    """Original page pixels → PDF points, or None for a non-PDF page.

    The two axes are scaled independently because a render's aspect ratio can
    differ from the page's by a pixel of rounding. PDF y-origin: PyMuPDF's
    rect coordinates already run top-down like the raster render, so no flip
    is applied here — a consumer using a bottom-up library must flip itself.
    """
    if not geometry.pdf_points or not geometry.width or not geometry.height:
        return None
    pw, ph = geometry.pdf_points
    sx = pw / geometry.width
    sy = ph / geometry.height
    return [(x * sx, y * sy) for x, y in points]


def points_to_pixels(
    points: Iterable[Point], geometry: PageGeometry
) -> Optional[list[Point]]:
    """PDF points → original page pixels (the inverse of
    :func:`pixels_to_points`). Returns None for a non-PDF page."""
    if not geometry.pdf_points or not geometry.width or not geometry.height:
        return None
    pw, ph = geometry.pdf_points
    if not pw or not ph:
        return None
    sx = geometry.width / pw
    sy = geometry.height / ph
    return [(x * sx, y * sy) for x, y in points]


def grid_to_pixels(
    bbox: Sequence[float], geometry: PageGeometry, grid: float = DEFAULT_GRID
) -> list[Point]:
    """A model's ``[x1, y1, x2, y2]`` on a 0–``grid`` square → page pixels.

    The grid is square but pages are not, so each axis is scaled by its own
    dimension — which is exactly what a model trained on "the image spans
    0–1000 in both directions" means.

    Args:
        bbox:     Four numbers, corners in grid units.
        geometry: The page the grid refers to.
        grid:     Grid span (default 1000).

    Returns:
        ``[(x1, y1), (x2, y2)]`` in original page pixels.
    """
    x1, y1, x2, y2 = (float(v) for v in bbox[:4])
    sx = geometry.width / grid
    sy = geometry.height / grid
    return [(x1 * sx, y1 * sy), (x2 * sx, y2 * sy)]


def pixels_to_grid(
    points: Sequence[Point], geometry: PageGeometry, grid: float = DEFAULT_GRID
) -> list[float]:
    """Page pixels → a model's ``[x1, y1, x2, y2]`` grid box (the inverse of
    :func:`grid_to_pixels`), taking the bounding box of ``points``."""
    xs = [p[0] for p in points] or [0.0]
    ys = [p[1] for p in points] or [0.0]
    sx = grid / geometry.width if geometry.width else 0.0
    sy = grid / geometry.height if geometry.height else 0.0
    return [min(xs) * sx, min(ys) * sy, max(xs) * sx, max(ys) * sy]


def clamp_points(
    points: Iterable[Point], width: float, height: float
) -> list[Point]:
    """Pull every coordinate inside ``0..width`` / ``0..height``.

    A Haar box at the image edge, a contour that touches the border, and an
    LLM box that overshoots all produce coordinates a hair outside the page;
    clamping keeps the SVG viewBox honest and stops PIL from silently
    wrapping a negative coordinate.
    """
    return [
        (min(max(0.0, x), float(width)), min(max(0.0, y), float(height)))
        for x, y in points
    ]


def rescale_region(
    region: Region, working_scale: float, geometry: Optional[PageGeometry] = None
) -> Region:
    """Return a copy of ``region`` moved from working space to original space.

    The common call in a detector pipeline: the detector emitted coordinates
    on the ≤1000-px working image, and the result has to be stored in original
    page pixels. When ``geometry`` is supplied the result is also clamped to
    the page and stamped with its page index.

    Args:
        region:        Region whose ``points`` are in working-image space.
        working_scale: The page's original → working factor.
        geometry:      Optional page frame for clamping + the page index.

    Returns:
        A new Region; the input is not modified.
    """
    points = to_original(region.points, working_scale)
    if geometry is not None:
        points = clamp_points(points, geometry.width, geometry.height)
    return Region(
        page=geometry.page if geometry is not None else region.page,
        kind=region.kind,
        points=points,
        label=region.label,
        score=region.score,
        source=region.source,
        attrs=dict(region.attrs),
    )


def box_region(
    bounds: Sequence[float],
    label: str,
    *,
    page: int = 0,
    score: Optional[float] = None,
    source: str = "cv",
    attrs: Optional[dict] = None,
) -> Region:
    """Build a ``kind="box"`` Region from ``(x1, y1, x2, y2)``.

    A convenience for the many producers that have a rectangle rather than a
    point list; corners are normalised so x1 ≤ x2 and y1 ≤ y2.
    """
    x1, y1, x2, y2 = (float(v) for v in bounds[:4])
    return Region(
        page=page,
        kind="box",
        points=[(min(x1, x2), min(y1, y2)), (max(x1, x2), max(y1, y2))],
        label=label,
        score=score,
        source=source,  # type: ignore[arg-type]
        attrs=dict(attrs or {}),
    )


def iou(a: Region, b: Region) -> float:
    """Intersection-over-union of two regions' bounding boxes, 0.0–1.0.

    Bounding boxes rather than exact polygons: the only consumer is the
    detector/LLM cross-check, which compares boxes anyway, and an exact
    polygon intersection would pull in a geometry dependency.
    """
    ax1, ay1, ax2, ay2 = a.bounds()
    bx1, by1, bx2, by2 = b.bounds()
    ix = max(0.0, min(ax2, bx2) - max(ax1, bx1))
    iy = max(0.0, min(ay2, by2) - max(ay1, by1))
    inter = ix * iy
    if inter <= 0:
        return 0.0
    union = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter
    return inter / union if union > 0 else 0.0
