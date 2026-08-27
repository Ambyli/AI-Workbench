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
from pydantic import BaseModel, Field

from common.jobs.postgres import PostgresRegistry
from common.jobs.router import build_router

from mcp import build_mcp
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
# Build the MCP server with the shared spawn callable, then mount its ASGI
# app under /mcp. LiteLLM's mcp_servers entry points at this path.
async def _mcp_run(
    runtime: str,
    files: dict[str, str],
    entrypoint: Optional[str],
    ttl_seconds: Optional[int],
) -> dict:
    return await _spawn_and_track(runtime, files, entrypoint, ttl_seconds)


_mcp = build_mcp(_mcp_run)
app.mount("/mcp", _mcp.http_app())
