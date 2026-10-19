"""Format detection by content, not by filename or Content-Type header.

Any caller can upload ``invoice.pdf`` with ``Content-Type: image/png``, so the
bytes themselves are the only trustworthy signal. ``detect_kind`` sniffs magic
signatures first and only falls back to a structural check (ZIP directory for
.docx, decodability for .txt) when the leading bytes are ambiguous.

Supported kinds and how they are recognised:

    image  — JPEG ``FF D8 FF``, PNG ``89 50 4E 47 0D 0A 1A 0A``, or an
             ISO-BMFF ``ftyp`` box naming a HEIC, AVIF or generic HEIF brand
             (an iPhone photo). ``image_format`` says which of the five
             formats it is; the kind stays ``image`` for all of them, so a
             HEIC counts, loads and is assessed exactly like a JPEG.
    pdf    — ``%PDF-`` (allowing the leading junk some producers emit)
    docx   — ZIP magic ``PK\\x03\\x04`` whose central directory contains
             ``word/document.xml`` (an .xlsx/.pptx ZIP is therefore rejected)
    svg    — UTF-8 XML whose first element is ``<svg>`` (``svg.looks_like_svg``).
             Checked BEFORE txt: an SVG decodes as text too, and calling it
             txt would hand its markup to a text search and a vision model.
    txt    — decodes as UTF-8 (BOM tolerated), contains no NUL bytes, and is
             overwhelmingly printable

Anything else raises ``UnsupportedDocumentError``. Three near-misses get their
own message, because "unsupported file" is not a useful answer when the user
is holding the file: legacy ``.doc`` (OLE2 compound files, magic
``D0 CF 11 E0 A1 B1 1A E1``) is told to convert to .docx, gzip data
(magic ``1F 8B`` — a ``.svgz`` in practice) is told to decompress to .svg,
and an ``ftyp`` box with no image brand (an MP4, or the ``.MOV`` half of an
iPhone Live Photo) is told that video is unsupported and to send the still.
``.svgz`` is refused rather than inflated on purpose: a few kilobytes of gzip
can expand to gigabytes, and supporting it would buy little.

The HEIF family is told apart by brand, not by a fixed signature: the file
opens with an ``ftyp`` box (big-endian uint32 size, ``ftyp``, the major
brand, a minor version, then the compatible brands). A HEIC brand anywhere
wins (a Samsung photo is major ``mif1`` with compatible ``heic``), then an
AVIF brand, then a generic HEIF one (``mif1`` / ``msf1``). Telling the three
apart matters only for the content type and for which decoder must be
present (``loaders.ensure_image_support``); this module never decodes.

Process flow position: called by ``loaders.load_document`` before dispatch;
services also call it directly to reject an upload before queueing work.
"""

from __future__ import annotations

import io
import zipfile
from typing import Literal, Optional

from .svg import looks_like_svg

DocumentKind = Literal["image", "pdf", "txt", "docx", "svg"]
# The formats inside the image kind (``image_format``).
ImageFormat = Literal["jpeg", "png", "heic", "heif", "avif"]

# Leading signatures that identify a kind outright.
JPEG_MAGIC = b"\xff\xd8\xff"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
# ISO base media file format (HEIF, AVIF, MP4, MOV): the first box is
# ``<uint32 size>ftyp<major brand><minor version><compatible brands...>``.
FTYP_TYPE = b"ftyp"
# Brands that make an ftyp an image this package reads. HEIC = HEVC-coded
# HEIF (still and sequence), AVIF = AV1-coded, and the two structural HEIF
# brands that say "image" / "image sequence" without naming a codec.
HEIC_BRANDS = frozenset({b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"hevm", b"hevs"})
AVIF_BRANDS = frozenset({b"avif", b"avis"})
HEIF_GENERIC_BRANDS = frozenset({b"mif1", b"msf1"})
PDF_MAGIC = b"%PDF-"
ZIP_MAGIC = b"PK\x03\x04"
# OLE2 compound document — legacy .doc/.xls/.ppt. Detected only to produce a
# better error than "unrecognised file".
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
# gzip member header — what a .svgz is. Detected only to produce a better
# error than "unrecognised file".
GZIP_MAGIC = b"\x1f\x8b"

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

