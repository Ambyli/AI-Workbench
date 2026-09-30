"""A labelled coordinate grid drawn over an image, for a model to read from.

Asked "where is X, on a 0-1000 grid?", a vision model answers from its own
sense of where things are in the frame — and on a dense document that sense
is coarse: measured on photographed utility bills, Muse Glimmer placed the
right text line with the right x and a y that was 25-100 grid units off, on
a line 30 units tall. Give it the grid to read from — thin lines every 100
units with the numbers printed along every edge — and the same question on
the same image comes back within a few units, because locating a feature
becomes reading a ruler rather than estimating a fraction.

    draw_grid_overlay(image)  → the image with the grid burned in, RGB

The overlay is for the ASK image only. The scoring call and the verify crop
never see it, and it is never stored: it is a measuring aid, not a layer.

Pure Pillow, imported at call time like the renderers in ``render.py``. The
labels use Pillow's bundled scalable default font (``load_default(size=…)``,
Pillow ≥ 10.1) so a container without system fonts still gets legible numbers;
on an older Pillow the bitmap default is used, small but readable at the
≤1000-px working size this exists for.
"""

from __future__ import annotations

from typing import Any, Optional

DEFAULT_GRID_STEP = 100
_LINE_COLOUR = (200, 0, 0)


def draw_grid_overlay(
    image: Any,
    *,
    grid: float = 1000.0,
    step: int = DEFAULT_GRID_STEP,
    colour: tuple[int, int, int] = _LINE_COLOUR,
    line_alpha: int = 110,
    label_alpha: int = 220,
    font_size: Optional[int] = None,
) -> Any:
    """Return ``image`` with a labelled ``grid``-unit coordinate grid drawn on.

    Lines every ``step`` grid units on both axes, translucent so the content
    under them stays readable. Every interior line carries its coordinate at
    both ends: x along the top and bottom edges, y along the left and right —
    on a white tab so a number never disappears into a dark photo.

    Args:
        image:       A ``PIL.Image.Image`` or an ``(H, W, 3)`` numpy array in
                     **RGB** order (a BGR array must be reversed first — the
                     grid is drawn on whatever it is given).
        grid:        The span the coordinates run over (the model is asked
                     for boxes on ``0..grid``).
        step:        Spacing of the lines, in grid units.
        colour:      Line and label colour.
        line_alpha:  0-255 opacity of the lines.
        label_alpha: 0-255 opacity of the label tabs.
        font_size:   Label height in pixels; default scales with the image.

    Returns:
        A new RGB ``PIL.Image.Image`` the same size as the input.
    """
    from PIL import Image, ImageDraw, ImageFont

    if isinstance(image, Image.Image):
        base = image.convert("RGBA")
    else:
        base = Image.fromarray(_as_uint8(image)).convert("RGBA")
    width, height = base.size
    if width < 2 or height < 2:
        return base.convert("RGB")

    size = font_size or max(12, round(min(width, height) / 45))
    font = _font(ImageFont, size)
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    line = (*colour, max(0, min(255, line_alpha)))
    ink = (*colour, 255)
    tab = (255, 255, 255, max(0, min(255, label_alpha)))

    marks = [g for g in range(0, int(grid) + 1, max(1, int(step)))]
    for g in marks:
        x = round(g * (width - 1) / grid)
        y = round(g * (height - 1) / grid)
        draw.line([(x, 0), (x, height)], fill=line, width=1)
        draw.line([(0, y), (width, y)], fill=line, width=1)

    for g in marks:
        if g in (0, int(grid)):
            continue  # the frame edges are their own label
        text = str(g)
        x = round(g * (width - 1) / grid)
        y = round(g * (height - 1) / grid)
        # x along the top and bottom edges, just right of the line.
        for tx, ty in ((x + 2, 1), (x + 2, height - size - 3)):
            _label(draw, text, (tx, ty), font, tab, ink)
        # y along the left and right edges, just below the line.
        text_w = draw.textlength(text, font=font)
        for tx, ty in ((1, y + 2), (width - text_w - 4, y + 2)):
            _label(draw, text, (tx, ty), font, tab, ink)

    return Image.alpha_composite(base, overlay).convert("RGB")


def _label(draw: Any, text: str, at: tuple[float, float], font: Any, tab: Any, ink: Any) -> None:
    left, top, right, bottom = draw.textbbox(at, text, font=font)
    draw.rectangle((left - 2, top - 1, right + 2, bottom + 1), fill=tab)
    draw.text(at, text, fill=ink, font=font)


def _font(ImageFont: Any, size: int) -> Any:
    try:
        return ImageFont.load_default(size=size)   # Pillow ≥ 10.1: scalable
    except TypeError:                              # older Pillow: bitmap only
        return ImageFont.load_default()


def _as_uint8(array: Any) -> Any:
    import numpy as np

    arr = np.asarray(array)
    if arr.dtype != np.uint8:
        arr = np.clip(arr, 0, 255).astype(np.uint8)
    if arr.ndim == 2:
        arr = np.stack([arr] * 3, axis=-1)
    return arr
