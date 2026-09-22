"""The OCR engine singleton, and the decision to use it.

Loading three ONNX graphs costs about a second, so the engine is built on
first use and cached for the life of the process — a deployment that only
scores native PDFs never pays for it, and ``_needs_ocr`` is what keeps that
promise per job.

    get_ocr_engine()    — the configured engine, or None when OCR is disabled
                          or its dependencies are missing. ``False`` is the
                          "already tried and unavailable" cache value.
    ocr_engine_status() — the introspection blob GET /document-kinds and
                          ``document_info.ocr`` both read.
    _needs_ocr()        — whether THIS job should spend time on recognition.

Process flow position: step 2 of ``analysis.pipeline.analyze_document``.
``ocr_engine_status`` is also read by ``api.introspection``.
"""

from common.documents import Document, RapidOCREngine

from api.schemas import CriterionInput
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


def _needs_ocr(doc: Document, criteria: list[CriterionInput], ocr_mode: str) -> bool:
    """Decide whether this job should spend time on OCR.

    ``always`` — yes, whenever there is a page image at all.
    ``never``  — no.
    ``auto``   — yes when text is both WANTED and MISSING:
                   * wanted: any text criterion (it has nothing to search
                     otherwise), or any llm criterion on a document whose
                     pages have no text — a scan the model reads far better
                     with the recognised text alongside it, and
                   * missing: at least one page carries an image whose text
                     layer is under OCR_MIN_NATIVE_CHARS.
                 A native PDF, a .txt, or a .docx therefore never pays for
                 loading the OCR models.
    """
    if ocr_mode == "never":
        return False
    if not doc.has_images():
        return False  # nothing to recognise: txt/docx have no rendered surface
    if ocr_mode == "always":
        return True

    pages_missing_text = any(
        p.image_bgr is not None and p.text_chars() < OCR_MIN_NATIVE_CHARS
        for p in doc.pages
    )
    if not pages_missing_text:
        return False
    return any(c.type in ("text", "llm") for c in criteria)
