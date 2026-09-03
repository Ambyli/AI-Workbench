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
import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from common.jobs.postgres import PostgresRegistry
from common.jobs.router import build_router
from common.logging_setup import CsvLogger, setup_logging

# Import name is `sandbox_mcp`, not `mcp`, because `ai/sandbox/` is on
# sys.path and a module literally named `mcp` would shadow the `mcp`
# PyPI package that FastMCP itself imports internally (from mcp.types
# import ...) — that manifests as a confusing "FastMCP server support
# is not installed" ImportError at startup.
from sandbox_mcp import build_mcp, render_preview_html
from reaper import Reaper
from runtimes import RUNTIMES, get_runtime
from spawner import Spawner
from tests_runner import (
    TestResult,
    extract_test_files,
    result_to_dict,
    run_tests_in_companion,
)


# ── Config from env ───────────────────────────────────────────────────────
MAX_CONCURRENT = int(os.environ.get("SANDBOX_MAX_CONCURRENT", "8"))
DEFAULT_TTL_S = int(os.environ.get("SANDBOX_DEFAULT_TTL_SECONDS", "900"))
HARD_TTL_S = int(os.environ.get("SANDBOX_HARD_TTL_SECONDS", "3600"))
PROXY_URL = os.environ.get("SANDBOX_PROXY_URL", "http://sandbox-proxy")
LOG_DIR = os.environ.get("LOG_DIR", "/data")
DEBUG_LOGGING = os.environ.get("DEBUG_LOGGING", "false").lower() in (
    "1", "true", "yes", "on",
)
# Behavioral-test enforcement. See ai/sandbox/SANDBOX.md § Behavioral
# tests. When True, /run + preview_app return 400 if the caller didn't
# supply any files under tests/. When False, the tester still runs
# tests that ARE supplied — this knob only controls the gate.
TESTS_REQUIRED = os.environ.get("SANDBOX_TESTS_REQUIRED", "true").lower() in (
    "1", "true", "yes", "on",
)
TEST_TIMEOUT_S = float(os.environ.get("SANDBOX_TEST_TIMEOUT_SECONDS", "60"))

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
    "PROXY_URL=%s DEBUG_LOGGING=%s LOG_DIR=%s TESTS_REQUIRED=%s "
    "TEST_TIMEOUT_S=%.1f",
    MAX_CONCURRENT, DEFAULT_TTL_S, HARD_TTL_S, PROXY_URL, DEBUG_LOGGING, LOG_DIR,
    TESTS_REQUIRED, TEST_TIMEOUT_S,
)

# Sessions are the durable identity of a preview across turns. Regex is
# both a validation surface (reject anything that could path-inject when
# a caller later uses the id in a URL or filesystem context) and a hint
# to the model that the id is a short opaque string, not free text.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _new_session_id() -> str:
    """URL-safe 12-char session id. Enough entropy to avoid collisions
    across concurrent chats without dragging a UUID through log lines."""
    sid = secrets.token_urlsafe(9)  # 9 bytes → 12 base64 chars
    log.debug("generated new session_id: %s", sid)
    return sid


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


# Global singletons — initialized in lifespan().
_registry: Optional[PostgresRegistry] = None
_spawner: Optional[Spawner] = None
_reaper: Optional[Reaper] = None
# Simple semaphore for concurrency capping; the runner also relies on
# Docker to reject a container-create if resources are exhausted, but this
# semaphore gives us a clean 429 without paying a docker round-trip first.
_slot_sem: Optional[asyncio.Semaphore] = None
# Audit trail — one CSV row per /run + /mcp preview_app call. CsvLogger
# is not thread-safe, so writes are serialized behind an asyncio.Lock
# on the runner's event loop.
_audit: Optional[CsvLogger] = None
_audit_lock: Optional[asyncio.Lock] = None


_AUDIT_COLUMNS = [
    "event",           # "spawn" / "reuse" / "tests_missing" / "lint_failed"
                       # / "readiness_failed" / "spawn_error"
    "sandbox_id",
    "session_id",
    "runtime",
    "n_files",         # total files including tests
    "n_test_files",
    "reused",
    "phase",           # final registry phase, when applicable
    "tests_ok",
    "tests_exit_code",
    "tests_duration_s",
    "detail",          # short error message when the event was a failure
]


async def _audit_log(**fields) -> None:
    """Fire-and-forget audit write. Silently drops if the audit logger
    isn't up yet (module-import order corner case; the info also lands
    in the stdlib log)."""
    if _audit is None or _audit_lock is None:
        return
    # Defensive: never let audit surface a failure back to the caller.
    try:
        async with _audit_lock:
            await asyncio.to_thread(_audit.log, **fields)
    except Exception as exc:
        log.warning("audit_log write failed: %s", exc)


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
    log.debug(
        "MCP preview_app: runtime=%s session_id=%s n_files=%d n_deletes=%d "
        "entrypoint=%r ttl=%s",
        runtime, session_id, len(files or {}), len(deletes or []),
        entrypoint, ttl_seconds,
    )
    return await _reuse_or_spawn(
        runtime, files, entrypoint, ttl_seconds, session_id, deletes
    )


