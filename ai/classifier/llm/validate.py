"""Believing the model's answer only as far as it can be checked.

A vision model returns JSON that is usually right and occasionally not: a
score of 47, a confidence of "high", the answer wrapped in the multi-criterion
shape an older prompt asked for. All of it is fixed here, so every consumer of
an ``llm`` result can assume a 1-10 score, a 0-100 confidence, and a verdict
that agrees with the score.

    find_answer()  — the one criterion's answer object inside whatever the
                     model returned: the flat ``{"score": …}`` the prompt asks
                     for, or — tolerated — ``{"assessment":
                     {"per_criterion_scores": {name: {…}}}}``, matched by
                     name (exact, case-insensitive, fuzzy, or the only entry).
                     ``ValueError`` when there is none, which ``call_vllm``
                     treats as a parse failure and retries.
    clamp_answer() — clamp score and confidence, recompute the verdict FROM
                     the clamped score, keep the reason as a string.

The weighted overall score is not computed here — that is
``analysis.weighting.compute_weighted_score``, over every criterion.

Process flow position: passed to ``llm.client.call_vllm`` as its
``validator`` by ``analysis.llm_eval``.
"""

import difflib
from typing import Any

from logger import logger
from utils import verdict_from_score as _verdict_from_score


def find_answer(raw: Any, name: str) -> dict:
    """The answer object for criterion ``name`` inside the model's JSON.

    Raises:
        ValueError: No object carrying a ``score`` could be found.
    """
    if not isinstance(raw, dict):
        raise ValueError(f"expected a JSON object, got {type(raw).__name__}")
    if "score" in raw:
        return raw

    nested = raw.get("assessment", raw)
    per = nested.get("per_criterion_scores") if isinstance(nested, dict) else None
    if isinstance(per, dict) and per:
        entries = {k: v for k, v in per.items() if isinstance(v, dict)}
        if name in entries:
            return entries[name]
        lowered = {k.lower().strip(): k for k in entries}
        if name.lower().strip() in lowered:
            return entries[lowered[name.lower().strip()]]
        close = difflib.get_close_matches(name, list(entries), n=1, cutoff=0.6)
        if close:
            logger.debug("find_answer: fuzzy key '%s' -> '%s'", close[0], name)
            return entries[close[0]]
        if len(entries) == 1:
            return next(iter(entries.values()))
    raise ValueError(f"no 'score' for {name!r} in the answer: {str(raw)[:200]}")


def clamp_answer(entry: dict) -> dict:
    """Clamp one answer to valid ranges and derive the verdict from the score.

    A missing or non-numeric score becomes 5 and a missing confidence 50 —
    logged, because either means the model half-answered.
    """
    try:
        score = max(1, min(10, int(round(float(entry.get("score", 5))))))
    except (TypeError, ValueError):
        logger.warning("clamp_answer: invalid score %r, defaulting to 5", entry.get("score"))
        score = 5
    try:
        confidence = max(0, min(100, int(round(float(entry.get("confidence", 50))))))
    except (TypeError, ValueError):
        logger.warning(
            "clamp_answer: invalid confidence %r, defaulting to 50", entry.get("confidence")
        )
        confidence = 50
    reason = entry.get("reason")
    return {
        "score": score,
        "verdict": _verdict_from_score(score),
        "confidence": confidence,
        "reason": str(reason) if reason is not None else "",
    }


def answer_validator(name: str):
    """A ``call_vllm`` validator that extracts and clamps ``name``'s answer."""

    def _validate(raw: Any) -> dict:
        return clamp_answer(find_answer(raw, name))

    return _validate
