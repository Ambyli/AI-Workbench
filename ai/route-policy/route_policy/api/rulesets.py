"""Read-only ruleset and binding endpoints (the write API is phase 5).

    GET /rulesets                  every stored version, without documents
    GET /rulesets/{id}             the highest version with its document, plus every version's summary
    GET /rulesets/{id}/{version}   one version with its document
    GET /bindings                  requested model (+ key alias) -> ruleset version

All read route-policy-db (503 when it is unreachable), so ``created_at`` /
``active_from`` / ``active_to`` are the stored values.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Path, Request

from ..db.repositories import rulesets as rulesets_repo

router = APIRouter()


def _jsonable(row: Dict[str, Any]) -> Dict[str, Any]:
    out = {}
    for key, value in row.items():
        out[key] = value.isoformat() if isinstance(value, (dt.datetime, dt.date)) else value
    return out


def _summary(row: Dict[str, Any]) -> Dict[str, Any]:
    out = _jsonable({k: v for k, v in row.items() if k != "document"})
    out["sha256"] = out.pop("content_sha256", None)
    return out


def _database(request: Request):
    database = request.app.state.service.database
    if database is None:
        raise HTTPException(503, "no database configured")
    return database


@router.get("/rulesets")
async def list_rulesets(request: Request) -> dict:
    database = _database(request)
    try:
        async with database.acquire() as conn:
            rows = await rulesets_repo.list_rulesets(conn)
    except Exception:  # noqa: BLE001
        raise HTTPException(503, "route-policy-db is unavailable") from None
    return {"rulesets": [_summary(r) for r in rows]}


@router.get("/rulesets/{ruleset_id}")
async def get_ruleset(request: Request, ruleset_id: str = Path(max_length=64)) -> dict:
    database = _database(request)
    try:
        async with database.acquire() as conn:
            latest = await rulesets_repo.get(conn, ruleset_id)
            versions = await rulesets_repo.versions_of(conn, ruleset_id) if latest else []
    except Exception:  # noqa: BLE001
        raise HTTPException(503, "route-policy-db is unavailable") from None
    if latest is None:
        raise HTTPException(404, f"no ruleset {ruleset_id!r}")
    return {**_summary(latest), "document": latest["document"],
            "versions": [_summary(v) for v in versions]}


@router.get("/rulesets/{ruleset_id}/{version}")
async def get_ruleset_version(request: Request, ruleset_id: str = Path(max_length=64),
                              version: int = Path(ge=1)) -> dict:
    database = _database(request)
    try:
        async with database.acquire() as conn:
            row = await rulesets_repo.get(conn, ruleset_id, version)
    except Exception:  # noqa: BLE001
        raise HTTPException(503, "route-policy-db is unavailable") from None
    if row is None:
        raise HTTPException(404, f"no ruleset {ruleset_id}@{version}")
    return {**_summary(row), "document": row["document"]}


@router.get("/bindings")
async def list_bindings(request: Request) -> dict:
    database = _database(request)
    try:
        async with database.acquire() as conn:
            rows = await rulesets_repo.list_bindings(conn)
    except Exception:  # noqa: BLE001
        raise HTTPException(503, "route-policy-db is unavailable") from None
    return {"bindings": [_jsonable(r) for r in rows]}
