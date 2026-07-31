"""
test_phoenix_spy.py
-------------------
Standalone driver that uses cdp_interceptor to open
https://phoenix.zeoenergy.com/projects in an isolated Chrome, then extracts
any network response whose body contains project records shaped like:

    {
        "Entity Email": "...",
        "Entity Phone": "...",
        "Project ID": 27716,
        "Project Name": "...",
        "Project Object Status": "...",
        "Project Object Type": "...",
        "id": 27716,
        "meta": {...}
    }

Because the exact API URL is unknown up front, we use a body-shape match
instead of url_patterns: parse_fn walks the JSON, finds arrays whose items
have "Project ID" and "Project Name" fields, and returns those.

Run from widget/:
    .venv\\Scripts\\python.exe test_phoenix_spy.py

First run opens a visible Chrome — log in when prompted. The session cookies
are cached in `--profile-dir` so subsequent runs skip the login.

Ctrl-C to stop.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common.cdp_interceptor import InterceptorClient
from common.cdp_interceptor.cdp_session import Capture


TARGET_URL = "https://phoenix.zeoenergy.com/projects"
PROFILE_DIR = os.path.expandvars(r"%USERPROFILE%\.phoenix_spy\chrome_profile")

# Fields that identify a project record. A body is considered a "hit" if it
# contains at least one object with all REQUIRED_FIELDS set (any non-None value).
REQUIRED_FIELDS = ("Project ID", "Project Name")


def _looks_like_project(obj) -> bool:
    if not isinstance(obj, dict):
        return False
    return all(field in obj for field in REQUIRED_FIELDS)


def _find_projects(node, acc: list[dict]) -> None:
    """Depth-first walk of a JSON value; collect any dict that looks like a project."""
    if isinstance(node, dict):
        if _looks_like_project(node):
            acc.append(node)
            # Still recurse — nested project objects are possible in wrappers.
        for v in node.values():
            _find_projects(v, acc)
    elif isinstance(node, list):
        for v in node:
            _find_projects(v, acc)


def parse_fn(cap: Capture) -> dict | None:
    """Return {'url': ..., 'projects': [...]} when the response body contains
    at least one project-shaped object, else None."""
    projects: list[dict] = []
    _find_projects(cap.body, projects)
    if not projects:
        return None
    return {"url": cap.url, "count": len(projects), "projects": projects}


# ── Callbacks ────────────────────────────────────────────────────────────────


def on_data(parsed: dict) -> None:
    print(f"\n=== MATCHED {parsed['count']} project(s) from {parsed['url']} ===", flush=True)
    for p in parsed["projects"]:
        pid = p.get("Project ID") or p.get("id")
        name = p.get("Project Name")
        status = p.get("Project Object Status")
        otype = p.get("Project Object Type")
        print(f"  #{pid} — {name}  [{otype} / {status}]", flush=True)
    # Uncomment to dump the first full record:
    # print(json.dumps(parsed["projects"][0], indent=2), flush=True)


def on_capture(cap: Capture) -> None:
    """Fires for every JSON response. Useful for discovering the actual API
    endpoint — non-matching captures still show up here."""
    body_preview = json.dumps(cap.body)[:180]
    print(f"[raw] {cap.url}  →  {body_preview}...", flush=True)


def on_status(status: str, error: str | None) -> None:
    tag = f"[status] {status}"
    if error:
        tag += f"  ({error})"
    print(tag, flush=True)


# ── Main ─────────────────────────────────────────────────────────────────────


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if not InterceptorClient.is_available():
        print("cdp_interceptor deps missing (requests + websocket-client).", file=sys.stderr)
        return 1

    os.makedirs(PROFILE_DIR, exist_ok=True)
    print(f"Profile dir: {PROFILE_DIR}", flush=True)
    print(f"Target:      {TARGET_URL}", flush=True)
    print("Launching Chrome (visible on first run for login)...\n", flush=True)

    client = InterceptorClient(
        profile_dir=PROFILE_DIR,
        # No url_patterns — we filter by body shape via parse_fn.
        parse_fn=parse_fn,
        on_data=on_data,
        on_capture=on_capture,
        on_status=on_status,
        # phoenix login page: adjust if the login flow uses a different keyword.
        login_url_keywords=("login", "signin", "/auth", "sso"),
        session_sentinel=True,  # go headless on subsequent runs
        capture_timeout=60,     # give the SPA time to hydrate + fetch
    )

    client.launch(TARGET_URL)

    stop = threading.Event()
    try:
        while not stop.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping...", flush=True)
    finally:
        client.quit()

    return 0


if __name__ == "__main__":
    sys.exit(main())
