"""
Session cookies for the Roofix scraper.

Roofix (a Bubble.io app) has no public API, so we drive a real browser via
Playwright. Sessions are saved as Playwright storage-state JSON so subsequent
proposal fetches skip the login wall.

Refresh strategy: the operator runs save_roofix_session.py locally against a
visible browser, then POSTs the resulting JSON to /session/refresh here. The
container itself cannot present a login UI, so refresh is an operator action,
not an autonomous flow.
"""

from __future__ import annotations

import json
import os
from typing import Optional


SESSION_PATH = os.getenv("ROOFIX_SESSION_PATH", "/data/roofix_session.json")


def session_exists() -> bool:
    return os.path.exists(SESSION_PATH) and os.path.getsize(SESSION_PATH) > 0


def load() -> Optional[dict]:
    if not session_exists():
        return None
    try:
        with open(SESSION_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return None


def save(state: dict) -> None:
    os.makedirs(os.path.dirname(SESSION_PATH), exist_ok=True)
    with open(SESSION_PATH, "w") as f:
        json.dump(state, f)


def info() -> dict:
    return {
        "path": SESSION_PATH,
        "present": session_exists(),
        "size_bytes": os.path.getsize(SESSION_PATH) if session_exists() else 0,
    }