async def _mcp_logs(session_id: str, lines: int) -> dict:
    log.debug("MCP get_sandbox_logs: session_id=%s lines=%d", session_id, lines)
    return await logs_session(session_id, lines)


_mcp = build_mcp(_mcp_run, _mcp_logs)
# path="/" so FastMCP puts its transport at the mount root — mounting
# the returned ASGI app at /mcp then gives clients a single POST /mcp/
# endpoint. Without this, FastMCP defaults to /mcp/ inside the mounted
# app, so the full URL becomes /mcp/mcp/ and every client (including
# LiteLLM) hits 404 on POST /mcp/.
_mcp_app = _mcp.http_app(path="/")


@asynccontextmanager
async def lifespan(app_: FastAPI):
    global _registry, _spawner, _reaper, _slot_sem, _audit, _audit_lock
    log.info("lifespan startup: begin")
    _registry = PostgresRegistry(_build_dsn())
    await _registry.init()
    log.info("Postgres registry initialized")
    await _ensure_session_index(_registry)
    _spawner = Spawner()
    log.info("Docker spawner initialized")
    _slot_sem = asyncio.Semaphore(MAX_CONCURRENT)
    log.info("concurrency semaphore initialized: MAX_CONCURRENT=%d", MAX_CONCURRENT)
    # Audit CSV lives alongside the stdlib log under LOG_DIR. Operators
    # inspect via `docker exec sandbox-runner cat /data/audit.log`.
    _audit = CsvLogger(
        path=os.path.join(LOG_DIR, "audit.log"),
        columns=_AUDIT_COLUMNS,
        logger=log,
    )
    _audit_lock = asyncio.Lock()
    log.info("audit CSV logger initialized at %s/audit.log", LOG_DIR)
    _reaper = Reaper(_spawner, _registry, _slot_sem)
    _reaper.start()
    log.info("reaper started")
    # Mount the jobs router now that the registry is live. Delete is
    # handled by our own endpoint below (needs to stop the container),
    # so we don't ask build_router to add its own.
    app_.include_router(
        build_router(_registry, include_delete=False)
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
            if _reaper is not None:
                await _reaper.stop()
                log.info("reaper stopped")
            if _registry is not None:
                await _registry.close()
                log.info("Postgres registry closed")
            log.info("lifespan shutdown: complete")


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
    # Present on every successful spawn/update. `runtime` reflects the
    # runtime originally spawned under this session (needed by clients
    # that want to render startup output correctly per runtime).
    # `startup_output` is a tail of /tmp/sandbox.log — empty when the
    # container just spawned and has not printed anything yet.
    runtime: Optional[str] = None
    startup_output: str = ""
    # Result of running the caller-supplied tests against the live
    # preview inside a sandbox-tester companion container. Shape is
    # asdict(TestResult) — see tests_runner.TestResult. Absent (None)
    # only when tests were skipped deployment-wide
    # (SANDBOX_TESTS_REQUIRED=false AND no files under tests/).
    tests: Optional[dict] = None


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
        log.warning("_find_running_session called before registry init")
        return None
    pool = _registry._pool
    if pool is None:
        log.warning("_find_running_session: pool not ready")
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
        log.debug("_find_running_session: no running row for %s", session_id)
        return None
    meta = row["metadata"]
    if isinstance(meta, str):
        import json
        meta = json.loads(meta)
    result = row["result"] or {}
    if isinstance(result, str):
        import json
        result = json.loads(result)
    log.debug(
        "_find_running_session: session=%s → sandbox=%s container=%s",
        session_id, row["id"], result.get("container_name"),
    )
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
    log.info(
        "_reuse_or_spawn: runtime=%s session_id=%s n_files=%d n_deletes=%d "
        "entrypoint=%r",
        runtime, session_id, len(files or {}), len(deletes or []), entrypoint,
    )
    if _registry is None or _spawner is None or _slot_sem is None:
        log.error("_reuse_or_spawn called before lifespan init")
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
                log.info(
                    "reuse path: session=%s → container=%s alive, overlaying "
                    "%d file(s), removing %d",
                    session_id, existing["container_name"],
                    len(files or {}), len(deletes or []),
                )
                # Split the caller's files/deletes into app + test halves.
                # Test files never touch the sandbox's /app — they live in
                # the job's metadata and get put_archived into an ephemeral
                # sandbox-tester on each call.
                incoming_tests = extract_test_files(files)
                app_files = {
                    k: v for k, v in files.items()
                    if not k.startswith("tests/")
                }
                test_deletes = [d for d in deletes if d.startswith("tests/")]
                app_deletes = [d for d in deletes if not d.startswith("tests/")]
                # Load persisted tests from metadata and overlay the
                # caller's delta. This is the same "delta merge"
                # contract the app files follow (see update_files) —
                # unlisted tests are preserved. `deletes` with a
                # `tests/` prefix removes an entry from the map.
                existing_snap = await _registry.get(existing["sandbox_id"])
                prior_tests = (
                    (existing_snap.metadata.get("tests") or {})
                    if existing_snap is not None else {}
                )
                merged_tests = {**prior_tests, **incoming_tests}
                for d in test_deletes:
                    merged_tests.pop(d, None)
                existing_runtime = (
                    existing_snap.metadata.get("runtime")
                    if existing_snap is not None else None
                )
                # Enforce the tests-required gate against the MERGED
                # map, not the delta — a reuse call that only changes
                # app files should NOT fail the gate as long as the
                # session still has tests attached.
                if existing_runtime:
                    _validate_tests_present(
                        {**app_files, **merged_tests},
                        existing_runtime,
                        session_id,
                    )
                # Write a boundary marker to /tmp/sandbox.log BEFORE
                # the overlay. Downstream `tail_logs_since_last_marker`
                # anchors on this so the `startup_output` we return
                # holds only what the dev server printed AFTER the
                # reload. Old tracebacks stay on disk (get_sandbox_logs
                # still sees them) but don't leak into the preview
                # response, which was causing the model to report
                # "there's an error" after the user had already
                # corrected it. Marker is visible in the full log too,
                # so operators can tell exactly when each reload
                # happened.
                await asyncio.to_thread(
                    _spawner.write_reload_marker, existing["container_name"]
                )
                try:
                    await asyncio.to_thread(
                        _spawner.update_files,
                        existing["container_name"],
                        app_files,
                        app_deletes,
                    )
                except ValueError as exc:
                    # Path traversal etc. — surface as a 400, not a 500.
                    log.warning("reuse path: rejected unsafe path: %s", exc)
                    raise HTTPException(400, str(exc))
                # Persist the merged test map + bump last_used_at. Both
                # go through the same update_metadata call so the reuse
                # is atomic from the registry's perspective.
                await _registry.update_metadata(
                    existing["sandbox_id"],
                    {"last_used_at": _now_iso(), "tests": merged_tests},
                )
                # Give the dev server a moment to observe the file
                # change and print its reload notice / traceback before
                # we tail. Streamlit takes ~200 ms to notice an mtime
                # bump; Vite's HMR is faster; nginx doesn't print
                # anything on file overwrite. 500 ms hits the sweet
                # spot without adding perceptible latency to the tool
                # response.
                await asyncio.sleep(0.5)
                # Grab everything printed after the marker so the tool
                # wrapper can inline reload feedback in the response —
                # models get "did the reload succeed?" without a
                # second tool call, and without seeing the old error.
                startup_output = await asyncio.to_thread(
                    _spawner.tail_logs_since_last_marker,
                    existing["container_name"],
                    40,
                )
                # Run the merged test map against the (now updated)
                # sandbox. On reuse we DO care about test results —
                # this is the whole point of the feature: the model
                # gets feedback that the change didn't break the app
                # before the user has to click around.
                test_result = None
                if existing_runtime and merged_tests:
                    try:
                        rt = get_runtime(existing_runtime)
                    except KeyError:
                        rt = None
                    if rt is not None and rt.test_command:
                        test_result = await run_tests_in_companion(
                            _spawner,
                            existing["sandbox_id"],
                            rt,
                            merged_tests,
                            TEST_TIMEOUT_S,
                        )
                log.info(
                    "reuse path complete: session=%s sandbox=%s reused=True "
                    "startup_output_bytes=%d tests_ok=%s",
                    session_id, existing["sandbox_id"], len(startup_output),
                    None if test_result is None else test_result.ok,
                )
                await _audit_log(
                    event="reuse",
                    sandbox_id=existing["sandbox_id"],
                    session_id=session_id,
                    runtime=existing_runtime or "",
                    n_files=len(files),
                    n_test_files=len(merged_tests),
                    reused=True,
                    phase="running",
                    tests_ok="" if test_result is None else test_result.ok,
                    tests_exit_code="" if test_result is None else test_result.exit_code,
                    tests_duration_s="" if test_result is None else f"{test_result.duration_s:.2f}",
                    detail="",
                )
                return _build_run_result(
                    sandbox_id=existing["sandbox_id"],
                    session_id=session_id,
                    url=existing["url"],
                    expires_at=existing["expires_at"],
                    reused=True,
                    runtime=existing_runtime,
                    startup_output=startup_output,
                    test_result=test_result,
                )
            # Container is gone but the Postgres row still says "running"
            # — normal when the reaper hasn't swept yet, or the sandbox
            # crashed out-of-band. Mark it expired and fall through to
            # respawn under the same session_id.
            log.info(
                "self-heal: session=%s sandbox=%s row says running but container "
                "is gone, marking expired and respawning",
                session_id, existing["sandbox_id"],
            )
            await _registry.set_phase(existing["sandbox_id"], "expired")

    # Spawn path — either no session_id given, or the session_id had no
    # live container. Generate one if missing so the caller always gets
    # a stable handle back.
    session_id = session_id or _new_session_id()

    try:
        rt = get_runtime(runtime)
    except KeyError:
        log.warning("unknown runtime requested: %r", runtime)
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
        log.warning(
            "runtime=%s rejects custom entrypoint %r", runtime, entrypoint
        )
        raise HTTPException(
            400,
            f"the {runtime!r} runtime does not accept a custom entrypoint "
            f"(it uses a fixed process: {rt.default_entrypoint!r}). "
            "Remove the `entrypoint` field, or switch to runtime=python or "
            "runtime=node if you need to run a specific command.",
        )

    # Mandatory-tests gate — refuse the spawn if the caller didn't
    # ship at least one file under `tests/`. Runs before the lint pass
    # so a model that forgot BOTH tests AND fixed syntax gets a single
    # error (missing tests) instead of a stack of two. Skipped when
    # SANDBOX_TESTS_REQUIRED=false.
    _validate_tests_present(files, runtime, session_id)

    # Split files into the app half (goes into /app in the sandbox) and
    # the test half (stashed in job metadata, put_archived into an
    # ephemeral tester on this and every future reuse call). Test files
    # do NOT enter the sandbox's /app — that would leak them via
    # nginx/express/etc. and pollute the app filesystem for the dev
    # server's own file watcher.
    test_files = extract_test_files(files)
    app_files = {k: v for k, v in files.items() if not k.startswith("tests/")}

    # Static lint pass — catches syntax errors in ~1 ms without spawning
    # a container. Python is the only runtime we can lint from inside
    # the runner (we already have a Python interpreter); Node/HTML would
    # need shelling out to node/html-tidy. Model gets a specific
    # SyntaxError with line/column before we waste 30 s on readiness.
    # Lint only the app half — test files are executed by pytest/jest
    # in the tester container and Python's `compile()` doesn't
    # understand JS-flavored .test.js files anyway.
    lint_errors = _lint_python_files(app_files)
    if lint_errors:
        log.warning(
            "static lint failed for session=%s: %d error(s) across %d file(s)",
            session_id, len(lint_errors),
            len({e["path"] for e in lint_errors}),
        )
        await _audit_log(
            event="lint_failed",
            sandbox_id="",
            session_id=session_id,
            runtime=runtime,
            n_files=len(files),
            n_test_files=len(test_files),
            reused=False,
            phase="",
            tests_ok="",
            tests_exit_code="",
            tests_duration_s="",
            detail=f"{len(lint_errors)} python syntax error(s)",
        )
        raise HTTPException(
            400,
            {
                "error": "static lint failed",
                "session_id": session_id,
                "errors": lint_errors,
                "hint": (
                    "Fix the syntax errors above and call preview_app again "
                    "with the same session_id. No container was spawned."
                ),
            },
        )

    # Fast-fail if the pool is full. wait_for with a short timeout works
    # because asyncio.Semaphore.acquire returns immediately when a slot is
    # available; TimeoutError only happens when the pool is genuinely full.
    try:
        await asyncio.wait_for(_slot_sem.acquire(), timeout=0.1)
    except asyncio.TimeoutError:
        log.warning(
            "sandbox pool exhausted (MAX_CONCURRENT=%d), rejecting session=%s",
            MAX_CONCURRENT, session_id,
        )
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
            # Persist test files so reuse calls can run them again
            # without the caller re-sending the whole map every turn.
            "tests": test_files,
        },
        initial_phase="spawning",
    )

    log.info(
        "spawn path: session=%s sandbox=%s runtime=%s ttl=%ds entrypoint=%r",
        session_id, sandbox_id, runtime, ttl, entrypoint or rt.default_entrypoint,
    )

    try:
        result = await asyncio.to_thread(
            _spawner.spawn, sandbox_id, rt, app_files, entrypoint
        )
        log.info(
            "container created: sandbox=%s container=%s, polling readiness",
            sandbox_id, result.container_name,
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
            # Grab logs BEFORE tearing the container down — otherwise
            # the traceback / npm error / streamlit exception vanishes
            # with the container and the model just sees "did not
            # become ready" with no actionable signal.
            log.warning(
                "readiness FAILED: sandbox=%s container=%s did not bind port %d "
                "within 30s, capturing logs before teardown",
                sandbox_id, result.container_name, rt.internal_port,
            )
            logs = await asyncio.to_thread(
                _spawner.tail_logs, result.container_name, 100
            )
            await asyncio.to_thread(_spawner.stop, result.container_name)
            await _registry.set_error(
                sandbox_id,
                "sandbox did not become ready within 30s\n\n" + logs,
            )
            await _audit_log(
                event="readiness_failed",
                sandbox_id=sandbox_id,
                session_id=session_id,
                runtime=runtime,
                n_files=len(files),
                n_test_files=len(test_files),
                reused=False,
                phase="failed",
                tests_ok="",
                tests_exit_code="",
                tests_duration_s="",
                detail="did not bind port within 30s",
            )
            raise HTTPException(
                504,
                {
                    "error": "sandbox did not become ready within 30s",
                    "session_id": session_id,
                    "logs": logs,
                    "hint": (
                        "Read the container logs above to see why the app "
                        "failed to start (traceback, missing module, port "
                        "bind error, etc.). Fix the code and call "
                        "preview_app again with the same session_id — the "
                        "runner will spawn a fresh container."
                    ),
                },
            )

        url = f"{PROXY_URL}/{sandbox_id}/"
        await _registry.set_result(
            sandbox_id,
            {"url": url, "container_name": result.container_name},
            phase="running",
        )
        # Grab a fresh tail of the container's stdout so the tool
        # wrapper can inline it in the response. Fetching here (before
        # returning) means the model gets startup diagnostics in the
        # same tool response as the URL — no second tool call, no
        # reliance on the model remembering to check.
        startup_output = await asyncio.to_thread(
            _spawner.tail_logs, result.container_name, 40
        )
        # Behavioral tests — soft-fail. The URL is returned regardless
        # so the model + operator can inspect what shipped, but a
        # failing result is loudly surfaced in the response so the
        # model iterates BEFORE handing back to the user.
        test_result: Optional[TestResult] = None
        if test_files and rt.test_command:
            test_result = await run_tests_in_companion(
                _spawner, sandbox_id, rt, test_files, TEST_TIMEOUT_S,
            )
        log.info(
            "spawn READY: sandbox=%s session=%s url=%s expires=%s "
            "startup_output_bytes=%d tests_ok=%s",
            sandbox_id, session_id, url, expires_at.isoformat(),
            len(startup_output),
            None if test_result is None else test_result.ok,
        )
        await _audit_log(
            event="spawn",
            sandbox_id=sandbox_id,
            session_id=session_id,
            runtime=runtime,
            n_files=len(files),
            n_test_files=len(test_files),
            reused=False,
            phase="running",
            tests_ok="" if test_result is None else test_result.ok,
            tests_exit_code="" if test_result is None else test_result.exit_code,
            tests_duration_s="" if test_result is None else f"{test_result.duration_s:.2f}",
            detail="",
        )
        return _build_run_result(
            sandbox_id=sandbox_id,
            session_id=session_id,
            url=url,
            expires_at=expires_at.isoformat(),
            reused=False,
            runtime=runtime,
            startup_output=startup_output,
            test_result=test_result,
        )
    except HTTPException:
        # Propagate structured errors (400 lint, 504 readiness). Release
        # the slot; the sandbox_id row keeps its "failed" phase and gets
        # reaped by the sweeper later.
        _slot_sem.release()
        log.debug(
            "spawn HTTPException propagated (slot released) for sandbox=%s",
            sandbox_id,
        )
        raise
    except Exception as exc:
        log.exception(
            "spawn EXCEPTION for sandbox=%s session=%s: %s",
            sandbox_id, session_id, exc,
        )
        await _registry.set_error(sandbox_id, str(exc))
        await _audit_log(
            event="spawn_error",
            sandbox_id=sandbox_id,
            session_id=session_id,
            runtime=runtime,
            n_files=len(files),
            n_test_files=len(test_files),
            reused=False,
            phase="failed",
            tests_ok="",
            tests_exit_code="",
            tests_duration_s="",
            detail=f"{type(exc).__name__}: {exc}",
        )
        _slot_sem.release()
        raise HTTPException(500, f"spawn failed: {exc}")
    # Slot is intentionally NOT released on success — it's released when
    # the reaper (or DELETE /jobs/{id}) tears the sandbox down.


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_run_result(
    *,
    sandbox_id: str,
    session_id: str,
    url: str,
    expires_at: str,
    reused: bool,
    runtime: Optional[str],
    startup_output: str,
    test_result: Optional[TestResult],
) -> dict:
    """Assemble the dict returned by ``_reuse_or_spawn`` to /run, the MCP
    tool, and the OpenWebUI Tool Server route.

    Extracted so the spawn path and the reuse path can't drift on
    fields. Adding a new field to the response ONLY happens here — the
    RunResponse model + this builder are the single source of truth."""
    return {
        "sandbox_id": sandbox_id,
        "session_id": session_id,
        "url": url,
        "expires_at": expires_at,
        "reused": reused,
        "runtime": runtime,
        "startup_output": startup_output,
        "tests": result_to_dict(test_result),
    }


def _validate_tests_present(
    files: dict[str, str],
    runtime_name: str,
    session_id: str,
) -> None:
    """Raise HTTPException(400) if TESTS_REQUIRED is on and the caller
    didn't include anything under ``tests/``.

    The 400 detail matches the shape ``_lint_python_files`` uses for
    static lint failures — ``error``, ``session_id``, ``hint``, plus a
    runtime-specific ``example`` map the MCP wrapper renders as a
    fenced block so the model has a concrete file to copy.

    No-op when ``TESTS_REQUIRED=false`` or when at least one file
    starts with the ``tests/`` prefix. Content is not inspected here —
    the tester container is the authority on whether a test file is
    actually useful."""
    if not TESTS_REQUIRED:
        log.debug("_validate_tests_present: SANDBOX_TESTS_REQUIRED=false, skipping gate")
        return
    if extract_test_files(files):
        return
    try:
        rt = get_runtime(runtime_name)
    except KeyError:
        # Unknown runtime — let the downstream get_runtime call in
        # _reuse_or_spawn raise the specific "unknown runtime" error.
        # We only surface a missing-tests error when we can quote a
        # per-runtime example, and we can't do that for an unknown
        # runtime. Fall through.
        return
    example = rt.test_example or {}
    hint = (
        "This deployment requires model-supplied behavioral tests. Add at "
        "least one file under the top-level `tests/` directory that "
        "exercises the running preview (navigate, click, assert visible "
        f"text/data) — not a tautology like `assert 1+1==2`. Use "
        f"`{rt.test_files_hint}` if you're unsure of the shape; PREVIEW_URL "
        "is exported into the tester's env, so tests should read it and "
        "hit the running app.\n\n"
        "Call preview_app again with the same session_id and the tests "
        "included. No container has been spawned yet."
    )
    log.warning(
        "tests missing: session=%s runtime=%s (no file under tests/)",
        session_id, runtime_name,
    )
    # Fire-and-forget audit write. _validate_tests_present is called
    # from an async caller (both _reuse_or_spawn paths), so
    # create_task finds the running loop. RuntimeError only happens
    # if this function is ever invoked from sync test code — silently
    # skipping the audit there is fine.
    try:
        asyncio.create_task(_audit_log(
            event="tests_missing",
            sandbox_id="",
            session_id=session_id,
            runtime=runtime_name,
            n_files=len(files),
            n_test_files=0,
            reused=False,
            phase="",
            tests_ok=False,
            tests_exit_code="",
            tests_duration_s="",
            detail="no files under tests/",
        ))
    except RuntimeError:
        pass
    raise HTTPException(
        400,
        {
            "error": "tests missing",
            "session_id": session_id,
            "runtime": runtime_name,
            "hint": hint,
            "example": example,
        },
    )


def _lint_python_files(files: dict[str, str]) -> list[dict]:
    """Compile every ``.py`` file to catch SyntaxError before we spawn.

    Returns a list of ``{path, line, offset, message}`` entries — empty
    if everything parses. Uses Python's built-in ``compile()`` so we
    don't need a separate linter; catches every syntax error the
    interpreter would raise at import time, plus indentation errors.

    Deliberately does NOT run imports or execute code — the container
    is where that runs. Static-only, ~1 ms per file even for large
    Streamlit apps."""
    out: list[dict] = []
    py_paths = [p for p in files if p.endswith(".py")]
    log.debug("_lint_python_files: checking %d .py file(s)", len(py_paths))
    for path in py_paths:
        content = files[path]
        # Wrap the display path in angle brackets so Python's SyntaxError
        # doesn't try to read a file with that name off the runner's own
        # filesystem for the ``text`` attribute — otherwise a sandbox
        # ``app.py`` collides with the runner's ``app.py`` and the error
        # text shows runner source instead of the sandbox source.
        display_name = f"<sandbox:{path}>"
        try:
            compile(content, display_name, "exec")
        except SyntaxError as exc:
            # Reconstruct the offending line from the source ourselves —
            # exc.text is unreliable when Python can't find the file on
            # disk (as it can't, with the angle-bracket name).
            source_lines = content.splitlines()
            text = ""
            if isinstance(exc.lineno, int) and 1 <= exc.lineno <= len(source_lines):
                text = source_lines[exc.lineno - 1]
            log.debug(
                "lint: %s:%s:%s SyntaxError: %s",
                path, exc.lineno, exc.offset, exc.msg,
            )
            out.append({
                "path": path,
                "line": exc.lineno,
                "offset": exc.offset,
                "message": f"{type(exc).__name__}: {exc.msg}",
                "text": text.rstrip(),
            })
        except Exception as exc:
            # Rare — usually a null byte or encoding surprise. Report it
            # rather than let the container silently fail later.
            log.warning("lint: %s unexpected %s: %s", path, type(exc).__name__, exc)
            out.append({
                "path": path,
                "line": None,
                "offset": None,
                "message": f"{type(exc).__name__}: {exc}",
                "text": "",
            })
    if not out:
        log.debug("_lint_python_files: all files clean")
    return out


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
    )
    return RunResponse(**result)


