"""``Page.native_lines`` — a PDF / SVG page's own text lines, as geometry.

The classifier's marks refine numbers text lines in a window and boxes the
ones the model picks. On a digital PDF the text layer is PyMuPDF's
``get_text("text")``, which has no positions, so the loader keeps the lines'
geometry beside it. What is pinned here:

  * a native line's box is exactly ``pdf_text_regions``' box for the same
    words (the one ``_points_to_pixels`` conversion), and lands on ink, at
    ``/Rotate`` 0, 90, 180 and 270;
  * a gap over ``NATIVE_LINE_SPLIT_GAP`` × the line's height splits one
    PyMuPDF line in two (a label and its amount a column apart), and a
    sentence stays whole;
  * an image-only PDF has none, an SVG's ``<text>`` has them (a capped zoom
    included), and a failure to measure them never fails a load;
  * they never leak into the text machinery: a native page's ``ocr_lines``
    stay ``[]``, ``match_text(locate=True)`` answers exactly as without
    them, ``TextLayer.as_dict`` does not count them — and every layer of the
    page carries them, an OCR one included.

The PDFs are generated (``insert_text`` + ``set_rotation``), as in
``test_documents_locate.py``.
"""

from __future__ import annotations

import re

import pytest

from common.documents import (
    NATIVE_LINE_SPLIT_GAP,
    OCRResult,
    TextLayer,
    load_document,
    match_text,
    pdf_text_regions,
    recognize_text_layer,
    with_text_layers,
)
from common.documents import loaders

pymupdf = pytest.importorskip("pymupdf")


def _pdf(lines, *, rotation=0, width=612, height=792) -> bytes:
    """A one-page PDF with each ``(x, y, text, fontsize)`` drawn as one run."""
    pdf = pymupdf.open()
    page = pdf.new_page(width=width, height=height)
    for x, y, text, size in lines:
        page.insert_text((x, y), text, fontsize=size)
    page.set_rotation(rotation)
    blob = pdf.tobytes()
    pdf.close()
    return blob


def _bounds(box):
    xs, ys = [p[0] for p in box], [p[1] for p in box]
    return [min(xs), min(ys), max(xs), max(ys)]


TWO_LINES = [(60, 100, "NOTICE TO OWNER", 16), (300, 600, "Total Due 4,850.00", 12)]


# ── The same rectangle pdf_text_regions draws, at every rotation ─────────────
@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_native_lines_are_pdf_text_regions_boxes_for_the_same_words(rotation):
    blob = _pdf(TWO_LINES, rotation=rotation)
    doc = load_document(blob, "rotated.pdf", keep_source=True)
    page = doc.pages[0]
    assert (page.width > page.height) == (rotation in (90, 270))
    assert [line["text"] for line in page.native_lines] == [
        "NOTICE TO OWNER", "Total Due 4,850.00"]
    for line in page.native_lines:
        # The word-span path of pdf_text_regions: the union of the same words'
        # boxes, through the same conversion.
        pattern = re.escape(line["text"])
        hits = match_text(doc, pattern, "regex", locate=True)
        (region,) = pdf_text_regions(blob, page, hits.hits, pattern=pattern, mode="regex")
        (x0, y0), (x1, y1) = region.points
        assert _bounds(line["box"]) == pytest.approx([x0, y0, x1, y1], abs=0.01), rotation
        bx0, by0, bx1, by1 = _bounds(line["box"])
        assert 0 <= bx0 < bx1 <= page.width and 0 <= by0 < by1 <= page.height
        # Horizontal text runs along the long side of its box until the page
        # turns it a quarter — and there is ink under it.
        assert ((bx1 - bx0) > (by1 - by0)) == (rotation in (0, 180)), rotation
        crop = page.image_bgr[int(by0):int(by1) + 1, int(bx0):int(bx1) + 1]
        assert (crop < 128).mean() > 0.05, f"no ink under {line['text']!r} at /Rotate {rotation}"
        # A 4-point polygon, as OCR lines are.
        assert len(line["box"]) == 4 and set(line) == {"text", "box"}


# ── Split at a column gap, not at a word space ───────────────────────────────
def test_a_wide_gap_splits_a_line_and_a_sentence_stays_whole(monkeypatch):
    blob = _pdf([(60, 100, "Total Due" + " " * 40 + "$4,850.00", 12),
                 (60, 200, "Payment is due within 30 days of the invoice date.", 12)])
    # PyMuPDF itself reads the first run as ONE line: the split is ours.
    with pymupdf.open(stream=blob, filetype="pdf") as pdf:
        words = pdf.load_page(0).get_text("words")
    first = {(w[5], w[6]) for w in words if w[4] in ("Total", "Due", "$4,850.00")}
    assert len(first) == 1

    lines = load_document(blob, "gap.pdf").pages[0].native_lines
    assert [line["text"] for line in lines] == [
        "Total Due", "$4,850.00", "Payment is due within 30 days of the invoice date."]
    label, amount = (_bounds(line["box"]) for line in lines[:2])
    assert label[2] < amount[0]                               # side by side, apart
    assert label[1] == amount[1] and label[3] == amount[3]    # one row
    # The threshold is the module constant: raise it and the run is one line.
    assert NATIVE_LINE_SPLIT_GAP == 2.0
    monkeypatch.setattr(loaders, "NATIVE_LINE_SPLIT_GAP", 1000.0)
    lines = load_document(blob, "gap.pdf").pages[0].native_lines
    assert lines[0]["text"] == "Total Due $4,850.00"


