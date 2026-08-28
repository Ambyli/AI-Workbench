"""sandbox-runner — FastAPI + MCP front-end for the sandbox spawner.

Gives OpenWebUI a way to ask "run this code and give me a URL I can
iframe". The URL is served by sandbox-proxy (Caddy) on ai_shared and
routes to a short-lived sandbox-{id} container on sandbox_net. The
network segmentation is what keeps model-generated code from reaching
litellm, phoenix-mcp, roofix-db, or anything else on the stack.

See ai/sandbox/SANDBOX.md for the operator guide and the security-invariant
checklist that must be re-verified after any change here.

Endpoints:
    GET  /health                healthcheck
    POST /run                   spawn a sandbox, return its URL
    GET  /jobs                  all managed sandboxes + phase
    GET  /jobs/{id}             one sandbox detail
    DELETE /jobs/{id}           tear down a sandbox early
    /mcp                        FastMCP HTTP transport — preview_app tool
    /tool/*                     OpenWebUI Tool Server sub-app:
        GET  /tool/openapi.json      OpenAPI spec for OpenWebUI discovery
        POST /tool/preview_app       spawn + return HTMLResponse iframe
                                     with `Content-Disposition: inline`
                                     for OpenWebUI rich-UI rendering
"""

from __future__ import annotations

# Load .env before anything reads os.environ.
from common.env import load_env

load_env()

import asyncio
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, field_validator

from common.jobs.postgres import PostgresRegistry
from common.jobs.router import build_router

# Import name is `sandbox_mcp`, not `mcp`, because `ai/sandbox/` is on
# sys.path and a module literally named `mcp` would shadow the `mcp`
# PyPI package that FastMCP itself imports internally (from mcp.types
# import ...) — that manifests as a confusing "FastMCP server support
# is not installed" ImportError at startup.
from sandbox_mcp import build_mcp, _IFRAME_HEIGHT_CSS
from reaper import Reaper
from runtimes import RUNTIMES, get_runtime
from spawner import Spawner


# ── Config from env ───────────────────────────────────────────────────────
MAX_CONCURRENT = int(os.environ.get("SANDBOX_MAX_CONCURRENT", "8"))
DEFAULT_TTL_S = int(os.environ.get("SANDBOX_DEFAULT_TTL_SECONDS", "900"))
HARD_TTL_S = int(os.environ.get("SANDBOX_HARD_TTL_SECONDS", "3600"))
PROXY_URL = os.environ.get("SANDBOX_PROXY_URL", "http://sandbox-proxy")

# Sessions are the durable identity of a preview across turns. Regex is
# both a validation surface (reject anything that could path-inject when
# a caller later uses the id in a URL or filesystem context) and a hint
# to the model that the id is a short opaque string, not free text.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _new_session_id() -> str:
    """URL-safe 12-char session id. Enough entropy to avoid collisions
    across concurrent chats without dragging a UUID through log lines."""
    return secrets.token_urlsafe(9)  # 9 bytes → 12 base64 chars


def _build_dsn() -> str:
    """Assemble the Postgres DSN from the env parts.

    Kept as a function so a caller can override with a fully-formed
    SANDBOX_DB_DSN env var when convenient (e.g. testing against a remote
    Postgres) without touching the individual parts.
    """
    if "SANDBOX_DB_DSN" in os.environ:
        return os.environ["SANDBOX_DB_DSN"]
    return (
        "postgresql://"
        f"{os.environ.get('SANDBOX_DB_USER', 'sandbox')}:"
        f"{os.environ.get('SANDBOX_DB_PASSWORD', 'sandbox')}@"
        f"{os.environ.get('SANDBOX_DB_HOST', 'sandbox-db')}:"
        f"{os.environ.get('SANDBOX_DB_PORT', '5432')}/"
        f"{os.environ.get('SANDBOX_DB_NAME', 'sandbox')}"
    )


# Global singletons — initialized in lifespan().
_registry: Optional[PostgresRegistry] = None
_spawner: Optional[Spawner] = None
_reaper: Optional[Reaper] = None
# Simple semaphore for concurrency capping; the runner also relies on
# Docker to reject a container-create if resources are exhausted, but this
# semaphore gives us a clean 429 without paying a docker round-trip first.
_slot_sem: Optional[asyncio.Semaphore] = None


