"""Per-type criterion options: the models, their defaults, and their caps.

A criterion's top level holds only what every type shares — ``name``,
``type``, ``weight``, ``depends_on``, ``score``. Everything that only means
something to one evaluation path lives in ``options``, validated by the model
for that path:

    llm       LLMOptions       hint, boxes, max_attempts, ocr, reference, aggregate
    text      TextOptions      pattern, match, case_sensitive, fuzzy_threshold,
                               min_count, ocr, scope, aggregate
    cv        CVOptions        fallback, reference, aggregate
    detector  DetectorOptions  threshold, aggregate

``reference`` (``ReferenceOptions``) says how the request's ``references`` —
stored worked examples, see ``references/`` — guide THIS criterion: whether
at all, which reference criterion is its example, how several examples'
answers combine, and the opt-in position check. It exists only where the
vision model answers: ``llm``, and a ``cv`` name that falls back to the llm.
On ``text`` / ``detector`` it is refused by name ("references guide the
vision model; ...") rather than as an unknown key, and on a ``cv`` name an
OpenCV detector or the detector service answers it is refused the same way
(``api.schemas``). It appears in ``resolve()`` — and so in ``options_used`` —
only when the caller sent it, so a criterion without it resolves exactly as
before.

``aggregate`` is on every type: how a criterion's per-ITEM results (one per
page of every document) collapse into one answer — pages into a per-document
answer, then documents into the request's. A string applies to both levels;
``{"pages": <rule>, "documents": <rule>}`` sets them apart. Rules:

    any          the best item (highest score): PASS if any item passes
    worst / all  the lowest item: every item must pass ("all" is an alias)
    mean         the mean of the scores, verdict from that mean
    sum          ``text`` only: hit counts added up, then scored against
                 ``min_count`` (a 400 on any other type)

Omitted levels default from the type (and an ``llm`` criterion's hint) —
``default_aggregate`` below is the one table.

Every model is ``extra="forbid"`` and ``strict=True``: an unknown key, a
string where a number belongs, or a value past a server cap is a 400 at
submit naming the field, not a job that does something the caller did not
ask for. ``resolve()`` turns the options a caller sent into the values the
evaluator will actually use (defaults filled, caps applied); that dict is
echoed on every result as ``options_used``.

``criterion_types()`` is the payload of ``GET /criterion-types``: each
type's JSON schema straight from pydantic, its resolved defaults, and its
caps — so a caller can build a form, or check a request, against the
container that will run it.

Process flow position: the bottom of the ``api`` package next to
``api.schemas``, which is the only module that imports it for validation.
Imports ``config``, ``cv`` (the detector registry, to know whether a ``cv``
name has an OpenCV detector) and ``detector.client`` (whether DETECTOR_URL is
set) — none of which import back.
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from common.documents import MAX_PATTERN_CHARS

from config import (
    CRITERION_NAME_MAX_CHARS as NAME_MAX_CHARS,
    DETECTOR_MIN_SCORE,
    LLM_BBOX_MAX_ATTEMPTS,
    REFERENCE_MAX_PER_CRITERION,
    REFERENCE_MAX_PER_REQUEST,
    REFERENCE_POSITION_CAP,
    REFERENCE_POSITION_MAX_OFFSET,
    REFERENCE_POSITION_MIN_IOU,
    VISION_LLM_MAX_IMAGES_PER_PROMPT,
    TEXT_MIN_COUNT_CAP as MIN_COUNT_CAP,
)
from cv import get_detector
from cv.quality import check_blur, check_exposure
# A module, not names: `detector_client.is_configured()` reads DETECTOR_URL at
# call time, which is what lets a test point it at a stub.
from detector import client as detector_client

OcrMode = Literal["auto", "always", "never"]

# Server caps a caller cannot exceed — NAME_MAX_CHARS and MIN_COUNT_CAP are
# the config env knobs under their local names (api.schemas imports them here).

# The two whole-page OpenCV measurements. They score a property of the page,
# not a thing on it, so they emit no regions — which matters for
# `score: false`, where a criterion with no possible geometry is refused.
_WHOLE_PAGE_DETECTORS = (check_blur, check_exposure)


AggregateRule = Literal["any", "worst", "all", "mean", "sum"]
AGGREGATE_RULES: tuple[str, ...] = ("any", "worst", "all", "mean", "sum")
AGGREGATE_LEVELS: tuple[str, ...] = ("pages", "documents")


class AggregateSplit(BaseModel):
    """``{"pages": <rule>, "documents": <rule>}`` — either may be omitted."""

    model_config = ConfigDict(extra="forbid", strict=True)

    pages: Optional[AggregateRule] = Field(
        default=None,
        description="How one document's pages collapse into its answer.",
    )
    documents: Optional[AggregateRule] = Field(
        default=None,
        description="How the documents' answers collapse into the request's.",
    )


def default_aggregate(type_: str, hint: Optional[str] = None) -> dict[str, str]:
    """The default rules per level, from the type (and an llm hint).

        llm presence, detector        pages any    documents all
        llm quality / auto, cv        pages worst  documents worst
        text                          pages sum    documents sum

    A presence question asks "is it on SOME page" of each document, and then
    wants every document to have it; a quality question wants every page good.
    """
    if type_ == "text":
        return {"pages": "sum", "documents": "sum"}
    if type_ == "detector" or (type_ == "llm" and hint == "presence"):
        return {"pages": "any", "documents": "all"}
    return {"pages": "worst", "documents": "worst"}


class _Options(BaseModel):
    """Shared config: no unknown keys, no silent type coercion."""

    model_config = ConfigDict(extra="forbid", strict=True)

    # Only `text` may add hit counts up; every other type's result is a score.
    _ALLOWS_SUM: ClassVar[bool] = False

    aggregate: Optional[Union[AggregateRule, AggregateSplit]] = Field(
        default=None,
        description=(
            "How the per-item results (one per page of every document) collapse: "
            "a rule for both levels, or {\"pages\": rule, \"documents\": rule}. "
            "Rules: 'any' (best item), 'worst' / 'all' (lowest item — every item "
            "must pass), 'mean' (mean score), 'sum' (text only: hit counts added). "
            "Omitted levels default from the type — see GET /criterion-types."
        ),
    )

    @field_validator("aggregate", mode="before")
    @classmethod
    def _aggregate_shape(cls, value: Any) -> Any:
        """One readable sentence instead of pydantic's union-branch errors."""
        expected = (
            f"aggregate must be one of {', '.join(AGGREGATE_RULES)}, or an object "
            "{\"pages\": rule, \"documents\": rule}"
        )
        if value is None or isinstance(value, AggregateSplit):
            return value
        if isinstance(value, str):
            if value not in AGGREGATE_RULES:
                raise ValueError(f"{expected}; got {value!r}")
            return value
        if isinstance(value, dict):
            stray = sorted(k for k in value if k not in AGGREGATE_LEVELS)
            if stray:
                raise ValueError(f"{expected}; unknown level(s) {stray}")
            for level, rule in value.items():
                if rule is not None and rule not in AGGREGATE_RULES:
                    raise ValueError(f"{expected}; got {level}: {rule!r}")
            return value
        raise ValueError(f"{expected}; got {type(value).__name__}")

    @model_validator(mode="after")
    def _sum_is_text_only(self) -> "_Options":
        if not self._ALLOWS_SUM and "sum" in self._given_rules().values():
            raise ValueError(
                "aggregate 'sum' adds text hit counts and is only for text criteria; "
                "use any, worst / all, or mean"
            )
        return self

    def _given_rules(self) -> dict[str, str]:
        """The levels the caller set explicitly."""
        agg = self.aggregate
        if agg is None:
            return {}
        if isinstance(agg, str):
            return {level: agg for level in AGGREGATE_LEVELS}
        return {
            level: rule
            for level, rule in (("pages", agg.pages), ("documents", agg.documents))
            if rule is not None
        }

    def resolved_aggregate(self, defaults: dict[str, str]) -> dict[str, str]:
        """``{"pages", "documents"}`` — explicit levels over the defaults."""
        return {**defaults, **self._given_rules()}

    def resolve(self, name: str) -> dict[str, Any]:  # pragma: no cover - overridden
        raise NotImplementedError

    @classmethod
    def caps(cls) -> dict[str, Any]:
        return {}


