"""Tests for common.documents.loaders — bytes → Document for every kind."""

from __future__ import annotations

import pytest

from common.documents import UnsupportedDocumentError, load_document

from .documents_fixtures import make_docx, make_png, make_pdf, make_scanned_pdf


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------


def test_image_loads_as_one_page_with_no_text() -> None:
    doc = load_document(make_png((300, 200)), filename="photo.png")
    assert doc.kind == "image"
    assert doc.content_type == "image/png"
    assert len(doc.pages) == 1
    page = doc.pages[0]
    assert page.image_bgr is not None
    assert (page.width, page.height) == (300, 200)
    assert page.text_source == "none"
    assert doc.has_images() and not doc.has_text()


def test_custom_image_decoder_is_used() -> None:
    import numpy as np

    calls: list[int] = []

    def decoder(raw: bytes):
        calls.append(len(raw))
        return np.zeros((40, 60, 3), dtype=np.uint8)

    doc = load_document(make_png(), filename="p.png", image_decoder=decoder)
    assert calls, "injected decoder was not called"
    assert (doc.pages[0].width, doc.pages[0].height) == (60, 40)


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------


def test_pdf_native_text_per_page_plus_renders() -> None:
    doc = load_document(
        make_pdf(["Invoice #INV-2026-0042", "Payment Terms: Net 30"]),
        filename="invoice.pdf",
    )
    assert doc.kind == "pdf"
    assert doc.content_type == "application/pdf"
    assert len(doc.pages) == 2
    assert "INV-2026-0042" in doc.pages[0].text
    assert "Net 30" in doc.pages[1].text
    assert all(p.text_source == "native" for p in doc.pages)
    # Every page also carries a render for CV / vision use.
    assert len(doc.page_images()) == 2
    assert doc.pages[0].width > 0 and doc.pages[0].height > 0
    assert doc.full_text().count("Invoice") == 1
    assert doc.truncated_pages == 0


def test_pdf_render_dpi_changes_page_pixel_size() -> None:
    raw = make_pdf(["one page"])
    low = load_document(raw, render_dpi=72).pages[0]
    high = load_document(raw, render_dpi=150).pages[0]
    assert high.width > low.width


def test_pdf_max_pages_truncates_and_records_the_count() -> None:
    doc = load_document(make_pdf(["a", "b", "c", "d"]), max_pages=2)
    assert len(doc.pages) == 2
    assert doc.truncated_pages == 2


def test_scanned_pdf_has_images_but_no_text_layer() -> None:
    doc = load_document(make_scanned_pdf(["Notice to Owner"]), filename="scan.pdf")
    assert doc.kind == "pdf"
    assert doc.has_images()
    assert not doc.has_text()
    assert doc.pages[0].text_source == "none"


def test_corrupt_pdf_raises_unsupported() -> None:
    with pytest.raises(UnsupportedDocumentError):
        load_document(b"%PDF-1.7\nnot actually a pdf", filename="broken.pdf")


# ---------------------------------------------------------------------------
# TXT
# ---------------------------------------------------------------------------


def test_txt_loads_as_one_textual_page_with_no_image() -> None:
    raw = "Limited Warranty\n\nEffective 2026-01-05.\n".encode("utf-8")
    doc = load_document(raw, filename="contract.txt")
    assert doc.kind == "txt"
    assert doc.content_type == "text/plain"
    assert doc.pages[0].image_bgr is None
    assert not doc.has_images()
    assert "Limited Warranty" in doc.full_text()
    assert doc.pages[0].text_source == "native"


def test_txt_bom_is_stripped() -> None:
    doc = load_document("﻿Limited Warranty".encode("utf-8"), filename="bom.txt")
    assert doc.full_text().startswith("Limited Warranty")


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------


