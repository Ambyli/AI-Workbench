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
                                          ``page_script`` / ``actions`` drive the page, and a
                                          ``screenshot`` action returns an image of it
    GET    /jobs                          snapshot of the port pool + running captures
    GET    /jobs/{job_id}                 detail on one in-flight capture (404 if not found)
    POST   /jobs/{job_id}/cancel          abort an in-flight capture, reclaim its slot
    /mcp                                  FastMCP HTTP transport — exposes tools:
                                          capture_url, list_profiles, list_jobs, get_job

``page_script`` / ``actions`` ride a SECOND CDP connection to the same tab
(see ``common.cdp_interceptor.actions``): a helper thread started right after
launch waits for the page to be ready, then fills / clicks / evaluates, so the
requests the page fires in response are captured through the normal path.
``stop_when_matched`` ends the window as soon as every pattern has a match.

Screenshots are an action step (``{"type": "screenshot", …}``), taken on that
same side connection wherever the step sits in ``actions`` — so a caller can
see the page between steps. A failed one is recorded and the run continues.
The image comes back on the step's ``actions_report`` entry: base64 inside the
JSON over HTTP, an ``ImageContent`` block per step over MCP. A capture with no
``url_patterns`` (allowed only when an action is a screenshot) ends as soon as
the actions finish.

