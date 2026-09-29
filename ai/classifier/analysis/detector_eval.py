"""The `detector` evaluator: the open-vocabulary detector as a scoring path.

``detector.client`` is the transport — one HTTP call, boxes back. This module
is what a criterion does with it: ask about ONE label on the page image, and
score the criterion from the boxes alone, so a `has bicycle` criterion costs
no tokens and comes back with geometry.

    evaluate()             — the shared evaluator interface; the threshold is
                             the criterion's ``options.threshold``.
    evaluate_label()       — the same for a bare label, which is how a `cv`
                             criterion's "detector" fallback reaches it.
    _score_from_detector() — boxes → a scored Outcome, no LLM call.

**A failure fails the criterion, not the job.** An unreachable detector (or
one that answers garbage) raises ``EvaluationError`` with the client's
caller-facing sentence; the scheduler turns that into ``status: "error"`` for
this criterion alone. There is no silent fall-through to the LLM any more — a
caller who asked for the detector is told it did not answer.

Process flow position: one of the four evaluators ``analysis.scheduler``
dispatches to; also called by ``analysis.cv_eval``.
"""

from __future__ import annotations

from common.vision import Region

from analysis.context import DocumentContext
from analysis.outcome import EvaluationError, Outcome, skipped
from analysis.result_specs import (
    AGGREGATE_FIELD,
    METRIC_FIELD,
    VALUE_FIELD,
    FieldSpec,
    ResultSpec,
    result_spec,
    with_metric,
)
from api.schemas import CriterionInput
from config import DETAIL_FLOAT_DECIMALS, DETECTOR_STRONG_SCORE
# Imported as a module, not by name: `detector_client.is_configured()` reads
# DETECTOR_URL at call time, which is what lets a test point it at a stub.
from detector import client as detector_client
from logger import logger
from utils import verdict_from_score as _verdict_from_score


# The detail a `detector` result carries — declared here, beside
# `_score_from_detector`, and registered by type (analysis.result_specs).
# Checked against real output by unit-tests/classifier/test_result_specs.py.
DETECTOR_RESULT = ResultSpec(
    type="detector",
    metric="best_score",
    metric_from="detail",
    fields={
        "metric": METRIC_FIELD,
        "value": VALUE_FIELD,
        "detector_matches": FieldSpec("Boxes found at or above min_score", "integer"),
        "best_score": FieldSpec("The best box's confidence, 0-1; 0 when none", "number"),
        "min_score": FieldSpec("The confidence floor used", "number", stable=True),
        "aggregate": AGGREGATE_FIELD,
    },
)


@result_spec(DETECTOR_RESULT)
async def evaluate(c: CriterionInput, ctx: DocumentContext) -> Outcome:
    """Score ``c`` from the detector's boxes at its resolved threshold."""
    return await evaluate_label(c.name, ctx, c.resolved_options()["threshold"])


@result_spec(DETECTOR_RESULT)
async def evaluate_label(name: str, ctx: DocumentContext, threshold: float) -> Outcome:
    """Ask the detector about ``name`` on the page; score from the boxes.

    Raises:
        EvaluationError: DETECTOR_URL is unset, or the service could not
            answer. The message is the one the caller sees.
    """
    if not ctx.has_image:
        return skipped(
            f"Skipped - document has no page image ({ctx.doc.kind} documents are "
            "text-only, so the detector has nothing to look at).",
            method="detector",
        )
    if not detector_client.is_configured():
        raise EvaluationError(
            "the open-vocabulary detector is not configured on this container "
            "(DETECTOR_URL is empty)"
        )
    try:
        found = await detector_client.detect_page(
            ctx.working_image,
            [name],
            ctx.geometry,
            min_score=threshold,
            stats=ctx.detector_stats,
        )
    except detector_client.DetectorUnavailable as exc:
        logger.warning("detector_eval: '%s' — %s", name, exc)
        ctx.detector_stats.errors.append(str(exc))
        raise EvaluationError(str(exc)) from exc
    return _score_from_detector(name, found.get(name, []), threshold)


def _score_from_detector(name: str, regions: list[Region], threshold: float) -> Outcome:
    """Score one criterion from the detector's boxes alone — no LLM call.

    The rubric mirrors the rest of the service. A box at or above
    ``DETECTOR_STRONG_SCORE`` is a clear PASS (10); a box above the floor but
    below it is a real finding that should not be built on (7 — PASS, same
    meaning as everywhere else); nothing at all is a FAIL (1).

    **No LLM second opinion on a negative.** A criterion the detector looked
    for and did not find is FAILed here. If you want the model's opinion, ask
    for it: give the criterion ``type: "llm"``.
    """
    if not regions:
        return Outcome(
            method="detector",
            score=1,
            verdict="FAIL",
            # The floor is what we asked for, so "nothing above it" is a
            # statement about the floor as much as about the document.
            confidence=int(round((1.0 - threshold) * 100)),
            reason=(
                f"The open-vocabulary detector found no '{name}' at or above the "
                f"{threshold:.2f} confidence floor. Lower options.threshold, or "
                'give the criterion type "llm" to have the vision model judge it.'
            ),
            # The same keys as a hit, best_score 0 — "looked, found nothing".
            detail=with_metric(
                {"detector_matches": 0, "best_score": 0.0, "min_score": threshold},
                "detector", 0.0,
            ),
        )

    best = max(float(r.score or 0.0) for r in regions)
    score = 10 if best >= DETECTOR_STRONG_SCORE else 7
    return Outcome(
        method="detector",
        score=score,
        verdict=_verdict_from_score(score),
        confidence=int(round(min(1.0, best) * 100)),
        reason=(
            f"The open-vocabulary detector found {len(regions)} '{name}' box(es); "
            f"best confidence {best:.2f} (>={DETECTOR_STRONG_SCORE:.2f} scores 10, "
            f"above the {threshold:.2f} floor scores 7)."
        ),
        detail=with_metric(
            {
                "detector_matches": len(regions),
                "best_score": round(best, DETAIL_FLOAT_DECIMALS),
                "min_score": threshold,
            },
            "detector", round(best, DETAIL_FLOAT_DECIMALS),
        ),
        regions=list(regions),
    )
