"""Core operations shared between HTTP endpoints and MCP tools.

Every ``_do_*`` function raises ``HTTPException`` on failure and returns
a plain dict on success — FastAPI's endpoint handlers, the FastMCP tool
handlers, and the OpenWebUI Tool Server all consume the same dicts.

All Postgres and Docker state is read via the ``state`` module — no
direct imports of the singletons from ``app.py``, so this module sits
below ``app.py`` in the import graph.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException
from pydantic import BaseModel, ValidationError

from constants import (
    BROWSER_LOG_RATE_LIMIT_PER_MIN,
    CHAT_ID_RE,
    DEFAULT_TTL_S,
    EDIT_MAX_LISTED_MATCHES,
    EDIT_SNIPPET_CONTEXT_LINES,
    EDIT_SNIPPET_MAX_LINES,
    EXEC_DEFAULT_TIMEOUT_S,
    EXEC_HARD_TIMEOUT_S,
    EXEC_MAX_OUTPUT_BYTES,
    GET_FILES_HARD_CAP_BYTES,
    HARD_TTL_S,
    HEALTH_PROBE_TIMEOUT_S,
    MAX_FILE_BYTES,
    PROXY_URL,
    READ_FILE_DEFAULT_LINES,
    READ_FILE_MAX_BYTES,
    READ_FILE_MAX_LINE_CHARS,
    READ_FILE_MAX_LINES,
    SESSION_ID_RE,
    UPDATE_SETTLE_S,
    WRITE_GUARD_MIN_LINES,
    WRITE_GUARD_SIMILARITY,
)
from models import FileContent
from common.textops import (
    FileOpError,
    RewriteVerdict,
    apply_edit,
    decode_utf8_prefix,
    looks_binary,
    number_lines,
    rewrite_guard,
    split_lines,
)
from runtimes import RUNTIMES, get_runtime

import state


log = logging.getLogger("sandbox-runner.operations")


# asyncpg raises UniqueViolationError when the partial UNIQUE index
# ``jobs_chat_id_active_uniq`` rejects a second active row for one chat_id —
# that's the atomic backstop the one-sandbox-per-chat guard relies on. Import
# defensively so this module still imports in unit environments where asyncpg
# isn't installed (the real runner always has it).
try:  # pragma: no cover - trivial import shim
    from asyncpg.exceptions import UniqueViolationError as _UniqueViolationError
except Exception:  # asyncpg missing/stubbed
    class _UniqueViolationError(Exception):
        """Fallback so ``except _UniqueViolationError`` is always valid."""


# ── Browser-log rate limiter (module-scoped, in-process) ─────────────────
# Keyed by sandbox_id. Each entry is a deque of monotonic timestamps
# (seconds). We prune the head on every ingest so the window slides.
#
# Kept in-process because:
#   * Ingest is best-effort — a runner restart can drop the counter
#     with no user-visible consequence beyond a briefly higher rate.
#   * Cross-process rate limiting (Redis, DB) would drag another
#     dependency in for a debug-only feature.
#
# The dict is bounded implicitly by SANDBOX_MAX_CONCURRENT (there's
# only ever a handful of live sandbox_ids). Reaper teardown does not
# purge the dict eagerly — dead sandbox_ids have their deques emptied
# on the next ingest attempt (which will 404 anyway) and eventually
# GC'd when the process restarts. If this ever needs eviction, add a
# TTL sweep here.
_browser_log_windows: dict[str, deque] = {}
_BROWSER_LOG_WINDOW_S = 60.0


# ── Small utilities ───────────────────────────────────────────────────────

def _new_session_id() -> str:
    """URL-safe 12-char session id. Enough entropy to avoid collisions
    across concurrent chats without dragging a UUID through log lines."""
    sid = secrets.token_urlsafe(9)  # 9 bytes → 12 base64 chars
    log.debug("generated new session_id: %s", sid)
    return sid


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_tool_args(
    model_cls: type[BaseModel],
    tool: str,
    sid: Optional[str] = None,
    /,
    **fields,
):
    """Build ``model_cls(**fields)`` — the same pydantic model the HTTP
    route validates with — for an MCP adapter. ``sid`` (positional-only,
    so a model field may also be called ``session_id``) is only echoed
    into the error detail.

    Without it an MCP tool would hand raw dicts straight to ``_do_*``
    and skip every model validator: the per-file / payload size caps
    (``_validate_files_map``), reserved env keys, the session/chat id
    regexes. A failure becomes a structured ``HTTPException`` the MCP layer renders:
    413 ``payload too large`` for a size cap, 422 ``invalid arguments``
    for anything else.
    """
    try:
        return model_cls(**fields)
    except ValidationError as exc:
        parts: list[str] = []
        for err in exc.errors():
            loc = ".".join(str(p) for p in err.get("loc", ()))
            msg = str(err.get("msg", "")).removeprefix("Value error, ")
            parts.append(f"{loc}: {msg}" if loc else msg)
        message = "\n".join(parts) or str(exc)
        too_large = (
            "exceeded limits" in message or "exceeds the per-file cap" in message
        )
        log.info(
            "%s: argument validation failed (too_large=%s): %s",
            tool, too_large, message.replace("\n", " | "),
        )
        detail = {
            "error": "payload too large" if too_large else "invalid arguments",
            "tool": tool,
            "message": message,
            "hint": (
                "Nothing was written. Send smaller files, or split them "
                "across several calls."
                if too_large else
                f"Fix the arguments above and call {tool} again."
            ),
        }
        if sid:
            detail["session_id"] = sid
        raise HTTPException(413 if too_large else 422, detail) from exc


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


# ── Session lookup helpers ────────────────────────────────────────────────

def _active_pool(where: str):
    """Return the registry's asyncpg pool, or ``None`` (logging why) when the
    registry or its pool isn't ready yet. Shared guard for every raw-SQL
    helper below so the two-line null-check isn't copied at each call site."""
    if state.registry is None:
        log.warning("%s called before registry init", where)
        return None
    pool = state.registry._pool
    if pool is None:
        log.warning("%s: pool not ready", where)
    return pool


def _decode_row(row) -> tuple[dict, dict]:
    """Split a ``jobs`` row into ``(metadata, result)`` dicts, tolerating the
    JSONB columns arriving as ``str`` (some asyncpg codec setups) or as an
    already-decoded ``dict``. ``result`` defaults to ``{}`` when NULL."""
    meta = row["metadata"]
    if isinstance(meta, str):
        meta = json.loads(meta)
    result = row["result"] or {}
    if isinstance(result, str):
        result = json.loads(result)
    return meta or {}, result


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
    pool = _active_pool("_find_running_session")
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
        log.debug("_find_running_session: no running row for %s", session_id)
        return None
    meta, result = _decode_row(row)
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
        "chat_id": meta.get("chat_id"),
    }


