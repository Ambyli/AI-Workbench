"""Page and Document — the in-memory shape every loader produces.

A ``Document`` is the common currency of this subpackage: whatever the caller
uploaded (a photo, a PDF, a .txt, a .docx), the loaders in ``loaders.py``
normalise it into an ordered list of ``Page`` objects, each of which may carry

  * a rasterised image (BGR numpy array) — always for image/PDF kinds, never
    for txt/docx, which have no rendered surface, and
  * a text layer — native (PDF text objects, .txt bytes, .docx runs), OCR'd
    (filled in later by ``ocr.apply_ocr``), or absent.

Downstream code therefore never branches on the upload's file type: it asks
``document.page_images()`` for something to run OpenCV or a vision model on,
and ``document.full_text()`` for something to search or hand to an LLM.

Process flow position: constructed by ``loaders.load_document``, mutated in
place by ``ocr.apply_ocr``, read by ``textmatch.match_text`` and consumers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Optional

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps numpy off the import path
    import numpy as np


# Where a page's ``text`` came from. Consumers use this to decide how much to
# trust the text: "native" is exact, "ocr" may contain recognition errors,
# "none" means the page has no text layer at all (an unOCR'd photo).
TextSource = Literal["native", "ocr", "none"]

# The document kinds this package can load. Mirrors detect.DocumentKind —
# kept as a plain Literal here so model.py imports nothing from detect.py.
DocumentKind = Literal["image", "pdf", "txt", "docx"]


@dataclass
class Page:
    """One page of a loaded document.

    Attributes:
        index:          Zero-based page number in document order.
        image_bgr:      Rendered page as a BGR numpy array, or None for kinds
                        with no rendered surface (txt, docx).
        text:           Extracted text for this page ("" when none).
        text_source:    "native" | "ocr" | "none" — provenance of ``text``.
        ocr_confidence: Mean recogniser confidence 0.0–1.0 when text_source is
                        "ocr", else None.
        width/height:   Pixel dimensions of ``image_bgr`` (0 when there is no
                        image).
        ocr_lines:      Per-line OCR detail kept from the recogniser —
                        ``{"text", "confidence", "box"}`` where ``box`` is a
                        4-point polygon in THIS page's image pixels. Empty for
                        native text. It is what lets ``match_text(...,
                        locate=True)`` map a character offset back to the
                        polygon it was recognised from, so a text hit can be
                        drawn on the page instead of merely counted.
    """

    index: int
    image_bgr: Optional["np.ndarray"] = None
    text: str = ""
    text_source: TextSource = "none"
    ocr_confidence: Optional[float] = None
    width: int = 0
    height: int = 0
    ocr_lines: list[dict] = field(default_factory=list)

    def has_image(self) -> bool:
        """True when this page carries a rasterised image."""
        return self.image_bgr is not None

    def text_chars(self) -> int:
        """Length of the stripped text layer — the cheap "is there text here?"
        signal used by OCR decisions and by the ``document_info`` block."""
        return len(self.text.strip())


@dataclass
class Document:
    """A loaded document: metadata plus its pages in document order.

    Attributes:
        kind:            "image" | "pdf" | "txt" | "docx" (see detect.py).
        filename:        Original filename as supplied by the caller ("" if
                         unknown) — informational only, never trusted for
                         format detection.
        size_bytes:      Size of the original upload in bytes.
        content_type:    MIME type for the original upload. Derived from the
                         detected kind, not from the caller's header.
        pages:           Ordered pages. Always at least one for a successful
                         load.
        truncated_pages: How many pages were dropped because the load hit the
                         ``max_pages`` cap (0 when the whole document loaded).
        source_bytes:    The original upload, kept ONLY when the caller passed
                         ``load_document(..., keep_source=True)``. The one
                         consumer is ``loaders.pdf_text_regions``, which has to
                         re-open the PDF to ask PyMuPDF where a phrase sits on
                         the page. Off by default because holding a 40 MB PDF
                         for the life of a job is a real cost for a feature
                         most requests never use.
    """

    kind: DocumentKind
    filename: str = ""
    size_bytes: int = 0
    content_type: str = "application/octet-stream"
    pages: list[Page] = field(default_factory=list)
    truncated_pages: int = 0
    source_bytes: Optional[bytes] = None

    # ── Text helpers ──────────────────────────────────────────────────────
    def full_text(self, separator: str = "\n\n") -> str:
        """Concatenate every page's text in document order.

        Pages with an empty text layer are skipped so the separator never
        produces runs of blank lines. This is what gets searched by
        ``textmatch.match_text`` and (truncated) handed to an LLM.

        Args:
            separator: Joined between consecutive non-empty page texts.

        Returns:
            The whole document's text, or "" when no page has any.
        """
        return separator.join(p.text for p in self.pages if p.text.strip())

    def has_text(self) -> bool:
        """True when at least one page has a non-empty text layer.

        False means the document is image-only and un-OCR'd (or OCR found
        nothing) — text criteria cannot be evaluated against it.
        """
        return any(p.text.strip() for p in self.pages)

    def text_sources(self) -> set[str]:
        """Set of distinct ``text_source`` values across pages that have text.

        Used to report whether a result came from native text, OCR, or a mix.
        """
        return {p.text_source for p in self.pages if p.text.strip()}

    # ── Image helpers ─────────────────────────────────────────────────────
    def page_images(self) -> list[tuple[int, "np.ndarray"]]:
        """Every page that carries an image, as ``(page_index, image_bgr)``.

        Returns an empty list for txt/docx documents — callers that need an
        image (OpenCV detectors, a vision prompt) treat that as "not
        applicable" rather than an error.
        """
        return [(p.index, p.image_bgr) for p in self.pages if p.image_bgr is not None]

    def has_images(self) -> bool:
        """True when at least one page carries an image."""
        return any(p.image_bgr is not None for p in self.pages)

    def page(self, index: int) -> Optional[Page]:
        """Return the page with this index, or None when out of range."""
        for p in self.pages:
            if p.index == index:
                return p
        return None