# ── MCP server + ASGI app ─────────────────────────────────────────────────
# Build here (module scope, before the FastAPI construction) so we can
# compose the MCP app's lifespan into ours. The tool implementation
# (_mcp_run below) references module globals that are populated in the
# FastAPI lifespan — that's fine because it's called lazily.
async def _mcp_run(
    runtime: str,
    files: dict[str, str],
    entrypoint: Optional[str],
    ttl_seconds: Optional[int],
    session_id: Optional[str],
    deletes: list[str],
) -> dict:
    return await _reuse_or_spawn(
        runtime, files, entrypoint, ttl_seconds, session_id, deletes
    )


_mcp = build_mcp(_mcp_run)
# path="/" so FastMCP puts its transport at the mount root — mounting
# the returned ASGI app at /mcp then gives clients a single POST /mcp/
# endpoint. Without this, FastMCP defaults to /mcp/ inside the mounted
# app, so the full URL becomes /mcp/mcp/ and every client (including
# LiteLLM) hits 404 on POST /mcp/.
_mcp_app = _mcp.http_app(path="/")


@asynccontextmanager
async def lifespan(app_: FastAPI):
    global _registry, _spawner, _reaper, _slot_sem
    _registry = PostgresRegistry(_build_dsn())
    await _registry.init()
    await _ensure_session_index(_registry)
    _spawner = Spawner()
    _slot_sem = asyncio.Semaphore(MAX_CONCURRENT)
    _reaper = Reaper(_spawner, _registry, _slot_sem)
    _reaper.start()
    # Mount the jobs router now that the registry is live. Delete is
    # handled by our own endpoint below (needs to stop the container),
    # so we don't ask build_router to add its own.
    app_.include_router(
        build_router(_registry, include_delete=False)
    )
    # Compose the MCP app's lifespan — FastMCP's streamable-HTTP
    # transport initializes session state in its lifespan; skipping this
    # means POST /mcp/ succeeds at the routing layer but throws inside
    # the tool handler.
    async with _mcp_app.lifespan(app_):
        try:
            yield
        finally:
            if _reaper is not None:
                await _reaper.stop()
            if _registry is not None:
                await _registry.close()


app = FastAPI(title="sandbox-runner", lifespan=lifespan)


# ── Request / response models ─────────────────────────────────────────────
class RunRequest(BaseModel):
    runtime: str = Field(description="One of the keys in runtimes.RUNTIMES")
    files: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Path → content map. On the first call for a session this is "
            "the initial file set. On a follow-up call (same session_id) "
            "it is an overlay — paths given here overwrite files in the "
            "running container, unlisted files are left alone."
        ),
    )
    entrypoint: Optional[str] = Field(
        default=None,
        description="Shell command inside the sandbox; must bind to port 80",
    )
    ttl_seconds: Optional[int] = Field(
        default=None,
        description="Idle TTL. Server clamps to SANDBOX_HARD_TTL_SECONDS.",
    )
    session_id: Optional[str] = Field(
        default=None,
        description=(
            "Persistent identifier for a preview across turns. Omit on "
            "the first call; the server generates one and returns it. "
            "Pass the same value back on follow-up calls to update files "
            "in place — no respawn, same URL, dev server hot-reloads."
        ),
    )
    deletes: list[str] = Field(
        default_factory=list,
        description=(
            "Relative paths (under /app) to delete on a follow-up call. "
            "Ignored on the first call. Absolute paths and '..' are "
            "rejected."
        ),
    )

    @field_validator("session_id")
    @classmethod
    def _validate_session_id(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not _SESSION_ID_RE.match(v):
            raise ValueError(
                "session_id must match ^[A-Za-z0-9_-]{1,64}$"
            )
        return v


class RunResponse(BaseModel):
    sandbox_id: str
    session_id: str
    url: str
    expires_at: str
    reused: bool = False


# ── Endpoints ─────────────────────────────────────────────────────────────
@app.get("/health")
async def health() -> dict:
    # Report degraded if the DB is unreachable so the compose healthcheck
    # can catch a partial outage. The DB check is a cheap round-trip.
    ok_db = False
    if _registry is not None:
        try:
            await _registry.list_all(limit=1)
            ok_db = True
        except Exception:
            ok_db = False
    return {
        "status": "ok" if ok_db else "degraded",
        "db": "up" if ok_db else "down",
    }


async def _ensure_session_index(registry: PostgresRegistry) -> None:
    """Install the functional index that backs session lookup.

    Kept here rather than in ``common.jobs.PostgresRegistry`` because it
    is sandbox-specific — other consumers of the shared registry (e.g.
    interceptor) don't have a session concept and shouldn't pay for the
    index. Idempotent — ``IF NOT EXISTS`` means safe on every startup.

    The predicate ``WHERE phase = 'running'`` keeps the index tiny: only
    the handful of live sandboxes are indexed, not every historical job
    row. That's the exact shape the reuse lookup queries."""
    pool = registry._pool
    if pool is None:
        return
    async with pool.acquire() as conn:
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS jobs_session_id_running "
            "ON jobs ((metadata->>'session_id')) "
            "WHERE phase = 'running'"
        )


