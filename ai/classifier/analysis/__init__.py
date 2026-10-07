"""Document analysis: documents in, one result per criterion out.

The package the whole service is arranged around. A request's documents are
split into ITEMS — every page of every document — and the unit of work is
(criterion, item). ``pipeline`` is the orchestration; every step's work lives
in a sibling:

    loading.py       bytes -> Document (content type, EXIF, kind, URL fetch,
                     every page), and the submit-time SVG image inliner
    ocr.py           the OCR engine singleton
    geometry.py      the <=1000-px working frame and the map back to originals
    context.py       what every unit on one item shares, computed once,
                     read-only — including the text layers, memoised per item
                     and OCR setting — and the per-document grouping
    outcome.py       the one shape every evaluator (and aggregate) returns
    cv_eval.py       the `cv` evaluator: one OpenCV detector (or its fallback)
    text_eval.py     the `text` evaluator: deterministic search of a text layer
                     (one page, or a document's pages joined)
    llm_eval.py      the `llm` evaluator: one scoring call (or its reference-
                     guided calls, combined), then maybe boxes
    references.py    a job's reference plan: which examples guide which
                     criterion, their images, the position check, the result
                     block — and the reference page's description call
    detector_eval.py the `detector` evaluator: the open-vocabulary service
    scheduler.py     (criterion, item) units, per-item dependency gating, the
                     per-job unit cap, error isolation
    aggregate.py     pages -> per document -> per request, per criterion
    weighting.py     the weighted score (per item, and overall), and whether
                     it is complete
    pipeline.py      analyze_document

Layering: this package may import ``cv``, ``llm``, ``detector`` and
``regions``; none of them import back.

The public entry points are re-exported here, so a caller writes
``from analysis import analyze_document`` and never has to know which step
module a function lives in.
"""

from analysis.loading import (
    load_document_bytes,
    load_document_page,
    load_input_bytes,
    resolve_svg_images,
    validate_content_type,
)
from analysis.pipeline import analyze_document

__all__ = [
    "analyze_document",
    "load_document_bytes",
    "load_document_page",
    "load_input_bytes",
    "resolve_svg_images",
    "validate_content_type",
]
