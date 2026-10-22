"""GET /evaluator-types -- what a ruleset can contain.

``evaluator_types``: every registered evaluator type with its ``config``
JSON schema and its DECLARED output (``summary``, when it ``votes``, what one
exchange's ``relevance`` means -- null when the type cannot be a cooldown
judge -- and every ``detail`` key with its kind). The declarations live next
to each evaluator (``@declares``) and are tested against the real evaluators.

``combiner_types``: priority and weighted, with their option schemas.
``roles``: vote | force | veto (combiners/base.py).
"""

from __future__ import annotations

from fastapi import APIRouter

from ..combiners.base import ROLES, combiner_registry
from ..evaluators.base import registry

router = APIRouter()


@router.get("/evaluator-types")
async def evaluator_types() -> dict:
    return {
        "evaluator_types": {name: cls.describe() for name, cls in sorted(registry().items())},
        "combiner_types": {name: cls.describe() for name, cls in sorted(combiner_registry().items())},
        "roles": list(ROLES),
    }