async def _find_running_session(session_id: str) -> Optional[dict]:
    """Return the newest running sandbox for a session, if any.

    Reads directly through the registry's asyncpg pool because
    PostgresRegistry doesn't expose metadata-filtered lookup — adding
    that method would leak sandbox concepts into the shared library.

    Note the two-column read: ``session_id`` + ``expires_at`` live in
    ``metadata`` (set at ``register`` time), but ``container_name`` +
    ``url`` live in ``result`` (set at ``set_result`` time). Both are
    populated by the time a job's phase reaches 'running', so a session
    lookup is safe to trust."""
    if _registry is None:
        return None
    pool = _registry._pool
    if pool is None:
        return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, metadata, result "
            "FROM jobs "
            "WHERE metadata->>'session_id' = $1 AND phase = 'running' "
            "ORDER BY created_at DESC LIMIT 1",
            session_id,
        )
    if row is None:
        return None
    meta = row["metadata"]
    if isinstance(meta, str):
        import json
        meta = json.loads(meta)
    result = row["result"] or {}
    if isinstance(result, str):
        import json
        result = json.loads(result)
    return {
        "sandbox_id": row["id"],
        "container_name": result.get("container_name"),
        "url": result.get("url"),
        "expires_at": meta.get("expires_at"),
    }


