"""Render a list of regions into the three layer formats.

    render_svg         → ``str``   an overlay whose viewBox is the original
                                   page, one ``<g>`` per criterion.
    render_png_layer   → ``bytes`` a transparent RGBA PNG the size of the
                                   page — the literal "Photoshop layer".
    render_preview     → ``bytes`` the original page with the layer burned
                                   in, JPEG, for chat and Postman.

Design choices worth knowing before editing:

  * **SVG is built with ``xml.etree.ElementTree``, never string concatenation.**
    Criterion names are caller-supplied text that ends up in ids, attributes,
    and ``<title>`` tooltips; letting the XML writer do the escaping is the
    only way a name containing ``&`` or ``<`` cannot produce a broken file.
  * **The raster renderers use Pillow, not OpenCV.** ``shared/common`` must
    stay installable without an OpenCV wheel (``common[documents]`` already
    pulls Pillow), and ImageDraw handles the RGBA compositing and polygon
    fills the layers need.
  * **Only the SVG carries a legend.** The PNG is meant to be dropped onto the
    original image in another tool, where a legend baked into the pixels is
    in the way; the preview draws one because it is the format a human looks
    at directly.
  * Coordinates arrive in ORIGINAL page pixels (``common.vision.model``), so
    every renderer's canvas is ``geometry.width × geometry.height`` and no
    scaling happens here.

Process flow position: called at job time to write the stored layers, and
again by the artifact file endpoint when a caller asks for a filtered view.
"""

from __future__ import annotations

import io
import math
from typing import Any, Iterable, Optional, Sequence
from xml.etree import ElementTree as ET

from .model import PageGeometry, Region
from .palette import criterion_color, dasharray, legend_entries, slugify_criterion, stroke_for

# Stroke width as a fraction of the page's short side, floored at 2 px. A
# fixed pixel width disappears on a 4000-px photo and swamps a 400-px thumb.
STROKE_FRACTION = 0.0035
MIN_STROKE = 2.0

# Fill opacity under each region. Low enough that the document stays readable
# through it, high enough to show which side of the outline is "inside".
FILL_OPACITY = 0.16

# Preview JPEG quality — § 4 of the regions plan.
PREVIEW_QUALITY = 85


def stroke_width(geometry: PageGeometry) -> float:
    """Base stroke width in page pixels for this geometry."""
    short_side = min(geometry.width or 1, geometry.height or 1)
    return max(MIN_STROKE, short_side * STROKE_FRACTION)


def _group_by_criterion(regions: Iterable[Region]) -> dict[str, list[Region]]:
    """Bucket regions by criterion label, preserving first-seen order.

    Order matters only for reproducibility: two renders of the same region
    list must produce byte-identical output so a cached file and a fresh one
    cannot disagree.
    """
    grouped: dict[str, list[Region]] = {}
    for region in regions:
        grouped.setdefault(region.label, []).append(region)
    return grouped


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    value = color.lstrip("#")
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


