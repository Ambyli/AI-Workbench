"""Numbered marks drawn over an image, for a model to pick from ("set-of-marks").

Asked "where is X, as a box?", a vision model has to produce four numbers, and
on a dense document it places a text line a few pixels off or boxes a value
without its label. Asked "which of these numbered outlines is X?", it only
has to RECOGNISE: the outlines come from something that measured them (OCR
line boxes, detector boxes), so the answer is exactly as precise as they are,
and several numbers can name one thing written over several lines.

    draw_marks_overlay(image, boxes)  → the image with each box outlined and
                                        numbered, RGB

Each box gets a thin outline and a solid tab with its number at its left end
— just outside the box when there is room, else just inside — vertically
centred on it, so a column of text lines gets a column of tabs in its
margin. Colours cycle through a small palette (no red: the coordinate grid of
``grid_overlay`` is red, and the two are never meant to be confused) so
neighbouring outlines stay distinguishable. Labels default to ``1..N`` in the
order given; the caller decides the order (reading order, for text lines).

Like the grid, the overlay is a question aid: it is drawn on the image a
model is ASKED about and is never stored.

Pure Pillow, imported at call time like ``grid_overlay``; the labels use
Pillow's bundled scalable default font (``load_default(size=…)``, Pillow ≥
10.1), the bitmap default on an older Pillow.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

# Outline / tab colours, cycled. Saturated, mutually distinct, none of them
# red (the grid's colour) and every one dark enough for white digits.
MARK_COLOURS: tuple[tuple[int, int, int], ...] = (
    (0, 90, 230),     # blue
    (190, 0, 160),    # magenta
    (0, 130, 60),     # green
    (200, 100, 0),    # orange
    (110, 40, 190),   # purple
    (0, 125, 140),    # teal
)


def draw_marks_overlay(
    image: Any,
    boxes: Sequence[Sequence[float]],
    *,
    labels: Optional[Sequence[str]] = None,
    colours: Sequence[tuple[int, int, int]] = MARK_COLOURS,
    line_width: int = 2,
    font_size: Optional[int] = None,
) -> Any:
    """Return ``image`` with each box outlined and labelled.

    Args:
        image:      A ``PIL.Image.Image`` or an ``(H, W, 3)`` numpy array in
                    **RGB** order (reverse a BGR array first).
        boxes:      ``[x1, y1, x2, y2]`` per mark, in THIS image's pixels. A
                    box partly outside the image is drawn clipped; one with
                    no width or height still gets its tab.
        labels:     The text on each tab; default ``"1"``, ``"2"``, … in the
                    order of ``boxes``. Must be as long as ``boxes``.
        colours:    Cycled per box (outline and tab).
        line_width: Outline width in pixels.
        font_size:  Tab text height in pixels; default scales with the image
                    as the grid's labels do.

    Returns:
        A new RGB ``PIL.Image.Image`` the same size as the input.

    Raises:
        ValueError: ``labels`` is not as long as ``boxes``.
    """
    from PIL import Image, ImageDraw, ImageFont

    if labels is None:
        labels = [str(i) for i in range(1, len(boxes) + 1)]
    if len(labels) != len(boxes):
        raise ValueError(f"{len(labels)} labels for {len(boxes)} boxes")
    if isinstance(image, Image.Image):
        base = image.convert("RGB")
    else:
        base = Image.fromarray(_as_uint8(image)).convert("RGB")
    out = base.copy()
    width, height = out.size
    if width < 2 or height < 2 or not boxes:
        return out

    size = font_size or max(12, round(min(width, height) / 45))
    font = _font(ImageFont, size)
    draw = ImageDraw.Draw(out)
    palette = list(colours) or [MARK_COLOURS[0]]

    # Outlines first, then every tab on top, so no outline crosses a number.
    for i, box in enumerate(boxes):
        x1, y1, x2, y2 = _clip(box, width, height)
        draw.rectangle((x1, y1, x2, y2), outline=palette[i % len(palette)], width=line_width)
    for i, (box, label) in enumerate(zip(boxes, labels)):
        colour = palette[i % len(palette)]
        x1, y1, x2, y2 = _clip(box, width, height)
        text = str(label)
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
        tab_w, tab_h = (right - left) + 6, (bottom - top) + 4
        # Left of the box when it fits, else inside its left edge; centred on
        # the box vertically; always inside the image.
        tx = x1 - tab_w - 1 if x1 - tab_w - 1 >= 0 else x1 + 1
        ty = (y1 + y2) / 2 - tab_h / 2
        tx = min(max(0, tx), width - tab_w)
        ty = min(max(0, ty), height - tab_h)
        draw.rectangle((tx, ty, tx + tab_w, ty + tab_h), fill=colour)
        draw.text((tx + 3 - left, ty + 2 - top), text, fill=(255, 255, 255), font=font)
    return out


def _clip(box: Sequence[float], width: int, height: int) -> tuple[float, float, float, float]:
    """``[x1, y1, x2, y2]`` ordered and pulled inside the image."""
    x1, y1, x2, y2 = (float(v) for v in box[:4])
    x1, x2 = sorted((x1, x2))
    y1, y2 = sorted((y1, y2))
    return (
        min(max(0.0, x1), width - 1.0), min(max(0.0, y1), height - 1.0),
        min(max(0.0, x2), width - 1.0), min(max(0.0, y2), height - 1.0),
    )


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