async def _reuse_or_spawn(
    runtime: str,
    files: dict[str, str],
    entrypoint: Optional[str],
    ttl_seconds: Optional[int],
    session_id: Optional[str],
    deletes: list[str],
) -> dict:
    """Session-aware entry point. Called from POST /run, the MCP tool,
    and the OpenWebUI Tool Server route.

    If ``session_id`` names a still-running sandbox, files are overlaid
    onto that container's ``/app`` and the same URL is returned — the
    dev server inside reloads itself. Otherwise this falls through to a
    fresh spawn, stamping the session_id (generated if the caller
    omitted one) into the job's metadata so the next call can find it."""
    if _registry is None or _spawner is None or _slot_sem is None:
        raise HTTPException(500, "runner not initialized")

    # Session-reuse path — cheap, short-circuits before we touch the
    # concurrency semaphore or the runtime validator.
    if session_id:
        existing = await _find_running_session(session_id)
        if existing and existing["container_name"]:
            alive = await asyncio.to_thread(
                _spawner.container_exists, existing["container_name"]
            )
            if alive:
                try:
                    await asyncio.to_thread(
                        _spawner.update_files,
                        existing["container_name"],
                        files,
                        deletes,
                    )
                except ValueError as exc:
                    # Path traversal etc. — surface as a 400, not a 500.
                    raise HTTPException(400, str(exc))
                # Bump last_used_at so the reaper's idle-TTL check sees
                # this activity. The rest of the metadata is untouched.
                await _registry.update_metadata(
                    existing["sandbox_id"],
                    {"last_used_at": _now_iso()},
                )
                return {
                    "sandbox_id": existing["sandbox_id"],
                    "session_id": session_id,
                    "url": existing["url"],
                    "expires_at": existing["expires_at"],
                    "reused": True,
                }
            # Container is gone but the Postgres row still says "running"
            # — normal when the reaper hasn't swept yet, or the sandbox
            # crashed out-of-band. Mark it expired and fall through to
            # respawn under the same session_id.
            await _registry.set_phase(existing["sandbox_id"], "expired")

    # Spawn path — either no session_id given, or the session_id had no
    # live container. Generate one if missing so the caller always gets
    # a stable handle back.
    session_id = session_id or _new_session_id()

    try:
        rt = get_runtime(runtime)
    except KeyError:
        raise HTTPException(
            400,
            f"unknown runtime {runtime!r}. Valid: {sorted(RUNTIMES)}. "
            "Call list_runtimes for full descriptions.",
        )

    # Reject an entrypoint on a runtime that doesn't allow one. This is
    # a targeted guard against a real failure mode we've seen: a model
    # picks runtime=static and sets entrypoint="python3 -m http.server
    # 80", nginx never starts, the readiness probe times out at 30s,
    # and the caller gets an opaque 504. Failing upfront with a
    # specific message gives the model a corrective signal on retry.
    if not rt.allows_custom_entrypoint and entrypoint:
        raise HTTPException(
            400,
            f"the {runtime!r} runtime does not accept a custom entrypoint "
            f"(it uses a fixed process: {rt.default_entrypoint!r}). "
            "Remove the `entrypoint` field, or switch to runtime=python or "
            "runtime=node if you need to run a specific command.",
        )

    # Fast-fail if the pool is full. wait_for with a short timeout works
    # because asyncio.Semaphore.acquire returns immediately when a slot is
    # available; TimeoutError only happens when the pool is genuinely full.
    try:
        await asyncio.wait_for(_slot_sem.acquire(), timeout=0.1)
    except asyncio.TimeoutError:
        raise HTTPException(429, "sandbox pool exhausted")

    ttl = min(ttl_seconds or DEFAULT_TTL_S, HARD_TTL_S)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl)
    now_iso = _now_iso()

    sandbox_id = await _registry.register(
        {
            "runtime": runtime,
            "entrypoint": entrypoint or rt.default_entrypoint,
            "ttl_seconds": ttl,
            "expires_at": expires_at.isoformat(),
            "session_id": session_id,
            "last_used_at": now_iso,
        },
        initial_phase="spawning",
    )

    try:
        result = await asyncio.to_thread(
            _spawner.spawn, sandbox_id, rt, files, entrypoint
        )
        await _registry.set_phase(sandbox_id, "starting")
        ready = await asyncio.to_thread(
            _spawner.readiness_ok,
            result.container_name,
            rt.internal_port,
            rt.readiness_probe_path,
            30.0,  # readiness deadline — enough for pip/npm install-on-boot
        )
        if not ready:
            await asyncio.to_thread(_spawner.stop, result.container_name)
            await _registry.set_error(
                sandbox_id, "sandbox did not become ready within 30s"
            )
            raise HTTPException(504, "sandbox did not become ready")

        url = f"{PROXY_URL}/{sandbox_id}/"
        await _registry.set_result(
            sandbox_id,
            {"url": url, "container_name": result.container_name},
            phase="running",
        )
        return {
            "sandbox_id": sandbox_id,
            "session_id": session_id,
            "url": url,
            "expires_at": expires_at.isoformat(),
            "reused": False,
        }
    except HTTPException:
        _slot_sem.release()
        raise
    except Exception as exc:
        await _registry.set_error(sandbox_id, str(exc))
        _slot_sem.release()
        raise HTTPException(500, f"spawn failed: {exc}")
    # Slot is intentionally NOT released on success — it's released when
    # the reaper (or DELETE /jobs/{id}) tears the sandbox down.


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@app.post("/run", response_model=RunResponse)
async def run(req: RunRequest) -> RunResponse:
    result = await _reuse_or_spawn(
        req.runtime,
        req.files,
        req.entrypoint,
        req.ttl_seconds,
        req.session_id,
        req.deletes,
    )
    return RunResponse(**result)