# Phases a sandbox passes through before it's either serving or dead. A chat
# "already has a create" if it has a row in any of these — including the brief
# spawning/starting window, so two near-simultaneous creates for one chat don't
# both slip past the guard and spawn duplicate containers.
_ACTIVE_PHASES = ("spawning", "starting", "running")


async def _find_active_session_by_chat(chat_id: str) -> Optional[dict]:
    """Return the newest not-yet-dead sandbox for a chat_id, or None.

    Backs the "one create per chat" guard. Unlike ``_find_running_session``
    (which matches only phase='running' because it's about reusing a live
    container), this also matches the spawning/starting window so a duplicate
    create issued while the first is still warming is caught too.

    ``container_name`` / ``url`` come from ``result`` and are populated only
    once the row reaches 'running'; for a still-warming row they're None and
    callers fall back to the deterministic ``{PROXY_URL}/{sandbox_id}/`` URL.
    """
    pool = _active_pool("_find_active_session_by_chat")
    if pool is None:
        return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, phase, metadata, result "
            "FROM jobs "
            "WHERE metadata->>'chat_id' = $1 AND phase = ANY($2::text[]) "
            "ORDER BY created_at DESC LIMIT 1",
            chat_id,
            list(_ACTIVE_PHASES),
        )
    if row is None:
        log.debug("_find_active_session_by_chat: no active row for chat=%s", chat_id)
        return None
    meta, result = _decode_row(row)
    log.debug(
        "_find_active_session_by_chat: chat=%s → sandbox=%s phase=%s session=%s",
        chat_id, row["id"], row["phase"], meta.get("session_id"),
    )
    return {
        "sandbox_id": row["id"],
        "phase": row["phase"],
        "session_id": meta.get("session_id"),
        "container_name": result.get("container_name"),
        "url": result.get("url"),
        "expires_at": meta.get("expires_at"),
        "runtime": meta.get("runtime"),
        "entrypoint": meta.get("entrypoint"),
        "ttl_seconds": meta.get("ttl_seconds"),
        "env": meta.get("env") or {},
    }


def _existing_session_as_create_response(existing: dict, chat_id: str) -> dict:
    """Shape an already-live session into the ``create``/``run`` response dict,
    flagged so the caller can tell the user a duplicate spawn was avoided.

    URL falls back to the deterministic proxy path when the row is still
    warming (its ``result`` isn't written yet)."""
    url = existing.get("url") or f"{PROXY_URL}/{existing['sandbox_id']}/"
    return {
        "sandbox_id": existing["sandbox_id"],
        "session_id": existing.get("session_id"),
        "url": url,
        "expires_at": existing.get("expires_at"),
        "reused": True,
        "runtime": existing.get("runtime"),
        "startup_output": "",
        "app_status": {
            "code": 200,
            "latency_ms": 0,
            "note": f"existing sandbox for chat (phase={existing.get('phase')})",
        },
        "recreated": False,
        "warming": existing.get("phase") in ("spawning", "starting"),
        "duplicate_create_prevented": True,
        "chat_id": chat_id,
    }


async def _list_running_sessions_rows() -> list[dict]:
    """Return every currently-running sandbox row. Backs ``list_sessions``
    and its HTTP counterpart. Includes every live row across ALL sessions
    globally — until per-user filtering lands, callers are expected to
    reason about global visibility."""
    pool = _active_pool("_list_running_sessions_rows")
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
    for r in rows:
        meta, result = _decode_row(r)
        out.append({
            "session_id": meta.get("session_id"),
            "sandbox_id": r["id"],
            "runtime": meta.get("runtime"),
            "url": result.get("url"),
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            "expires_at": meta.get("expires_at"),
            "last_used_at": meta.get("last_used_at"),
            "phase": "running",
        })
    return out


# ── Spawn helpers shared by _do_create and _reuse_or_spawn ────────────────

def _resolve_runtime(runtime: str, entrypoint: Optional[str]):
    """Validate the runtime name and the custom-entrypoint rule, returning the
    runtime spec. Raises ``HTTPException(400)`` with the same messages for
    both spawn paths."""
    try:
        rt = get_runtime(runtime)
    except KeyError:
        raise HTTPException(
            400,
            f"unknown runtime {runtime!r}. Valid: {sorted(RUNTIMES)}. "
            "Call get_runtime_types for full descriptions.",
        )
    if not rt.allows_custom_entrypoint and entrypoint:
        raise HTTPException(
            400,
            f"the {runtime!r} runtime does not accept a custom entrypoint "
            f"(it uses a fixed process: {rt.default_entrypoint!r}). "
            "Remove the `entrypoint` field, or switch to runtime=python or "
            "runtime=node if you need to run a specific command.",
        )
    return rt


class _LostRace(Exception):
    """Raised by ``_reserve_active_slot`` when a concurrent create for the same
    chat won the one-sandbox-per-chat UNIQUE-index race. ``winner`` is that
    chat's existing active session so the caller can route onto it."""

    def __init__(self, winner: dict) -> None:
        self.winner = winner
        super().__init__("lost the chat spawn race")


async def _reserve_active_slot(reserve_meta: dict, chat_id: Optional[str]) -> str:
    """Take a concurrency slot and atomically insert the chat's active job row.

    Returns the new ``sandbox_id``. The partial UNIQUE index
    ``jobs_chat_id_active_uniq`` is the arbiter: if a concurrent create for the
    same chat already inserted its active row, ``register()`` raises
    ``UniqueViolationError`` and we raise ``_LostRace(winner)`` so the caller
    hands back the winner instead of spawning a rival. The slot is released on
    a lost race, so a prevented duplicate costs nothing.

    Raises ``HTTPException(429)`` when the pool is exhausted, or ``(409)`` if a
    violation keeps firing but the winner vanishes on both attempts.
    """
    for _attempt in range(2):
        try:
            await asyncio.wait_for(state.slot_sem.acquire(), timeout=0.1)
        except asyncio.TimeoutError:
            raise HTTPException(429, "sandbox pool exhausted")
        try:
            return await state.registry.register(
                reserve_meta, initial_phase="spawning"
            )
        except _UniqueViolationError:
            state.slot_sem.release()
            winner = (
                await _find_active_session_by_chat(chat_id) if chat_id else None
            )
            if winner is not None:
                raise _LostRace(winner)
            # The winner was torn down in the same instant it won — retry once.
            log.warning(
                "chat=%s hit a unique-violation but found no active row; "
                "retrying reservation once",
                chat_id,
            )
    raise HTTPException(
        409,
        {
            "error": "could not reserve a sandbox for this chat — retry",
            "chat_id": chat_id,
        },
    )


