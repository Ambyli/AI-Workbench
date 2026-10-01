"""Generate the classifier's manual region-layer fixtures.

Run from anywhere; files are written next to this script:

    uv run --package classifier python unit-tests/classifier/regions/make_fixtures.py

Same discipline as the documents fixtures next door: everything is generated
rather than committed as an opaque binary, and the noise is seeded, so
re-running produces byte-comparable files and an unchanged git diff unless the
recipe itself changed.

Fixtures produced (see README.md for the criteria → expected regions table):

    greenery_and_sky.png   a synthetic scene: blue sky gradient across the top,
                           a green mass along the bottom, and a flat blue
                           "pool" rectangle — one file that exercises
                           detect_sky, detect_vegetation AND detect_water, each
                           producing polygons in a place you can check by eye
    text_blocks.png        two columns of rendered paragraphs, for detect_text's
                           merged high-density block boxes and (with a `text`
                           criterion's options.ocr "always") for OCR line
                           polygons

(The scene_before / scene_after change-detection pair was removed with
`/assess/compare`; it was generated last, so removing it moved no other
fixture's noise.)

Deliberately NOT generated
--------------------------
**Faces.** A drawn face does not reliably trip a Haar cascade — it was trained
on photographs, and a synthetic one either fails outright or passes for reasons
that have nothing to do with the recipe, which makes it a fixture that lies.
Use the real photo already in this repo instead:

    ../Neighborhood.jpeg   a real street photo — vegetation, sky, and whatever
                           faces the cascade actually finds

**Documents.** The document fixtures already exist and are exactly the right
inputs for the text-hit overlays, so they are referenced, not copied:

    ../documents/photo_of_letter.png   OCR line polygons (source="ocr")
    ../documents/invoice_native.pdf    native PDF rectangles (source="pdf-text")

Dependencies: pillow, numpy — both already in ai/classifier/pyproject.toml.
"""

from __future__ import annotations

import io
import pathlib

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = pathlib.Path(__file__).resolve().parent

# Seeded so the texture noise is identical on every run.
RNG = np.random.default_rng(20260922)

# Page size for both fixtures. Comfortably over the 100×100 minimum and close
# enough to the ≤1000-px working size that the working_scale in the result is
# a number you can sanity-check by hand.
WIDTH, HEIGHT = 900, 700

# The two paragraph columns of text_blocks.png. Real sentences rather than
# lorem ipsum so `text` criteria against this fixture can search for something
# meaningful ("Notice to Owner", "$4,850.00").
COLUMN_LEFT = [
    "NOTICE TO OWNER",
    "",
    "Under Florida law, those who",
    "work on your property or",
    "provide materials and are not",
    "paid have a right to enforce a",
    "claim against your property.",
    "",
    "This claim is known as a",
    "construction lien.",
]

COLUMN_RIGHT = [
    "PAYMENT TERMS",
    "",
    "Payment Terms: Net 30. All",
    "invoices are due within thirty",
    "days of the invoice date.",
    "",
    "Total amount claimed:",
    "$4,850.00",
    "",
    "Questions: billing@example.com",
]


def _font(size: int) -> ImageFont.ImageFont:
    """A scalable font, falling back to Pillow's sized default.

    Same helper as the documents fixtures: the ancient fixed-size bitmap font
    is too small for OCR to read, so a TrueType face is tried first.
    """
    for candidate in (
        "DejaVuSans.ttf", "arial.ttf", "Arial.ttf", "LiberationSans-Regular.ttf"
    ):
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def _texture(img: Image.Image, amount: float) -> Image.Image:
    """Add seeded per-pixel noise.

    Not decoration: a perfectly flat colour field has zero Laplacian variance,
    which makes detect_water's flat-texture test pass for the sky as readily as
    for the pool. A little grain in the vegetation and none in the pool is what
    makes the three detectors disagree in the way a real photo does.
    """
    if not amount:
        return img
    arr = np.asarray(img).astype(np.float32)
    arr += RNG.normal(0.0, amount, arr.shape).astype(np.float32)
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))


