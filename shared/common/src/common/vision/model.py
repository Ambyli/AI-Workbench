"""Region and PageGeometry — the two values every localisation path produces.

A ``Region`` says *where* something is on a page; a ``PageGeometry`` says what
"where" means for that page (its original pixel size, the scale factor of the
working image detectors ran on, and — for PDFs — the page's size in points).

Both are plain dataclasses with ``as_dict`` / ``from_dict`` round-trips, so a
region survives a trip through ``regions.json``, a job result, or an HTTP
response without a serialiser of its own.

Coordinate contract (the one rule that matters):

    Regions are ALWAYS stored in original page pixel space.

Detectors and vision models work on a downscaled ≤1000-px working image, so
every producer divides its coordinates by ``PageGeometry.working_scale`` before
building a Region — see ``common.vision.geometry``. PDF text hits additionally
carry ``attrs["pdf_rect"]`` in points so PDF tooling can use them directly.

Process flow position: constructed by detectors / text matchers, rendered by
``common.vision.render``, persisted by ``common.vision.store``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional, Sequence

# A box is stored as its two opposite corners; a polygon as ≥3 vertices. Two
# kinds rather than one keeps the SVG/PNG renderers honest about which SVG
# element to emit, and keeps a box's JSON small.
RegionKind = Literal["box", "polygon"]

# Where a region came from. The renderer maps each source to a stroke style
# (see ``common.vision.palette``) so a layer stays readable in one colour per
# criterion: solid = cv/detector/pdf-text, dashed = ocr, dotted = llm,
# double = diff.
RegionSource = Literal["cv", "ocr", "pdf-text", "llm", "detector", "diff"]

REGION_SOURCES: tuple[str, ...] = ("cv", "ocr", "pdf-text", "llm", "detector", "diff")


def _as_points(points: Sequence[Any]) -> list[tuple[float, float]]:
    """Normalise any ``[(x, y), ...]``-ish input into a list of float pairs.

    Accepts tuples, lists, or numpy rows — anything with two indexable
    elements — so a detector can hand over ``cv2.boundingRect`` output or a
    numpy contour without converting first.
    """
    out: list[tuple[float, float]] = []
    for point in points:
        x, y = point[0], point[1]
        out.append((float(x), float(y)))
    return out


@dataclass
class Region:
    """One localised finding on one page, in original page pixel space.

    Attributes:
        page:   Zero-based page index the region belongs to.
        kind:   ``"box"`` (two corner points) or ``"polygon"`` (≥3 vertices).
        points: ``[(x, y), ...]`` in original page pixels.
        label:  Criterion name, detector label, or matched text — whatever the
                producer wants shown in a tooltip and used for the colour hash.
        score:  Producer confidence (detector score, OCR confidence, fuzzy
                ratio, …) or None when the path has no notion of one.
        source: Which path produced it — see ``RegionSource``.
        attrs:  Free-form extras: ``text`` snippet, ``pdf_rect`` in points,
                LLM ``attempt`` / ``accepted`` flags, ``change`` for diffs.
    """

    page: int
    kind: RegionKind
    points: list[tuple[float, float]]
    label: str
    score: Optional[float] = None
    source: RegionSource = "cv"
    attrs: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.points = _as_points(self.points)

    # ── Geometry helpers ──────────────────────────────────────────────────
    def bounds(self) -> tuple[float, float, float, float]:
        """Axis-aligned bounding box as ``(x1, y1, x2, y2)``.

        For a box this is the box itself (corners normalised so x1 ≤ x2); for
        a polygon it is the extent of its vertices. Used for the PNG/preview
        renderers' label placement and for area checks.
        """
        xs = [p[0] for p in self.points] or [0.0]
        ys = [p[1] for p in self.points] or [0.0]
        return (min(xs), min(ys), max(xs), max(ys))

    def area(self) -> float:
        """Shoelace area for a polygon, rectangle area for a box.

        Always non-negative; a degenerate region (fewer than 3 polygon points)
        falls back to its bounding-box area.
        """
        if self.kind == "box" or len(self.points) < 3:
            x1, y1, x2, y2 = self.bounds()
            return max(0.0, x2 - x1) * max(0.0, y2 - y1)
        total = 0.0
        n = len(self.points)
        for i in range(n):
            x1, y1 = self.points[i]
            x2, y2 = self.points[(i + 1) % n]
            total += x1 * y2 - x2 * y1
        return abs(total) / 2.0

    # ── Serialisation ─────────────────────────────────────────────────────
    def as_dict(self) -> dict[str, Any]:
        """JSON-safe view. Coordinates are rounded to 2 dp — sub-pixel
        precision is noise on a photo and doubles the file size."""
        return {
            "page": self.page,
            "kind": self.kind,
            "points": [[round(x, 2), round(y, 2)] for x, y in self.points],
            "label": self.label,
            "score": self.score,
            "source": self.source,
            "attrs": dict(self.attrs),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Region":
        """Rebuild a Region from ``as_dict`` output (or ``regions.json``)."""
        return cls(
            page=int(data.get("page", 0)),
            kind=data.get("kind", "box"),
            points=_as_points(data.get("points", [])),
            label=str(data.get("label", "")),
            score=data.get("score"),
            source=data.get("source", "cv"),
            attrs=dict(data.get("attrs") or {}),
        )


@dataclass
class PageGeometry:
    """The coordinate frame one page's regions live in.

    Attributes:
        width/height:  ORIGINAL page pixel size — after EXIF transpose for a
                       photo, after the raster render for a PDF page.
        working_scale: original → working-image factor (≤ 1.0). Detectors run
                       on the working image; dividing their output by this
                       lands in original space.
        pdf_points:    ``(width_pt, height_pt)`` for a PDF page, else None.
                       Lets a consumer convert a pixel region back to the
                       coordinate system the PDF itself uses.
    """

    page: int
    width: int
    height: int
    working_scale: float = 1.0
    pdf_points: Optional[tuple[float, float]] = None

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe view, used verbatim in ``page_geometry`` and the manifest."""
        return {
            "page": self.page,
            "width": self.width,
            "height": self.height,
            "working_scale": round(self.working_scale, 6),
            "pdf_points": list(self.pdf_points) if self.pdf_points else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PageGeometry":
        """Rebuild a PageGeometry from ``as_dict`` output."""
        pts = data.get("pdf_points")
        return cls(
            page=int(data.get("page", 0)),
            width=int(data.get("width", 0)),
            height=int(data.get("height", 0)),
            working_scale=float(data.get("working_scale", 1.0) or 1.0),
            pdf_points=(float(pts[0]), float(pts[1])) if pts else None,
        )
