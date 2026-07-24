"""
Roofix Scraper — FastAPI wrapper around common.cdp_interceptor.

Endpoints:
    GET  /health                  healthcheck
    GET  /profile                 current profile-dir status
    POST /profile/refresh         upload a .tgz of a captured Chrome profile
    GET  /proposal/{project_id}   scrape a proposal by Roofix project id
                                  (or ?tracking_url=... for tokenized email links)
"""

from __future__ import annotations

# Load .env before anything that reads os.environ at import time.
from common.env import load_env

load_env()

import json
import os
import sys
import threading
import time
from collections import Counter
from typing import Optional

from fastapi import FastAPI, HTTPException, UploadFile, File
from pydantic import BaseModel

from common.cdp_interceptor import Capture, InterceptorClient, BrowserNotFoundError

import profile as _profile


PROJECT_URL_TEMPLATE = "https://roofix.io/project/{project_id}"
HEADLESS = os.environ.get("ROOFIX_HEADLESS", "true").lower() != "false"
DEBUG_PORT = int(os.environ.get("ROOFIX_DEBUG_PORT", "9223"))
CAPTURE_WINDOW_SECONDS = int(os.environ.get("ROOFIX_CAPTURE_WINDOW_SECONDS", "20"))

_LOGIN_URL_MARKERS = ("/login", "/signin", "sign_in", "signup")


def _log(msg: str) -> None:
    print(f"[scraper] {msg}", file=sys.stderr, flush=True)


def _looks_like_login(url: str) -> bool:
    u = (url or "").lower()
    return any(m in u for m in _LOGIN_URL_MARKERS)


app = FastAPI(title="Roofix Scraper")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/profile")
def profile_status() -> dict:
    return _profile.profile_info()


@app.post("/profile/refresh")
def profile_refresh(archive: UploadFile = File(...)) -> dict:
    """Accept a .tgz of a Playwright/Chrome user-data-dir and persist it."""
    try:
        info = _profile.unpack_profile(archive.file)
        return {"unpacked": True, **info}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"failed to unpack profile: {e}")


@app.get("/proposal/{project_id}")
def proposal(project_id: str, tracking_url: Optional[str] = None) -> dict:
    """One-shot scrape: launch cdp_interceptor at the target URL, collect
    every `elasticsearch/mget` + `/api/1.1/` capture for CAPTURE_WINDOW_SECONDS,
    then stop. Returns the merged docs + diagnostic info."""
    target_url = tracking_url or PROJECT_URL_TEMPLATE.format(project_id=project_id)
    _log(f"start  headless={HEADLESS}  url={target_url}")

    captures: list[Capture] = []
    landed_urls: list[str] = []
    lock = threading.Lock()

    def on_capture(cap: Capture) -> None:
        with lock:
            captures.append(cap)
            _log(f"captured  {cap.url[:110]}")

    def on_status(status: str, error: Optional[str]) -> None:
        _log(f"status  {status}  {error or ''}")
        # We don't try to intercept "waiting_login" here — the login-wall check
        # happens after the capture window closes so we can report it cleanly.

    client = InterceptorClient(
        profile_dir=_profile.PROFILE_DIR,
        debug_port=DEBUG_PORT,
        url_patterns=[r"elasticsearch/mget", r"/api/1\.1/"],
        on_capture=on_capture,
        on_status=on_status,
        session_sentinel=False,   # scraper doesn't auto-recover — operator uploads profiles
        login_timeout=CAPTURE_WINDOW_SECONDS,  # bail fast if we hit the login wall
        capture_timeout=CAPTURE_WINDOW_SECONDS,
    )

    try:
        client.launch(target_url=target_url)
    except BrowserNotFoundError as e:
        raise HTTPException(status_code=500, detail=str(e))

    # Let the client's worker thread capture for the window, then stop.
    time.sleep(CAPTURE_WINDOW_SECONDS)
    state = client.get_state()
    client.quit()

    # Assemble the response.
    docs: list[dict] = []
    doc_types_counter: Counter = Counter()
    captured_urls: list[str] = []
    with lock:
        for cap in captures:
            captured_urls.append(cap.url)
            body = cap.body if isinstance(cap.body, dict) else {}
            if isinstance(body.get("docs"), list):
                for d in body["docs"]:
                    docs.append(d)
                    doc_types_counter[d.get("_type", "?")] += 1

    project_ids = sorted({
        d.get("_id") for d in docs
        if d.get("_type") == "project" and d.get("_id")
    })
    login_wall = state.status == "waiting_login" or (
        state.error is not None and "login" in state.error.lower()
    )
    _log(f"done  status={state.status}  login_wall={login_wall}  captures={len(captures)}  "
         f"docs={len(docs)}  project_ids={project_ids}")

    return {
        "url": target_url,
        "status": state.status,
        "error": state.error,
        "login_wall": login_wall,
        "docs": docs,
        "doc_types": dict(doc_types_counter),
        "response_count": len(captures),
        "captured_urls": captured_urls,
        "project_ids": project_ids,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
