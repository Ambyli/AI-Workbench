"""User Concurrency & Queueing Manager for Chatbot Service.

Limits the number of concurrent active chat sessions to prevent system and LLM
overload. When maximum active capacity is reached, incoming users are placed in a
FIFO queue, receive real-time queue position updates, and are automatically
promoted as active slots open up or idle sessions time out.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Dict, List, Optional, Tuple

from .config import settings

logger = logging.getLogger("chatbot.queue")

_LOCK = threading.Lock()

# Active sessions: {session_id: last_active_timestamp}
_ACTIVE_USERS: Dict[str, float] = {}

# FIFO Queue: [session_id_1, session_id_2, ...]
_WAITING_QUEUE: List[str] = []

# Queue metadata: {session_id: (joined_at, last_ping_timestamp)}
_QUEUE_PINGS: Dict[str, Tuple[float, float]] = {}


def cleanup_stale() -> None:
    """Evict inactive active users and dropped queued sessions, promoting waiting users."""
    now = time.time()
    active_timeout = settings.ACTIVE_SESSION_TIMEOUT_SECONDS
    queue_timeout = settings.QUEUE_POLL_TIMEOUT_SECONDS

    # 1. Clean inactive active users
    expired_active = [
        sid for sid, last_active in _ACTIVE_USERS.items()
        if (now - last_active) > active_timeout
    ]
    for sid in expired_active:
        logger.info("Queue: session %s idle timeout (>%ss) — releasing active slot.", sid, active_timeout)
        _ACTIVE_USERS.pop(sid, None)

    # 2. Clean dropped queue clients (stopped polling)
    dropped_queued = [
        sid for sid, (_, last_ping) in _QUEUE_PINGS.items()
        if (now - last_ping) > queue_timeout
    ]
    for sid in dropped_queued:
        logger.info("Queue: queued session %s stopped pinging — removing from queue.", sid)
        if sid in _WAITING_QUEUE:
            _WAITING_QUEUE.remove(sid)
        _QUEUE_PINGS.pop(sid, None)

    # 3. Promote from queue if slots are available
    _promote_next(now)


def _promote_next(now: Optional[float] = None) -> None:
    """Promote top-of-queue users into available active slots."""
    if now is None:
        now = time.time()
    max_active = settings.MAX_ACTIVE_USERS
    while len(_ACTIVE_USERS) < max_active and _WAITING_QUEUE:
        promoted_sid = _WAITING_QUEUE.pop(0)
        _QUEUE_PINGS.pop(promoted_sid, None)
        _ACTIVE_USERS[promoted_sid] = now
        logger.info("Queue: promoted session %s from queue to active slot (%d/%d).",
                    promoted_sid, len(_ACTIVE_USERS), max_active)


def acquire_or_enqueue(session_id: str) -> Tuple[bool, int, str]:
    """Attempt to acquire an active chat slot or place in queue.

    Returns:
        (allowed: bool, queue_position: int, status_code: str)
        - allowed=True, position=0, status="active"
        - allowed=False, position=N, status="queued"
        - allowed=False, position=-1, status="full"
    """
    with _LOCK:
        cleanup_stale()
        now = time.time()

        # Already active -> update activity timestamp
        if session_id in _ACTIVE_USERS:
            _ACTIVE_USERS[session_id] = now
            return True, 0, "active"

        # Active slot available -> grant immediately
        if len(_ACTIVE_USERS) < settings.MAX_ACTIVE_USERS:
            if session_id in _WAITING_QUEUE:
                _WAITING_QUEUE.remove(session_id)
                _QUEUE_PINGS.pop(session_id, None)
            _ACTIVE_USERS[session_id] = now
            return True, 0, "active"

        # Capacity full -> check or add to queue
        if session_id in _WAITING_QUEUE:
            joined_at, _ = _QUEUE_PINGS.get(session_id, (now, now))
            _QUEUE_PINGS[session_id] = (joined_at, now)
            position = _WAITING_QUEUE.index(session_id) + 1
            return False, position, "queued"

        # New user entering queue
        if len(_WAITING_QUEUE) >= settings.MAX_QUEUE_SIZE:
            logger.warning("Queue: maximum queue capacity (%d) reached — rejecting session %s.",
                           settings.MAX_QUEUE_SIZE, session_id)
            return False, -1, "full"

        _WAITING_QUEUE.append(session_id)
        _QUEUE_PINGS[session_id] = (now, now)
        position = len(_WAITING_QUEUE)
        logger.info("Queue: session %s enqueued at position #%d.", session_id, position)
        return False, position, "queued"


def check_status(session_id: str) -> Tuple[bool, int, str]:
    """Check whether a session is active or its current position in queue."""
    with _LOCK:
        cleanup_stale()
        now = time.time()

        if session_id in _ACTIVE_USERS:
            _ACTIVE_USERS[session_id] = now
            return True, 0, "active"

        if session_id in _WAITING_QUEUE:
            joined_at, _ = _QUEUE_PINGS.get(session_id, (now, now))
            _QUEUE_PINGS[session_id] = (joined_at, now)
            position = _WAITING_QUEUE.index(session_id) + 1
            return False, position, "queued"

        return False, -1, "untracked"


def release_active(session_id: str) -> None:
    """Release an active session slot (e.g. on turn completion or safety exit)."""
    with _LOCK:
        if session_id in _ACTIVE_USERS:
            _ACTIVE_USERS.pop(session_id, None)
            logger.info("Queue: released active slot for session %s.", session_id)
            cleanup_stale()


def leave_queue(session_id: str) -> None:
    """Remove a session from the waiting queue if cancelled."""
    with _LOCK:
        if session_id in _WAITING_QUEUE:
            _WAITING_QUEUE.remove(session_id)
            _QUEUE_PINGS.pop(session_id, None)
            logger.info("Queue: session %s left the queue.", session_id)


def get_stats() -> dict:
    """Return live metrics on active users and queue depth."""
    with _LOCK:
        cleanup_stale()
        return {
            "active_users": len(_ACTIVE_USERS),
            "max_active_users": settings.MAX_ACTIVE_USERS,
            "queued_users": len(_WAITING_QUEUE),
            "max_queue_size": settings.MAX_QUEUE_SIZE,
            "active_timeout_seconds": settings.ACTIVE_SESSION_TIMEOUT_SECONDS,
        }


def reset() -> None:
    """Reset all active users and queue state (for testing)."""
    with _LOCK:
        _ACTIVE_USERS.clear()
        _WAITING_QUEUE.clear()
        _QUEUE_PINGS.clear()
