"""POST /decide -- decide (and log) the chain for one request.

Called by LiteLLM's pre-call hook on every ``auto`` request and AWAITED,
under ROUTE_POLICY_DECIDE_TIMEOUT_S (0.25 s by default): the hook rewrites
the request to ``route`` when ``apply`` is true and its own copy of the
invariants agrees, and sends it to ``local-private`` on anything else (a
timeout, a non-200, ``apply: false``). So this is on the critical path of
every ``auto`` turn: the engine takes well under a millisecond, and no
database wait may hold the answer for long -- the decision write is awaited
for at most ROUTE_POLICY_DECISION_WRITE_BUDGET_S (then finishes in the
background, service.py), the binding refresh and an uncached version fetch
for at most config.DB_READ_BUDGET_S (rulesets/store.py). Contract:
ROUTE_POLICY_PLAN § 7, ROUTE_POLICY.md § API.

The steps: resolve the binding (the primary ruleset; its shadow slots are
only refs here) -> read, concurrently and each in one query within
DB_READ_BUDGET_S, the stored verdicts the primary's evaluators name (none
for a ruleset without an ``llm_classifier``, so no query for ``default``)
and, for a ``pin: turn`` ruleset, the turn's first decision (same chat id +
message id; none without both ids, or for a background task) -> run the
pipeline (which keeps the turn's route, unless the fresh one is
local-private) -> record (within the write budget) -> answer. THEN, in one
background task (``Service.after_decide``): the primary's analysis requests
go to the analysis queue (none from a call that is not its turn's first),
and each shadow ruleset is decided on the same request and stored with
``shadow_of`` -- none of which can change, delay or fail the response.

    200  the decision (a duplicate decision_id gets the FIRST stored decision)
    404  no ruleset is bound to requested_model (+ key_alias)
    422  the body does not match the contract (decision_id not a UUID, ...)
    503  the bound ruleset cannot be loaded by this copy (it cannot compile it,
         or the database did not hand it over within the read budget)
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
    resolved = await service.store.resolve(body.requested_model, body.key_alias)
    if resolved is None:
        binding = service.store.binding_for(body.requested_model, body.key_alias)
        if binding is not None:
            raise HTTPException(503, f"the ruleset bound to {body.requested_model!r} "
                                     f"({binding.ruleset_id}@{binding.version}) cannot be loaded by this "
                                     "copy right now")
        metrics.errors_total.labels("unbound_model").inc()
        raise HTTPException(404, f"no ruleset is bound to model {body.requested_model!r}")
    ruleset = resolved.primary
    ctx = body.context()
    await service.load_context(ruleset, ctx, body.decision_id, shadow=False, budget_s=service.read_budget_s)
    decision = await pipeline.run(ruleset, ctx)
    status, response = await service.record_decision(body, decision, decision.response(str(body.decision_id)))
    logger.info("[decide] %s %s -> %s (%s%s; %s@%d %s) chat=%s%s", body.decision_id, body.requested_model,
                response.get("route"), decision.reason,
                f", {status}" if status in ("duplicate", "deferred", "dropped") else "", ruleset.id, ruleset.version,
                ruleset.mode, body.chat_id or "-", " task" if body.background_task else "")
    service.after_decide(body, decision, resolved, ctx)
    return response
