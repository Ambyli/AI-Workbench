"""outcomes: one row per POST /observe -- what really answered.

No foreign key to decisions (a /decide the hook gave up on can land after
the /observe of the fallback answer, or never); the ``decision_outcomes``
view joins them. Idempotent on ``decision_id``: the first write wins.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, List, Optional


@dataclass(frozen=True)
class OutcomeRecord:
    decision_id: uuid.UUID
    litellm_call_id: Optional[str]
    response_id: Optional[str]
    served_model_group: Optional[str]
    served_deployment: Optional[str]
    overflow_reason: Optional[str]
    finish_reason: Optional[str]
    status: str
    error: Optional[str]
    tool_calls: List[str] = field(default_factory=list)


async def insert(conn: Any, record: OutcomeRecord) -> bool:
    """True when stored; False when this decision_id already had an outcome."""
    inserted = await conn.fetchval(
        "INSERT INTO outcomes (decision_id, litellm_call_id, response_id, served_model_group, "
        "served_deployment, overflow_reason, finish_reason, tool_calls, status, error) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) "
        "ON CONFLICT (decision_id) DO NOTHING RETURNING true",
        record.decision_id, record.litellm_call_id, record.response_id, record.served_model_group,
        record.served_deployment, record.overflow_reason, record.finish_reason, list(record.tool_calls),
        record.status, record.error,
    )
    return bool(inserted)
