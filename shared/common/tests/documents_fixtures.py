"""Programmatic fixture builders for the common.documents tests.

Every test document is generated in-process rather than committed as a binary
blob: the suite stays diffable, the fixtures can be tuned per test, and there
is no risk of a stale .pdf drifting from what the loader expects.

  make_png / make_jpeg  — tiny raster images (PIL); make_jpeg can carry an
                          EXIF orientation
  make_heic             — a HEIC via pillow-heif's encoder; with an
                          orientation it carries it twice, as an iPhone photo
                          does (container irot/imir AND the EXIF tag)
  make_avif             — an AVIF via Pillow's own plugin (≥ 11.3), whose
                          encoder turns an EXIF orientation into irot/imir
  make_ftyp             — just an ISO-BMFF ftyp box, for brand detection
  marker_picture        — a picture whose orientation can be read back: a
                          red block in the top-left corner (``marker_box``)
  make_text_image       — a white page with rendered text, for OCR tests
  make_pdf              — a text-layer PDF, one page per string (PyMuPDF)
  make_scanned_pdf      — the same text rasterised, so the PDF has NO text
                          layer and only OCR can read it
  make_svg              — an SVG document as UTF-8 bytes (a string template;
                          MuPDF renders it)
  make_docx             — paragraphs plus an optional table (python-docx)
  make_document         — a Document assembled directly, for pure-logic tests
                          that should not depend on any parser
"""

from __future__ import annotations

import io
import struct
from typing import Iterable, Optional, Sequence

from common.documents.model import Document, Page


def make_png(size: tuple[int, int] = (120, 80), color: str = "white") -> bytes:
    """A minimal PNG."""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _exif(orientation: Optional[int]) -> dict:
    """``save`` keyword arguments carrying an EXIF orientation tag (or none)."""
    if not orientation:
        return {}
    from PIL import Image

    exif = Image.Exif()
    exif[0x0112] = orientation
    return {"exif": exif.tobytes()}


