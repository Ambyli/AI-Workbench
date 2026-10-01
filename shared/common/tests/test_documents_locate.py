"""Tests for the "where did that text hit land?" additions.

Covers ``match_text(..., locate=True)`` (character offsets + OCR line
polygons) and ``loaders.pdf_text_regions`` (native-PDF rectangles). The OCR
side uses a fake engine with hand-made boxes so the assertions are about the
offset → line mapping, not about a recogniser's accuracy.
"""

from __future__ import annotations

import pytest

from common.documents import (
    Document,
    OCRResult,
    Page,
    apply_ocr,
    load_document,
    match_text,
    ocr_line_regions,
    pdf_text_regions,
)


class _FakeEngine:
    """An OCREngine that returns a fixed set of lines with known boxes."""

    def __init__(self, lines: list[tuple[str, float, list[list[float]]]]) -> None:
        self.lines = lines

    def recognize(self, image_bgr):  # noqa: D401 - protocol method
        detail = [
            {"text": text, "confidence": conf, "box": box}
            for text, conf, box in self.lines
        ]
        return OCRResult(
            text="\n".join(d["text"] for d in detail),
            confidence=sum(d["confidence"] for d in detail) / len(detail),
            lines=detail,
        )


def _box(x1, y1, x2, y2):
    return [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]


def _ocr_document():
    import numpy as np

    page = Page(index=0, image_bgr=np.zeros((400, 600, 3), dtype="uint8"),
                width=600, height=400)
    doc = Document(kind="image", pages=[page])
    engine = _FakeEngine(
        [
            ("ACME ROOFING LLC", 0.99, _box(40, 30, 380, 70)),
            ("NOTICE TO OWNER", 0.95, _box(40, 100, 360, 140)),
            ("Total amount claimed: $4,850.00", 0.90, _box(40, 200, 520, 240)),
        ]
    )
    apply_ocr(doc, engine, mode="always")
    return doc


# ── OCR line boxes survive onto the Page ───────────────────────────────────
def test_apply_ocr_keeps_line_boxes_on_the_page():
    doc = _ocr_document()
    page = doc.pages[0]
    assert page.text_source == "ocr"
    assert len(page.ocr_lines) == 3
    assert page.ocr_lines[1]["text"] == "NOTICE TO OWNER"
    assert page.ocr_lines[1]["box"][0] == [40, 100]


def test_page_text_is_exactly_the_joined_lines():
    # The offset → line mapping depends on this being true.
    page = _ocr_document().pages[0]
    assert page.text == "\n".join(line["text"] for line in page.ocr_lines)


# ── locate=True ────────────────────────────────────────────────────────────
def test_locate_off_by_default_collects_nothing():
    doc = _ocr_document()
    res = match_text(doc, "NOTICE TO OWNER", "contains")
    assert res.found is True
    assert res.hits == []
    assert res.regions == []


def test_locate_maps_a_contains_hit_to_its_ocr_line():
    doc = _ocr_document()
    res = match_text(doc, "NOTICE TO OWNER", "contains", locate=True,
                     label="Notice to Owner")
    assert [(h.page, h.text) for h in res.hits] == [(0, "NOTICE TO OWNER")]
    assert len(res.regions) == 1
    region = res.regions[0]
    assert region.source == "ocr"
    assert region.kind == "polygon"
    assert region.label == "Notice to Owner"
    assert region.score == 0.95
    assert region.points[0] == (40.0, 100.0)
    assert region.attrs["line"] == 1


def test_locate_label_defaults_to_the_pattern():
    doc = _ocr_document()
    res = match_text(doc, "ACME", "contains", locate=True)
    assert res.regions[0].label == "ACME"


def test_locate_works_for_regex_and_fuzzy():
    doc = _ocr_document()
    regex = match_text(doc, r"\$[\d,]+\.\d{2}", "regex", locate=True)
    assert regex.hits[0].text == "$4,850.00"
    assert regex.regions[0].attrs["line"] == 2

    # Fuzzy windows slide over the page text, newlines included, so a loose
    # threshold can also clip the tail of the line above — assert the right
    # line is among the regions rather than that it is the only one.
    fuzzy = match_text(doc, "Notlce to 0wner", "fuzzy", fuzzy_threshold=0.7, locate=True)
    assert fuzzy.found is True
    assert 1 in {r.attrs["line"] for r in fuzzy.regions}
    assert all(0.0 < r.attrs["ratio"] <= 1.0 for r in fuzzy.regions)


