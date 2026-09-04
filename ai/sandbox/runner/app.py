"""sandbox-runner — FastAPI + MCP front-end for the sandbox spawner.

Gives OpenWebUI a way to ask "run this code and give me a URL I can
iframe". The URL is served by sandbox-proxy (Caddy) on ai_shared and
routes to a short-lived sandbox-{id} container on sandbox_net. The
network segmentation is what keeps model-generated code from reaching
litellm, phoenix-mcp, roofix-db, or anything else on the stack.

See ai/sandbox/SANDBOX.md for the operator guide and the security-invariant
checklist that must be re-verified after any change here.

## Module layout

This file is now orchestration-only: config, lifespan, endpoint
decorators, mounts. Business logic lives in sibling modules — see
``SANDBOX.md § Runner file layout`` for the split:

    state.py       — module-level singletons (registry, spawner, ...)
    models.py      — pydantic request/response models + validators
    operations.py  — _do_* core operations, shared by HTTP + MCP
    tool_server.py — OpenWebUI Tool Server sub-app (mounted at /tool)
    sandbox_mcp.py — FastMCP tool surface (mounted at /mcp)
    spawner.py     — Docker interactions
    reaper.py      — TTL sweeper
    runtimes.py    — runtime registry (static/python/node)

MCP tool surface (all under the ``sandbox`` server):

    get_runtime_types()             — describe available runtime types (catalog)
    create(runtime, env?, ...)      — warm an empty container, returns session
    write_files(session_id, files, deletes?, recreate_if_gone?)
                                    — overlay files, health-probe after
    get_files(session_id, paths?)   — read files back from /app
    get_logs(session_id, lines?)    — tail container stdout+stderr
    exec(session_id, command, ...)  — run non-interactive shell command
    patch_files(session_id, patches)— strict line-range edits, all-or-nothing
    preview(session_id)             — return the iframe artifact HTML
    close(session_id)               — teardown and release slot
    list_sessions()                 — enumerate live sandboxes globally
    run(runtime, files, ...)        — convenience: create + update + preview

HTTP endpoints:

    GET  /health                    healthcheck
    POST /run                       spawn a sandbox, return its URL
    GET  /jobs                      all managed sandboxes + phase
    GET  /jobs/{id}                 one sandbox detail
    DELETE /jobs/{id}               tear down a sandbox early
    GET  /sessions                  list live sessions (list_sessions backing)
    POST /session/{id}/files        write_files overlay (JSON)
    GET  /session/{id}/files        list files under /app or read specified paths
    POST /session/{id}/exec         run a shell command in the container
    POST /session/{id}/patch        strict line-range file edits (patch_files)
    /mcp                            FastMCP HTTP transport
    /tool/*                         OpenWebUI Tool Server sub-app:
        GET  /tool/openapi.json         OpenAPI spec for OpenWebUI discovery
        GET  /tool/get_runtime_types    describe runtime types
        POST /tool/run                  spawn + return HTMLResponse iframe
"""

from __future__ import annotations

# Load .env before anything reads os.environ.
from common.env import load_env

load_env()

import asyncio
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from common.jobs.postgres import PostgresRegistry
from common.jobs.router import build_router
from common.logging_setup import setup_logging

import state
from constants import (
    BROWSER_LOG_MAX_BODY_BYTES,
    DEBUG_LOGGING,
    DEFAULT_TTL_S,
    EXEC_DEFAULT_TIMEOUT_S,
    GET_FILES_DEFAULT_BYTES,
    HARD_TTL_S,
    LOG_DIR,
    MAX_CONCURRENT,
    MAX_FILE_BYTES,
    MAX_PAYLOAD_BYTES,
    PROXY_URL,
    SESSION_ID_RE,
)
from models import (
    CreateRequest,
    ExecRequest,
    PatchFilesRequest,
    RunRequest,
    RunResponse,
    WriteFilesRequest,
)
from operations import (
    _do_browser_log_ingest,
    _do_close,
    _do_create,
    _do_exec,
    _do_get_files,
    _do_list_sessions,
    _do_patch_files,
    _do_preview,
    _do_write_files,
    _find_running_session,
    _reuse_or_spawn,
)
from reaper import Reaper
# Import name is `sandbox_mcp`, not `mcp`, because `ai/sandbox/` is on
# sys.path and a module literally named `mcp` would shadow the `mcp`
# PyPI package that FastMCP itself imports internally (from mcp.types
# import ...) — that manifests as a confusing "FastMCP server support
# is not installed" ImportError at startup.
from sandbox_mcp import build_mcp
from spawner import Spawner
from tool_server import tool_app


