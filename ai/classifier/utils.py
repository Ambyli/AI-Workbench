"""Shared utility functions that depend only on ``config`` and ``logger``.

Kept minimal so any module in the classifier package — at any layer — can
import from here without risking circular imports (``config`` imports only
the standard library and ``common.net``; ``logger`` only the standard
library).
"""

from config import SCORE_MARGINAL_MIN, SCORE_PASS_MIN
from logger import logger


def verdict_from_score(score: int) -> str:
    """Map a 1-10 score to a PASS / MARGINAL / FAIL verdict string.

    Thresholds (config.py § The score scale and the verdict bands):
    ``SCORE_PASS_MIN`` and above = PASS (7-10), ``SCORE_MARGINAL_MIN`` and
    above = MARGINAL (4-6), below = FAIL (1-3).
    """
    logger.debug("verdict_from_score: score=%s", score)
    verdict = (
        "PASS" if score >= SCORE_PASS_MIN
        else ("MARGINAL" if score >= SCORE_MARGINAL_MIN else "FAIL")
    )
    logger.debug("verdict_from_score: returning %s", verdict)
    return verdict
