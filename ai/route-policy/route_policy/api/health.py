"""GET /health and GET /ready.

/health is liveness, and what the compose healthcheck and the edge's
http-check probe: the process is up and finished startup, which itself
required route-policy-db (migrations and ruleset seeding run there). It does
not touch the database, so a database blip does not take every copy out of
the edge at once -- /decide keeps deciding from the cached rulesets.

/ready adds a live ``SELECT 1``: 200 when decisions can be logged right now,
503 otherwise.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/health")
async def health(request: Request) -> dict:
    store = request.app.state.service.store
    return {
        "status": "ok",
        "rulesets": [r.ref() for r in store.compiled],
        "bindings": len(store.bindings),
    }


@router.get("/ready")
async def ready(request: Request) -> JSONResponse:
    database = request.app.state.service.database
    up = database is not None and await database.ping()
    return JSONResponse({"status": "ready" if up else "not ready", "db": "up" if up else "down"},
                        status_code=200 if up else 503)
