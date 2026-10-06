"""Account service — the gapped side of the account check.

This is the ONLY component that can reach the CRM (phoenix credentials, the SQL,
the matching gates) or see a match's details. It also finalizes the request:
it appends the account result to the end of the handoff and delivers it (email,
team chat, CRM case). The customer-facing chatbot never holds any of that.

The chatbot talks to it through two calls (backend/account_gateway.py):

    POST /v1/check     {account_name, address}  -> {matched: bool, ref: str}
    POST /v1/dispatch  {ref, case}               -> {matched: bool, email_id: str}

`ref` is an opaque random token. The match details stay in this process, keyed by
the ref, and are appended to the handoff only at dispatch. The chatbot learns one
bit (found / not found), which it needs to tell the customer.

Run as its own process with its own env file (CRM and mail secrets; no model key):

    set ZEO_ENV_FILE=.env.account
    python -m uvicorn backend.account_service:app --host 127.0.0.1 --port 8100

Every call needs the X-Internal-Key header (ACCOUNT_SERVICE_KEY). Bind it to
localhost or a private network only; it is never exposed to the internet.
"""
from __future__ import annotations

import hmac
import logging
import secrets
import threading
import time
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from . import account_match, pipeline, retention
from .config import settings

logging.basicConfig(level=logging.INFO)  # same as the chatbot; delivery/audit lines are INFO
logger = logging.getLogger("chatbot.account_service")

_MAX_TEXT = 300
_MAX_REFS = 10_000


# ---------------------------------------------------------------------------
# Core (also used in-process by dev/tests through account_gateway)
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_REFS: dict[str, dict] = {}  # ref -> {"outcome": MatchOutcome.to_private(), "at": monotonic}


def _purge_refs(now: float) -> None:
    ttl = settings.ACCOUNT_REF_TTL_SECONDS
    for ref in [r for r, v in _REFS.items() if now - v["at"] > ttl]:
        _REFS.pop(ref, None)
    if len(_REFS) > _MAX_REFS:  # memory guard: drop the oldest
        for ref, _ in sorted(_REFS.items(), key=lambda kv: kv[1]["at"])[: len(_REFS) - _MAX_REFS]:
            _REFS.pop(ref, None)


def check(account_name: str, address: str) -> dict:
    """Run the match; keep the details here; return one bit plus an opaque ref."""
    outcome = account_match.run(account_name, address)
    ref = secrets.token_urlsafe(18)
    now = time.monotonic()
    with _lock:
        _purge_refs(now)
        _REFS[ref] = {"outcome": outcome.to_private(), "at": now}
    return {"matched": outcome.matched, "ref": ref}


def dispatch(ref: Optional[str], case: dict) -> dict:
    """Append the account result to the end of the handoff and deliver it.

    The ref is single-use. If it is unknown or expired (e.g. a service restart),
    the match is re-run here from the customer's typed answers, so a request is
    never lost or misrouted because of the gap.
    """
    with _lock:
        entry = _REFS.pop(ref, None) if ref else None
    if entry is not None:
        private = entry["outcome"]
    else:
        raw = case.get("_acct_raw") or {}
        private = account_match.run(raw.get("account_name") or case.get("account_name"),
                                    raw.get("account_address") or case.get("account_address")).to_private()
    case = dict(case)
    case["_acct"] = private
    result = pipeline.dispatch(case)
    return {"matched": bool(result.matched), "email_id": result.email_id}


# ---------------------------------------------------------------------------
# HTTP app
# ---------------------------------------------------------------------------
class CheckRequest(BaseModel):
    account_name: str = Field("", max_length=_MAX_TEXT)
    address: str = Field("", max_length=_MAX_TEXT)


class DispatchRequest(BaseModel):
    ref: Optional[str] = Field(None, max_length=64)
    case: dict


def _require_key(x_internal_key: str = Header("")) -> None:
    expected = settings.ACCOUNT_SERVICE_KEY
    if not expected or not hmac.compare_digest(x_internal_key.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="unauthorized")


app = FastAPI(title="Zeo account service (internal)", docs_url=None, redoc_url=None, openapi_url=None)


@app.on_event("startup")
def _startup() -> None:
    if not settings.ACCOUNT_SERVICE_KEY:
        raise RuntimeError("ACCOUNT_SERVICE_KEY must be set for the account service.")
    retention.start_background_purge(scope="account")
    logger.info("Account service ready (CRM backend: %s).", settings.CRM_BACKEND)


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.post("/v1/check", dependencies=[Depends(_require_key)])
def http_check(req: CheckRequest) -> dict:
    return check(req.account_name, req.address)


@app.post("/v1/dispatch", dependencies=[Depends(_require_key)])
def http_dispatch(req: DispatchRequest) -> dict:
    return dispatch(req.ref, req.case)