def test_docx_paragraphs_and_table_cells_in_document_order() -> None:
    raw = make_docx(
        ["Roofix Proposal", "Prepared for the homeowner."],
        table_rows=[["Component", "Value"], ["System Size", "8.4 kW"]],
        trailing_paragraph="Signature",
    )
    doc = load_document(raw, filename="proposal.docx")
    assert doc.kind == "docx"
    assert doc.content_type.endswith("wordprocessingml.document")
    assert not doc.has_images()
    text = doc.full_text()
    assert "Roofix Proposal" in text
    # Table rows are flattened to "cell | cell" so a row reads as one line.
    assert "System Size | 8.4 kW" in text
    # Document order: heading, then table, then the paragraph after it.
    assert text.index("Roofix Proposal") < text.index("System Size") < text.index("Signature")


def test_docx_empty_paragraphs_are_dropped() -> None:
    doc = load_document(make_docx(["First", "", "   ", "Second"]), filename="d.docx")
    assert doc.full_text().splitlines() == ["First", "Second"]


# ---------------------------------------------------------------------------
# Dispatch / errors
# ---------------------------------------------------------------------------


def test_legacy_doc_is_rejected_by_load_document() -> None:
    with pytest.raises(UnsupportedDocumentError) as exc:
        load_document(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 32, filename="old.doc")
    assert ".docx" in str(exc.value)


def test_document_metadata_records_size_and_filename() -> None:
    raw = make_pdf(["hello"])
    doc = load_document(raw, filename="hello.pdf")
    assert doc.size_bytes == len(raw)
    assert doc.filename == "hello.pdf"


# ---------------------------------------------------------------------------
# PyMuPDF from worker threads
# ---------------------------------------------------------------------------


def test_pdfs_load_correctly_from_many_threads_at_once() -> None:
    """Services call these loaders via asyncio.to_thread, several jobs at once.
    PyMuPDF is not thread-safe, so every call holds the module's lock; the
    threaded result must be exactly the serial one."""
    from concurrent.futures import ThreadPoolExecutor

    import numpy as np

    from common.documents import pdf_page_count

    raw = make_pdf(["alpha page", "beta page", "gamma page"])
    serial = load_document(raw, filename="s.pdf")

    def load(_: int):
        assert pdf_page_count(raw) == 3
        return load_document(raw, filename="t.pdf")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(load, range(16)))

    for doc in results:
        assert [p.text for p in doc.pages] == [p.text for p in serial.pages]
        for got, want in zip(doc.pages, serial.pages):
            assert (got.width, got.height) == (want.width, want.height)
            assert np.array_equal(got.image_bgr, want.image_bgr)


def test_pdf_calls_hold_the_pymupdf_lock(monkeypatch) -> None:
    """Every PyMuPDF entry point runs under the lock (re-entrant, so the
    check can take it too)."""
    from common.documents import loaders

    held: list[bool] = []
    real_open = __import__("pymupdf").open

    def spy_open(*args, **kwargs):
        # An RLock held by this thread can be re-acquired without blocking;
        # held by nobody it could too — so check ownership directly.
        held.append(loaders._PYMUPDF_LOCK._is_owned())
        return real_open(*args, **kwargs)

    raw = make_pdf(["one"])  # before the spy: the fixture itself opens PyMuPDF
    monkeypatch.setattr("pymupdf.open", spy_open)
    load_document(raw, filename="x.pdf")
    loaders.pdf_page_count(raw)
    assert held == [True, True]


def test_pdf_page_size_matches_the_render_without_rendering() -> None:
    """The size a page WILL render to, read from its rectangle — exactly what
    load_document's raster gives, at the default and at another DPI."""
    import pytest as _pytest

    from common.documents import pdf_page_size

    raw = make_pdf(["alpha page", "beta page"])
    for dpi in (150, 97):
        doc = load_document(raw, filename="s.pdf", render_dpi=dpi)
        for page in doc.pages:
            assert pdf_page_size(raw, page.index, render_dpi=dpi) == (page.width, page.height)
    with _pytest.raises(IndexError):
        pdf_page_size(raw, 2)
    with _pytest.raises(UnsupportedDocumentError):
        pdf_page_size(b"%PDF-1.4 not really", 0)
