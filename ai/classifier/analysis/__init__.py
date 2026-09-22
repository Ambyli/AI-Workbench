"""Document analysis: bytes in, a scored (or located) result out.

The package the whole service is arranged around. ``pipeline`` is the
orchestration — nine numbered steps — and every step's work lives in a
sibling so that file stays readable:

    criteria.py      parse and validate the criteria a request asked for
    loading.py       bytes -> Document (content type, EXIF, kind, URL fetch)
    ocr.py           the OCR engine singleton, and whether this job needs it
    geometry.py      the <=1000-px working frame and the map back to originals
    cv_eval.py       the `cv` path: one OpenCV detector across every page
    detector_eval.py the `detector` path: the open-vocabulary service
    text_eval.py     the `text` path: deterministic search of the text layer
    llm_eval.py      what the one LLM call gets to see
    weighting.py     depends_on resolution and the weighted overall score
    pipeline.py      analyze_document and its three entry points

Layering: this package may import ``cv``, ``llm``, ``detector`` and
``regions``; none of them import back.

The public entry points are re-exported here, so a caller writes
``from analysis import analyze_document`` and never has to know which step
module a function lives in.
"""

from analysis.criteria import (
    criterion_pattern,
    parse_criteria,
    validate_text_criteria,
)
from analysis.loading import load_document_bytes, validate_content_type
from analysis.pipeline import (
    analyze_document,
    analyze_input,
    analyze_upload,
    resolve_example,
)

__all__ = [
    "analyze_document",
    "analyze_input",
    "analyze_upload",
    "criterion_pattern",
    "load_document_bytes",
    "parse_criteria",
    "resolve_example",
    "validate_content_type",
    "validate_text_criteria",
]
