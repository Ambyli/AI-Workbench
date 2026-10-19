"""POST /decide -- decide (and log) the chain for one request.

Called by the LiteLLM hook on every ``auto`` request (phase 2: fired and
forgotten; phase 3: awaited under a ~150 ms budget, fail-safe to
``local-private``). Contract: ROUTE_POLICY_PLAN § 7, ROUTE_POLICY.md § API.

    200  the decision (a duplicate decision_id gets the FIRST stored decision)
    404  no ruleset is bound to requested_model (+ key_alias)
    422  the body does not match the contract (decision_id not a UUID, ...)
    503  the bound ruleset cannot be compiled by this copy
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from .. import metrics
from ..engine import pipeline
from .schemas import DecideRequest

router = APIRouter()
logger = logging.getLogger("route_policy")


@router.post("/decide")
async def decide(body: DecideRequest, request: Request) -> dict:
    service = request.app.state.service
    ruleset = await service.store.resolve(body.requested_model, body.key_alias)
    if ruleset is None:
        binding = service.store.binding_for(body.requested_model, body.key_alias)
        if binding is not None:
            raise HTTPException(503, f"the ruleset bound to {body.requested_model!r} "
                                     f"({binding.ruleset_id}@{binding.version}) cannot be loaded by this copy")
        metrics.errors_total.labels("unbound_model").inc()
        raise HTTPException(404, f"no ruleset is bound to model {body.requested_model!r}")
    decision = await pipeline.run(ruleset, body.context())
    status, response = await service.record_decision(body, decision, decision.response(str(body.decision_id)))
    logger.info("[decide] %s %s -> %s (%s%s; %s@%d %s) chat=%s", body.decision_id, body.requested_model,
                response.get("route"), decision.reason, "" if status != "duplicate" else ", duplicate",
                ruleset.id, ruleset.version, ruleset.mode, body.chat_id or "-")
    return response
