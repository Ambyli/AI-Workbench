"""Format detection by content, not by filename or Content-Type header.

Any caller can upload ``invoice.pdf`` with ``Content-Type: image/png``, so the
bytes themselves are the only trustworthy signal. ``detect_kind`` sniffs magic
signatures first and only falls back to a structural check (ZIP directory for
.docx, decodability for .txt) when the leading bytes are ambiguous.

Supported kinds and how they are recognised:

    image  — JPEG ``FF D8 FF`` or PNG ``89 50 4E 47 0D 0A 1A 0A``
    pdf    — ``%PDF-`` (allowing the leading junk some producers emit)
    docx   — ZIP magic ``PK\\x03\\x04`` whose central directory contains
             ``word/document.xml`` (an .xlsx/.pptx ZIP is therefore rejected)
    txt    — decodes as UTF-8 (BOM tolerated), contains no NUL bytes, and is
             overwhelmingly printable

Anything else raises ``UnsupportedDocumentError``. Legacy ``.doc`` (OLE2
compound files, magic ``D0 CF 11 E0 A1 B1 1A E1``) gets its own message
telling the caller to convert to .docx, because "unsupported file" is not a
useful answer when the user is holding a Word document.

Process flow position: called by ``loaders.load_document`` before dispatch;
services also call it directly to reject an upload before queueing work.
"""

from __future__ import annotations

import io
import zipfile
from typing import Literal, Optional

DocumentKind = Literal["image", "pdf", "txt", "docx"]

# Leading signatures that identify a kind outright.
JPEG_MAGIC = b"\xff\xd8\xff"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
PDF_MAGIC = b"%PDF-"
ZIP_MAGIC = b"PK\x03\x04"
# OLE2 compound document — legacy .doc/.xls/.ppt. Detected only to produce a
# better error than "unrecognised file".
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

# The member every .docx has; its absence means the ZIP is some other OOXML
# (or just a zip archive) and we do not claim to read it.
DOCX_MARKER = "word/document.xml"

# Some PDF producers prepend junk before %PDF-; Acrobat itself tolerates the
# header appearing anywhere in the first kilobyte, so we do too.
_PDF_SCAN_WINDOW = 1024

# A candidate .txt must be at least this fraction printable (after decoding)
# to be accepted. Control characters beyond the usual whitespace set mean the
# bytes are really some binary format that happens to decode.
_TXT_PRINTABLE_RATIO = 0.95

# Canonical MIME type per kind — used to fill Document.content_type so the
# rest of the system reports what the file actually is, not what the caller
# claimed it was.
CONTENT_TYPES: dict[str, str] = {
    "pdf": "application/pdf",
    "txt": "text/plain",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

# File extensions per kind, for introspection endpoints and error messages.
EXTENSIONS: dict[str, list[str]] = {
    "image": [".jpg", ".jpeg", ".png"],
    "pdf": [".pdf"],
    "txt": [".txt"],
    "docx": [".docx"],
}


class UnsupportedDocumentError(ValueError):
    """Raised when the bytes are not one of the supported document kinds.

    Carries a human-readable message intended to be surfaced straight to the
    API caller (services map it to HTTP 400).
    """


def _looks_like_text(raw: bytes) -> bool:
    """True when ``raw`` decodes as UTF-8 and reads as plain text.

    Two guards beyond decodability:
      * a NUL byte anywhere means binary (UTF-16 text, or a truncated binary
        blob that happens to decode), and
      * at least ``_TXT_PRINTABLE_RATIO`` of characters must be printable or
        ordinary whitespace.
    """
    if b"\x00" in raw:
        return False
    try:
        # utf-8-sig strips a BOM when present and behaves like utf-8 otherwise.
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return False
    if not text:
        return True  # an empty file is a (useless but valid) text file
    printable = sum(1 for ch in text if ch.isprintable() or ch in "\r\n\t\f\v")
    return printable / len(text) >= _TXT_PRINTABLE_RATIO


def _is_docx(raw: bytes) -> bool:
    """True when the ZIP archive in ``raw`` contains a Word main document part."""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            return DOCX_MARKER in zf.namelist()
    except (zipfile.BadZipFile, OSError):
        return False


def detect_kind(
    raw: bytes,
    filename: Optional[str] = None,
    content_type: Optional[str] = None,
) -> DocumentKind:
    """Identify what kind of document ``raw`` is, from its bytes.

    ``filename`` and ``content_type`` are accepted for logging and error
    messages only — they never influence the decision, because both are
    caller-controlled.

    Args:
        raw:          The complete file bytes.
        filename:     Original filename, if known (used in error text only).
        content_type: Declared MIME type, if any (used in error text only).

    Returns:
        "image" | "pdf" | "txt" | "docx".

    Raises:
        UnsupportedDocumentError: Empty input, a legacy .doc file, or bytes
            that match none of the supported signatures.
    """
    if not raw:
        raise UnsupportedDocumentError("Empty file — nothing to classify.")

    if raw.startswith(JPEG_MAGIC) or raw.startswith(PNG_MAGIC):
        return "image"

    if raw.startswith(PDF_MAGIC) or PDF_MAGIC in raw[:_PDF_SCAN_WINDOW]:
        return "pdf"

    if raw.startswith(OLE_MAGIC):
        # Legacy Word/Excel/PowerPoint binary container. python-docx cannot
        # read it and we deliberately do not shell out to a converter.
        raise UnsupportedDocumentError(
            "Legacy .doc (OLE2 compound) files are not supported. "
            "Convert the file to .docx (Word: File → Save As → .docx, or "
            "`libreoffice --convert-to docx`) and upload that instead."
        )

    if raw.startswith(ZIP_MAGIC):
        if _is_docx(raw):
            return "docx"
        raise UnsupportedDocumentError(
            "ZIP archive without a Word main document part "
            f"('{DOCX_MARKER}'). Only .docx is supported among OOXML formats "
            "— .xlsx / .pptx / plain .zip uploads are rejected."
        )

    if _looks_like_text(raw):
        return "txt"

    hint = f" (filename={filename!r}, content_type={content_type!r})" if filename or content_type else ""
    raise UnsupportedDocumentError(
        "Unsupported file type"
        f"{hint}. Supported: JPEG/PNG images, PDF, plain text (UTF-8), and "
        f".docx. First bytes were {raw[:8].hex()}."
    )


def guess_content_type(kind: DocumentKind, raw: bytes) -> str:
    """Canonical MIME type for a detected kind.

    Images need the raw bytes to tell JPEG from PNG; every other kind maps
    one-to-one from ``CONTENT_TYPES``.
    """
    if kind == "image":
        return "image/png" if raw.startswith(PNG_MAGIC) else "image/jpeg"
    return CONTENT_TYPES.get(kind, "application/octet-stream")
