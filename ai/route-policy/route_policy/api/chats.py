"""GET /chats/{chat_id}/decisions -- one conversation's decisions and outcomes.

The latest ``limit`` PRIMARY decisions (default 100, at most 1000) for an
Open WebUI chat id (``X-OpenWebUI-Chat-Id``, forwarded by LiteLLM as
``chat_id``), returned OLDEST FIRST, each with its votes, its outcome (null
until /observe arrives) and ``shadows``: the decisions the binding's shadow
rulesets made beside it (same keys but ``shadows``; ``shadow_of`` names the
primary, ``outcome`` is always null). An unknown chat id is an empty list,
not a 404.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Path, Query, Request

from .. import config
from ..db.repositories import decisions as decisions_repo

router = APIRouter()


@router.get("/chats/{chat_id}/decisions")
async def chat_decisions(
    request: Request,
    chat_id: str = Path(min_length=1, max_length=256),
    limit: int = Query(config.CHAT_DECISIONS_DEFAULT_LIMIT, ge=1, le=config.CHAT_DECISIONS_MAX_LIMIT),
) -> dict:
    database = request.app.state.service.database
    if database is None:
        raise HTTPException(503, "no database configured")
    try:
        async with database.acquire() as conn:
            rows = await decisions_repo.list_for_chat(conn, chat_id, limit)
    except Exception:  # noqa: BLE001
        raise HTTPException(503, "route-policy-db is unavailable") from None
    return {"chat_id": chat_id, "decisions": rows}