def _polygon_points(region: Region) -> list[tuple[float, float]]:
    """The region as a closed point list, expanding a box into 4 corners."""
    if region.kind == "box" and len(region.points) >= 2:
        (x1, y1), (x2, y2) = region.points[0], region.points[1]
        return [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
    return list(region.points)


# ---------------------------------------------------------------------------
# SVG
# ---------------------------------------------------------------------------


def render_svg(
    geometry: PageGeometry,
    regions: Sequence[Region],
    *,
    legend: bool = True,
    title: Optional[str] = None,
) -> str:
    """Build an SVG overlay for one page.

    The ``viewBox`` is the original page, so the result composites over the
    page image at any display size with no transform. Each criterion becomes
    one ``<g id="c-<slug>" data-criterion="<name>" data-source="…">``, which
    is what lets a client that downloaded the combined file toggle criteria on
    and off in place — layer groups inside the layer.

    Args:
        geometry: The page frame (defines the viewBox).
        regions:  Regions for THIS page, in original page pixels. Regions for
                  other pages are ignored rather than mis-drawn.
        legend:   Embed the colour/source legend in the corner.
        title:    Optional ``<title>`` for the document as a whole.

    Returns:
        A complete, standalone SVG document as a string.
    """
    page_regions = [r for r in regions if r.page == geometry.page]
    width = geometry.width or 1
    height = geometry.height or 1
    base = stroke_width(geometry)

    svg = ET.Element(
        "svg",
        {
            "xmlns": "http://www.w3.org/2000/svg",
            "viewBox": f"0 0 {width} {height}",
            "width": str(width),
            "height": str(height),
            "data-page": str(geometry.page),
            "data-region-count": str(len(page_regions)),
        },
    )
    if title:
        ET.SubElement(svg, "title").text = title

    for label, group in _group_by_criterion(page_regions).items():
        sources = sorted({r.source for r in group})
        node = ET.SubElement(
            svg,
            "g",
            {
                "id": f"c-{slugify_criterion(label)}",
                "data-criterion": label,
                "data-source": ",".join(sources),
                "data-count": str(len(group)),
                "fill": criterion_color(label),
                "stroke": criterion_color(label),
                "fill-opacity": str(FILL_OPACITY),
            },
        )
        for region in group:
            _svg_region(node, region, base)

    if legend and page_regions:
        _svg_legend(svg, page_regions, geometry, base)

    ET.indent(svg, space="  ")
    return ET.tostring(svg, encoding="unicode", xml_declaration=False)


def _svg_region(parent: ET.Element, region: Region, base: float) -> None:
    """Append one ``<rect>``/``<polygon>`` (plus its ``<title>``) to a group."""
    stroke = stroke_for(region.source)
    attrs: dict[str, str] = {
        "stroke-width": str(round(base * stroke["width"], 2)),
        "data-source": region.source,
    }
    if region.score is not None:
        attrs["data-score"] = str(region.score)
    for key in ("attempt", "accepted", "change"):
        if key in region.attrs:
            attrs[f"data-{key}"] = str(region.attrs[key]).lower()
    dash = dasharray(region.source, base)
    if dash:
        attrs["stroke-dasharray"] = dash

    if region.kind == "box" and len(region.points) >= 2:
        (x1, y1), (x2, y2) = region.points[0], region.points[1]
        attrs.update(
            {
                "x": str(round(min(x1, x2), 2)),
                "y": str(round(min(y1, y2), 2)),
                "width": str(round(abs(x2 - x1), 2)),
                "height": str(round(abs(y2 - y1), 2)),
            }
        )
        element = ET.SubElement(parent, "rect", attrs)
    else:
        attrs["points"] = " ".join(
            f"{round(x, 2)},{round(y, 2)}" for x, y in region.points
        )
        element = ET.SubElement(parent, "polygon", attrs)

    # Tooltip: what it is, where it came from, how sure the producer was.
    bits = [region.label, region.source]
    if region.score is not None:
        bits.append(f"score {region.score}")
    snippet = region.attrs.get("text")
    if snippet:
        bits.append(f"“{snippet}”")
    ET.SubElement(element, "title").text = " · ".join(str(b) for b in bits)

    if stroke["double"]:
        # A second, inset outline — the "double" stroke that marks a diff.
        inset = dict(attrs)
        inset["stroke-width"] = str(round(base * 0.5, 2))
        inset["fill"] = "none"
        inset["transform"] = _inset_transform(region, base)
        ET.SubElement(parent, element.tag, inset)


def _inset_transform(region: Region, base: float) -> str:
    """A tiny scale-about-centre used to draw the second line of a double stroke."""
    x1, y1, x2, y2 = region.bounds()
    cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    span = max(1.0, min(x2 - x1, y2 - y1))
    factor = max(0.5, 1.0 - (base * 2.5) / span)
    return (
        f"translate({round(cx * (1 - factor), 2)},{round(cy * (1 - factor), 2)}) "
        f"scale({round(factor, 4)})"
    )


def _svg_legend(
    svg: ET.Element, regions: Sequence[Region], geometry: PageGeometry, base: float
) -> None:
    """Draw the colour/source key in the top-left corner of the overlay."""
    entries = legend_entries(regions)
    font_size = max(10.0, min(geometry.width, geometry.height) * 0.018)
    pad = font_size * 0.6
    row = font_size * 1.6
    swatch = font_size * 0.9
    box_w = font_size * 18
    box_h = pad * 2 + row * len(entries)

    group = ET.SubElement(svg, "g", {"id": "legend", "font-size": str(round(font_size, 2))})
    ET.SubElement(
        group,
        "rect",
        {
            "x": str(round(pad, 2)),
            "y": str(round(pad, 2)),
            "width": str(round(box_w, 2)),
            "height": str(round(box_h, 2)),
            "fill": "#ffffff",
            "fill-opacity": "0.78",
            "stroke": "#333333",
            "stroke-width": str(round(base * 0.4, 2)),
            "rx": str(round(font_size * 0.3, 2)),
        },
    )
    y = pad * 2
    for entry in entries:
        attrs = {
            "x": str(round(pad * 2, 2)),
            "y": str(round(y, 2)),
            "width": str(round(swatch, 2)),
            "height": str(round(swatch, 2)),
            "fill": entry["color"],
            "fill-opacity": "0.85",
            "stroke": entry["color"],
            "stroke-width": str(round(base * 0.5, 2)),
        }
        if entry["dash"]:
            attrs["stroke-dasharray"] = " ".join(str(v) for v in entry["dash"])
        ET.SubElement(group, "rect", attrs)
        text = ET.SubElement(
            group,
            "text",
            {
                "x": str(round(pad * 2 + swatch * 1.6, 2)),
                "y": str(round(y + swatch * 0.85, 2)),
                "fill": "#111111",
                "fill-opacity": "1",
            },
        )
        text.text = f"{entry['label']} · {entry['source']} ({entry['count']})"
        y += row


# ---------------------------------------------------------------------------
# Raster layers
# ---------------------------------------------------------------------------


def _dashed_segments(
    points: Sequence[tuple[float, float]], on: float, off: float
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """Walk a closed polyline and return the "pen down" segments of a dash.

    Pillow has no dash support, so the pattern is walked by arc length: this
    is what keeps an OCR polygon's dashed outline reading as dashed rather
    than as a stack of independent line calls.
    """
    segments: list[tuple[tuple[float, float], tuple[float, float]]] = []
    if len(points) < 2:
        return segments
    closed = list(points) + [points[0]]
    phase = 0.0  # distance travelled inside the current on/off cycle
    drawing = True
    for i in range(len(closed) - 1):
        (x1, y1), (x2, y2) = closed[i], closed[i + 1]
        length = math.hypot(x2 - x1, y2 - y1)
        if length <= 0:
            continue
        travelled = 0.0
        while travelled < length:
            remaining = (on if drawing else off) - phase
            step = min(remaining, length - travelled)
            if drawing and step > 0:
                t0, t1 = travelled / length, (travelled + step) / length
                segments.append(
                    (
                        (x1 + (x2 - x1) * t0, y1 + (y2 - y1) * t0),
                        (x1 + (x2 - x1) * t1, y1 + (y2 - y1) * t1),
                    )
                )
            travelled += step
            phase += step
            if phase >= (on if drawing else off) - 1e-9:
                drawing = not drawing
                phase = 0.0
    return segments


def _draw_regions(draw: Any, regions: Iterable[Region], base: float) -> None:
    """Paint every region onto an ImageDraw bound to an RGBA canvas."""
    for region in regions:
        points = _polygon_points(region)
        if len(points) < 2:
            continue
        colour = _hex_to_rgb(criterion_color(region.label))
        stroke = stroke_for(region.source)
        width = max(1, int(round(base * stroke["width"])))

        if len(points) >= 3:
            draw.polygon(points, fill=colour + (int(255 * FILL_OPACITY),))

        dash = stroke["dash"]
        if dash:
            on, off = dash[0] * base, dash[1] * base
            for start, end in _dashed_segments(points, on, off):
                draw.line([start, end], fill=colour + (255,), width=width)
        else:
            draw.line(points + [points[0]], fill=colour + (255,), width=width)

        if stroke["double"]:
            x1, y1, x2, y2 = region.bounds()
            inset = base * 2.0
            if x2 - x1 > inset * 2 and y2 - y1 > inset * 2:
                draw.rectangle(
                    [x1 + inset, y1 + inset, x2 - inset, y2 - inset],
                    outline=colour + (255,),
                    width=max(1, width // 2),
                )


def render_png_layer(
    geometry: PageGeometry, regions: Sequence[Region]
) -> bytes:
    """Build a transparent RGBA PNG the exact size of the original page.

    Alpha is zero everywhere except the strokes and the translucent fills, so
    the file drops straight onto the original image as a layer in any editor.

    Args:
        geometry: The page frame — also the canvas size.
        regions:  Regions for THIS page, in original page pixels.

    Returns:
        PNG bytes. A page with no regions still returns a valid, fully
        transparent PNG rather than None — a caller asking for a layer gets
        a layer.
    """
    from PIL import Image, ImageDraw

    width = max(1, geometry.width)
    height = max(1, geometry.height)
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas, "RGBA")
    _draw_regions(
        draw, [r for r in regions if r.page == geometry.page], stroke_width(geometry)
    )

    buf = io.BytesIO()
    canvas.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _as_rgb_image(base_image: Any) -> Any:
    """Coerce whatever the caller has into an RGB ``PIL.Image``.

    Accepts encoded bytes (any format Pillow reads), a ``PIL.Image``, or an
    ``H×W×3`` uint8 numpy array in **RGB** channel order. OpenCV consumers
    hold BGR arrays and must flip (``array[:, :, ::-1]``) before calling —
    this function cannot tell the two apart, so it does not guess.
    """
    from PIL import Image

    if isinstance(base_image, (bytes, bytearray, memoryview)):
        img = Image.open(io.BytesIO(bytes(base_image)))
        img.load()
        return img.convert("RGB")
    if hasattr(base_image, "convert"):  # already a PIL Image
        return base_image.convert("RGB")
    return Image.fromarray(base_image).convert("RGB")


def render_preview(
    base_image: Any,
    geometry: PageGeometry,
    regions: Sequence[Region],
    *,
    quality: int = PREVIEW_QUALITY,
    legend: bool = True,
) -> bytes:
    """Composite the layer onto the page and return a JPEG.

    The base image is resized to the geometry when the two disagree — which
    happens when a caller passes the working image rather than the original —
    so the regions always land where they belong.

    Args:
        base_image: Encoded bytes, a ``PIL.Image``, or an RGB numpy array.
        geometry:   The page frame the regions are expressed in.
        regions:    Regions for THIS page.
        quality:    JPEG quality (default 85).
        legend:     Draw the colour/source key in the top-left corner.

    Returns:
        JPEG bytes.
    """
    from PIL import Image, ImageDraw

    page = _as_rgb_image(base_image)
    if geometry.width and geometry.height and page.size != (geometry.width, geometry.height):
        page = page.resize((geometry.width, geometry.height), Image.LANCZOS)

    page_regions = [r for r in regions if r.page == geometry.page]
    overlay = Image.new("RGBA", page.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay, "RGBA")
    base = stroke_width(geometry)
    _draw_regions(draw, page_regions, base)
    if legend and page_regions:
        _draw_legend(draw, page_regions, geometry)

    composed = Image.alpha_composite(page.convert("RGBA"), overlay).convert("RGB")
    buf = io.BytesIO()
    composed.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def _draw_legend(draw: Any, regions: Sequence[Region], geometry: PageGeometry) -> None:
    """Draw the preview's corner key with Pillow's default bitmap font.

    No TrueType lookup: the font a container happens to ship is not something
    a layer's legibility should depend on, and the default font is legible at
    the sizes used here.
    """
    from PIL import ImageFont

    entries = legend_entries(regions)
    if not entries:
        return
    size = max(11, int(min(geometry.width, geometry.height) * 0.018))
    try:
        font = ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1 — fixed-size bitmap default
        font = ImageFont.load_default()
    pad = int(size * 0.6)
    row = int(size * 1.6)
    swatch = int(size * 0.9)
    box_w = int(size * 18)
    box_h = pad * 2 + row * len(entries)

    draw.rectangle(
        [pad, pad, pad + box_w, pad + box_h], fill=(255, 255, 255, 200),
        outline=(60, 60, 60, 220), width=1,
    )
    y = pad * 2
    for entry in entries:
        colour = _hex_to_rgb(entry["color"])
        draw.rectangle(
            [pad * 2, y, pad * 2 + swatch, y + swatch],
            fill=colour + (220,), outline=colour + (255,), width=1,
        )
        draw.text(
            (pad * 2 + int(swatch * 1.6), y),
            f"{entry['label']} · {entry['source']} ({entry['count']})",
            fill=(17, 17, 17, 255),
            font=font,
        )
        y += row