# All config constants live in ``constants.py``; imported above.
# ``load_env()`` ran before that import so env-derived constants
# picked up the ``.env`` values, not the defaults.

# Configure the root logger for every runner module. `setup_logging` writes
# to /data/sandbox-runner.log (file) and stderr (console) with the shared
# common format `%(asctime)s [%(levelname)s] %(funcName)s: %(message)s`.
# DEBUG_LOGGING=true lifts both channels to DEBUG so operators can trace
# request flow through spawn/reap/self-heal; the default INFO/WARNING
# levels keep the file useful under load without drowning stderr.
setup_logging("sandbox-runner", log_dir=LOG_DIR, debug=DEBUG_LOGGING)
log = logging.getLogger("sandbox-runner.app")
log.info(
    "runner boot config: MAX_CONCURRENT=%d DEFAULT_TTL_S=%d HARD_TTL_S=%d "
    "PROXY_URL=%s DEBUG_LOGGING=%s LOG_DIR=%s MAX_FILE=%d MAX_PAYLOAD=%d",
    MAX_CONCURRENT, DEFAULT_TTL_S, HARD_TTL_S, PROXY_URL, DEBUG_LOGGING,
    LOG_DIR, MAX_FILE_BYTES, MAX_PAYLOAD_BYTES,
)


def _build_dsn() -> str:
    """Assemble the Postgres DSN from the env parts.

    Kept as a function so a caller can override with a fully-formed
    SANDBOX_DB_DSN env var when convenient (e.g. testing against a remote
    Postgres) without touching the individual parts.
    """
    if "SANDBOX_DB_DSN" in os.environ:
        log.debug("using SANDBOX_DB_DSN env var")
        return os.environ["SANDBOX_DB_DSN"]
    host = os.environ.get('SANDBOX_DB_HOST', 'sandbox-db')
    port = os.environ.get('SANDBOX_DB_PORT', '5432')
    name = os.environ.get('SANDBOX_DB_NAME', 'sandbox')
    log.debug("DSN assembled from parts: host=%s port=%s db=%s", host, port, name)
    return (
        "postgresql://"
        f"{os.environ.get('SANDBOX_DB_USER', 'sandbox')}:"
        f"{os.environ.get('SANDBOX_DB_PASSWORD', 'sandbox')}@"
        f"{host}:{port}/{name}"
    )


# ── MCP server + ASGI app ─────────────────────────────────────────────────
# Build here (module scope, before the FastAPI construction) so we can
# compose the MCP app's lifespan into ours. The tool implementations
# reference state module globals that are populated in the FastAPI
# lifespan — that's fine because they're called lazily.
async def _mcp_run(
    runtime: str,
    files: dict,
    entrypoint: Optional[str],
    ttl_seconds: Optional[int],
    session_id: Optional[str],
    deletes: list[str],
    env: Optional[dict[str, str]] = None,
    recreate_if_gone: bool = True,
) -> dict:
    """MCP-facing bridge to ``_reuse_or_spawn``. All fields already
    validated by the tool wrapper, so this is a thin passthrough."""
    log.debug(
        "MCP run: runtime=%s session=%s n_files=%d n_deletes=%d "
        "entrypoint=%r ttl=%s recreate_if_gone=%s env_keys=%s",
        runtime, session_id, len(files or {}), len(deletes or []),
        entrypoint, ttl_seconds, recreate_if_gone,
        sorted((env or {}).keys()),
    )
    return await _reuse_or_spawn(
        runtime, files, entrypoint, ttl_seconds, session_id, deletes,
        env=env, recreate_if_gone=recreate_if_gone,
    )


