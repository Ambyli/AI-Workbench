"""OpenWebUI Tool Server sub-app, mounted by ``app.py`` at ``/tool``.

Publishes an OpenAPI schema OpenWebUI's Admin Panel can register against
(Base URL: http://sandbox-runner:8000/tool). Each ``POST /tool/<action>``
becomes one tool in OpenWebUI's picker; responses set
``Content-Disposition: inline`` so OpenWebUI's rich-UI embed renders
the returned iframe HTML directly.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from models import SESSION_ID_RE, ToolRunRequest
from operations import _reuse_or_spawn
from sandbox_mcp import render_preview_html


log = logging.getLogger("sandbox-runner.tool_server")


# ── OpenWebUI Tool Server sub-app ─────────────────────────────────────────
tool_app = FastAPI(
    title="sandbox-runner tools",
    description=(
        "OpenWebUI Tool Server exposing the sandbox runner. Register in "
        "Admin Panel → Settings → Tools → Add Connection with base URL "
        "http://sandbox-runner:8000/tool. See ai/sandbox/ENDPOINTS.md."
    ),
    version="0.2.0",
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


@tool_app.post(
    "/run",
    response_class=HTMLResponse,
    operation_id="run",
    summary="Spawn a sandbox and render its preview inline",
    description=(
        "Spawns (or updates) a sandbox container and returns an HTML page "
        "containing an iframe pointing at it. Response uses "
        "Content-Disposition: inline for OpenWebUI's rich-UI embed."
    ),
)
async def tool_run(req: ToolRunRequest) -> HTMLResponse:
    if req.session_id and not SESSION_ID_RE.match(req.session_id):
        raise HTTPException(400, "invalid session_id")
    result = await _reuse_or_spawn(
        req.runtime,
        req.files,
        req.entrypoint,
        req.ttl_seconds,
        req.session_id,
        req.deletes,
        env=req.env,
        recreate_if_gone=True,
    )
    return HTMLResponse(
        content=render_preview_html(
            result["url"], result["sandbox_id"], result["session_id"]
        ),
        headers={
            "Content-Disposition": "inline",
            "Access-Control-Expose-Headers": "Content-Disposition",
        },
    )


@tool_app.post(
    "/preview_app",
    response_class=HTMLResponse,
    operation_id="preview_app",
    summary="Deprecated alias for /tool/run",
    description=(
        "DEPRECATED — use /tool/run. Kept for one release cycle so "
        "existing OpenWebUI Tool Server registrations don't break "
        "mid-rollout. Behaves identically to /tool/run."
    ),
    deprecated=True,
)
async def tool_preview_app(req: ToolRunRequest) -> HTMLResponse:
    log.info("Tool Server: /tool/preview_app called (deprecated alias)")
    return await tool_run(req)


@tool_app.get(
    "/get_runtimes",
    operation_id="get_runtimes",
    summary="Describe available runtimes",
    description=(
        "Returns metadata for each runtime: summary, default entrypoint, "
        "pre-baked packages, and a minimal example files map."
    ),
)
async def tool_get_runtimes() -> list[dict]:
    from runtimes import describe_runtimes as _dr
    return _dr()


# Deprecated alias for the previous name.
@tool_app.get(
    "/list_runtimes",
    operation_id="list_runtimes",
    summary="Deprecated alias for /tool/get_runtimes",
    deprecated=True,
)
async def tool_list_runtimes() -> list[dict]:
    return await tool_get_runtimes()
