"""
interceptor — FastAPI + FastMCP wrapper around common.cdp_interceptor.

Give it a URL and a list of URL regex patterns; it launches Chrome under a
named profile, injects the interceptor, waits a bounded window, and returns
the captured JSON bodies bucketed by which pattern matched them.

Concurrency (see ai/interceptor/INTERCEPTOR.md § Concurrency for the operator view):
  * Different profiles fully parallel.
  * Same profile: the fast path uses the base ``--user-data-dir`` directly so
    refreshed session cookies persist. Concurrent same-profile requests fall
    into the slow path — each gets a ``shutil.copytree`` clone under
    ``PROFILES_ROOT/.temp/temp_profile_<uuid>/`` that's deleted on completion.
  * Port pool of CDP debug ports caps total concurrency
    (``INTERCEPTOR_MAX_CONCURRENT``, default 8). Pool exhausted → HTTP 429.
  * Chrome crash recovery is transparent: ``InterceptorClient.launch``
    already calls ``clear_singleton_locks`` before every start.

Job tracking (registry + endpoints) lives in ``common.jobs`` — see
``shared/common/src/common/jobs/``. Both the ``GET /jobs`` observability
endpoints and the ``POST /jobs/{id}/cancel`` operator hook are mounted via
``build_router`` there.

Endpoints:
    GET    /health                        healthcheck
    GET    /profiles                      list all named profiles
    GET    /profiles/{name}               one profile's status
    POST   /profiles/{name}/refresh       upload a .tgz of a captured Chrome profile
    DELETE /profiles/{name}               wipe one profile
    POST   /capture                       run one capture (see CaptureRequest); optional
                                          ``screenshot`` block returns an image of the page too
    POST   /screenshot                    navigate + screenshot only, no XHR patterns needed
    GET    /jobs                          snapshot of the port pool + running captures
    GET    /jobs/{job_id}                 detail on one in-flight capture (404 if not found)
    POST   /jobs/{job_id}/cancel          abort an in-flight capture, reclaim its slot
    /mcp                                  FastMCP HTTP transport — exposes tools:
                                          capture_url, screenshot_url, list_profiles,
                                          list_jobs, get_job

Screenshots ride on a SECOND CDP connection to the same tab (see
``common.cdp_interceptor.screenshot``) taken after the capture window elapses
and before Chrome quits, so the page has had the whole window to render. MCP
tools return the image as an ``ImageContent`` block next to the JSON payload;
the HTTP endpoints return it base64-encoded inside the JSON.

Registered with LiteLLM in ai/litellm/litellm_config.yaml both as an `mcp_servers`
entry (model-invokable tool) and as a `pass_through_endpoints` entry
(``/v1/interceptor/...`` proxied to this service).
"""

from __future__ import annotations

# Load .env before anything reads os.environ at import time.
from common.env import load_env

load_env()

import json
import os
import queue
import re
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastmcp import FastMCP
from fastmcp.tools import ToolResult
from fastmcp.utilities.types import Image as McpImage
from mcp.types import TextContent
from pydantic import BaseModel, Field, ValidationError, model_validator

from common.cdp_interceptor import (
    BrowserNotFoundError,
    Capture,
    InterceptorClient,
    ScreenshotError,
)
from common.jobs import InMemoryRegistry
from common.jobs.router import build_router

import profiles


# ── Config from env ────────────────────────────────────────────────────────
MAX_CONCURRENT = int(os.environ.get("INTERCEPTOR_MAX_CONCURRENT", "8"))
DEBUG_PORT_BASE = int(os.environ.get("INTERCEPTOR_DEBUG_PORT", "9224"))
DEFAULT_CAPTURE_WINDOW_SECONDS = int(
    os.environ.get("INTERCEPTOR_CAPTURE_WINDOW_SECONDS", "20")
)
# How long POST /screenshot / screenshot_url leave the page to render before
# the shot is taken. Chrome needs ~4-5s of that just to boot and navigate.
DEFAULT_SCREENSHOT_WAIT_SECONDS = int(
    os.environ.get("INTERCEPTOR_SCREENSHOT_WAIT_SECONDS", "15")
)


