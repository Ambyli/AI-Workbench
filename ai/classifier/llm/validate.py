"""Believing the model's answer only as far as it can be checked.

A vision model returns JSON that is usually right and occasionally not: a key
capitalised differently, a score of 47, a confidence of "high". Both are fixed
here rather than anywhere else, so every consumer of an assessment can assume
1-10 scores, 0-100 confidence, and criterion keys spelled exactly as they were
asked for.

    _normalize_criterion_keys() — remap returned keys onto the requested names
                                  (exact, then case-insensitive, then fuzzy).
    validate_and_clamp()        — clamp scores and confidence, recompute every
                                  verdict FROM its clamped score.

It does NOT compute the weighted overall score — that is
``analysis.weighting.compute_weighted_score``, which runs over the merged
assessment (cv + text + detector + llm), not just this call's half.

A SKIPPED entry is left alone on purpose: it carries ``score: None``, and
clamping would turn it into a middling 5 and drag it back into the weighted
average.

Process flow position: immediately after ``llm.client.call_vllm`` in step 6,
and again over the merged assessment in step 7.
"""

import difflib

from api.schemas import CriterionInput
from logger import logger
from utils import verdict_from_score as _verdict_from_score


def _normalize_criterion_keys(
    per_criterion: dict, criteria: list[CriterionInput]
) -> dict:
    """Remap LLM-returned criterion keys to the exact names that were requested.

    The LLM sometimes returns keys that differ from the requested names:
      - capitalisation: "Image Sharpness" instead of "image sharpness"
      - minor wording: "solar_panels" instead of "has solar panels"

    Resolution order (first match wins):
      1. Exact match — no change needed.
      2. Case-insensitive match — strip and lower both sides.
      3. Fuzzy match via difflib (cutoff 0.6) — catches minor spelling diffs.
      4. No match — keep the key as-is (logged as a warning).

    Args:
        per_criterion: The dict of criterion scores returned by the LLM.
        criteria:      The original list of CriterionInput objects.

    Returns:
        A new dict with keys remapped to the canonical criterion names.
    """
    requested_names = [c.name for c in criteria]
    logger.debug(
        "_normalize_criterion_keys: returned=%s requested=%s",
        list(per_criterion.keys()),
        requested_names,
    )
    normalized: dict = {}

    for returned_key, value in per_criterion.items():
        # 1. Exact match — most common case after prompt improvements
        if returned_key in requested_names:
            normalized[returned_key] = value
            continue

        # 2. Case-insensitive match
        lower = returned_key.lower().strip()
        exact_ci = next(
            (n for n in requested_names if n.lower().strip() == lower), None
        )
        if exact_ci:
            if exact_ci != returned_key:
                logger.debug(
                    "_normalize_criterion_keys: case match '%s' -> '%s'",
                    returned_key,
                    exact_ci,
                )
            normalized[exact_ci] = value
            continue

        # 3. Fuzzy match — handles minor wording differences
        close = difflib.get_close_matches(
            returned_key, requested_names, n=1, cutoff=0.6
        )
        if close:
            logger.debug(
                "_normalize_criterion_keys: fuzzy match '%s' -> '%s'",
                returned_key,
                close[0],
            )
            normalized[close[0]] = value
        else:
            # 4. No match — preserve the original key but warn
            logger.warning(
                "_normalize_criterion_keys: no match for '%s', keeping as-is",
                returned_key,
            )
            normalized[returned_key] = value

    logger.debug(
        "_normalize_criterion_keys: returning keys=%s", list(normalized.keys())
    )
    return normalized


def validate_and_clamp(assessment: dict, criteria: list[CriterionInput]) -> dict:
    """Clamp scores/confidence to valid ranges, normalise criterion keys, and
    recompute per-criterion verdicts from the clamped scores.

    Does NOT compute the weighted overall score — call compute_weighted_score()
    separately when a weighted breakdown is needed (i.e. for combined_assessment).

    Steps:
      1. Clamp raw overall_score to [1, 10]; set preliminary overall_verdict.
      2. Normalise criterion keys via _normalize_criterion_keys().
      3. Clamp per-criterion score to [1, 10] and confidence to [0, 100].
      4. Recompute per-criterion verdict from the clamped score.

    Args:
        assessment: Raw assessment dict from call_vllm() (may have bad values).
        criteria:   Criteria list used for key normalisation.

    Returns:
        Cleaned assessment dict with valid scores and verdicts.
    """
    logger.info(
        "validate_and_clamp: raw overall_score=%s overall_verdict=%s criteria=%s",
        assessment.get("overall_score"),
        assessment.get("overall_verdict"),
        [c.name for c in criteria],
    )

    # Step 1 — clamp raw overall score and set a preliminary verdict
    raw_score = assessment.get("overall_score", 5)
    try:
        overall_score = max(1, min(10, int(raw_score)))
    except (TypeError, ValueError):
        logger.warning(
            "validate_and_clamp: invalid overall_score=%r, defaulting to 5", raw_score
        )
        overall_score = 5
    assessment["overall_score"] = overall_score
    assessment["overall_verdict"] = _verdict_from_score(overall_score)

    # Step 2 — normalise criterion keys
    per_criterion = _normalize_criterion_keys(
        assessment.get("per_criterion_scores", {}), criteria
    )

    # Step 3+4 — clamp per-criterion scores/confidence and recompute verdicts
    for key, val in per_criterion.items():
        if not isinstance(val, dict):
            continue
        if val.get("verdict") == "SKIPPED":
            # Not applicable, not scored: a SKIPPED entry carries score=None on
            # purpose (apply_dependencies, or a cv criterion on a document with
            # no page images). Clamping would turn it into a middling 5 and
            # drag it back into the weighted average.
            continue
        try:
            score = max(1, min(10, int(val.get("score", 5))))
        except (TypeError, ValueError):
            logger.warning(
                "validate_and_clamp: invalid score for '%s', defaulting to 5", key
            )
            score = 5
        try:
            confidence = max(0, min(100, int(val.get("confidence", 50))))
        except (TypeError, ValueError):
            logger.warning(
                "validate_and_clamp: invalid confidence for '%s', defaulting to 50", key
            )
            confidence = 50
        val["score"] = score
        val["confidence"] = confidence
        val["verdict"] = _verdict_from_score(score)

    assessment["per_criterion_scores"] = per_criterion

    logger.info(
        "validate_and_clamp: returning overall_score=%s overall_verdict=%s keys=%s",
        assessment["overall_score"],
        assessment["overall_verdict"],
        list(per_criterion.keys()),
    )
    return assessment
