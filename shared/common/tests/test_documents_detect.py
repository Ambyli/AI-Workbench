"""Tests for common.documents.detect — magic-byte format detection.

Fixtures are generated programmatically (fitz / python-docx / PIL) so the
repo carries no binary test assets and every case is reproducible.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from common.documents import UnsupportedDocumentError, detect_kind, guess_content_type

from .documents_fixtures import make_docx, make_jpeg, make_pdf, make_png, make_svg


def test_detects_png_and_jpeg() -> None:
    assert detect_kind(make_png()) == "image"
    assert detect_kind(make_jpeg()) == "image"


def test_guess_content_type_distinguishes_png_from_jpeg() -> None:
    assert guess_content_type("image", make_png()) == "image/png"
    assert guess_content_type("image", make_jpeg()) == "image/jpeg"
    assert guess_content_type("pdf", b"%PDF-1.7") == "application/pdf"
    assert guess_content_type("txt", b"hello") == "text/plain"
    assert guess_content_type("docx", b"PK\x03\x04").endswith("wordprocessingml.document")
    assert guess_content_type("svg", make_svg()) == "image/svg+xml"


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
    # An ELF header: binary, not one of the near-misses with their own message.
    with pytest.raises(UnsupportedDocumentError) as exc:
        detect_kind(b"\x7fELF\x02\x01\x01\x00binary", filename="thing.bin")
    assert "7f454c4602010100" in str(exc.value)
    assert "SVG" in str(exc.value)  # the "Supported:" list names every kind


# ---------------------------------------------------------------------------
# SVG — checked before the txt fallback
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        make_svg('<rect width="10" height="10"/>'),
        make_svg(prolog='<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'),
        make_svg(
            prolog='<?xml version="1.0"?>\n<!DOCTYPE svg [\n'
                   '  <!ENTITY logo "<image href=\'x.png\'/>">\n'
                   '  <!ELEMENT svg ANY>\n]>\n'
        ),
        make_svg(prolog='<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" '
                        '"http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">\n'),
        make_svg(prolog="<!-- Generator: Illustrator 27.0 -->\n\n"),
        b"\xef\xbb\xbf" + make_svg(),
        b'<s:svg xmlns:s="http://www.w3.org/2000/svg" width="10" height="10"/>',
        b'<?xml-stylesheet type="text/css" href="a.css"?>\n<svg/>',
    ],
    ids=["plain", "xml-decl", "doctype-subset", "doctype-public", "comment", "bom",
         "prefixed-root", "processing-instruction"],
)
def test_svg_is_detected(raw: bytes) -> None:
    assert detect_kind(raw) == "svg"


def test_svg_detection_ignores_the_declared_type() -> None:
    # An SVG uploaded as text/plain is still an SVG.
    assert detect_kind(make_svg(), filename="logo.txt", content_type="text/plain") == "svg"


def test_text_that_mentions_svg_stays_txt() -> None:
    assert detect_kind(b"Embed it with <svg width='10'> like this.\n") == "txt"
    assert detect_kind(b"notes\n<svg xmlns='http://www.w3.org/2000/svg'/>\n") == "txt"


def test_html_with_inline_svg_stays_txt() -> None:
    html = b"<!DOCTYPE html>\n<html><body><svg width='10' height='10'></svg></body></html>"
    assert detect_kind(html) == "txt"


def test_an_svgfoo_root_is_not_svg() -> None:
    assert detect_kind(b"<svgfoo>hello</svgfoo>") == "txt"


def test_svg_with_a_nul_byte_is_not_svg() -> None:
    with pytest.raises(UnsupportedDocumentError):
        detect_kind(make_svg() + b"\x00")


def test_gzip_gets_the_svgz_message() -> None:
    import gzip

    with pytest.raises(UnsupportedDocumentError) as exc:
        detect_kind(gzip.compress(make_svg()), filename="logo.svgz")
    message = str(exc.value)
    assert ".svgz" in message and "decompress" in message


def test_filename_and_content_type_do_not_override_bytes() -> None:
    # A PNG uploaded as application/pdf is still a PNG.
    assert detect_kind(make_png(), filename="invoice.pdf", content_type="application/pdf") == "image"