def marker_picture(size: tuple[int, int] = (120, 80)):
    """A blue picture with a red block in its top-left quarter.

    Where the block ends up after decoding (``marker_box``) says which way up
    the picture came out, so an orientation applied twice — or not at all —
    is visible, not just the dimensions.
    """
    from PIL import Image

    width, height = size
    img = Image.new("RGB", size, (0, 0, 255))
    img.paste((255, 0, 0), (0, 0, width // 4, height // 4))
    return img


def marker_box(bgr) -> Optional[tuple[int, int, int, int]]:
    """``(x0, y0, x1, y1)`` of the red block in a decoded BGR array, or None."""
    import numpy as np

    red = (bgr[:, :, 2] > 180) & (bgr[:, :, 1] < 90) & (bgr[:, :, 0] < 90)
    ys, xs = np.nonzero(red)
    if not len(xs):
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


def make_jpeg(
    size: tuple[int, int] = (120, 80),
    color: str = "white",
    *,
    exif_orientation: Optional[int] = None,
    image=None,
) -> bytes:
    """A minimal JPEG — of ``image`` when given — optionally carrying an EXIF
    orientation tag (the stored pixels stay as they are)."""
    from PIL import Image

    img = image if image is not None else Image.new("RGB", size, color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90, **_exif(exif_orientation))
    return buf.getvalue()


def make_heic(
    size: tuple[int, int] = (120, 80),
    color: str = "white",
    *,
    exif_orientation: Optional[int] = None,
    image=None,
) -> bytes:
    """A HEIC (HEVC-coded HEIF) written by pillow-heif's encoder.

    With ``exif_orientation`` the stored pixels are left as they are and the
    file says how to turn them twice over, the way an iPhone photo does: the
    encoder writes the container's ``irot`` / ``imir`` for that orientation
    AND keeps the tag in the stored EXIF. A decoder that honoured both would
    rotate twice — which is what the loader tests check does not happen.
    """
    import pillow_heif
    from PIL import Image

    img = image if image is not None else Image.new("RGB", size, color)
    buf = io.BytesIO()
    pillow_heif.from_pillow(img).save(buf, quality=90, **_exif(exif_orientation))
    return buf.getvalue()


def make_avif(
    size: tuple[int, int] = (120, 80),
    color: str = "white",
    *,
    exif_orientation: Optional[int] = None,
    image=None,
) -> bytes:
    """An AVIF written by Pillow's own plugin (Pillow ≥ 11.3 built with AVIF).

    Pillow's encoder moves an EXIF orientation into the container — it writes
    ``irot`` / ``imir`` and drops the tag from the stored EXIF — so the file
    looks like a camera's AVIF, with the stored pixels unrotated.
    """
    from PIL import Image

    img = image if image is not None else Image.new("RGB", size, color)
    buf = io.BytesIO()
    img.save(buf, format="AVIF", quality=90, **_exif(exif_orientation))
    return buf.getvalue()


def make_ftyp(
    major: str,
    compatible: Sequence[str] = (),
    *,
    size: Optional[int] = None,
    tail: bytes = b"",
) -> bytes:
    """Just an ISO-BMFF ``ftyp`` box (size, ``ftyp``, major brand, minor
    version 0, compatible brands) followed by ``tail`` — enough for brand
    detection, nothing a decoder could open. ``size`` overrides the box's
    size field, to fake a truncated file or a lying header."""
    brands = b"".join(b.encode("latin-1") for b in compatible)
    declared = 16 + len(brands) if size is None else size
    return (
        struct.pack(">I", declared) + b"ftyp" + major.encode("latin-1")
        + b"\x00\x00\x00\x00" + brands + tail
    )


def make_text_image(lines: Sequence[str], width: int = 1000, line_height: int = 70) -> bytes:
    """A white PNG with ``lines`` rendered large enough for OCR to read."""
    from PIL import Image, ImageDraw

    height = line_height * (len(lines) + 1)
    img = Image.new("RGB", (width // 2, height // 2), "white")
    draw = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        draw.text((20, 20 + i * (line_height // 2)), line, fill="black")
    # Upscale so the default bitmap font becomes comfortably large.
    img = img.resize((width, height), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def make_pdf(pages: Sequence[str], *, fontsize: int = 12) -> bytes:
    """A PDF with a real text layer — one page per entry in ``pages``.

    Newlines inside an entry become separate lines on that page, so a page
    can carry several phrases without needing a layout engine.
    """
    import pymupdf

    doc = pymupdf.open()
    leading = int(fontsize * 2.2)
    for body in pages:
        page = doc.new_page()
        y = 96
        for line in body.split("\n"):
            page.insert_text((72, y), line, fontsize=fontsize)
            y += leading
    raw = doc.tobytes()
    doc.close()
    return raw


def make_scanned_pdf(pages: Sequence[str], *, dpi: int = 150) -> bytes:
    """A PDF whose pages are images of text — no text layer at all.

    Built by rendering a text PDF to pixmaps and re-inserting them as page
    images, which is exactly what a scanner produces.

    Note for OCR tests: a single short line alone on a letter-size page is
    genuinely hard for a detector (the glyphs are ~1% of the page height).
    Pass a few lines, like a real document has.
    """
    import pymupdf

    source = pymupdf.open(stream=make_pdf(pages, fontsize=14), filetype="pdf")
    out = pymupdf.open()
    for i in range(source.page_count):
        pix = source.load_page(i).get_pixmap(dpi=dpi)
        page = out.new_page(width=pix.width, height=pix.height)
        page.insert_image(pymupdf.Rect(0, 0, pix.width, pix.height), pixmap=pix)
    raw = out.tobytes()
    source.close()
    out.close()
    return raw


SVG_NS = 'xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"'


def make_svg(
    body: str = "",
    *,
    width: str = "400",
    height: str = "200",
    view_box: Optional[str] = None,
    prolog: str = "",
) -> bytes:
    """An SVG document: ``prolog`` (an XML declaration, DOCTYPE, comment …),
    then a root ``<svg>`` with both namespaces declared, then ``body``.

    Pass ``width=""`` / ``height=""`` to leave the attribute off (so MuPDF
    falls back to ``view_box``).
    """
    attrs = [SVG_NS]
    if width:
        attrs.append(f'width="{width}"')
    if height:
        attrs.append(f'height="{height}"')
    if view_box:
        attrs.append(f'viewBox="{view_box}"')
    return f'{prolog}<svg {" ".join(attrs)}>{body}</svg>'.encode("utf-8")


def make_docx(
    paragraphs: Iterable[str],
    table_rows: Optional[Sequence[Sequence[str]]] = None,
    *,
    trailing_paragraph: Optional[str] = None,
) -> bytes:
    """A .docx with paragraphs, an optional table, and an optional trailing
    paragraph after the table (so document-order extraction can be asserted)."""
    import docx

    document = docx.Document()
    for text in paragraphs:
        document.add_paragraph(text)
    if table_rows:
        table = document.add_table(rows=len(table_rows), cols=len(table_rows[0]))
        for r, row in enumerate(table_rows):
            for c, value in enumerate(row):
                table.cell(r, c).text = value
    if trailing_paragraph:
        document.add_paragraph(trailing_paragraph)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def make_document(page_texts: Sequence[str], *, kind: str = "pdf", with_images: bool = False) -> Document:
    """Assemble a Document directly — no parser involved.

    Used by the textmatch and OCR tests, which care about the logic operating
    on pages rather than about any particular file format.
    """
    import numpy as np

    pages = []
    for i, text in enumerate(page_texts):
        image = np.zeros((120, 200, 3), dtype=np.uint8) if with_images else None
        pages.append(
            Page(
                index=i,
                image_bgr=image,
                text=text,
                text_source="native" if text.strip() else "none",
                width=200 if with_images else 0,
                height=120 if with_images else 0,
            )
        )
    return Document(kind=kind, filename="fixture", size_bytes=0, content_type="", pages=pages)
