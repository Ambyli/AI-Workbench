"""POST /observe -- record what really answered a decided request.

Called by the LiteLLM hook after the call, with the same decision_id it sent
to /decide; the two may arrive in either order (no foreign key; the
``decision_outcomes`` view joins them). Contract: ROUTE_POLICY_PLAN § 7.

    202  {"decision_id", "stored": true}; false when this decision_id already
         had an outcome (the first write wins)
    422  the body does not match the contract
    503  the outcome could not be stored
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from .. import metrics
from .schemas import ObserveRequest

router = APIRouter()
logger = logging.getLogger("route_policy")


@router.post("/observe", status_code=202)
async def observe(body: ObserveRequest, request: Request) -> dict:
    service = request.app.state.service
    try:
        stored = await service.record_outcome(body)
    except Exception as exc:  # noqa: BLE001
        metrics.errors_total.labels("outcome_write").inc()
        logger.error("observe: outcome for %s not stored: %r", body.decision_id, exc)
        raise HTTPException(503, "the outcome could not be stored; route-policy-db is unavailable") from None
    return {"decision_id": str(body.decision_id), "stored": stored}
