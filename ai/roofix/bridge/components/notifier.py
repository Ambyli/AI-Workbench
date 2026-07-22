"""
NOTIFIER — one-way rep notification (Phase 1 only).

Contract F (Orchestrator -> Notifier):
    notify_rep(rep_contact, message) -> {sent: bool, error: str|None}

One-way only. Off entirely in Phase 0. Wire to a CloudTalk MCP when Phase 1 lands.
"""

from __future__ import annotations


def notify_rep(rep_contact: str, message: str) -> dict:
    """Phase 1 stub. Returns immediately without sending in Phase 0."""
    return {"sent": False, "error": "notify_rep not enabled (Phase 0)"}
