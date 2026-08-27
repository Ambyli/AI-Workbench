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
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from common.jobs.postgres import PostgresRegistry
from common.jobs.router import build_router

# Import name is `sandbox_mcp`, not `mcp`, because `ai/sandbox/` is on
# sys.path and a module literally named `mcp` would shadow the `mcp`
# PyPI package that FastMCP itself imports internally (from mcp.types
# import ...) — that manifests as a confusing "FastMCP server support
# is not installed" ImportError at startup.
from sandbox_mcp import build_mcp, _render_html_block
from reaper import Reaper
from runtimes import RUNTIMES, get_runtime
from spawner import Spawner


# ── Config from env ───────────────────────────────────────────────────────
MAX_CONCURRENT = int(os.environ.get("SANDBOX_MAX_CONCURRENT", "8"))
DEFAULT_TTL_S = int(os.environ.get("SANDBOX_DEFAULT_TTL_SECONDS", "900"))
HARD_TTL_S = int(os.environ.get("SANDBOX_HARD_TTL_SECONDS", "3600"))
PROXY_URL = os.environ.get("SANDBOX_PROXY_URL", "http://sandbox-proxy")


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
) -> dict:
    return await _spawn_and_track(runtime, files, entrypoint, ttl_seconds)


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
    files: dict[str, str] = Field(description="Path → content map")
    entrypoint: Optional[str] = Field(
        default=None,
        description="Shell command inside the sandbox; must bind to port 80",
    )
    ttl_seconds: Optional[int] = Field(
        default=None,
        description="Idle TTL. Server clamps to SANDBOX_HARD_TTL_SECONDS.",
    )


class RunResponse(BaseModel):
    sandbox_id: str
    url: str
    expires_at: str


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


async def _spawn_and_track(
    runtime: str,
    files: dict[str, str],
    entrypoint: Optional[str],
    ttl_seconds: Optional[int],
) -> dict:
    """Core spawn path. Called from both POST /run and the MCP tool."""
    if _registry is None or _spawner is None or _slot_sem is None:
        raise HTTPException(500, "runner not initialized")

    try:
        rt = get_runtime(runtime)
    except KeyError:
        raise HTTPException(
            400,
            f"unknown runtime {runtime!r}. Valid: {sorted(RUNTIMES)}",
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

    sandbox_id = await _registry.register(
        {
            "runtime": runtime,
            "entrypoint": entrypoint or rt.default_entrypoint,
            "ttl_seconds": ttl,
            "expires_at": expires_at.isoformat(),
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
            "url": url,
            "expires_at": expires_at.isoformat(),
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


@app.post("/run", response_model=RunResponse)
async def run(req: RunRequest) -> RunResponse:
    result = await _spawn_and_track(
        req.runtime, req.files, req.entrypoint, req.ttl_seconds
    )
    return RunResponse(**result)


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
        description="Map of relative path → file contents.",
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


@tool_app.post(
    "/preview_app",
    response_class=HTMLResponse,
    summary="Spawn a sandbox and return an iframe that renders inline in chat",
    description=(
        "Spawns a sandbox container from the supplied files + entrypoint "
        "and returns an HTML iframe pointing at it. The response uses "
        "Content-Disposition: inline, which OpenWebUI recognizes as a "
        "rich-UI embed and renders as a sandboxed iframe under the tool "
        "call. See https://docs.openwebui.com/features/extensibility/"
        "plugin/development/rich-ui/."
    ),
)
async def tool_preview_app(req: ToolPreviewAppRequest) -> HTMLResponse:
    result = await _spawn_and_track(
        req.runtime, req.files, req.entrypoint, req.ttl_seconds
    )
    return HTMLResponse(
        content=_render_html_block(result["url"]),
        headers={
            "Content-Disposition": "inline",
            # Re-declared here even though CORSMiddleware sets it — some
            # OpenWebUI versions look at the response headers directly
            # rather than the CORS preflight; belt + suspenders.
            "Access-Control-Expose-Headers": "Content-Disposition",
        },
    )


app.mount("/tool", tool_app)
