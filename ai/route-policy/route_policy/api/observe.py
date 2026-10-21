"""POST /observe -- record what really answered a decided request.

Called by LiteLLM's hook after the call (fired, not awaited), with the same
decision_id it sent to /decide. The two may still arrive in either order --
a /decide the hook gave up on (its timeout) can land after the /observe of
the fallback answer -- so there is no foreign key; the ``decision_outcomes``
view joins them. Contract: ROUTE_POLICY_PLAN § 7.

    202  {"decision_id", "stored": true}; false when this decision_id already
         had an outcome (the first write wins); {"stored": null,
         "deferred": true} when the write outlasted
         ROUTE_POLICY_DECISION_WRITE_BUDGET_S and goes on in the background
    422  the body does not match the contract
    503  the outcome could not be stored (no database, the write failed in
         time, or too many writes in flight)
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from .. import metrics
from ..service import OutcomeNotStored
from .schemas import ObserveRequest

router = APIRouter()
logger = logging.getLogger("route_policy")


@router.post("/observe", status_code=202)
async def observe(body: ObserveRequest, request: Request) -> dict:
    service = request.app.state.service
    try:
        stored = await service.record_outcome(body)
    except OutcomeNotStored:  # counted and logged by the service
        raise HTTPException(503, "the outcome could not be stored; route-policy-db is unavailable") from None
    except Exception as exc:  # noqa: BLE001 -- anything unexpected: same answer, counted here
        metrics.errors_total.labels("outcome_write").inc()
        logger.error("observe: outcome for %s not stored: %s", body.decision_id, type(exc).__name__)
        raise HTTPException(503, "the outcome could not be stored; route-policy-db is unavailable") from None
    if stored is None:
        return {"decision_id": str(body.decision_id), "stored": None, "deferred": True}
    return {"decision_id": str(body.decision_id), "stored": stored}
