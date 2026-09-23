"""Generate the classifier's manual document-test fixtures.

Run from anywhere; files are written next to this script:

    uv run --package classifier python unit-tests/classifier/documents/make_fixtures.py

Everything is generated rather than committed as opaque binaries, so a fixture
can be tuned (more skew, heavier blur, an extra page) by editing one constant
and re-running. Output is deterministic — the noise generators are seeded — so
re-running produces byte-comparable files and an unchanged git diff unless the
recipe itself changed.

Fixtures produced (see README.md for the criteria/expectations table):

    invoice_native.pdf        2-page PDF with a real text layer and a table
    invoice_scanned.pdf       the same invoice as page IMAGES only (no text
                              layer), slightly rotated and noisy → needs OCR
    contract.txt              plain UTF-8 text, "Limited Warranty" + a date
    proposal.docx             headings, paragraphs, and a table row
                              "System Size | 8.4 kW"
    photo_of_letter.png       a letter photographed badly: ~3° skew, uneven
                              brightness, mild blur, sensor noise → OCR-able
    photo_of_letter_blurry.png  same letter, heavily blurred → sharpness FAILs
                              and OCR confidence drops
    unsupported_legacy.doc    the OLE2 magic plus padding, to demonstrate the
                              clear "convert to .docx" rejection

The letter both photo fixtures are made from also carries three non-text
marks — a company logo, a "RECEIVED" stamp, and a signature — so a presence
criterion ("has a company logo") has something real to find and a known box
to be checked against. They are drawn from fixed constants, never from the
RNG, so adding them did not move the noise stream of any other fixture; see
``LOGO_BOX`` / ``STAMP_CENTER`` / ``SIGNATURE_BOX`` below and the README's
"Marks on the letter" table.

Dependencies: pymupdf, python-docx, pillow, numpy — all already in
ai/classifier/pyproject.toml.
"""

from __future__ import annotations

import datetime
import io
import math
import pathlib

import numpy as np
import pymupdf
from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = pathlib.Path(__file__).resolve().parent

# Seeded so noise is identical on every run.
RNG = np.random.default_rng(20260921)

# Both writers stamp a creation time into their output, which would make every
# regeneration a fresh binary diff. Pinning them keeps the PDFs and the .docx
# byte-stable as long as the recipe itself is unchanged.
FIXED_TIMESTAMP = "D:20260314120000Z"          # PDF date syntax
FIXED_DATETIME = datetime.datetime(2026, 3, 14, 12, 0, 0)  # python-docx core properties


def _pin_pdf_metadata(doc: "pymupdf.Document") -> None:
    """Replace PyMuPDF's generated timestamps with the fixed ones."""
    doc.set_metadata(
        {
            "producer": "make_fixtures.py",
            "creator": "make_fixtures.py",
            "title": "classifier document fixture",
            "creationDate": FIXED_TIMESTAMP,
            "modDate": FIXED_TIMESTAMP,
        }
    )

# ── The invoice, as data, so the PDF and the scan stay in sync ────────────
INVOICE_PAGE_1 = [
    ("ACME ROOFING LLC", 22),
    ("1420 Industrial Way, Tampa FL 33601", 11),
    ("", 11),
    ("Invoice #INV-2026-0042", 16),
    ("Date: 2026-03-14", 12),
    ("Bill To: Jane Homeowner, 88 Palm Ridge Road", 12),
    ("", 12),
    ("NOTICE TO OWNER", 14),
    (
        "Under Florida law, those who work on your property or provide materials",
        11,
    ),
    ("and are not paid have a right to enforce a claim against your property.", 11),
]

# (description, qty, amount) — rendered as a table in the PDF/DOCX and as
# aligned text in the scan.
INVOICE_ROWS = [
    ("Description", "Qty", "Amount"),
    ("Tear-off and haul away", "1", "$1,200.00"),
    ("Architectural shingle install", "1", "$3,650.00"),
    ("Total Due", "", "$4,850.00"),
]