class ReferenceOptions(BaseModel):
    """``options.reference`` — how the request's references guide one criterion.

    Only meaningful when the request lists ``references``; sending it without
    them is a 400. Every field has a default, so ``{}`` means "guided, the
    defaults" — the same as omitting it on a request that has references.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    use: bool = Field(
        default=True,
        description="false = score this criterion without examples even though the "
                    "request lists references.",
    )
    criterion: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=NAME_MAX_CHARS,
        description="Which reference criterion is the example (matched case-insensitively). "
                    "Defaults to this criterion's own name. Named but in no listed "
                    "reference: a 400.",
    )
    position: Literal["off", "check"] = Field(
        default="off",
        description="'check': compare this criterion's located box with the example's "
                    "(each as a fraction of its own page) — a miss caps the score at "
                    "CLASSIFIER_REFERENCE_POSITION_CAP. Needs options.boxes: true and a "
                    "presence / auto hint; no model call.",
    )
    min_iou: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Overlap that counts as a position HIT. Defaults to "
                    "CLASSIFIER_REFERENCE_POSITION_MIN_IOU.",
    )
    combine: Literal["any", "all", "mean"] = Field(
        default="any",
        description="How several examples' answers become one score: 'any' the best "
                    "(with its reason), 'all' the lowest, 'mean' the rounded mean.",
    )

    def resolve(self, name: str) -> dict[str, Any]:
        return {
            "use": self.use,
            "criterion": self.criterion or name,
            "position": self.position,
            "min_iou": REFERENCE_POSITION_MIN_IOU if self.min_iou is None else self.min_iou,
            "combine": self.combine,
        }


def _reference_field() -> Any:
    return Field(
        default=None,
        description="How the request's `references` guide this criterion — see GET "
                    "/criterion-types `reference`. Only with `references` on the request.",
    )


class LLMOptions(_Options):
    """Options for ``type: "llm"`` — the vision model scores the criterion."""

    hint: Literal["quality", "presence", "auto"] = Field(
        default="auto",
        description=(
            "Which rubric the model applies. 'quality': 1-3 FAIL, 4-6 MARGINAL, "
            "7-10 PASS. 'presence': 10 present, 5 uncertain, 1 absent, with "
            "evidence-first reasoning. 'auto': the model infers the rubric from "
            "the name. See GET /hints."
        ),
    )
    boxes: bool = Field(
        default=False,
        description=(
            "Run the bounding-box enforcement loop after scoring: ask for a box "
            "on a labelled 0-1000 grid, refine on a zoomed crop, verify the "
            "crop alone, retry with the rejection as feedback. Only for hint "
            "presence/auto, only when the presence score is 7+, only on a page "
            "image. Never changes the score. Costs up to 3 small model calls "
            "per attempt."
        ),
    )
    max_attempts: Optional[int] = Field(
        default=None,
        ge=1,
        description=(
            "Attempts the enforcement loop gets. Defaults to, and may not "
            "exceed, the server's CLASSIFIER_LLM_BBOX_MAX_ATTEMPTS."
        ),
    )
    ocr: OcrMode = Field(
        default="auto",
        description=(
            "The text layer sent with the prompt. 'auto': OCR when the page has "
            "an image and its native text is under "
            "CLASSIFIER_OCR_MIN_NATIVE_CHARS. 'always': recognise regardless. "
            "'never': native text only."
        ),
    )

    reference: Optional[ReferenceOptions] = _reference_field()

    @model_validator(mode="after")
    def _position_needs_boxes(self) -> "LLMOptions":
        if self.reference is not None and self.reference.position == "check":
            if not self.boxes or self.hint == "quality":
                raise ValueError(
                    "reference.position 'check' compares the LOCATED box with the "
                    "example's, so it needs options.boxes: true and hint presence or "
                    "auto (a quality criterion has no location)"
                )
        return self

    @field_validator("max_attempts")
    @classmethod
    def _cap_attempts(cls, value: Optional[int]) -> Optional[int]:
        if value is not None and value > LLM_BBOX_MAX_ATTEMPTS:
            raise ValueError(
                f"max_attempts {value} exceeds this server's cap "
                f"(CLASSIFIER_LLM_BBOX_MAX_ATTEMPTS={LLM_BBOX_MAX_ATTEMPTS}); "
                f"send {LLM_BBOX_MAX_ATTEMPTS} or less, or omit it for the cap"
            )
        return value

    def resolve(self, name: str) -> dict[str, Any]:
        resolved = {
            "hint": self.hint,
            "boxes": self.boxes,
            "max_attempts": self.max_attempts or LLM_BBOX_MAX_ATTEMPTS,
            "ocr": self.ocr,
            "aggregate": self.resolved_aggregate(default_aggregate("llm", self.hint)),
        }
        return _with_reference(resolved, self.reference, name)

    @classmethod
    def caps(cls) -> dict[str, Any]:
        return {"max_attempts": LLM_BBOX_MAX_ATTEMPTS}


class TextOptions(_Options):
    """Options for ``type: "text"`` — deterministic search of the text layer."""

    pattern: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=MAX_PATTERN_CHARS,
        description=(
            "What to search for. Defaults to the criterion's name. Literal text "
            "for contains/exact/fuzzy; a Python regular expression for regex."
        ),
    )
    match: Literal["contains", "exact", "regex", "fuzzy"] = Field(
        default="contains",
        description=(
            "'contains': substring anywhere. 'exact': whole word / whole line. "
            "'regex': re.search. 'fuzzy': best sliding-window similarity — the "
            "mode for OCR'd text."
        ),
    )
    case_sensitive: bool = Field(default=False, description="Match case-sensitively.")
    fuzzy_threshold: float = Field(
        default=0.85,
        ge=0.0,
        le=1.0,
        description=(
            "Similarity a fuzzy window must reach to count as a hit. Below it "
            "the criterion scores 1-6 by how close it got."
        ),
    )
    min_count: int = Field(
        default=1,
        ge=1,
        le=MIN_COUNT_CAP,
        description="Hits required to PASS.",
    )
    ocr: OcrMode = Field(
        default="auto",
        description=(
            "The text layer searched. 'auto': OCR when the page has an image "
            "and its native text is under CLASSIFIER_OCR_MIN_NATIVE_CHARS. "
            "'always': recognise regardless. 'never': native text only."
        ),
    )

    scope: Literal["page", "document"] = Field(
        default="page",
        description=(
            "'page': one search per page (per item), aggregated. 'document': one "
            "search over each document's pages joined in page order with a single "
            "space, so a phrase broken across a page break still matches; hits map "
            "back to the page they landed on, and only the documents level of "
            "aggregate applies."
        ),
    )

    _ALLOWS_SUM: ClassVar[bool] = True

    def resolve(self, name: str) -> dict[str, Any]:
        return {
            "pattern": self.pattern or name,
            "match": self.match,
            "case_sensitive": self.case_sensitive,
            "fuzzy_threshold": self.fuzzy_threshold,
            "min_count": self.min_count,
            "ocr": self.ocr,
            "scope": self.scope,
            "aggregate": self.resolved_aggregate(default_aggregate("text")),
        }

    @classmethod
    def caps(cls) -> dict[str, Any]:
        return {"pattern_max_chars": MAX_PATTERN_CHARS, "min_count": MIN_COUNT_CAP}


class CVOptions(_Options):
    """Options for ``type: "cv"`` — a registered OpenCV detector by name."""

    fallback: Optional[Literal["detector", "llm"]] = Field(
        default=None,
        description=(
            "What answers when no OpenCV detector matches the name (see GET "
            "/cv-detectors). 'detector': the open-vocabulary detector service, "
            "scored from its boxes; 'llm': the vision model with the default "
            "llm options. Omitted: 'detector' when DETECTOR_URL is configured "
            "on this container, else 'llm'."
        ),
    )

    reference: Optional[ReferenceOptions] = _reference_field()

    @model_validator(mode="after")
    def _no_position_on_cv(self) -> "CVOptions":
        if self.reference is not None and self.reference.position == "check":
            raise ValueError(
                "reference.position 'check' needs a located box, and a cv criterion's "
                "llm fallback runs without boxes; use an llm criterion with "
                "options.boxes: true"
            )
        return self

    def resolve(self, name: str) -> dict[str, Any]:
        default = "detector" if detector_client.is_configured() else "llm"
        resolved = {
            "fallback": self.fallback or default,
            "aggregate": self.resolved_aggregate(default_aggregate("cv")),
        }
        return _with_reference(resolved, self.reference, name)


class DetectorOptions(_Options):
    """Options for ``type: "detector"`` — the open-vocabulary detector."""

    threshold: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "Confidence floor, sent as the detector's threshold AND the line a "
            "box must clear to count as evidence. Defaults to DETECTOR_MIN_SCORE."
        ),
    )

    def resolve(self, name: str) -> dict[str, Any]:
        return {
            "threshold": DETECTOR_MIN_SCORE if self.threshold is None else self.threshold,
            "aggregate": self.resolved_aggregate(default_aggregate("detector")),
        }


def _with_reference(
    resolved: dict[str, Any], reference: Optional[ReferenceOptions], name: str
) -> dict[str, Any]:
    """``resolved`` plus ``reference`` — only when the caller sent one, so a
    criterion without it resolves (and echoes ``options_used``) exactly as
    it always did."""
    if reference is None:
        return resolved
    return {**resolved, "reference": reference.resolve(name)}


# The message a reference on a criterion the model does not answer gets.
NOT_GUIDED = (
    "references guide the vision model; this criterion is not answered by it"
)


def answered_by_llm(type_: str, name: str, resolved: dict[str, Any]) -> bool:
    """Whether the vision model answers the criterion — the only kind a
    reference can guide: ``llm``, or a ``cv`` name with no OpenCV detector
    whose fallback resolves to the llm."""
    if type_ == "llm":
        return True
    if type_ == "cv":
        return get_detector(name) is None and resolved.get("fallback") == "llm"
    return False


OPTIONS_MODELS: dict[str, type[_Options]] = {
    "llm": LLMOptions,
    "text": TextOptions,
    "cv": CVOptions,
    "detector": DetectorOptions,
}

# Keys that used to live at the top level of a criterion. Named in the 400 so
# a caller porting an old request is told where each one went.
LEGACY_TOP_LEVEL_KEYS: dict[str, str] = {
    "hint": "llm",
    "pattern": "text",
    "match": "text",
    "case_sensitive": "text",
    "fuzzy_threshold": "text",
    "min_count": "text",
}


def can_locate(type_: str, name: str, resolved: dict[str, Any]) -> bool:
    """Whether a criterion can produce ANY geometry, given its resolved options.

    ``score: false`` means "locate without judging"; a criterion that cannot
    locate anything would then do nothing at all, so it is refused at submit.

      * llm       — only with ``boxes`` and a presence/auto hint (the loop
                    never boxes a quality criterion: sharpness is not a place).
      * text      — yes (OCR line polygons, or native-PDF rectangles).
      * detector  — yes.
      * cv        — a feature detector yes; the two whole-page measurements
                    (sharpness, exposure) no; no OpenCV detector at all →
                    whatever the fallback does (detector yes, llm no — the
                    fallback runs with default llm options, boxes off).
    """
    if type_ == "llm":
        return bool(resolved.get("boxes")) and resolved.get("hint") in ("presence", "auto")
    if type_ in ("text", "detector"):
        return True
    if type_ == "cv":
        fn = get_detector(name)
        if fn is not None:
            return fn not in _WHOLE_PAGE_DETECTORS
        return resolved.get("fallback") == "detector"
    return False


def criterion_types() -> dict[str, Any]:
    """The ``GET /criterion-types`` payload: schema, defaults, caps per type."""
    types: dict[str, Any] = {}
    for type_, model in OPTIONS_MODELS.items():
        types[type_] = {
            "options_schema": model.model_json_schema(),
            # Defaults as the evaluator would resolve them for a criterion
            # named "<name>" — `pattern` shows the name it defaults to.
            "defaults": model().resolve("<name>"),
            "caps": model.caps(),
        }
    return {
        "shared_fields": {
            "name": f"string, 1-{NAME_MAX_CHARS} characters, unique in the request",
            "type": "llm | text | cv | detector (default llm)",
            "weight": "number > 0 (default 1)",
            "depends_on": "name of another scored criterion that must PASS first",
            "score": "bool (default true); false = locate without judging",
        },
        # options.aggregate is on every type; this is what the rules mean and
        # what an omitted level resolves to. `defaults` in each type above is
        # the same table for a criterion with that type's default options.
        "aggregate": {
            "levels": {
                "pages": "one document's pages (items) → that document's answer",
                "documents": "the documents' answers → the request's answer",
            },
            "rules": {
                "any": "the best item (highest score) — PASS if any item passes",
                "worst": "the lowest item — every item must pass",
                "all": "alias of worst",
                "mean": "mean of the scores; the verdict comes from the rounded mean",
                "sum": "text only: hit counts added, then scored against min_count",
            },
            "defaults": {
                "llm (hint presence)": default_aggregate("llm", "presence"),
                "llm (hint quality / auto)": default_aggregate("llm", "quality"),
                "cv": default_aggregate("cv"),
                "detector": default_aggregate("detector"),
                "text": default_aggregate("text"),
            },
            "excluded": "skipped and errored items are left out of an aggregate; any "
                        "errored item makes the criterion incomplete",
            "text_scope_document": "a text criterion with options.scope 'document' is "
                                   "one unit per document, so only the documents rule applies",
        },
        "types": types,
        # options.reference: on llm criteria and cv names that fall back to
        # the llm only. The live caps and knobs of THIS container.
        "reference": {
            "options_schema": ReferenceOptions.model_json_schema(),
            "defaults": ReferenceOptions().resolve("<name>"),
            "applies_to": "llm criteria, and cv criteria answered by the llm fallback",
            "requires": "a `references` list on the request",
            "caps": {
                "max_per_request": REFERENCE_MAX_PER_REQUEST,
                "max_per_criterion": REFERENCE_MAX_PER_CRITERION,
                "images_per_llm_prompt": VISION_LLM_MAX_IMAGES_PER_PROMPT,
            },
            "position": {
                "min_iou": REFERENCE_POSITION_MIN_IOU,
                "max_offset": REFERENCE_POSITION_MAX_OFFSET,
                "cap": REFERENCE_POSITION_CAP,
                "requires": "options.boxes: true and hint presence / auto",
            },
        },
        "detector_configured": detector_client.is_configured(),
    }