def _log(msg: str) -> None:
    """Stderr log line without a job_id (startup / global events). Per-job
    log lines go through ``job.log(...)`` on the registry's ``JobHandle``,
    which prepends the job_id tag automatically."""
    print(f"[interceptor] {msg}", file=sys.stderr, flush=True)


# ── Port pool ──────────────────────────────────────────────────────────────
# Each capture pulls one CDP debug port from the pool at start, returns it at
# end. Pool exhausted → HTTP 429 (retry later). The pool sets the hard
# resource ceiling — each active slot is one Chrome instance in RAM.
_port_pool: "queue.Queue[int]" = queue.Queue(maxsize=MAX_CONCURRENT)
for _p in range(DEBUG_PORT_BASE, DEBUG_PORT_BASE + MAX_CONCURRENT):
    _port_pool.put(_p)


def _acquire_port() -> Optional[int]:
    """Non-blocking port grab. Returns ``None`` when the pool is exhausted."""
    try:
        return _port_pool.get_nowait()
    except queue.Empty:
        return None


def _release_port(port: int) -> None:
    _port_pool.put(port)


# ── Per-profile locks ──────────────────────────────────────────────────────
# Non-blocking try-acquire chooses fast path (use base profile) vs slow path
# (clone the profile for this request). Two different profiles never contend.
_per_profile_locks: dict[str, threading.Lock] = {}
_locks_meta_lock = threading.Lock()


def _get_profile_lock(name: str) -> threading.Lock:
    with _locks_meta_lock:
        lock = _per_profile_locks.get(name)
        if lock is None:
            lock = threading.Lock()
            _per_profile_locks[name] = lock
        return lock


# ── Shared job registry ────────────────────────────────────────────────────
# In-memory backend from common.jobs — auto-mounts GET /jobs, GET /jobs/{id},
# and POST /jobs/{id}/cancel via build_router below.
class InterceptorMetadata(BaseModel):
    """Interceptor-api's per-job metadata payload. Serialized into the
    ``metadata`` field of ``JobBase`` for ``GET /jobs`` responses."""

    profile: str
    url: str
    port: int
    used_base_profile: bool
    temp_dir: Optional[str] = None


_registry: InMemoryRegistry = InMemoryRegistry(max_concurrent=MAX_CONCURRENT)


# ── FastAPI + FastMCP mount ────────────────────────────────────────────────
# Kokoro pattern (ai/kokoro/api/kokoro_server.py:22) mounts FastMCP by passing
# its lifespan directly. We need our own startup step (sweep orphaned temp
# clones), so we compose the two lifespans via asynccontextmanager.
mcp = FastMCP("Interceptor")
mcp_app = mcp.http_app(path="/")


@asynccontextmanager
async def lifespan(app: FastAPI):
    swept = profiles.sweep_temp_profiles()
    _log(f"startup sweep removed {swept} orphaned temp-profile dirs")
    _log(
        f"port pool: {MAX_CONCURRENT} slots "
        f"(ports {DEBUG_PORT_BASE}..{DEBUG_PORT_BASE + MAX_CONCURRENT - 1})"
    )
    async with mcp_app.lifespan(app):
        yield


app = FastAPI(title="Interceptor API", lifespan=lifespan)
app.include_router(build_router(_registry, include_cancel=True))


# ── Request / response models ───────────────────────────────────────────────
ScreenshotFormat = Literal["jpeg", "png", "webp"]


class ScreenshotOptions(BaseModel):
    """How to render the page image. Attached to ``CaptureRequest.screenshot``
    (opt-in) and flattened onto ``ScreenshotRequest``."""

    format: ScreenshotFormat = Field(
        default="jpeg",
        description="Image encoding. jpeg (default) is ~10x smaller than png "
        "for a typical page; png is lossless; webp is smallest but less "
        "universally decodable.",
    )
    quality: int = Field(
        default=80, ge=1, le=100,
        description="jpeg/webp compression quality. Ignored for png.",
    )
    full_page: bool = Field(
        default=False,
        description="Capture the whole scrollable document (clamped to "
        "max_height) instead of just the 1920x1080 viewport.",
    )
    scale: float = Field(
        default=1.0, gt=0, le=2.0,
        description="Output scale factor. 0.5 halves both axes and roughly "
        "quarters the payload — use it when the image is going into an LLM "
        "context and pixel-level detail isn't needed.",
    )
    max_height: int = Field(
        default=8000, ge=100, le=16384,
        description="full_page only: cap on captured document height in CSS "
        "px. Chrome refuses clips beyond 16384.",
    )


