"""The cheap introspection endpoints: what can I send, and what will run?

Four routes that answer questions about the SERVICE rather than about a
document, so a caller can check before submitting a job:

    GET /hints           every hint value and the LLM rubric it selects.
    GET /cv-detectors    every registered OpenCV detector name, grouped by the
                         function behind it.
    GET /document-kinds  the upload kinds, the text-match modes, the live OCR
                         and detector status, the limits, and the regions
                         block — the honest answer for THIS container, not the
                         compiled-in one.
    GET /health          liveness, for the Docker healthcheck.

Nothing here is a per-request setting: the limits come from the container's
environment.

Process flow position: mounted by ``main``; reads ``config``, ``cv.REGISTRY``,
``analysis.ocr`` and ``detector.client`` and touches no job state.
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from common.documents import EXTENSIONS, MAX_PATTERN_CHARS

from analysis.ocr import ocr_engine_status
from config import (
    ARTIFACT_DIR,
    ARTIFACT_MAX_BYTES,
    ARTIFACT_SWEEP_INTERVAL_S,
    DIFF_MAX_REGIONS,
    DIFF_MIN_AREA,
    DIFF_MIN_INLIERS,
    DOC_MAX_PAGES,
    HINT_RUBRICS,
    INLINE_REGIONS_MAX,
    JOB_TTL_HOURS,
    LLM_BBOX_GRID,
    LLM_BBOX_MAX_AREA,
    LLM_BBOX_MAX_ATTEMPTS,
    LLM_BBOX_MIN_AREA,
    LLM_BBOX_PRESENCE_MIN,
    LLM_BBOX_VERIFY_PASS,
    PDF_RENDER_DPI,
    REGION_LAYER_FORMATS,
    TEXT_CHAR_BUDGET,
)
from cv import REGISTRY
from detector import client as detector_client
from logger import logger

router = APIRouter(tags=["introspection"])


@router.get("/hints")
def list_hints():
    """Return all available hint values and their LLM scoring instructions.

    Hints control which rubric the LLM uses when scoring a criterion.
    Set hint on any criterion (type='llm', or type='cv' as a fallback).
    """
    logger.debug("list_hints: returning %d hint definitions", len(HINT_RUBRICS))
    return JSONResponse(content={"hints": HINT_RUBRICS})


@router.get("/cv-detectors")
def list_cv_detectors():
    """Return all registered CV detector names, grouped by detector function.

    Use these names as the 'name' field of a criterion with type='cv'.
    Fuzzy matching is applied at runtime, so near-matches also work.
    """
    logger.debug("list_cv_detectors: building detector map from %d registry entries", len(REGISTRY))

    grouped: dict[str, list[str]] = {}
    for name, fn in REGISTRY.items():
        fn_name = fn.__name__
        grouped.setdefault(fn_name, []).append(name)

    detectors = [
        {"function": fn_name, "names": sorted(names)}
        for fn_name, names in sorted(grouped.items())
    ]

    logger.debug("list_cv_detectors: returning %d detectors", len(detectors))
    return JSONResponse(content={"detectors": detectors, "total_names": len(REGISTRY)})


@router.get("/document-kinds")
def list_document_kinds():
    """Return the upload kinds this service accepts and how they are handled.

    A cheap introspection endpoint, like /hints and /cv-detectors: it answers
    "what can I send, what will it be able to evaluate, and is OCR actually
    available right now?" without submitting a job. Nothing here is a
    per-request setting — the limits come from the container's environment.
    """
    logger.debug("list_document_kinds: building capability map")

    kinds = [
        {
            "kind": "image",
            "extensions": EXTENSIONS["image"],
            "content_types": ["image/jpeg", "image/png"],
            "detection": "magic bytes: FF D8 FF (JPEG) / 89 50 4E 47 0D 0A 1A 0A (PNG)",
            "pages": "1",
            "has_page_images": True,
            "native_text": False,
            "notes": "EXIF orientation is applied on load. Text criteria need OCR.",
        },
        {
            "kind": "pdf",
            "extensions": EXTENSIONS["pdf"],
            "content_types": ["application/pdf"],
            "detection": "magic bytes: %PDF- (anywhere in the first 1 KB)",
            "pages": f"up to CLASSIFIER_DOC_MAX_PAGES ({DOC_MAX_PAGES}); extras are "
                     "reported in document_info.truncated_pages",
            "has_page_images": True,
            "native_text": True,
            "notes": f"Each page is rendered at {PDF_RENDER_DPI} dpi for cv/llm criteria. "
                     "A scanned PDF has no native text layer, so OCR fills it in.",
        },
        {
            "kind": "txt",
            "extensions": EXTENSIONS["txt"],
            "content_types": ["text/plain"],
            "detection": "decodes as UTF-8 (BOM allowed), no NUL bytes, mostly printable",
            "pages": "1",
            "has_page_images": False,
            "native_text": True,
            "notes": "No rendered surface — cv criteria are SKIPPED and the llm call is "
                     "text-only.",
        },
        {
            "kind": "docx",
            "extensions": EXTENSIONS["docx"],
            "content_types": [
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            ],
            "detection": "ZIP magic PK\\x03\\x04 containing word/document.xml",
            "pages": "1 (python-docx reads XML, not a laid-out page)",
            "has_page_images": False,
            "native_text": True,
            "notes": "Paragraphs and table cells are extracted in document order; table "
                     "rows are flattened to 'cell | cell'. cv criteria are SKIPPED.",
        },
    ]

    return JSONResponse(
        content={
            "kinds": kinds,
            "unsupported": [
                {
                    "kind": "doc",
                    "reason": "Legacy OLE2 Word files are not readable by python-docx. "
                              "Convert to .docx and re-upload.",
                    "detection": "magic bytes: D0 CF 11 E0 A1 B1 1A E1",
                }
            ],
            "text_match_modes": {
                "contains": "substring anywhere in the document text (default)",
                "exact": "whole word or whole line (word-boundary anchored)",
                "regex": f"Python regular expression, max {MAX_PATTERN_CHARS} characters",
                "fuzzy": "best sliding-window similarity — use this on OCR'd text",
            },
            "ocr": {
                **ocr_engine_status(),
                "modes": ["auto", "always", "never"],
                "default": "auto",
            },
            "limits": {
                "max_pages": DOC_MAX_PAGES,
                "pdf_render_dpi": PDF_RENDER_DPI,
                "llm_text_char_budget": TEXT_CHAR_BUDGET,
                "images_per_llm_prompt": 1,
            },
            # What this container can tell you about WHERE, and what it costs.
            # Same spirit as the ocr block: the honest, live answer, so a
            # caller can check before submitting a job that asks for a layer
            # this build does not produce.
            "regions": {
                "enabled_by_default": False,
                "layers": sorted(REGION_LAYER_FORMATS),
                "default_layers": ["svg"],
                "sources": ["cv", "ocr", "pdf-text", "detector", "llm", "diff"],
                "not_implemented": [],
                # The two options that need a reference document, so they do
                # nothing on /assess and /locate however they are spelled.
                "compare_only": ["diff", "examples"],
                "llm_boxes": {
                    "max_attempts": LLM_BBOX_MAX_ATTEMPTS,
                    "verify_pass": LLM_BBOX_VERIFY_PASS,
                    "min_area": LLM_BBOX_MIN_AREA,
                    "max_area": LLM_BBOX_MAX_AREA,
                    "presence_min_score": LLM_BBOX_PRESENCE_MIN,
                    "grid": LLM_BBOX_GRID,
                },
                "diff": {
                    "min_inliers": DIFF_MIN_INLIERS,
                    "min_area": DIFF_MIN_AREA,
                    "max_regions": DIFF_MAX_REGIONS,
                    "changes": ["added", "removed", "changed"],
                },
                # The live answer, not the compiled-in one: `detector` is a
                # source only while DETECTOR_URL points at something. A
                # caller can check here before submitting a job that asks
                # for boxes this container cannot produce.
                "detector": detector_client.status(),
                "inline_max_per_criterion": INLINE_REGIONS_MAX,
                "artifact_dir": ARTIFACT_DIR,
                "artifact_max_bytes": ARTIFACT_MAX_BYTES,
                "artifact_ttl_hours": JOB_TTL_HOURS,
                "sweep_interval_seconds": ARTIFACT_SWEEP_INTERVAL_S,
            },
        }
    )


@router.get("/health")
def health():
    """Liveness check used by the Docker healthcheck and load balancers."""
    logger.debug("health: returning ok")
    return {"status": "ok"}
