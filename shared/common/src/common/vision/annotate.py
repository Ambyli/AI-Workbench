"""Draw regions onto a page image, with a readable label on each one.

``common.vision.render`` already burns regions into a preview — but that
preview is the *service's* answer, rendered by the same code that produced the
regions. Checking whether a job's geometry is right therefore needs a second
opinion: the same regions, drawn independently from ``regions.json``, onto the
original fixture. When the two pictures disagree, one of them is wrong, and
that is exactly the signal a hand-review tool exists to surface.

So this module is deliberately NOT a variant of ``render_preview``:

  * **Every region carries a caption**, not just a hover tooltip. A reviewer
    reading a printed page cannot hover, and "which of these four boxes is the
    one that scored 3?" is the whole question.
  * **Labels are supplied by the caller**, because the interesting parts of a
    caption — the score, the verdict, the attempt number, the verify result —
    live in the job result, not in the region. ``default_label`` covers the
    boring case.
  * **A rejected LLM attempt is drawn differently from an accepted one**, so a
    picture of the enforcement loop reads as a loop rather than as four
    equally-weighted claims.

Colours come from ``common.vision.palette`` (one hue per criterion, stroke
style per source), so an annotation drawn here and the service's own SVG can be
laid side by side and compared by eye without a legend lookup.

Dependencies: Pillow, imported at call time, exactly as ``render`` does — a
consumer that only wants regions and SVG pays nothing for it.

Process flow position: a leaf. Nothing in ``common.vision`` imports it; it is
called by review tooling (``unit-tests/classifier/regions_report.py``) and is
available to any service that wants a captioned overlay.
"""

from __future__ import annotations

import io
from typing import Any, Callable, Iterable, Optional, Sequence, Union

from .model import PageGeometry, Region
from .palette import criterion_color, stroke_for

# Fill opacity under each region. Lower than ``render``'s 0.16: a captioned
# overlay stacks a text chip on top of the fill, and two translucent layers
# over document text is one too many.
FILL_OPACITY = 0.12

# Stroke width as a fraction of the page's short side, floored at 2 px — the
# same policy as ``render.stroke_width`` so the two overlays line up visually.
STROKE_FRACTION = 0.0030
MIN_STROKE = 2.0

# Caption size as a fraction of the short side, clamped. Small enough that a
# page with twenty regions is still a page, large enough to read at 100%.
LABEL_FRACTION = 0.016
MIN_LABEL_PX = 11
MAX_LABEL_PX = 28

# Dash pattern (on, off), in multiples of the base stroke, used for a region
# the producer itself rejected — see ``is_rejected``.
REJECTED_DASH = (3.0, 3.0)

# Captions are drawn with Pillow's default face, which has no glyph for the
# symbols a review caption naturally reaches for — and a ``.notdef`` box in a
# caption reads as a rendering bug rather than as a mark, which is exactly the
# wrong signal in a picture whose job is to be trusted. Hunting for a TrueType
# face that has them would make legibility depend on what a machine happens to
# ship, so they are transliterated instead. ``·`` (U+00B7) is deliberately
# absent: the default face does have it.
GLYPH_FALLBACKS = {
    "✗": "x",       # ✗  a rejected attempt
    "✘": "x",       # ✘
    "✓": "ok",      # ✓
    "✔": "ok",      # ✔
    "→": "->",      # →
    "—": "-",       # —
    "–": "-",       # –
    "…": "...",     # …
    "“": '"',       # “
    "”": '"',       # ”
    "’": "'",       # ’
}

# What a caller can pass as ``labels``: one string per region, a function of
# the region, or nothing.
Labels = Union[Sequence[str], Callable[[Region], str], None]


def default_label(region: Region) -> str:
    """The caption used when the caller supplies none: ``"<label> · <source>"``.

    Deliberately thin. Anything richer — a score, a verdict, an attempt
    number — is in the job result rather than the region, so a caller that has
    the result should build its own caption and pass it in.
    """
    bits = [region.label, region.source]
    if region.score is not None:
        bits.insert(1, f"{region.score}")
    return " · ".join(str(b) for b in bits)


