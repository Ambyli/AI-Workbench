"""Combiners: votes -> one route. See base.py for the shared frame
(force, veto, choose, default).

    base.py       Ballot, Choice, Combination, Combiner, @registers, scores_of
    priority.py   first non-abstaining vote wins
    weighted.py   best Σ weight × confidence at or above a threshold
"""

from .base import (
    ROLE_FORCE,
    ROLE_VETO,
    ROLE_VOTE,
    ROLES,
    Ballot,
    Choice,
    Combination,
    Combiner,
    CombinerConfig,
    combiner_registry,
    registers,
    scores_of,
)

__all__ = [
    "ROLE_FORCE",
    "ROLE_VETO",
    "ROLE_VOTE",
    "ROLES",
    "Ballot",
    "Choice",
    "Combination",
    "Combiner",
    "CombinerConfig",
    "combiner_registry",
    "registers",
    "scores_of",
]
