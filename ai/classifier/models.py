"""Pydantic request/response models for the classifier API.

These models are used both for request validation (FastAPI deserialises
incoming JSON into them) and for type safety throughout the analysis pipeline.

Data model relationships
------------------------
  POST /assess
    file (UploadFile)  +  criteria (list[CriterionInput])  +  ocr
        → queued as an assess job → analyzed by analysis.py

  POST /assess/compare
    CompareRequest
      ├── image     : DocumentInput       — the subject document
      ├── criteria  : list[CriterionInput]— what to evaluate
      ├── aggregation: mean|min|max       — how to collapse N example scores
      ├── ocr       : auto|always|never   — text-recognition policy
      └── examples  : list[ExampleInput]  — reference documents to compare against

"Document" here means any supported upload: JPEG/PNG, PDF, plain text, or
.docx (see common.documents). The fields are still named ``image`` for
backwards compatibility with existing callers — a JPEG is simply the
single-page case.

Process flow position: imported by analysis.py, main.py, runners.py, and llm.py.
"""

from typing import Literal, Optional

from pydantic import BaseModel, Field

from config import DEFAULT_CRITERIA


class CriterionInput(BaseModel):
    """A single evaluation criterion.

    Three evaluation paths are supported (see the ``type`` field):
      - "llm":  the vision LLM scores the criterion, using the page image and
                (when available) the document's extracted text.
      - "cv":   a registered OpenCV detector runs on the page images — no
                token cost, deterministic.
      - "text": a deterministic search over the document's text layer (native
                or OCR'd) — no token cost, no image needed.

    Weights control how much each criterion contributes to the overall score.
    A criterion with weight=3.0 counts three times as heavily as one with
    weight=1.0 in the weighted average computed by validate_and_clamp().

    The ``pattern`` / ``match`` / ``case_sensitive`` / ``fuzzy_threshold`` /
    ``min_count`` fields only apply to type="text" and are ignored otherwise.
    """

    name: str = Field(description="The criterion to evaluate.")
    type: Literal["cv", "llm", "text"] = Field(
        default="llm",
        description=(
            "'llm': scored by the vision LLM (no detector required). "
            "'cv': run through a registered OpenCV detector by name — no token cost. "
            "If no detector matches the criterion name, falls back to 'llm' automatically. "
            "'text': deterministic search of the document's text layer for 'pattern' "
            "(defaults to 'name') — no token cost. Text comes from the PDF/DOCX/TXT "
            "native layer, or from OCR when the document is a scan or photo. "
            "The result always includes a 'method' field showing which path was actually used. "
            "Built-in cv names: 'sharpness', 'exposure' / 'proper exposure', "
            "'has trees' / 'has vegetation' / 'has greenery' / 'has plants', "
            "'has sky', 'has faces' / 'has people' / 'has person', "
            "'has water' / 'has pool' / 'has swimming pool', "
            "'has text' / 'has text regions' / 'has writing'."
        ),
    )
    weight: float = Field(
        default=1.0,
        gt=0.0,
        description=(
            "Relative weight for this criterion when computing the overall score. "
            "Higher values make this criterion matter more. Default 1.0 for equal weighting."
        ),
    )
    hint: Literal["quality", "presence", "auto"] = Field(
        default="auto",
        description=(
            "Tells the LLM which scoring rubric to apply to this criterion. "
            "'quality': score image quality 1-10 (1-3=FAIL, 4-6=MARGINAL, 7-10=PASS). "
            "'presence': detect presence/absence (10=present, 5=uncertain, 1=absent) with "
            "chain-of-thought reasoning. "
            "'auto' (default): LLM infers the rubric from the criterion name. "
            "For type='cv' criteria: hint is ignored when a detector runs, but is passed "
            "to the LLM if no detector matches the name and the criterion falls back."
        ),
    )
    depends_on: Optional[str] = Field(
        default=None,
        description=(
            "Name of another criterion that must PASS (score >= 7) before this "
            "criterion is evaluated. If the dependency does not pass — including "
            "if it was itself skipped — this criterion is marked SKIPPED and "
            "excluded from the weighted score entirely. "
            "Chains are supported: A → B → C all skip if A fails. "
            "Future: a minimum score threshold will be configurable here; "
            "for now PASS verdict is the only qualifying condition."
        ),
    )

    # ── type="text" fields ────────────────────────────────────────────────
    # All five are ignored for cv/llm criteria. They map one-to-one onto
    # common.documents.textmatch.match_text().
    pattern: Optional[str] = Field(
        default=None,
        description=(
            "What to look for in the document text. Defaults to 'name' when omitted, "
            "so {\"name\": \"Notice to Owner\", \"type\": \"text\"} just works. "
            "Literal text for match=contains/exact/fuzzy; a Python regular expression "
            "for match=regex (max 500 characters). Only used when type='text'."
        ),
    )
    match: Literal["contains", "exact", "regex", "fuzzy"] = Field(
        default="contains",
        description=(
            "How 'pattern' is matched against the document text. "
            "'contains': substring anywhere. "
            "'exact': whole word or whole line (word-boundary anchored). "
            "'regex': Python regular expression, re.search per page. "
            "'fuzzy': best sliding-window similarity — the mode to use on OCR'd "
            "text, where 'Notice to Owner' often comes back as 'Notlce to 0wner'. "
            "Only used when type='text'."
        ),
    )
    case_sensitive: bool = Field(
        default=False,
        description="Match case-sensitively. Default False (both sides are case-folded).",
    )
    fuzzy_threshold: float = Field(
        default=0.85,
        ge=0.0,
        le=1.0,
        description=(
            "Similarity (0.0–1.0) a window must reach to count as a hit when match='fuzzy'. "
            "Below the threshold the criterion still scores 1-6 proportionally to the best "
            "ratio seen, rather than a flat fail. Ignored by the other match modes."
        ),
    )
    min_count: int = Field(
        default=1,
        ge=1,
        description=(
            "How many hits are required before the criterion PASSes. "
            "Use 2+ for 'the phrase must appear on every copy'. Only used when type='text'."
        ),
    )


