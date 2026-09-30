"""The one shape every evaluator (and every aggregate) returns.

Four evaluation paths (``cv_eval``, ``text_eval``, ``llm_eval``,
``detector_eval``) share one interface::

    async def evaluate(c: CriterionInput, ctx: DocumentContext) -> Outcome

and one return type, defined here, so the scheduler, the weighting and the
result assembly never branch on which path produced a result.

    Outcome          — status, method, the judgement (score / verdict /
                       confidence), the explanation (reason / detail), the
                       geometry (regions in ORIGINAL page pixels) and, for
                       ``llm``, the enforcement loop's record.
    EvaluationError  — raised by an evaluator for a failure that has a
                       caller-facing explanation (the detector is
                       unreachable, say). The scheduler catches EVERY
                       exception, this one included, and turns it into
                       ``status: "error"`` for that criterion alone.
    skipped()        — the not-evaluated shape.

Process flow position: the bottom of the evaluation layer; imported by every
evaluator and by ``analysis.scheduler``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from common.vision import Region


class EvaluationError(RuntimeError):
    """A criterion could not be evaluated; the message says why."""


@dataclass
class Outcome:
    """One criterion's result, before it is turned into the response shape.

    Attributes:
        status:       "ok" | "error" | "skipped".
        method:       The path that actually answered — "cv", "text", "llm",
                      "detector" — which for a ``cv`` criterion with no
                      OpenCV detector is its fallback's. None when skipped.
        score / verdict / confidence: The judgement; None when not scored.
        reason:       One human sentence (or two).
        detail:       Structured diagnostics (match counts, the measurement,
                      what text the model was sent).
        regions:      Where, in ORIGINAL page pixels.
        localization: The LLM enforcement loop's record; None off ``llm``.
        text_layer:   ``{"key", "source", "chars"}`` of the text layer this
                      criterion searched (``text``) or sent in its prompt
                      (``llm``) — the key names the ``text.<key>.json``
                      artifact holding it. None when no text was used.
        error:        Why, when ``status == "error"``.
        complete:     False on an AGGREGATE (``analysis.aggregate``) when any
                      unit under it errored — the answer then ignores part of
                      what was asked. Always True on a single unit.
    """

    status: str = "ok"
    method: Optional[str] = None
    score: Optional[int] = None
    verdict: Optional[str] = None
    confidence: Optional[int] = None
    reason: Optional[str] = None
    detail: Any = None
    regions: list[Region] = field(default_factory=list)
    localization: Optional[dict] = None
    text_layer: Optional[dict] = None
    error: Optional[str] = None
    complete: bool = True


def skipped(reason: str, method: Optional[str] = None) -> Outcome:
    """A criterion that was not evaluated — excluded from the weighting."""
    return Outcome(status="skipped", method=method, reason=reason)


def empty_localization() -> dict:
    """The loop's record when it did not run — the same shape, no attempts."""
    return {"attempts": [], "accepted_attempt": None, "calls": 0}
