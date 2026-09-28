"""Per-type criterion options: the models, their defaults, and their caps.

A criterion's top level holds only what every type shares — ``name``,
``type``, ``weight``, ``depends_on``, ``score``. Everything that only means
something to one evaluation path lives in ``options``, validated by the model
for that path:

    llm       LLMOptions       hint, boxes, max_attempts, ocr
    text      TextOptions      pattern, match, case_sensitive, fuzzy_threshold,
                               min_count, ocr
    cv        CVOptions        fallback
    detector  DetectorOptions  threshold

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

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from common.documents import MAX_PATTERN_CHARS

from config import (
    DETECTOR_MIN_SCORE,
    LLM_BBOX_MAX_ATTEMPTS,
)
from cv import get_detector
from cv.quality import check_blur, check_exposure
# A module, not names: `detector_client.is_configured()` reads DETECTOR_URL at
# call time, which is what lets a test point it at a stub.
from detector import client as detector_client

OcrMode = Literal["auto", "always", "never"]

# Server caps a caller cannot exceed. MAX_ATTEMPTS is the env knob itself;
# the text caps mirror the matcher's own guards (common.documents.textmatch).
MIN_COUNT_CAP: int = 1000
NAME_MAX_CHARS: int = 200

# The two whole-page OpenCV measurements. They score a property of the page,
# not a thing on it, so they emit no regions — which matters for
# `score: false`, where a criterion with no possible geometry is refused.
_WHOLE_PAGE_DETECTORS = (check_blur, check_exposure)


class _Options(BaseModel):
    """Shared config: no unknown keys, no silent type coercion."""

    model_config = ConfigDict(extra="forbid", strict=True)

    def resolve(self, name: str) -> dict[str, Any]:  # pragma: no cover - overridden
        raise NotImplementedError

    @classmethod
    def caps(cls) -> dict[str, Any]:
        return {}


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
        return {
            "hint": self.hint,
            "boxes": self.boxes,
            "max_attempts": self.max_attempts or LLM_BBOX_MAX_ATTEMPTS,
            "ocr": self.ocr,
        }

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

    def resolve(self, name: str) -> dict[str, Any]:
        return {
            "pattern": self.pattern or name,
            "match": self.match,
            "case_sensitive": self.case_sensitive,
            "fuzzy_threshold": self.fuzzy_threshold,
            "min_count": self.min_count,
            "ocr": self.ocr,
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

    def resolve(self, name: str) -> dict[str, Any]:
        default = "detector" if detector_client.is_configured() else "llm"
        return {"fallback": self.fallback or default}


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
            "threshold": DETECTOR_MIN_SCORE if self.threshold is None else self.threshold
        }


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
        "types": types,
        "detector_configured": detector_client.is_configured(),
    }