def safe_text(text: str) -> str:
    """Replace glyphs the default caption font cannot draw — see
    :data:`GLYPH_FALLBACKS`. Applied to every caption before it is measured,
    so the chip is sized around what is actually drawn."""
    for glyph, replacement in GLYPH_FALLBACKS.items():
        if glyph in text:
            text = text.replace(glyph, replacement)
    return text


def is_rejected(region: Region) -> bool:
    """True when the producer recorded this region as one it threw away.

    Today that is only the LLM enforcement loop's ``attrs.accepted: False``.
    It is a function rather than an inline check so a new producer with the
    same notion has one place to join.
    """
    return region.attrs.get("accepted") is False


def stroke_width(geometry: PageGeometry) -> float:
    """Base stroke width in page pixels for this geometry."""
    short_side = min(geometry.width or 1, geometry.height or 1)
    return max(MIN_STROKE, short_side * STROKE_FRACTION)


def geometry_for_image(image: Any, page: int = 0) -> PageGeometry:
    """A ``PageGeometry`` describing ``image`` itself, at ``working_scale`` 1.

    For the common review case where the picture being drawn on IS the
    original page, so there is no scale to carry.
    """
    img = _as_rgb_image(image)
    return PageGeometry(page=page, width=img.width, height=img.height)


def draw_regions(
    image: Any,
    regions: Sequence[Region],
    *,
    geometry: Optional[PageGeometry] = None,
    labels: Labels = None,
    label_regions: bool = True,
    font_size: Optional[int] = None,
    stroke_scale: float = 1.0,
) -> Any:
    """Return a new RGB image with ``regions`` drawn and captioned on it.

    The input image is never modified — a reviewer comparing an annotated page
    against a clean one needs both, and a caller that passed a ``PIL.Image``
    it still holds must not find it painted over.

    Args:
        image:    The page to draw on: encoded bytes, a ``PIL.Image``, or an
                  ``H×W×3`` uint8 numpy array in **RGB** order (OpenCV callers
                  hold BGR and must flip first — this cannot tell them apart).
        regions:  Regions in ORIGINAL page pixels. Regions whose ``page`` is
                  not ``geometry.page`` are ignored rather than mis-drawn.
        geometry: The frame ``regions`` are expressed in. Defaults to the
                  image's own size at page 0. When it disagrees with the
                  image, the IMAGE is resized to it — a page rendered at a
                  different DPI than the service used is the usual cause, and
                  moving the picture is safer than moving the geometry.
        labels:   One caption per entry of ``regions`` (same order and length),
                  or a callable taking a Region, or None for
                  :func:`default_label`.
        label_regions: Draw the captions at all. False gives a bare overlay.
        font_size: Caption size in pixels. Defaults to a fraction of the
                  page's short side.
        stroke_scale: Multiplier on every outline width, for a page whose
                  regions are dense enough that the default swamps it.

    Returns:
        A ``PIL.Image.Image`` in RGB mode.

    Raises:
        ValueError: If ``labels`` is a sequence whose length does not match
            ``regions`` — a silently mis-aligned caption is worse than no
            caption at all, because it reads as evidence.
    """
    from PIL import Image, ImageDraw

    page = _as_rgb_image(image)
    if geometry is None:
        geometry = PageGeometry(page=0, width=page.width, height=page.height)
    if geometry.width and geometry.height and page.size != (geometry.width, geometry.height):
        page = page.resize((geometry.width, geometry.height), Image.LANCZOS)

    captions = _resolve_labels(regions, labels)
    drawable = [
        (region, caption)
        for region, caption in zip(regions, captions)
        if region.page == geometry.page
    ]

    base = stroke_width(geometry) * max(0.1, stroke_scale)
    overlay = Image.new("RGBA", page.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay, "RGBA")

    for region, _caption in drawable:
        _draw_one(draw, region, base)

    composed = Image.alpha_composite(page.convert("RGBA"), overlay).convert("RGB")

    if label_regions and drawable:
        text_layer = Image.new("RGBA", composed.size, (0, 0, 0, 0))
        text_draw = ImageDraw.Draw(text_layer, "RGBA")
        font = _load_font(font_size or _label_px(geometry))
        taken: list[tuple[float, float, float, float]] = []
        for region, caption in drawable:
            if caption:
                _draw_caption(text_draw, region, caption, font, geometry, taken)
        composed = Image.alpha_composite(
            composed.convert("RGBA"), text_layer
        ).convert("RGB")

    return composed


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _resolve_labels(regions: Sequence[Region], labels: Labels) -> list[str]:
    """Normalise ``labels`` into one string per region."""
    if labels is None:
        return [default_label(r) for r in regions]
    if callable(labels):
        return [str(labels(r)) for r in regions]
    captions = list(labels)
    if len(captions) != len(regions):
        raise ValueError(
            f"labels has {len(captions)} entries but regions has {len(regions)}; "
            "pass one caption per region, a callable, or None"
        )
    return [str(c) for c in captions]