async def _await_ready_or_teardown(
    sandbox_id: str,
    container_name: str,
    rt,
    session_id: str,
    *,
    hint: str,
) -> None:
    """Mark the sandbox ``starting`` and poll readiness for 30s. On success,
    return (the caller writes the result). On failure, capture logs, stop the
    container, mark the job errored, release the slot, and raise
    ``HTTPException(504)`` with the caller-supplied ``hint``. Shared by both
    spawn paths so the teardown-on-timeout sequence lives in one place."""
    await state.registry.set_phase(sandbox_id, "starting")
    ready = await asyncio.to_thread(
        state.spawner.readiness_ok,
        container_name,
        rt.internal_port,
        rt.readiness_probe_path,
        30.0,
    )
    if ready:
        return
    log.warning(
        "readiness FAILED: sandbox=%s container=%s did not bind port %d "
        "within 30s, capturing logs before teardown",
        sandbox_id, container_name, rt.internal_port,
    )
    logs = await asyncio.to_thread(state.spawner.tail_logs, container_name, 100)
    await asyncio.to_thread(state.spawner.stop, container_name)
    await state.registry.set_error(
        sandbox_id, "sandbox did not become ready within 30s\n\n" + logs
    )
    state.slot_sem.release()
    raise HTTPException(
        504,
        {
            "error": "sandbox did not become ready within 30s",
            "session_id": session_id,
            "logs": logs,
            "hint": hint,
        },
    )


# ── Core operations (used by both HTTP and MCP tools) ─────────────────────

async def _do_create(
    runtime: str,
    ttl_seconds: Optional[int],
    entrypoint: Optional[str],
    env: Optional[dict[str, str]],
    chat_id: Optional[str] = None,
) -> dict:
    """Spawn an empty (warming-files-only) container. Returns the same
    dict shape as ``_reuse_or_spawn`` so downstream renderers don't
    care which entry point they went through.

    ``chat_id`` scopes one create per conversation: if the chat already
    has an active sandbox, no new container is spawned — the existing
    session is returned with ``duplicate_create_prevented=true`` and no
    slot is consumed.
    """
    if state.registry is None or state.spawner is None or state.slot_sem is None:
        raise HTTPException(500, "runner not initialized")

    # One-create-per-chat guard. Runs BEFORE the concurrency slot is taken so a
    # duplicate create is free and never counts against SANDBOX_MAX_CONCURRENT.
    if chat_id:
        existing = await _find_active_session_by_chat(chat_id)
        if existing:
            log.info(
                "create: chat=%s already has sandbox=%s (phase=%s) — returning "
                "it instead of spawning a duplicate",
                chat_id, existing["sandbox_id"], existing.get("phase"),
            )
            return _existing_session_as_create_response(existing, chat_id)

    rt = _resolve_runtime(runtime, entrypoint)

    session_id = _new_session_id()
    ttl = min(ttl_seconds or DEFAULT_TTL_S, HARD_TTL_S)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl)
    now_iso = _now_iso()
    reserve_meta = {
        "runtime": runtime,
        "entrypoint": entrypoint or rt.default_entrypoint,
        "ttl_seconds": ttl,
        "expires_at": expires_at.isoformat(),
        "session_id": session_id,
        "chat_id": chat_id,
        "last_used_at": now_iso,
        "env": env or {},
        "warmed": True,
    }

    # Atomically reserve the chat's one active slot. The pre-check above catches
    # the common "created twice, seconds apart" case cheaply; the UNIQUE-index
    # backstop inside _reserve_active_slot handles the sub-millisecond race
    # where two creates both pass the SELECT — the loser raises _LostRace and we
    # hand back the winner instead of spawning a second container.
    try:
        sandbox_id = await _reserve_active_slot(reserve_meta, chat_id)
    except _LostRace as lost:
        log.info(
            "create: chat=%s lost the spawn race to sandbox=%s — returning it "
            "instead of spawning a duplicate",
            chat_id, lost.winner["sandbox_id"],
        )
        return _existing_session_as_create_response(lost.winner, chat_id)
    log.info(
        "create: session=%s sandbox=%s runtime=%s ttl=%ds chat=%s",
        session_id, sandbox_id, runtime, ttl, chat_id,
    )
    try:
        result = await asyncio.to_thread(
            state.spawner.spawn_empty, sandbox_id, rt, entrypoint, env
        )
        await _await_ready_or_teardown(
            sandbox_id, result.container_name, rt, session_id,
            hint=(
                "The warming container failed to start. Read the container "
                "logs above; likely a base-image or runtime configuration "
                "issue (not caller code, since create was called with no "
                "files). Try a different runtime or contact the operator."
            ),
        )
        url = f"{PROXY_URL}/{sandbox_id}/"
        await state.registry.set_result(
            sandbox_id,
            {"url": url, "container_name": result.container_name},
            phase="running",
        )
        startup_output = await asyncio.to_thread(
            state.spawner.tail_logs, result.container_name, 40
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
            "duplicate_create_prevented": False,
            "chat_id": chat_id,
        }
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("create EXCEPTION for sandbox=%s: %s", sandbox_id, exc)
        await state.registry.set_error(sandbox_id, str(exc))
        state.slot_sem.release()
        raise HTTPException(500, f"spawn failed: {exc}")


async def _do_write_files(
    session_id: str,
    files: dict[str, FileContent],
    deletes: list[str],
    recreate_if_gone: bool,
    overwrite: bool = False,
) -> dict:
    """Overlay files into a live session's ``/app``. On a dead container,
    respawn only if the caller asked for it — otherwise return a 409 with
    the exact remediation the caller should take.

    Runs static Python lint on the overlay before touching Docker so a
    syntax error is caught in ~1 ms. Unless ``overwrite``, a write that
    would re-send a mostly-unchanged existing file is refused with a 409
    ``rewrite_of_existing_file`` (see ``_check_rewrite_guard``).
    """
    if state.registry is None or state.spawner is None or state.slot_sem is None:
        raise HTTPException(500, "runner not initialized")
    if not SESSION_ID_RE.match(session_id):
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
                    "Fix the syntax errors above. The lint runs before "
                    "any container write, so this failure did not touch "
                    "/app.\n\n"
                    "If the file already exists in the sandbox, fix just "
                    "the broken lines with edit_file (same session_id): "
                    "read_file(path, offset=<line - 5>, limit=20) shows the "
                    "numbered lines, then copy the bad text into "
                    "old_string and your fix into new_string. Re-send the "
                    "whole file with write_files only when it is new."
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
        state.spawner.container_exists, existing["container_name"]
    )
    if alive:
        return await _apply_files_to_running(
            existing, files, deletes, overwrite=overwrite,
        )

    # Container is gone.
    if not recreate_if_gone:
        raise HTTPException(
            409,
            {
                "error": "sandbox container is gone",
                "session_id": session_id,
                "hint": (
                    "Sandbox is no longer running (container gone). To keep "
                    "working under this session_id, call write_files "
                    "again with recreate_if_gone=true. The container will "
                    "be respawned FRESH — packages installed via exec and "
                    "in-container files not in your current files map will "
                    "be LOST. Env vars set at create time ARE preserved."
                ),
            },
        )
    # Self-heal path.
    return await _respawn_session(existing, session_id, files, deletes)


