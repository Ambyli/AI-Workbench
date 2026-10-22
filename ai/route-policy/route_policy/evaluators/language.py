"""language -- the latest user message's language (the signals sidecar's
``language`` task: fastText ``lid.176``, microseconds, never cut).

Config::

    languages: [es, pt]        # vote when the language is one of these ...
    not_languages: [en]        # ... or is NOT one of these (give exactly one of the two)
    min_confidence: 0.6        # and the detector's confidence is at least this

Codes are the detector's: ISO 639-1 where one exists (``en``, ``es``), else
639-2/3 (``als``, ``yue``). The ``default-signals`` draft observes
``not_languages: [en]`` -- how often a non-English turn reaches each chain,
which is what deciding on a multilingual route needs.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

from pydantic import Field, field_validator, model_validator

from ..signals.model import SignalRequest
from ._signals import feature, lookup
from .base import Evaluator, EvaluatorConfig, FieldSpec, Outcome, OutputSpec, declares

TASK = "language"
CODE_PATTERN = r"^[a-z]{2,3}$"


class LanguageConfig(EvaluatorConfig):
    languages: Optional[List[str]] = Field(None, min_length=1, max_length=200)
    not_languages: Optional[List[str]] = Field(None, min_length=1, max_length=200)
    min_confidence: float = Field(0.6, ge=0.0, le=1.0)

    @field_validator("languages", "not_languages")
    @classmethod
    def _codes(cls, value: Optional[List[str]]) -> Optional[List[str]]:
        for code in value or []:
            if not re.match(CODE_PATTERN, code):
                raise ValueError(f"{code!r} is not a language code (2-3 lower-case letters)")
        if value is not None and len(set(value)) != len(value):
            raise ValueError("a language is listed twice")
        return value

    @model_validator(mode="after")
    def _one_list(self) -> "LanguageConfig":
        if (self.languages is None) == (self.not_languages is None):
            raise ValueError("give exactly one of `languages` or `not_languages`")
        return self


@declares(OutputSpec(
    type="language",
    summary="The latest user message's language (the signals sidecar's `language` task: fastText lid.176).",
    votes="vote.route when the detected language is one of `languages` (or none of `not_languages`) at "
          "confidence >= min_confidence; abstains otherwise, when the latest user message has no text, "
          "and when the language was not detected (no answer, a timeout).",
    fields={
        "language": FieldSpec("The detected language code; null when not detected", "string", nullable=True),
        "confidence": FieldSpec("The detector's confidence, 0-1; null when not detected", "number",
                                nullable=True),
        "status": FieldSpec("The language task's status (evaluators/_signals.py)", "string"),
        "signals_ms": FieldSpec("The signals call's time; null when there was none", "number", nullable=True),
    },
))
class Language(Evaluator):
    Config = LanguageConfig

    def _request(self, ctx) -> Optional[SignalRequest]:
        latest = ctx.user_items[-1] if ctx.user_items else None
        if latest is None or not latest.text:
            return None
        return SignalRequest(TASK, (latest,))

    def signal_tasks(self) -> Dict[str, str]:
        return {TASK: SignalRequest(TASK).key}

    def signal_requests(self, ctx) -> List[SignalRequest]:
        request = self._request(ctx)
        return [request] if request is not None else []

    async def evaluate(self, ctx) -> Outcome:
        request = self._request(ctx)
        detail = {"language": None, "confidence": None, "status": "not_sent", "signals_ms": ctx.signals_ms}
        if request is None:
            return Outcome.abstain("no user text", detail)
        found = lookup(ctx, request)
        detail["status"] = found.status
        if not found.usable:
            return Outcome.abstain(found.why(), detail)
        features = found.task.texts.get(request.texts[0].id)
        language = features.get("language") if isinstance(features, dict) else None
        confidence = feature(found.task, request.texts[0].id, "confidence")
        if not isinstance(language, str) or not language or confidence is None:
            return Outcome.abstain("language not detected", detail)
        detail.update(language=language[:16], confidence=round(confidence, 6))
        if self.config.languages is not None:
            matches = language in self.config.languages
        else:
            matches = language not in self.config.not_languages
        if confidence < self.config.min_confidence:
            return Outcome.abstain(f"{detail['language']} at {confidence:.2f}, below "
                                   f"{self.config.min_confidence:g}", detail)
        if matches:
            return Outcome.vote(f"language {detail['language']} ({confidence:.2f})", detail,
                                confidence=round(confidence, 6))
        return Outcome.abstain(f"language {detail['language']} does not match", detail)
