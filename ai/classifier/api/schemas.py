"""Pydantic request/response models for the classifier API.

These models are used both for request validation (FastAPI deserialises
incoming JSON into them) and for type safety throughout the analysis pipeline.

Data model relationships
------------------------
  POST /assess
    file (UploadFile)  +  criteria (list[CriterionInput])  +  ocr
        → queued as an assess job → analyzed by the analysis package

  POST /assess/compare
    CompareRequest
      ├── image     : DocumentInput       — the subject document
      ├── criteria  : list[CriterionInput]— what to evaluate
      ├── aggregation: mean|min|max       — how to collapse N example scores
      ├── ocr       : auto|always|never   — text-recognition policy
      ├── examples  : list[ExampleInput]  — reference documents to compare against
      └── regions   : RegionsOptions|None — where-did-you-find-it, off by default

  POST /locate
    LocateRequest (multipart: file + the fields below as form values)
      ├── features  : list[CriterionInput]— what to find, scoring suppressed
      ├── ocr       : auto|always|never
      └── regions   : RegionsOptions      — enabled is implied

"Document" here means any supported upload: JPEG/PNG, PDF, plain text, or
.docx (see common.documents). The fields are still named ``image`` for
backwards compatibility with existing callers — a JPEG is simply the
single-page case.

``ClassifierMetadata`` is the other half of the shape: not a request body, but
the per-job blob stored in ``jobs.metadata`` and echoed by GET /jobs.

Process flow position: the bottom of the ``api`` package and the one module in
it that everything else imports — ``analysis``, ``llm``, ``regions``,
``compare`` and ``jobs`` all validate against these models, which is why
``api/__init__.py`` imports nothing (a router import there would close the
cycle).
"""

import json
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

from config import DEFAULT_CRITERIA, REGION_LAYER_FORMATS


