"""The cheap introspection endpoints: what can I send, and what will run?

Five routes that answer questions about the SERVICE rather than about a
document, so a caller can check before submitting a job:

    GET /criterion-types each criterion type's options: JSON schema (from the
                         pydantic models themselves), resolved defaults, and
                         the server caps — the live answer for THIS container.
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
``analysis.ocr``, ``api.criterion_options`` and ``detector.client`` and
touches no job state.
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from common.documents import DEFAULT_MAX_RENDER_PIXELS, EXTENSIONS, MAX_PATTERN_CHARS

from analysis.ocr import ocr_engine_status
from api.criterion_options import criterion_types
from config import (
    ARTIFACT_DIR,
    ARTIFACT_MAX_BYTES,
    ARTIFACT_SWEEP_INTERVAL_S,
    HINT_RUBRICS,
    INLINE_REGIONS_MAX,
    JOB_TTL_HOURS,
    LLM_BBOX_GRID,
    LLM_BBOX_MAX_AREA,
    LLM_BBOX_MAX_ATTEMPTS,
    LLM_BBOX_MIN_AREA,
    LLM_BBOX_PRESENCE_MIN,
    LLM_BBOX_VERIFY_PASS,
    MAX_CONCURRENT,
    MAX_ITEMS,
    MAX_LLM_CALLS,
    MAX_UNITS_PER_JOB,
    OCR_WORKERS,
    PDF_RENDER_DPI,
    REFERENCE_AUTO_MIN_CONFIDENCE,
    REFERENCE_AUTO_POOL_MAX,
    REFERENCE_DIR,
    REFERENCE_MAX_COUNT,
    REFERENCE_MAX_PER_CRITERION,
    REFERENCE_MAX_PER_REQUEST,
    REGION_LAYER_FORMATS,
    SVG_FETCH_IMAGES,
    SVG_FETCH_MAX_BYTES,
    SVG_FETCH_MAX_IMAGES,
    SVG_FETCH_TIMEOUT_S,
    TEXT_CHAR_BUDGET,
    VISION_LLM_MAX_IMAGES_PER_PROMPT,
)
from cv import REGISTRY
from cv.result import spec_of
from detector import client as detector_client
from logger import logger

router = APIRouter(tags=["introspection"])


@router.get("/criterion-types")
def list_criterion_types():
    """Every criterion type's options: JSON schema, defaults, and caps.

    The schema is generated from the same pydantic models /assess validates
    against, so it cannot drift from what the endpoint accepts. ``defaults``
    are resolved for THIS container (a `cv` criterion's fallback depends on
    whether DETECTOR_URL is set); ``caps`` are the server limits a request
    may not exceed.

    Each type also carries ``result``: the shape of the ``detail`` it returns
    (``analysis.result_specs``) — its headline ``metric``, every field with
    its JSON kind, when it appears, and whether it survives a ``mean``
    aggregate — and the payload carries ``aggregate_detail``, the block every
    aggregated ``detail`` gains. A ``cv`` criterion's measurement keys are
    per detector: ``GET /cv-detectors``.
    """
    # Merged here rather than in api.criterion_options, which the analysis
    # package imports (through api.schemas): importing analysis there would
    # close a cycle. This module is the top of the stack.
    from analysis.result_specs import AGGREGATE_BLOCK, specs

    declared = specs()  # each type's spec, declared beside its evaluator
    payload = criterion_types()
    for type_, entry in payload.get("types", {}).items():
        if type_ in declared:
            entry["result"] = declared[type_].as_dict()
    payload["aggregate_detail"] = dict(AGGREGATE_BLOCK)
    return JSONResponse(content=payload)


@router.get("/hints")
def list_hints():
    """Return all available hint values and their LLM scoring instructions.

    Hints control which rubric the LLM uses when scoring a criterion. Set
    ``options.hint`` on an ``llm`` criterion (a ``cv`` criterion that falls
    back to the llm uses ``auto``).
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
    functions: dict[str, object] = {}
    for name, fn in REGISTRY.items():
        fn_name = fn.__name__
        grouped.setdefault(fn_name, []).append(name)
        functions[fn_name] = fn

    # Each detector's declared result shape (cv.result.describes): what its
    # `detail` carries. Test-checked against the detectors' real output.
    detectors = [
        {
            "function": fn_name,
            "names": sorted(names),
            **(spec.as_dict() if (spec := spec_of(functions[fn_name])) else {}),
        }
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
            "pages": "1 — one item",
            "has_page_images": True,
            "native_text": False,
            "notes": "EXIF orientation is applied on load. Text criteria need OCR.",
        },
        {
            "kind": "pdf",
            "extensions": EXTENSIONS["pdf"],
            "content_types": ["application/pdf"],
            "detection": "magic bytes: %PDF- (anywhere in the first 1 KB)",
            "pages": "any — every page is one item; the request's total items are "
                     "capped at CLASSIFIER_MAX_ITEMS (counted at submit, without rendering)",
            "has_page_images": True,
            "native_text": True,
            "notes": f"Each page is rendered at {PDF_RENDER_DPI} dpi for cv/llm criteria. "
                     "A scanned PDF has no native text layer, so OCR fills it in.",
        },
        {
            "kind": "svg",
            "extensions": EXTENSIONS["svg"],
            "content_types": ["image/svg+xml"],
            "detection": "UTF-8 XML whose first element is <svg> (or <prefix:svg>), "
                         "after any XML declaration, comments and DOCTYPE — checked "
                         "before plain text",
            "pages": "1 — one item",
            "has_page_images": True,
            "native_text": True,
            "notes": f"Rendered by MuPDF like a one-page PDF at {PDF_RENDER_DPI} dpi, "
                     f"scaled down to at most {DEFAULT_MAX_RENDER_PIXELS:,} pixels; "
                     "<text> elements are the native text layer (text criteria get "
                     "pdf-text boxes). MuPDF fetches NOTHING an SVG links to — each "
                     "external reference it did not draw is a line in the result's "
                     "documents[].warnings. Only data: images are drawn, unless "
                     "external_images.fetch is on.",
            # The live answer for THIS container: whether <image> links are
            # fetched at submit (CLASSIFIER_SVG_FETCH_IMAGES) and the bounds.
            "external_images": {
                "fetch": SVG_FETCH_IMAGES,
                "max_images": SVG_FETCH_MAX_IMAGES,
                "max_bytes": SVG_FETCH_MAX_BYTES,
                "timeout_s": SVG_FETCH_TIMEOUT_S,
                "accepted": ["image/png", "image/jpeg"],
                "redirects": "not followed",
                "when": "at submit; a link that fails is blanked and warned about, "
                        "never a request failure",
            },
        },
        {
            "kind": "txt",
            "extensions": EXTENSIONS["txt"],
            "content_types": ["text/plain"],
            "detection": "decodes as UTF-8 (BOM allowed), no NUL bytes, mostly printable",
            "pages": "1 — one item",
            "has_page_images": False,
            "native_text": True,
            "notes": "No rendered surface — cv and detector criteria are SKIPPED, the "
                     "llm call is text-only, and score:false criteria are refused "
                     "when no document in the request has a page image.",
        },
        {
            "kind": "docx",
            "extensions": EXTENSIONS["docx"],
            "content_types": [
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            ],
            "detection": "ZIP magic PK\\x03\\x04 containing word/document.xml",
            "pages": "1 — one item (python-docx reads XML, not a laid-out page)",
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
                },
                {
                    "kind": "svgz",
                    "reason": "gzip-compressed SVG. Refused rather than inflated (a "
                              "small gzip can expand to gigabytes); decompress to .svg "
                              "and re-upload.",
                    "detection": "magic bytes: 1F 8B (any gzip data)",
                },
            ],
            "text_match_modes": {
                "contains": "substring anywhere in the document text (default)",
                "exact": "whole word or whole line (word-boundary anchored)",
                "regex": f"Python regular expression, max {MAX_PATTERN_CHARS} characters",
                "fuzzy": "best sliding-window similarity — use this on OCR'd text",
            },
            "inputs": {
                "json": ["base64", "url", "text"],
                "json_shape": "documents: [{type, data, filename?}, ...] — or document: "
                              "{...} for one; not both",
                "multipart": ["file (or legacy 'image'), repeatable", "text, repeatable"],
            },
            "ocr": {
                **ocr_engine_status(),
                "modes": ["auto", "always", "never"],
                "default": "auto",
                "set_on": "each llm / text criterion's options.ocr",
                "text_layer_artifact": "text.p<item>.<key>.json per item and distinct "
                                       "setting; text.d<document>.<key>.json for a "
                                       "scope-document text search",
            },
            "limits": {
                "max_items": MAX_ITEMS,
                "items": "every page of every document is one item; the cap is "
                         "inclusive and counted at submit",
                "pdf_render_dpi": PDF_RENDER_DPI,
                "svg_max_render_pixels": DEFAULT_MAX_RENDER_PIXELS,
                "llm_text_char_budget": TEXT_CHAR_BUDGET,
                # What one vision-model request may carry (the candidate plus
                # reference examples); every call that is not reference-guided
                # still sends one image.
                "images_per_llm_prompt": VISION_LLM_MAX_IMAGES_PER_PROMPT,
                "max_concurrent_jobs": MAX_CONCURRENT,
                "max_units_per_job": MAX_UNITS_PER_JOB,
                "max_llm_calls": MAX_LLM_CALLS,
                "ocr_workers": OCR_WORKERS,
            },
            # What this container can tell you about WHERE, and what it costs.
            # Same spirit as the ocr block: the honest, live answer, so a
            # caller can check before submitting a job that asks for a layer
            # this build does not produce.
            "regions": {
                "always_stored": True,
                "layers": sorted(REGION_LAYER_FORMATS),
                "layers_rendered": "on first fetch, then cached",
                "sources": ["cv", "ocr", "pdf-text", "detector", "llm"],
                "llm_boxes": {
                    "set_on": "each llm criterion's options.boxes",
                    "max_attempts": LLM_BBOX_MAX_ATTEMPTS,
                    "verify_pass": LLM_BBOX_VERIFY_PASS,
                    "min_area": LLM_BBOX_MIN_AREA,
                    "max_area": LLM_BBOX_MAX_AREA,
                    "presence_min_score": LLM_BBOX_PRESENCE_MIN,
                    "grid": LLM_BBOX_GRID,
                },
                # The live answer, not the compiled-in one: `detector` is a
                # source only while DETECTOR_URL points at something. A
                # caller can check here before submitting a job that asks
                # for boxes this container cannot produce.
                "detector": detector_client.status(),
                "inline_max_per_criterion": INLINE_REGIONS_MAX,
                "artifact_dir": ARTIFACT_DIR,
                "artifact_max_bytes_per_item": ARTIFACT_MAX_BYTES,
                "artifact_ttl_hours": JOB_TTL_HOURS,
                "sweep_interval_seconds": ARTIFACT_SWEEP_INTERVAL_S,
            },
            # Saved worked examples (POST /references) and what an /assess may
            # ask of them on THIS container.
            "references": {
                "enabled": VISION_LLM_MAX_IMAGES_PER_PROMPT >= 2,
                "images_per_llm_prompt": VISION_LLM_MAX_IMAGES_PER_PROMPT,
                "contrastive": VISION_LLM_MAX_IMAGES_PER_PROMPT >= 3,
                "max_count": REFERENCE_MAX_COUNT,
                "max_per_request": REFERENCE_MAX_PER_REQUEST,
                "max_per_criterion": REFERENCE_MAX_PER_CRITERION,
                "auto": True,
                "auto_pool_max": REFERENCE_AUTO_POOL_MAX,
                "auto_min_confidence": REFERENCE_AUTO_MIN_CONFIDENCE,
                "applies_to": "llm criteria, and cv criteria answered by the llm fallback",
                "dir": REFERENCE_DIR,
                "retention": "kept until DELETE /references/{id} — never swept",
            },
        }
    )


@router.get("/health")
def health():
    """Liveness check used by the Docker healthcheck and load balancers."""
    logger.debug("health: returning ok")
    return {"status": "ok"}
