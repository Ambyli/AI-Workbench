"""The process-wide objects the routes share, and the two writes.

``Service`` is built by the app's lifespan and hung on ``app.state.service``:

    store         the ruleset cache (rulesets.store.RulesetStore)
    database      route-policy-db (db.pool.Database), or None in DB-less tests
    rulesets_dir  where refs in a POST /evaluate document resolve

``record_decision`` and ``record_outcome`` turn an engine result / an
/observe body into rows, idempotently, and keep the metrics.

**A decision is returned even when it cannot be stored.** Routing must never
depend on the log: a failed write is counted
(``route_policy_errors_total{kind="decision_write"}``) and logged, and the
caller still gets the decision. An outcome has nothing to return, so a failed
/observe write is a 503.
"""

from __future__ import annotations

import logging
import pathlib
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from . import metrics
from .api.schemas import DecideRequest, ObserveRequest
from .db.pool import Database
from .db.repositories import decisions as decisions_repo
from .db.repositories import outcomes as outcomes_repo
from .db.repositories.evaluations import EvaluationRecord
from .engine.pipeline import Decision
from .rulesets.store import RulesetStore

logger = logging.getLogger("route_policy")

STORED, DUPLICATE, NOT_STORED = "stored", "duplicate", "not_stored"


@dataclass
class Service:
    store: RulesetStore
    database: Optional[Database]
    rulesets_dir: pathlib.Path

    async def record_decision(self, body: DecideRequest, decision: Decision,
                              response: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        """Store a decision. Returns (status, the response to send): the
        stored FIRST response on a duplicate decision_id, else ``response``."""
        if self.database is None:
            self._count(decision)
            return NOT_STORED, response
        record = decisions_repo.DecisionRecord(
            decision_id=body.decision_id,
            chat_id=body.chat_id,
            message_id=body.message_id,
            litellm_call_id=body.litellm_call_id,
            user_id=body.user_id,
            key_alias=body.key_alias,
            requested_model=body.requested_model,
            chosen_route=decision.route,
            applied=decision.apply,
            mode=decision.mode,
            ruleset_id=decision.ruleset.id,
            ruleset_version=decision.ruleset.version,
            reason=decision.reason,
            scores=decision.scores,
            backend_snapshot=({k: v.model_dump() for k, v in body.backends.items()}
                              if body.backends is not None else None),
            message_count=len(body.messages),
            tool_count=len(body.tools) if body.tools is not None else 0,
            latency_ms=decision.latency_ms,
            error="; ".join(decision.errors) or None,
            response=response,
        )
        evaluations = [
            EvaluationRecord(i, v.evaluator, v.type, v.role, v.route, v.weight, v.confidence, v.reason,
                             v.abstained, v.latency_ms, v.error, v.detail)
            for i, v in enumerate(decision.votes)
        ]
        try:
            async with self.database.acquire() as conn:
                if await decisions_repo.insert(conn, record, evaluations):
                    self._count(decision)
                    return STORED, response
                stored = await decisions_repo.get_response(conn, body.decision_id)
        except Exception as exc:  # noqa: BLE001 -- the decision is still returned
            metrics.errors_total.labels("decision_write").inc()
            logger.error("decide: decision %s computed but not stored: %r", body.decision_id, exc)
            self._count(decision)
            return NOT_STORED, response
        metrics.duplicates_total.labels("decide").inc()
        return DUPLICATE, stored if isinstance(stored, dict) else response

    async def record_outcome(self, body: ObserveRequest) -> bool:
        """Store an outcome. True when stored, False on a duplicate. Raises
        when there is no database or the write fails (the route answers 503)."""
        if self.database is None:
            raise RuntimeError("no database configured")
        record = outcomes_repo.OutcomeRecord(
            decision_id=body.decision_id,
            litellm_call_id=body.litellm_call_id,
            response_id=body.response_id,
            served_model_group=body.served_model_group,
            served_deployment=body.served_deployment,
            overflow_reason=body.overflow_reason,
            finish_reason=body.finish_reason,
            status=body.status,
            error=body.error,
            tool_calls=list(body.tool_calls),
        )
        async with self.database.acquire() as conn:
            stored = await outcomes_repo.insert(conn, record)
        if stored:
            # Bounded label: a caller's own status string is stored, not labelled.
            label = body.status if body.status in ("success", "failure") else "other"
            metrics.observations_total.labels(label).inc()
        else:
            metrics.duplicates_total.labels("observe").inc()
        return stored

    @staticmethod
    def _count(decision: Decision) -> None:
        metrics.decisions_total.labels(decision.route, decision.mode, str(decision.apply).lower()).inc()
        metrics.decide_seconds.observe(decision.latency_ms / 1000.0)
        for v in decision.votes:
            metrics.evaluator_seconds.labels(v.evaluator, v.type).observe(v.latency_ms / 1000.0)
            outcome = "error" if v.error else ("abstain" if v.abstained else "vote")
            metrics.votes_total.labels(v.evaluator, v.type, outcome).inc()
        for invariant in decision.overrides:
            metrics.invariant_overrides_total.labels(invariant).inc()