class ScreenshotResult(BaseModel):
    """One captured image. ``data_base64`` is omitted from MCP payloads (the
    image travels as an ImageContent block instead) but always present on the
    HTTP responses."""

    format: ScreenshotFormat
    mime_type: str
    width: int
    height: int
    full_page: bool
    bytes: int
    page_url: str = Field(
        description="Tab URL at capture time — reflects any redirects that "
        "happened after navigation (login bounce, canonical URL, etc.)."
    )
    data_base64: Optional[str] = None


class CaptureRequest(BaseModel):
    url: str = Field(..., description="URL to navigate to")
    url_patterns: list[str] = Field(
        default_factory=list,
        description="Regex patterns matched against every JSON XHR/fetch URL. "
        "Any capture whose URL matches at least one pattern is returned, "
        "bucketed by the first pattern that matched it. May be empty ONLY "
        "when `screenshot` is set (screenshot-only navigation).",
    )
    screenshot: Optional[ScreenshotOptions] = Field(
        default=None,
        description="If set, take a screenshot of the page after the capture "
        "window elapses (just before Chrome quits) and return it as "
        "`screenshot` on the response. A failed screenshot never fails the "
        "capture — it lands in `screenshot_error` instead.",
    )

    @model_validator(mode="after")
    def _patterns_or_screenshot(self) -> "CaptureRequest":
        if not self.url_patterns and self.screenshot is None:
            raise ValueError(
                "url_patterns must contain at least one pattern unless "
                "`screenshot` is requested"
            )
        return self
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
        description="If true, leave Chrome running after the window. Useful "
        "for interactive debugging; the job also stays in GET /jobs until the "
        "operator manually kills Chrome (or POSTs /jobs/{id}/cancel).",
    )
    login_timeout: int = Field(default=300, ge=1)
    max_matches_per_pattern: Optional[int] = Field(default=None, ge=1)
    debug_logging: bool = Field(default=False)
    login_url_patterns: list[str] = Field(
        default_factory=lambda: ["login", "signin", "/auth"],
        description="Regex patterns matched (re.search) against the tab URL "
        "AFTER navigation, to detect a redirect to a login wall. Omit to use "
        "the built-in defaults (login / signin / /auth). Because these are full "
        "regexes — not substrings — you can anchor one to a bare domain that a "
        "substring couldn't distinguish from an in-app URL, e.g. "
        r"'^https?://roofix\.io/?$' matches the logged-out root but not "
        "'roofix.io/project/...'. An empty list disables login detection.",
    )


class CaptureMatch(BaseModel):
    url: str
    body: Any


class CaptureResponse(BaseModel):
    job_id: str
    url: str
    status: str
    login_wall: bool
    error: Optional[str]
    matches: dict[str, list[CaptureMatch]]
    captured_urls: list[str]
    screenshot: Optional[ScreenshotResult] = None
    screenshot_error: Optional[str] = None


class ScreenshotRequest(ScreenshotOptions):
    """``POST /screenshot`` body — navigate under a profile and return an image.
    No XHR patterns; inherits the render knobs from ``ScreenshotOptions``."""

    url: str = Field(..., description="URL to navigate to")
    profile: str = Field(
        ...,
        description="Named Chrome profile under INTERCEPTOR_PROFILES_ROOT.",
    )
    wait_seconds: int = Field(
        default=DEFAULT_SCREENSHOT_WAIT_SECONDS,
        ge=1,
        le=600,
        description="Seconds to let the page load before the shot is taken. "
        "Chrome spends the first ~4-5s booting and navigating, so values "
        "under 8 mostly capture blank or half-rendered pages.",
    )
    login_timeout: int = Field(default=300, ge=1)
    login_url_patterns: list[str] = Field(
        default_factory=lambda: ["login", "signin", "/auth"],
        description="Same semantics as CaptureRequest.login_url_patterns.",
    )


