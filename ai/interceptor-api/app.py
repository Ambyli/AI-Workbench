"""
interceptor-api — FastAPI + FastMCP wrapper around common.cdp_interceptor.

Give it a URL and a list of URL regex patterns; it launches Chrome under a
named profile, injects the interceptor, waits a bounded window, and returns
the captured JSON bodies bucketed by which pattern matched them.

Endpoints:
    GET    /health                        healthcheck
    GET    /profiles                      list all named profiles
    GET    /profiles/{name}               one profile's status
    POST   /profiles/{name}/refresh       upload a .tgz of a captured Chrome profile
    DELETE /profiles/{name}               wipe one profile
    POST   /capture                       run one capture (see CaptureRequest)
    /mcp                                  FastMCP HTTP transport — same `capture` tool

Registered with LiteLLM in ai/litellm_config.yaml both as an `mcp_servers`
entry (model-invokable tool) and as a `pass_through_endpoints` entry
(``/v1/interceptor/...`` proxied to this service).
"""

from __future__ import annotations

# Load .env before anything reads os.environ at import time.
from common.env import load_env

load_env()

import os
import re
import sys
import threading
import time
from typing import Any, Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastmcp import FastMCP
from pydantic import BaseModel, Field

from common.cdp_interceptor import (
    BrowserNotFoundError,
    Capture,
    InterceptorClient,
)

import profiles


DEFAULT_DEBUG_PORT = int(os.environ.get("INTERCEPTOR_DEBUG_PORT", "9224"))
DEFAULT_CAPTURE_WINDOW_SECONDS = int(
    os.environ.get("INTERCEPTOR_CAPTURE_WINDOW_SECONDS", "20")
)

def _log(msg: str) -> None:
    print(f"[interceptor-api] {msg}", file=sys.stderr, flush=True)


# ── Concurrency guard ──────────────────────────────────────────────────────
# InterceptorClient binds to a single debug port; two captures at once would
# collide. Serialize with a non-blocking try-acquire so callers get 409
# immediately instead of piling up on a lock.
_capture_lock = threading.Lock()


# ── FastAPI + FastMCP mount (Kokoro pattern: ai/kokoro/api/kokoro_server.py:22) ──
mcp = FastMCP("Interceptor")
mcp_app = mcp.http_app(path="/")
app = FastAPI(title="Interceptor API", lifespan=mcp_app.lifespan)


# ── Request / response models ───────────────────────────────────────────────
class CaptureRequest(BaseModel):
    url: str = Field(..., description="URL to navigate to")
    url_patterns: list[str] = Field(
        ...,
        description="Regex patterns matched against every JSON XHR/fetch URL. "
        "Any capture whose URL matches at least one pattern is returned, "
        "bucketed by the first pattern that matched it.",
        min_length=1,
    )
    profile: str = Field(
        ...,
        description="Named Chrome profile under INTERCEPTOR_PROFILES_ROOT. "
        "Must be refreshed via POST /profiles/{name}/refresh first if the "
        "target requires auth.",
    )
    capture_window_seconds: int = Field(
        default=DEFAULT_CAPTURE_WINDOW_SECONDS,
        ge=1,
        le=600,
        description="How long to keep Chrome running to collect captures.",
    )
    keep_open: bool = Field(
        default=False,
        description="If true, leave Chrome running after the window. "
        "Debug-port collision means the next /capture call for this "
        "service will 409 until the operator kills it.",
    )
    login_timeout: int = Field(default=300, ge=1)
    max_matches_per_pattern: Optional[int] = Field(default=None, ge=1)
    debug_logging: bool = Field(default=False)


class CaptureMatch(BaseModel):
    url: str
    body: Any


class CaptureResponse(BaseModel):
    url: str
    status: str
    login_wall: bool
    error: Optional[str]
    matches: dict[str, list[CaptureMatch]]
    captured_urls: list[str]


# ── Health + profile endpoints ──────────────────────────────────────────────
@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/profiles")
def profiles_list() -> dict:
    return {"root": profiles.PROFILES_ROOT, "profiles": profiles.list_profiles()}


@app.get("/profiles/{name}")
def profiles_get(name: str) -> dict:
    try:
        return profiles.profile_info(name)
    except profiles.InvalidProfileNameError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/profiles/{name}/refresh")
def profiles_refresh(name: str, archive: UploadFile = File(...)) -> dict:
    """Accept a .tgz of a Chrome ``--user-data-dir`` and persist it under
    ``PROFILES_ROOT/{name}``. See ``profiles.py`` module docstring for the
    laptop-side capture flow."""
    try:
        profiles.validate_name(name)
    except profiles.InvalidProfileNameError as e:
        raise HTTPException(status_code=400, detail=str(e))
    try:
        info = profiles.unpack_profile(name, archive.file)
        return {"unpacked": True, **info}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"failed to unpack profile: {e}")


@app.delete("/profiles/{name}")
def profiles_delete(name: str) -> dict:
    try:
        return profiles.delete_profile(name)
    except profiles.InvalidProfileNameError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Capture core (shared by HTTP + MCP) ─────────────────────────────────────