class ClassifierMetadata(BaseModel):
    """Per-job metadata for the classifier. Everything that used to live in
    the legacy ``type`` + ``request_id`` columns is now stored in the shared
    ``jobs.metadata`` JSON blob under this shape."""

    type: str        # "assess" | "compare" | "locate"
    request_id: str


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
    type: Literal["cv", "llm", "text", "detector"] = Field(
        default="llm",
        description=(
            "'llm': scored by the vision LLM (no detector required). "
            "'cv': run through a registered OpenCV detector by name — no token cost. "
            "If no detector matches the criterion name it is answered by the "
            "open-vocabulary detector when `regions.detector` is set and "
            "DETECTOR_URL is configured, and falls back to 'llm' otherwise. "
            "'detector': always answered by the open-vocabulary detector service "
            "(ai/detector) using the criterion name as the text prompt — boxes and "
            "a score, no token cost, whether or not layers were requested. Falls "
            "back to 'llm' when the service is unconfigured or unreachable. "
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


class RegionsOptions(BaseModel):
    """Whether — and how — a request wants to be told *where*.

    Off by default on `/assess` and `/assess/compare`: collecting regions
    costs extra detector work and writing layers costs disk, and most callers
    only want a score. `/locate` implies ``enabled=True`` — that endpoint is
    nothing but this.

    ``regions.json`` and ``manifest.json`` are always written when ``enabled``
    is true, whatever ``layers`` says; ``layers: []`` means "the machine
    -readable geometry only, no pictures".

    Every field does something as of phases 3 and 4. Two of them —  ``diff``
    and ``examples`` — need a reference document, so they apply to
    `/assess/compare` only; set on `/assess` or `/locate` they are recorded
    in the manifest and `artifacts.notes` says they were ignored.
    """

    enabled: bool = Field(
        default=True,
        description=(
            "Collect regions at all. The multipart shorthand on /assess sets "
            "this: `regions=false` (or an absent field) off, `regions=true` on "
            "with the SVG layer, `regions=svg,png` on with those layers."
        ),
    )
    layers: list[Literal["svg", "png", "preview"]] = Field(
        default_factory=lambda: ["svg"],
        description=(
            "Rendered formats to write, any subset. 'svg': overlay whose "
            "viewBox is the original page, one <g> per criterion (the default "
            "and the cheapest). 'png': transparent RGBA layer at full page "
            "size. 'preview': the page with the layer burned in, JPEG — also "
            "writes an un-annotated `p{n}.base.jpg` so a FILTERED preview can "
            "be re-rendered later. `[]` writes regions.json only."
        ),
    )
    layers_per_criterion: bool = Field(
        default=False,
        description=(
            "Pre-render one file per criterion per page at job time "
            "(`p0.<slug>.svg`, …) instead of on first fetch. For pipelines "
            "that will pull every one anyway; with 8 criteria × 20 pages × 3 "
            "formats it is a lot of files nobody may open, hence off."
        ),
    )
    llm_boxes: bool = Field(
        default=False,
        description=(
            "Run the bounding-box enforcement loop for every `llm` criterion "
            "with hint presence/auto that the model scored 7 or higher: ask "
            "for a box on a 0-1000 grid, validate it, crop it out of the "
            "ORIGINAL page and ask whether the feature is visible in the crop "
            "alone, retry with the rejection as feedback, up to "
            "CLASSIFIER_LLM_BBOX_MAX_ATTEMPTS. EVERY attempt comes back in "
            "`localization.attempts` and as a region (rejected ones carry "
            "`attrs.accepted: false` and are reachable at `?attempt=n`). "
            "Costs up to two small LLM calls per attempt per criterion, which "
            "is why it is off by default. It runs AFTER scoring and never "
            "changes a score or a verdict. Needs a page image; on /locate it "
            "triggers a presence-only scoring call to have something to gate "
            "on, whose judgement is then discarded."
        ),
    )
    detector: bool = Field(
        default=False,
        description=(
            "Hand the open-vocabulary detector service (ai/detector) the "
            "IMPLICIT work: every `cv` criterion with no registered OpenCV "
            "detector is scored from its boxes instead of costing a vision-LLM "
            "call, and every `llm` criterion with hint presence/auto is "
            "localised by it (the model still scores those). Needs DETECTOR_URL; "
            "without it the request still succeeds and `artifacts.notes` says "
            "the service is not configured. A criterion spelled "
            "`\"type\": \"detector\"` uses the service whether or not this is set."
        ),
    )
    diff: bool = Field(
        default=False,
        description=(
            "/assess/compare only: change detection between the subject and "
            "each LIVE single-image example. ORB features and a RANSAC "
            "homography align the two, then a thresholded difference yields "
            "`source=\"diff\"` polygons with `attrs.change` of added / removed "
            "/ changed, filed under the synthetic criterion `_diff:e{i}` and "
            "rendered as `diff-e{i}-p0.<fmt>`. Under "
            "CLASSIFIER_DIFF_MIN_INLIERS the result is `aligned: false` with "
            "NO regions — different framing is reported, not guessed at. Diff "
            "regions are NOT criteria: they never reach the weighted score or "
            "the similarity comparison. An example supplied as "
            "`pre_generated_analysis` carries no pixels and is skipped with a "
            "note."
        ),
    )
    examples: bool = Field(
        default=False,
        description=(
            "/assess/compare only: also collect regions and render layers for "
            "the LIVE example documents, as `e{i}.p{n}.<fmt>` in the SAME job "
            "directory (an example has no job of its own). Off by default "
            "because it doubles the artifact volume; the subject is what a "
            "caller is usually looking at."
        ),
    )

    @field_validator("layers", mode="before")
    @classmethod
    def _normalise_layers(cls, value):
        """Accept ``"svg,png"`` as well as ``["svg", "png"]``.

        The multipart shorthand hands a comma list straight through, and a
        JSON caller who writes the same string should not get a 422 for it.
        Duplicates are collapsed and order is normalised so two spellings of
        the same request produce the same manifest.
        """
        if isinstance(value, str):
            value = [part.strip() for part in value.split(",")]
        if isinstance(value, (list, tuple, set)):
            seen = [str(v).strip().lower() for v in value if str(v).strip()]
            order = ["svg", "png", "preview"]
            return [fmt for fmt in order if fmt in seen]
        return value

    def as_manifest(self) -> dict:
        """The options block recorded in ``manifest.json``."""
        return self.model_dump()


def parse_regions_option(raw: Optional[str]) -> Optional[RegionsOptions]:
    """Turn the `/assess` multipart ``regions`` field into options, or None.

    Four spellings, all of which a curl user or an n8n node might produce:

        ""  / "false" / "0" / "no" / "off"  → None (regions off — the default)
        "true" / "1" / "yes" / "on"         → enabled, SVG layer
        "svg,png"  / "png"  / "none"        → enabled, those layers
                                              ("none" = regions.json only)
        '{"enabled": true, "layers": []}'   → the full object as JSON

    Returns:
        A ``RegionsOptions`` or None. Raises ``ValueError`` with an actionable
        message for anything else, which the endpoint turns into a 400.
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text or text.lower() in ("false", "0", "no", "off"):
        return None
    if text.lower() in ("true", "1", "yes", "on"):
        return RegionsOptions(enabled=True, layers=["svg"])
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"regions is not valid JSON: {exc}")
        if not isinstance(data, dict):
            raise ValueError("regions JSON must be an object")
        return RegionsOptions(**data)
    if text.lower() in ("none", "json", "regions.json"):
        return RegionsOptions(enabled=True, layers=[])

    formats = [part.strip().lower() for part in text.split(",") if part.strip()]
    unknown = [f for f in formats if f not in REGION_LAYER_FORMATS]
    if unknown:
        raise ValueError(
            f"Unknown regions layer(s) {unknown}. Expected any of "
            f"{sorted(REGION_LAYER_FORMATS)}, the shorthand 'true'/'false', "
            "'none' for regions.json only, or a JSON object."
        )
    return RegionsOptions(enabled=True, layers=formats)


class DocumentInput(BaseModel):
    """A document supplied as either a base64 string or a remote URL.

    Used in the JSON body of POST /assess/compare. Any supported kind works —
    JPEG/PNG, PDF, plain text, or .docx — and the kind is determined from the
    bytes, not from the URL or any declared content type (see
    common.documents.detect). The 'type' field only says how to obtain the
    bytes in analysis.loading._load_bytes_from_input(); URLs are SSRF-checked
    before fetching (see analysis.loading.validate_url).
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
    regions: Optional[RegionsOptions] = Field(
        default=None,
        description=(
            "Where-did-you-find-it options. Omit (the default) for no regions. "
            "Layers are rendered for the SUBJECT document unless "
            "`examples: true`; `diff: true` adds change-detection layers "
            "against each live example. See API.md § Regions and layers."
        ),
    )


class LocateRequest(BaseModel):
    """Body shape for POST /locate, for documentation and validation only.

    The endpoint itself is multipart (it takes a file), so FastAPI does not
    bind this model directly; ``main.locate_document`` validates the parsed
    ``features`` array against it. It exists so the accepted shape has one
    definition rather than being implied by the parsing code.
    """

    features: list[CriterionInput] = Field(
        min_length=1,
        description=(
            "What to find. Each entry is either a bare string (the feature "
            "name — resolved to a registered OpenCV detector when one matches "
            "the name, else to the open-vocabulary detector when DETECTOR_URL "
            "is configured, else to the LLM) or a full CriterionInput object, "
            "which is what lets a text feature set `match`, `pattern`, and the "
            "rest."
        ),
    )
    ocr: Literal["auto", "always", "never"] = Field(default="auto")
    regions: RegionsOptions = Field(default_factory=RegionsOptions)