def test_a_hit_spanning_two_lines_yields_one_region_per_line():
    doc = _ocr_document()
    # "LLC\nNOTICE" straddles the join between line 0 and line 1.
    res = match_text(doc, "LLC\nNOTICE", "contains", locate=True)
    assert res.found is True
    assert sorted(r.attrs["line"] for r in res.regions) == [0, 1]


def test_no_regions_when_the_page_has_no_line_boxes():
    doc = Document(kind="txt", pages=[Page(index=0, text="NOTICE TO OWNER",
                                           text_source="native")])
    res = match_text(doc, "NOTICE", "contains", locate=True)
    assert res.hits and res.regions == []


def test_ocr_line_regions_respects_its_cap():
    doc = _ocr_document()
    res = match_text(doc, "O", "contains", locate=True)
    capped = ocr_line_regions(doc.pages[0], res.hits, "letter O", max_regions=2)
    assert len(capped) == 2


# ── Native PDF rectangles ──────────────────────────────────────────────────
@pytest.fixture
def native_pdf() -> bytes:
    pymupdf = pytest.importorskip("pymupdf")
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((60, 100), "NOTICE TO OWNER", fontsize=16)
    page.insert_text((60, 160), "Total Due $4,850.00", fontsize=12)
    blob = doc.tobytes()
    doc.close()
    return blob


def test_pdf_text_regions_locates_a_literal_phrase(native_pdf):
    doc = load_document(native_pdf, "invoice.pdf", keep_source=True)
    assert doc.source_bytes == native_pdf
    page = doc.pages[0]

    res = match_text(doc, "NOTICE TO OWNER", "contains", locate=True)
    regions = pdf_text_regions(
        doc.source_bytes, page, res.hits, pattern="NOTICE TO OWNER",
        mode="contains", label="Notice to Owner",
    )
    assert len(regions) == 1
    region = regions[0]
    assert region.source == "pdf-text"
    assert region.kind == "box"
    assert region.label == "Notice to Owner"
    assert region.score == 1.0
    # pdf_rect is in points; the region itself is in rendered page pixels, so
    # at 150 dpi the pixel x is about twice the point x.
    x_pt = region.attrs["pdf_rect"][0]
    assert 55 < x_pt < 65
    assert region.points[0][0] > x_pt


def test_pdf_text_regions_reconstructs_a_regex_hit_from_word_spans(native_pdf):
    doc = load_document(native_pdf, "invoice.pdf", keep_source=True)
    res = match_text(doc, r"\$[\d,]+\.\d{2}", "regex", locate=True)
    regions = pdf_text_regions(
        doc.source_bytes, doc.pages[0], res.hits,
        pattern=r"\$[\d,]+\.\d{2}", mode="regex", label="total amount",
    )
    assert len(regions) == 1
    assert regions[0].attrs["text"] == "$4,850.00"
    assert regions[0].attrs["pdf_rect"][1] > 100  # below the first line


def test_pdf_text_regions_returns_empty_rather_than_raising(native_pdf):
    doc = load_document(native_pdf, "invoice.pdf", keep_source=True)
    # Nothing to find …
    assert pdf_text_regions(doc.source_bytes, doc.pages[0], [],
                            pattern="LIEN WAIVER", mode="contains") == []
    # … no bytes kept …
    assert pdf_text_regions(b"", doc.pages[0], [], pattern="x", mode="contains") == []
    # … and a page index past the end of the file.
    ghost = Page(index=9, width=100, height=100)
    assert pdf_text_regions(doc.source_bytes, ghost, [], pattern="x",
                            mode="contains") == []


def test_keep_source_is_off_by_default(native_pdf):
    assert load_document(native_pdf, "invoice.pdf").source_bytes is None