@app.delete("/session/{session_id}", status_code=204)
async def delete_session(session_id: str) -> None:
    """Explicitly tear down a session's running sandbox. Idempotent —
    a session that never existed or has already expired still returns
    204 so the model doesn't have to remember state to clean up."""
    if not _SESSION_ID_RE.match(session_id):
        log.warning("DELETE /session: invalid session_id: %r", session_id)
        raise HTTPException(400, "invalid session_id")
    if _registry is None or _spawner is None or _slot_sem is None:
        raise HTTPException(500, "runner not initialized")
    existing = await _find_running_session(session_id)
    if existing is None:
        log.info("DELETE /session: session=%s already gone (noop 204)", session_id)
        return
    if existing["container_name"]:
        await asyncio.to_thread(
            _spawner.stop, existing["container_name"]
        )
    await _registry.set_phase(existing["sandbox_id"], "closed")
    # The slot was held for the running sandbox — give it back.
    _slot_sem.release()
    log.info(
        "DELETE /session: closed session=%s sandbox=%s (slot released)",
        session_id, existing["sandbox_id"],
    )


# Jobs GET routes are mounted from lifespan(). DELETE lives here so it can
# stop the container in addition to purging the row — build_router's built-in
# DELETE only touches the DB.
@app.delete("/jobs/{sandbox_id}", status_code=204)
async def delete_sandbox(sandbox_id: str) -> None:
    if _registry is None or _spawner is None or _slot_sem is None:
        raise HTTPException(500, "runner not initialized")
    got = await _registry.get(sandbox_id)
    if got is None:
        log.warning("DELETE /jobs: unknown sandbox=%s", sandbox_id)
        raise HTTPException(404, f"no sandbox {sandbox_id!r}")
    await asyncio.to_thread(_spawner.stop, f"sandbox-{sandbox_id}")
    await _registry.delete(sandbox_id)
    # Give the slot back to the pool if the sandbox was still running.
    slot_released = got.phase in ("running", "starting", "spawning")
    if slot_released:
        _slot_sem.release()
    log.info(
        "DELETE /jobs: deleted sandbox=%s (was phase=%s, slot_released=%s)",
        sandbox_id, got.phase, slot_released,
    )


