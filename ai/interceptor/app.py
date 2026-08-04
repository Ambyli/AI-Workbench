"""
interceptor — FastAPI + FastMCP wrapper around common.cdp_interceptor.

Give it a URL and a list of URL regex patterns; it launches Chrome under a
named profile, injects the interceptor, waits a bounded window, and returns
the captured JSON bodies bucketed by which pattern matched them.

Concurrency (see ai/INTERCEPTOR.md § Concurrency for the operator view):
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
    POST   /capture                       run one capture (see CaptureRequest)
    GET    /jobs                          snapshot of the port pool + running captures
    GET    /jobs/{job_id}                 detail on one in-flight capture (404 if not found)
    POST   /jobs/{job_id}/cancel          abort an in-flight capture, reclaim its slot
    /mcp                                  FastMCP HTTP transport — exposes tools:
                                          capture_url, list_profiles, list_jobs, get_job

Registered with LiteLLM in ai/litellm_config.yaml both as an `mcp_servers`
entry (model-invokable tool) and as a `pass_through_endpoints` entry
(``/v1/interceptor/...`` proxied to this service).
"""

from __future__ import annotations

# Load .env before anything reads os.environ at import time.
from common.env import load_env

load_env()

import os
import queue
import re
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastmcp import FastMCP
from pydantic import BaseModel, Field

from common.cdp_interceptor import (
    BrowserNotFoundError,
    Capture,
    InterceptorClient,
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
            f"matched={ {k: len(v) for k, v in matches_snapshot.items()} }"
        )

        return CaptureResponse(
            job_id=job.job_id,
            url=req.url,
            status=status,
            login_wall=login_wall,
            error=state.error,
            matches=matches_snapshot,
            captured_urls=urls_snapshot,
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


# ── MCP tools ───────────────────────────────────────────────────────────────
# Model-invokable tools return dicts and NEVER raise — errors surface inside
# the payload so the LLM can act on them. Operator-only knobs like
# ``keep_open`` (leaves Chrome running until manually killed) and
# ``debug_logging`` (emits traces to a DevTools console the LLM can't attach
# to) are deliberately omitted from the MCP surface; they remain available on
# the HTTP ``POST /capture`` request for operator use.


@mcp.tool()
def capture_url(
    url: str,
    url_patterns: list[str],
    profile: str,
    capture_window_seconds: int = DEFAULT_CAPTURE_WINDOW_SECONDS,
    login_timeout: int = 300,
    max_matches_per_pattern: Optional[int] = None,
) -> dict:
    """Load a URL under a named Chrome profile and return JSON XHR/fetch bodies
    whose URLs match any of the given regex patterns.

    Args:
        url: Fully-qualified URL to navigate to (https://…).
        url_patterns: List of regex patterns. Each intercepted XHR/fetch URL
            is matched with ``re.search`` against every pattern; the response
            body lands in the bucket of the first pattern it matches.
        profile: Named profile under ``INTERCEPTOR_PROFILES_ROOT``. Discover
            available names via the ``list_profiles`` tool. Profiles are
            uploaded out-of-band by an operator (see INTERCEPTOR.md).
        capture_window_seconds: How long to run Chrome to collect captures
            (default 20). Increase if the page fires XHRs late.
        login_timeout: Max seconds to wait for a login redirect to resolve
            before returning ``login_wall: true``.
        max_matches_per_pattern: Cap on how many bodies to return per pattern.

    Returns:
        A dict with keys ``job_id``, ``url``, ``status``, ``login_wall``,
        ``error``, ``matches`` (pattern → list of {url, body}), and
        ``captured_urls`` (every JSON XHR/fetch URL seen, for diagnostics).
    """
    req = CaptureRequest(
        url=url,
        url_patterns=url_patterns,
        profile=profile,
        capture_window_seconds=capture_window_seconds,
        keep_open=False,
        login_timeout=login_timeout,
        max_matches_per_pattern=max_matches_per_pattern,
        debug_logging=False,
    )
    try:
        return _run_capture(req).model_dump()
    except HTTPException as e:
        return {
            "job_id": "",
            "url": url,
            "status": "error",
            "login_wall": False,
            "error": f"HTTP {e.status_code}: {e.detail}",
            "matches": {p: [] for p in url_patterns},
            "captured_urls": [],
        }


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