# ── Pages with no native lines, and an SVG's ─────────────────────────────────
def test_an_image_only_pdf_has_no_native_lines():
    import io

    from PIL import Image

    png = io.BytesIO()
    Image.new("RGB", (200, 100), (30, 30, 30)).save(png, format="PNG")
    pdf = pymupdf.open()
    page = pdf.new_page(width=612, height=792)
    page.insert_image(pymupdf.Rect(50, 50, 450, 250), stream=png.getvalue())
    blob = pdf.tobytes()
    pdf.close()
    page = load_document(blob, "scan.pdf").pages[0]
    assert page.text_source == "none" and page.native_lines == []
    # Every other kind has none either.
    assert load_document(b"Total Due $4,850.00\n", "a.txt").pages[0].native_lines == []


SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="640" height="480">'
    '<rect width="640" height="480" fill="white"/>'
    '<text x="32" y="60" font-size="28" font-family="Helvetica">System Size: 8.4 kW</text>'
    '<text x="32" y="400" font-size="20" font-family="Helvetica">12 modules</text>'
    "</svg>"
).encode("utf-8")


@pytest.mark.parametrize("max_pixels", [25_000_000, 100_000], ids=["dpi", "capped"])
def test_an_svgs_text_has_native_lines(max_pixels):
    doc = load_document(SVG, "plan.svg", keep_source=True, max_render_pixels=max_pixels)
    page = doc.pages[0]
    assert [line["text"] for line in page.native_lines] == ["System Size: 8.4 kW", "12 modules"]
    for line in page.native_lines:
        pattern = re.escape(line["text"])
        hits = match_text(doc, pattern, "regex", locate=True)
        (region,) = pdf_text_regions(SVG, page, hits.hits, pattern=pattern, mode="regex",
                                     filetype="svg")
        (x0, y0), (x1, y1) = region.points
        assert _bounds(line["box"]) == pytest.approx([x0, y0, x1, y1], abs=0.01)
        bx0, by0, bx1, by1 = _bounds(line["box"])
        assert 0 <= bx0 < bx1 <= page.width and 0 <= by0 < by1 <= page.height
        crop = page.image_bgr[int(by0):int(by1) + 1, int(bx0):int(bx1) + 1]
        assert (crop < 128).mean() > 0.02


# ── Never a failed load ──────────────────────────────────────────────────────
def test_measuring_lines_never_fails_a_load(monkeypatch):
    class Broken:
        def get_text(self, *args, **kwargs):
            raise RuntimeError("mupdf fell over")

    assert loaders._native_lines(Broken(), 100, 100) == []
    assert loaders._native_lines(Broken(), 0, 0) == []

    def explode(*args, **kwargs):
        raise RuntimeError("bad matrix")

    monkeypatch.setattr(loaders, "_points_to_pixels", explode)
    page = load_document(_pdf(TWO_LINES), "x.pdf").pages[0]
    assert page.native_lines == [] and "NOTICE TO OWNER" in page.text
    svg = load_document(SVG, "plan.svg").pages[0]
    assert svg.native_lines == [] and "12 modules" in svg.text


# ── Geometry only: the text machinery never sees them ────────────────────────
class _FakeEngine:
    def recognize(self, image_bgr):
        lines = [{"text": "NOTICE TO OWNER", "confidence": 0.9,
                  "box": [[40, 100], [360, 100], [360, 140], [40, 140]]}]
        return OCRResult(text="NOTICE TO OWNER", confidence=0.9, lines=lines)


def test_native_lines_stay_out_of_the_text_layer():
    blob = _pdf(TWO_LINES)
    doc = load_document(blob, "native.pdf", keep_source=True)
    page = doc.pages[0]
    assert page.native_lines and page.ocr_lines == []
    # match_text(locate=True) answers exactly as on the page without them: no
    # OCR lines, so no ocr regions — the native lines are not a substitute.
    bare = load_document(blob, "native.pdf", keep_source=True)
    bare.pages[0].native_lines = []
    for pattern, mode in (("NOTICE", "contains"), (r"\d,\d{3}\.\d{2}", "regex"),
                          ("Total Dve", "fuzzy")):
        got = match_text(doc, pattern, mode, locate=True)
        want = match_text(bare, pattern, mode, locate=True)
        assert got.found and got.regions == [] == want.regions
        assert [(h.page, h.start, h.end, h.text) for h in got.hits] == [
            (h.page, h.start, h.end, h.text) for h in want.hits]

    # The native layer carries them, outside `lines`; its summary is unchanged.
    native = TextLayer.of_page(page)
    assert native.lines == [] and native.native_lines == page.native_lines
    assert native.as_dict() == {"page": 0, "source": "native", "chars": native.chars(),
                                "confidence": None, "lines": 0}
    # An OCR layer of the same page carries them too — the geometry is the
    # page's — while its `lines` are the recogniser's, offset-aligned.
    ocr = recognize_text_layer(page, _FakeEngine(), mode="always")
    assert ocr.source == "ocr" and ocr.native_lines == page.native_lines
    assert ocr.text == "\n".join(line["text"] for line in ocr.lines)
    view = with_text_layers(doc, [ocr])
    assert view.pages[0].native_lines == page.native_lines
    assert view.pages[0].ocr_lines == ocr.lines
    located = match_text(view, "NOTICE", "contains", locate=True)
    assert [r.source for r in located.regions] == ["ocr"]
    assert located.regions[0].points[0] == (40.0, 100.0)