def _as_rgb_image(image: Any) -> Any:
    """Coerce bytes / PIL.Image / RGB ndarray into an RGB ``PIL.Image``."""
    from PIL import Image

    if isinstance(image, (bytes, bytearray, memoryview)):
        img = Image.open(io.BytesIO(bytes(image)))
        img.load()
        return img.convert("RGB")
    if hasattr(image, "convert"):
        return image.convert("RGB")
    return Image.fromarray(image).convert("RGB")


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    value = color.lstrip("#")
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


def _label_px(geometry: PageGeometry) -> int:
    short_side = min(geometry.width or 1, geometry.height or 1)
    return int(min(MAX_LABEL_PX, max(MIN_LABEL_PX, short_side * LABEL_FRACTION)))


def _load_font(size: int) -> Any:
    """Pillow's default bitmap font at ``size``, with a pre-10.1 fallback.

    No TrueType lookup on purpose: which faces a machine happens to ship is
    not something a review picture's legibility should depend on.
    """
    from PIL import ImageFont

    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1 — fixed-size bitmap default
        return ImageFont.load_default()


def _outline_points(region: Region) -> list[tuple[float, float]]:
    """The region as a closed point list, expanding a box into four corners."""
    if region.kind == "box" and len(region.points) >= 2:
        (x1, y1), (x2, y2) = region.points[0], region.points[1]
        return [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
    return list(region.points)


def _dash(points: Sequence[tuple[float, float]], on: float, off: float):
    """Walk a closed polyline and yield its "pen down" segments.

    Pillow has no dash support, so the pattern is walked by arc length. Same
    approach as ``render._dashed_segments``; kept local because this module's
    dash is a review convention (rejected vs accepted), not the source-derived
    one the renderer draws.
    """
    if len(points) < 2 or on <= 0 or off <= 0:
        return
    closed = list(points) + [points[0]]
    phase = 0.0
    pen_down = True
    for i in range(len(closed) - 1):
        (x1, y1), (x2, y2) = closed[i], closed[i + 1]
        length = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
        if length <= 0:
            continue
        travelled = 0.0
        while travelled < length:
            step = min((on if pen_down else off) - phase, length - travelled)
            if pen_down and step > 0:
                t0 = travelled / length
                t1 = (travelled + step) / length
                yield (
                    (x1 + (x2 - x1) * t0, y1 + (y2 - y1) * t0),
                    (x1 + (x2 - x1) * t1, y1 + (y2 - y1) * t1),
                )
            travelled += step
            phase += step
            if phase >= (on if pen_down else off) - 1e-9:
                pen_down = not pen_down
                phase = 0.0


def _draw_one(draw: Any, region: Region, base: float) -> None:
    """Paint one region's fill and outline onto an RGBA ImageDraw."""
    points = _outline_points(region)
    if len(points) < 2:
        return

    colour = _hex_to_rgb(criterion_color(region.label))
    stroke = stroke_for(region.source)
    rejected = is_rejected(region)
    width = max(1, int(round(base * stroke["width"] * (0.6 if rejected else 1.0))))

    if len(points) >= 3 and not rejected:
        draw.polygon(points, fill=colour + (int(255 * FILL_OPACITY),))

    dash = REJECTED_DASH if rejected else stroke["dash"]
    if dash:
        on, off = dash[0] * base, dash[1] * base
        for start, end in _dash(points, on, off):
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


def _draw_caption(
    draw: Any,
    region: Region,
    text: str,
    font: Any,
    geometry: PageGeometry,
    taken: list[tuple[float, float, float, float]],
) -> None:
    """Draw ``text`` in a filled chip anchored to the region's top-left.

    Two placement rules, both there because the alternative produces an
    unreadable picture rather than a wrong one:

      * a chip that would fall off the page is pulled back inside it;
      * a chip that would land on one already drawn is nudged downwards until
        it does not, up to a handful of tries — overlapping captions on a page
        with a dozen regions is the failure mode this replaces.
    """
    colour = _hex_to_rgb(criterion_color(region.label))
    x1, y1, x2, _y2 = region.bounds()

    text = safe_text(text)
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    tw, th = right - left, bottom - top
    pad = max(2, int(th * 0.25))
    cw, ch = tw + pad * 2, th + pad * 2

    cx = min(max(0.0, x1), max(0.0, geometry.width - cw))
    cy = y1 - ch - pad
    if cy < 0:
        cy = min(y1 + pad, max(0.0, geometry.height - ch))

    for _ in range(12):
        box = (cx, cy, cx + cw, cy + ch)
        if not any(_overlaps(box, other) for other in taken):
            break
        cy += ch + pad
        if cy + ch > geometry.height:
            cy = max(0.0, y1 - ch - pad)
            cx = min(cx + cw * 0.35, max(0.0, geometry.width - cw))

    box = (cx, cy, cx + cw, cy + ch)
    taken.append(box)

    draw.rectangle(list(box), fill=colour + (232,))
    draw.text((cx + pad - left, cy + pad - top), text, fill=_ink_for(colour), font=font)

    # A short leader from the chip to the region, so a nudged caption still
    # says which box it belongs to.
    anchor_x = min(max(cx + cw / 2.0, x1), x2)
    draw.line(
        [(anchor_x, cy + ch), (anchor_x, max(y1, cy + ch))],
        fill=colour + (255,),
        width=2,
    )


def _overlaps(
    a: tuple[float, float, float, float], b: tuple[float, float, float, float]
) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def _ink_for(colour: tuple[int, int, int]) -> tuple[int, int, int, int]:
    """Black or white caption text, whichever reads on ``colour``.

    ITU-R BT.601 luma, which is what every "is this swatch light or dark"
    heuristic uses and is close enough for a 12-px chip.
    """
    luma = 0.299 * colour[0] + 0.587 * colour[1] + 0.114 * colour[2]
    return (17, 17, 17, 255) if luma > 150 else (255, 255, 255, 255)


def annotate_to_jpeg(
    image: Any,
    regions: Sequence[Region],
    *,
    quality: int = 88,
    **kwargs: Any,
) -> bytes:
    """:func:`draw_regions`, encoded as JPEG bytes — the usual sink.

    Keyword arguments are forwarded to :func:`draw_regions`.
    """
    annotated = draw_regions(image, regions, **kwargs)
    buf = io.BytesIO()
    annotated.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def regions_from_json(
    data: Iterable[dict], *, page: Optional[int] = None
) -> list[Region]:
    """``regions.json`` region dicts → ``Region`` objects, optionally one page.

    A convenience so a consumer reading the artifact file does not have to
    import ``Region`` itself just to call :func:`draw_regions`.
    """
    out = [Region.from_dict(item) for item in data]
    return out if page is None else [r for r in out if r.page == page]
