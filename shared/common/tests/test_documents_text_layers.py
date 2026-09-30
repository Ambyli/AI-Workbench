"""Tests for text layers held outside the page.

``recognize_text_layer`` is the non-mutating sibling of ``apply_ocr``: it
returns a ``TextLayer`` and leaves the page alone, so a service can keep one
text layer per OCR setting of the same document. ``with_text_layers`` builds
a view carrying them, and ``match_text_layers`` searches one — the results,
located regions included, must be exactly what ``apply_ocr`` + ``match_text``
would have produced.
"""

from __future__ import annotations

import numpy as np
import pytest

from common.documents import (
    Document,
    OCRResult,
    Page,
    TextLayer,
    apply_ocr,
    match_text,
    match_text_layers,
    needs_recognition,
    pdf_page_count,
    recognize_text_layer,
    with_text_layers,
)

from .documents_fixtures import make_document, make_pdf


def _box(x1, y1, x2, y2):
    return [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]


class _Engine:
    """Fixed lines with known boxes; counts its calls."""

    def __init__(self, lines=None):
        self.lines = lines if lines is not None else [
            ("ACME ROOFING LLC", 0.99, _box(40, 30, 380, 70)),
            ("NOTICE TO OWNER", 0.95, _box(40, 100, 360, 140)),
        ]
        self.calls = 0

    def recognize(self, image_bgr):
        self.calls += 1
        if not self.lines:
            return OCRResult()
        detail = [{"text": t, "confidence": c, "box": b} for t, c, b in self.lines]
        return OCRResult(
            text="\n".join(d["text"] for d in detail),
            confidence=sum(d["confidence"] for d in detail) / len(detail),
            lines=detail,
        )


def _image_page(text: str = "") -> Page:
    return Page(
        index=0,
        image_bgr=np.zeros((400, 600, 3), dtype="uint8"),
        text=text,
        text_source="native" if text.strip() else "none",
        width=600,
        height=400,
    )


# ── recognize_text_layer never mutates ─────────────────────────────────────


def test_recognize_returns_an_ocr_layer_and_leaves_the_page_alone():
    page = _image_page()
    engine = _Engine()
    layer = recognize_text_layer(page, engine, mode="auto")

    assert layer.source == "ocr"
    assert layer.text == "ACME ROOFING LLC\nNOTICE TO OWNER"
    assert layer.confidence == pytest.approx(0.97)
    assert len(layer.lines) == 2
    # The page is exactly as it was.
    assert page.text == "" and page.text_source == "none"
    assert page.ocr_lines == [] and page.ocr_confidence is None


def test_auto_keeps_a_native_layer_without_calling_the_engine():
    page = _image_page("This page already has plenty of native text on it.")
    engine = _Engine()
    layer = recognize_text_layer(page, engine, mode="auto", min_native_chars=20)
    assert engine.calls == 0
    assert layer.source == "native" and layer.text == page.text


def test_always_recognises_even_over_native_text():
    page = _image_page("native text that is long enough to count")
    layer = recognize_text_layer(page, _Engine(), mode="always")
    assert layer.source == "ocr"
    assert page.text_source == "native"  # still untouched


def test_never_and_missing_engine_return_the_page_layer():
    page = _image_page()
    engine = _Engine()
    assert recognize_text_layer(page, engine, mode="never").source == "none"
    assert recognize_text_layer(page, None, mode="always").source == "none"
    assert engine.calls == 0


def test_an_empty_recognition_does_not_pretend_to_be_ocr():
    page = _image_page("12")
    layer = recognize_text_layer(page, _Engine(lines=[]), mode="always")
    assert layer.source == "native" and layer.text == "12"


def test_a_page_without_an_image_is_never_recognised():
    page = Page(index=0, text="", text_source="none")
    engine = _Engine()
    assert recognize_text_layer(page, engine, mode="always").source == "none"
    assert engine.calls == 0
    assert needs_recognition(page, "always") is False


def test_needs_recognition_is_the_rule_apply_ocr_uses():
    doc = make_document(["plenty of native text on this first page", ""], with_images=True)
    decisions = [needs_recognition(p, "auto", 20) for p in doc.pages]
    assert decisions == [False, True]
    assert apply_ocr(doc, _Engine(), mode="auto", min_native_chars=20) == 1


# ── Views and search ───────────────────────────────────────────────────────


def test_with_text_layers_builds_a_view_and_shares_the_image():
    page = _image_page()
    doc = Document(kind="image", pages=[page])
    layer = recognize_text_layer(page, _Engine(), mode="always")
    view = with_text_layers(doc, [layer])

    assert view is not doc
    assert view.pages[0].text_source == "ocr"
    assert view.pages[0].image_bgr is page.image_bgr  # shared, not copied
    assert doc.pages[0].text_source == "none"  # original untouched


def test_a_page_without_a_layer_keeps_its_own_text():
    doc = make_document(["first page text", "second page text"])
    view = with_text_layers(doc, {1: TextLayer(page=1, text="replaced", source="ocr")})
    assert [p.text for p in view.pages] == ["first page text", "replaced"]


def test_match_text_layers_equals_apply_ocr_then_match_text():
    lines = [
        ("ACME ROOFING LLC", 0.99, _box(40, 30, 380, 70)),
        ("NOTICE TO OWNER", 0.95, _box(40, 100, 360, 140)),
    ]
    # Path 1: mutate, then search.
    mutated = Document(kind="image", pages=[_image_page()])
    apply_ocr(mutated, _Engine(lines), mode="always")
    expected = match_text(mutated, "notice to owner", "contains", locate=True, label="n")

    # Path 2: the layer, searched without mutation.
    pristine = Document(kind="image", pages=[_image_page()])
    layer = recognize_text_layer(pristine.pages[0], _Engine(lines), mode="always")
    got = match_text_layers(
        pristine, [layer], "notice to owner", "contains", locate=True, label="n"
    )

    assert got.as_dict() == expected.as_dict()
    assert [r.as_dict() for r in got.regions] == [r.as_dict() for r in expected.regions]
    assert got.regions and got.regions[0].source == "ocr"
    assert pristine.pages[0].text == ""


def test_two_layers_of_one_page_coexist():
    """The point of the whole exercise: two settings, one document, no clash."""
    page = _image_page("short")
    doc = Document(kind="image", pages=[page])
    native = recognize_text_layer(page, _Engine(), mode="never")
    ocr = recognize_text_layer(page, _Engine(), mode="always")

    assert match_text_layers(doc, [native], "NOTICE").found is False
    assert match_text_layers(doc, [ocr], "NOTICE").found is True
    assert match_text_layers(doc, [native], "short").found is True


def test_text_layer_summary_is_json_safe():
    layer = TextLayer(page=0, text=" abc ", source="ocr", confidence=0.5, lines=[{}])
    assert layer.chars() == 3
    assert layer.as_dict() == {
        "page": 0, "source": "ocr", "chars": 3, "confidence": 0.5, "lines": 1
    }


# ── pdf_page_count ─────────────────────────────────────────────────────────


def test_pdf_page_count_reads_the_count_without_rendering():
    pytest.importorskip("pymupdf")
    assert pdf_page_count(make_pdf(["one"])) == 1
    assert pdf_page_count(make_pdf(["one", "two", "three"])) == 3


def test_pdf_page_count_rejects_garbage():
    pytest.importorskip("pymupdf")
    from common.documents import UnsupportedDocumentError

    with pytest.raises(UnsupportedDocumentError):
        pdf_page_count(b"%PDF-1.4 this is not a pdf at all")