def _run_capture(req: CaptureRequest) -> CaptureResponse:
    """Perform one capture. Caller holds the concurrency lock."""
    try:
        profile_dir = str(profiles.profile_path(req.profile))
    except profiles.InvalidProfileNameError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Compile once so a bad regex fails fast with a 400.
    try:
        compiled: list[tuple[str, re.Pattern[str]]] = [
            (p, re.compile(p)) for p in req.url_patterns
        ]
    except re.error as e:
        raise HTTPException(status_code=400, detail=f"invalid url_pattern: {e}")

    _log(
        f"start  profile={req.profile}  "
        f"keep_open={req.keep_open}  patterns={len(compiled)}  url={req.url}"
    )

    matches: dict[str, list[CaptureMatch]] = {p: [] for p, _ in compiled}
    captured_urls: list[str] = []
    lock = threading.Lock()

    def on_capture(cap: Capture) -> None:
        with lock:
            captured_urls.append(cap.url)
            for pattern_str, rx in compiled:
                if rx.search(cap.url):
                    bucket = matches[pattern_str]
                    if (
                        req.max_matches_per_pattern is not None
                        and len(bucket) >= req.max_matches_per_pattern
                    ):
                        return
                    bucket.append(CaptureMatch(url=cap.url, body=cap.body))
                    _log(f"match  {pattern_str}  {cap.url[:110]}")
                    return

    def on_status(status: str, error: Optional[str]) -> None:
        _log(f"status  {status}  {error or ''}")

    # session_sentinel=True + the sentinel that profiles.unpack_profile writes
    # on upload = InterceptorClient launches headless on the first call. In a
    # container that's required (no display). If the persisted session has
    # expired, InterceptorClient hits TimeoutError, clears the sentinel, and
    # sets status="waiting_login" — which we surface as login_wall=true.
    client = InterceptorClient(
        profile_dir=profile_dir,
        debug_port=DEFAULT_DEBUG_PORT,
        url_patterns=req.url_patterns,
        on_capture=on_capture,
        on_status=on_status,
        session_sentinel=True,
        login_timeout=req.login_timeout,
        capture_timeout=req.capture_window_seconds,
        debug_logging=req.debug_logging,
    )

    try:
        client.launch(target_url=req.url)
    except BrowserNotFoundError as e:
        raise HTTPException(status_code=500, detail=str(e))

    time.sleep(req.capture_window_seconds)
    state = client.get_state()
    if req.keep_open:
        _log(
            "keep_open=true — Chromium left running. Kill it manually before "
            "the next /capture call or debug port 9224 will collide."
        )
    else:
        client.quit()

    login_wall = state.status == "waiting_login" or (
        state.error is not None and "login" in state.error.lower()
    )

    with lock:
        matches_snapshot = {k: list(v) for k, v in matches.items()}
        urls_snapshot = list(captured_urls)

    _log(
        f"done  status={state.status}  login_wall={login_wall}  "
        f"seen_urls={len(urls_snapshot)}  "
        f"matched={ {k: len(v) for k, v in matches_snapshot.items()} }"
    )

    return CaptureResponse(
        url=req.url,
        status=state.status,
        login_wall=login_wall,
        error=state.error,
        matches=matches_snapshot,
        captured_urls=urls_snapshot,
    )


@app.post("/capture", response_model=CaptureResponse)
def capture(req: CaptureRequest) -> CaptureResponse:
    if not _capture_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail="another capture is in progress; retry after it finishes",
        )
    try:
        return _run_capture(req)
    finally:
        _capture_lock.release()


# ── MCP tool ────────────────────────────────────────────────────────────────
@mcp.tool()
def capture_url(
    url: str,
    url_patterns: list[str],
    profile: str,
    capture_window_seconds: int = DEFAULT_CAPTURE_WINDOW_SECONDS,
    keep_open: bool = False,
    login_timeout: int = 300,
    max_matches_per_pattern: Optional[int] = None,
    debug_logging: bool = False,
) -> dict:
    """Load a URL under a named Chrome profile and return JSON XHR/fetch bodies
    whose URLs match any of the given regex patterns.

    Args:
        url: Fully-qualified URL to navigate to (https://…).
        url_patterns: List of regex patterns. Each intercepted XHR/fetch URL
            is matched with ``re.search`` against every pattern; the response
            body lands in the bucket of the first pattern it matches.
        profile: Named profile under ``INTERCEPTOR_PROFILES_ROOT``. The profile
            must be refreshed first via ``POST /profiles/{name}/refresh`` when
            the target site requires login.
        capture_window_seconds: How long to run Chrome to collect captures
            (default 20). Increase if the page fires XHRs late.
        keep_open: Leave Chrome running after the window — for interactive
            debugging. Blocks the next call until the operator kills it.
        login_timeout: Max seconds to wait for a login redirect to resolve.
        max_matches_per_pattern: Cap on how many bodies to return per pattern.
        debug_logging: Emit ``[interceptor]`` traces to the browser console.

    Returns:
        A dict with keys ``url``, ``status``, ``login_wall``, ``error``,
        ``matches`` (pattern → list of {url, body}), and ``captured_urls``
        (every JSON XHR/fetch URL seen, for diagnostics).
    """
    req = CaptureRequest(
        url=url,
        url_patterns=url_patterns,
        profile=profile,
        capture_window_seconds=capture_window_seconds,
        keep_open=keep_open,
        login_timeout=login_timeout,
        max_matches_per_pattern=max_matches_per_pattern,
        debug_logging=debug_logging,
    )
    if not _capture_lock.acquire(blocking=False):
        return {
            "url": url,
            "status": "error",
            "login_wall": False,
            "error": "another capture is in progress; retry after it finishes",
            "matches": {p: [] for p in url_patterns},
            "captured_urls": [],
        }
    try:
        return _run_capture(req).model_dump()
    finally:
        _capture_lock.release()


app.mount("/mcp", mcp_app)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
