"""Tests for common.documents.detect — magic-byte format detection.

Fixtures are generated programmatically (fitz / python-docx / PIL) so the
repo carries no binary test assets and every case is reproducible.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from common.documents import UnsupportedDocumentError, detect_kind, guess_content_type

from .documents_fixtures import make_docx, make_jpeg, make_pdf, make_png


def test_detects_png_and_jpeg() -> None:
    assert detect_kind(make_png()) == "image"
    assert detect_kind(make_jpeg()) == "image"


def test_guess_content_type_distinguishes_png_from_jpeg() -> None:
    assert guess_content_type("image", make_png()) == "image/png"
    assert guess_content_type("image", make_jpeg()) == "image/jpeg"
    assert guess_content_type("pdf", b"%PDF-1.7") == "application/pdf"
    assert guess_content_type("txt", b"hello") == "text/plain"
    assert guess_content_type("docx", b"PK\x03\x04").endswith("wordprocessingml.document")


def test_detects_pdf() -> None:
    assert detect_kind(make_pdf(["hello"])) == "pdf"


def test_detects_pdf_with_leading_junk() -> None:
    # Some producers emit bytes before the header; Acrobat tolerates it.
    raw = b"\n\n" + make_pdf(["hello"])
    assert detect_kind(raw) == "pdf"


def test_detects_docx() -> None:
    assert detect_kind(make_docx(["hello"])) == "docx"


def test_zip_without_word_part_is_rejected() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("xl/workbook.xml", "<workbook/>")
    with pytest.raises(UnsupportedDocumentError) as exc:
        detect_kind(buf.getvalue(), filename="book.xlsx")
    assert "word/document.xml" in str(exc.value)


def test_detects_txt_including_bom_and_unicode() -> None:
    assert detect_kind(b"Limited Warranty\nDated 2026-01-05\n") == "txt"
    assert detect_kind("﻿Café notice\n".encode("utf-8")) == "txt"


def test_binary_that_decodes_but_has_nul_is_not_txt() -> None:
    with pytest.raises(UnsupportedDocumentError):
        detect_kind(b"plain text\x00\x00 with nulls")


def test_legacy_doc_gets_a_specific_message() -> None:
    ole = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64
    with pytest.raises(UnsupportedDocumentError) as exc:
        detect_kind(ole, filename="proposal.doc")
    message = str(exc.value)
    assert ".doc" in message and ".docx" in message


def test_empty_input_is_rejected() -> None:
    with pytest.raises(UnsupportedDocumentError) as exc:
        detect_kind(b"")
    assert "Empty" in str(exc.value)


def test_unknown_binary_is_rejected_with_hex_hint() -> None:
    with pytest.raises(UnsupportedDocumentError) as exc:
        detect_kind(b"\x1f\x8b\x08\x00\x01\x02\x03\x04binary", filename="thing.gz")
    assert "1f8b0800" in str(exc.value)


def test_filename_and_content_type_do_not_override_bytes() -> None:
    # A PNG uploaded as application/pdf is still a PNG.
    assert detect_kind(make_png(), filename="invoice.pdf", content_type="application/pdf") == "image"
