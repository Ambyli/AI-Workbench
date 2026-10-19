"""evaluations: one row per evaluator per decision.

Written only inside ``decisions.insert``'s transaction, so a decision and its
votes are stored together or not at all.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Optional


@dataclass(frozen=True)
class EvaluationRecord:
    position: int
    evaluator_id: str
    type: str
    role: str
    vote_route: Optional[str]
    weight: float
    confidence: Optional[float]
    reason: str
    abstained: bool
    latency_ms: float
    error: Optional[str] = None
    detail: Dict[str, Any] = field(default_factory=dict)


async def insert_many(conn: Any, decision_id: uuid.UUID, rows: Iterable[EvaluationRecord]) -> None:
    await conn.executemany(
        "INSERT INTO evaluations (decision_id, position, evaluator_id, type, role, vote_route, weight, "
        "confidence, reason, abstained, latency_ms, error, detail) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)",
        [(decision_id, r.position, r.evaluator_id, r.type, r.role, r.vote_route, r.weight, r.confidence,
          r.reason, r.abstained, r.latency_ms, r.error, r.detail) for r in rows],
    )
