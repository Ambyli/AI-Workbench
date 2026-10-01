"""Tests for common.documents.ocr — apply_ocr policy and the engine contract.

The policy tests use a fake engine so the suite never downloads or runs the
real ONNX models. One opt-in test exercises RapidOCR itself and skips when
the ``documents`` extra is not installed.
"""

from __future__ import annotations

import pytest

from common.documents import OCREngine, OCRResult, apply_ocr, load_document, preprocess_for_ocr

from .documents_fixtures import make_document, make_scanned_pdf, make_text_image


class FakeEngine:
    """An ``OCREngine`` that returns canned text and counts its calls."""

    def __init__(self, text: str = "RECOGNISED TEXT", confidence: float = 0.9) -> None:
        self.text = text
        self.confidence = confidence
        self.calls = 0

    def recognize(self, image_bgr) -> OCRResult:
        self.calls += 1
        if not self.text:
            return OCRResult()
        return OCRResult(
            text=self.text,
            confidence=self.confidence,
            lines=[{"text": self.text, "confidence": self.confidence, "box": None}],
        )


def test_fake_engine_satisfies_the_protocol() -> None:
    assert isinstance(FakeEngine(), OCREngine)


def test_auto_mode_only_ocrs_pages_without_native_text() -> None:
    doc = make_document(["This page already has plenty of native text.", ""], with_images=True)
    engine = FakeEngine()
    replaced = apply_ocr(doc, engine, mode="auto")
    assert replaced == 1
    assert engine.calls == 1
    assert doc.pages[0].text_source == "native"
    assert doc.pages[1].text_source == "ocr"
    assert doc.pages[1].text == "RECOGNISED TEXT"
    assert doc.pages[1].ocr_confidence == pytest.approx(0.9)


def test_min_native_chars_threshold_is_respected() -> None:
    doc = make_document(["1"], with_images=True)  # a page number is not a text layer
    engine = FakeEngine()
    assert apply_ocr(doc, engine, mode="auto", min_native_chars=20) == 1
    assert apply_ocr(make_document(["1"], with_images=True), engine, mode="auto", min_native_chars=1) == 0


def test_always_mode_replaces_native_text() -> None:
    doc = make_document(["native text that is long enough to count"], with_images=True)
    assert apply_ocr(doc, FakeEngine(), mode="always") == 1
    assert doc.pages[0].text_source == "ocr"


def test_never_mode_and_missing_engine_are_no_ops() -> None:
    doc = make_document([""], with_images=True)
    engine = FakeEngine()
    assert apply_ocr(doc, engine, mode="never") == 0
    assert engine.calls == 0
    assert apply_ocr(doc, None, mode="always") == 0
    assert doc.pages[0].text_source == "none"


def test_pages_without_images_are_skipped() -> None:
    doc = make_document([""], with_images=False)  # a .docx/.txt style page
    engine = FakeEngine()
    assert apply_ocr(doc, engine, mode="always") == 0
    assert engine.calls == 0


def test_empty_recognition_leaves_the_page_untouched() -> None:
    doc = make_document([""], with_images=True)
    assert apply_ocr(doc, FakeEngine(text=""), mode="always") == 0
    assert doc.pages[0].text_source == "none"
    assert doc.has_text() is False


def test_ocr_result_is_falsey_when_empty() -> None:
    assert not OCRResult()
    assert OCRResult(text="something")


def test_preprocess_grayscales_and_upscales() -> None:
    import numpy as np

    small = np.zeros((50, 80, 3), dtype=np.uint8)
    small[:, :, 2] = 200  # red channel only, so grayscaling is observable
    out = preprocess_for_ocr(small, min_long_side=400)
    assert max(out.shape[:2]) >= 400
    # All three channels equal after grayscaling.
    assert np.array_equal(out[:, :, 0], out[:, :, 1])
    assert np.array_equal(out[:, :, 1], out[:, :, 2])


def test_preprocess_leaves_large_images_alone() -> None:
    import numpy as np

    big = np.zeros((1200, 1600, 3), dtype=np.uint8)
    assert preprocess_for_ocr(big, min_long_side=1000).shape[:2] == (1200, 1600)


# ---------------------------------------------------------------------------
# Real engine — opt-in, skipped when rapidocr/onnxruntime are absent
# ---------------------------------------------------------------------------


def test_rapidocr_reads_a_rendered_page() -> None:
    """End-to-end check against the real recogniser.

    Skipped unless ``rapidocr`` + ``onnxruntime`` are installed (the
    ``documents`` extra); the first run also downloads the PP-OCRv6 models.
    """
    pytest.importorskip("rapidocr", reason="documents extra not installed")
    pytest.importorskip("onnxruntime", reason="documents extra not installed")

    from common.documents import RapidOCREngine

    # A realistic page: several lines, as any scanned document has. One short
    # line alone on a letter-size page is a detector edge case, not a test.
    body = "\n".join(["ACME ROOFING LLC", "Notice to Owner", "Total Due: $4,850.00"])
    doc = load_document(make_scanned_pdf([body]), filename="scan.pdf")
    assert not doc.has_text()  # nothing to read without OCR

    replaced = apply_ocr(doc, RapidOCREngine(), mode="auto")
    assert replaced == 1
    assert doc.pages[0].text_source == "ocr"
    assert "owner" in doc.full_text().lower()
    assert 0.0 < (doc.pages[0].ocr_confidence or 0.0) <= 1.0


def test_rapidocr_reads_a_photographed_line() -> None:
    """Same engine, but straight from a PNG of rendered text."""
    pytest.importorskip("rapidocr", reason="documents extra not installed")
    pytest.importorskip("onnxruntime", reason="documents extra not installed")

    from common.documents import RapidOCREngine

    doc = load_document(make_text_image(["CASE-77813"]), filename="letter.png")
    apply_ocr(doc, RapidOCREngine(), mode="always")
    assert "77813" in doc.full_text()