class ScreenshotResponse(BaseModel):
    job_id: str
    url: str
    status: str = Field(
        description="Interceptor capture status. 'loading' is normal for a "
        "page that fired no JSON XHRs (static pages) — it does not mean the "
        "screenshot failed; check `screenshot` / `screenshot_error`."
    )
    login_wall: bool
    error: Optional[str]
    screenshot: Optional[ScreenshotResult]
    screenshot_error: Optional[str]


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
    """Perform one capture — handles fast/slow path selection, port pool,
    job registration, and cleanup entirely internally. Raises 429 only when
    the port pool is exhausted."""
    # Validate profile name early so 400 doesn't consume a port.
    try:
        profiles.validate_name(req.profile)
    except profiles.InvalidProfileNameError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Compile patterns early so bad regex fails fast with a 400 (also no port
    # consumed yet).
    try:
        compiled: list[tuple[str, re.Pattern[str]]] = [
            (p, re.compile(p)) for p in req.url_patterns
        ]
    except re.error as e:
        raise HTTPException(status_code=400, detail=f"invalid url_pattern: {e}")

    # Same fail-fast validation for the login-wall patterns. These are compiled
    # again inside the interceptor; compile here only to surface a clean 400.
    try:
        for p in req.login_url_patterns:
            re.compile(p)
    except re.error as e:
        raise HTTPException(status_code=400, detail=f"invalid login_url_pattern: {e}")

    # Reserve a port BEFORE touching anything else. If capacity is out, we're
    # done — 429 the caller.
    port = _acquire_port()
    if port is None:
        raise HTTPException(
            status_code=429,
            detail=(
                f"capacity exhausted (max_concurrent={MAX_CONCURRENT}); "
                "retry later"
            ),
        )

    profile_lock = _get_profile_lock(req.profile)
    used_base = profile_lock.acquire(blocking=False)

    temp_dir: Optional[Path] = None
    profile_dir: str
    job = None  # populated after we know fast/slow path
    try:
        if used_base:
            profile_dir = str(profiles.profile_path(req.profile))
            job = _registry.register(
                InterceptorMetadata(
                    profile=req.profile,
                    url=req.url,
                    port=port,
                    used_base_profile=True,
                    temp_dir=None,
                ),
                initial_phase="capturing",
            )
            job.log(
                f"start  profile={req.profile}  path=base  port={port}  "
                f"keep_open={req.keep_open}  patterns={len(compiled)}  url={req.url}"
            )
        else:
            job = _registry.register(
                InterceptorMetadata(
                    profile=req.profile,
                    url=req.url,
                    port=port,
                    used_base_profile=False,
                    temp_dir=None,
                ),
                initial_phase="cloning",
            )
            job.log(f"clone  profile={req.profile}  base is in use — cloning to temp")
            try:
                temp_dir = profiles.clone_profile(req.profile)
            except FileNotFoundError as e:
                _registry.unregister(job.job_id)
                raise HTTPException(status_code=400, detail=str(e))
            profile_dir = str(temp_dir)
            job.set_phase("capturing")
            job.update_metadata(temp_dir=profile_dir)
            job.log(
                f"start  profile={req.profile}  path={profile_dir}  port={port}  "
                f"keep_open={req.keep_open}  patterns={len(compiled)}  url={req.url}"
            )

        # ── Collector callbacks ────────────────────────────────────────────
        matches: dict[str, list[CaptureMatch]] = {p: [] for p, _ in compiled}
        captured_urls: list[str] = []
        results_lock = threading.Lock()

        def on_capture(cap: Capture) -> None:
            with results_lock:
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
                        job.log(f"match  {pattern_str}  {cap.url[:110]}")
                        return

        def on_status(status: str, error: Optional[str]) -> None:
            job.log(f"status  {status}  {error or ''}")

        # session_sentinel=True + the sentinel that profiles.unpack_profile
        # writes on upload = InterceptorClient launches headless on the first
        # call. copytree preserves the sentinel into the clone, so the slow
        # path launches headless too. If the persisted session has expired,
        # InterceptorClient hits TimeoutError and sets status="waiting_login",
        # which we surface as login_wall=true.
        client = InterceptorClient(
            profile_dir=profile_dir,
            debug_port=port,
            url_patterns=req.url_patterns,
            on_capture=on_capture,
            on_status=on_status,
            session_sentinel=True,
            login_timeout=req.login_timeout,
            capture_timeout=req.capture_window_seconds,
            debug_logging=req.debug_logging,
            login_url_keywords=tuple(req.login_url_patterns),
        )

        try:
            client.launch(target_url=req.url)
        except BrowserNotFoundError as e:
            raise HTTPException(status_code=500, detail=str(e))

        # Wait for the capture window, but wake early if cancelled.
        # wait_or_cancel returns True on cancel, False on timeout.
        cancelled = job.wait_or_cancel(timeout=req.capture_window_seconds)
        state = client.get_state()

        # Screenshot BEFORE quitting Chrome. Runs on its own CDP socket so the
        # worker's session is untouched. Failure is reported, never raised —
        # the XHR captures are still valid even if the image isn't.
        shot_result: Optional[ScreenshotResult] = None
        shot_error: Optional[str] = None
        if req.screenshot is not None and not cancelled:
            job.set_phase("screenshot")
            opts = req.screenshot
            try:
                shot = client.screenshot(
                    format=opts.format,
                    quality=opts.quality,
                    full_page=opts.full_page,
                    scale=opts.scale,
                    max_height=opts.max_height,
                )
                shot_result = ScreenshotResult(
                    format=shot.format,
                    mime_type=shot.mime_type,
                    width=shot.width,
                    height=shot.height,
                    full_page=shot.full_page,
                    bytes=len(shot.data),
                    page_url=shot.page_url,
                    data_base64=shot.to_base64(),
                )
                job.log(
                    f"screenshot  {shot.format}  {shot.width}x{shot.height}  "
                    f"full_page={shot.full_page}  bytes={len(shot.data)}  "
                    f"page_url={shot.page_url[:110]}"
                )
            except ScreenshotError as e:
                shot_error = str(e)
                job.log(f"screenshot failed: {e}")
            except Exception as e:  # never let an image kill a capture
                shot_error = f"{type(e).__name__}: {e}"
                job.log(f"screenshot failed (unexpected): {shot_error}")

        if cancelled:
            job.log("cancelled — aborting capture and reclaiming slot")
            client.quit()
        elif req.keep_open:
            job.log(
                "keep_open=true — Chromium left running. Kill it manually or "
                "POST /jobs/{id}/cancel to reclaim port + profile-lock (fast "
                "path) or temp dir (slow path)."
            )
        else:
            client.quit()

        login_wall = state.status == "waiting_login" or (
            state.error is not None and "login" in state.error.lower()
        )

        with results_lock:
            matches_snapshot = {k: list(v) for k, v in matches.items()}
            urls_snapshot = list(captured_urls)

        status = "cancelled" if cancelled else state.status
        job.log(
            f"done  status={status}  login_wall={login_wall}  "
            f"seen_urls={len(urls_snapshot)}  "
            f"matched={ {k: len(v) for k, v in matches_snapshot.items()} }  "
            f"screenshot={'yes' if shot_result else ('failed' if shot_error else 'no')}"
        )

        return CaptureResponse(
            job_id=job.job_id,
            url=req.url,
            status=status,
            login_wall=login_wall,
            error=state.error,
            matches=matches_snapshot,
            captured_urls=urls_snapshot,
            screenshot=shot_result,
            screenshot_error=shot_error,
        )
    finally:
        if job is not None:
            job.set_phase("cleaning_up")
        # Skip cleanup for keep_open=true UNLESS the capture was cancelled.
        # Cancel is deliberately the way to reclaim a hung keep_open slot.
        # When we skip cleanup, the job stays in the registry so /jobs still
        # shows the hung capture — matching pre-migration behavior.
        skip_cleanup = req.keep_open and job is not None and not job.is_cancelled()
        if not skip_cleanup:
            if used_base:
                try:
                    profile_lock.release()
                except RuntimeError:
                    pass
            elif temp_dir is not None:
                profiles.remove_temp_profile(temp_dir)
            _release_port(port)
            if job is not None:
                _registry.unregister(job.job_id)


@app.post("/capture", response_model=CaptureResponse)
def capture(req: CaptureRequest) -> CaptureResponse:
    return _run_capture(req)


def _run_screenshot(req: ScreenshotRequest) -> ScreenshotResponse:
    """Screenshot-only navigation: a capture with no URL patterns whose window
    is ``wait_seconds``. Reuses ``_run_capture`` so the port pool, profile
    fast/slow path, job registry, and cancel semantics are all identical."""
    cap = CaptureRequest(
        url=req.url,
        url_patterns=[],
        profile=req.profile,
        capture_window_seconds=req.wait_seconds,
        keep_open=False,
        login_timeout=req.login_timeout,
        debug_logging=False,
        login_url_patterns=req.login_url_patterns,
        screenshot=ScreenshotOptions(
            format=req.format,
            quality=req.quality,
            full_page=req.full_page,
            scale=req.scale,
            max_height=req.max_height,
        ),
    )
    res = _run_capture(cap)
    return ScreenshotResponse(
        job_id=res.job_id,
        url=res.url,
        status=res.status,
        login_wall=res.login_wall,
        error=res.error,
        screenshot=res.screenshot,
        screenshot_error=res.screenshot_error,
    )


@app.post("/screenshot", response_model=ScreenshotResponse)
def screenshot(req: ScreenshotRequest) -> ScreenshotResponse:
    return _run_screenshot(req)


# ── MCP tools ───────────────────────────────────────────────────────────────
# Model-invokable tools return dicts and NEVER raise — errors surface inside
# the payload so the LLM can act on them. Operator-only knobs like
# ``keep_open`` (leaves Chrome running until manually killed) and
# ``debug_logging`` (emits traces to a DevTools console the LLM can't attach
# to) are deliberately omitted from the MCP surface; they remain available on
# the HTTP ``POST /capture`` request for operator use.
#
# Screenshots: the image is delivered as an MCP ``ImageContent`` block (so a
# multimodal model can look at it) alongside the JSON payload, which carries
# only the image metadata — the base64 is NOT duplicated into the text.


def _screenshot_tool_result(payload: dict) -> ToolResult:
    """Build the MCP result for a response that may carry a screenshot.

    ``payload`` is a ``model_dump()`` of CaptureResponse / ScreenshotResponse.
    If it holds a screenshot, the base64 is pulled out of the JSON and emitted
    as a separate ImageContent block; the JSON keeps width/height/format/etc.
    """
    content: list = []
    shot = payload.get("screenshot")
    image_block = None
    if isinstance(shot, dict) and shot.get("data_base64"):
        import base64 as _b64

        raw = _b64.b64decode(shot["data_base64"])
        image_block = McpImage(data=raw, format=shot["format"]).to_image_content()
        shot = {k: v for k, v in shot.items() if k != "data_base64"}
        payload = {**payload, "screenshot": shot}
    content.append(TextContent(type="text", text=json.dumps(payload, default=str)))
    if image_block is not None:
        content.append(image_block)
    return ToolResult(content=content, structured_content=payload)


def _http_error_text(e: Exception) -> str:
    if isinstance(e, HTTPException):
        return f"HTTP {e.status_code}: {e.detail}"
    if isinstance(e, ValidationError):
        return "invalid request: " + "; ".join(
            err.get("msg", str(err)) for err in e.errors()
        )
    return f"{type(e).__name__}: {e}"


@mcp.tool()
def capture_url(
    url: str,
    url_patterns: list[str],
    profile: str,
    capture_window_seconds: int = DEFAULT_CAPTURE_WINDOW_SECONDS,
    login_timeout: int = 300,
    max_matches_per_pattern: Optional[int] = None,
    screenshot: bool = False,
    screenshot_full_page: bool = False,
    screenshot_format: ScreenshotFormat = "jpeg",
    screenshot_scale: float = 1.0,
) -> ToolResult:
    """Load a URL under a named Chrome profile and return JSON XHR/fetch bodies
    whose URLs match any of the given regex patterns — optionally with a
    screenshot of the rendered page.

    Args:
        url: Fully-qualified URL to navigate to (https://…).
        url_patterns: List of regex patterns. Each intercepted XHR/fetch URL
            is matched with ``re.search`` against every pattern; the response
            body lands in the bucket of the first pattern it matches. May be
            empty only when ``screenshot`` is true (use ``screenshot_url`` for
            that case — it's the same thing with clearer defaults).
        profile: Named profile under ``INTERCEPTOR_PROFILES_ROOT``. Discover
            available names via the ``list_profiles`` tool. Profiles are
            uploaded out-of-band by an operator (see INTERCEPTOR.md).
        capture_window_seconds: How long to run Chrome to collect captures
            (default 20). Increase if the page fires XHRs late.
        login_timeout: Max seconds to wait for a login redirect to resolve
            before returning ``login_wall: true``.
        max_matches_per_pattern: Cap on how many bodies to return per pattern.
        screenshot: Also capture an image of the page at the end of the
            window, returned as an image content block plus ``screenshot``
            metadata ({format, mime_type, width, height, full_page, bytes,
            page_url}) in the JSON. On failure ``screenshot_error`` is set and
            the XHR results are still returned.
        screenshot_full_page: Whole scrollable document (max 8000px tall)
            instead of the 1920x1080 viewport.
        screenshot_format: "jpeg" (default, small), "png" (lossless), "webp".
        screenshot_scale: Output scale, 0 < scale <= 2. 0.5 quarters the
            payload; use it when detail isn't needed.

    Returns:
        JSON with keys ``job_id``, ``url``, ``status``, ``login_wall``,
        ``error``, ``matches`` (pattern → list of {url, body}),
        ``captured_urls`` (every JSON XHR/fetch URL seen, for diagnostics),
        ``screenshot`` and ``screenshot_error`` — followed by the image block
        when a screenshot was taken.
    """
    try:
        req = CaptureRequest(
            url=url,
            url_patterns=url_patterns,
            profile=profile,
            capture_window_seconds=capture_window_seconds,
            keep_open=False,
            login_timeout=login_timeout,
            max_matches_per_pattern=max_matches_per_pattern,
            debug_logging=False,
            screenshot=ScreenshotOptions(
                format=screenshot_format,
                full_page=screenshot_full_page,
                scale=screenshot_scale,
            )
            if screenshot
            else None,
        )
        return _screenshot_tool_result(_run_capture(req).model_dump())
    except (HTTPException, ValidationError) as e:
        return _screenshot_tool_result(
            {
                "job_id": "",
                "url": url,
                "status": "error",
                "login_wall": False,
                "error": _http_error_text(e),
                "matches": {p: [] for p in url_patterns},
                "captured_urls": [],
                "screenshot": None,
                "screenshot_error": None,
            }
        )


@mcp.tool()
def screenshot_url(
    url: str,
    profile: str,
    wait_seconds: int = DEFAULT_SCREENSHOT_WAIT_SECONDS,
    full_page: bool = False,
    format: ScreenshotFormat = "jpeg",
    quality: int = 80,
    scale: float = 1.0,
    login_timeout: int = 300,
) -> ToolResult:
    """Navigate to a URL under a named Chrome profile and return a screenshot
    of the rendered page. Use this to *see* a page — layout, charts, error
    banners, whatever isn't in an XHR body. Use ``capture_url`` when you want
    the JSON the page fetched (optionally with ``screenshot=true`` for both).

    Args:
        url: Fully-qualified URL to navigate to (https://…).
        profile: Named profile under ``INTERCEPTOR_PROFILES_ROOT`` — see
            ``list_profiles``. The page renders with that profile's cookies,
            so authenticated dashboards work if the profile is logged in.
        wait_seconds: Seconds to let the page load before the shot (default
            15). Chrome spends ~4-5s of this booting and navigating; raise it
            for slow SPAs, lower it (not below ~8) for static pages.
        full_page: Whole scrollable document (clamped to 8000px tall) instead
            of the 1920x1080 viewport.
        format: "jpeg" (default, ~100-300 KB for a viewport), "png"
            (lossless, often 1-3 MB), or "webp".
        quality: 1-100 for jpeg/webp. Ignored for png.
        scale: Output scale, 0 < scale <= 2. 0.5 halves each axis.
        login_timeout: Max seconds to wait for a login redirect to resolve
            before returning ``login_wall: true``.

    Returns:
        JSON with ``job_id``, ``url``, ``status``, ``login_wall``, ``error``,
        ``screenshot`` ({format, mime_type, width, height, full_page, bytes,
        page_url}) and ``screenshot_error`` — followed by the image itself as
        an image content block. ``page_url`` is where the tab actually ended
        up, so a redirect to a login page is visible even without
        ``login_wall``. ``status: "loading"`` is normal for pages that fire
        no JSON XHRs and does not indicate a failed screenshot.
    """
    try:
        req = ScreenshotRequest(
            url=url,
            profile=profile,
            wait_seconds=wait_seconds,
            full_page=full_page,
            format=format,
            quality=quality,
            scale=scale,
            login_timeout=login_timeout,
        )
        return _screenshot_tool_result(_run_screenshot(req).model_dump())
    except (HTTPException, ValidationError) as e:
        return _screenshot_tool_result(
            {
                "job_id": "",
                "url": url,
                "status": "error",
                "login_wall": False,
                "error": _http_error_text(e),
                "screenshot": None,
                "screenshot_error": None,
            }
        )


@mcp.tool()
def list_profiles() -> dict:
    """List every named Chrome profile currently uploaded to interceptor.

    Use this before ``capture_url`` to see which ``profile`` values are valid.
    Only profiles with ``sentinel_present: true`` are usable — a false value
    means the profile hasn't been through the operator's upload flow and
    Chrome would fail to launch headless.

    Returns:
        A dict with ``root`` (the profiles directory path) and ``profiles``
        (a list of ``{name, path, present, size_bytes, sentinel_present}``
        objects, one per named profile).
    """
    return {"root": profiles.PROFILES_ROOT, "profiles": profiles.list_profiles()}


@mcp.tool()
def list_jobs() -> dict:
    """Return a snapshot of the port pool + all currently-running captures.

    Use this to see how busy interceptor is before firing a `capture_url`
    (avoids surprise 429s when the pool is exhausted), or to correlate a
    ``job_id`` from a previous response with what's actually still running.

    Completed captures are not retained — they disappear from ``jobs`` the
    moment they return to their caller.

    Returns:
        A dict with ``max_concurrent`` (pool size), ``active_count`` (jobs
        currently running), ``available`` (slots free), and ``jobs`` (a list
        of ``JobBase`` objects — see common.jobs.model).
    """
    return _registry.list_all().model_dump()


@mcp.tool()
def get_job(job_id: str) -> dict:
    """Return the status of one in-flight capture by its ``job_id``.

    The ``job_id`` is the 12-char hex identifier returned from a
    ``capture_url`` call or listed by ``list_jobs``. If the id is unknown or
    the capture has already completed, this returns an error payload rather
    than raising — completed captures are not retained.

    Returns:
        On success, a ``JobBase`` dict with ``job_id``, ``phase``,
        ``created_at``, ``updated_at``, ``elapsed_seconds``, ``metadata``
        (interceptor's own {profile, url, port, used_base_profile, temp_dir}),
        ``result``, and ``error``.
        On unknown/finished id, ``{"error": "no active job <id>"}``.
    """
    snap = _registry.get(job_id)
    if snap is None:
        return {"error": f"no active job {job_id!r}"}
    return snap.model_dump()


app.mount("/mcp", mcp_app)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
