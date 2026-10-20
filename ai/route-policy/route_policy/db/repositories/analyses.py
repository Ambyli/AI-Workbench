"""analyses: one row per classified conversation prefix (the llm_classifier).

Written by the analysis queue (``analysis.queue``) once a classification has
ended -- ok, incomplete or failed -- and read back, many keys in one query,
when a ruleset's ``llm_classifier`` evaluators name the verdicts they want
(``Service.load_analyses``).

``input_key`` is the primary key (a SHA-256 of the evaluator's fingerprint
and the transcript it classified) and the first write wins
(``ON CONFLICT DO NOTHING``): two copies of the service during a swap, or two
queue workers, can never store two verdicts for one prefix. A failed
analysis is stored too -- its rate is what Trino reads -- and that prefix is
then not retried; the next user message is a new prefix.

**No transcript is stored**, and no message text: the row keeps the key, the
scores, the route, the winning criterion's reason (the model's own words
ABOUT the conversation, capped at 200 characters here and by a CHECK in the
table), the provenance ids and what the classification cost.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence

from ...analysis.model import Verdict

REASON_MAX_CHARS = 200
ERROR_MAX_CHARS = 300


def _cap(text: Optional[str], limit: int) -> Optional[str]:
    if text is None:
        return None
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass(frozen=True)
class AnalysisRecord:
    input_key: str
    fingerprint: str
    model_id: str
    ruleset_id: Optional[str]
    ruleset_version: Optional[int]
    evaluator_id: Optional[str]
    chat_id: Optional[str]
    message_id: Optional[str]
    decision_id: Optional[uuid.UUID]
    exchange_index: int
    input_chars: int
    route: Optional[str]
    confidence: Optional[float]
    scores: Dict[str, Optional[int]]
    reason: Optional[str]
    status: str
    error: Optional[str]
    classifier_job_id: Optional[str]
    calls: Optional[int]
    prompt_tokens: Optional[int]
    completion_tokens: Optional[int]
    model_seconds: Optional[float]
    queue_ms: Optional[float]
    latency_ms: float


async def insert(conn: Any, record: AnalysisRecord) -> bool:
    """True when stored; False when this prefix already had a verdict."""
    inserted = await conn.fetchval(
        "INSERT INTO analyses (input_key, fingerprint, model_id, ruleset_id, ruleset_version, evaluator_id, "
        "chat_id, message_id, decision_id, exchange_index, input_chars, route, confidence, scores, reason, "
        "status, error, classifier_job_id, calls, prompt_tokens, completion_tokens, model_seconds, queue_ms, "
        "latency_ms) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, "
        "$21, $22, $23, $24) ON CONFLICT (input_key) DO NOTHING RETURNING true",
        record.input_key, record.fingerprint, record.model_id, record.ruleset_id, record.ruleset_version,
        record.evaluator_id, record.chat_id, record.message_id, record.decision_id, record.exchange_index,
        record.input_chars, record.route, record.confidence, dict(record.scores),
        _cap(record.reason, REASON_MAX_CHARS), record.status, _cap(record.error, ERROR_MAX_CHARS),
        record.classifier_job_id, record.calls, record.prompt_tokens, record.completion_tokens,
        record.model_seconds, record.queue_ms, record.latency_ms,
    )
    return bool(inserted)


async def exists(conn: Any, input_key: str) -> bool:
    return bool(await conn.fetchval("SELECT true FROM analyses WHERE input_key = $1", input_key))


async def get_many(conn: Any, keys: Sequence[str]) -> Dict[str, Verdict]:
    """The stored verdicts among ``keys`` (one query), by key."""
    if not keys:
        return {}
    rows = await conn.fetch(
        "SELECT input_key, status, route, confidence, scores, exchange_index, model_id "
        "FROM analyses WHERE input_key = ANY($1::text[])", list(keys))
    return {r["input_key"]: Verdict(r["input_key"], r["status"], r["route"], r["confidence"],
                                    dict(r["scores"] or {}), r["exchange_index"], r["model_id"])
            for r in rows}
