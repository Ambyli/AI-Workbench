"""The OCR engine singleton.

Loading three ONNX graphs costs about a second, so the engine is built on
first use and cached for the life of the process — a deployment that only
scores native PDFs never pays for it. WHETHER a page is recognised is decided
per text layer by ``common.documents.needs_recognition`` (called from
``analysis.context``), and the engine is only fetched when that says yes.

    get_ocr_engine()    — the configured engine, or None when OCR is disabled
                          or its dependencies are missing. ``False`` is the
                          "already tried and unavailable" cache value.
    ocr_engine_status() — the introspection blob GET /document-kinds and
                          ``document_info.ocr`` both read.

Process flow position: read by ``analysis.context`` when a text layer needs
recognising; ``ocr_engine_status`` by ``api.introspection``.
"""

from common.documents import RapidOCREngine

from config import OCR_ENGINE, OCR_MIN_NATIVE_CHARS
from logger import logger

# Process-wide OCR engine. Built on first use because loading three ONNX
# models costs ~1s and a deployment that only scores native PDFs never needs
# it. ``False`` means "already tried and unavailable" so we don't retry the
# import on every job.
_ocr_engine = None


def get_ocr_engine():
    """Return the configured OCR engine, or None when OCR is unavailable.

    Built once per process and cached. Returns None when
    CLASSIFIER_OCR_ENGINE=none (deliberately disabled) or when the engine's
    dependencies are missing — in that case text criteria on scans fail with
    a clear reason rather than the whole job erroring.

    Returns:
        An object satisfying ``common.documents.OCREngine``, or None.
    """
    global _ocr_engine

    if _ocr_engine is False:
        return None
    if _ocr_engine is not None:
        return _ocr_engine

    if OCR_ENGINE in ("", "none", "off", "disabled"):
        logger.info("get_ocr_engine: OCR disabled (CLASSIFIER_OCR_ENGINE=%s)", OCR_ENGINE)
        _ocr_engine = False
        return None
    if OCR_ENGINE != "rapidocr":
        logger.warning(
            "get_ocr_engine: unknown CLASSIFIER_OCR_ENGINE=%s — OCR disabled "
            "(supported: rapidocr | none)",
            OCR_ENGINE,
        )
        _ocr_engine = False
        return None

    try:
        engine = RapidOCREngine()
        # Touch the underlying engine now so a missing dependency surfaces
        # here (once, at first use) rather than mid-analysis.
        _ = engine.engine
    except Exception as exc:
        logger.error("get_ocr_engine: RapidOCR unavailable (%s) — OCR disabled", exc)
        _ocr_engine = False
        return None

    logger.info("get_ocr_engine: RapidOCR ready")
    _ocr_engine = engine
    return engine


def ocr_engine_status() -> dict:
    """Small introspection blob for GET /document-kinds and document_info."""
    return {
        "engine": OCR_ENGINE or "none",
        "available": get_ocr_engine() is not None,
        "min_native_chars": OCR_MIN_NATIVE_CHARS,
    }