async def _mcp_create(
    runtime: str,
    ttl_seconds: Optional[int],
    entrypoint: Optional[str],
    env: Optional[dict[str, str]],
) -> dict:
    log.debug(
        "MCP create: runtime=%s ttl=%s entrypoint=%r env_keys=%s",
        runtime, ttl_seconds, entrypoint, sorted((env or {}).keys()),
    )
    return await _do_create(runtime, ttl_seconds, entrypoint, env)


async def _mcp_write_files(
    session_id: str,
    files: dict,
    deletes: list[str],
    recreate_if_gone: bool,
) -> dict:
    log.debug(
        "MCP write_files: session=%s n_files=%d n_deletes=%d recreate=%s",
        session_id, len(files or {}), len(deletes or []), recreate_if_gone,
    )
    return await _do_write_files(session_id, files, deletes, recreate_if_gone)


async def _mcp_get_files(
    session_id: str,
    paths: Optional[list[str]],
    max_bytes_per_file: int,
) -> dict:
    log.debug(
        "MCP get_files: session=%s paths=%s max_bytes=%d",
        session_id, paths, max_bytes_per_file,
    )
    return await _do_get_files(session_id, paths, max_bytes_per_file)


async def _mcp_get_logs(session_id: str, lines: int) -> dict:
    log.debug("MCP get_logs: session=%s lines=%d", session_id, lines)
    return await logs_session(session_id, lines)


async def _mcp_exec(
    session_id: str,
    command: str,
    timeout_seconds: int,
    working_dir: str,
) -> dict:
    log.debug(
        "MCP exec: session=%s cmd=%r timeout=%d workdir=%s",
        session_id, command, timeout_seconds, working_dir,
    )
    return await _do_exec(session_id, command, timeout_seconds, working_dir)


async def _mcp_patch_files(
    session_id: str,
    patches: list[dict],
    recreate_if_gone: bool,
) -> dict:
    log.debug(
        "MCP patch_files: session=%s n_patches=%d recreate=%s",
        session_id, len(patches or []), recreate_if_gone,
    )
    return await _do_patch_files(session_id, patches, recreate_if_gone)


async def _mcp_preview(session_id: str) -> dict:
    log.debug("MCP preview: session=%s", session_id)
    return await _do_preview(session_id)


async def _mcp_close(session_id: str) -> dict:
    log.debug("MCP close: session=%s", session_id)
    return await _do_close(session_id)


async def _mcp_list_sessions() -> dict:
    log.debug("MCP list_sessions")
    return await _do_list_sessions()


_mcp = build_mcp(
    run_callable=_mcp_run,
    logs_callable=_mcp_get_logs,
    create_callable=_mcp_create,
    write_files_callable=_mcp_write_files,
    get_files_callable=_mcp_get_files,
    exec_callable=_mcp_exec,
    patch_files_callable=_mcp_patch_files,
    preview_callable=_mcp_preview,
    close_callable=_mcp_close,
    list_sessions_callable=_mcp_list_sessions,
)
# path="/" so FastMCP puts its transport at the mount root — mounting
# the returned ASGI app at /mcp then gives clients a single POST /mcp/
# endpoint. Without this, FastMCP defaults to /mcp/ inside the mounted
# app, so the full URL becomes /mcp/mcp/ and every client (including
# LiteLLM) hits 404 on POST /mcp/.
_mcp_app = _mcp.http_app(path="/")


