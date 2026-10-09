"""Comprehensive test suite for user concurrency limiting and FIFO queue management.

Tests:
  1. Active user capacity limits (MAX_ACTIVE_USERS).
  2. FIFO queueing & real-time position updates.
  3. Automatic queue promotion upon active session completion (done=True).
  4. Automatic queue promotion upon idle timeout eviction.
  5. Maximum queue capacity ceiling (MAX_QUEUE_SIZE -> status="full").
  6. API endpoints (/api/queue/status, /api/queue/stats, /api/chat).
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Exercises staff/demo routes (queue stats/simulate, inbox, lookup), which are off
# by default (S-C1). Opt in explicitly before importing the app.
os.environ.setdefault("ENABLE_STAFF_API", "true")

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from fastapi.testclient import TestClient
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _session_client import SessionClient

from backend import email_render, queue_manager, seed
from backend.main import app

email_render._STORE.clear()
seed.seed_if_empty()
email_render._persist()

c = SessionClient(app)  # contract v2: server-issued sessions
c.__enter__()


def test_capacity_and_fifo_queueing():
    print("Testing active user capacity and FIFO queueing...")
    queue_manager.reset()

    with patch.dict(os.environ, {"MAX_ACTIVE_USERS": "2", "MAX_QUEUE_SIZE": "5"}):
        # User 1 & 2 acquire active slots immediately
        allow1, pos1, stat1 = queue_manager.acquire_or_enqueue("user_1")
        assert allow1 is True and pos1 == 0 and stat1 == "active"

        allow2, pos2, stat2 = queue_manager.acquire_or_enqueue("user_2")
        assert allow2 is True and pos2 == 0 and stat2 == "active"

        # User 3 enters queue -> Position 1
        allow3, pos3, stat3 = queue_manager.acquire_or_enqueue("user_3")
        assert allow3 is False and pos3 == 1 and stat3 == "queued"

        # User 4 enters queue -> Position 2
        allow4, pos4, stat4 = queue_manager.acquire_or_enqueue("user_4")
        assert allow4 is False and pos4 == 2 and stat4 == "queued"

        stats = queue_manager.get_stats()
        assert stats["active_users"] == 2
        assert stats["queued_users"] == 2

    print("  ✓ Active capacity enforcement and FIFO queue placement verified")


def test_automatic_promotion_on_release():
    print("Testing automatic promotion on slot release...")
    queue_manager.reset()

    with patch.dict(os.environ, {"MAX_ACTIVE_USERS": "2", "MAX_QUEUE_SIZE": "5"}):
        queue_manager.acquire_or_enqueue("user_A")
        queue_manager.acquire_or_enqueue("user_B")
        queue_manager.acquire_or_enqueue("user_C")  # queued #1
        queue_manager.acquire_or_enqueue("user_D")  # queued #2

        # User A finishes / releases slot
        queue_manager.release_active("user_A")

        # User C must now be automatically promoted to active
        allow_c, pos_c, stat_c = queue_manager.check_status("user_C")
        assert allow_c is True and pos_c == 0 and stat_c == "active"

        # User D must now advance to Position #1 in queue
        allow_d, pos_d, stat_d = queue_manager.check_status("user_D")
        assert allow_d is False and pos_d == 1 and stat_d == "queued"

    print("  ✓ Queue promotion and position advancement on release verified")


def test_idle_timeout_eviction():
    print("Testing idle session timeout eviction...")
    queue_manager.reset()

    with patch.dict(os.environ, {
        "MAX_ACTIVE_USERS": "1",
        "ACTIVE_SESSION_TIMEOUT_SECONDS": "2",
    }):
        queue_manager.acquire_or_enqueue("active_idle_user")
        queue_manager.acquire_or_enqueue("waiting_user")

        # Wait for idle timeout
        time.sleep(2.2)

        # Triggers cleanup and promotion
        allow_w, pos_w, stat_w = queue_manager.check_status("waiting_user")
        assert allow_w is True and stat_w == "active", "Waiting user must be promoted after idle session expires"

        allow_i, _, stat_i = queue_manager.check_status("active_idle_user")
        assert allow_i is False and stat_i == "untracked"

    print("  ✓ Stale idle active sessions evicted and queued users promoted")


def test_heartbeat_keeps_open_chat_alive():
    """T-06: the page's 45 s heartbeat (POST /api/queue/status) keeps an open chat's slot;
    a closed tab (no heartbeat) loses it after the idle timeout and the next customer moves up."""
    print("Testing active-slot heartbeat...")
    queue_manager.reset()
    with patch.dict(os.environ, {"MAX_ACTIVE_USERS": "2", "ACTIVE_SESSION_TIMEOUT_SECONDS": "2"}):
        queue_manager.acquire_or_enqueue("open_tab")
        queue_manager.acquire_or_enqueue("closed_tab")
        queue_manager.acquire_or_enqueue("waiting")
        for _ in range(3):                      # open tab heartbeats; closed tab is silent
            time.sleep(0.9)
            queue_manager.check_status("open_tab")
        assert queue_manager.check_status("open_tab")[2] == "active", "heartbeat must keep the slot"
        assert queue_manager.check_status("closed_tab")[2] == "untracked", "silent tab must be evicted"
        assert queue_manager.check_status("waiting")[2] == "active", "next customer moves up"
    print("  ✓ Open chats keep their slot; closed tabs free it")


def test_max_queue_ceiling():
    print("Testing max queue capacity ceiling...")
    queue_manager.reset()

    with patch.dict(os.environ, {"MAX_ACTIVE_USERS": "1", "MAX_QUEUE_SIZE": "2"}):
        queue_manager.acquire_or_enqueue("u1")  # active
        queue_manager.acquire_or_enqueue("u2")  # queue #1
        queue_manager.acquire_or_enqueue("u3")  # queue #2

        # u4 exceeds max queue size of 2
        allow4, pos4, stat4 = queue_manager.acquire_or_enqueue("u4")
        assert allow4 is False and pos4 == -1 and stat4 == "full"

    print("  ✓ Max queue size limit strictly enforced")


def test_api_queue_integration():
    print("Testing API queue endpoints and chat flow integration...")
    queue_manager.reset()

    with patch.dict(os.environ, {"MAX_ACTIVE_USERS": "1", "MAX_QUEUE_SIZE": "5"}):
        # 1. First user starts chat -> active
        r1 = c.post("/api/chat", json={"session_id": "api_user_1", "message": ""})
        assert r1.status_code == 200
        d1 = r1.json()
        assert d1["queued"] is False
        assert d1["state"] == "collect"

        # 2. Second user attempts chat -> queued
        r2 = c.post("/api/chat", json={"session_id": "api_user_2", "message": ""})
        assert r2.status_code == 200
        d2 = r2.json()
        assert d2["queued"] is True
        assert d2["state"] == "queued"
        assert d2["queue_position"] == 1
        assert "in line" in d2["messages"][0]["text"].lower()

        # 3. Check queue status endpoint (contract v2: POST, read-only)
        st = c.post("/api/queue/status", json={"session_id": "api_user_2"}).json()
        assert st["state"] == "queued", st
        assert st["queue_position"] == 1

        # 4. Check queue stats endpoint
        stats = c.get("/api/queue/stats").json()
        assert stats["active_users"] == 1
        assert stats["queued_users"] == 1

        # 5. First user finishes conversation -> releases slot
        # Simulate user 1 flow completion or release
        queue_manager.release_active(c.real_sid("api_user_1"))

        # 6. Now user 2 checks status -> promoted to active
        st2 = c.post("/api/queue/status", json={"session_id": "api_user_2"}).json()
        assert st2["state"] == "active", st2

        # 7. User 2 can now chat normally
        r2_active = c.post("/api/chat", json={"session_id": "api_user_2", "message": ""})
        assert r2_active.json()["queued"] is False
        assert r2_active.json()["state"] == "collect"

    print("  ✓ Full API queue lifecycle and automatic admittance verified")


if __name__ == "__main__":
    try:
        test_capacity_and_fifo_queueing()
        test_automatic_promotion_on_release()
        test_idle_timeout_eviction()
        test_heartbeat_keeps_open_chat_alive()
        test_max_queue_ceiling()
        test_api_queue_integration()
        print("\nALL QUEUE & CONCURRENCY CHECKS PASSED ✓")
    except AssertionError as e:
        print(f"\nQUEUE CONCURRENCY TEST FAILED: {e}")
        sys.exit(1)