@app.delete("/session/{session_id}", status_code=204)
async def delete_session(session_id: str) -> None:
    """Explicitly tear down a session's running sandbox. Idempotent —
    a session that never existed or has already expired still returns
    204 so the model doesn't have to remember state to clean up."""
    if not _SESSION_ID_RE.match(session_id):
        raise HTTPException(400, "invalid session_id")
    if _registry is None or _spawner is None or _slot_sem is None:
        raise HTTPException(500, "runner not initialized")
    existing = await _find_running_session(session_id)
    if existing is None:
        return
    if existing["container_name"]:
        await asyncio.to_thread(
            _spawner.stop, existing["container_name"]
        )
    await _registry.set_phase(existing["sandbox_id"], "closed")
    # The slot was held for the running sandbox — give it back.
    _slot_sem.release()


# Jobs GET routes are mounted from lifespan(). DELETE lives here so it can
# stop the container in addition to purging the row — build_router's built-in
# DELETE only touches the DB.
@app.delete("/jobs/{sandbox_id}", status_code=204)
async def delete_sandbox(sandbox_id: str) -> None:
    if _registry is None or _spawner is None or _slot_sem is None:
        raise HTTPException(500, "runner not initialized")
    got = await _registry.get(sandbox_id)
    if got is None:
        raise HTTPException(404, f"no sandbox {sandbox_id!r}")
    await asyncio.to_thread(_spawner.stop, f"sandbox-{sandbox_id}")
    await _registry.delete(sandbox_id)
    # Give the slot back to the pool if the sandbox was still running.
    if got.phase in ("running", "starting", "spawning"):
        _slot_sem.release()


# ── MCP mount ─────────────────────────────────────────────────────────────
# The MCP ASGI app is built at module import (see the `_mcp_app` block
# near the top of this file so its lifespan can be composed into
# FastAPI's). We only need to attach it to the router here.
app.mount("/mcp", _mcp_app)


# ── OpenWebUI Tool Server sub-app ─────────────────────────────────────────
# Per the OpenWebUI docs (Extensibility → Plugin Development → Rich UI),
# a Tool Server is a REST service that publishes an OpenAPI schema and
# returns responses with `Content-Disposition: inline` on paths that
# should render as embedded iframes in the chat.
#
# We mount a full FastAPI sub-app at /tool so:
#   * OpenAPI is exposed at GET /tool/openapi.json (the sub-app's root)
#     — that's the URL you point Admin Panel → Settings → Tools →
#     Add Connection at (base URL: http://sandbox-runner:8000/tool).
#   * Each POST /tool/<action> becomes one tool in OpenWebUI's picker.
#   * The response uses fastapi.responses.HTMLResponse with the required
#     header + the CORS-expose header that lets a browser-side OpenWebUI
#     read the Content-Disposition value.
#
# For the LiteLLM-MCP path (mcp_servers.sandbox), the model receives the
# same HTML block as text and passes it through — see sandbox_mcp.py.
tool_app = FastAPI(
    title="sandbox-runner tools",
    description=(
        "OpenWebUI Tool Server exposing the sandbox runner. Register in "
        "Admin Panel → Settings → Tools → Add Connection with base URL "
        "http://sandbox-runner:8000/tool. See ai/sandbox/ENDPOINTS.md."
    ),
    version="0.1.0",
)

# CORSMiddleware is needed for the OpenWebUI browser to read the
# Content-Disposition header — the docs call this out explicitly.
# We keep origins permissive because access control lives at the
# network / oauth2-proxy layer, not here.
tool_app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)


class ToolPreviewAppRequest(BaseModel):
    runtime: str = Field(
        description=(
            "One of 'static', 'python', or 'node'. Matches POST /run's "
            "runtime field."
        )
    )
    files: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Map of relative path → file contents. On a follow-up call "
            "with the same session_id, only send the file(s) that "
            "changed — the rest are preserved."
        ),
    )
    entrypoint: Optional[str] = Field(
        default=None,
        description=(
            "Shell command run inside the sandbox. Must bind to port 80. "
            "Leave unset to use the runtime's default."
        ),
    )
    ttl_seconds: Optional[int] = Field(
        default=None,
        description=(
            "Idle lifetime in seconds. Server clamps to "
            "SANDBOX_HARD_TTL_SECONDS."
        ),
    )
    session_id: Optional[str] = Field(
        default=None,
        description=(
            "Reuse the URL from a previous preview_app call. Omit on "
            "first call. Pass the value from the previous response to "
            "update files in place (dev server hot-reloads)."
        ),
    )
    deletes: list[str] = Field(
        default_factory=list,
        description=(
            "Relative paths under /app to remove on a follow-up call."
        ),
    )


