"""
common.documents — load JPEG/PNG/PDF/TXT/DOCX into one page-oriented shape.

Services that used to accept "an image" and push a BGR array through OpenCV
and a vision model need very little to accept "a document" instead: something
that turns any of the supported uploads into pages carrying an image, text, or
both. That is this package.

    detect_kind(raw)          → "image" | "pdf" | "txt" | "docx", by magic
                                bytes and structure, never by filename or the
                                caller's Content-Type. Legacy .doc raises a
                                specific "convert to .docx" error.

    load_document(raw, ...)   → ``Document`` with ``pages: list[Page]``. PDFs
                                carry native text plus a render per page (up
                                to ``max_pages``, at ``render_dpi``); images
                                are one page with no text; txt/docx are one
                                page with text and no image. The image decoder
                                is injectable so a caller can keep its own
                                EXIF-aware decode path.

    apply_ocr(doc, engine)    → fills the text layer for pages that lack one
                                (mode ``auto``), or all of them (``always``).
                                ``RapidOCREngine`` is the bundled engine;
                                ``OCREngine`` is the Protocol to implement for
                                anything else (tests use a fake).

    match_text(doc, pattern)  → ``TextMatchResult`` for one of four modes:
                                contains / exact / regex / fuzzy. Fuzzy is the
                                one that survives OCR noise. Pass
                                ``locate=True`` and it also reports WHERE:
                                character offsets in ``hits``, and — for an
                                OCR'd page — the line polygons in ``regions``
                                (``common.vision.Region``).

    pdf_text_regions(...)     → the same question for a NATIVE PDF page, which
                                needs the file itself: PyMuPDF ``search_for``
                                for literal modes, word-span reconstruction
                                for regex/fuzzy. Load with
                                ``keep_source=True`` to have the bytes around
                                for it.

Typical flow::

    kind = detect_kind(raw, filename)                 # 400 on unsupported
    doc  = load_document(raw, filename, max_pages=20)
    apply_ocr(doc, RapidOCREngine(), mode="auto")     # no-op if text is there
    hit  = match_text(doc, "Notice to Owner", "fuzzy")
    for index, image in doc.page_images():            # CV / vision model
        ...

Optional deps (the ``documents`` extra): ``pymupdf`` for PDF, ``python-docx``
for DOCX, ``pillow`` + ``numpy`` for images, ``rapidocr`` + ``onnxruntime``
for the bundled OCR engine. Each is imported lazily at the point of use, so a
consumer that only handles text pays for none of them. ``detect.py`` and
``textmatch.py`` are pure-stdlib and always importable.
"""

from .detect import (
    CONTENT_TYPES,
    EXTENSIONS,
    DocumentKind,
    UnsupportedDocumentError,
    detect_kind,
    guess_content_type,
)
from .loaders import (
    DEFAULT_MAX_PAGES,
    DEFAULT_RENDER_DPI,
    ImageDecoder,
    default_image_decoder,
    load_document,
    pdf_text_regions,
)
from .model import Document, Page, TextSource
from .ocr import (
    DEFAULT_MIN_NATIVE_CHARS,
    OCREngine,
    OCRMode,
    OCRResult,
    RapidOCREngine,
    apply_ocr,
    preprocess_for_ocr,
)
from .textmatch import (
    MAX_PATTERN_CHARS,
    InvalidPatternError,
    MatchMode,
    TextHit,
    TextMatchResult,
    match_text,
    ocr_line_regions,
)

__all__ = [
    # detect
    "CONTENT_TYPES",
    "EXTENSIONS",
    "DocumentKind",
    "UnsupportedDocumentError",
    "detect_kind",
    "guess_content_type",
    # loaders
    "DEFAULT_MAX_PAGES",
    "DEFAULT_RENDER_DPI",
    "ImageDecoder",
    "default_image_decoder",
    "load_document",
    "pdf_text_regions",
    # model
    "Document",
    "Page",
    "TextSource",
    # ocr
    "DEFAULT_MIN_NATIVE_CHARS",
    "OCREngine",
    "OCRMode",
    "OCRResult",
    "RapidOCREngine",
    "apply_ocr",
    "preprocess_for_ocr",
    # textmatch
    "MAX_PATTERN_CHARS",
    "InvalidPatternError",
    "MatchMode",
    "TextHit",
    "TextMatchResult",
    "match_text",
    "ocr_line_regions",
]
