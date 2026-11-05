"""Per-type criterion options: the models, their defaults, and their caps.

A criterion's top level holds only what every type shares — ``name``,
``key`` (reference matching only), ``type``, ``weight``, ``depends_on``,
``score``. Everything that only means
something to one evaluation path lives in ``options``, validated by the model
for that path:

    llm       LLMOptions       hint, boxes, max_attempts, refine,
                               verify_partial, verify_min_span, ocr,
                               reference, model, aggregate
    text      TextOptions      pattern, match, case_sensitive, fuzzy_threshold,
                               min_count, ocr, scope, aggregate
    cv        CVOptions        fallback, reference, model, aggregate
    detector  DetectorOptions  threshold, aggregate

**The box loop's settings, per criterion** (ZT-527). ``refine``,
``verify_partial`` and ``verify_min_span`` on ``llm`` options, and
``reference.boxes`` (``{ask, refine, verify}``) and ``reference.text``,
tune the bounding-box loop (``llm.boxes``) for THIS criterion: the refine
pass by numbered OCR-line marks, the verify's "cut off" field, a taller
verify crop, and which of the loop's calls are shown the criterion's
reference examples (and whether their captions quote the region's text).
Each is optional; unset (None) is the SERVICE DEFAULT, the env var of the
same meaning (``CLASSIFIER_LLM_BBOX_REFINE_MODE``, ``_VERIFY_PARTIAL``,
``_VERIFY_MIN_SPAN``, ``CLASSIFIER_REFERENCE_BOX_ASK`` / ``_REFINE`` /
``_VERIFY``, ``CLASSIFIER_REFERENCE_BOX_TEXT`` — all off by default). Set,
the criterion's value wins. ``resolve()`` fills the default in, so
``options_used`` shows what actually ran, and ``box_loop_settings`` turns
the resolved options into the keywords ``analysis.llm_eval`` hands the loop
— the one place the service reads those env vars (``llm.boxes`` keeps its
own defaults only for a direct caller, ``bin/grounding_experiment.py``).
They tune the loop, so they are refused unless ``boxes`` is true, and on a
``cv`` criterion (its llm fallback never locates); on ``text`` /
``detector`` they are unknown keys. The marks cap
(``CLASSIFIER_LLM_BBOX_MARKS_MAX``) stays service-wide. None of them ever
moves a score or a verdict: the loop runs after scoring.

``model`` picks the registry model (``models.yaml``, GET /models) that
answers the criterion — on ``llm``, and on a ``cv`` name answered by its llm
fallback. Precedence: ``options.model``, then the request's ``model``, then
the registry default. An unknown or unavailable id is a 400 naming the valid
ones. Like ``reference`` it appears in ``resolve()`` only when sent, and it
is NOT part of a criterion's identity (``without_model``): the same question
asked of another model is the same criterion.

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
container that will run it — plus a ``box_loop`` block naming the
per-criterion loop settings and this container's defaults for them.

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

# The box loop's service defaults (LLM_BBOX_REFINE_MODE … REFERENCE_BOX_TEXT)
# are module-level names read at RESOLVE time, so a test sets the service
# default by patching them here.
from config import (
    CRITERION_NAME_MAX_CHARS as NAME_MAX_CHARS,
    DETECTOR_MIN_SCORE,
    LLM_BBOX_MARKS_MAX,
    LLM_BBOX_MAX_ATTEMPTS,
    LLM_BBOX_REFINE_MODE,
    LLM_BBOX_VERIFY_MIN_SPAN,
    LLM_BBOX_VERIFY_PARTIAL,
    REFERENCE_BOX_ASK,
    REFERENCE_BOX_REFINE,
    REFERENCE_BOX_TEXT,
    REFERENCE_BOX_VERIFY,
    REFERENCE_MAX_PER_CRITERION,
    REFERENCE_MAX_PER_REQUEST,
    REFERENCE_POSITION_CAP,
    REFERENCE_POSITION_MAX_OFFSET,
    REFERENCE_POSITION_MIN_IOU,
    TEXT_MIN_COUNT_CAP as MIN_COUNT_CAP,
)
from cv import get_detector
# A module, not names: the registry is looked up at call time (tests swap it).
from llm import models as llm_models
from cv.quality import check_blur, check_exposure
# A module, not names: `detector_client.is_configured()` reads DETECTOR_URL at
# call time, which is what lets a test point it at a stub.
from detector import client as detector_client

OcrMode = Literal["auto", "always", "never"]
RefineMode = Literal["grid", "marks"]

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


def service_refine_mode() -> str:
    """The service default for ``options.refine`` — CLASSIFIER_LLM_BBOX_REFINE_
    MODE, read as ``llm.boxes`` reads it: ``marks``, or anything else is
    ``grid``."""
    return "marks" if LLM_BBOX_REFINE_MODE == "marks" else "grid"


class ReferenceBoxes(BaseModel):
    """``options.reference.boxes`` — which of the box loop's calls are shown
    the criterion's reference examples. Each step unset is the service
    default (its CLASSIFIER_REFERENCE_BOX_* knob, off unless the deployment
    turned it on); ``{}`` therefore means "the defaults"."""

    model_config = ConfigDict(extra="forbid", strict=True)

    ask: Optional[bool] = Field(
        default=None,
        description="Show the examples to the ASK call — composites (the reference page with "
                    "the feature outlined), region crops under position 'check'. The fix for "
                    "IDENTIFICATION: the model boxing a look-alike. Unset: "
                    "CLASSIFIER_REFERENCE_BOX_ASK.",
    )
    refine: Optional[bool] = Field(
        default=None,
        description="Show the examples' region crops to the REFINE call (grid or marks). "
                    "Unset: CLASSIFIER_REFERENCE_BOX_REFINE.",
    )
    verify: Optional[bool] = Field(
        default=None,
        description="Show the examples' region crops to the VERIFY call. Unset: "
                    "CLASSIFIER_REFERENCE_BOX_VERIFY.",
    )

    def resolve(self) -> dict[str, bool]:
        """``{ask, refine, verify}`` — each the value sent, else its knob."""
        return {
            "ask": REFERENCE_BOX_ASK if self.ask is None else self.ask,
            "refine": REFERENCE_BOX_REFINE if self.refine is None else self.refine,
            "verify": REFERENCE_BOX_VERIFY if self.verify is None else self.verify,
        }


class ReferenceOptions(BaseModel):
    """``options.reference`` — how the request's references guide one criterion.

    Only meaningful when the request lists ``references``; sending it without
    them is a 400. Every field has a default, so ``{}`` means "guided, the
    defaults" — the same as omitting it on a request that has references.

    ``boxes`` and ``text`` guide the BOX LOOP rather than the scoring call
    (the module docstring's per-criterion loop settings): they need
    ``options.boxes: true`` (``LLMOptions``) and are refused on ``cv``.
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
        description="Which reference criterion is the example, by its NAME (matched "
                    "case-insensitively). Wins over the criterion's `key`. Omitted: the "
                    "reference criteria with the criterion's `key`, else its own name — "
                    "the matching order is criterion → key → name. Named but in no "
                    "listed reference: a 400.",
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
    boxes: Optional[ReferenceBoxes] = Field(
        default=None,
        description="Which box-loop calls are shown the examples: {\"ask\", \"refine\", "
                    "\"verify\"}, each a boolean, each unset = its "
                    "CLASSIFIER_REFERENCE_BOX_* service default (off). Same calls, more "
                    "images: each guided call carries 1 + examples images. Examples with "
                    "regions only; a FAIL only when its reference criterion's hint is "
                    "presence. Needs options.boxes: true; refused on cv.",
    )
    text: Optional[bool] = Field(
        default=None,
        description="Each box-loop example's caption also quotes the text inside its region "
                    "(OCR'd from the reference page once per job, cut to 80 characters). "
                    "Only matters when a `boxes` step is on. Unset: "
                    "CLASSIFIER_REFERENCE_BOX_TEXT (off). Needs options.boxes: true; refused "
                    "on cv.",
    )

    def resolve(self, name: str) -> dict[str, Any]:
        return {
            "use": self.use,
            "criterion": self.criterion or name,
            "position": self.position,
            "min_iou": REFERENCE_POSITION_MIN_IOU if self.min_iou is None else self.min_iou,
            "combine": self.combine,
            # The box loop's: what will run, the service defaults filled in.
            "boxes": (self.boxes or ReferenceBoxes()).resolve(),
            "text": REFERENCE_BOX_TEXT if self.text is None else self.text,
        }

    def loop_keys_sent(self) -> list[str]:
        """The box-loop keys the caller set (``reference.boxes`` /
        ``reference.text``), for the refusals that need ``boxes``."""
        return [
            f"reference.{key}" for key, value in (("boxes", self.boxes), ("text", self.text))
            if value is not None
        ]


def _reference_field() -> Any:
    return Field(
        default=None,
        description="How the request's `references` guide this criterion — see GET "
                    "/criterion-types `reference`. Only with `references` on the request.",
    )


def _model_field(what: str) -> Any:
    return Field(
        default=None,
        min_length=1,
        max_length=64,
        description=(
            f"Which registry model {what} — an id from GET /models. Omitted: the "
            "request's `model`, else the registry default. Unknown or unavailable "
            "is a 400."
        ),
    )


def check_model_id(value: Optional[str]) -> Optional[str]:
    """A model id a request may select (known AND available), or ValueError —
    the one check every `model` field runs, so the messages agree."""
    if value is not None:
        llm_models.get_registry().check_selectable(value)
    return value


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
    # The box loop's per-criterion settings (module docstring). Unset = the
    # service default; each needs `boxes: true` (_loop_options_need_boxes).
    refine: Optional[RefineMode] = Field(
        default=None,
        description=(
            "How the loop's refine pass asks. 'grid': the zoomed crop with its own "
            "grid, a box on it. 'marks': every text line in the window (a digital "
            "PDF's own, else the OCR layer's, else an OCR pass of the window) outlined "
            "and numbered, 'which mark(s)?', the box the union of those lines — "
            "exact on a text field (an amount due), wrong for a chart or a logo "
            "(it picks text lines); falls back to the grid (one more call) when the "
            "window holds no lines or more than CLASSIFIER_LLM_BBOX_MARKS_MAX. "
            "Unset: CLASSIFIER_LLM_BBOX_REFINE_MODE (grid). Needs boxes: true."
        ),
    )
    verify_partial: Optional[bool] = Field(
        default=None,
        description=(
            "The verify also answers whether the crop cuts the feature off; a "
            "rejected cut-off crop's retry is told 'same place, larger' instead of "
            "'look elsewhere'. Never moves the score; no extra call. Unset: "
            "CLASSIFIER_LLM_BBOX_VERIFY_PARTIAL (false). Needs boxes: true."
        ),
    )
    verify_min_span: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "Grow the verify crop, centred and inside the page, to at least this "
            "share of the page on each axis (0-1; ~0.08 keeps a one-line field's "
            "label in view). The box itself is unchanged. Unset: "
            "CLASSIFIER_LLM_BBOX_VERIFY_MIN_SPAN (0, the padded box). Needs boxes: true."
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

    model: Optional[str] = _model_field("scores this criterion (and runs its box loop)")

    @field_validator("model")
    @classmethod
    def _known_model(cls, value: Optional[str]) -> Optional[str]:
        return check_model_id(value)

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

    @model_validator(mode="after")
    def _loop_options_need_boxes(self) -> "LLMOptions":
        """The loop's settings without the loop would silently do nothing —
        refused, as ``position: "check"`` is, naming every one sent."""
        if self.boxes:
            return self
        sent = [
            key for key, value in (
                ("refine", self.refine), ("verify_partial", self.verify_partial),
                ("verify_min_span", self.verify_min_span),
            ) if value is not None
        ]
        if self.reference is not None:
            sent += self.reference.loop_keys_sent()
        if sent:
            raise ValueError(
                f"{', '.join(sent)} {'tunes' if len(sent) == 1 else 'tune'} the "
                "bounding-box loop, which runs only with options.boxes: true; set "
                f"boxes: true or drop {'it' if len(sent) == 1 else 'them'}"
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
            # The loop's settings as they will run: the criterion's value, else
            # the service default — present whether or not boxes is on.
            "refine": self.refine or service_refine_mode(),
            "verify_partial": (
                LLM_BBOX_VERIFY_PARTIAL if self.verify_partial is None else self.verify_partial
            ),
            "verify_min_span": (
                LLM_BBOX_VERIFY_MIN_SPAN if self.verify_min_span is None
                else self.verify_min_span
            ),
            "ocr": self.ocr,
            "aggregate": self.resolved_aggregate(default_aggregate("llm", self.hint)),
        }
        return _with_model(_with_reference(resolved, self.reference, name), self.model)

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

    model: Optional[str] = _model_field(
        "answers this criterion when the llm fallback does (only then — a 400 otherwise)"
    )

    @field_validator("model")
    @classmethod
    def _known_model(cls, value: Optional[str]) -> Optional[str]:
        return check_model_id(value)

    @model_validator(mode="after")
    def _no_position_on_cv(self) -> "CVOptions":
        if self.reference is not None and self.reference.position == "check":
            raise ValueError(
                "reference.position 'check' needs a located box, and a cv criterion's "
                "llm fallback runs without boxes; use an llm criterion with "
                "options.boxes: true"
            )
        return self

    @model_validator(mode="after")
    def _no_loop_options_on_cv(self) -> "CVOptions":
        sent = self.reference.loop_keys_sent() if self.reference is not None else []
        if sent:
            raise ValueError(
                f"{', '.join(sent)} {'tunes' if len(sent) == 1 else 'tune'} the "
                "bounding-box loop, and a cv criterion's llm fallback runs without boxes; "
                "use an llm criterion with options.boxes: true"
            )
        return self

    def resolve(self, name: str) -> dict[str, Any]:
        default = "detector" if detector_client.is_configured() else "llm"
        resolved = {
            "fallback": self.fallback or default,
            "aggregate": self.resolved_aggregate(default_aggregate("cv")),
        }
        return _with_model(_with_reference(resolved, self.reference, name), self.model)


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


def _with_model(resolved: dict[str, Any], model: Optional[str]) -> dict[str, Any]:
    """``resolved`` plus ``model`` — only when the caller sent one, so a
    criterion without it resolves (and echoes) exactly as it always did. The
    model the criterion actually ran on is ``detail.model`` on its result."""
    if model is None:
        return resolved
    return {**resolved, "model": model}


def without_model(resolved: dict[str, Any]) -> dict[str, Any]:
    """Resolved options with ``model`` dropped — a criterion's IDENTITY.

    Which model answers a question does not change the question, so two
    criteria that differ only in ``options.model`` are the same criterion:
    a reference saved under one model guides the criterion under any other
    (``references.resolve``, ``api.references``)."""
    return {k: v for k, v in resolved.items() if k != "model"}


def box_loop_settings(resolved: dict[str, Any]) -> dict[str, Any]:
    """What the box loop runs with for one criterion, from its RESOLVED llm
    options — keyed by the keywords that take them:

        refine_mode, verify_partial, verify_min_span   llm.boxes.locate_criterion
        guide_ask, guide_refine, guide_verify          (reference.boxes)
        reference_text                                 JobReferences.box_examples
                                                       (``with_text``; reference.text)

    ``resolve()`` already put the service default under every key the
    caller left unset. A key missing altogether — ``reference`` is in the
    resolved options only when sent, and a dict resolved elsewhere may lack
    the loop's keys — takes the service default here, from the same
    ``resolve()``, so the two can never disagree. Called by
    ``analysis.llm_eval`` once the loop's gate has passed."""
    defaults = LLMOptions().resolve("")
    reference = {**ReferenceOptions().resolve(""), **(resolved.get("reference") or {})}
    steps = reference["boxes"]
    return {
        "refine_mode": resolved.get("refine", defaults["refine"]),
        "verify_partial": bool(resolved.get("verify_partial", defaults["verify_partial"])),
        "verify_min_span": float(resolved.get("verify_min_span", defaults["verify_min_span"])),
        "guide_ask": bool(steps["ask"]),
        "guide_refine": bool(steps["refine"]),
        "guide_verify": bool(steps["verify"]),
        "reference_text": bool(reference["text"]),
    }


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
            "key": f"optional string, 1-{NAME_MAX_CHARS} characters (stripped): matches "
                   "reference examples only (see `reference.match`), never shown to the "
                   "model; may repeat in a request, unique within one reference",
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
            # The one matching order (references.resolve.match_target).
            "match": "options.reference.criterion (a reference criterion NAME), else the "
                     "criterion's `key` (the reference criteria with that key — a keyed "
                     "criterion never falls back to its name), else its own `name`; "
                     "case-insensitive, usable reference criteria only. The same order for "
                     "explicit ids, the `auto` pool and each item's `auto` pick",
            "caps": {
                "max_per_request": REFERENCE_MAX_PER_REQUEST,
                "max_per_criterion": REFERENCE_MAX_PER_CRITERION,
            },
            # Images per prompt is a property of the MODEL now: GET /models
            # max_images_per_prompt / supports.references.
            "requires_model": "a vision model with max_images_per_prompt >= 2 "
                              "(3 pairs a PASS and a FAIL example in one call) — GET /models",
            "position": {
                "min_iou": REFERENCE_POSITION_MIN_IOU,
                "max_offset": REFERENCE_POSITION_MAX_OFFSET,
                "cap": REFERENCE_POSITION_CAP,
                "requires": "options.boxes: true and hint presence / auto",
            },
        },
        # The bounding-box loop's per-criterion settings (llm criteria with
        # options.boxes: true). Unset = this container's service default.
        "box_loop": {
            "applies_to": "llm criteria with options.boxes: true (refused without it, and on "
                          "cv, whose llm fallback never locates)",
            "options": {
                "refine": "grid | marks — how the refine pass asks",
                "verify_partial": "bool — the verify also says whether the crop cuts the "
                                  "feature off",
                "verify_min_span": "0-1 — grow the verify crop to this share of the page",
                "reference.boxes": "{ask, refine, verify} bools — which calls are shown the "
                                   "criterion's reference examples",
                "reference.text": "bool — each example's caption quotes its region's text",
            },
            "precedence": "the criterion's value, else this container's service default "
                          "(the env var); options_used shows the value that ran",
            "service_defaults": {
                "refine": service_refine_mode(),
                "verify_partial": LLM_BBOX_VERIFY_PARTIAL,
                "verify_min_span": LLM_BBOX_VERIFY_MIN_SPAN,
                "reference.boxes": ReferenceBoxes().resolve(),
                "reference.text": REFERENCE_BOX_TEXT,
            },
            # Service-wide on purpose: a window with more text lines than this
            # (in the marks tier it reached) refines on the grid instead.
            "marks_max": LLM_BBOX_MARKS_MAX,
            "score": "never changes a score or a verdict — the loop runs after scoring",
        },
        # options.model on llm criteria (and a cv criterion's llm fallback);
        # the request-level `model` sets the default for the job.
        "model": {
            "applies_to": "llm criteria, and cv criteria answered by the llm fallback",
            "precedence": "options.model, then the request's `model`, then the registry default",
            "default": llm_models.get_registry().default_id,
            "models": llm_models.get_registry().ids(),
            "catalog": "GET /models",
            "identity": "options.model is not part of a criterion's identity: references "
                        "match across models",
        },
        "detector_configured": detector_client.is_configured(),
    }