# ── Source-code download ─────────────────────────────────────────────────
# Streams the sandbox's /app back to the caller as a plain tar. The Docker
# daemon does the packing (get_archive) so the runner never buffers the
# whole archive in memory — chunks pass straight through. The Caddy route
# /sandboxes/download/{session_id} reverse-proxies to the session variant,
# so end users get an oauth2-proxy-authenticated download URL that matches
# the same auth model as the preview iframe.
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
    """Direct download by sandbox_id. Works whether or not the sandbox
    still has a session — useful for operator debugging. If the session
    self-healed since the download URL was minted, this endpoint returns
    the OLD (dead) sandbox_id's archive — the session endpoint below
    resolves to the current running one instead."""
    log.info("download by sandbox_id=%s (direct)", sandbox_id)
    if _registry is None or _spawner is None:
        raise HTTPException(500, "runner not initialized")
    job = await _registry.get(sandbox_id)
    if job is None:
        log.warning("download: unknown sandbox=%s", sandbox_id)
        raise HTTPException(404, f"no sandbox {sandbox_id!r}")
    container_name = (job.result or {}).get("container_name")
    if not container_name:
        log.warning(
            "download: sandbox=%s has no container_name (phase=%s)",
            sandbox_id, job.phase,
        )
        raise HTTPException(
            409, f"sandbox {sandbox_id!r} has no container to download"
        )
    try:
        stream = await asyncio.to_thread(
            _spawner.export_files, container_name
        )
    except Exception as exc:
        log.warning("download: container %s gone: %s", container_name, exc)
        raise HTTPException(404, f"container gone: {exc}")
    return _tar_response(stream, sandbox_id)