@asynccontextmanager
async def lifespan(app_: FastAPI):
    log.info("lifespan startup: begin")
    state.registry = PostgresRegistry(_build_dsn())
    await state.registry.init()
    log.info("Postgres registry initialized")
    await _ensure_session_index(state.registry)
    state.spawner = Spawner()
    log.info("Docker spawner initialized")
    state.slot_sem = asyncio.Semaphore(MAX_CONCURRENT)
    log.info("concurrency semaphore initialized: MAX_CONCURRENT=%d", MAX_CONCURRENT)
    state.reaper = Reaper(state.spawner, state.registry, state.slot_sem)
    state.reaper.start()
    log.info("reaper started")
    # Mount the jobs router now that the registry is live. Delete is
    # handled by our own endpoint below (needs to stop the container),
    # so we don't ask build_router to add its own.
    app_.include_router(
        build_router(state.registry, include_delete=False)
    )
    log.info("jobs router mounted (delete=False)")
    # Compose the MCP app's lifespan — FastMCP's streamable-HTTP
    # transport initializes session state in its lifespan; skipping this
    # means POST /mcp/ succeeds at the routing layer but throws inside
    # the tool handler.
    async with _mcp_app.lifespan(app_):
        log.info("lifespan startup: complete, ready to serve")
        try:
            yield
        finally:
            log.info("lifespan shutdown: begin")
            if state.reaper is not None:
                await state.reaper.stop()
                log.info("reaper stopped")
            if state.registry is not None:
                await state.registry.close()
                log.info("Postgres registry closed")
            log.info("lifespan shutdown: complete")


app = FastAPI(title="sandbox-runner", lifespan=lifespan)


# ── Endpoints ─────────────────────────────────────────────────────────────
@app.get("/health")
async def health() -> dict:
    # Report degraded if the DB is unreachable so the compose healthcheck
    # can catch a partial outage. The DB check is a cheap round-trip.
    ok_db = False
    if state.registry is not None:
        try:
            await state.registry.list_all(limit=1)
            ok_db = True
        except Exception as exc:
            log.warning("health: DB probe failed: %s", exc)
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
        log.warning("_ensure_session_index: pool not initialized, skipping")
        return
    async with pool.acquire() as conn:
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS jobs_session_id_running "
            "ON jobs ((metadata->>'session_id')) "
            "WHERE phase = 'running'"
        )
    log.info("session lookup index ensured (jobs_session_id_running)")


@app.post("/run", response_model=RunResponse)
async def run(req: RunRequest) -> RunResponse:
    log.debug(
        "POST /run: runtime=%s session_id=%s n_files=%d n_deletes=%d",
        req.runtime, req.session_id, len(req.files or {}),
        len(req.deletes or []),
    )
    result = await _reuse_or_spawn(
        req.runtime,
        req.files,
        req.entrypoint,
        req.ttl_seconds,
        req.session_id,
        req.deletes,
        env=req.env,
        recreate_if_gone=req.recreate_if_gone,
    )
    return RunResponse(**result)


@app.post("/create", response_model=RunResponse)
async def create(req: CreateRequest) -> RunResponse:
    """Warm an empty container. Mirrors the ``create`` MCP tool."""
    result = await _do_create(req.runtime, req.ttl_seconds, req.entrypoint, req.env)
    return RunResponse(**result)


@app.get("/sessions")
async def sessions_list() -> dict:
    return await _do_list_sessions()


@app.post("/session/{session_id}/files", response_model=RunResponse)
async def session_write_files(
    session_id: str, req: WriteFilesRequest,
) -> RunResponse:
    result = await _do_write_files(
        session_id, req.files, req.deletes, req.recreate_if_gone,
    )
    return RunResponse(**result)


@app.get("/session/{session_id}/files")
async def session_read_files(
    session_id: str,
    paths: Optional[str] = None,
    max_bytes_per_file: int = GET_FILES_DEFAULT_BYTES,
) -> dict:
    """Read files back from the running sandbox's ``/app``.

    ``paths`` is a comma-separated list of relative paths. Omit it to
    get a directory listing (paths + sizes only, no contents)."""
    path_list: Optional[list[str]] = None
    if paths:
        path_list = [p.strip() for p in paths.split(",") if p.strip()]
    return await _do_get_files(session_id, path_list, max_bytes_per_file)


@app.post("/session/{session_id}/exec")
async def session_exec(session_id: str, req: ExecRequest) -> dict:
    return await _do_exec(
        session_id,
        req.command,
        req.timeout_seconds or EXEC_DEFAULT_TIMEOUT_S,
        req.working_dir or "/app",
    )