INVOICE_PAGE_2 = [
    ("Payment Terms: Net 30", 16),
    ("", 12),
    ("Payment is due within 30 days of the invoice date.", 11),
    ("Late payments accrue interest at 1.5% per month.", 11),
    ("", 12),
    ("Questions: billing@acme-roofing.example", 11),
]

LETTER_LINES = [
    "ACME ROOFING LLC",
    "",
    "NOTICE TO OWNER",
    "Case number: CASE-77813",
    "Date: 2026-03-14",
    "",
    "To: Jane Homeowner",
    "88 Palm Ridge Road",
    "Tampa, FL 33601",
    "",
    "This letter serves as formal notice that materials",
    "have been furnished for improvements to the real",
    "property described above. Under Florida law, those",
    "who work on your property or provide materials and",
    "are not paid have a right to enforce a claim against",
    "your property.",
    "",
    "Total amount claimed: $4,850.00",
]

# ── Marks on the letter: logo, stamp, signature ──────────────────────────
# Drawn on the CLEAN 1000×1300 render, at these exact coordinates, so the box
# a presence criterion should come back with is known before the photograph
# step moves it. `_photograph` rotates the page 3° about its centre
# (500, 650), which displaces a mark by up to ~40 px — the README quotes both
# the clean box and the rotated one.
#
# Every number here is a constant, not an RNG draw: the module-level RNG is
# consumed by `_photograph` in a fixed order, and taking samples for the
# signature would have re-rolled the sensor noise of every other fixture.

# Top-left letterhead: a filled navy block with a white roof chevron, plus
# the company name beside it.
LOGO_BOX = (95, 26, 436, 88)       # measured ink extent, block + wordmark
LOGO_MARK_WIDTH = 62               # the filled block; the rest is the wordmark
LOGO_FILL = (26, 46, 92)

# A circular "RECEIVED" stamp, rotated, composited OVER the body paragraph so
# it overlaps real text the way a real one does. Red and translucent, outline
# only, so the text underneath still OCRs.
STAMP_CENTER = (700, 600)
STAMP_RADIUS = 118
STAMP_ANGLE = -18                  # degrees; negative tilts it clockwise
STAMP_INK = (196, 38, 38)
STAMP_ALPHA = 185

# A handwritten-looking scribble in the empty space below the body text.
SIGNATURE_BOX = (120, 946, 520, 1062)
SIGNATURE_INK = (18, 24, 70)
SIGNATURE_WIDTH = 5