def make_greenery_and_sky() -> pathlib.Path:
    """A scene with sky, vegetation, and water in known places.

    Layout, top to bottom:
        0 – 35%    sky: a blue vertical gradient (this is exactly the band
                   detect_sky measures, so it should read near 100% coverage)
        35 – 55%   a pale neutral band — ground/haze, so the green mass does
                   not run into the sky and merge into one contour
        55 – 100%  vegetation: a textured green mass with an irregular top
                   edge, so the contour has real vertices to simplify
        a rectangle at lower-left: a flat, untextured blue pool
    """
    img = Image.new("RGB", (WIDTH, HEIGHT), (232, 228, 220))
    draw = ImageDraw.Draw(img)

    # ── Sky: vertical gradient, deeper blue at the top ────────────────────
    sky_bottom = int(HEIGHT * 0.35)
    for y in range(sky_bottom):
        t = y / max(1, sky_bottom - 1)
        draw.line(
            [(0, y), (WIDTH, y)],
            fill=(int(90 + 90 * t), int(150 + 70 * t), int(225 + 25 * t)),
        )

    # ── Vegetation: a green mass with a lumpy top edge ────────────────────
    veg_top = int(HEIGHT * 0.55)
    edge = [
        (x, veg_top + int(26 * np.sin(x / 70.0)) + int(RNG.integers(-8, 9)))
        for x in range(0, WIDTH + 30, 30)
    ]
    draw.polygon(edge + [(WIDTH, HEIGHT), (0, HEIGHT)], fill=(46, 122, 48))
    # A couple of darker clumps so the mask is not one uniform blob.
    draw.ellipse([120, veg_top + 40, 360, veg_top + 190], fill=(34, 96, 38))
    draw.ellipse([560, veg_top + 20, 830, veg_top + 170], fill=(58, 140, 56))

    img = _texture(img, 4.0)

    # ── Pool: drawn AFTER the texture pass so it stays flat ───────────────
    # detect_water only counts a blue contour whose Laplacian variance is
    # under 200, so the pool has to be smoother than everything around it.
    pool = [90, int(HEIGHT * 0.74), 430, int(HEIGHT * 0.93)]
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(pool, radius=18, fill=(196, 132, 58))  # BGR-ish teal in RGB
    draw.rounded_rectangle(pool, radius=18, fill=(58, 132, 196))
    img = img.filter(ImageFilter.GaussianBlur(0.4))  # soften the seams a little

    # A JPEG round-trip before saving as PNG, for the same reason the document
    # fixtures do it: per-pixel noise is incompressible, and without this the
    # file is ~900 KB — most of a fixture folder's budget for one synthetic
    # scene. q86 keeps the grain the detectors need and the pool flat.
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=86, optimize=True)
    round_tripped = Image.open(buf)
    round_tripped.load()

    out = HERE / "greenery_and_sky.png"
    round_tripped.save(out, format="PNG", optimize=True)
    return out


def make_text_blocks() -> pathlib.Path:
    """Two columns of paragraphs on white, for text-block boxes and OCR lines.

    Two columns rather than one on purpose: detect_text merges ADJACENT
    high-density blocks, so a single column would prove nothing about the
    merge. Two well-separated columns should come back as two (or a few)
    boxes, not one box covering the page.
    """
    img = Image.new("RGB", (WIDTH, HEIGHT), "white")
    draw = ImageDraw.Draw(img)

    for column, x in ((COLUMN_LEFT, 70), (COLUMN_RIGHT, WIDTH // 2 + 40)):
        y = 80
        for line in column:
            size = 30 if line.isupper() and line else 22
            if line:
                draw.text((x, y), line, fill=(20, 20, 20), font=_font(size))
            y += int(size * 1.65)

    # A light JPEG round-trip: real pages are never perfectly clean, and the
    # 8×8 artefacts keep detect_text honest about what "high edge density"
    # means on a photographed page.
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88, optimize=True)
    round_tripped = Image.open(buf)
    round_tripped.load()

    out = HERE / "text_blocks.png"
    round_tripped.save(out, format="PNG", optimize=True)
    return out


def main() -> None:
    builders = [
        make_greenery_and_sky,
        make_text_blocks,
    ]
    total = 0
    for build in builders:
        path = build()
        size = path.stat().st_size
        total += size
        print(f"{path.name:28s} {size:>9,d} bytes")
    print(f"{'TOTAL':28s} {total:>9,d} bytes")
    print("\nReferenced, not generated (see README.md):")
    for name in (
        "../Neighborhood.jpeg",
        "../documents/photo_of_letter.png",
        "../documents/invoice_native.pdf",
    ):
        print(f"  {name}")


if __name__ == "__main__":
    main()
