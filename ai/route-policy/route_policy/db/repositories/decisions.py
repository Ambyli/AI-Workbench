"""decisions: one row per POST /decide, plus its evaluations, idempotently.

``insert`` writes the decision and its evaluations in ONE transaction, and
only if the ``decision_id`` is new (``ON CONFLICT DO NOTHING``): the first
write wins, and a duplicate gets the stored response back
(``get_response``) instead of a second, possibly different, decision.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from . import evaluations as evaluations_repo


@dataclass(frozen=True)
class DecisionRecord:
    decision_id: uuid.UUID
    chat_id: Optional[str]
    message_id: Optional[str]
    litellm_call_id: Optional[str]
    user_id: Optional[str]
    key_alias: Optional[str]
    requested_model: str
    chosen_route: str
    applied: bool
    mode: str
    ruleset_id: str
    ruleset_version: int
    reason: str
    scores: Dict[str, float]
    backend_snapshot: Optional[Dict[str, Any]]
    message_count: int
    tool_count: int
    latency_ms: float
    error: Optional[str]
    response: Dict[str, Any]


async def insert(conn: Any, record: DecisionRecord,
                 evaluations: Sequence[evaluations_repo.EvaluationRecord]) -> bool:
    """True when stored; False when the decision_id already existed (nothing written)."""
    async with conn.transaction():
        inserted = await conn.fetchval(
            "INSERT INTO decisions (decision_id, chat_id, message_id, litellm_call_id, user_id, key_alias, "
            "requested_model, chosen_route, applied, mode, ruleset_id, ruleset_version, reason, scores, "
            "backend_snapshot, message_count, tool_count, latency_ms, error, response) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20) "
            "ON CONFLICT (decision_id) DO NOTHING RETURNING true",
            record.decision_id, record.chat_id, record.message_id, record.litellm_call_id, record.user_id,
            record.key_alias, record.requested_model, record.chosen_route, record.applied, record.mode,
            record.ruleset_id, record.ruleset_version, record.reason, record.scores, record.backend_snapshot,
            record.message_count, record.tool_count, record.latency_ms, record.error, record.response,
        )
        if not inserted:
            return False
        await evaluations_repo.insert_many(conn, record.decision_id, evaluations)
    return True


async def get_response(conn: Any, decision_id: uuid.UUID) -> Optional[Dict[str, Any]]:
    return await conn.fetchval("SELECT response FROM decisions WHERE decision_id = $1", decision_id)


async def list_for_chat(conn: Any, chat_id: str, limit: int) -> List[Dict[str, Any]]:
    """The chat's latest ``limit`` decisions, OLDEST FIRST, each with its
    outcome (null when /observe has not arrived)."""
    rows = await conn.fetch(
        "SELECT d.decision_id, d.created_at, d.message_id, d.litellm_call_id, d.requested_model, "
        "d.chosen_route, d.applied, d.mode, d.ruleset_id, d.ruleset_version, d.reason, d.scores, "
        "d.latency_ms, d.error, d.response -> 'votes' AS votes, "
        "o.decision_id IS NOT NULL AS observed, o.created_at AS observed_at, o.response_id, "
        "o.served_model_group, o.served_deployment, o.overflow_reason, o.finish_reason, o.tool_calls, "
        "o.status, o.error AS outcome_error "
        "FROM decisions d LEFT JOIN outcomes o USING (decision_id) "
        "WHERE d.chat_id = $1 ORDER BY d.created_at DESC, d.decision_id LIMIT $2",
        chat_id, int(limit),
    )
    out = []
    for r in reversed(rows):
        out.append({
            "decision_id": str(r["decision_id"]),
            "created_at": r["created_at"].isoformat(),
            "message_id": r["message_id"],
            "litellm_call_id": r["litellm_call_id"],
            "requested_model": r["requested_model"],
            "route": r["chosen_route"],
            "applied": r["applied"],
            "mode": r["mode"],
            "ruleset": {"id": r["ruleset_id"], "version": r["ruleset_version"]},
            "reason": r["reason"],
            "scores": r["scores"],
            "votes": r["votes"] or [],
            "latency_ms": r["latency_ms"],
            "error": r["error"],
            "outcome": None if not r["observed"] else {
                "observed_at": r["observed_at"].isoformat(),
                "response_id": r["response_id"],
                "served_model_group": r["served_model_group"],
                "served_deployment": r["served_deployment"],
                "overflow_reason": r["overflow_reason"],
                "finish_reason": r["finish_reason"],
                "tool_calls": r["tool_calls"],
                "status": r["status"],
                "error": r["outcome_error"],
                "agree": r["chosen_route"] == r["served_model_group"],
            },
        })
    return out