@app.get("/session/{session_id}/logs")
async def logs_session(session_id: str, lines: int = 100) -> dict:
    """Return the last ``lines`` of the running sandbox's combined
    stdout+stderr. Session-based so this survives self-heal spawns.

    Model call this when the user reports the app looks broken but
    ``preview_app`` returned a normal ready response — Streamlit /
    Flask / Vite dev servers usually print the offending traceback
    here before rendering an error card in the browser. The model can
    fix the code and re-issue ``preview_app`` without needing the user
    to relay the error text."""
    if not _SESSION_ID_RE.match(session_id):
        log.warning("logs_session: invalid session_id: %r", session_id)
        raise HTTPException(400, "invalid session_id")
    if _registry is None or _spawner is None:
        raise HTTPException(500, "runner not initialized")
    existing = await _find_running_session(session_id)
    if existing is None or not existing.get("container_name"):
        log.warning("logs_session: no running sandbox for session=%s", session_id)
        raise HTTPException(
            404, f"no running sandbox for session {session_id!r}"
        )
    clamped = max(1, min(lines, 1000))
    text = await asyncio.to_thread(
        _spawner.tail_logs, existing["container_name"], clamped
    )
    log.debug(
        "logs_session: session=%s sandbox=%s returned %d bytes",
        session_id, existing["sandbox_id"], len(text),
    )
    return {
        "session_id": session_id,
        "sandbox_id": existing["sandbox_id"],
        "lines_requested": lines,
        "logs": text,
    }


