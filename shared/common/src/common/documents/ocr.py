"""OCR — fill in the text layer for pages that have none.

Two pieces:

  ``OCREngine``      a Protocol with a single ``recognize(image_bgr)`` method.
                     Anything matching it can be passed to ``apply_ocr``, so
                     tests use a fake engine and services can swap RapidOCR
                     for a hosted recogniser without touching call sites.

  ``RapidOCREngine`` the default implementation, wrapping ``rapidocr`` (v3,
                     ONNXRuntime backend). The import is lazy and the engine
                     is constructed on first use, because loading three ONNX
                     models costs ~1s and most requests never need OCR.

``apply_ocr(document, engine, mode=...)`` is the only function most callers
need: it walks the pages, decides which ones need recognising, and writes the
result back onto each ``Page`` (``text``, ``text_source="ocr"``,
``ocr_confidence``).

Recognition quality notes (why the preprocessing exists):
  * RapidOCR's detector works on the image as given — a 600px-wide phone photo
    of a letter has ~8px tall glyphs, below what the recogniser resolves. The
    long side is therefore upscaled to at least ``min_long_side``.
  * Colour carries no information for text recognition but does add noise, so
    the image is flattened to grey (replicated back to three channels, which
    is the shape the models expect).

Requires the ``documents`` extra (``rapidocr`` + ``onnxruntime``) for
``RapidOCREngine`` only — ``apply_ocr`` itself works with any engine object.

Process flow position: called after ``loaders.load_document`` and before
``textmatch.match_text`` / any LLM prompt that includes document text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Optional, Protocol, runtime_checkable

from .model import Document

if TYPE_CHECKING:  # pragma: no cover - typing only
    import numpy as np

# "auto"   — recognise only pages whose native text is below the threshold
# "always" — recognise every page that has an image, even one with native text
# "never"  — do nothing (the caller disabled OCR, or no engine is configured)
OCRMode = Literal["auto", "always", "never"]

# A page with fewer than this many native characters is treated as having no
# usable text layer. 20 catches the common scanned-PDF case where the only
# native text is a stamp or a page number.
DEFAULT_MIN_NATIVE_CHARS = 20

# Upscale target for the long side before recognition (see module docstring).
DEFAULT_MIN_LONG_SIDE = 1000


@dataclass
class OCRResult:
    """What an engine returns for one image.

    Attributes:
        text:       Recognised text, one detected line per newline, in the
                    engine's reading order.
        confidence: Mean per-line confidence 0.0–1.0 (0.0 when nothing was
                    recognised).
        lines:      Per-line detail — ``{"text": str, "confidence": float,
                    "box": [[x, y], ...] | None}``. Boxes are plain lists so
                    the result is JSON-serialisable.
    """

    text: str = ""
    confidence: float = 0.0
    lines: list[dict[str, Any]] = field(default_factory=list)

    def __bool__(self) -> bool:
        """An OCRResult is falsey when nothing legible came back."""
        return bool(self.text.strip())


@runtime_checkable
class OCREngine(Protocol):
    """Anything that can turn a BGR image into text.

    Implementations must be safe to call repeatedly from one thread; callers
    that need concurrency should construct one engine per worker.
    """

    def recognize(self, image_bgr: "np.ndarray") -> OCRResult:
        """Recognise text in a BGR numpy array."""
        ...


# ---------------------------------------------------------------------------
# Preprocessing
# ---------------------------------------------------------------------------


def preprocess_for_ocr(
    image_bgr: "np.ndarray",
    *,
    grayscale: bool = True,
    min_long_side: int = DEFAULT_MIN_LONG_SIDE,
) -> "np.ndarray":
    """Light, reversible clean-up before recognition.

    Deliberately conservative — no thresholding, deskewing, or denoising.
    Modern detector models handle moderate skew and uneven lighting better
    than a fixed binarisation does, and an over-processed image loses faint
    strokes entirely.

    Args:
        image_bgr:     Source page image.
        grayscale:     Flatten colour to grey (replicated to 3 channels).
        min_long_side: Upscale until the long side reaches this many pixels.
                       Set 0 to disable scaling.

    Returns:
        A new BGR array; the input is never modified.
    """
    import numpy as np
    from PIL import Image

    arr = image_bgr
    if grayscale and arr.ndim == 3 and arr.shape[2] >= 3:
        # Luma weights on BGR channel order.
        grey = (
            arr[:, :, 0].astype(np.float32) * 0.114
            + arr[:, :, 1].astype(np.float32) * 0.587
            + arr[:, :, 2].astype(np.float32) * 0.299
        ).astype(np.uint8)
        arr = np.stack([grey, grey, grey], axis=2)

    h, w = arr.shape[:2]
    long_side = max(h, w)
    if min_long_side and 0 < long_side < min_long_side:
        scale = min_long_side / long_side
        # PIL's LANCZOS upscale keeps glyph edges cleaner than nearest/linear.
        pil = Image.fromarray(arr[:, :, ::-1])  # BGR → RGB for PIL
        pil = pil.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        arr = np.array(pil)[:, :, ::-1].copy()  # back to BGR

    return arr


# ---------------------------------------------------------------------------
# RapidOCR implementation
# ---------------------------------------------------------------------------


class RapidOCREngine:
    """Default ``OCREngine``, backed by ``rapidocr`` 3.x on ONNXRuntime.

    The models (PP-OCRv6 detection + recognition, plus the angle classifier)
    live inside the installed ``rapidocr`` package directory and are fetched
    on first construction if missing. Containers should pre-warm them at build
    time so no runtime download is needed — see
    ``ai/classifier/Dockerfile``'s OCR warm-up step.

    Construction is lazy: nothing is imported or loaded until the first
    ``recognize`` call, so importing this module in a service that never OCRs
    costs nothing.
    """

    def __init__(
        self,
        *,
        params: Optional[dict[str, Any]] = None,
        grayscale: bool = True,
        min_long_side: int = DEFAULT_MIN_LONG_SIDE,
    ) -> None:
        """
        Args:
            params:        Extra ``RapidOCR(params=...)` overrides (engine
                           choice, model paths, thresholds). Usually omitted.
            grayscale:     Flatten to grey before recognition.
            min_long_side: Upscale target for small images (0 disables).
        """
        self._params = params
        self._grayscale = grayscale
        self._min_long_side = min_long_side
        self._engine: Any = None

    @property
    def engine(self) -> Any:
        """The underlying ``RapidOCR`` instance, constructed on first access.

        Raises:
            ImportError: If the ``documents`` extra (rapidocr + onnxruntime)
                is not installed — with an actionable message rather than a
                bare ModuleNotFoundError.
        """
        if self._engine is None:
            try:
                from rapidocr import RapidOCR
            except ImportError as exc:  # pragma: no cover - depends on env
                raise ImportError(
                    "RapidOCREngine needs the 'documents' extra: "
                    "pip install 'common[documents]' (rapidocr + onnxruntime)."
                ) from exc
            self._engine = RapidOCR(params=self._params) if self._params else RapidOCR()
        return self._engine

    def recognize(self, image_bgr: "np.ndarray") -> OCRResult:
        """Recognise text in one page image.

        Returns an empty ``OCRResult`` (rather than raising) when the page
        holds no legible text — a blank page is a normal outcome, not an
        error.
        """
        prepared = preprocess_for_ocr(
            image_bgr, grayscale=self._grayscale, min_long_side=self._min_long_side
        )
        out = self.engine(prepared)

        # rapidocr 3.x returns a RapidOCROutput whose txts/scores are tuples,
        # or an object with txts=None when the detector found nothing.
        txts = getattr(out, "txts", None) or ()
        scores = getattr(out, "scores", None) or ()
        boxes = getattr(out, "boxes", None)

        lines: list[dict[str, Any]] = []
        for i, txt in enumerate(txts):
            conf = float(scores[i]) if i < len(scores) else 0.0
            box = None
            if boxes is not None and i < len(boxes):
                # numpy array of 4 corner points → plain nested lists.
                box = [[float(x), float(y)] for x, y in boxes[i]]
            lines.append({"text": str(txt), "confidence": round(conf, 4), "box": box})

        text = "\n".join(line["text"] for line in lines)
        mean_conf = (
            sum(line["confidence"] for line in lines) / len(lines) if lines else 0.0
        )
        return OCRResult(text=text, confidence=round(mean_conf, 4), lines=lines)


# ---------------------------------------------------------------------------
# Document-level driver
# ---------------------------------------------------------------------------


def apply_ocr(
    document: Document,
    engine: Optional[OCREngine],
    *,
    mode: OCRMode = "auto",
    min_native_chars: int = DEFAULT_MIN_NATIVE_CHARS,
) -> int:
    """Fill in missing text layers on ``document`` in place.

    Which pages are recognised:

      ``never``  — none. Returns 0 immediately (also when ``engine`` is None,
                   i.e. the deployment disabled OCR).
      ``auto``   — every page that has an image and fewer than
                   ``min_native_chars`` characters of native text.
      ``always`` — every page that has an image, native text or not. The
                   recognised text replaces the native layer, so use this only
                   when the native layer is suspect.

    A page whose recognition comes back empty is left untouched (its
    ``text_source`` stays ``"none"``) — reporting an empty OCR layer as
    ``"ocr"`` would make ``Document.has_text()`` lie.

    Args:
        document:         Loaded document; mutated in place.
        engine:           Any ``OCREngine``; None means "OCR unavailable".
        mode:             auto | always | never.
        min_native_chars: Native-text threshold for ``auto``.

    Returns:
        Number of pages whose text layer was replaced by OCR output.
    """
    if mode == "never" or engine is None:
        return 0

    replaced = 0
    for page in document.pages:
        if page.image_bgr is None:
            continue  # txt/docx pages have nothing to recognise
        if mode == "auto" and page.text_chars() >= min_native_chars:
            continue

        result = engine.recognize(page.image_bgr)
        if not result:
            continue

        page.text = result.text
        page.text_source = "ocr"
        page.ocr_confidence = result.confidence
        replaced += 1

    return replaced
