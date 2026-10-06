"""Server-issued conversation sessions (contract v2, S-M2 / S-H1 / S-H3).

Before: the browser invented its own session_id, so anyone who learned or guessed an
id could continue someone else's conversation (and re-display their PII), and a
script could mint unlimited ids to squat queue slots.

Now: the server issues an unguessable 128-bit token on the first /api/chat call, and
every route rejects ids it did not issue (HTTP 404 `session_expired`). Each client IP
may hold only a few live sessions, and uploads are bound to the session that made
them so one conversation can never attach another's photos.

State is in-memory (single-process, like the rest of the prototype).
"""
from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass, field

from .config import settings

logger = logging.getLogger("chatbot.sessions")

_LOCK = threading.Lock()


@dataclass
class _Session:
    client: str
    created: float
    last: float
    uploads: set[str] = field(default_factory=set)


_SESSIONS: dict[str, _Session] = {}


class TooManySessions(Exception):
    """This client already holds MAX_SESSIONS_PER_CLIENT live sessions."""


class UploadLimitReached(Exception):
    """This session already uploaded MAX_UPLOADS_PER_SESSION files."""


def _purge_expired(now: float) -> None:
    ttl = settings.SESSION_TTL_SECONDS
    for sid in [s for s, v in _SESSIONS.items() if now - v.last > ttl]:
        _SESSIONS.pop(sid, None)


def short(sid: str | None) -> str:
    """Non-reversible short form for logs (never log a full token)."""
    return (sid or "-")[:6] + "…"


def issue(client: str) -> str:
    """Create a new session for `client`; raises TooManySessions over the per-client cap."""
    with _LOCK:
        now = time.time()
        _purge_expired(now)
        live = sum(1 for v in _SESSIONS.values() if v.client == client)
        if live >= settings.MAX_SESSIONS_PER_CLIENT:
            raise TooManySessions()
        sid = secrets.token_urlsafe(16)  # 128 bits -> 22 url-safe chars
        _SESSIONS[sid] = _Session(client=client, created=now, last=now)
        logger.info("Session issued %s (client has %d live).", short(sid), live + 1)
        return sid


def touch(sid: str | None) -> bool:
    """True (and refresh idle timer) if `sid` was issued and has not expired."""
    if not sid:
        return False
    with _LOCK:
        now = time.time()
        _purge_expired(now)
        s = _SESSIONS.get(sid)
        if s is None:
            return False
        s.last = now
        return True


def end(sid: str | None) -> None:
    """Forget a session (e.g. after its request was submitted) — PII minimisation."""
    if sid:
        with _LOCK:
            _SESSIONS.pop(sid, None)


def register_upload(sid: str, filename: str) -> None:
    with _LOCK:
        s = _SESSIONS.get(sid)
        if s is None:
            raise KeyError(sid)
        if len(s.uploads) >= settings.MAX_UPLOADS_PER_SESSION:
            raise UploadLimitReached()
        s.uploads.add(filename)


def upload_slots_left(sid: str) -> int:
    with _LOCK:
        s = _SESSIONS.get(sid)
        return 0 if s is None else max(0, settings.MAX_UPLOADS_PER_SESSION - len(s.uploads))


def owns_upload(sid: str, filename: str) -> bool:
    with _LOCK:
        s = _SESSIONS.get(sid)
        return s is not None and filename in s.uploads


def reset() -> None:
    """Clear all sessions (tests)."""
    with _LOCK:
        _SESSIONS.clear()
