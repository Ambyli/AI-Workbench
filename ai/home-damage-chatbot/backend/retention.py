"""Data retention (S-M6): purge stored customer data older than RETENTION_DAYS.

What the service persists at runtime (under settings.DATA_DIR):
  * handoff emails   -> email_render._STORE  (+ emails.json)
  * Chat feed        -> chat_notifier._FEED  (+ chat_notifications.json)
  * CRM cases        -> crm_store._STORE      (in memory)
  * uploaded photos  -> DATA_DIR/uploads/*

Without this, every submission (name, contact, address, photos) was kept forever.
Demo seed records are marked `_seed` and exempt. RETENTION_DAYS=0 disables purging
(e.g. when a downstream system of record owns retention).

The duration is a business/legal decision — 30 days is a conservative default.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from .config import settings

logger = logging.getLogger("chatbot.retention")

_INTERVAL_SECONDS = 3600
_started = False


def _older_than(iso: str | None, cutoff: datetime) -> bool:
    if not iso:
        return False
    try:
        ts = datetime.fromisoformat(str(iso))
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts < cutoff


def purge(now: datetime | None = None, scope: str = "all") -> dict:
    """Remove expired records and files. Returns counts (never logs PII).

    scope: "chatbot" (uploads only: the gapped chatbot holds no staff records),
    "account" (emails, chat feed, cases: what the account service owns), or "all".
    """
    days = settings.RETENTION_DAYS
    counts = {"emails": 0, "chat": 0, "cases": 0, "uploads": 0}
    if days <= 0:
        return counts
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    if scope in ("account", "all"):
        counts.update(_purge_records(cutoff))
    if scope in ("chatbot", "all"):
        counts["uploads"] = _purge_uploads(cutoff)
    if any(counts.values()):
        logger.info("Retention purge (>%dd, %s): %s", days, scope, counts)
    return counts


def _purge_records(cutoff: datetime) -> dict:
    from . import chat_notifier, crm_store, email_render

    keep = lambda r: r.get("_seed") or not _older_than(r.get("created_at"), cutoff)  # noqa: E731

    before = len(email_render._STORE)
    email_render._STORE[:] = [r for r in email_render._STORE if keep(r)]
    n_emails = before - len(email_render._STORE)
    if n_emails:
        email_render._persist()

    before = len(chat_notifier._FEED)
    chat_notifier._FEED[:] = [r for r in chat_notifier._FEED if keep(r)]
    n_chat = before - len(chat_notifier._FEED)
    if n_chat:
        chat_notifier._persist()

    stale = [cid for cid, c in crm_store._STORE.items() if not keep(c)]
    for cid in stale:
        crm_store._STORE.pop(cid, None)
    return {"emails": n_emails, "chat": n_chat, "cases": len(stale)}


def _purge_uploads(cutoff: datetime) -> int:
    from . import upload

    n_uploads = 0
    cutoff_ts = cutoff.timestamp()
    for f in upload.UPLOAD_DIR.glob("*"):
        if f.is_file() and f.name != ".gitkeep" and f.stat().st_mtime < cutoff_ts:
            f.unlink(missing_ok=True)
            n_uploads += 1
    return n_uploads


def start_background_purge(scope: str = "all") -> None:
    """Purge now, then hourly, on a daemon thread (single-process service)."""
    global _started
    if _started:
        return
    _started = True

    def _loop() -> None:
        while True:
            try:
                purge(scope=scope)
            except Exception as exc:  # noqa: BLE001 - never kill the thread
                logger.warning("Retention purge failed (%s).", type(exc).__name__)
            time.sleep(_INTERVAL_SECONDS)

    threading.Thread(target=_loop, name="retention-purge", daemon=True).start()