async def _post_write_status(existing: dict) -> tuple[str, dict]:
    """After a write into a live container: bump ``last_used_at``, let the
    dev server notice (``UPDATE_SETTLE_S``), tail what it printed since
    the last reload marker, and take one health probe. Shared by
    write_files and edit_file so they report the same way.
    Returns ``(startup_output, app_status)``."""
    await state.registry.update_metadata(
        existing["sandbox_id"],
        {"last_used_at": _now_iso()},
    )
    # Let the dev server notice the file change before we probe / tail.
    await asyncio.sleep(UPDATE_SETTLE_S)
    startup_output = await asyncio.to_thread(
        state.spawner.tail_logs_since_last_marker,
        existing["container_name"],
        40,
    )
    # Look up the runtime so probe knows which probe-path/port to hit.
    rt_name = existing.get("runtime")
    probe_path = "/"
    if rt_name and rt_name in RUNTIMES:
        probe_path = RUNTIMES[rt_name].readiness_probe_path
    app_status = await asyncio.to_thread(
        state.spawner.probe_health,
        existing["container_name"],
        80,
        probe_path,
        HEALTH_PROBE_TIMEOUT_S,
    )
    return startup_output, app_status


# ── write_files rewrite guard (sandbox policy over common.textops) ────────


def _text_of_file_value(value) -> Optional[str]:
    """The text a ``files``-map value would write, or ``None`` for a
    base64 (binary) value — the guard only compares text."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and value.get("encoding") in (None, "utf-8", "utf8", "text"):
        return str(value.get("content", ""))
    return None


def _guard_writes(
    files: dict,
    current: dict[str, bytes],
    *,
    min_lines: int = WRITE_GUARD_MIN_LINES,
    threshold: float = WRITE_GUARD_SIMILARITY,
) -> list[RewriteVerdict]:
    """The write_files decision as a pure function: every path in
    ``files`` whose existing bytes (``current``, path → bytes, only for
    paths that exist) would be needlessly rewritten. Empty list = allow."""
    verdicts: list[RewriteVerdict] = []
    for path, value in (files or {}).items():
        if path not in current:
            continue
        new_text = _text_of_file_value(value)
        if new_text is None:
            continue
        verdict = rewrite_guard(
            path, current[path], new_text, min_lines=min_lines, threshold=threshold,
        )
        if verdict is not None:
            verdicts.append(verdict)
    return verdicts


def _describe_rewrite(v: RewriteVerdict) -> str:
    """One sentence per guarded file, used in the 409 message."""
    if v.identical:
        return (
            f"{v.path} already exists with exactly this content "
            f"({v.old_lines} lines) — nothing to write."
        )
    return (
        f"{v.path} already exists and your new content keeps "
        f"{round(v.similarity * 100)}% of its lines ({v.changed_lines} of "
        f"{v.old_lines} changed)."
    )


async def _check_rewrite_guard(
    existing: dict,
    files: dict[str, FileContent],
    session_id: str,
    tool: str,
) -> None:
    """Refuse (409 ``rewrite_of_existing_file``) a write that re-sends an
    existing text file of ``WRITE_GUARD_MIN_LINES``+ lines while keeping
    ``WRITE_GUARD_SIMILARITY`` or more of its lines. Called before
    anything is written, so the whole call is rejected — no file of it
    lands. New, small, binary and genuinely rewritten files pass; the
    decision itself is ``_guard_writes`` (pure, unit-tested)."""
    text_paths = [
        p for p, v in (files or {}).items() if _text_of_file_value(v) is not None
    ]
    if not text_paths:
        return
    current = await asyncio.to_thread(
        state.spawner.read_existing_files,
        existing["container_name"],
        text_paths,
        MAX_FILE_BYTES,
    )
    verdicts = _guard_writes(files, current)
    if not verdicts:
        return
    log.info(
        "%s: session=%s refused needless rewrite of %s",
        tool, session_id, [(v.path, v.similarity) for v in verdicts],
    )
    raise HTTPException(409, _rewrite_detail(verdicts, session_id, tool))


def _rewrite_detail(
    verdicts: list[RewriteVerdict], session_id: str, tool: str,
) -> dict:
    paths = ", ".join(v.path for v in verdicts)
    return {
        "error": "rewrite of existing file",
        "kind": "rewrite_of_existing_file",
        "tool": tool,
        "session_id": session_id,
        "files": [
            {
                "path": v.path,
                "similarity": v.similarity,
                "changed_lines": v.changed_lines,
                "old_lines": v.old_lines,
                "new_lines": v.new_lines,
                "identical": v.identical,
            }
            for v in verdicts
        ],
        "message": " ".join(_describe_rewrite(v) for v in verdicts),
        "hint": (
            "No files were written. Use edit_file for changes like this: "
            f"read_file the part you are changing, then edit_file with "
            "old_string (the exact current text) and new_string. If you "
            f"really mean to replace {paths} wholesale, call {tool} again "
            "with overwrite=true."
        ),
    }


async def _apply_files_to_running(
    existing: dict,
    files: dict[str, FileContent],
    deletes: list[str],
    *,
    overwrite: bool = False,
    tool: str = "write_files",
) -> dict:
    """Existing container overlay path (extracted from _reuse_or_spawn's
    reuse branch). Rewrite guard (unless ``overwrite``), reload marker,
    write, then ``_post_write_status`` (last_used_at, settle, log tail,
    health probe)."""
    session_id = None
    if isinstance(existing.get("expires_at"), str) and state.registry is not None:
        # Session id is stored on the registry row; fetch to get the pure
        # id for logging and last_used bumping.
        got = await state.registry.get(existing["sandbox_id"])
        if got is not None:
            session_id = got.metadata.get("session_id")
    session_id = session_id or existing.get("session_id") or "?"

    if not overwrite:
        await _check_rewrite_guard(existing, files, session_id, tool)

    log.info(
        "update path: session=%s sandbox=%s alive, overlaying %d file(s), "
        "removing %d",
        session_id, existing["sandbox_id"], len(files or {}), len(deletes or []),
    )
    await asyncio.to_thread(
        state.spawner.write_reload_marker, existing["container_name"]
    )
    try:
        await asyncio.to_thread(
            state.spawner.write_files,
            existing["container_name"],
            files,
            deletes,
        )
    except ValueError as exc:
        log.warning("update path: rejected unsafe path: %s", exc)
        raise HTTPException(400, str(exc))
    startup_output, app_status = await _post_write_status(existing)
    rt_name = existing.get("runtime")
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
    await state.registry.set_phase(existing["sandbox_id"], "expired")

    # Reuse the previous runtime + env + entrypoint recorded in metadata.
    runtime = existing.get("runtime")
    if runtime is None or runtime not in RUNTIMES:
        raise HTTPException(
            500,
            {
                "error": "cannot self-heal: runtime metadata missing",
                "session_id": session_id,
                "hint": "Call create + write_files to start fresh.",
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
        chat_id=existing.get("chat_id"),
    )
    result["recreated"] = True
    return result


async def _do_get_files(
    session_id: str,
    paths: Optional[list[str]],
    max_bytes_per_file: int,
) -> dict:
    if state.registry is None or state.spawner is None:
        raise HTTPException(500, "runner not initialized")
    if not SESSION_ID_RE.match(session_id):
        raise HTTPException(400, "invalid session_id")
    existing = await _find_running_session(session_id)
    if existing is None or not existing.get("container_name"):
        raise HTTPException(
            404, f"no running sandbox for session {session_id!r}"
        )
    max_bytes = max(1, min(max_bytes_per_file, GET_FILES_HARD_CAP_BYTES))
    entries = await asyncio.to_thread(
        state.spawner.read_files, existing["container_name"], paths, max_bytes,
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
    if state.registry is None or state.spawner is None:
        raise HTTPException(500, "runner not initialized")
    if not SESSION_ID_RE.match(session_id):
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
                    "should recreate one, call write_files first with "
                    "recreate_if_gone=true — but note that packages you "
                    "install via exec do NOT survive a respawn."
                ),
            },
        )
    try:
        res = await asyncio.to_thread(
            state.spawner.exec_command,
            existing["container_name"],
            command,
            timeout,
            workdir,
            EXEC_MAX_OUTPUT_BYTES,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    # exec counts as activity — bump last_used_at so the reaper resets.
    await state.registry.update_metadata(
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


# ── read_file / edit_file ─────────────────────────────────────────────────

async def _require_live_container(session_id: str, tool: str) -> dict:
    """Session lookup shared by read_file / edit_file: 400 bad id, 404 no
    running session, 409 container gone (neither tool respawns — a fresh
    container would not have the file)."""
    if state.registry is None or state.spawner is None:
        raise HTTPException(500, "runner not initialized")
    if not SESSION_ID_RE.match(session_id or ""):
        raise HTTPException(400, "invalid session_id")
    existing = await _find_running_session(session_id)
    if existing is None or not existing.get("container_name"):
        raise HTTPException(
            404,
            {
                "error": "session not found",
                "session_id": session_id,
                "hint": (
                    "No running sandbox for this session_id. Call `create` "
                    "or `run` first (list_sessions recovers a lost id)."
                ),
            },
        )
    alive = await asyncio.to_thread(
        state.spawner.container_exists, existing["container_name"]
    )
    if not alive:
        raise HTTPException(
            409,
            {
                "error": "sandbox container is gone",
                "session_id": session_id,
                "hint": (
                    f"{tool} cannot respawn a container — a fresh one would "
                    "not have your files. Call write_files with "
                    "recreate_if_gone=true and the files' full content to "
                    "bring the session back, then continue."
                ),
            },
        )
    return existing


_FILE_OP_STATUS = {
    "unsafe_path": 400,
    "missing_file": 404,
    "too_large": 413,
}

_FILE_OP_ERROR = {
    "unsafe_path": "unsafe path",
    "missing_file": "file does not exist",
    "is_directory": "path is a directory",
    "not_a_file": "not a regular file",
    "binary_file": "not a UTF-8 text file",
    "too_large": "file too large",
    "empty_old_string": "old_string is empty",
    "no_change": "old_string equals new_string",
    "not_found": "old_string not found",
    "line_number_prefix": "old_string contains read_file line numbers",
    "multiple_matches": "old_string matches more than once",
}


def _file_op_hint(kind: str, tool: str, path: str, extra: dict) -> str:
    """The exact next action for a refused read_file / edit_file."""
    edit = tool == "edit_file"
    tail = " No file was modified." if edit else ""
    if kind == "unsafe_path":
        return (
            "Paths are relative to /app (e.g. `src/app.js`); absolute paths "
            "and '..' are rejected." + tail
        )
    if kind == "missing_file":
        if edit:
            return (
                "edit_file only changes existing files. Create it with "
                "write_files (its full content), or call get_files() with "
                "no paths to list what exists under /app."
            )
        return "Call get_files() with no paths to list what exists under /app."
    if kind in ("is_directory", "not_a_file"):
        return "Call get_files() with no paths to list the files under /app." + tail
    if kind == "binary_file":
        if edit:
            return (
                "edit_file only edits UTF-8 text. Replace a binary file with "
                "write_files and a base64 value."
            )
        return (
            "read_file shows UTF-8 text only. get_files(paths=[...]) returns "
            "a binary file as base64."
        )
    if kind == "too_large":
        return (
            f"edit_file handles files up to {MAX_FILE_BYTES:,} bytes "
            "(SANDBOX_MAX_FILE_BYTES). Edit a large file with exec (e.g. "
            "sed) or split it into smaller files." + tail
        )
    if kind == "empty_old_string":
        return (
            "To create a file or replace all of it, use write_files "
            "(overwrite=true for an existing file)." + tail
        )
    if kind == "no_change":
        return "Put the text you want in new_string." + tail
    if kind == "line_number_prefix":
        n = extra.get("matches_without_prefixes")
        found = (
            " Without the prefixes, old_string matches exactly one place."
            if n == 1 else ""
        )
        return (
            "Remove the `<number><TAB>` prefix from every line of old_string "
            "(and new_string) and call edit_file again." + found + tail
        )
    if kind == "not_found":
        a = extra.get("closest_start_line")
        if a is not None:
            ws = (
                " It differs only in whitespace or indentation."
                if extra.get("whitespace_only") else ""
            )
            return (
                f"The closest text is lines {a}-{extra.get('closest_end_line')} "
                f"(shown above).{ws} Copy old_string exactly from it — or "
                f"read_file(path=\"{path}\", offset={max(1, a - 10)}, limit=40) "
                "for more context — and call edit_file again." + tail
            )
        return (
            f"Call read_file(path=\"{path}\") to see the current text, then "
            "copy old_string from it exactly." + tail
        )
    if kind == "multiple_matches":
        lines = ", ".join(str(n) for n in extra.get("match_lines") or [])
        return (
            f"Matches start on lines {lines}. Include enough surrounding "
            "lines in old_string to pick one, or set replace_all=true to "
            f"change all {extra.get('count')}." + tail
        )
    return f"Fix the issue above and call {tool} again." + tail


def _file_op_http(
    exc: FileOpError, *, session_id: str, tool: str, path: str,
) -> HTTPException:
    """``FileOpError`` → the structured HTTPException every MCP tool
    renders: ``{error, kind, tool, session_id, path, message, hint, ...}``."""
    target = exc.extra.get("path") or path
    detail = {
        "error": _FILE_OP_ERROR.get(exc.kind, f"{tool} failed"),
        "kind": exc.kind,
        "tool": tool,
        "session_id": session_id,
        "path": target,
        "message": exc.message,
    }
    for key, value in exc.extra.items():
        if key != "path":
            detail[key] = value
    detail["hint"] = _file_op_hint(exc.kind, tool, target, exc.extra)
    log.info(
        "%s: refused session=%s path=%s kind=%s", tool, session_id, target, exc.kind,
    )
    return HTTPException(_FILE_OP_STATUS.get(exc.kind, 409), detail)


def _decode_text(data: bytes, safe: str, *, partial: bool) -> str:
    """UTF-8 text of a file, or ``FileOpError("binary_file")``."""
    if looks_binary(data):
        raise FileOpError(
            "binary_file", f"{safe} is not UTF-8 text (it contains a NUL byte).",
            path=safe,
        )
    try:
        return decode_utf8_prefix(data) if partial else data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FileOpError(
            "binary_file", f"{safe} is not valid UTF-8 text.", path=safe,
        ) from exc


async def _do_read_file(
    session_id: str,
    path: str,
    offset: Optional[int] = 1,
    limit: Optional[int] = None,
) -> dict:
    """A ``cat -n``-style window of one text file — Claude Code's Read.

    ``offset`` is the 1-based first line, ``limit`` the line count
    (default ``READ_FILE_DEFAULT_LINES``, capped at ``READ_FILE_MAX_LINES``).
    At most ``READ_FILE_MAX_BYTES`` are read from the container; a longer
    file reports ``file_truncated`` and its readable line count. Lines
    over ``READ_FILE_MAX_LINE_CHARS`` are cut with a marker. Binary and
    non-UTF-8 files are refused (``binary_file``) — get_files keeps the
    base64 path for those.
    """
    existing = await _require_live_container(session_id, "read_file")
    start = max(1, int(offset or 1))
    count = max(1, min(int(limit or READ_FILE_DEFAULT_LINES), READ_FILE_MAX_LINES))
    try:
        safe, data, size, byte_capped = await asyncio.to_thread(
            state.spawner.read_file_bytes,
            existing["container_name"],
            path,
            READ_FILE_MAX_BYTES,
        )
        text = _decode_text(data, safe, partial=byte_capped)
    except FileOpError as exc:
        raise _file_op_http(exc, session_id=session_id, tool="read_file", path=path)
    if byte_capped and not text.endswith("\n"):
        # The cap cut a line in half; show only whole lines.
        text = text[: text.rfind("\n") + 1]
    lines = split_lines(text)
    total = len(lines)
    window = lines[start - 1:start - 1 + count]
    end = start + len(window) - 1 if window else min(start - 1, total)
    content, cut = number_lines(
        window, start, max_line_chars=READ_FILE_MAX_LINE_CHARS,
    )
    await state.registry.update_metadata(
        existing["sandbox_id"], {"last_used_at": _now_iso()},
    )
    more = max(0, total - end) if window else 0
    log.info(
        "read_file: session=%s path=%s lines %d-%d of %d (size=%d capped=%s)",
        session_id, safe, start, end, total, size, byte_capped,
    )
    return {
        "session_id": session_id,
        "sandbox_id": existing["sandbox_id"],
        "path": safe,
        "start_line": start,
        "end_line": end,
        "total_lines": total,
        "more_lines": more,
        "truncated": more > 0 or (byte_capped and bool(window) and end == total),
        "lines_cut": cut,
        "size": size,
        "file_truncated": byte_capped,
        "content": content,
    }


async def _do_edit_file(
    session_id: str,
    path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> dict:
    """Exact-string replacement in one existing file — Claude Code's Edit.

    All-or-nothing: the file is read, ``common.textops.apply_edit`` checks
    the rules (non-empty, changed, exactly one match unless
    ``replace_all``) and builds the new content, a ``.py`` result is linted, and only then
    is the file written (reload marker first, so ``startup_output`` is
    fresh; ``_build_app_tarball`` ownership via ``Spawner.put_files``).
    Refusals are ``FileOpError``s rendered as structured 4xx details; a
    not-found ``old_string`` carries the closest region of the file.
    """
    existing = await _require_live_container(session_id, "edit_file")
    container_name = existing["container_name"]
    try:
        safe, data, _size, truncated = await asyncio.to_thread(
            state.spawner.read_file_bytes, container_name, path, MAX_FILE_BYTES,
        )
        if truncated:
            raise FileOpError(
                "too_large",
                f"{safe} is larger than {MAX_FILE_BYTES:,} bytes.",
                path=safe,
            )
        text = _decode_text(data, safe, partial=False)
        result = await asyncio.to_thread(
            lambda: apply_edit(
                text, old_string, new_string, replace_all,
                context=EDIT_SNIPPET_CONTEXT_LINES,
                max_snippet_lines=EDIT_SNIPPET_MAX_LINES,
                max_listed=EDIT_MAX_LISTED_MATCHES,
                max_line_chars=READ_FILE_MAX_LINE_CHARS,
            )
        )
        new_bytes = result.new_text.encode("utf-8")
        if len(new_bytes) > MAX_FILE_BYTES:
            raise FileOpError(
                "too_large",
                f"The edited {safe} would be {len(new_bytes):,} bytes.",
                path=safe,
            )
    except FileOpError as exc:
        raise _file_op_http(exc, session_id=session_id, tool="edit_file", path=path)

    if safe.endswith(".py"):
        lint_errors = _lint_python_files({safe: result.new_text})
        if lint_errors:
            raise HTTPException(
                400,
                {
                    "error": "static lint failed",
                    "session_id": session_id,
                    "errors": lint_errors,
                    "hint": (
                        "The edit was NOT applied — the file would not "
                        "compile with it. Fix new_string and call edit_file "
                        "again."
                    ),
                },
            )

    log.info(
        "edit_file: session=%s path=%s replacements=%d lines %d-%d",
        session_id, safe, result.replacements, result.start_line, result.end_line,
    )
    await asyncio.to_thread(state.spawner.write_reload_marker, container_name)
    await asyncio.to_thread(state.spawner.put_files, container_name, {safe: new_bytes})
    startup_output, app_status = await _post_write_status(existing)
    return {
        "sandbox_id": existing["sandbox_id"],
        "session_id": session_id,
        "url": existing["url"],
        "expires_at": existing["expires_at"],
        "runtime": existing.get("runtime"),
        "path": safe,
        "replacements": result.replacements,
        "start_line": result.start_line,
        "end_line": result.end_line,
        "match_lines": result.match_lines,
        "total_lines": result.total_lines,
        "snippet": result.snippet,
        "startup_output": startup_output,
        "app_status": app_status,
        "recreated": False,
    }


async def _do_preview(session_id: str) -> dict:
    if state.registry is None:
        raise HTTPException(500, "runner not initialized")
    if not SESSION_ID_RE.match(session_id):
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
                    "container died, call write_files with "
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
    if state.registry is None or state.spawner is None or state.slot_sem is None:
        raise HTTPException(500, "runner not initialized")
    if not SESSION_ID_RE.match(session_id):
        raise HTTPException(400, "invalid session_id")
    existing = await _find_running_session(session_id)
    if existing is None:
        return {
            "session_id": session_id,
            "was_running": False,
            "note": "already closed or never existed",
        }
    if existing["container_name"]:
        await asyncio.to_thread(state.spawner.stop, existing["container_name"])
    await state.registry.set_phase(existing["sandbox_id"], "closed")
    state.slot_sem.release()
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


# ── Browser log ingest ───────────────────────────────────────────────────
#
# Wire path: shim in the sandbox app → sandbox-proxy → this handler → docker
# exec into the sandbox container's /tmp/sandbox.log → get_logs surfaces
# with a `[browser]` prefix. See ai/sandbox/SANDBOX.md § Browser console
# capture for scope, limits, and known gaps.

_BROWSER_LEVELS = {"error", "warn", "log", "info", "debug"}


def _format_browser_entry(entry: dict) -> Optional[str]:
    """Render one shim-produced entry as a single log line (plus indented
    stack lines if present). Returns None if the entry is malformed
    beyond repair — we drop rather than write garbage."""
    level = entry.get("level")
    ts_ms = entry.get("ts")
    message = entry.get("message")
    if level not in _BROWSER_LEVELS or not isinstance(message, str):
        return None
    if not isinstance(ts_ms, (int, float)):
        ts_ms = time.time() * 1000
    ts_iso = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).isoformat()

    # `message` may itself be multi-line (e.g. a stringified object with
    # embedded newlines). Fold to one physical line so the log is
    # grep-friendly; the stack (if present) carries the full detail.
    one_line = message.replace("\n", " ").replace("\r", " ")

    # source/line/col are all optional — the shim omits them for
    # console.error/warn on a plain string; window.onerror always sets them.
    tail_parts: list[str] = []
    source = entry.get("source")
    line = entry.get("line")
    col = entry.get("col")
    if isinstance(source, str) and source:
        loc = source
        if isinstance(line, int):
            loc += f":{line}"
            if isinstance(col, int):
                loc += f":{col}"
        tail_parts.append(f"(at {loc})")

    header = f"[browser] {ts_iso} {level}: {one_line}"
    if tail_parts:
        header += "    " + " ".join(tail_parts)

    stack = entry.get("stack")
    if isinstance(stack, str) and stack.strip():
        indented = "\n".join("    " + s for s in stack.rstrip().split("\n"))
        return header + "\n" + indented
    return header


def _slide_browser_window(sandbox_id: str, now: float) -> deque:
    """Return the sandbox's rolling window with entries older than
    ``_BROWSER_LOG_WINDOW_S`` seconds pruned from the head."""
    q = _browser_log_windows.get(sandbox_id)
    if q is None:
        q = deque()
        _browser_log_windows[sandbox_id] = q
    cutoff = now - _BROWSER_LOG_WINDOW_S
    while q and q[0] < cutoff:
        q.popleft()
    return q


async def _write_lines_to_container_log(container_name: str, lines: list[str]) -> None:
    """Fire-and-forget append to the container's ``/tmp/sandbox.log``
    via ``spawner.append_to_log``. Swallows every failure — the browser
    won't retry regardless.
    """
    if not lines or state.spawner is None:
        return
    payload = "".join(line + "\n" for line in lines)
    try:
        await asyncio.to_thread(
            state.spawner.append_to_log, container_name, payload
        )
    except Exception as exc:  # noqa: BLE001 — best-effort ingest
        log.warning(
            "browser-log: append to %s failed: %s", container_name, exc,
        )


async def _do_browser_log_ingest(sandbox_id: str, entries: list[dict]) -> None:
    """Format, rate-limit, and forward browser-side events into the
    sandbox container's /tmp/sandbox.log.

    Returns None so the endpoint can `await` it (or fire-and-forget with
    ``asyncio.create_task``) and immediately respond 204. All failure
    paths are swallowed — the browser never sees an error.

    Rate limit: ``BROWSER_LOG_RATE_LIMIT_PER_MIN`` per sandbox_id per
    rolling 60 s window. Overage in a single POST is dropped and
    collapsed into ONE synthetic warn line so the model can see events
    are being lost.
    """
    if state.registry is None or state.spawner is None:
        log.debug("browser-log: runner not initialized, dropping ingest")
        return
    # Cheap 404-equivalent: unknown sandbox_id → drop silently. We don't
    # raise here so the endpoint stays 204 either way.
    job = await state.registry.get(sandbox_id)
    if job is None:
        log.debug("browser-log: unknown sandbox=%s, dropping %d entries",
                  sandbox_id, len(entries))
        return
    container_name = (job.result or {}).get("container_name")
    if not container_name:
        log.debug("browser-log: sandbox=%s has no container, dropping", sandbox_id)
        return

    now = time.monotonic()
    window = _slide_browser_window(sandbox_id, now)
    room = max(0, BROWSER_LOG_RATE_LIMIT_PER_MIN - len(window))

    accepted: list[dict] = []
    dropped = 0
    for entry in entries:
        if not isinstance(entry, dict):
            dropped += 1
            continue
        if room <= 0:
            dropped += 1
            continue
        accepted.append(entry)
        window.append(now)
        room -= 1

    lines: list[str] = []
    for entry in accepted:
        rendered = _format_browser_entry(entry)
        if rendered is not None:
            lines.append(rendered)

    if dropped > 0:
        # One synthetic line no matter how many were dropped; the model
        # gets a clear "we're rate-limiting" signal without a flood.
        ts = datetime.now(tz=timezone.utc).isoformat()
        lines.append(
            f"[browser] {ts} warn: [browser rate-limited: {dropped} events "
            f"dropped in the last minute — cap is {BROWSER_LOG_RATE_LIMIT_PER_MIN}/min]"
        )

    if not lines:
        return

    log.debug(
        "browser-log: sandbox=%s accepted=%d dropped=%d",
        sandbox_id, len(accepted), dropped,
    )
    await _write_lines_to_container_log(container_name, lines)


async def _reuse_or_spawn(
    runtime: str,
    files: dict[str, FileContent],
    entrypoint: Optional[str],
    ttl_seconds: Optional[int],
    session_id: Optional[str],
    deletes: list[str],
    env: Optional[dict[str, str]] = None,
    recreate_if_gone: bool = True,
    chat_id: Optional[str] = None,
    overwrite: bool = False,
) -> dict:
    """Session-aware entry point behind ``POST /run``, the ``run`` MCP
    tool and ``/tool/run``.

    Two branches:
      * ``session_id`` names a live container → overlay + probe. Same shape
        as ``_do_write_files`` but returns the ``run`` response schema.
      * No session_id, OR the session's container is gone AND
        ``recreate_if_gone`` is true → full spawn.

    ``chat_id`` makes ``run`` one-create-per-chat: when the caller gives a
    chat_id but no explicit session_id, and the chat already has a live
    container, we resolve that chat's session_id and take the overlay path
    instead of spawning a second container.

    ``overwrite`` is the overlay path's rewrite-guard opt-out (see
    ``_check_rewrite_guard``); a fresh spawn has no existing files to
    guard. The OpenWebUI Tool Server passes ``True`` — its only tool is
    ``run``, so it has no edit_file to fall back on.
    """
    log.info(
        "_reuse_or_spawn: runtime=%s session_id=%s chat_id=%s n_files=%d "
        "n_deletes=%d entrypoint=%r recreate=%s env_keys=%s",
        runtime, session_id, chat_id, len(files or {}), len(deletes or []),
        entrypoint, recreate_if_gone, sorted((env or {}).keys()),
    )
    if state.registry is None or state.spawner is None or state.slot_sem is None:
        raise HTTPException(500, "runner not initialized")

    # Resolve an explicit session_id from the chat when the caller didn't pass
    # one. Only adopt a chat's session if its container is actually running —
    # a still-warming or dead row falls through to the spawn path (which will
    # record this chat_id, so the next call converges).
    if not session_id and chat_id:
        chat_existing = await _find_active_session_by_chat(chat_id)
        if (
            chat_existing
            and chat_existing.get("phase") == "running"
            and chat_existing.get("container_name")
        ):
            session_id = chat_existing["session_id"]
            log.info(
                "run: chat=%s → reusing existing session=%s sandbox=%s",
                chat_id, session_id, chat_existing["sandbox_id"],
            )

    if session_id:
        existing = await _find_running_session(session_id)
        if existing and existing["container_name"]:
            alive = await asyncio.to_thread(
                state.spawner.container_exists, existing["container_name"]
            )
            if alive:
                # Session reuse: overlay + probe path.
                return await _apply_files_to_running(
                    existing, files, deletes, overwrite=overwrite, tool="run",
                )
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
            await state.registry.set_phase(existing["sandbox_id"], "expired")
            # Preserve original env if the caller didn't supply new env.
            if env is None:
                env = existing.get("env") or {}

    # Spawn path — either no session_id given, or the session_id had no
    # live container. Generate one if missing so the caller always gets
    # a stable handle back.
    session_id = session_id or _new_session_id()

    rt = _resolve_runtime(runtime, entrypoint)

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

    ttl = min(ttl_seconds or DEFAULT_TTL_S, HARD_TTL_S)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl)
    now_iso = _now_iso()
    reserve_meta = {
        "runtime": runtime,
        "entrypoint": entrypoint or rt.default_entrypoint,
        "ttl_seconds": ttl,
        "expires_at": expires_at.isoformat(),
        "session_id": session_id,
        "chat_id": chat_id,
        "last_used_at": now_iso,
        "env": env or {},
    }

    # Atomically reserve the chat's active slot (same UNIQUE-index backstop as
    # _do_create). If a concurrent run for this chat won the race, route onto
    # the winner instead of spawning a rival: overlay our files if it's already
    # running, otherwise hand back its (still-warming) session.
    try:
        sandbox_id = await _reserve_active_slot(reserve_meta, chat_id)
    except _LostRace as lost:
        winner = lost.winner
        if winner.get("phase") == "running" and winner.get("container_name"):
            log.info(
                "run: chat=%s lost the spawn race — overlaying files onto the "
                "winner sandbox=%s",
                chat_id, winner["sandbox_id"],
            )
            return await _apply_files_to_running(
                winner, files, deletes, overwrite=overwrite, tool="run",
            )
        log.info(
            "run: chat=%s lost the spawn race to still-warming sandbox=%s — "
            "returning it",
            chat_id, winner["sandbox_id"],
        )
        return _existing_session_as_create_response(winner, chat_id)

    log.info(
        "spawn path: session=%s sandbox=%s runtime=%s ttl=%ds entrypoint=%r "
        "chat=%s",
        session_id, sandbox_id, runtime, ttl,
        entrypoint or rt.default_entrypoint, chat_id,
    )

    try:
        result = await asyncio.to_thread(
            state.spawner.spawn, sandbox_id, rt, files, entrypoint, env
        )
        log.info(
            "container created: sandbox=%s container=%s, polling readiness",
            sandbox_id, result.container_name,
        )
        await _await_ready_or_teardown(
            sandbox_id, result.container_name, rt, session_id,
            hint=(
                "Read the container logs above to see why the app failed to "
                "start (traceback, missing module, port bind error, etc.). "
                "Fix the code and call again with the same session_id — the "
                "runner will spawn a fresh container."
            ),
        )

        url = f"{PROXY_URL}/{sandbox_id}/"
        await state.registry.set_result(
            sandbox_id,
            {"url": url, "container_name": result.container_name},
            phase="running",
        )
        startup_output = await asyncio.to_thread(
            state.spawner.tail_logs, result.container_name, 40
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
        await state.registry.set_error(sandbox_id, str(exc))
        state.slot_sem.release()
        raise HTTPException(500, f"spawn failed: {exc}")
