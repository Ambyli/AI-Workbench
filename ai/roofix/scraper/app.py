"""
Roofix Scraper — FastAPI wrapper around common.cdp_interceptor.

Endpoints:
    GET  /health                  healthcheck
    GET  /profile                 current profile-dir status
    POST /profile/refresh         upload a .tgz of a captured Chrome profile
    GET  /proposal/{project_id}   scrape a proposal by Roofix project id
                                  (or ?tracking_url=... for tokenized email links)

Data model: Roofix is a Bubble.io app that hydrates each page in ONE XHR to
``/api/1.1/init/data?location=...``. The response body is a large JSON blob
containing project + customer + estimates + everything else the page needs.
This scraper's job is to load the target URL, wait for that specific request
to fly by, and hand the body back. Everything else observed on the wire is
discarded (still logged in ``captured_urls`` for diagnostics).
"""

from __future__ import annotations

# Load .env before anything that reads os.environ at import time.
from common.env import load_env

load_env()

import os
import re
import sys
import threading
import time
from typing import Optional

from fastapi import FastAPI, HTTPException, UploadFile, File

from common.cdp_interceptor import Capture, InterceptorClient, BrowserNotFoundError

import profile as _profile


PROJECT_URL_TEMPLATE = "https://roofix.io/project/{project_id}"
HEADLESS = os.environ.get("ROOFIX_HEADLESS", "true").lower() != "false"
DEBUG_PORT = int(os.environ.get("ROOFIX_DEBUG_PORT", "9223"))
CAPTURE_WINDOW_SECONDS = int(os.environ.get("ROOFIX_CAPTURE_WINDOW_SECONDS", "20"))

# The one endpoint that carries the page's full data blob. Override via env
# if Bubble ever changes the shape.
INIT_DATA_URL_PATTERN = os.environ.get(
    "ROOFIX_INIT_DATA_URL_PATTERN",
    r"roofix\.io/api/1\.1/init/data",
)
_INIT_DATA_RE = re.compile(INIT_DATA_URL_PATTERN)

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
def proposal(
    project_id: str,
    tracking_url: Optional[str] = None,
    keep_open: bool = False,
) -> dict:
    """One-shot scrape: launch cdp_interceptor at the target URL, watch for
    the ``init/data`` XHR, and return its response body.

    ``tracking_url`` overrides the built ``roofix.io/project/{id}`` URL when
    the caller has a tokenized email link (which redirects without login).

    ``keep_open=true`` skips the ``client.quit()`` call at the end, leaving
    the Chromium window (and its DevTools) open for interactive debugging.
    Kill it manually when done. Subsequent ``/proposal`` calls will fail
    until you do, because the debug port stays held.
    """
    target_url = tracking_url or PROJECT_URL_TEMPLATE.format(project_id=project_id)
    _log(f"start  headless={HEADLESS}  keep_open={keep_open}  url={target_url}")

    init_data_bodies: list[dict] = []
    captured_urls: list[str] = []   # every JSON XHR/fetch URL — diagnostic
    lock = threading.Lock()

    def on_capture(cap: Capture) -> None:
        """Runs for EVERY JSON XHR/fetch — regardless of URL. We use this for
        the "did the endpoint we expected actually fire?" diagnostic list, and
        also to catch our target endpoint here (since on_capture receives the
        URL, whereas on_data only receives the body)."""
        with lock:
            captured_urls.append(cap.url)
            if _INIT_DATA_RE.search(cap.url) and isinstance(cap.body, dict):
                init_data_bodies.append(cap.body)
                _log(f"init/data captured  {cap.url[:110]}  "
                     f"({len(cap.body)} top-level keys)")
            else:
                _log(f"seen (discarded)  {cap.url[:110]}")

    def on_status(status: str, error: Optional[str]) -> None:
        _log(f"status  {status}  {error or ''}")

    client = InterceptorClient(
        profile_dir=_profile.PROFILE_DIR,
        debug_port=DEBUG_PORT,
        # We do the URL-filtering ourselves in on_capture so we keep every URL
        # for diagnostics. url_patterns is left set to the same pattern so the
        # library's `on_data` path also honors it if a caller ever attaches one.
        url_patterns=[INIT_DATA_URL_PATTERN],
        on_capture=on_capture,
        on_status=on_status,
        session_sentinel=False,   # scraper doesn't auto-recover — operator uploads profiles
        login_timeout=CAPTURE_WINDOW_SECONDS,
        capture_timeout=CAPTURE_WINDOW_SECONDS,
    )

    try:
        client.launch(target_url=target_url)
    except BrowserNotFoundError as e:
        raise HTTPException(status_code=500, detail=str(e))

    # Let the worker thread capture for the window, then stop (unless the
    # caller asked us to leave the browser open for inspection).
    time.sleep(CAPTURE_WINDOW_SECONDS)
    state = client.get_state()
    if keep_open:
        _log("keep_open=true — Chromium left running. Kill it manually before "
             "your next /proposal call, or the debug port will collide.")
    else:
        client.quit()

    with lock:
        bodies_snapshot = list(init_data_bodies)
        urls_snapshot = list(captured_urls)

    login_wall = state.status == "waiting_login" or (
        state.error is not None and "login" in state.error.lower()
    )
    latest = bodies_snapshot[-1] if bodies_snapshot else None

    _log(f"done  status={state.status}  login_wall={login_wall}  "
         f"init_data_captures={len(bodies_snapshot)}  seen_urls={len(urls_snapshot)}")

    return {
        "url": target_url,
        "status": state.status,
        "error": state.error,
        "login_wall": login_wall,
        # The single response we care about — the last init/data body observed
        # (if the page hydrated more than once during the window).
        "init_data": latest,
        # Every init/data response captured during the window, oldest first.
        # Usually len 1; may be more if the page navigated / re-hydrated.
        "init_data_all": bodies_snapshot,
        "init_data_count": len(bodies_snapshot),
        # Every JSON XHR/fetch URL the interceptor saw — for diagnosing
        # "nothing captured" (the endpoint pattern may have changed).
        "captured_urls": urls_snapshot,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
