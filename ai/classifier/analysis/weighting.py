"""Dependency resolution and the weighted overall score.

The last two things that happen to a merged assessment, in this order:
``apply_dependencies`` marks a criterion SKIPPED when the criterion it
``depends_on`` did not PASS, and ``compute_weighted_score`` then collapses
what is left into one score — so a skipped criterion is excluded from the
weighting entirely rather than dragging the document down for the wrong
reason.

    apply_dependencies()     — resolve depends_on chains, mark SKIPPED.
    compute_weighted_score() — sum(score x weight) / total_weight, plus the
                               breakdown a caller can audit.
    _skipped_result()        — the SKIPPED shape, reused by the pipeline for a
                               criterion that cannot be evaluated at all.

Process flow position: step 7 of ``analysis.pipeline.analyze_document``, after
``llm.validate.validate_and_clamp`` and before the result is assembled.
"""

from api.schemas import CriterionInput
from logger import logger
from utils import verdict_from_score as _verdict_from_score


def apply_dependencies(assessment: dict, criteria: list[CriterionInput]) -> dict:
    """Mark dependent criteria SKIPPED if their dependency did not PASS.

    Called after validate_and_clamp() but before compute_weighted_score() so
    that skipped criteria are excluded from the weighted calculation.

    A criterion is skipped when its depends_on target has any verdict other
    than PASS — including FAIL, MARGINAL, or SKIPPED (propagating chains).
    Skipped criteria receive verdict="SKIPPED", score=None, and contribute
    zero weight to the overall score.

    Multiple passes are run until no further changes occur, which correctly
    resolves dependency chains of arbitrary depth (A → B → C).

    Args:
        assessment: Clamped assessment dict containing per_criterion_scores.
        criteria:   Full criteria list — only entries with depends_on are checked.

    Returns:
        The same assessment dict with any dependent criteria marked SKIPPED.
    """
    per_criterion = assessment.get("per_criterion_scores", {})

    # Quick exit if no criteria have dependencies
    if not any(c.depends_on for c in criteria):
        return assessment

    # Map criterion name → depends_on name for fast lookup
    dependency_map = {c.name: c.depends_on for c in criteria if c.depends_on}

    # Multi-pass to resolve chains: keep iterating until nothing changes
    changed = True
    while changed:
        changed = False
        for criterion_name, depends_on_name in dependency_map.items():
            if criterion_name not in per_criterion:
                continue

            current = per_criterion[criterion_name]

            # Already skipped — nothing to do
            if isinstance(current, dict) and current.get("verdict") == "SKIPPED":
                continue

            # Check the dependency's verdict
            dep_result = per_criterion.get(depends_on_name, {})
            dep_verdict = dep_result.get("verdict", "FAIL") if isinstance(dep_result, dict) else "FAIL"

            if dep_verdict != "PASS":
                logger.info(
                    "apply_dependencies: skipping '%s' — dependency '%s' verdict=%s",
                    criterion_name, depends_on_name, dep_verdict,
                )
                per_criterion[criterion_name] = {
                    "verdict":    "SKIPPED",
                    "score":      None,
                    "confidence": None,
                    "reason":     (
                        f"Skipped - dependency '{depends_on_name}' "
                        f"did not pass (verdict: {dep_verdict})."
                    ),
                    "method":     "skipped",
                }
                changed = True

    assessment["per_criterion_scores"] = per_criterion
    return assessment


def compute_weighted_score(assessment: dict, criteria: list[CriterionInput]) -> dict:
    """Compute the weighted overall score and attach the breakdown to the assessment.

    Called only on combined_assessment after validate_and_clamp() has already
    clamped all per-criterion scores.  Overwrites overall_score and
    overall_verdict with the weighted result and adds weighted_score_breakdown.

    Args:
        assessment: Clamped assessment dict containing per_criterion_scores.
        criteria:   Full criteria list providing each criterion's weight.

    Returns:
        The same assessment dict with overall_score, overall_verdict, and
        weighted_score_breakdown updated in place.
    """
    per_criterion = assessment.get("per_criterion_scores", {})
    matched = [
        (c, per_criterion[c.name])
        for c in criteria
        if c.name in per_criterion
        and isinstance(per_criterion[c.name], dict)
        and per_criterion[c.name].get("verdict") != "SKIPPED"
    ]

    if not matched:
        logger.warning("compute_weighted_score: no matched criteria — skipping")
        return assessment

    total_weight = sum(c.weight for c, _ in matched)
    if total_weight == 0:
        logger.warning("compute_weighted_score: total_weight is 0 — skipping")
        return assessment

    weighted_sum = sum(val["score"] * c.weight for c, val in matched)
    unrounded = weighted_sum / total_weight
    weighted_score = max(1, min(10, round(unrounded)))

    assessment["overall_score"] = weighted_score
    assessment["overall_verdict"] = _verdict_from_score(weighted_score)
    assessment["weighted_score_breakdown"] = {
        "formula": "sum(score * weight) / total_weight",
        "total_weight": round(total_weight, 4),
        "weighted_sum": round(weighted_sum, 4),
        "unrounded_average": round(unrounded, 4),
        "final_score": weighted_score,
        "per_criterion": {
            c.name: {
                "score": val["score"],
                "weight": c.weight,
                "contribution": round(val["score"] * c.weight, 4),
            }
            for c, val in matched
        },
    }
    logger.info(
        "compute_weighted_score: final_score=%s weights=%s",
        weighted_score,
        {c.name: c.weight for c, _ in matched},
    )
    return assessment


def _skipped_result(reason: str) -> dict:
    """The SKIPPED shape used by apply_dependencies, reused for criteria that
    cannot be evaluated at all (e.g. a cv criterion on a .docx).

    SKIPPED criteria carry no score and are excluded from the weighted average
    entirely — which is the right answer for "not applicable", as opposed to
    FAIL, which would drag the document's score down for the wrong reason.
    """
    return {
        "verdict":    "SKIPPED",
        "score":      None,
        "confidence": None,
        "reason":     reason,
        "method":     "skipped",
    }
