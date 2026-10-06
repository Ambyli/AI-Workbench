"""Account gateway — the chatbot's ONLY way to the account check.

The chatbot never touches the CRM. It asks this gateway two things and gets back
only what it needs:

    check(account_name, address) -> AccountCheck(matched, ref)
    dispatch(ref, case)          -> Dispatched(matched, email_id)

`matched` is the single bit the customer is told (found / not found). `ref` is an
opaque token the chatbot hands back at submit so the account service can append
the match details to the end of the handoff. The chatbot never sees the project,
the candidates, the scores, the SQL or the database credentials.

Modes (ACCOUNT_GATEWAY):
  service    the gapped deployment: HTTP to the separate account service
             (backend/account_service.py). Used by the mock and production.
  inprocess  dev, tests and the internal demo: calls the same core functions in
             this process. Not gapped; refused when REQUIRE_ACCOUNT_GAP=true.

This module must not import the CRM, matching, SQL or delivery code at module
level (tests/account_gap_test.py checks the chatbot's import graph).
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Optional

import httpx

from .config import settings

logger = logging.getLogger("chatbot.account_gateway")

# The only two things the customer is ever told about the account check.
FOUND_MESSAGE = "We found your account. Please continue with the rest of the form."
NOT_FOUND_MESSAGE = (
    "We were unable to locate your account. Please continue filling out the form, and "
    "someone from our team will locate your account before reaching out."
)


class AccountServiceError(RuntimeError):
    pass


@dataclass(frozen=True)
class AccountCheck:
    matched: bool
    ref: Optional[str]

    def customer_message(self) -> str:
        return FOUND_MESSAGE if self.matched else NOT_FOUND_MESSAGE


@dataclass(frozen=True)
class Dispatched:
    matched: bool
    email_id: Optional[str]
    queued: bool = False  # True when held in the outbox for retry


# Test hook: an httpx.Client-compatible object to use instead of a real connection.
_client_override = None


def _client():
    if _client_override is not None:
        return _client_override
    return httpx.Client(base_url=settings.ACCOUNT_SERVICE_URL,
                        timeout=settings.ACCOUNT_SERVICE_TIMEOUT_SECONDS)


def _post(path: str, body: dict) -> dict:
    client = _client()
    try:
        r = client.post(path, json=body, headers={"X-Internal-Key": settings.ACCOUNT_SERVICE_KEY})
    finally:
        if client is not _client_override:
            client.close()
    if r.status_code != 200:
        raise AccountServiceError(f"account service HTTP {r.status_code}")
    return r.json()


def _mode() -> str:
    return settings.ACCOUNT_GATEWAY


def check(account_name: Optional[str], address: Optional[str]) -> AccountCheck:
    """Never raises: if the service is unreachable the customer is told 'not found'
    and staff locate the account (the same safe path as a real miss)."""
    name, addr = (account_name or "")[:300], (address or "")[:300]
    try:
        if _mode() == "service":
            data = _post("/v1/check", {"account_name": name, "address": addr})
        else:
            from . import account_service  # noqa: PLC0415 - in-process mode only
            data = account_service.check(name, addr)
        ref = data.get("ref")
        return AccountCheck(matched=data.get("matched") is True,
                            ref=ref if isinstance(ref, str) and ref else None)
    except Exception as exc:  # noqa: BLE001 - the chat must continue
        logger.error("Account check unavailable (%s); telling the customer 'not found'.",
                     type(exc).__name__)
        return AccountCheck(matched=False, ref=None)


def _case_for_dispatch(case: dict) -> dict:
    """Customer answers plus the bookkeeping the handoff needs. Session internals
    (phase, retries, timestamps) stay in the chatbot."""
    keep_private = {"_acct_raw", "_unconfirmed", "_incomplete", "_unclear", "_escalation",
                    "_safety_shown", "_alerted"}
    return {k: v for k, v in case.items()
            if (not k.startswith("_") or k in keep_private) and k != "session_id"}


def dispatch(ref: Optional[str], case: dict, matched_hint: bool) -> Dispatched:
    """Hand the finished request to the account service, which appends the account
    result and delivers it. On failure the request goes to the outbox and is retried,
    so a customer's request is never lost."""
    body = {"ref": ref, "case": _case_for_dispatch(case)}
    try:
        if _mode() == "service":
            data = _post("/v1/dispatch", body)
        else:
            from . import account_service  # noqa: PLC0415 - in-process mode only
            data = account_service.dispatch(ref, json.loads(json.dumps(body["case"])))
        return Dispatched(matched=data.get("matched") is True, email_id=data.get("email_id"))
    except Exception as exc:  # noqa: BLE001
        logger.error("Dispatch failed (%s); holding the request in the outbox.", type(exc).__name__)
        _outbox_put(body)
        return Dispatched(matched=matched_hint, email_id=None, queued=True)


# ---------------------------------------------------------------------------
# Outbox: requests the account service could not take yet, retried in background.
# ---------------------------------------------------------------------------
_outbox_lock = threading.Lock()


def _outbox_dir():
    d = settings.DATA_DIR / "outbox"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _outbox_put(body: dict) -> None:
    path = _outbox_dir() / f"{int(time.time())}-{uuid.uuid4().hex[:8]}.json"
    with _outbox_lock:
        path.write_text(json.dumps(body), encoding="utf-8")


def flush_outbox() -> int:
    """Retry held requests. Returns how many were delivered."""
    delivered = 0
    with _outbox_lock:
        for path in sorted(_outbox_dir().glob("*.json")):
            try:
                body = json.loads(path.read_text(encoding="utf-8"))
                if _mode() == "service":
                    _post("/v1/dispatch", body)
                else:
                    from . import account_service  # noqa: PLC0415
                    account_service.dispatch(body.get("ref"), body["case"])
                path.unlink()
                delivered += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("Outbox retry failed (%s); will try again.", type(exc).__name__)
                break
    if delivered:
        logger.info("Outbox delivered %d held request(s).", delivered)
    return delivered


def pending_outbox() -> int:
    return len(list(_outbox_dir().glob("*.json")))


def start_outbox_retry(interval: float = 60.0) -> None:
    def loop():
        while True:
            time.sleep(interval)
            try:
                flush_outbox()
            except Exception:  # noqa: BLE001
                pass
    threading.Thread(target=loop, name="account-outbox", daemon=True).start()


def gap_violations() -> list[str]:
    """Why this chatbot process would NOT be gapped (empty list = gapped)."""
    problems = []
    if settings.ACCOUNT_GATEWAY != "service":
        problems.append("ACCOUNT_GATEWAY is not 'service'")
    if not settings.ACCOUNT_SERVICE_KEY:
        problems.append("ACCOUNT_SERVICE_KEY is not set")
    if settings.ENABLE_STAFF_API:
        problems.append("ENABLE_STAFF_API is on (staff data belongs to the account service)")
    for key in ("CRM_DB_URL", "CRM_API_KEY", "GOOGLE_SA_CREDENTIALS_FILE",
                "CHAT_WEBHOOK_DISPOSITION", "CHAT_WEBHOOK_UNVERIFIED", "CHAT_WEBHOOK_SOLAR"):
        if getattr(settings, key, ""):
            problems.append(f"{key} is set in the chatbot process")
    return problems
