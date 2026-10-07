"""
common.documents — load JPEG/PNG/PDF/SVG/TXT/DOCX into one page-oriented shape.

Services that used to accept "an image" and push a BGR array through OpenCV
and a vision model need very little to accept "a document" instead: something
that turns any of the supported uploads into pages carrying an image, text, or
both. That is this package.

    detect_kind(raw)          → "image" | "pdf" | "txt" | "docx" | "svg", by
                                magic bytes and structure, never by filename
                                or the caller's Content-Type. Legacy .doc
                                raises a specific "convert to .docx" error,
                                gzip (.svgz) a "decompress to .svg" one.

    load_document(raw, ...)   → ``Document`` with ``pages: list[Page]``. PDFs
                                carry native text plus a render per page (up
                                to ``max_pages``, at ``render_dpi``); an SVG
                                is one page rendered by MuPDF the same way
                                (capped at ``max_render_pixels``), with its
                                ``<text>`` as native text and one entry in
                                ``Document.warnings`` per external reference
                                it did not draw — MuPDF fetches nothing;
                                images are one page with no text; txt/docx
                                are one page with text and no image. The
                                image decoder is injectable so a caller can
                                keep its own EXIF-aware decode path.

    apply_ocr(doc, engine)    → fills the text layer for pages that lack one
                                (mode ``auto``), or all of them (``always``).

    recognize_text_layer(page, engine)
                              → the same decision and recognition, returned
                                as a ``TextLayer`` WITHOUT mutating the page,
                                so several OCR settings can coexist on one
                                document. ``with_text_layers`` builds a
                                (cheap, image-sharing) view carrying them and
                                ``match_text_layers`` searches one.

    pdf_page_count(raw)       → a PDF's page count without rendering it.
    pdf_page_size(raw, i)     → the pixel size page i WILL render to at a DPI,
                                without rendering it.
    svg_page_size(raw)        → the same for an SVG, pixel cap included.

    find_external_refs(raw)   → an SVG's external ``<image>`` / ``<feImage>``
                                / ``<use>`` hrefs with their byte spans, and
    replace_refs(raw, {...})  → those values rewritten — how a service inlines
                                an image it fetched itself (``svg.py``).
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
                                for it. ``filetype="svg"`` asks it of an SVG.

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
``textmatch.py`` (and ``svg.py``) are pure-stdlib and always importable.
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
    DEFAULT_MAX_RENDER_PIXELS,
    DEFAULT_RENDER_DPI,
    ImageDecoder,
    default_image_decoder,
    load_document,
    pdf_page_count,
    pdf_page_size,
    pdf_text_regions,
    svg_page_size,
)
from .model import (
    Document,
    Page,
    TextLayer,
    TextSource,
    page_with_layer,
    with_text_layers,
)
from .ocr import (
    DEFAULT_MIN_NATIVE_CHARS,
    OCREngine,
    OCRMode,
    OCRResult,
    RapidOCREngine,
    apply_ocr,
    needs_recognition,
    preprocess_for_ocr,
    recognize_text_layer,
)
from .svg import ExternalRef, find_external_refs, looks_like_svg, replace_refs
from .textmatch import (
    MAX_PATTERN_CHARS,
    InvalidPatternError,
    MatchMode,
    TextHit,
    TextMatchResult,
    match_text,
    match_text_layers,
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
    "DEFAULT_MAX_RENDER_PIXELS",
    "DEFAULT_RENDER_DPI",
    "ImageDecoder",
    "default_image_decoder",
    "load_document",
    "pdf_page_count",
    "pdf_page_size",
    "pdf_text_regions",
    "svg_page_size",
    # model
    "Document",
    "Page",
    "TextLayer",
    "TextSource",
    "page_with_layer",
    "with_text_layers",
    # ocr
    "DEFAULT_MIN_NATIVE_CHARS",
    "OCREngine",
    "OCRMode",
    "OCRResult",
    "RapidOCREngine",
    "apply_ocr",
    "needs_recognition",
    "preprocess_for_ocr",
    "recognize_text_layer",
    # svg
    "ExternalRef",
    "find_external_refs",
    "looks_like_svg",
    "replace_refs",
    # textmatch
    "MAX_PATTERN_CHARS",
    "InvalidPatternError",
    "MatchMode",
    "TextHit",
    "TextMatchResult",
    "match_text",
    "match_text_layers",
    "ocr_line_regions",
]
