"""Shared utility functions with no intra-package dependencies.

Kept minimal so any module in the classifier package can import from here
without risking circular imports.
"""

from logger import logger


def verdict_from_score(score: int) -> str:
    """Map a 1-10 score to a PASS / MARGINAL / FAIL verdict string.

    Thresholds: 7-10 = PASS, 4-6 = MARGINAL, 1-3 = FAIL.
    """
    logger.debug("verdict_from_score: score=%s", score)
    verdict = "PASS" if score >= 7 else ("MARGINAL" if score >= 4 else "FAIL")
    logger.debug("verdict_from_score: returning %s", verdict)
    return verdict