@app.post("/session/{session_id}/patch")
async def session_patch(session_id: str, req: PatchFilesRequest) -> dict:
    """Strict line-range file edits. All-or-nothing across the batch.

    Every patch must pass byte-for-byte validation against the current
    file content — see ENDPOINTS.md § POST /session/{id}/patch for the
    full behavior including the mismatch response shape.
    """
    patches_dicts = [p.model_dump() for p in req.patches]
    return await _do_patch_files(
        session_id, patches_dicts, req.recreate_if_gone,
    )


# ── Browser log ingest (internal) ────────────────────────────────────────
# Called by the browser shim (see ai/sandbox/proxies/browser_shim.js)
# via sandbox-proxy. NOT exposed publicly — only reachable through the
# Caddy `_browser_log` route which pins on a 12-hex sandbox id.
#
# Design points captured here (not the docstring so LSPs don't dump
# the whole design into hover tooltips):
#   * Returns 204 regardless of success — the browser doesn't retry and
#     surfacing a failure would only turn a debugging feature into a
#     source of network-tab noise for end users.
#   * Fire-and-forget: we spawn the ingest as an asyncio task so the
#     endpoint returns immediately. Rate-limit accounting is synchronous
#     inside _do_browser_log_ingest, so nothing about the response
#     depends on the docker exec completing.
#   * Body-byte cap enforced BEFORE JSON parsing — a hostile client
#     can't DoS us with a 100 MB payload.
_SANDBOX_ID_RE = re.compile(r"^[a-f0-9]{12}$")


@app.post("/internal/browser-log/{sandbox_id}", status_code=204)
async def browser_log(sandbox_id: str, request: Request) -> Response:
    """Ingest a batch of shim-produced browser events. Internal — see
    ai/sandbox/SANDBOX.md § Browser console capture.
    """
    if not _SANDBOX_ID_RE.match(sandbox_id):
        # Silently drop with a 204 so an attacker can't probe for
        # sandbox_id shape by watching the status code.
        log.debug("browser-log: rejecting malformed sandbox_id: %r", sandbox_id)
        return Response(status_code=204)
    body = await request.body()
    if len(body) > BROWSER_LOG_MAX_BODY_BYTES:
        log.warning(
            "browser-log: body too large (%d bytes) for sandbox=%s, rejecting",
            len(body), sandbox_id,
        )
        raise HTTPException(413, "browser-log body too large")
    try:
        payload = json.loads(body) if body else {}
    except ValueError:
        log.debug("browser-log: malformed JSON from sandbox=%s, dropping", sandbox_id)
        return Response(status_code=204)
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, list) or not entries:
        return Response(status_code=204)
    # Fire and forget so the browser gets its 204 immediately.
    asyncio.create_task(_do_browser_log_ingest(sandbox_id, entries))
    return Response(status_code=204)


@app.delete("/session/{session_id}", status_code=204)
async def delete_session(session_id: str) -> None:
    """Explicitly tear down a session's running sandbox. Idempotent —
    a session that never existed or has already expired still returns
    204 so the model doesn't have to remember state to clean up."""
    if not SESSION_ID_RE.match(session_id):
        log.warning("DELETE /session: invalid session_id: %r", session_id)
        raise HTTPException(400, "invalid session_id")
    await _do_close(session_id)


# Jobs GET routes are mounted from lifespan(). DELETE lives here so it can
# stop the container in addition to purging the row — build_router's built-in
# DELETE only touches the DB.
@app.delete("/jobs/{sandbox_id}", status_code=204)
async def delete_sandbox(sandbox_id: str) -> None:
    if state.registry is None or state.spawner is None or state.slot_sem is None:
        raise HTTPException(500, "runner not initialized")
    got = await state.registry.get(sandbox_id)
    if got is None:
        log.warning("DELETE /jobs: unknown sandbox=%s", sandbox_id)
        raise HTTPException(404, f"no sandbox {sandbox_id!r}")
    await asyncio.to_thread(state.spawner.stop, f"sandbox-{sandbox_id}")
    await state.registry.delete(sandbox_id)
    slot_released = got.phase in ("running", "starting", "spawning")
    if slot_released:
        state.slot_sem.release()
    log.info(
        "DELETE /jobs: deleted sandbox=%s (was phase=%s, slot_released=%s)",
        sandbox_id, got.phase, slot_released,
    )


