"""sandbox-runner — FastAPI + MCP front-end for the sandbox spawner.

Gives OpenWebUI a way to ask "run this code and give me a URL I can
iframe". The URL is served by sandbox-proxy (Caddy) on ai_shared and
routes to a short-lived sandbox-{id} container on sandbox_net. The
network segmentation is what keeps model-generated code from reaching
litellm, phoenix-mcp, roofix-db, or anything else on the stack.

See ai/sandbox/SANDBOX.md for the operator guide and the security-invariant
checklist that must be re-verified after any change here.

MCP tool surface (all under the ``sandbox`` server):

    get_runtimes()                  — describe available runtimes
    create(runtime, env?, ...)      — warm an empty container, returns session
    update_files(session_id, files, deletes?, recreate_if_gone?)
                                    — overlay files, health-probe after
    get_files(session_id, paths?)   — read files back from /app
    get_logs(session_id, lines?)    — tail container stdout+stderr
    exec(session_id, command, ...)  — run non-interactive shell command
    patch_files(session_id, patches)— strict line-range edits, all-or-nothing
    preview(session_id)             — return the iframe artifact HTML
    close(session_id)               — teardown and release slot
    list_sessions()                 — enumerate live sandboxes globally
    run(runtime, files, ...)        — convenience: create + update + preview
    preview_app(...)                — deprecated alias for run

HTTP endpoints:

    GET  /health                    healthcheck
    POST /run                       spawn a sandbox, return its URL
    GET  /jobs                      all managed sandboxes + phase
    GET  /jobs/{id}                 one sandbox detail
    DELETE /jobs/{id}               tear down a sandbox early
    GET  /sessions                  list live sessions (list_sessions backing)
    POST /session/{id}/files        update_files overlay (JSON)
    GET  /session/{id}/files        list files under /app or read specified paths
    POST /session/{id}/exec         run a shell command in the container
    POST /session/{id}/patch        strict line-range file edits (patch_files)
    /mcp                            FastMCP HTTP transport
    /tool/*                         OpenWebUI Tool Server sub-app:
        GET  /tool/openapi.json     OpenAPI spec for OpenWebUI discovery
        POST /tool/run              spawn + return HTMLResponse iframe
        POST /tool/preview_app      deprecated alias for /tool/run
"""

from __future__ import annotations

# Load .env before anything reads os.environ.
from common.env import load_env

load_env()

import asyncio
import base64
import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional, Union

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from common.jobs.postgres import PostgresRegistry
from common.jobs.router import build_router
from common.logging_setup import setup_logging

# Import name is `sandbox_mcp`, not `mcp`, because `ai/sandbox/` is on
# sys.path and a module literally named `mcp` would shadow the `mcp`
# PyPI package that FastMCP itself imports internally (from mcp.types
# import ...) — that manifests as a confusing "FastMCP server support
# is not installed" ImportError at startup.
from sandbox_mcp import build_mcp, render_preview_html
from reaper import Reaper
from runtimes import RUNTIMES, get_runtime
from spawner import Spawner


# ── Config from env ───────────────────────────────────────────────────────
MAX_CONCURRENT = int(os.environ.get("SANDBOX_MAX_CONCURRENT", "8"))
DEFAULT_TTL_S = int(os.environ.get("SANDBOX_DEFAULT_TTL_SECONDS", "900"))
HARD_TTL_S = int(os.environ.get("SANDBOX_HARD_TTL_SECONDS", "3600"))
PROXY_URL = os.environ.get("SANDBOX_PROXY_URL", "http://sandbox-proxy")
LOG_DIR = os.environ.get("LOG_DIR", "/data")
DEBUG_LOGGING = os.environ.get("DEBUG_LOGGING", "false").lower() in (
    "1", "true", "yes", "on",
)

# Per-file and total-payload caps for update_files / run / POST /run.
# Enforced at pydantic-validation time so a hostile payload never touches
# the tarball builder. Base64 files count their DECODED length so a client
# can't smuggle a huge blob past the cap by encoding it.
MAX_FILE_BYTES = int(os.environ.get("SANDBOX_MAX_FILE_BYTES", "1000000"))
MAX_PAYLOAD_BYTES = int(os.environ.get("SANDBOX_MAX_PAYLOAD_BYTES", "10000000"))

# get_files response: default per-file byte cap and a hard cap the caller
# can't exceed. Kept smaller than MAX_FILE_BYTES because get_files is a
# READ path — 64 KB is enough to spot-check a source file without paging
# through the whole payload for something huge like an assets bundle.
GET_FILES_DEFAULT_BYTES = 8 * 1024
GET_FILES_HARD_CAP_BYTES = 64 * 1024

# exec_command: model-facing defaults + hard caps.
EXEC_DEFAULT_TIMEOUT_S = 30
EXEC_HARD_TIMEOUT_S = 120
EXEC_MAX_OUTPUT_BYTES = 8 * 1024

# Health-probe deadline used after update_files. Kept tight so the tool
# response isn't dominated by waiting on a broken app.
HEALTH_PROBE_TIMEOUT_S = 3.0
# Post-update settle delay — gives dev servers a moment to notice the
# file change (Streamlit mtime scan, Vite HMR, nginx cache invalidation)
# before the probe fires. 500 ms is the same window the pre-refactor
# code used before its log tail; matches Streamlit's polling cadence.
UPDATE_SETTLE_S = 0.5

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

# Sessions are the durable identity of a preview across turns. Regex is
# both a validation surface (reject anything that could path-inject when
# a caller later uses the id in a URL or filesystem context) and a hint
# to the model that the id is a short opaque string, not free text.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Env vars we refuse to accept from the caller — these are runner-controlled
# invariants (egress proxy, log buffering) and must not be overridable.
# The spawner reapplies them on top anyway; refusing at the request layer
# gives the caller a clear error instead of silently discarding their value.
_RESERVED_ENV_KEYS = frozenset({
    "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
    "PYTHONUNBUFFERED", "NPM_CONFIG_LOGLEVEL", "FORCE_COLOR", "TERM",
})


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


# ── MCP server + ASGI app ─────────────────────────────────────────────────
# Build here (module scope, before the FastAPI construction) so we can
# compose the MCP app's lifespan into ours. The tool implementations
# reference module globals that are populated in the FastAPI lifespan —
# that's fine because they're called lazily.
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


