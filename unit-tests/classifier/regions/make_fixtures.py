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
                           merged high-density block boxes and (with ocr=always)
                           for OCR line polygons behind a `text` criterion
    scene_before.png       a synthetic yard: sky gradient, a flat-colour house
    scene_after.png        the same yard, RE-SHOT — the camera moved ~6 px and
                           rotated ~1°, one object was added and one removed.
                           The pair for `/assess/compare` with `regions.diff`:
                           the shift is what makes the ORB + RANSAC alignment
                           do real work rather than line up by construction

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


# ── The change-detection pair ──────────────────────────────────────────────
# Geometry shared by both frames, so "what moved" is only ever the camera and
# the two objects the recipe deliberately changes.
_HOUSE = (250, 250, 650, 470)          # x1, y1, x2, y2 — the flat wall
_SHED = (700, 360, 830, 470)           # present in BEFORE only → "removed"
_CAR = (120, 500, 330, 590)            # present in AFTER only  → "added"

# How far the "after" camera moved. Small enough that a homography can undo
# it, large enough that a diff which SKIPPED alignment would light up every
# edge in the scene — which is exactly the failure this pair is here to catch.
_SHIFT_PX = 6
_ROTATION_DEG = 1.0


def _draw_scene(*, with_shed: bool, with_car: bool) -> Image.Image:
    """One frame of the yard. Everything but the two swapped objects is fixed.

    Texture matters more than prettiness here: ORB needs corners, and a scene
    of flat rectangles on a smooth gradient gives it almost none. The fence
    pickets, the window grid and the seeded grain are what make the alignment
    step have something to align.
    """
    img = Image.new("RGB", (WIDTH, HEIGHT), (238, 236, 230))
    draw = ImageDraw.Draw(img)

    # Sky gradient, top third.
    horizon = int(HEIGHT * 0.34)
    for y in range(horizon):
        t = y / max(1, horizon - 1)
        draw.line([(0, y), (WIDTH, y)], fill=(int(120 + 70 * t), int(170 + 50 * t), 235))

    # Ground.
    draw.rectangle([0, horizon, WIDTH, HEIGHT], fill=(126, 148, 96))

    # A picket fence along the horizon — a long row of corners for ORB.
    for x in range(20, WIDTH - 10, 26):
        draw.rectangle([x, horizon - 34, x + 12, horizon + 6], fill=(206, 200, 186))
    draw.rectangle([0, horizon - 14, WIDTH, horizon - 8], fill=(190, 184, 170))

    # The house: wall, roof, door, and a grid of windows.
    x1, y1, x2, y2 = _HOUSE
    draw.polygon([(x1 - 26, y1), (x2 + 26, y1), ((x1 + x2) // 2, y1 - 86)],
                 fill=(142, 78, 62))
    draw.rectangle([x1, y1, x2, y2], fill=(226, 218, 200))
    draw.rectangle([x1 + 160, y2 - 86, x1 + 214, y2], fill=(98, 72, 54))
    for wx in (x1 + 36, x1 + 262):
        for wy in (y1 + 42, y1 + 130):
            draw.rectangle([wx, wy, wx + 74, wy + 54], fill=(120, 158, 186))
            draw.line([wx + 37, wy, wx + 37, wy + 54], fill=(226, 218, 200), width=3)
            draw.line([wx, wy + 27, wx + 74, wy + 27], fill=(226, 218, 200), width=3)

    if with_shed:
        sx1, sy1, sx2, sy2 = _SHED
        draw.rectangle([sx1, sy1, sx2, sy2], fill=(150, 126, 96))
        draw.polygon([(sx1 - 10, sy1), (sx2 + 10, sy1), ((sx1 + sx2) // 2, sy1 - 40)],
                     fill=(104, 86, 66))
        draw.rectangle([sx1 + 48, sy2 - 46, sx1 + 82, sy2], fill=(74, 60, 46))

    if with_car:
        cx1, cy1, cx2, cy2 = _CAR
        draw.rounded_rectangle([cx1, cy1 + 28, cx2, cy2], radius=14, fill=(178, 62, 58))
        draw.rounded_rectangle([cx1 + 42, cy1, cx2 - 42, cy1 + 40], radius=10,
                               fill=(150, 48, 46))
        for wheel in (cx1 + 34, cx2 - 34):
            draw.ellipse([wheel - 20, cy2 - 20, wheel + 20, cy2 + 20], fill=(38, 38, 42))

    # Light grain only: ORB takes its keypoints from the fence pickets and
    # window frames, and per-pixel noise is incompressible — at amount 3 this
    # pair alone was 840 KB of the fixture folder.
    return _texture(img, 1.2)


def _save_png(img: Image.Image, name: str, quality: int = 82) -> pathlib.Path:
    """JPEG round-trip, then PNG — the same size discipline as the others."""
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    round_tripped = Image.open(buf)
    round_tripped.load()
    out = HERE / name
    round_tripped.save(out, format="PNG", optimize=True)
    return out


def make_scene_before() -> pathlib.Path:
    """The reference frame: shed present, no car."""
    return _save_png(_draw_scene(with_shed=True, with_car=False), "scene_before.png")


def make_scene_after() -> pathlib.Path:
    """The subject frame: car present, shed gone, camera moved.

    The rotate-then-translate is the whole point. Without it the two images
    are pixel-aligned by construction, the homography is the identity, and the
    fixture would pass for a diff implementation that never aligned anything.
    With it, a diff that skips alignment lights up every fence picket and
    window frame in the scene.
    """
    after = _draw_scene(with_shed=False, with_car=True)
    after = after.rotate(
        _ROTATION_DEG, resample=Image.BICUBIC, fillcolor=(238, 236, 230)
    )
    after = after.transform(
        after.size,
        Image.AFFINE,
        (1, 0, -_SHIFT_PX, 0, 1, -_SHIFT_PX),
        resample=Image.BICUBIC,
        fillcolor=(238, 236, 230),
    )
    return _save_png(after, "scene_after.png")


def main() -> None:
    builders = [
        make_greenery_and_sky,
        make_text_blocks,
        make_scene_before,
        make_scene_after,
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