# ── Source-code download ─────────────────────────────────────────────────
def _tar_response(stream, sandbox_id: str) -> StreamingResponse:
    return StreamingResponse(
        stream,
        media_type="application/x-tar",
        headers={
            "Content-Disposition": (
                f'attachment; filename="sandbox-{sandbox_id}.tar"'
            ),
        },
    )


@app.get("/jobs/{sandbox_id}/download")
async def download_sandbox_job(sandbox_id: str) -> StreamingResponse:
    log.info("download by sandbox_id=%s (direct)", sandbox_id)
    if state.registry is None or state.spawner is None:
        raise HTTPException(500, "runner not initialized")
    job = await state.registry.get(sandbox_id)
    if job is None:
        raise HTTPException(404, f"no sandbox {sandbox_id!r}")
    container_name = (job.result or {}).get("container_name")
    if not container_name:
        raise HTTPException(
            409, f"sandbox {sandbox_id!r} has no container to download"
        )
    try:
        stream = await asyncio.to_thread(
            state.spawner.export_files, container_name
        )
    except Exception as exc:
        raise HTTPException(404, f"container gone: {exc}")
    return _tar_response(stream, sandbox_id)


@app.get("/session/{session_id}/logs")
async def logs_session(session_id: str, lines: int = 100) -> dict:
    """Return the last ``lines`` of the running sandbox's combined
    stdout+stderr. Session-based so this survives self-heal spawns."""
    if not SESSION_ID_RE.match(session_id):
        raise HTTPException(400, "invalid session_id")
    if state.registry is None or state.spawner is None:
        raise HTTPException(500, "runner not initialized")
    existing = await _find_running_session(session_id)
    if existing is None or not existing.get("container_name"):
        raise HTTPException(
            404, f"no running sandbox for session {session_id!r}"
        )
    clamped = max(1, min(lines, 1000))
    text = await asyncio.to_thread(
        state.spawner.tail_logs, existing["container_name"], clamped
    )
    return {
        "session_id": session_id,
        "sandbox_id": existing["sandbox_id"],
        "lines_requested": lines,
        "logs": text,
    }


@app.get("/jobs/{sandbox_id}/logs")
async def logs_sandbox_job(sandbox_id: str, lines: int = 100) -> dict:
    if state.registry is None or state.spawner is None:
        raise HTTPException(500, "runner not initialized")
    job = await state.registry.get(sandbox_id)
    if job is None:
        raise HTTPException(404, f"no sandbox {sandbox_id!r}")
    container_name = (job.result or {}).get("container_name")
    if not container_name:
        raise HTTPException(
            409, f"sandbox {sandbox_id!r} has no container to read logs from"
        )
    clamped = max(1, min(lines, 1000))
    text = await asyncio.to_thread(
        state.spawner.tail_logs, container_name, clamped
    )
    return {
        "sandbox_id": sandbox_id,
        "lines_requested": lines,
        "logs": text,
    }


@app.get("/session/{session_id}/download")
async def download_session(session_id: str) -> StreamingResponse:
    if not SESSION_ID_RE.match(session_id):
        raise HTTPException(400, "invalid session_id")
    if state.registry is None or state.spawner is None:
        raise HTTPException(500, "runner not initialized")
    existing = await _find_running_session(session_id)
    if existing is None or not existing.get("container_name"):
        raise HTTPException(
            404, f"no running sandbox for session {session_id!r}"
        )
    try:
        stream = await asyncio.to_thread(
            state.spawner.export_files, existing["container_name"]
        )
    except Exception as exc:
        raise HTTPException(404, f"container gone: {exc}")
    return _tar_response(stream, existing["sandbox_id"])


# ── MCP + Tool Server mounts ─────────────────────────────────────────────
app.mount("/mcp", _mcp_app)
app.mount("/tool", tool_app)
