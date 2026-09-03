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
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException

from common.jobs.postgres import PostgresRegistry

from constants import (
    DEFAULT_TTL_S,
    EXEC_DEFAULT_TIMEOUT_S,
    EXEC_HARD_TIMEOUT_S,
    EXEC_MAX_OUTPUT_BYTES,
    GET_FILES_DEFAULT_BYTES,
    GET_FILES_HARD_CAP_BYTES,
    HARD_TTL_S,
    HEALTH_PROBE_TIMEOUT_S,
    MAX_CONCURRENT,
    PROXY_URL,
    SESSION_ID_RE,
    UPDATE_SETTLE_S,
)
from models import FileContent
from runtimes import RUNTIMES, get_runtime

import state


log = logging.getLogger("sandbox-runner.operations")


# ── Small utilities ───────────────────────────────────────────────────────

def _new_session_id() -> str:
    """URL-safe 12-char session id. Enough entropy to avoid collisions
    across concurrent chats without dragging a UUID through log lines."""
    sid = secrets.token_urlsafe(9)  # 9 bytes → 12 base64 chars
    log.debug("generated new session_id: %s", sid)
    return sid


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


# ── Session lookup helpers ────────────────────────────────────────────────

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
    if state.registry is None:
        log.warning("_find_running_session called before registry init")
        return None
    pool = state.registry._pool
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
        meta = json.loads(meta)
    result = row["result"] or {}
    if isinstance(result, str):
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
    if state.registry is None:
        return []
    pool = state.registry._pool
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
    if state.registry is None or state.spawner is None or state.slot_sem is None:
        raise HTTPException(500, "runner not initialized")

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

    # Concurrency gate — matches the spawn path in _reuse_or_spawn.
    try:
        await asyncio.wait_for(state.slot_sem.acquire(), timeout=0.1)
    except asyncio.TimeoutError:
        raise HTTPException(429, "sandbox pool exhausted")

    session_id = _new_session_id()
    ttl = min(ttl_seconds or DEFAULT_TTL_S, HARD_TTL_S)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl)
    now_iso = _now_iso()

    sandbox_id = await state.registry.register(
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
            state.spawner.spawn_empty, sandbox_id, rt, entrypoint, env
        )
        await state.registry.set_phase(sandbox_id, "starting")
        ready = await asyncio.to_thread(
            state.spawner.readiness_ok,
            result.container_name,
            rt.internal_port,
            rt.readiness_probe_path,
            30.0,
        )
        if not ready:
            logs = await asyncio.to_thread(
                state.spawner.tail_logs, result.container_name, 100
            )
            await asyncio.to_thread(state.spawner.stop, result.container_name)
            await state.registry.set_error(
                sandbox_id,
                "sandbox did not become ready within 30s\n\n" + logs,
            )
            state.slot_sem.release()
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
        }
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("create EXCEPTION for sandbox=%s: %s", sandbox_id, exc)
        await state.registry.set_error(sandbox_id, str(exc))
        state.slot_sem.release()
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
                    "PREFERRED for a small edit at a known line — "
                    "call patch_files with the same session_id. Pass the "
                    "path, start_line, end_line, the current bytes as "
                    "`expected`, and your fix as `replacement`. It edits "
                    "in place instead of re-uploading the file.\n\n"
                    "If you don't already have the exact current bytes of "
                    "that range in this turn, call get_files(paths=[...]) "
                    "first — patch_files requires a byte-for-byte match on "
                    "`expected`.\n\n"
                    "Use update_files (whole-file rewrite) only when the "
                    "fix is a large refactor, a new file, or the target "
                    "file's content has drifted from what you have."
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
    if isinstance(existing.get("expires_at"), str) and state.registry is not None:
        # Session id is stored on the registry row; fetch to get the pure
        # id for logging and last_used bumping.
        got = await state.registry.get(existing["sandbox_id"])
        if got is not None:
            session_id = got.metadata.get("session_id")
    session_id = session_id or existing.get("session_id") or "?"

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
            state.spawner.update_files,
            existing["container_name"],
            files,
            deletes,
        )
    except ValueError as exc:
        log.warning("update path: rejected unsafe path: %s", exc)
        raise HTTPException(400, str(exc))
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
                    "should recreate one, call update_files first with "
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
    if state.registry is None or state.spawner is None or state.slot_sem is None:
        raise HTTPException(500, "runner not initialized")
    if not SESSION_ID_RE.match(session_id):
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
        state.spawner.container_exists, existing["container_name"]
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
            state.spawner.patch_files,
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
    await state.registry.update_metadata(
        existing["sandbox_id"], {"last_used_at": _now_iso()},
    )
    await asyncio.sleep(UPDATE_SETTLE_S)
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
    startup_output = await asyncio.to_thread(
        state.spawner.tail_logs_since_last_marker,
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
    if state.registry is None or state.spawner is None or state.slot_sem is None:
        raise HTTPException(500, "runner not initialized")

    if session_id:
        existing = await _find_running_session(session_id)
        if existing and existing["container_name"]:
            alive = await asyncio.to_thread(
                state.spawner.container_exists, existing["container_name"]
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
            await state.registry.set_phase(existing["sandbox_id"], "expired")
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
            "Call get_runtime_types for full descriptions.",
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
        await asyncio.wait_for(state.slot_sem.acquire(), timeout=0.1)
    except asyncio.TimeoutError:
        log.warning(
            "sandbox pool exhausted (MAX_CONCURRENT=%d), rejecting session=%s",
            MAX_CONCURRENT, session_id,
        )
        raise HTTPException(429, "sandbox pool exhausted")

    ttl = min(ttl_seconds or DEFAULT_TTL_S, HARD_TTL_S)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl)
    now_iso = _now_iso()

    sandbox_id = await state.registry.register(
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
            state.spawner.spawn, sandbox_id, rt, files, entrypoint, env
        )
        log.info(
            "container created: sandbox=%s container=%s, polling readiness",
            sandbox_id, result.container_name,
        )
        await state.registry.set_phase(sandbox_id, "starting")
        ready = await asyncio.to_thread(
            state.spawner.readiness_ok,
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
                state.spawner.tail_logs, result.container_name, 100
            )
            await asyncio.to_thread(state.spawner.stop, result.container_name)
            await state.registry.set_error(
                sandbox_id,
                "sandbox did not become ready within 30s\n\n" + logs,
            )
            state.slot_sem.release()
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