@app.get("/jobs/{sandbox_id}/logs")
async def logs_sandbox_job(sandbox_id: str, lines: int = 100) -> dict:
    """Direct log fetch by internal sandbox_id. Useful for operator
    debugging when you want the logs from a SPECIFIC spawn event even
    after the session has self-healed to a new container."""
    if _registry is None or _spawner is None:
        raise HTTPException(500, "runner not initialized")
    job = await _registry.get(sandbox_id)
    if job is None:
        log.warning("logs_sandbox_job: unknown sandbox=%s", sandbox_id)
        raise HTTPException(404, f"no sandbox {sandbox_id!r}")
    container_name = (job.result or {}).get("container_name")
    if not container_name:
        log.warning(
            "logs_sandbox_job: sandbox=%s no container (phase=%s)",
            sandbox_id, job.phase,
        )
        raise HTTPException(
            409, f"sandbox {sandbox_id!r} has no container to read logs from"
        )
    clamped = max(1, min(lines, 1000))
    text = await asyncio.to_thread(
        _spawner.tail_logs, container_name, clamped
    )
    log.debug(
        "logs_sandbox_job: sandbox=%s returned %d bytes",
        sandbox_id, len(text),
    )
    return {
        "sandbox_id": sandbox_id,
        "lines_requested": lines,
        "logs": text,
    }


