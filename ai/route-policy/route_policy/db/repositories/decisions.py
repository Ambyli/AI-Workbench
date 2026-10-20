"""decisions: one row per POST /decide, plus its evaluations, idempotently.

``insert`` writes the decision and its evaluations in ONE transaction, and
only if the ``decision_id`` is new (``ON CONFLICT DO NOTHING``): the first
write wins, and a duplicate gets the stored response back
(``get_response``) instead of a second, possibly different, decision.

**Shadow decisions** (0002) are rows too: a shadow ruleset bound beside the
primary is decided after /decide has answered, under the id
``uuid5(primary_id, "<ruleset>@<version>")`` with ``shadow_of`` = the
primary's id -- so a repeated /decide derives the same ids and writes
nothing twice. ``list_for_chat`` returns the primaries, each carrying its
shadows.

**Turns** (0003): ``first_of_turn`` reads the earliest decision of one
ruleset id for one (chat id, message id) -- among the primaries, or among
that ruleset's own shadow rows -- for a ``pin: turn`` ruleset; a decision
held to it stores its id in ``pinned_from``. ``background_task`` is the
hook's flag for an Open WebUI background task: such a call is never pinned,
and no call is ever pinned to one.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

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
    shadow_of: Optional[uuid.UUID] = None   # the primary's decision_id; None for a primary
    pinned_from: Optional[uuid.UUID] = None  # the turn's first decision; None for it, and without `pin: turn`
    background_task: bool = False


async def insert(conn: Any, record: DecisionRecord,
                 evaluations: Sequence[evaluations_repo.EvaluationRecord]) -> bool:
    """True when stored; False when the decision_id already existed (nothing written)."""
    async with conn.transaction():
        inserted = await conn.fetchval(
            "INSERT INTO decisions (decision_id, chat_id, message_id, litellm_call_id, user_id, key_alias, "
            "requested_model, chosen_route, applied, mode, ruleset_id, ruleset_version, reason, scores, "
            "backend_snapshot, message_count, tool_count, latency_ms, error, response, shadow_of, pinned_from, "
            "background_task) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, "
            "$21, $22, $23) ON CONFLICT (decision_id) DO NOTHING RETURNING true",
            record.decision_id, record.chat_id, record.message_id, record.litellm_call_id, record.user_id,
            record.key_alias, record.requested_model, record.chosen_route, record.applied, record.mode,
            record.ruleset_id, record.ruleset_version, record.reason, record.scores, record.backend_snapshot,
            record.message_count, record.tool_count, record.latency_ms, record.error, record.response,
            record.shadow_of, record.pinned_from, record.background_task,
        )
        if not inserted:
            return False
        await evaluations_repo.insert_many(conn, record.decision_id, evaluations)
    return True


async def get_response(conn: Any, decision_id: uuid.UUID) -> Optional[Dict[str, Any]]:
    return await conn.fetchval("SELECT response FROM decisions WHERE decision_id = $1", decision_id)


async def first_of_turn(conn: Any, *, chat_id: str, message_id: str, ruleset_id: str, shadow: bool,
                        exclude: uuid.UUID) -> Optional[Tuple[uuid.UUID, str]]:
    """(decision_id, chosen_route) of the EARLIEST stored decision of
    ``ruleset_id`` (any version) for this (chat id, message id) -- among the
    primaries (``shadow`` false) or among the ruleset's own shadow rows
    (``shadow`` true) -- leaving out ``exclude`` (the decision being made, so
    a repeated /decide never finds itself) and background tasks. None when
    there is none: the caller is the turn's first. One query, served by
    ``decisions_turn_idx``."""
    row = await conn.fetchrow(
        "SELECT decision_id, chosen_route FROM decisions "
        "WHERE chat_id = $1 AND message_id = $2 AND ruleset_id = $3 "
        f"AND shadow_of IS {'NOT NULL' if shadow else 'NULL'} "
        "AND decision_id <> $4 AND NOT background_task "
        "ORDER BY created_at, decision_id LIMIT 1",
        chat_id, message_id, ruleset_id, exclude,
    )
    return (row["decision_id"], row["chosen_route"]) if row is not None else None


_ROW_COLUMNS = (
    "d.decision_id, d.created_at, d.message_id, d.litellm_call_id, d.requested_model, d.shadow_of, "
    "d.pinned_from, d.background_task, "
    "d.chosen_route, d.applied, d.mode, d.ruleset_id, d.ruleset_version, d.reason, d.scores, "
    "d.latency_ms, d.error, d.response -> 'votes' AS votes, "
    "o.decision_id IS NOT NULL AS observed, o.created_at AS observed_at, o.response_id, "
    "o.served_model_group, o.served_deployment, o.overflow_reason, o.finish_reason, o.tool_calls, "
    "o.status, o.error AS outcome_error "
)


def _row(r: Any) -> Dict[str, Any]:
    """One decision as GET /chats/{chat_id}/decisions returns it."""
    return {
        "decision_id": str(r["decision_id"]),
        "created_at": r["created_at"].isoformat(),
        "message_id": r["message_id"],
        "litellm_call_id": r["litellm_call_id"],
        "requested_model": r["requested_model"],
        "shadow_of": str(r["shadow_of"]) if r["shadow_of"] is not None else None,
        "pinned_from": str(r["pinned_from"]) if r["pinned_from"] is not None else None,
        "background_task": r["background_task"],
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
    }


async def list_for_chat(conn: Any, chat_id: str, limit: int) -> List[Dict[str, Any]]:
    """The chat's latest ``limit`` PRIMARY decisions, OLDEST FIRST, each with
    its outcome (null when /observe has not arrived) and ``shadows``: the
    shadow decisions made beside it, same keys (their ``outcome`` is always
    null -- the turn's outcome is the primary's), oldest ruleset first."""
    rows = await conn.fetch(
        f"SELECT {_ROW_COLUMNS}"
        "FROM decisions d LEFT JOIN outcomes o USING (decision_id) "
        "WHERE d.chat_id = $1 AND d.shadow_of IS NULL "
        "ORDER BY d.created_at DESC, d.decision_id LIMIT $2",
        chat_id, int(limit),
    )
    primaries = [_row(r) for r in reversed(rows)]
    shadows: Dict[str, List[Dict[str, Any]]] = {p["decision_id"]: [] for p in primaries}
    if rows:
        for r in await conn.fetch(
            f"SELECT {_ROW_COLUMNS}"
            "FROM decisions d LEFT JOIN outcomes o USING (decision_id) "
            "WHERE d.shadow_of = ANY($1::uuid[]) "
            "ORDER BY d.ruleset_id, d.ruleset_version, d.created_at",
            [r["decision_id"] for r in rows],
        ):
            shadows[str(r["shadow_of"])].append(_row(r))
    for p in primaries:
        p["shadows"] = shadows[p["decision_id"]]
    return primaries