# A real ftyp lists a handful of compatible brands. Past this many the rest
# are not read: the box's size field is the file's own claim, and a lying one
# must not turn a brand check into a walk over the whole upload.
_MAX_FTYP_BRANDS = 64
# A real ftyp box is a few dozen bytes. A declared size past this is not an
# ftyp at all — in practice a text file whose characters 5-8 happen to be
# "ftyp" ("The ftyp box…"): four printable ASCII bytes read as a size of at
# least 0x20202020, so the cap keeps such a file out of the video refusal.
_MAX_FTYP_BOX = 4096
# Brands named in the "this is a video" message, at most.
_MAX_BRANDS_IN_MESSAGE = 8

# Canonical MIME type per kind — used to fill Document.content_type so the
# rest of the system reports what the file actually is, not what the caller
# claimed it was.
CONTENT_TYPES: dict[str, str] = {
    "pdf": "application/pdf",
    "txt": "text/plain",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "svg": "image/svg+xml",
}
# The image kind's MIME type depends on the format (``image_format``).
IMAGE_CONTENT_TYPES: dict[str, str] = {
    "jpeg": "image/jpeg",
    "png": "image/png",
    "heic": "image/heic",
    "heif": "image/heif",
    "avif": "image/avif",
}

# File extensions per kind, for introspection endpoints and error messages.
EXTENSIONS: dict[str, list[str]] = {
    "image": [".jpg", ".jpeg", ".png", ".heic", ".heif", ".avif"],
    "pdf": [".pdf"],
    "txt": [".txt"],
    "docx": [".docx"],
    "svg": [".svg"],
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


def _ftyp_brands(raw: bytes) -> Optional[tuple[bytes, list[bytes]]]:
    """``(major_brand, compatible_brands)`` when ``raw`` opens with an ISO-BMFF
    ``ftyp`` box, else None.

    Reads only inside the box AND inside the buffer: the size field is the
    file's own claim, so a truncated file or a lying size can shorten the
    brand list but never send the scan past the bytes actually present (nor
    past ``_MAX_FTYP_BRANDS``). A buffer too short to hold the major brand
    and minor version, or a size field smaller than that or larger than
    ``_MAX_FTYP_BOX``, is not an ftyp.
    """
    if len(raw) < 16 or raw[4:8] != FTYP_TYPE:
        return None
    size = int.from_bytes(raw[0:4], "big")
    header = 8
    if size == 1:  # a 64-bit "largesize" follows the type
        if len(raw) < 24:
            return None
        size = int.from_bytes(raw[8:16], "big")
        header = 16
    elif size == 0:  # the box runs to the end of the file
        size = min(len(raw), _MAX_FTYP_BOX)
    if size < header + 8 or size > _MAX_FTYP_BOX:
        return None
    end = min(size, len(raw))
    major = raw[header:header + 4]
    start = header + 8  # past the major brand and the minor version
    count = min(max(0, (end - start) // 4), _MAX_FTYP_BRANDS)
    compatible = [raw[start + 4 * i:start + 4 * i + 4] for i in range(count)]
    return major, compatible


def _brand_text(brand: bytes) -> str:
    """A brand as it goes into an error message: quoted, printable ASCII."""
    return "'" + "".join(ch if " " <= ch <= "~" else "?" for ch in brand.decode("latin-1")) + "'"


def image_format(raw: bytes) -> Optional[ImageFormat]:
    """Which image format ``raw`` is — ``jpeg`` | ``png`` | ``heic`` | ``heif``
    | ``avif`` — or None when it is not one of them.

    JPEG and PNG by their magic bytes. The HEIF family by the ``ftyp`` box's
    brands: a HEIC or AVIF major brand decides outright; otherwise (a generic
    or unknown major brand) the compatible brands are read with HEIC first,
    then AVIF, then a generic HEIF brand. A Samsung photo — major ``mif1``,
    compatible ``heic`` — is therefore ``heic``, and an ftyp with no image
    brand at all (MP4, MOV) is None.

    Pure bytes, no decoder: whether THIS process can decode the format is
    ``loaders.ensure_image_support``'s question.
    """
    if raw.startswith(JPEG_MAGIC):
        return "jpeg"
    if raw.startswith(PNG_MAGIC):
        return "png"
    brands = _ftyp_brands(raw)
    if brands is None:
        return None
    major, compatible = brands
    if major in HEIC_BRANDS:
        return "heic"
    if major in AVIF_BRANDS:
        return "avif"
    compat = set(compatible)
    if compat & HEIC_BRANDS:
        return "heic"
    if compat & AVIF_BRANDS:
        return "avif"
    if major in HEIF_GENERIC_BRANDS or compat & HEIF_GENERIC_BRANDS:
        return "heif"
    return None


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
        "image" | "pdf" | "txt" | "docx" | "svg".

    Raises:
        UnsupportedDocumentError: Empty input, a legacy .doc file, gzip data
            (a .svgz), a video container (an ``ftyp`` with no image brand),
            or bytes that match none of the supported signatures.
    """
    if not raw:
        raise UnsupportedDocumentError("Empty file — nothing to classify.")

    if image_format(raw):
        return "image"

    brands = _ftyp_brands(raw)
    if brands is not None:
        # image_format already said no, so this ISO-BMFF file carries no image
        # brand: an MP4 ('isom', 'mp42', …) or a QuickTime movie ('qt  ') —
        # in practice the video half of an iPhone Live Photo.
        named: list[str] = []
        for brand in (brands[0], *brands[1]):
            text = _brand_text(brand)
            if text not in named:
                named.append(text)
        raise UnsupportedDocumentError(
            f"ISO media file with brands {', '.join(named[:_MAX_BRANDS_IN_MESSAGE])} "
            "and no HEIC/HEIF/AVIF image brand — this is a video (MP4 / MOV, such "
            "as the video half of a Live Photo), and video is not supported. Upload "
            "a still photo instead (JPEG, PNG, HEIC or AVIF)."
        )

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

    if raw.startswith(GZIP_MAGIC):
        # Almost always a .svgz. Inflating it here would make every caller
        # carry a decompression-bomb guard for a format nobody needs to send.
        raise UnsupportedDocumentError(
            "gzip-compressed data (.svgz?) is not supported — decompress it to "
            ".svg (`gunzip -c file.svgz > file.svg`) and upload that instead."
        )

    # Before txt: an SVG is UTF-8 text too, and the first element is what
    # tells the two apart.
    if looks_like_svg(raw):
        return "svg"

    if _looks_like_text(raw):
        return "txt"

    hint = f" (filename={filename!r}, content_type={content_type!r})" if filename or content_type else ""
    raise UnsupportedDocumentError(
        "Unsupported file type"
        f"{hint}. Supported: JPEG/PNG/HEIC/HEIF/AVIF images, PDF, SVG, plain text "
        f"(UTF-8), and .docx. First bytes were {raw[:8].hex()}."
    )


def guess_content_type(kind: DocumentKind, raw: bytes) -> str:
    """Canonical MIME type for a detected kind.

    Images need the raw bytes to tell the format (``image_format`` →
    ``IMAGE_CONTENT_TYPES``); every other kind maps one-to-one from
    ``CONTENT_TYPES``. Bytes that are no image format at all get
    ``application/octet-stream``, never a guessed ``image/jpeg``.
    """
    if kind == "image":
        fmt = image_format(raw)
        return IMAGE_CONTENT_TYPES[fmt] if fmt else "application/octet-stream"
    return CONTENT_TYPES.get(kind, "application/octet-stream")