@app.get("/session/{session_id}/download")
async def download_session(session_id: str) -> StreamingResponse:
    """Download the /app tar for whichever sandbox is currently running
    under this session_id. Preferred entry point — this URL stays valid
    across self-heal spawns because it resolves the session at request
    time, not at URL-generation time."""
    log.info("download by session=%s", session_id)
    if not _SESSION_ID_RE.match(session_id):
        log.warning("download_session: invalid session_id: %r", session_id)
        raise HTTPException(400, "invalid session_id")
    if _registry is None or _spawner is None:
        raise HTTPException(500, "runner not initialized")
    existing = await _find_running_session(session_id)
    if existing is None or not existing.get("container_name"):
        log.warning("download_session: no running sandbox for %s", session_id)
        raise HTTPException(
            404, f"no running sandbox for session {session_id!r}"
        )
    try:
        stream = await asyncio.to_thread(
            _spawner.export_files, existing["container_name"]
        )
    except Exception as exc:
        raise HTTPException(404, f"container gone: {exc}")
    return _tar_response(stream, existing["sandbox_id"])


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
    log.debug(
        "Tool Server /tool/preview_app: runtime=%s session_id=%s n_files=%d",
        req.runtime, req.session_id, len(req.files or {}),
    )
    # Session-id validation runs at the pydantic layer for RunRequest but
    # ToolPreviewAppRequest doesn't share that validator — reject invalid
    # ids here so path-injection attempts don't leak through this route.
    if req.session_id and not _SESSION_ID_RE.match(req.session_id):
        log.warning("tool_preview_app: invalid session_id %r", req.session_id)
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
        content=render_preview_html(
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
