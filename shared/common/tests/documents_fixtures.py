"""Programmatic fixture builders for the common.documents tests.

Every test document is generated in-process rather than committed as a binary
blob: the suite stays diffable, the fixtures can be tuned per test, and there
is no risk of a stale .pdf drifting from what the loader expects.

  make_png / make_jpeg  — tiny raster images (PIL)
  make_text_image       — a white page with rendered text, for OCR tests
  make_pdf              — a text-layer PDF, one page per string (PyMuPDF)
  make_scanned_pdf      — the same text rasterised, so the PDF has NO text
                          layer and only OCR can read it
  make_docx             — paragraphs plus an optional table (python-docx)
  make_document         — a Document assembled directly, for pure-logic tests
                          that should not depend on any parser
"""

from __future__ import annotations

import io
from typing import Iterable, Optional, Sequence

from common.documents.model import Document, Page


def make_png(size: tuple[int, int] = (120, 80), color: str = "white") -> bytes:
    """A minimal PNG."""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def make_jpeg(size: tuple[int, int] = (120, 80), color: str = "white") -> bytes:
    """A minimal JPEG."""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG", quality=90)
    return buf.getvalue()


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