``login_actions`` run only if the tab hits a login wall: the capture session
calls them (``InterceptorClient``'s ``on_login_wall`` hook) to fill and submit
the login form, with ``${key}`` references in fill values resolved from the
profile's server-side logins file (``logins.py``). The request — and so every
log line, job record and response — only ever carries the reference.

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
from typing import Annotated, Any, Literal, Optional, Union

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastmcp import FastMCP
from fastmcp.tools import ToolResult
from fastmcp.utilities.types import Image as McpImage
from mcp.types import TextContent
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from common.cdp_interceptor import (
    Action,
    ActionError,
    ActionsReport,
    BrowserNotFoundError,
    Capture,
    InterceptorClient,
    parse_actions,
)
from common.jobs import InMemoryRegistry
from common.jobs.router import build_router

import logins
import profiles


# ── Config from env ────────────────────────────────────────────────────────
MAX_CONCURRENT = int(os.environ.get("INTERCEPTOR_MAX_CONCURRENT", "8"))
DEBUG_PORT_BASE = int(os.environ.get("INTERCEPTOR_DEBUG_PORT", "9224"))
DEFAULT_CAPTURE_WINDOW_SECONDS = int(
    os.environ.get("INTERCEPTOR_CAPTURE_WINDOW_SECONDS", "20")
)
# Screenshot steps one request may carry. Each image is base64 in the HTTP
# JSON and an image block in a model's context, so the count is the payload.
MAX_SCREENSHOT_ACTIONS = 10
# Upper bound on one login_actions run (gate + every step) when a request
# omits login_actions_timeout_seconds.
DEFAULT_LOGIN_ACTIONS_TIMEOUT_SECONDS = 60


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


# Held while login_actions submit a profile's login form, so two captures on
# the same profile (fast path + a slow-path clone) never log in at once.
# Separate from the fast/slow-path lock above: that one is held for a whole
# capture and only ever try-acquired.
_login_locks: dict[str, threading.Lock] = {}


def _get_login_lock(name: str) -> threading.Lock:
    with _locks_meta_lock:
        lock = _login_locks.get(name)
        if lock is None:
            lock = threading.Lock()
            _login_locks[name] = lock
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
    # Set only when the request carries page_script / actions.
    actions_done: Optional[int] = None
    actions_total: Optional[int] = None


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


# ── Browser actions ─────────────────────────────────────────────────────────
# One model per step type, discriminated on ``type``, so OpenAPI documents each
# shape and a malformed step is a field-level 422. The models mirror
# ``common.cdp_interceptor.actions._FIELDS``; ``parse_actions`` re-validates
# the dumped dicts (press keys, ranges) and is the authority if they drift.
_SELECTOR_DESC = (
    "CSS selector, matched in the document AND inside every open shadow root "
    "(Salesforce LWC / custom elements), each tree on its own — so `input` "
    "finds an input inside a component, but a descendant combinator never "
    "crosses a shadow boundary (`lightning-input input` matches nothing; use "
    "the host `lightning-input` — fill/select descend into it — or "
    "`input[name=...]`). The first VISIBLE match wins."
)
_TEXT_DESC = (
    "Optional filter on the matches: substring of the element's trimmed text "
    "(including text rendered in its shadow tree), value, aria-label, "
    "placeholder or title. Written `/…/flags` it is a JS regex."
)
_TIMEOUT_DESC = "Seconds to keep polling for the element before the step fails."


class _ActionModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WaitForAction(_ActionModel):
    """Poll until an element exists (`attached`) or is visible (`visible`)."""
    type: Literal["wait_for"]
    selector: str = Field(..., min_length=1, description=_SELECTOR_DESC)
    text: Optional[str] = Field(default=None, description=_TEXT_DESC)
    state: Literal["visible", "attached"] = "visible"
    timeout_s: float = Field(default=15.0, gt=0, le=600, description=_TIMEOUT_DESC)


class FillAction(_ActionModel):
    """Focus the input (descending into a custom element's shadow root to the
    real <input>/<textarea>), optionally clear it, type `value` with trusted
    input events, then fire `change` and blur."""
    type: Literal["fill"]
    selector: str = Field(..., min_length=1, description=_SELECTOR_DESC)
    text: Optional[str] = Field(default=None, description=_TEXT_DESC)
    value: str = Field(..., description="Text to type.")
    clear: bool = Field(default=True, description="Empty the field before typing.")
    timeout_s: float = Field(default=15.0, gt=0, le=600, description=_TIMEOUT_DESC)


class ClickAction(_ActionModel):
    """Scroll the element into view and click its centre. `mouse` (default)
    sends real pressed/released mouse events; `js` calls `el.click()`."""
    type: Literal["click"]
    selector: str = Field(..., min_length=1, description=_SELECTOR_DESC)
    text: Optional[str] = Field(default=None, description=_TEXT_DESC)
    method: Literal["mouse", "js"] = "mouse"
    timeout_s: float = Field(default=15.0, gt=0, le=600, description=_TIMEOUT_DESC)


class PressAction(_ActionModel):
    """Send one key (keyDown + keyUp) to the focused element — or, with
    `selector`, to that element after focusing it. `fill` blurs the field when
    it finishes, so pass `selector` to press Enter in a field you just filled."""
    type: Literal["press"]
    key: str = Field(
        ...,
        description="Enter, Tab, Escape, Backspace, Delete, Space, ArrowUp/Down/"
        "Left/Right, Home, End, PageUp, PageDown, or a single character.",
    )
    selector: Optional[str] = Field(default=None, min_length=1, description=_SELECTOR_DESC)
    text: Optional[str] = Field(default=None, description=_TEXT_DESC)
    timeout_s: float = Field(default=15.0, gt=0, le=600, description=_TIMEOUT_DESC)


class SelectAction(_ActionModel):
    """Choose an option of a native <select> by value (or, failing that, by
    visible text) and fire input + change. Not for lightning-combobox — click
    it open and click the option instead."""
    type: Literal["select"]
    selector: str = Field(..., min_length=1, description=_SELECTOR_DESC)
    text: Optional[str] = Field(default=None, description=_TEXT_DESC)
    value: str
    timeout_s: float = Field(default=15.0, gt=0, le=600, description=_TIMEOUT_DESC)


class WaitAction(_ActionModel):
    """Sleep (cancellable)."""
    type: Literal["wait"]
    seconds: float = Field(..., ge=0, le=600)


class EvaluateAction(_ActionModel):
    """Run JS in the page and return its (JSON-serialisable) value. The script
    is evaluated as an EXPRESSION — wrap multi-statement code in an async IIFE:
    `(async () => { …; return x; })()`. Promises are awaited."""
    type: Literal["evaluate"]
    script: str = Field(..., min_length=1)
    timeout_s: float = Field(default=15.0, gt=0, le=600)


ScreenshotFormat = Literal["jpeg", "png", "webp"]


class ScreenshotAction(_ActionModel):
    """Capture an image of the page as it is at this point in the run — after
    a best-effort settle (~5 s) for a step that navigated. The image lands on
    this step's `actions_report` entry as `value` ({format, mime_type, width,
    height, full_page, bytes, page_url, data_base64}). A failed screenshot is
    recorded (ok=false) and the run continues."""
    type: Literal["screenshot"]
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
    timeout_s: float = Field(
        default=30.0, gt=0, le=600,
        description="Budget for the whole step — settle, layout read and the "
        "capture itself (a tall full-page clip can take several seconds).",
    )


class ScrollAction(_ActionModel):
    """Set the scroll position on whichever container actually scrolls — the
    document, or (Salesforce Lightning) an inner `overflow-y: auto` element,
    shadow DOM included. Three forms, at least one of selector / to / by:
    `selector` alone frames that element (`block`, default `start` = at the
    top); `to` jumps to the top or bottom; `by` nudges by CSS px. With a
    `selector`, `to` / `by` act on that element's scroll container. `click`
    and `fill` centre their element, so a `scroll` right before a
    `screenshot` frames it. Nothing to scroll is a no-op, not a failure.
    `value`: {target ('window' | '<div class=…>' | null), element?,
    scroll_top, scroll_height, client_height, at_bottom}."""
    type: Literal["scroll"]
    selector: Optional[str] = Field(default=None, min_length=1, description=_SELECTOR_DESC)
    text: Optional[str] = Field(default=None, description=_TEXT_DESC + " Needs `selector`.")
    to: Optional[Literal["top", "bottom"]] = Field(
        default=None,
        description="Jump to the top or bottom of the scroll container. "
        "Not with `by`.",
    )
    by: Optional[int] = Field(
        default=None, ge=-100000, le=100000, strict=True,
        description="Scroll by this many CSS px (negative is up). Not with `to`.",
    )
    block: Optional[Literal["start", "center", "end", "nearest"]] = Field(
        default=None,
        description="Selector-only form: where the element lands in the "
        "viewport. Default `start` (top of the frame).",
    )
    timeout_s: float = Field(
        default=15.0, gt=0, le=600,
        description=_TIMEOUT_DESC + " Bounds only the element lookup.",
    )

    @model_validator(mode="after")
    def _one_form(self) -> "ScrollAction":
        if self.text is not None and self.selector is None:
            raise ValueError("text needs a selector — it narrows the selector's matches")
        if self.selector is None and self.to is None and self.by is None:
            raise ValueError("give selector, to or by")
        if self.to is not None and self.by is not None:
            raise ValueError("to and by are mutually exclusive — give one")
        if self.block is not None and (self.to is not None or self.by is not None):
            raise ValueError("block only applies to the selector-only form (no to / by)")
        return self


ActionModel = Annotated[
    Union[WaitForAction, FillAction, ClickAction, PressAction, SelectAction,
          WaitAction, EvaluateAction, ScreenshotAction, ScrollAction],
    Field(discriminator="type"),
]

# login_actions: the same steps minus `evaluate` — a script on the login page
# could read the password field back into the report — and minus `screenshot`:
# an image of the login form would put the username into the response, and
# the login design keeps credentials out of every response. `scroll` stays:
# it reads nothing, and a sign-in button below the fold is a real case.
LoginActionModel = Annotated[
    Union[WaitForAction, FillAction, ClickAction, PressAction, SelectAction,
          WaitAction, ScrollAction],
    Field(discriminator="type"),
]
LOGIN_ACTION_TYPES = ("wait_for", "fill", "click", "press", "select", "wait", "scroll")


class ActionResultModel(BaseModel):
    index: int = Field(description="Position in `actions`; -1 for `page_script`.")
    type: str
    ok: bool
    elapsed_ms: int
    error: Optional[str] = Field(
        default=None,
        description="Why the step failed; 'skipped' when an earlier step "
        "failed (or the page never became ready) so it never ran.",
    )
    value: Any = Field(
        default=None,
        description="evaluate / page_script: the script's return value. DOM "
        "steps: details of the element acted on. screenshot: {format, "
        "mime_type, width, height, full_page, bytes, page_url, data_base64} "
        "(page_url is where the tab was at that step). scroll: {target, "
        "element?, scroll_top, scroll_height, client_height, at_bottom}.",
    )


class ActionsReportModel(BaseModel):
    page_script: Optional[ActionResultModel] = None
    actions: list[ActionResultModel] = Field(default_factory=list)
    aborted_reason: Optional[str] = Field(
        default=None,
        description="null when every step ran (a failed screenshot step does "
        "not stop the run, so check each step's `ok`). Otherwise e.g. "
        "'not ready: login page …', 'cancelled', 'actions[2] (click) failed: …', "
        "'capture window ended before actions finished'.",
    )


_LOGIN_ACTIONS_DESC = (
    "Steps (wait_for / fill / click / press / select / wait / scroll — no "
    "evaluate or screenshot) run "
    "ONLY if the tab lands on a login wall (`login_url_patterns`), once per "
    "capture, to sign in; the session then continues to `url` as it would "
    "after a human login. A fill `value` may reference the profile's stored "
    "credentials as `${username}` / `${password}` (`$$` = a literal $) — they "
    "are resolved server-side from INTERCEPTOR_LOGINS_DIR/<profile>.json, and "
    "a fill only types into a page whose origin is in that file's "
    "`allowed_origins`. `${` anywhere else is a 400. `capture_window_seconds` "
    "must cover the login, the SSO redirect AND the target page load. See "
    "INTERCEPTOR.md § Login actions."
)
_LOGIN_TIMEOUT_DESC = (
    "Upper bound on one login_actions run — the login page becoming ready plus "
    "every step (default 60). It runs inside capture_window_seconds, so the "
    "window must also be long enough for the login, the SSO redirect and the "
    "target page load."
)


class CaptureRequest(BaseModel):
    url: str = Field(..., description="URL to navigate to")
    url_patterns: list[str] = Field(
        default_factory=list,
        description="Regex patterns matched against every JSON XHR/fetch URL. "
        "Any capture whose URL matches at least one pattern is returned, "
        "bucketed by the first pattern that matched it. May be empty ONLY "
        "when `actions` has a `screenshot` step (a screenshot-only capture, "
        "whose window ends as soon as the actions finish).",
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
    page_script: Optional[str] = Field(
        default=None,
        description="JS evaluated in the page once it is ready (before any "
        "`actions`). An EXPRESSION — use an async IIFE for multi-statement "
        "code; promises are awaited and the JSON-serialisable result comes "
        "back as `actions_report.page_script.value`.",
    )
    actions: list[ActionModel] = Field(
        default_factory=list,
        description="Ordered browser steps (wait_for / fill / click / press / "
        "select / wait / evaluate / screenshot / scroll) run once the page is "
        "ready, so the page's own code fires the requests `url_patterns` is "
        "waiting for. Stops at the first failed step — except a failed "
        "`screenshot`, which is recorded and the run continues. At most "
        f"{MAX_SCREENSHOT_ACTIONS} screenshot steps. `click` / `fill` centre "
        "their element, so a `scroll` right before a `screenshot` frames it. "
        "See INTERCEPTOR.md § Page scripts and actions and § Screenshots.",
    )
    actions_ready_timeout_seconds: Optional[int] = Field(
        default=None,
        ge=1,
        le=600,
        description="How long the readiness gate (tab off about:blank, not on "
        "a login URL, document complete, capture hook installed) may wait "
        "before the steps are abandoned. Defaults to capture_window_seconds.",
    )
    stop_when_matched: bool = Field(
        default=False,
        description="End the capture window as soon as every `url_patterns` "
        "bucket has at least one match (and the actions, if any, have "
        "finished) instead of waiting it out. Requires url_patterns.",
    )
    login_actions: list[LoginActionModel] = Field(
        default_factory=list,
        description=_LOGIN_ACTIONS_DESC,
    )
    login_actions_timeout_seconds: Optional[int] = Field(
        default=None,
        ge=1,
        le=600,
        description=_LOGIN_TIMEOUT_DESC,
    )

    @model_validator(mode="after")
    def _patterns_or_screenshot(self) -> "CaptureRequest":
        shots = sum(1 for a in self.actions if a.type == "screenshot")
        if not self.url_patterns and not shots:
            raise ValueError(
                "url_patterns must contain at least one pattern unless `actions` "
                'has a screenshot step ({"type": "screenshot"}) — a screenshot-only capture'
            )
        if shots > MAX_SCREENSHOT_ACTIONS:
            raise ValueError(
                f"at most {MAX_SCREENSHOT_ACTIONS} screenshot steps per request "
                f"(got {shots}) — each image is returned in the response"
            )
        if self.stop_when_matched and not self.url_patterns:
            raise ValueError("stop_when_matched needs at least one url_pattern to match")
        if self.login_actions and not self.login_url_patterns:
            raise ValueError(
                "login_actions run only when a login wall is detected — they need "
                "login_url_patterns (an empty list disables detection)"
            )
        return self


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
    actions_report: Optional[ActionsReportModel] = Field(
        default=None,
        description="What page_script / actions did — null unless requested. "
        "Screenshot images are on their steps' `value`.",
    )
    login_actions_report: Optional[ActionsReportModel] = Field(
        default=None,
        description="What login_actions did — null unless they were sent AND "
        "the capture hit a login wall. A fill reports only `value_length`.",
    )
    ended_early: bool = Field(
        default=False,
        description="True only when the window ended before "
        "capture_window_seconds elapsed: stop_when_matched saw every pattern "
        "matched, or — with no url_patterns — the actions finished.",
    )


# ── Health + profile endpoints ──────────────────────────────────────────────
@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


def _with_login_info(entry: dict) -> dict:
    """Add the reference names and allowed origins of the profile's logins
    file (never a value), so a caller can see which ``${…}`` its
    ``login_actions`` may use."""
    info = logins.describe(entry["name"])
    out = {**entry, "login_keys": info["keys"], "login_origins": info["allowed_origins"]}
    if "error" in info:
        out["login_error"] = info["error"]
    return out


def _profiles_listing() -> dict:
    return {
        "root": profiles.PROFILES_ROOT,
        "profiles": [_with_login_info(p) for p in profiles.list_profiles()],
    }


@app.get("/profiles")
def profiles_list() -> dict:
    return _profiles_listing()


@app.get("/profiles/{name}")
def profiles_get(name: str) -> dict:
    try:
        return _with_login_info(profiles.profile_info(name))
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

    # Browser steps: pydantic has already checked each step's shape; the
    # library re-validates what it alone knows (press key names, ranges).
    # Still before the port, so a bad step never costs a slot.
    try:
        lib_actions = parse_actions([a.model_dump() for a in req.actions])
    except ActionError as e:
        raise HTTPException(status_code=400, detail=f"invalid action: {e}")
    want_actions = bool(lib_actions) or req.page_script is not None

    # Login steps: same library validation (no evaluate), then the profile's
    # logins file and the ${key} substitution. Resolved by profile NAME, so
    # the fast path and a slow-path clone use the same credentials. Every
    # error names a step or a key, never a value — and still no port taken.
    lib_login_actions: list[Action] = []
    login_origins: tuple[str, ...] = ()
    if req.login_actions:
        try:
            parsed_login = parse_actions(
                [a.model_dump() for a in req.login_actions],
                label="login_actions",
                allowed_types=LOGIN_ACTION_TYPES,
            )
        except ActionError as e:
            raise HTTPException(status_code=400, detail=f"invalid login_actions: {e}")
        try:
            logins.referenced_keys(parsed_login)
            login_cfg = logins.load(req.profile)
            lib_login_actions = logins.resolve(parsed_login, login_cfg)
        except logins.LoginConfigError as e:
            raise HTTPException(status_code=400, detail=str(e))
        login_origins = login_cfg.allowed_origins

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
            login_actions=lib_login_actions,
            login_fill_origins=login_origins,
            login_actions_timeout_s=float(
                req.login_actions_timeout_seconds or DEFAULT_LOGIN_ACTIONS_TIMEOUT_SECONDS
            ),
            login_lock=_get_login_lock(req.profile) if lib_login_actions else None,
        )

        try:
            client.launch(target_url=req.url)
        except BrowserNotFoundError as e:
            raise HTTPException(status_code=500, detail=str(e))

        # ── Actions (optional) ─────────────────────────────────────────────
        # Run on a helper thread so this thread keeps owning the window
        # timing. They ride their own CDP socket and their readiness gate
        # waits for the worker to navigate + install interceptor.js, so it is
        # safe to start them the moment Chrome is launched. ``window_over``
        # doubles as their cancel signal once the window closes.
        window_over = threading.Event()
        actions_box: dict[str, ActionsReport] = {}
        actions_thread: Optional[threading.Thread] = None
        if want_actions:
            job.update_metadata(actions_done=0, actions_total=len(lib_actions))

            def _actions_worker() -> None:
                job.set_phase("actions")
                try:
                    actions_box["report"] = client.run_actions(
                        lib_actions,
                        page_script=req.page_script,
                        ready_timeout_s=float(
                            req.actions_ready_timeout_seconds or req.capture_window_seconds
                        ),
                        login_url_patterns=req.login_url_patterns,
                        cancel=lambda: job.is_cancelled() or window_over.is_set(),
                        on_progress=lambda done, total: job.update_metadata(
                            actions_done=done, actions_total=total
                        ),
                    )
                finally:
                    if not (job.is_cancelled() or window_over.is_set()):
                        job.set_phase("capturing")

            actions_thread = threading.Thread(
                target=_actions_worker, name=f"actions-{job.job_id}", daemon=True
            )
            actions_thread.start()

        # ── Capture window ─────────────────────────────────────────────────
        # Short-poll instead of one long wait so the window can end on cancel,
        # on the deadline, or — with stop_when_matched — as soon as the
        # actions are done and every pattern bucket has a match. With no
        # patterns at all (a screenshot-only capture) there is nothing to
        # wait for once the actions are done: all([]) is True, so the same
        # check ends the window then.
        deadline = time.monotonic() + req.capture_window_seconds
        cancelled = False
        ended_early = False
        while True:
            if job.is_cancelled():
                cancelled = True
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if (req.stop_when_matched or not compiled) and (
                actions_thread is None or not actions_thread.is_alive()
            ):
                with results_lock:
                    all_matched = all(matches[p] for p, _ in compiled)
                if all_matched:
                    ended_early = True
                    job.log(
                        "stop_when_matched — every pattern matched, ending the window early"
                        if compiled else
                        "no url_patterns — actions finished, ending the window early"
                    )
                    break
            job.wait_or_cancel(timeout=min(0.25, remaining))
        window_over.set()

        actions_report: Optional[ActionsReportModel] = None
        if actions_thread is not None:
            # A step mid-RPC can take up to its own CDP timeout to notice the
            # cancel; don't let a wedged one hold the slot past that.
            actions_thread.join(timeout=12)
            report = actions_box.get("report")
            if report is None:
                report = ActionsReport.not_run(
                    lib_actions, req.page_script,
                    "actions still running when the capture window ended (abandoned)",
                )
            elif report.aborted_reason == "cancelled" and not job.is_cancelled():
                report.aborted_reason = "capture window ended before actions finished"
            actions_report = ActionsReportModel.model_validate(report.to_dict())
            failed = next((r for r in report.actions if not r.ok), None)
            # Counts only — a screenshot's base64 never goes near the log.
            shots = [r for r in report.actions
                     if r.type == "screenshot" and r.error != "skipped"]
            shots_ok = sum(1 for r in shots if r.ok)
            job.log(
                f"actions  ran={sum(1 for r in report.actions if r.error != 'skipped')}"
                f"/{len(report.actions)}  aborted={report.aborted_reason or '-'}"
                + (f"  first_failure=[{failed.index}] {failed.type}" if failed else "")
                + (f"  screenshots={shots_ok} taken, {len(shots) - shots_ok} failed"
                   if shots else "")
            )

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

        # Read AFTER quit: the login steps run on the session worker, and
        # quit() is what stops (and briefly joins) it. A run still going is a
        # placeholder report saying so (and so is a keep_open one).
        login_actions_report: Optional[ActionsReportModel] = None
        if lib_login_actions:
            login_rep = client.get_login_report()
            if login_rep is not None:
                if login_rep.aborted_reason == "cancelled" and not job.is_cancelled():
                    login_rep.aborted_reason = "capture window ended before login_actions finished"
                login_actions_report = ActionsReportModel.model_validate(login_rep.to_dict())
                failed = next((r for r in login_rep.actions if not r.ok), None)
                job.log(
                    f"login_actions ok={login_rep.ok} failed_at="
                    + (f"{failed.index}/{failed.type}" if failed else "-")
                )

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
            f"ended_early={ended_early}"
        )

        return CaptureResponse(
            job_id=job.job_id,
            url=req.url,
            status=status,
            login_wall=login_wall,
            error=state.error,
            matches=matches_snapshot,
            captured_urls=urls_snapshot,
            actions_report=actions_report,
            login_actions_report=login_actions_report,
            ended_early=ended_early,
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
#
# Screenshots: each screenshot step's image is delivered as an MCP
# ``ImageContent`` block (so a multimodal model can look at it), preceded by a
# short text label naming the step, in step order after the JSON payload. The
# JSON keeps each step's image metadata — the base64 is NOT duplicated into
# the text.


def _tool_result(payload: dict) -> ToolResult:
    """Build the MCP result for a ``CaptureResponse.model_dump()`` (or the
    error payload shaped like one).

    Every screenshot step in ``actions_report.actions`` that carries
    ``data_base64`` has it pulled out of the JSON and emitted as a
    ``TextContent`` label (``actions[3] screenshot — <page_url>, WxH``)
    followed by an ``ImageContent`` block, in step order after the JSON.
    """
    images: list = []
    report = payload.get("actions_report")
    if isinstance(report, dict) and isinstance(report.get("actions"), list):
        import base64 as _b64

        steps = []
        for step in report["actions"]:
            shot = step.get("value")
            if step.get("type") == "screenshot" and isinstance(shot, dict) and shot.get("data_base64"):
                raw = _b64.b64decode(shot["data_base64"])
                images.append(TextContent(
                    type="text",
                    text=f"actions[{step.get('index')}] screenshot — {shot.get('page_url') or '?'}, "
                    f"{shot.get('width')}x{shot.get('height')}",
                ))
                images.append(McpImage(data=raw, format=shot.get("format", "jpeg")).to_image_content())
                step = {**step, "value": {k: v for k, v in shot.items() if k != "data_base64"}}
            steps.append(step)
        payload = {**payload, "actions_report": {**report, "actions": steps}}
    content: list = [TextContent(type="text", text=json.dumps(payload, default=str)), *images]
    return ToolResult(content=content, structured_content=payload)


def _http_error_text(e: Exception) -> str:
    if isinstance(e, HTTPException):
        return f"HTTP {e.status_code}: {e.detail}"
    if isinstance(e, ValidationError):
        # Include the field path — for a bad step it reads
        # "actions.1.fill.value: Field required", which says which step.
        return "invalid request: " + "; ".join(
            (".".join(str(p) for p in err.get("loc", ())) + ": " if err.get("loc") else "")
            + err.get("msg", str(err))
            for err in e.errors()
        )
    return f"{type(e).__name__}: {e}"


def _login_patterns_kw(patterns: Optional[list[str]]) -> dict:
    """``login_url_patterns`` for a request model: omitted when the tool caller
    didn't pass any, so the model's defaults apply (an explicit ``[]`` still
    disables detection)."""
    return {} if patterns is None else {"login_url_patterns": patterns}


@mcp.tool()
def capture_url(
    url: str,
    profile: str,
    url_patterns: Optional[list[str]] = None,
    capture_window_seconds: int = DEFAULT_CAPTURE_WINDOW_SECONDS,
    login_timeout: int = 300,
    max_matches_per_pattern: Optional[int] = None,
    page_script: Optional[str] = None,
    actions: Optional[list[dict]] = None,
    stop_when_matched: bool = False,
    login_url_patterns: Optional[list[str]] = None,
    actions_ready_timeout_seconds: Optional[int] = None,
    login_actions: Optional[list[dict]] = None,
) -> ToolResult:
    """Load a URL under a named Chrome profile and return JSON XHR/fetch bodies
    whose URLs match any of the given regex patterns — optionally after
    driving the page (fill a form, click a button) so it fires the request you
    want, and optionally with screenshots of the page taken between steps.

    To just SEE a page, call it with no ``url_patterns`` and
    ``actions=[{"type": "screenshot", "scale": 0.5}]`` — it returns as soon
    as the image is taken.

    Args:
        url: Fully-qualified URL to navigate to (https://…).
        profile: Named profile under ``INTERCEPTOR_PROFILES_ROOT``. Discover
            available names via the ``list_profiles`` tool. Profiles are
            uploaded out-of-band by an operator (see INTERCEPTOR.md).
        url_patterns: List of regex patterns. Each intercepted XHR/fetch URL
            is matched with ``re.search`` against every pattern; the response
            body lands in the bucket of the first pattern it matches. May be
            omitted only when ``actions`` has a ``screenshot`` step — then the
            call ends as soon as the actions finish.
        capture_window_seconds: How long to run Chrome to collect captures
            (default 20). Increase if the page fires XHRs late.
        login_timeout: Max seconds to wait for a login redirect to resolve
            before returning ``login_wall: true``.
        max_matches_per_pattern: Cap on how many bodies to return per pattern.
        page_script: JavaScript run in the page once it has loaded, before
            any ``actions``. It is evaluated as an EXPRESSION; for several
            statements use an async IIFE, ``(async () => { ...; return x;
            })()``. Promises are awaited; the JSON-serialisable result comes
            back as ``actions_report.page_script.value``. Use it to inspect
            the DOM (e.g. list the form fields before writing ``actions``).
            Do NOT use it to call the site's API with ``fetch()`` — sites that
            sign their requests (CSRF tokens) refuse hand-made calls; drive
            the form with ``actions`` instead.
        actions: Ordered steps run after the page is ready (off about:blank,
            not on a login URL, fully loaded, capture hook installed). Each is
            an object with a ``type``:
            ``{"type": "wait_for", "selector", "text"?, "state"?: "visible"|"attached", "timeout_s"?}``,
            ``{"type": "fill", "selector", "value", "text"?, "clear"?: true, "timeout_s"?}``,
            ``{"type": "click", "selector", "text"?, "method"?: "mouse"|"js", "timeout_s"?}``,
            ``{"type": "press", "key": "Enter"|"Tab"|"Escape"|…|<one char>, "selector"?, "text"?}``,
            ``{"type": "select", "selector", "value", "text"?}`` (native <select> only),
            ``{"type": "wait", "seconds"}``,
            ``{"type": "evaluate", "script", "timeout_s"?}``,
            ``{"type": "screenshot", "format"?: "jpeg"|"png"|"webp", "quality"?: 1-100, "full_page"?: false, "scale"?: 0-2, "max_height"?: 100-16384, "timeout_s"?: 30}``,
            ``{"type": "scroll", "selector"?, "text"?, "to"?: "top"|"bottom", "by"?: <px>, "block"?: "start"|"center"|"end"|"nearest"}`` (at least one of selector / to / by; to and by exclusive; block only with selector alone).
            ``selector`` is CSS matched in the document AND inside every open
            shadow root (Salesforce Lightning / web components), each tree
            separately — so a descendant combinator never crosses into a
            component (``lightning-input input`` finds nothing; use
            ``lightning-input`` or ``input[name=x]``). First visible match
            wins; ``text`` narrows it by visible text / value /
            aria-label / placeholder / title (substring, or ``/regex/flags``).
            ``fill`` types into the real <input> inside a component host with
            trusted keyboard input; ``click`` sends a real mouse click at the
            element's centre. ``timeout_s`` defaults to 15. Steps stop at the
            first failure — read ``actions_report`` to see which and why.
            ``screenshot`` captures the page as it is at that point (after a
            short settle if the previous step navigated); put one after a
            step to see what it did. Each comes back as an image block, in
            step order, labelled ``actions[i] screenshot — <page_url>, WxH``,
            and its metadata ({format, mime_type, width, height, full_page,
            bytes, page_url}) is the step's ``value``. A failed screenshot is
            recorded (``ok: false``) and the steps after it still run. At most
            10 per call; prefer ``"scale": 0.5`` — it quarters the image and
            is plenty to read a page. ``full_page`` captures the whole
            document (up to ``max_height`` px) instead of the 1920x1080
            viewport.
            ``scroll`` sets where the page is scrolled: ``selector`` alone
            puts that element at the top of the frame (``block``); ``to`` /
            ``by`` (CSS px, negative is up) move the page's main scroller —
            the document, or the inner container a Salesforce Lightning page
            actually scrolls — or, with a ``selector``, that element's scroll
            container. ``click`` and ``fill`` centre their element, so a
            ``scroll`` right before a ``screenshot`` frames it. Nothing to
            scroll is a no-op (``target: null``), not a failure; the
            ``value`` reports ``scroll_top`` / ``scroll_height`` /
            ``at_bottom``.
        stop_when_matched: End as soon as every pattern has at least one
            match and the actions have finished, instead of waiting out
            ``capture_window_seconds``. A click finishes when the mouse is
            released, not when its request completes — if the page also makes
            matching calls on its own, end ``actions`` with a step that waits
            for the response you want — a ``wait_for`` on the element that
            shows the result, or an ``evaluate`` that polls
            ``window._capturedResponses`` for a new entry (INTERCEPTOR.md §
            Worked example) — plus a short ``wait`` so it reaches the service.
        login_url_patterns: Regexes ``re.search``-matched against the tab URL
            after navigation to spot a redirect to a login page. Omit for the
            defaults (``login``, ``signin``, ``/auth``); a list REPLACES them,
            so include them yourself if you still want them. Add the site's
            SSO host when it doesn't match those (Enphase:
            ``sso\\.enphaseenergy\\.com``) — otherwise an expired session
            looks like an empty result instead of ``login_wall: true``. An
            empty list disables login detection.
        actions_ready_timeout_seconds: How long the actions wait for the
            page to be ready (loaded, off any login page) before giving up.
            Defaults to ``capture_window_seconds``.
        login_actions: Steps that run ONLY if the page redirects to a login
            wall (``login_url_patterns``), to sign in with the profile's
            stored service account; the capture then continues to ``url``.
            Same step objects as ``actions`` except ``evaluate`` and
            ``screenshot``. For the
            credentials write REFERENCES — ``"value": "${username}"`` and
            ``"value": "${password}"`` — which the server fills in from its
            own credentials file; ``list_profiles`` shows the reference names
            (``login_keys``) each profile has. NEVER ask a user for a real
            username or password and never put one in a step. Enphase:
            ``login_url_patterns=["login", "signin", "/auth",
            "sso\\.enphaseenergy\\.com"]``, ``login_actions=[{"type": "fill",
            "selector": "#username", "value": "${username}"},
            {"type": "fill", "selector": "#password", "value":
            "${password}"}, {"type": "click", "selector":
            "input[type=submit].button"}]``. Raise
            ``capture_window_seconds`` (~120) so the login, the redirect and
            the page load all fit; the result says what happened in
            ``login_actions_report``.

    Returns:
        JSON with keys ``job_id``, ``url``, ``status``, ``login_wall``,
        ``error``, ``matches`` (pattern → list of {url, body}),
        ``captured_urls`` (every JSON XHR/fetch URL seen, for diagnostics),
        ``actions_report`` ({page_script, actions: [{index, type, ok,
        elapsed_ms, error, value}], aborted_reason}, or null),
        ``login_actions_report`` (same shape; null unless ``login_actions``
        were sent and a login wall was hit) and ``ended_early`` — followed by
        a text label + image block per screenshot step that succeeded.
    """
    try:
        req = CaptureRequest(
            url=url,
            url_patterns=url_patterns or [],
            profile=profile,
            capture_window_seconds=capture_window_seconds,
            keep_open=False,
            login_timeout=login_timeout,
            max_matches_per_pattern=max_matches_per_pattern,
            debug_logging=False,
            page_script=page_script,
            actions=actions or [],
            stop_when_matched=stop_when_matched,
            actions_ready_timeout_seconds=actions_ready_timeout_seconds,
            login_actions=login_actions or [],
            **_login_patterns_kw(login_url_patterns),
        )
        return _tool_result(_run_capture(req).model_dump())
    except (HTTPException, ValidationError) as e:
        return _tool_result(
            {
                "job_id": "",
                "url": url,
                "status": "error",
                "login_wall": False,
                "error": _http_error_text(e),
                "matches": {p: [] for p in url_patterns or []},
                "captured_urls": [],
                "actions_report": None,
                "login_actions_report": None,
                "ended_early": False,
            }
        )


@mcp.tool()
def list_profiles() -> dict:
    """List every named Chrome profile currently uploaded to interceptor.

    Use this before ``capture_url`` to see which ``profile`` values are valid.
    Only profiles with ``sentinel_present: true`` are usable — a false value
    means the profile hasn't been through the operator's upload flow and
    Chrome would fail to launch headless.

    ``login_keys`` lists the credential reference names a profile's
    ``login_actions`` may use (``${username}``, ``${password}``, …) and
    ``login_origins`` the sites they may be typed into; both are empty when
    the profile has no stored login. The values themselves are never shown.

    Returns:
        A dict with ``root`` (the profiles directory path) and ``profiles``
        (a list of ``{name, path, present, size_bytes, sentinel_present,
        login_keys, login_origins}`` objects, one per named profile;
        ``login_error`` is added when the profile's logins file is unusable).
    """
    return _profiles_listing()


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