async def _mcp_update_files(
    session_id: str,
    files: dict,
    deletes: list[str],
    recreate_if_gone: bool,
) -> dict:
    log.debug(
        "MCP update_files: session=%s n_files=%d n_deletes=%d recreate=%s",
        session_id, len(files or {}), len(deletes or []), recreate_if_gone,
    )
    return await _do_update_files(session_id, files, deletes, recreate_if_gone)


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
    update_files_callable=_mcp_update_files,
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
    global _registry, _spawner, _reaper, _slot_sem
    log.info("lifespan startup: begin")
    _registry = PostgresRegistry(_build_dsn())
    await _registry.init()
    log.info("Postgres registry initialized")
    await _ensure_session_index(_registry)
    _spawner = Spawner()
    log.info("Docker spawner initialized")
    _slot_sem = asyncio.Semaphore(MAX_CONCURRENT)
    log.info("concurrency semaphore initialized: MAX_CONCURRENT=%d", MAX_CONCURRENT)
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

# Type alias for the discriminated file map. On the wire it's:
#   {"path/to/file.txt": "utf-8 text"}
# or
#   {"path/to/image.png": {"encoding": "base64", "content": "iVBOR..."}}
# Pydantic doesn't do discriminated Unions of primitives cleanly, so we
# validate manually in `_validate_files_map`.
FileContent = Union[str, dict]


def _validate_files_map(files: dict) -> dict:
    """Pydantic ``field_validator`` body for the ``files`` map.

    Rejects non-str/non-dict values, enforces per-file and total payload
    size caps. Base64 files are decoded once here so the decoded byte-
    length is what counts against the caps — no bypass by encoding.
    Returns the ORIGINAL map (untouched — the spawner handles decoding
    on the write side) so downstream code sees exactly what the caller
    sent.
    """
    if not isinstance(files, dict):
        raise ValueError("files must be a mapping of path → content")
    total = 0
    per_file_over: list[tuple[str, int]] = []
    for path, value in files.items():
        if isinstance(value, str):
            size = len(value.encode("utf-8"))
        elif isinstance(value, dict):
            encoding = value.get("encoding")
            content = value.get("content", "")
            if encoding == "base64":
                # Length of the decoded payload. Use base64.b64decode's
                # own reject-on-noise mode so a garbage value fails at
                # validation, not deep inside the spawner.
                try:
                    decoded = base64.b64decode(content, validate=True)
                except Exception as exc:
                    raise ValueError(
                        f"invalid base64 in files[{path!r}]: {exc}"
                    ) from exc
                size = len(decoded)
            elif encoding in (None, "utf-8", "utf8", "text"):
                size = len(str(content).encode("utf-8"))
            else:
                raise ValueError(
                    f"unknown encoding {encoding!r} in files[{path!r}]; "
                    "expected 'base64' or 'utf-8'"
                )
        else:
            raise ValueError(
                f"files[{path!r}] must be str or "
                f"{{encoding, content}} dict, not {type(value).__name__}"
            )
        if size > MAX_FILE_BYTES:
            per_file_over.append((path, size))
        total += size
    if per_file_over or total > MAX_PAYLOAD_BYTES:
        parts = []
        parts.append(
            f"Rejected: files payload exceeded limits."
        )
        parts.append(
            f"  Individual file cap: {MAX_FILE_BYTES:,} bytes"
        )
        if per_file_over:
            biggest_path, biggest_size = max(per_file_over, key=lambda t: t[1])
            parts.append(
                f"  (largest oversized: {biggest_path!r} at {biggest_size:,} bytes; "
                f"{len(per_file_over)} file(s) over the cap)"
            )
        parts.append(
            f"  Total payload cap: {MAX_PAYLOAD_BYTES:,} bytes "
            f"(submitted: {total:,} bytes)"
        )
        parts.append(
            "Shrink files, drop non-essential assets, or split across "
            "multiple update_files calls."
        )
        raise ValueError("\n".join(parts))
    return files


def _validate_env(env: Optional[dict]) -> Optional[dict[str, str]]:
    """Reject non-string values and reserved keys. Kept in one place so
    ``create``, ``run``, and ``POST /run`` all enforce the same rules."""
    if env is None:
        return None
    if not isinstance(env, dict):
        raise ValueError("env must be a mapping of str → str")
    out: dict[str, str] = {}
    for k, v in env.items():
        if not isinstance(k, str):
            raise ValueError(f"env key must be str, got {type(k).__name__}")
        if k in _RESERVED_ENV_KEYS:
            raise ValueError(
                f"env key {k!r} is reserved by the runner and cannot be set "
                "by the caller (proxy + buffering invariants)"
            )
        if not isinstance(v, (str, int, float, bool)):
            raise ValueError(
                f"env value for {k!r} must be a scalar, got {type(v).__name__}"
            )
        out[k] = str(v)
    return out


