"""The weighted overall score, and whether it is complete.

Dependencies are resolved by the scheduler BEFORE evaluation (a dependant of
a criterion that did not pass is never run), so what arrives here is final;
this module only collapses it into one number a caller can audit.

    compute_weighted_score() — sum(score x weight) / total_weight over the
                               criteria that COUNT, plus the breakdown, the
                               verdict, and ``complete``.

What counts: a criterion with ``score: true`` and ``status: "ok"``. Excluded:

  * ``score: false``   — located, not judged;
  * ``status: "skipped"`` — not applicable, or its dependency did not pass;
    excluding it (rather than scoring it 1) is what keeps "not applicable"
    from dragging the document down for the wrong reason;
  * ``status: "error"`` — it has no score to count. Unlike the other two it
    makes the assessment INCOMPLETE: ``complete: false``, ``overall_score``
    and ``overall_verdict`` null, and the partial weighted score of what did
    succeed still shown in ``weighted_score_breakdown`` — a caller must never
    read a verdict that silently ignored a criterion it asked for.

Process flow position: called by ``analysis.pipeline.analyze_document`` after
``analysis.scheduler.run_criteria``.
"""

from __future__ import annotations

from analysis.outcome import Outcome
from api.schemas import CriterionInput
from logger import logger
from utils import verdict_from_score as _verdict_from_score


def compute_weighted_score(
    criteria: list[CriterionInput], outcomes: dict[str, Outcome]
) -> dict:
    """The ``assessment`` block's scoring fields.

    Returns:
        ``{"overall_score", "overall_verdict", "complete",
        "weighted_score_breakdown"}``. The breakdown is None when nothing
        counted at all (every criterion ``score: false``, skipped, or failed).
    """
    counted = [
        (c, outcomes[c.name])
        for c in criteria
        if c.score
        and outcomes[c.name].status == "ok"
        and isinstance(outcomes[c.name].score, (int, float))
    ]
    counted_names = {c.name for c, _ in counted}
    errored = [c.name for c in criteria if c.score and outcomes[c.name].status == "error"]
    complete = not errored

    breakdown = None
    final = None
    if counted:
        total_weight = sum(c.weight for c, _ in counted)
        weighted_sum = sum(o.score * c.weight for c, o in counted)
        unrounded = weighted_sum / total_weight
        final = max(1, min(10, round(unrounded)))
        breakdown = {
            "formula": "sum(score * weight) / total_weight",
            "total_weight": round(total_weight, 4),
            "weighted_sum": round(weighted_sum, 4),
            "unrounded_average": round(unrounded, 4),
            "final_score": final,
            "partial": not complete,
            "excluded": {
                c.name: (
                    "score: false" if not c.score else outcomes[c.name].status
                )
                for c in criteria
                if c.name not in counted_names
            },
            "per_criterion": {
                c.name: {
                    "score": o.score,
                    "weight": c.weight,
                    "contribution": round(o.score * c.weight, 4),
                }
                for c, o in counted
            },
        }

    overall = final if complete else None
    verdict = _verdict_from_score(overall) if overall is not None else None
    logger.info(
        "compute_weighted_score: final=%s complete=%s counted=%d errored=%s",
        final, complete, len(counted), errored,
    )
    return {
        "overall_score": overall,
        "overall_verdict": verdict,
        "complete": complete,
        "weighted_score_breakdown": breakdown,
    }