CONTRACT_TEXT = """ROOFING SERVICES AGREEMENT

This Roofing Services Agreement is entered into on 2026-03-14 between Acme
Roofing LLC ("Contractor") and Jane Homeowner ("Owner") for work at 88 Palm
Ridge Road, Tampa, FL 33601.

1. Scope of Work. Contractor shall remove the existing roof covering and
install architectural asphalt shingles across approximately 24 squares,
including underlayment, drip edge, and ridge vent.

2. Limited Warranty. Contractor provides a Limited Warranty covering
workmanship for ten (10) years from the date of substantial completion.
Manufacturer warranties on materials pass through to the Owner and are not
extended by this section. The Limited Warranty is void if the roof is
modified by anyone other than Contractor.

3. Payment Terms: Net 30. Invoices are due within thirty days. Amounts not
paid when due accrue interest at 1.5% per month.

4. Permits. Contractor shall obtain the building permit prior to commencing
work and shall schedule all required inspections.
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _font(size: int) -> ImageFont.ImageFont:
    """Load a scalable font, falling back to Pillow's bundled default.

    Fixture text must be big enough for OCR to read, which the ancient
    fixed-size bitmap font is not — so a TrueType face is tried first and the
    sized default is the fallback (Pillow ≥ 10.1).
    """
    for candidate in ("DejaVuSans.ttf", "arial.ttf", "Arial.ttf", "LiberationSans-Regular.ttf"):
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def _render_lines(lines: list[tuple[str, int]], width: int, height: int) -> Image.Image:
    """Draw ``(text, point_size)`` lines onto a white page."""
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    y = int(height * 0.07)
    for text, size in lines:
        px = int(size * 2.2)  # points → pixels at roughly 150 dpi
        if text:
            draw.text((int(width * 0.09), y), text, fill=(20, 20, 20), font=_font(px))
        y += int(px * 1.5)
    return img


def _draw_table(draw: ImageDraw.ImageDraw, x: int, y: int, width: int, size: int) -> int:
    """Draw the invoice line-items table; returns the y below it."""
    font = _font(size)
    col = [x, x + int(width * 0.62), x + int(width * 0.78)]
    row_h = int(size * 2.0)
    for r, (desc, qty, amount) in enumerate(INVOICE_ROWS):
        draw.text((col[0], y), desc, fill=(20, 20, 20), font=font)
        draw.text((col[1], y), qty, fill=(20, 20, 20), font=font)
        draw.text((col[2], y), amount, fill=(20, 20, 20), font=font)
        y += row_h
        if r == 0 or r == len(INVOICE_ROWS) - 2:
            draw.line([(col[0], y - row_h // 4), (x + width, y - row_h // 4)], fill=(90, 90, 90), width=2)
    return y


def _photograph(img: Image.Image, *, skew: float, blur: float, noise: float, gradient: float) -> Image.Image:
    """Make a clean render look like a phone photo of a sheet of paper.

    Applies, in order: rotation (skew), an uneven brightness gradient (the
    light source is off to one side), Gaussian blur (imperfect focus), and
    per-pixel sensor noise.
    """
    img = img.rotate(skew, resample=Image.BICUBIC, expand=False, fillcolor=(255, 255, 255))

    arr = np.asarray(img).astype(np.float32)
    h, w = arr.shape[:2]
    # Diagonal gradient: brightest top-left, dimmest bottom-right.
    gx = np.linspace(1.0, 1.0 - gradient, w, dtype=np.float32)[None, :]
    gy = np.linspace(1.0, 1.0 - gradient / 2, h, dtype=np.float32)[:, None]
    arr *= (gx * gy)[:, :, None]
    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))

    if blur:
        img = img.filter(ImageFilter.GaussianBlur(blur))

    if noise:
        arr = np.asarray(img).astype(np.float32)
        arr += RNG.normal(0.0, noise, arr.shape).astype(np.float32)
        img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))

    return img


def _normalise_zip_timestamps(path: pathlib.Path) -> None:
    """Rewrite an OOXML package so every entry carries ``FIXED_DATETIME``.

    python-docx writes each zip entry with the clock time at save, so without
    this the .docx is a fresh binary diff on every regeneration even when its
    content is identical. Entries are also written in sorted order for the
    same reason.
    """
    import zipfile

    fixed = FIXED_DATETIME.timetuple()[:6]
    with zipfile.ZipFile(path) as src:
        entries = [(info, src.read(info.filename)) for info in sorted(src.infolist(), key=lambda i: i.filename)]

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
        for info, data in entries:
            rewritten = zipfile.ZipInfo(info.filename, date_time=fixed)
            rewritten.compress_type = info.compress_type
            rewritten.external_attr = info.external_attr
            out.writestr(rewritten, data)
    path.write_bytes(buf.getvalue())


def _jpeg_bytes(img: Image.Image, quality: int = 78) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_invoice_native() -> pathlib.Path:
    """2-page PDF with a real text layer, including a line-items table.

    PyMuPDF draws the table as text in aligned columns; the extracted text
    keeps the row together on one line, which is what a `text` criterion
    searching for "Total Due" needs.
    """
    doc = pymupdf.open()

    page = doc.new_page()  # letter, 612×792 pt
    y = 72.0
    for text, size in INVOICE_PAGE_1:
        if text:
            page.insert_text((60, y), text, fontsize=size)
        y += size * 1.7
    y += 12
    for desc, qty, amount in INVOICE_ROWS:
        page.insert_text((60, y), desc, fontsize=11)
        page.insert_text((400, y), qty, fontsize=11)
        page.insert_text((470, y), amount, fontsize=11)
        y += 20

    page2 = doc.new_page()
    y = 72.0
    for text, size in INVOICE_PAGE_2:
        if text:
            page2.insert_text((60, y), text, fontsize=size)
        y += size * 1.7

    out = HERE / "invoice_native.pdf"
    _pin_pdf_metadata(doc)
    doc.save(out, deflate=True, no_new_id=True)
    doc.close()
    return out


def make_invoice_scanned() -> pathlib.Path:
    """The same invoice as page images only — no text layer, so OCR is required.

    Each page is rendered large, "photographed" (2° rotation + light noise),
    JPEG-compressed to keep the file small, and inserted as a full-page image.
    """
    pages = [
        _render_lines(INVOICE_PAGE_1, 1240, 1600),
        _render_lines(INVOICE_PAGE_2, 1240, 1600),
    ]
    # Draw the line-items table onto page 1 under the text block.
    draw = ImageDraw.Draw(pages[0])
    _draw_table(draw, x=int(1240 * 0.09), y=int(1600 * 0.62), width=int(1240 * 0.82), size=24)

    doc = pymupdf.open()
    for image in pages:
        scan = _photograph(image, skew=2.0, blur=0.5, noise=3.0, gradient=0.10)
        rect = pymupdf.Rect(0, 0, 612, 792)
        page = doc.new_page(width=rect.width, height=rect.height)
        page.insert_image(rect, stream=_jpeg_bytes(scan))

    out = HERE / "invoice_scanned.pdf"
    _pin_pdf_metadata(doc)
    doc.save(out, deflate=True, no_new_id=True)
    doc.close()
    return out


def make_contract_txt() -> pathlib.Path:
    out = HERE / "contract.txt"
    out.write_text(CONTRACT_TEXT, encoding="utf-8", newline="\n")
    return out


def make_proposal_docx() -> pathlib.Path:
    """Headings, body paragraphs, and a specs table with 'System Size | 8.4 kW'."""
    import docx

    document = docx.Document()
    document.add_heading("Roofix Proposal", level=0)
    document.add_paragraph(
        "Prepared for Jane Homeowner, 88 Palm Ridge Road, Tampa FL 33601, on 2026-03-14."
    )

    document.add_heading("Scope", level=1)
    document.add_paragraph(
        "Full tear-off of the existing roof covering, installation of architectural "
        "asphalt shingles, and a rooftop photovoltaic array sized to the home's "
        "annual consumption."
    )

    document.add_heading("System specification", level=1)
    rows = [
        ("Component", "Value"),
        ("System Size", "8.4 kW"),
        ("Module count", "21"),
        ("Inverter", "String, 7.6 kW"),
        ("Estimated annual production", "11,900 kWh"),
    ]
    table = document.add_table(rows=len(rows), cols=2)
    table.style = "Table Grid"
    for r, (left, right) in enumerate(rows):
        table.cell(r, 0).text = left
        table.cell(r, 1).text = right

    document.add_heading("Terms", level=1)
    document.add_paragraph(
        "Payment Terms: Net 30. This Roofix Proposal is valid for 30 days from the "
        "date above and includes a Limited Warranty on workmanship."
    )

    props = document.core_properties
    props.created = FIXED_DATETIME
    props.modified = FIXED_DATETIME
    props.last_modified_by = "make_fixtures.py"
    props.revision = 1

    out = HERE / "proposal.docx"
    document.save(out)
    _normalise_zip_timestamps(out)
    return out


def _draw_logo(draw: ImageDraw.ImageDraw) -> None:
    """Company logo in the top-left margin: a filled block plus a wordmark.

    The block is a rounded navy rectangle with a white roof chevron cut into
    it — a shape, not text, so a presence criterion asking for "a company
    logo" has something non-textual to find. The wordmark beside it is drawn
    at a size the OCR engine reads comfortably, which is deliberate: the
    fixture should exercise "a logo AND its text", not one or the other.
    """
    x0, y0, x1, y1 = LOGO_BOX
    mark_right = x0 + LOGO_MARK_WIDTH
    draw.rounded_rectangle([x0, y0, mark_right, y1], radius=8, fill=LOGO_FILL)

    # White chevron ("roof") inside the block, apex up.
    apex_y = y0 + int((y1 - y0) * 0.28)
    base_y = y0 + int((y1 - y0) * 0.66)
    inset = int(LOGO_MARK_WIDTH * 0.18)
    draw.polygon(
        [
            (x0 + inset, base_y),
            ((x0 + mark_right) / 2, apex_y),
            (mark_right - inset, base_y),
            (mark_right - inset, base_y + 9),
            ((x0 + mark_right) / 2, apex_y + 9),
            (x0 + inset, base_y + 9),
        ],
        fill=(255, 255, 255),
    )
    draw.text(
        (mark_right + 16, y0 + 8),
        "ACME ROOFING",
        fill=LOGO_FILL,
        font=_font(34),
    )


def _stamp_image() -> Image.Image:
    """The circular "RECEIVED" stamp as a standalone RGBA tile, rotated.

    Built on its own canvas so it can be rotated independently of the page
    and composited with alpha — a stamp drawn straight onto the page would
    have to be axis-aligned, and an axis-aligned stamp is the one thing a
    real one never is.
    """
    r = STAMP_RADIUS
    size = r * 2 + 24
    tile = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(tile)
    cx = cy = size / 2.0
    ink = STAMP_INK + (STAMP_ALPHA,)

    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=ink, width=7)
    draw.ellipse(
        [cx - r + 16, cy - r + 16, cx + r - 16, cy + r - 16], outline=ink, width=3
    )

    word_font = _font(38)
    date_font = _font(22)
    for text, font, dy in (("RECEIVED", word_font, -26), ("2026-03-18", date_font, 22)):
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
        draw.text(
            (cx - (right - left) / 2.0, cy + dy - (bottom - top) / 2.0),
            text,
            fill=ink,
            font=font,
        )

    return tile.rotate(STAMP_ANGLE, resample=Image.BICUBIC, expand=True)


def _signature_points() -> list[tuple[float, float]]:
    """The signature stroke, as a closed-form curve rather than random walk.

    Two sine components at incommensurate frequencies plus a downward drift
    give something that reads as handwriting while staying byte-identical on
    every run — the fixture's whole value is that its box never moves.
    """
    x0, y0, x1, y1 = SIGNATURE_BOX
    span = x1 - x0
    mid = (y0 + y1) / 2.0
    amp = (y1 - y0) * 0.36
    points: list[tuple[float, float]] = []
    for i in range(241):
        t = i / 240.0
        y = (
            mid
            + amp * math.sin(t * math.pi * 5.0) * (1.0 - 0.45 * t)
            + amp * 0.45 * math.sin(t * math.pi * 13.0 + 0.8)
            - amp * 0.55 * t
        )
        points.append((x0 + t * span, y))
    return points


def _draw_signature(draw: ImageDraw.ImageDraw) -> None:
    """The scribble plus its trailing flourish, in the space below the body."""
    x0, _y0, x1, y1 = SIGNATURE_BOX
    draw.line(
        _signature_points(), fill=SIGNATURE_INK, width=SIGNATURE_WIDTH, joint="curve"
    )
    # A flourish under the name — the long tail a signature usually ends with.
    mid_x = (x0 + x1) / 2.0
    draw.line(
        [(x0 + 24, y1 - 10), (mid_x, y1 - 22), (x1 - 34, y1 - 4)],
        fill=SIGNATURE_INK,
        width=max(2, SIGNATURE_WIDTH - 2),
        joint="curve",
    )


def _letter_image() -> Image.Image:
    """The clean letter render both photo fixtures are made from.

    Text first, then the logo in the top margin and the signature below the
    body, then the stamp composited last so it sits OVER the paragraph — the
    order a real document acquires them in, and the order that makes the
    stamp overlap text rather than hide behind it.
    """
    img = Image.new("RGB", (1000, 1300), "white")
    draw = ImageDraw.Draw(img)
    y = 100
    for line in LETTER_LINES:
        size = 30 if line in ("ACME ROOFING LLC", "NOTICE TO OWNER") else 23
        if line:
            draw.text((95, y), line, fill=(25, 25, 25), font=_font(size))
        y += int(size * 1.7)

    _draw_logo(draw)
    _draw_signature(draw)

    stamp = _stamp_image()
    origin = (
        int(STAMP_CENTER[0] - stamp.width / 2),
        int(STAMP_CENTER[1] - stamp.height / 2),
    )
    img.paste(stamp, origin, stamp)
    return img


def _as_photo_png(img: Image.Image, out: pathlib.Path) -> pathlib.Path:
    """Save a "photographed" render as PNG, via a JPEG round-trip.

    The round-trip is doing two jobs: it adds the blocky 8×8 artefacts a real
    phone photo carries, and it collapses the per-pixel sensor noise into
    something PNG can actually compress — without it these two fixtures alone
    are ~7 MB, which is more than the whole fixture folder is allowed.
    """
    round_tripped = Image.open(io.BytesIO(_jpeg_bytes(img, quality=62)))
    round_tripped.load()
    round_tripped.save(out, format="PNG", optimize=True)
    return out


def make_photo_of_letter() -> pathlib.Path:
    """A readable but badly photographed letter: skew, gradient, mild blur, noise."""
    photo = _photograph(_letter_image(), skew=3.0, blur=0.8, noise=4.0, gradient=0.26)
    return _as_photo_png(photo, HERE / "photo_of_letter.png")


def make_photo_of_letter_blurry() -> pathlib.Path:
    """The same letter, heavily out of focus — sharpness FAILs, OCR degrades.

    Blur is 3.8, not 4.0: this fixture sits deliberately on the detector's
    cliff (only the two large headings are meant to survive), and the ink the
    logo / stamp / signature added was enough to push 4.0 over it — the page
    came back with `ACME ROOFING LLC` alone, which would have flipped the
    documented `Notice to Owner` PASS to a FAIL for a reason that has nothing
    to do with the classifier. 3.8 restores "the headings and nothing else"
    and still measures a Laplacian variance of ~7, far under the 100 floor.
    """
    photo = _photograph(_letter_image(), skew=3.0, blur=3.8, noise=4.0, gradient=0.26)
    return _as_photo_png(photo, HERE / "photo_of_letter_blurry.png")


def make_unsupported_legacy_doc() -> pathlib.Path:
    """The smallest file that trips the OLE2 (.doc) rejection path."""
    out = HERE / "unsupported_legacy.doc"
    out.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 56)
    return out


def main() -> None:
    builders = [
        make_invoice_native,
        make_invoice_scanned,
        make_contract_txt,
        make_proposal_docx,
        make_photo_of_letter,
        make_photo_of_letter_blurry,
        make_unsupported_legacy_doc,
    ]
    total = 0
    for build in builders:
        path = build()
        size = path.stat().st_size
        total += size
        print(f"{path.name:32s} {size:>9,d} bytes")
    print(f"{'TOTAL':32s} {total:>9,d} bytes")


if __name__ == "__main__":
    main()
