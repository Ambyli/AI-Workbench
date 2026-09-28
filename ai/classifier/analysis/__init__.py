"""Document analysis: bytes in, one result per criterion out.

The package the whole service is arranged around. ``pipeline`` is the
orchestration; every step's work lives in a sibling:

    loading.py       bytes -> Document (content type, EXIF, kind, URL fetch,
                     the single-page rule)
    ocr.py           the OCR engine singleton
    geometry.py      the <=1000-px working frame and the map back to originals
    context.py       what every criterion shares, computed once, read-only —
                     including the text layers, memoised per OCR setting
    outcome.py       the one shape every evaluator returns
    cv_eval.py       the `cv` evaluator: one OpenCV detector (or its fallback)
    text_eval.py     the `text` evaluator: deterministic search of a text layer
    llm_eval.py      the `llm` evaluator: one scoring call, then maybe boxes
    detector_eval.py the `detector` evaluator: the open-vocabulary service
    scheduler.py     dependency waves, the per-job cap, error isolation
    weighting.py     the weighted overall score, and whether it is complete
    pipeline.py      analyze_document

Layering: this package may import ``cv``, ``llm``, ``detector`` and
``regions``; none of them import back.

The public entry points are re-exported here, so a caller writes
``from analysis import analyze_document`` and never has to know which step
module a function lives in.
"""

from analysis.loading import (
    load_document_bytes,
    load_input_bytes,
    validate_content_type,
)
from analysis.pipeline import analyze_document

__all__ = [
    "analyze_document",
    "load_document_bytes",
    "load_input_bytes",
    "validate_content_type",
]