def _render_tool_html(url: str, sandbox_id: str, session_id: str) -> str:
    """Wrap the sandbox iframe in a full HTML document.

    Compared to just ``<iframe src="…">``, this:
      * Ships as a complete document so OpenWebUI's outer sandboxed
        iframe treats it as a real page (some sanitizers strip a bare
        <iframe> tag but leave a document's body iframe alone).
      * Renders the URL as a visible link fallback so the user can
        open the preview in a new tab even if the nested iframe is
        blocked by the outer sandbox attribute (this is the failure
        mode where "the rendered element is blank" — we've hit it).
      * Sets ``sandbox`` on the nested iframe explicitly so the browser
        gives the sandbox app the permissions it needs (scripts, forms,
        popups) rather than inheriting the parent's restrictive default.
    """
    return f"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>sandbox preview</title></head>
<body style="margin:0;font-family:system-ui;background:#0e1116;color:#e6edf3">
  <iframe
      src="{url}"
      style="width:100%;height:{_IFRAME_HEIGHT_CSS};border:0;display:block;background:#0e1116"
      sandbox="allow-scripts allow-forms allow-popups allow-same-origin allow-downloads"
      allow="clipboard-read; clipboard-write"
      loading="lazy"
      referrerpolicy="no-referrer"></iframe>
  <p style="padding:8px 12px;margin:0;font-size:12px;opacity:0.75">
    Sandbox <code>{sandbox_id}</code> &middot;
    Session <code>{session_id}</code> &middot;
    <a href="{url}" target="_top" style="color:#8ab4f8">Open in new tab</a>
  </p>
</body>
</html>"""


@tool_app.post(
    "/preview_app",
    response_class=HTMLResponse,
    operation_id="preview_app",  # OpenWebUI uses this as the tool name.
    summary="Spawn a sandbox and render its preview inline",
    description=(
        "Spawns a sandbox container from the supplied files + entrypoint "
        "and returns an HTML page containing an iframe pointing at it. "
        "The response uses Content-Disposition: inline, which OpenWebUI "
        "recognizes as a rich-UI embed. See "
        "https://docs.openwebui.com/features/extensibility/plugin/"
        "development/rich-ui/. If the model has not seen this deployment "
        "before, call `list_runtimes` first to see which runtimes are "
        "available."
    ),
)
async def tool_preview_app(req: ToolPreviewAppRequest) -> HTMLResponse:
    # Session-id validation runs at the pydantic layer for RunRequest but
    # ToolPreviewAppRequest doesn't share that validator — reject invalid
    # ids here so path-injection attempts don't leak through this route.
    if req.session_id and not _SESSION_ID_RE.match(req.session_id):
        raise HTTPException(400, "invalid session_id")
    result = await _reuse_or_spawn(
        req.runtime,
        req.files,
        req.entrypoint,
        req.ttl_seconds,
        req.session_id,
        req.deletes,
    )
    return HTMLResponse(
        content=_render_tool_html(
            result["url"], result["sandbox_id"], result["session_id"]
        ),
        headers={
            "Content-Disposition": "inline",
            # Re-declared here even though CORSMiddleware sets it — some
            # OpenWebUI versions look at the response headers directly
            # rather than the CORS preflight; belt + suspenders.
            "Access-Control-Expose-Headers": "Content-Disposition",
        },
    )


@tool_app.get(
    "/list_runtimes",
    operation_id="list_runtimes",  # OpenWebUI uses this as the tool name.
    summary="Describe the runtimes available on this sandbox deployment",
    description=(
        "Returns metadata for each runtime: summary, default entrypoint, "
        "pre-baked packages, and a minimal example files map. Call this "
        "BEFORE `preview_app` if you're unsure which runtime fits the "
        "user's request or which packages are already installed. It "
        "requires no arguments and does not spawn anything."
    ),
)
async def tool_list_runtimes() -> list[dict]:
    from runtimes import describe_runtimes as _dr  # lazy: avoid cycle at import
    return _dr()


app.mount("/tool", tool_app)