class RunRequest(BaseModel):
    runtime: str = Field(description="One of the keys in runtimes.RUNTIMES")
    files: dict[str, FileContent] = Field(
        default_factory=dict,
        description=(
            "Path → content map. On the first call for a session this is "
            "the initial file set. On a follow-up call (same session_id) "
            "it is an overlay — paths given here overwrite files in the "
            "running container, unlisted files are left alone. Values are "
            "either raw UTF-8 strings, or {\"encoding\": \"base64\", "
            "\"content\": \"...\"} for binary payloads (images, PDFs, "
            "wheels). Per-file cap: SANDBOX_MAX_FILE_BYTES; total cap: "
            "SANDBOX_MAX_PAYLOAD_BYTES."
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
    env: Optional[dict[str, str]] = Field(
        default=None,
        description=(
            "Process env vars set inside the container. Immutable after "
            "spawn (respawned self-heal replays the same env). Reserved "
            "keys HTTP_PROXY/HTTPS_PROXY/PYTHONUNBUFFERED/TERM/etc. are "
            "rejected — those are runner-controlled invariants."
        ),
    )
    recreate_if_gone: bool = Field(
        default=True,
        description=(
            "Backward-compat default for POST /run: silently respawn if "
            "the session's container is gone. Set false to require the "
            "caller to reason about self-heal explicitly."
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

    @field_validator("files")
    @classmethod
    def _validate_files(cls, v: dict) -> dict:
        return _validate_files_map(v)

    @field_validator("env")
    @classmethod
    def _validate_env_field(cls, v: Optional[dict]) -> Optional[dict[str, str]]:
        return _validate_env(v)


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
    # Health probe result, populated by the update path. Present on
    # every response so callers have a consistent shape; the fresh-spawn
    # response inlines the readiness probe result.
    app_status: Optional[dict] = None
    # Self-heal notice. Populated when the update path had to respawn
    # a container that was gone. None on the plain reuse or fresh spawn
    # paths.
    recreated: bool = False


class UpdateFilesRequest(BaseModel):
    files: dict[str, FileContent] = Field(
        default_factory=dict,
        description=(
            "Path → content overlay. Absent paths are preserved. Values "
            "are str (UTF-8) or {encoding: 'base64', content: '...'}."
        ),
    )
    deletes: list[str] = Field(
        default_factory=list,
        description="Relative paths under /app to delete.",
    )
    recreate_if_gone: bool = Field(
        default=False,
        description=(
            "If true and the session's container is gone, respawn a fresh "
            "container under the same session_id (files and packages "
            "installed via exec are LOST; env is preserved). Default "
            "false — caller must opt-in explicitly."
        ),
    )

    @field_validator("files")
    @classmethod
    def _validate_files(cls, v: dict) -> dict:
        return _validate_files_map(v)


class CreateRequest(BaseModel):
    runtime: str
    entrypoint: Optional[str] = None
    ttl_seconds: Optional[int] = None
    env: Optional[dict[str, str]] = None

    @field_validator("env")
    @classmethod
    def _validate_env_field(cls, v: Optional[dict]) -> Optional[dict[str, str]]:
        return _validate_env(v)


class ExecRequest(BaseModel):
    command: str = Field(description="Shell command (sh -c). Non-interactive.")
    timeout_seconds: Optional[int] = Field(default=None)
    working_dir: Optional[str] = Field(default=None)


class PatchSpec(BaseModel):
    """One hunk in a ``patch_files`` call — line-range strict replacement."""

    path: str = Field(
        description=(
            "Relative path under /app. Same validation as `deletes` — "
            "absolute paths and '..' rejected."
        ),
    )
    start_line: int = Field(
        description="1-indexed, inclusive start of the range to replace.",
        ge=1,
    )
    end_line: int = Field(
        description="1-indexed, inclusive end of the range to replace.",
        ge=1,
    )
    expected: str = Field(
        description=(
            "The EXACT current content of lines [start_line, end_line] "
            "joined with '\\n'. Byte-for-byte match required — no "
            "whitespace or trailing-newline lenience. If your view is "
            "stale, call get_files first."
        ),
    )
    replacement: str = Field(
        description=(
            "What to write in place of `expected`. May be shorter, "
            "longer, or the same length."
        ),
    )
    note: str = Field(
        default="",
        description=(
            "Optional free-text note logged for operator debugging. "
            "Not applied to the file."
        ),
    )


class PatchFilesRequest(BaseModel):
    patches: list[PatchSpec] = Field(
        description=(
            "List of patches. All-or-nothing: every patch validates first "
            "(byte-for-byte + no overlap on the same file); only then are "
            "any files modified."
        ),
    )
    recreate_if_gone: bool = Field(
        default=False,
        description=(
            "Accepted for interface consistency but has NO effect on this "
            "tool. patch_files depends on file content that would not "
            "exist in a fresh container, so a dead container always "
            "returns 409 regardless. Call update_files first to establish "
            "file state before reissuing patches."
        ),
    )

    @field_validator("patches")
    @classmethod
    def _validate_patches(cls, v: list[PatchSpec]) -> list[PatchSpec]:
        if not v:
            raise ValueError("patches must contain at least one patch")
        total = 0
        per_patch_over: list[tuple[int, str, int]] = []
        for i, p in enumerate(v):
            if p.end_line < p.start_line:
                raise ValueError(
                    f"patches[{i}]: end_line ({p.end_line}) must be >= "
                    f"start_line ({p.start_line})"
                )
            # Each patch contributes both expected and replacement to the
            # payload budget. Kept symmetric with `files`: bytes are UTF-8.
            expected_bytes = len(p.expected.encode("utf-8"))
            replacement_bytes = len(p.replacement.encode("utf-8"))
            patch_size = expected_bytes + replacement_bytes
            if patch_size > MAX_FILE_BYTES:
                per_patch_over.append((i, p.path, patch_size))
            total += patch_size
        if per_patch_over or total > MAX_PAYLOAD_BYTES:
            parts = ["Rejected: patch_files payload exceeded limits."]
            parts.append(f"  Individual patch cap: {MAX_FILE_BYTES:,} bytes")
            if per_patch_over:
                biggest = max(per_patch_over, key=lambda t: t[2])
                parts.append(
                    f"  (largest oversized: patch #{biggest[0] + 1} on "
                    f"{biggest[1]!r} at {biggest[2]:,} bytes; "
                    f"{len(per_patch_over)} patch(es) over the cap)"
                )
            parts.append(
                f"  Total payload cap: {MAX_PAYLOAD_BYTES:,} bytes "
                f"(submitted: {total:,} bytes)"
            )
            parts.append(
                "Shrink or split patches into multiple patch_files calls."
            )
            raise ValueError("\n".join(parts))
        return v


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

    Note the two-column read: ``session_id`` + ``expires_at`` + ``env``
    live in ``metadata`` (set at ``register`` time), but
    ``container_name`` + ``url`` live in ``result`` (set at
    ``set_result`` time). Both are populated by the time a job's phase
    reaches 'running', so a session lookup is safe to trust."""
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
        "runtime": meta.get("runtime"),
        "entrypoint": meta.get("entrypoint"),
        "ttl_seconds": meta.get("ttl_seconds"),
        "env": meta.get("env") or {},
    }


async def _list_running_sessions_rows() -> list[dict]:
    """Return every currently-running sandbox row. Backs ``list_sessions``
    and its HTTP counterpart. Includes every live row across ALL sessions
    globally — until per-user filtering lands, callers are expected to
    reason about global visibility."""
    if _registry is None:
        return []
    pool = _registry._pool
    if pool is None:
        return []
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, metadata, result, created_at, updated_at "
            "FROM jobs "
            "WHERE phase = 'running' "
            "ORDER BY created_at DESC"
        )
    out: list[dict] = []
    import json
    for r in rows:
        meta = r["metadata"]
        if isinstance(meta, str):
            meta = json.loads(meta)
        result = r["result"] or {}
        if isinstance(result, str):
            result = json.loads(result)
        out.append({
            "session_id": (meta or {}).get("session_id"),
            "sandbox_id": r["id"],
            "runtime": (meta or {}).get("runtime"),
            "url": result.get("url"),
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            "expires_at": (meta or {}).get("expires_at"),
            "last_used_at": (meta or {}).get("last_used_at"),
            "phase": "running",
        })
    return out


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _lint_python_files(files: dict[str, FileContent]) -> list[dict]:
    """Compile every ``.py`` file to catch SyntaxError before we spawn.

    Returns a list of ``{path, line, offset, message}`` entries — empty
    if everything parses. Uses Python's built-in ``compile()`` so we
    don't need a separate linter; catches every syntax error the
    interpreter would raise at import time, plus indentation errors.

    Deliberately does NOT run imports or execute code — the container
    is where that runs. Static-only, ~1 ms per file even for large
    Streamlit apps.

    Only lints TEXT (str) values — base64 blobs are not source. Even if
    a base64 payload decodes to Python, we treat it as an asset (the
    only way to send binary through the API); linting it would be a
    footgun for anyone shipping compiled resources.
    """
    out: list[dict] = []
    py_paths = [p for p in files if p.endswith(".py") and isinstance(files[p], str)]
    log.debug("_lint_python_files: checking %d .py file(s)", len(py_paths))
    for path in py_paths:
        content = files[path]
        display_name = f"<sandbox:{path}>"
        try:
            compile(content, display_name, "exec")
        except SyntaxError as exc:
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


# ── Core operations (used by both HTTP and MCP tools) ─────────────────────

async def _do_create(
    runtime: str,
    ttl_seconds: Optional[int],
    entrypoint: Optional[str],
    env: Optional[dict[str, str]],
) -> dict:
    """Spawn an empty (warming-files-only) container. Returns the same
    dict shape as ``_reuse_or_spawn`` so downstream renderers don't
    care which entry point they went through.
    """
    if _registry is None or _spawner is None or _slot_sem is None:
        raise HTTPException(500, "runner not initialized")

    try:
        rt = get_runtime(runtime)
    except KeyError:
        raise HTTPException(
            400,
            f"unknown runtime {runtime!r}. Valid: {sorted(RUNTIMES)}. "
            "Call get_runtimes for full descriptions.",
        )

    if not rt.allows_custom_entrypoint and entrypoint:
        raise HTTPException(
            400,
            f"the {runtime!r} runtime does not accept a custom entrypoint "
            f"(it uses a fixed process: {rt.default_entrypoint!r}). "
            "Remove the `entrypoint` field, or switch to runtime=python or "
            "runtime=node if you need to run a specific command.",
        )

    # Concurrency gate — matches the spawn path in _reuse_or_spawn.
    try:
        await asyncio.wait_for(_slot_sem.acquire(), timeout=0.1)
    except asyncio.TimeoutError:
        raise HTTPException(429, "sandbox pool exhausted")

    session_id = _new_session_id()
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
            "env": env or {},
            "warmed": True,
        },
        initial_phase="spawning",
    )
    log.info(
        "create: session=%s sandbox=%s runtime=%s ttl=%ds",
        session_id, sandbox_id, runtime, ttl,
    )
    try:
        result = await asyncio.to_thread(
            _spawner.spawn_empty, sandbox_id, rt, entrypoint, env
        )
        await _registry.set_phase(sandbox_id, "starting")
        ready = await asyncio.to_thread(
            _spawner.readiness_ok,
            result.container_name,
            rt.internal_port,
            rt.readiness_probe_path,
            30.0,
        )
        if not ready:
            logs = await asyncio.to_thread(
                _spawner.tail_logs, result.container_name, 100
            )
            await asyncio.to_thread(_spawner.stop, result.container_name)
            await _registry.set_error(
                sandbox_id,
                "sandbox did not become ready within 30s\n\n" + logs,
            )
            _slot_sem.release()
            raise HTTPException(
                504,
                {
                    "error": "sandbox did not become ready within 30s",
                    "session_id": session_id,
                    "logs": logs,
                    "hint": (
                        "The warming container failed to start. Read the "
                        "container logs above; likely a base-image or "
                        "runtime configuration issue (not caller code, since "
                        "create was called with no files). Try a different "
                        "runtime or contact the operator."
                    ),
                },
            )
        url = f"{PROXY_URL}/{sandbox_id}/"
        await _registry.set_result(
            sandbox_id,
            {"url": url, "container_name": result.container_name},
            phase="running",
        )
        startup_output = await asyncio.to_thread(
            _spawner.tail_logs, result.container_name, 40
        )
        log.info(
            "create READY: sandbox=%s session=%s url=%s expires=%s",
            sandbox_id, session_id, url, expires_at.isoformat(),
        )
        return {
            "sandbox_id": sandbox_id,
            "session_id": session_id,
            "url": url,
            "expires_at": expires_at.isoformat(),
            "reused": False,
            "runtime": runtime,
            "startup_output": startup_output,
            "app_status": {"code": 200, "latency_ms": 0, "note": "warming"},
            "recreated": False,
            "warming": True,
        }
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("create EXCEPTION for sandbox=%s: %s", sandbox_id, exc)
        await _registry.set_error(sandbox_id, str(exc))
        _slot_sem.release()
        raise HTTPException(500, f"spawn failed: {exc}")


async def _do_update_files(
    session_id: str,
    files: dict[str, FileContent],
    deletes: list[str],
    recreate_if_gone: bool,
) -> dict:
    """Overlay files into a live session's ``/app``. On a dead container,
    respawn only if the caller asked for it — otherwise return a 409 with
    the exact remediation the caller should take.

    Runs static Python lint on the overlay before touching Docker so a
    syntax error is caught in ~1 ms.
    """
    if _registry is None or _spawner is None or _slot_sem is None:
        raise HTTPException(500, "runner not initialized")
    if not _SESSION_ID_RE.match(session_id):
        raise HTTPException(400, "invalid session_id")

    lint_errors = _lint_python_files(files)
    if lint_errors:
        raise HTTPException(
            400,
            {
                "error": "static lint failed",
                "session_id": session_id,
                "errors": lint_errors,
                "hint": (
                    "Fix the syntax errors above and call update_files "
                    "again with the same session_id. Nothing was written "
                    "into the container."
                ),
            },
        )

    existing = await _find_running_session(session_id)
    if existing is None:
        raise HTTPException(
            404,
            {
                "error": "session not found",
                "session_id": session_id,
                "hint": (
                    "No running sandbox for this session_id. Call `create` "
                    "or `run` first."
                ),
            },
        )

    alive = await asyncio.to_thread(
        _spawner.container_exists, existing["container_name"]
    )
    if alive:
        return await _apply_files_to_running(existing, files, deletes)

    # Container is gone.
    if not recreate_if_gone:
        raise HTTPException(
            409,
            {
                "error": "sandbox container is gone",
                "session_id": session_id,
                "hint": (
                    "Sandbox is no longer running (container gone). To keep "
                    "working under this session_id, call update_files "
                    "again with recreate_if_gone=true. The container will "
                    "be respawned FRESH — packages installed via exec and "
                    "in-container files not in your current files map will "
                    "be LOST. Env vars set at create time ARE preserved."
                ),
            },
        )
    # Self-heal path.
    return await _respawn_session(existing, session_id, files, deletes)


async def _apply_files_to_running(
    existing: dict,
    files: dict[str, FileContent],
    deletes: list[str],
) -> dict:
    """Existing container overlay path (extracted from _reuse_or_spawn's
    reuse branch). Bumps last_used_at, tails logs, runs a health probe."""
    session_id = None
    if isinstance(existing.get("expires_at"), str) and _registry is not None:
        # Session id is stored on the registry row; fetch to get the pure
        # id for logging and last_used bumping.
        got = await _registry.get(existing["sandbox_id"])
        if got is not None:
            session_id = got.metadata.get("session_id")
    session_id = session_id or existing.get("session_id") or "?"

    log.info(
        "update path: session=%s sandbox=%s alive, overlaying %d file(s), "
        "removing %d",
        session_id, existing["sandbox_id"], len(files or {}), len(deletes or []),
    )
    await asyncio.to_thread(
        _spawner.write_reload_marker, existing["container_name"]
    )
    try:
        await asyncio.to_thread(
            _spawner.update_files,
            existing["container_name"],
            files,
            deletes,
        )
    except ValueError as exc:
        log.warning("update path: rejected unsafe path: %s", exc)
        raise HTTPException(400, str(exc))
    await _registry.update_metadata(
        existing["sandbox_id"],
        {"last_used_at": _now_iso()},
    )
    # Let the dev server notice the file change before we probe / tail.
    await asyncio.sleep(UPDATE_SETTLE_S)
    startup_output = await asyncio.to_thread(
        _spawner.tail_logs_since_last_marker,
        existing["container_name"],
        40,
    )
    # Look up the runtime so probe knows which probe-path/port to hit.
    rt_name = existing.get("runtime")
    probe_path = "/"
    if rt_name and rt_name in RUNTIMES:
        probe_path = RUNTIMES[rt_name].readiness_probe_path
    app_status = await asyncio.to_thread(
        _spawner.probe_health,
        existing["container_name"],
        80,
        probe_path,
        HEALTH_PROBE_TIMEOUT_S,
    )
    log.info(
        "update path complete: session=%s sandbox=%s reused=True app_status=%s",
        session_id, existing["sandbox_id"], app_status,
    )
    return {
        "sandbox_id": existing["sandbox_id"],
        "session_id": session_id,
        "url": existing["url"],
        "expires_at": existing["expires_at"],
        "reused": True,
        "runtime": rt_name,
        "startup_output": startup_output,
        "app_status": app_status,
        "recreated": False,
    }


async def _respawn_session(
    existing: dict,
    session_id: str,
    files: dict[str, FileContent],
    deletes: list[str],
) -> dict:
    """Container gone but caller asked to keep the session_id — full
    spawn under the same session_id, replaying env. Bumped ``recreated``
    flag on the response so the model can tell the user."""
    log.info(
        "self-heal: session=%s previous sandbox=%s gone, respawning",
        session_id, existing["sandbox_id"],
    )
    await _registry.set_phase(existing["sandbox_id"], "expired")

    # Reuse the previous runtime + env + entrypoint recorded in metadata.
    runtime = existing.get("runtime")
    if runtime is None or runtime not in RUNTIMES:
        raise HTTPException(
            500,
            {
                "error": "cannot self-heal: runtime metadata missing",
                "session_id": session_id,
                "hint": "Call create + update_files to start fresh.",
            },
        )
    result = await _reuse_or_spawn(
        runtime=runtime,
        files=files,
        entrypoint=existing.get("entrypoint"),
        ttl_seconds=existing.get("ttl_seconds"),
        session_id=session_id,
        deletes=deletes,
        env=existing.get("env") or {},
        recreate_if_gone=True,
    )
    result["recreated"] = True
    return result


async def _do_get_files(
    session_id: str,
    paths: Optional[list[str]],
    max_bytes_per_file: int,
) -> dict:
    if _registry is None or _spawner is None:
        raise HTTPException(500, "runner not initialized")
    if not _SESSION_ID_RE.match(session_id):
        raise HTTPException(400, "invalid session_id")
    existing = await _find_running_session(session_id)
    if existing is None or not existing.get("container_name"):
        raise HTTPException(
            404, f"no running sandbox for session {session_id!r}"
        )
    max_bytes = max(1, min(max_bytes_per_file, GET_FILES_HARD_CAP_BYTES))
    entries = await asyncio.to_thread(
        _spawner.read_files, existing["container_name"], paths, max_bytes,
    )
    return {
        "session_id": session_id,
        "sandbox_id": existing["sandbox_id"],
        "files": entries,
    }


async def _do_exec(
    session_id: str,
    command: str,
    timeout_seconds: int,
    working_dir: str,
) -> dict:
    if _registry is None or _spawner is None:
        raise HTTPException(500, "runner not initialized")
    if not _SESSION_ID_RE.match(session_id):
        raise HTTPException(400, "invalid session_id")
    if not command or not isinstance(command, str):
        raise HTTPException(400, "command must be a non-empty string")
    timeout = max(1, min(int(timeout_seconds or EXEC_DEFAULT_TIMEOUT_S), EXEC_HARD_TIMEOUT_S))
    workdir = working_dir or "/app"
    existing = await _find_running_session(session_id)
    if existing is None or not existing.get("container_name"):
        raise HTTPException(
            404,
            {
                "error": "no running sandbox for this session",
                "session_id": session_id,
                "hint": (
                    "exec requires a running container. If self-heal "
                    "should recreate one, call update_files first with "
                    "recreate_if_gone=true — but note that packages you "
                    "install via exec do NOT survive a respawn."
                ),
            },
        )
    try:
        res = await asyncio.to_thread(
            _spawner.exec_command,
            existing["container_name"],
            command,
            timeout,
            workdir,
            EXEC_MAX_OUTPUT_BYTES,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    # exec counts as activity — bump last_used_at so the reaper resets.
    await _registry.update_metadata(
        existing["sandbox_id"], {"last_used_at": _now_iso()},
    )
    return {
        "session_id": session_id,
        "sandbox_id": existing["sandbox_id"],
        "command": command,
        "exit_code": res.exit_code,
        "duration_ms": res.duration_ms,
        "output": res.output,
        "truncated": res.truncated,
        "timed_out": res.timed_out,
    }


async def _do_patch_files(
    session_id: str,
    patches: list[dict],
    recreate_if_gone: bool,
) -> dict:
    """Strict line-range replacement across one or more files.

    Two-pass model:
      1. Dry-run — every patch is validated (path safety, file exists,
         line range in bounds, expected byte-for-byte match, no overlap).
         If anything fails, no file is touched and the caller gets a
         structured 409 with the actual current content when applicable.
      2. Apply — bottom-up per file so earlier hunks' line indices stay
         valid. Then a single put_archive per file, followed by the same
         500 ms settle + HTTP health probe update_files uses.

    ``recreate_if_gone`` is accepted for interface consistency but does
    NOT trigger a respawn — a fresh container would not have the file
    the patch anchors on, so callers are told to establish state via
    update_files first.
    """
    if _registry is None or _spawner is None or _slot_sem is None:
        raise HTTPException(500, "runner not initialized")
    if not _SESSION_ID_RE.match(session_id):
        raise HTTPException(400, "invalid session_id")
    if not patches:
        raise HTTPException(400, "patches must contain at least one patch")

    existing = await _find_running_session(session_id)
    if existing is None:
        raise HTTPException(
            404,
            {
                "error": "session not found",
                "session_id": session_id,
                "hint": (
                    "No running sandbox for this session_id. Call `create` "
                    "or `run` first, then `update_files` to establish files, "
                    "then `patch_files` for targeted edits."
                ),
            },
        )
    alive = await asyncio.to_thread(
        _spawner.container_exists, existing["container_name"]
    )
    if not alive:
        # patch_files never respawns — a fresh container has no file to
        # anchor on. Signal 409 no matter what recreate_if_gone says.
        raise HTTPException(
            409,
            {
                "error": "sandbox container is gone",
                "session_id": session_id,
                "hint": (
                    "patch_files cannot respawn — it depends on files that "
                    "would not exist in a fresh container. Call "
                    "update_files first (with recreate_if_gone=true if you "
                    "want to force a respawn) to establish file state, then "
                    "reissue your patches."
                ),
            },
        )

    log.info(
        "patch_files: session=%s sandbox=%s n_patches=%d",
        session_id, existing["sandbox_id"], len(patches),
    )
    try:
        hunks, mismatch = await asyncio.to_thread(
            _spawner.patch_files,
            existing["container_name"],
            patches,
        )
    except ValueError as exc:
        # Path traversal etc. surfaced by _safe_relpath.
        log.warning("patch_files: rejected: %s", exc)
        raise HTTPException(400, str(exc))
    if mismatch is not None:
        log.info(
            "patch_files: rejected pre-apply — kind=%s path=%s patch=%s",
            mismatch.kind, mismatch.path, mismatch.patch_index,
        )
        detail = {
            "error": _PATCH_KIND_TO_ERROR.get(
                mismatch.kind, "patch_files validation failed",
            ),
            "session_id": session_id,
            "kind": mismatch.kind,
            "path": mismatch.path,
            "patch_index": mismatch.patch_index,
            "message": mismatch.message,
        }
        if mismatch.start_line is not None:
            detail["start_line"] = mismatch.start_line
            detail["end_line"] = mismatch.end_line
        if mismatch.file_line_count is not None:
            detail["file_line_count"] = mismatch.file_line_count
        if mismatch.expected is not None:
            detail["expected"] = mismatch.expected
            detail["actual"] = mismatch.actual
        if mismatch.other_start_line is not None:
            detail["other_start_line"] = mismatch.other_start_line
            detail["other_end_line"] = mismatch.other_end_line
            detail["other_patch_index"] = mismatch.other_index
        # Content mismatch and out-of-range are 409 (client-fixable state
        # divergence). Missing file, binary file, unsafe path are 4xx too
        # but carry different semantics; group them all under 409 to keep
        # the tool response shape consistent (the model reads the `kind`).
        detail["hint"] = _PATCH_HINT_FOR(mismatch.kind)
        raise HTTPException(409, detail)

    # Apply succeeded — bump last_used_at, settle, probe.
    await _registry.update_metadata(
        existing["sandbox_id"], {"last_used_at": _now_iso()},
    )
    await asyncio.sleep(UPDATE_SETTLE_S)
    rt_name = existing.get("runtime")
    probe_path = "/"
    if rt_name and rt_name in RUNTIMES:
        probe_path = RUNTIMES[rt_name].readiness_probe_path
    app_status = await asyncio.to_thread(
        _spawner.probe_health,
        existing["container_name"],
        80,
        probe_path,
        HEALTH_PROBE_TIMEOUT_S,
    )
    startup_output = await asyncio.to_thread(
        _spawner.tail_logs_since_last_marker,
        existing["container_name"],
        40,
    )
    hunks_out = [
        {
            "path": h.path,
            "start_line": h.start_line,
            "end_line": h.end_line,
            "replaced_bytes": h.replaced_bytes,
            "new_bytes": h.new_bytes,
        }
        for h in hunks
    ]
    files_touched = sorted({h["path"] for h in hunks_out})
    log.info(
        "patch_files complete: session=%s sandbox=%s files=%d hunks=%d "
        "app_status=%s",
        session_id, existing["sandbox_id"], len(files_touched),
        len(hunks_out), app_status,
    )
    return {
        "sandbox_id": existing["sandbox_id"],
        "session_id": session_id,
        "url": existing["url"],
        "expires_at": existing["expires_at"],
        "runtime": rt_name,
        "hunks_applied": hunks_out,
        "files_touched": files_touched,
        "startup_output": startup_output,
        "app_status": app_status,
        "recreated": False,
    }


# Mapping from PatchMismatch.kind to the `error` string surfaced in the
# structured tool response. Kept separate so sandbox_mcp can pattern-match
# without importing the dataclass.
_PATCH_KIND_TO_ERROR = {
    "unsafe_path": "unsafe path in patch",
    "missing_file": "target file does not exist",
    "binary_file": "cannot patch non-UTF-8 file",
    "out_of_range": "line range out of bounds",
    "content_mismatch": "expected content mismatch",
    "overlap": "overlapping patches on same file",
    "bad_expected_type": "expected must be a string",
    "bad_replacement_type": "replacement must be a string",
}


def _PATCH_HINT_FOR(kind: str) -> str:
    if kind == "missing_file":
        return (
            "patch_files does not create files. Call update_files with "
            "the file's full content to create it first."
        )
    if kind == "binary_file":
        return (
            "The target file is not UTF-8 (contains a null byte or "
            "invalid encoding). Use update_files with a base64 payload "
            "to overwrite it entirely."
        )
    if kind == "out_of_range":
        return (
            "Call get_files(paths=[<path>]) to refresh your view of the "
            "file, then reissue patch_files with the correct line range. "
            "No files were modified."
        )
    if kind == "content_mismatch":
        return (
            "The file has changed since you last read it. Call get_files "
            "with the affected path, copy the current bytes into "
            "`expected`, and reissue patch_files. No files were modified."
        )
    if kind == "overlap":
        return (
            "Combine the overlapping patches into ONE patch whose "
            "`expected` and `replacement` cover the merged range. No "
            "files were modified."
        )
    if kind == "unsafe_path":
        return (
            "Paths must be relative under /app. '..' and absolute paths "
            "are rejected. No files were modified."
        )
    return "Fix the issue above and reissue patch_files."


async def _do_preview(session_id: str) -> dict:
    if _registry is None:
        raise HTTPException(500, "runner not initialized")
    if not _SESSION_ID_RE.match(session_id):
        raise HTTPException(400, "invalid session_id")
    existing = await _find_running_session(session_id)
    if existing is None or not existing.get("url"):
        raise HTTPException(
            404,
            {
                "error": "no running sandbox for this session",
                "session_id": session_id,
                "hint": (
                    "preview is display-only and does not respawn. If the "
                    "container died, call update_files with "
                    "recreate_if_gone=true first, then call preview again."
                ),
            },
        )
    return {
        "session_id": session_id,
        "sandbox_id": existing["sandbox_id"],
        "url": existing["url"],
        "expires_at": existing["expires_at"],
        "runtime": existing.get("runtime"),
    }


async def _do_close(session_id: str) -> dict:
    if _registry is None or _spawner is None or _slot_sem is None:
        raise HTTPException(500, "runner not initialized")
    if not _SESSION_ID_RE.match(session_id):
        raise HTTPException(400, "invalid session_id")
    existing = await _find_running_session(session_id)
    if existing is None:
        return {
            "session_id": session_id,
            "was_running": False,
            "note": "already closed or never existed",
        }
    if existing["container_name"]:
        await asyncio.to_thread(_spawner.stop, existing["container_name"])
    await _registry.set_phase(existing["sandbox_id"], "closed")
    _slot_sem.release()
    log.info(
        "close: session=%s sandbox=%s slot released",
        session_id, existing["sandbox_id"],
    )
    return {
        "session_id": session_id,
        "sandbox_id": existing["sandbox_id"],
        "was_running": True,
    }


async def _do_list_sessions() -> dict:
    rows = await _list_running_sessions_rows()
    return {"count": len(rows), "sessions": rows}


async def _reuse_or_spawn(
    runtime: str,
    files: dict[str, FileContent],
    entrypoint: Optional[str],
    ttl_seconds: Optional[int],
    session_id: Optional[str],
    deletes: list[str],
    env: Optional[dict[str, str]] = None,
    recreate_if_gone: bool = True,
) -> dict:
    """Session-aware entry point retained for HTTP compatibility.

    Two branches:
      * ``session_id`` names a live container → overlay + probe. Same shape
        as ``_do_update_files`` but returns the ``run`` response schema.
      * No session_id, OR the session's container is gone AND
        ``recreate_if_gone`` is true → full spawn.

    Kept as a single function so ``POST /run`` and the ``run`` MCP tool
    still work exactly as they did before the redesign.
    """
    log.info(
        "_reuse_or_spawn: runtime=%s session_id=%s n_files=%d n_deletes=%d "
        "entrypoint=%r recreate=%s env_keys=%s",
        runtime, session_id, len(files or {}), len(deletes or []), entrypoint,
        recreate_if_gone, sorted((env or {}).keys()),
    )
    if _registry is None or _spawner is None or _slot_sem is None:
        raise HTTPException(500, "runner not initialized")

    if session_id:
        existing = await _find_running_session(session_id)
        if existing and existing["container_name"]:
            alive = await asyncio.to_thread(
                _spawner.container_exists, existing["container_name"]
            )
            if alive:
                # Session reuse: overlay + probe path.
                return await _apply_files_to_running(existing, files, deletes)
            # Container gone. Respect the caller's recreate_if_gone flag.
            if not recreate_if_gone:
                raise HTTPException(
                    409,
                    {
                        "error": "sandbox container is gone",
                        "session_id": session_id,
                        "hint": (
                            "Container was reaped or crashed. Retry with "
                            "recreate_if_gone=true to respawn under this "
                            "session_id (env preserved; in-container files "
                            "and exec-installed packages LOST)."
                        ),
                    },
                )
            log.info(
                "self-heal: session=%s sandbox=%s gone, respawning under same "
                "session_id",
                session_id, existing["sandbox_id"],
            )
            await _registry.set_phase(existing["sandbox_id"], "expired")
            # Preserve original env if the caller didn't supply new env.
            if env is None:
                env = existing.get("env") or {}

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
            "Call get_runtimes for full descriptions.",
        )

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

    lint_errors = _lint_python_files(files)
    if lint_errors:
        log.warning(
            "static lint failed for session=%s: %d error(s) across %d file(s)",
            session_id, len(lint_errors),
            len({e["path"] for e in lint_errors}),
        )
        raise HTTPException(
            400,
            {
                "error": "static lint failed",
                "session_id": session_id,
                "errors": lint_errors,
                "hint": (
                    "Fix the syntax errors above and call again with the "
                    "same session_id. No container was spawned."
                ),
            },
        )

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
            "env": env or {},
        },
        initial_phase="spawning",
    )

    log.info(
        "spawn path: session=%s sandbox=%s runtime=%s ttl=%ds entrypoint=%r",
        session_id, sandbox_id, runtime, ttl, entrypoint or rt.default_entrypoint,
    )

    try:
        result = await asyncio.to_thread(
            _spawner.spawn, sandbox_id, rt, files, entrypoint, env
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
            30.0,
        )
        if not ready:
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
            _slot_sem.release()
            raise HTTPException(
                504,
                {
                    "error": "sandbox did not become ready within 30s",
                    "session_id": session_id,
                    "logs": logs,
                    "hint": (
                        "Read the container logs above to see why the app "
                        "failed to start (traceback, missing module, port "
                        "bind error, etc.). Fix the code and call again "
                        "with the same session_id — the runner will spawn "
                        "a fresh container."
                    ),
                },
            )

        url = f"{PROXY_URL}/{sandbox_id}/"
        await _registry.set_result(
            sandbox_id,
            {"url": url, "container_name": result.container_name},
            phase="running",
        )
        startup_output = await asyncio.to_thread(
            _spawner.tail_logs, result.container_name, 40
        )
        # Fresh spawn — readiness_ok already confirmed a live 2xx-4xx,
        # so app_status is trivially healthy. Encode as the same probe
        # shape the update path uses so callers see a stable field.
        log.info(
            "spawn READY: sandbox=%s session=%s url=%s expires=%s "
            "startup_output_bytes=%d",
            sandbox_id, session_id, url, expires_at.isoformat(),
            len(startup_output),
        )
        return {
            "sandbox_id": sandbox_id,
            "session_id": session_id,
            "url": url,
            "expires_at": expires_at.isoformat(),
            "reused": False,
            "runtime": runtime,
            "startup_output": startup_output,
            "app_status": {"code": 200, "latency_ms": 0, "note": "readiness ok"},
            "recreated": False,
        }
    except HTTPException:
        raise
    except Exception as exc:
        log.exception(
            "spawn EXCEPTION for sandbox=%s session=%s: %s",
            sandbox_id, session_id, exc,
        )
        await _registry.set_error(sandbox_id, str(exc))
        _slot_sem.release()
        raise HTTPException(500, f"spawn failed: {exc}")


# ── FastAPI endpoints ─────────────────────────────────────────────────────
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
async def session_update_files(
    session_id: str, req: UpdateFilesRequest,
) -> RunResponse:
    result = await _do_update_files(
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


@app.delete("/session/{session_id}", status_code=204)
async def delete_session(session_id: str) -> None:
    """Explicitly tear down a session's running sandbox. Idempotent —
    a session that never existed or has already expired still returns
    204 so the model doesn't have to remember state to clean up."""
    if not _SESSION_ID_RE.match(session_id):
        log.warning("DELETE /session: invalid session_id: %r", session_id)
        raise HTTPException(400, "invalid session_id")
    await _do_close(session_id)


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
    slot_released = got.phase in ("running", "starting", "spawning")
    if slot_released:
        _slot_sem.release()
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
    if _registry is None or _spawner is None:
        raise HTTPException(500, "runner not initialized")
    job = await _registry.get(sandbox_id)
    if job is None:
        raise HTTPException(404, f"no sandbox {sandbox_id!r}")
    container_name = (job.result or {}).get("container_name")
    if not container_name:
        raise HTTPException(
            409, f"sandbox {sandbox_id!r} has no container to download"
        )
    try:
        stream = await asyncio.to_thread(
            _spawner.export_files, container_name
        )
    except Exception as exc:
        raise HTTPException(404, f"container gone: {exc}")
    return _tar_response(stream, sandbox_id)


@app.get("/session/{session_id}/logs")
async def logs_session(session_id: str, lines: int = 100) -> dict:
    """Return the last ``lines`` of the running sandbox's combined
    stdout+stderr. Session-based so this survives self-heal spawns."""
    if not _SESSION_ID_RE.match(session_id):
        raise HTTPException(400, "invalid session_id")
    if _registry is None or _spawner is None:
        raise HTTPException(500, "runner not initialized")
    existing = await _find_running_session(session_id)
    if existing is None or not existing.get("container_name"):
        raise HTTPException(
            404, f"no running sandbox for session {session_id!r}"
        )
    clamped = max(1, min(lines, 1000))
    text = await asyncio.to_thread(
        _spawner.tail_logs, existing["container_name"], clamped
    )
    return {
        "session_id": session_id,
        "sandbox_id": existing["sandbox_id"],
        "lines_requested": lines,
        "logs": text,
    }


@app.get("/jobs/{sandbox_id}/logs")
async def logs_sandbox_job(sandbox_id: str, lines: int = 100) -> dict:
    if _registry is None or _spawner is None:
        raise HTTPException(500, "runner not initialized")
    job = await _registry.get(sandbox_id)
    if job is None:
        raise HTTPException(404, f"no sandbox {sandbox_id!r}")
    container_name = (job.result or {}).get("container_name")
    if not container_name:
        raise HTTPException(
            409, f"sandbox {sandbox_id!r} has no container to read logs from"
        )
    clamped = max(1, min(lines, 1000))
    text = await asyncio.to_thread(
        _spawner.tail_logs, container_name, clamped
    )
    return {
        "sandbox_id": sandbox_id,
        "lines_requested": lines,
        "logs": text,
    }


@app.get("/session/{session_id}/download")
async def download_session(session_id: str) -> StreamingResponse:
    if not _SESSION_ID_RE.match(session_id):
        raise HTTPException(400, "invalid session_id")
    if _registry is None or _spawner is None:
        raise HTTPException(500, "runner not initialized")
    existing = await _find_running_session(session_id)
    if existing is None or not existing.get("container_name"):
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
app.mount("/mcp", _mcp_app)


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

tool_app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)


class ToolRunRequest(BaseModel):
    runtime: str = Field(
        description="One of 'static', 'python', 'node'."
    )
    files: dict[str, FileContent] = Field(
        default_factory=dict,
        description=(
            "Map of relative path → content. Values are UTF-8 str or "
            "{encoding:'base64', content:'...'} for binary. On follow-up "
            "with the same session_id, only send changed files."
        ),
    )
    entrypoint: Optional[str] = Field(default=None)
    ttl_seconds: Optional[int] = Field(default=None)
    session_id: Optional[str] = Field(default=None)
    deletes: list[str] = Field(default_factory=list)
    env: Optional[dict[str, str]] = Field(default=None)

    @field_validator("files")
    @classmethod
    def _validate_files(cls, v: dict) -> dict:
        return _validate_files_map(v)

    @field_validator("env")
    @classmethod
    def _validate_env_field(cls, v: Optional[dict]) -> Optional[dict[str, str]]:
        return _validate_env(v)


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
    if req.session_id and not _SESSION_ID_RE.match(req.session_id):
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


app.mount("/tool", tool_app)