class DocumentInput(BaseModel):
    """A document supplied as either a base64 string or a remote URL.

    Used in the JSON body of POST /assess/compare. Any supported kind works —
    JPEG/PNG, PDF, plain text, or .docx — and the kind is determined from the
    bytes, not from the URL or any declared content type (see
    common.documents.detect). The 'type' field only says how to obtain the
    bytes in analysis._load_bytes_from_input(); URLs are SSRF-checked before
    fetching (see ssrf.py).
    """

    data: str = Field(description="Base64-encoded document bytes or a URL.")
    type: Literal["base64", "url"] = Field(description="Whether data is 'base64' or 'url'.")


# Backwards-compatible alias: this model was ImageInput before document
# support landed. The shape is unchanged, so existing imports keep working.
ImageInput = DocumentInput


class ExampleInput(BaseModel):
    """A reference document to compare the subject against in /assess/compare.

    Each example carries its own weight (how much similarity to this example
    influences the combined score) and optionally a pre-generated analysis
    (to skip the LLM call for a reference document that was already assessed).
    """

    data: str = Field(description="Base64-encoded document bytes or a URL.")
    type: Literal["base64", "url"] = Field(description="Whether data is 'base64' or 'url'.")
    weight: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description=(
            "How much similarity to this example influences the combined score (0.0–1.0). "
            "0.0 = ignore example entirely; 1.0 = only similarity matters."
        ),
    )
    pre_generated_analysis: Optional[dict] = Field(
        default=None,
        description=(
            "A prior analysis result for this example document. "
            "Provide this to skip the LLM call and save tokens — "
            "the value should be the 'example_analysis' dict from a previous response."
        ),
    )


class CompareRequest(BaseModel):
    """Full request body for POST /assess/compare.

    Compares a subject document against one or more reference examples,
    producing per-example similarity scores and an aggregate verdict.
    """

    image: DocumentInput
    criteria: list[CriterionInput] = Field(
        default=DEFAULT_CRITERIA,
        description="List of criteria objects, each with a name and type ('llm', 'cv', or 'text').",
    )
    aggregation: Literal["mean", "min", "max"] = Field(
        default="mean",
        description=(
            "How to collapse per-example combined scores into a single aggregate verdict. "
            "mean = balanced; min = strictest (must match all examples); "
            "max = most lenient (must match any example)."
        ),
    )
    ocr: Literal["auto", "always", "never"] = Field(
        default="auto",
        description=(
            "Text-recognition policy applied to the subject and every live example. "
            "'auto' (default): OCR only when text is needed and not already there — "
            "any text criterion is present, or the document is a scan (page images "
            "with no native text layer) with llm criteria to judge. "
            "'always': OCR every page image even when a native text layer exists. "
            "'never': skip OCR entirely; text criteria then fail with "
            "'no text available'. Also effectively 'never' when the deployment sets "
            "CLASSIFIER_OCR_ENGINE=none."
        ),
    )
    examples: list[ExampleInput] = Field(
        min_length=1,
        description="One or more reference documents to compare the subject against.",
    )
