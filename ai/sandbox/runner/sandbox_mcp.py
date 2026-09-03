"""FastMCP wrapper — the ``sandbox`` MCP server.

Tools exposed (all invoked as ``sandbox.<name>``):

    get_runtimes()                  — describe available runtimes
    create(runtime, ...)            — warm an empty container
    update_files(session_id, ...)   — overlay files, health-probe after
    get_files(session_id, paths?)   — read files back from /app
    get_logs(session_id, lines?)    — tail container stdout+stderr
    exec(session_id, command, ...)  — run a non-interactive shell command
    patch_files(session_id, patches)— strict line-range edits, all-or-nothing
    preview(session_id)             — return the iframe artifact HTML
    close(session_id)               — teardown and release slot
    list_sessions()                 — enumerate live sandboxes
    run(runtime, files, ...)        — convenience: create + update + preview
    preview_app(...)                — deprecated alias for run

Every tool returns a ``ToolResult`` — a list of ``TextContent`` (what the
model reads) plus a ``structured_content`` JSON payload (what any
programmatic caller can parse without regex). The text form is unchanged
from the pre-refactor shape where a caller was already using it.

The tool implementations are bound to callables passed to ``build_mcp``
from ``app.py`` — that indirection keeps ``sandbox_mcp`` free of the
``docker.from_env`` and Postgres imports that would otherwise pull the
FastAPI lifespan into every tool import.
"""

from __future__ import annotations

import html
import json
import logging
import secrets
from typing import Any, Callable, Optional

from fastapi import HTTPException
from fastmcp import FastMCP
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent
from pydantic import Field

from runtimes import describe_runtimes


log = logging.getLogger("sandbox-runner.mcp")


# ── Rendering helpers (shared across tools) ───────────────────────────────

def _format_diagnostic(detail: dict) -> str:
    """Turn a runner HTTPException detail dict into a diagnostic string
    for the model. Same shape as pre-refactor — recognized errors:

      "static lint failed" (400)
      "sandbox did not become ready" (504)
      "sandbox container is gone" (409, new — surfaced by update_files
                                   when recreate_if_gone=false and the
                                   container was reaped)
      "no running sandbox for this session" (404)
      "session not found" (404)
    """
    log.debug(
        "_format_diagnostic: error=%r session=%s keys=%s",
        detail.get("error"), detail.get("session_id"), sorted(detail.keys()),
    )
    error = detail.get("error", "spawn failed")
    session_id = detail.get("session_id")
    hint = detail.get("hint", "")
    lines = [f"failed: {error}"]
    if session_id:
        lines.append(f"Session id: {session_id}")

    if error == "static lint failed":
        errors = detail.get("errors") or []
        lines.append("")
        lines.append(f"Static lint found {len(errors)} error(s):")
        for e in errors:
            path = e.get("path", "?")
            line = e.get("line") or "?"
            offset = e.get("offset") or "?"
            msg = e.get("message", "")
            lines.append(f"  {path}:{line}:{offset}: {msg}")
            text = e.get("text") or ""
            if text:
                lines.append(f"    {text}")
                if isinstance(e.get("offset"), int):
                    lines.append("    " + " " * (e["offset"] - 1) + "^")
    elif error.startswith("sandbox did not become ready"):
        logs = detail.get("logs") or ""
        lines.append("")
        if logs.strip():
            lines.append("Container logs (last 100 lines):")
            lines.append("---")
            lines.append(logs.rstrip())
            lines.append("---")
        else:
            lines.append("(container produced no output before timing out)")
    elif detail.get("kind") in {
        "content_mismatch", "out_of_range", "overlap",
        "missing_file", "binary_file", "unsafe_path",
        "bad_expected_type", "bad_replacement_type",
    }:
        # patch_files-specific structured failures. Rendered instead of
        # the raw JSON dump the fallback branch uses.
        kind = detail["kind"]
        path = detail.get("path", "?")
        sl = detail.get("start_line")
        el = detail.get("end_line")
        patch_idx = detail.get("patch_index")
        lines.append("")
        if kind == "content_mismatch":
            lines.append(
                f"patch_files failed: expected content mismatch at "
                f"{path}:{sl}-{el}."
            )
            lines.append("")
            lines.append("Your `expected`:")
            for src_line in (detail.get("expected") or "").split("\n"):
                lines.append(f"    {src_line}")
            lines.append("")
            lines.append("Actual (currently in file):")
            for src_line in (detail.get("actual") or "").split("\n"):
                lines.append(f"    {src_line}")
            lines.append("")
            lines.append(
                "No files were modified. Call get_files(paths=["
                f"\"{path}\"]) to refresh your view, then reissue "
                "patch_files with the correct `expected` content. "
                "The other patches in this call were NOT applied."
            )
        elif kind == "out_of_range":
            lines.append(
                f"patch_files failed: patch #{patch_idx} on {path} "
                f"targets lines {sl}-{el}, but the file has "
                f"{detail.get('file_line_count', '?')} line(s)."
            )
            lines.append("")
            lines.append(
                "No files were modified. Call get_files(paths=["
                f"\"{path}\"]) to see the current file, then reissue "
                "patch_files with a valid line range."
            )
        elif kind == "overlap":
            lines.append(f"patch_files failed: overlapping patches on {path}.")
            lines.append("")
            other_idx = detail.get("other_patch_index")
            other_sl = detail.get("other_start_line")
            other_el = detail.get("other_end_line")
            lines.append(f"  Patch #{other_idx} covers lines {other_sl}-{other_el}")
            lines.append(f"  Patch #{patch_idx} covers lines {sl}-{el}")
            lines.append(
                "These overlap. Combine them into ONE patch whose "
                "`expected` and `replacement` cover the merged range. "
                "No files were modified."
            )
        elif kind == "missing_file":
            lines.append(
                f"patch_files failed: {path} does not exist in the sandbox."
            )
            lines.append("")
            lines.append(
                "patch_files does not create files. Call update_files "
                "with the file's full content to create it first."
            )
        elif kind == "binary_file":
            lines.append(
                f"patch_files failed: {path} is not UTF-8 (contains a "
                "null byte or invalid encoding)."
            )
            lines.append("")
            lines.append(
                "Use update_files with a base64 payload to overwrite "
                "the file entirely."
            )
        elif kind == "unsafe_path":
            lines.append(
                f"patch_files failed: patch #{patch_idx} has an unsafe "
                f"path {path!r}. Absolute paths and '..' are rejected."
            )
        elif kind in ("bad_expected_type", "bad_replacement_type"):
            field = "expected" if kind == "bad_expected_type" else "replacement"
            lines.append(
                f"patch_files failed: patch #{patch_idx} on {path} has "
                f"a non-string `{field}` field."
            )
    else:
        # Any other structured error — pass through as JSON so the
        # model doesn't lose information.
        lines.append("")
        lines.append(json.dumps(
            {k: v for k, v in detail.items() if k not in ("hint",)},
            indent=2, default=str,
        ))

    if hint:
        lines.append("")
        lines.append(hint)
    return "\n".join(lines)


_SUSPICIOUS_STARTUP_MARKERS = (
    "Traceback (most recent call last)",
    "Error:",
    "Exception:",
    "ImportError",
    "ModuleNotFoundError",
    "SyntaxError",
    "TypeError",
    "AttributeError",
    "NameError",
    "KeyError",
    "IndexError",
    "ValueError",
    "Address already in use",
    "EADDRINUSE",
    "Cannot find module",
    "ENOENT: no such file",
    "npm ERR!",
    "FATAL",
    "panic:",
    "core dumped",
    "Segmentation fault",
    "unhandledPromiseRejection",
    "Uncaught",
    "[streamlit exception]",  # from _streamlit_bootstrap.py shim
)


def _format_startup_output(runtime: Optional[str], output: str) -> str:
    """Same 4-case renderer as before, minus the Streamlit special case
    (the bootstrap shim now surfaces Streamlit exceptions on stderr, so
    a clean log for Streamlit means the same thing it does for every
    other runtime)."""
    if runtime == "static":
        return (
            "Startup output: skipped (static runtime — nginx serves "
            "files, no application-level output to surface)."
        )
    stripped = (output or "").strip()
    if not stripped:
        return (
            "Startup output: (empty — container just spawned, or app "
            "writes to a file instead of stdout)."
        )
    hits = [m for m in _SUSPICIOUS_STARTUP_MARKERS if m in stripped]
    if hits:
        return (
            "Startup output: ⚠ SUSPICIOUS — found "
            f"{', '.join(hits[:3])}"
            f"{' (and more)' if len(hits) > 3 else ''}. "
            "Fix the code and call update_files (or `run`) again with "
            "the SAME session_id BEFORE the user has to report it.\n"
            "```\n"
            f"{stripped}\n"
            "```"
        )
    n_lines = stripped.count("\n") + 1
    return f"Startup output: clean ({n_lines} lines)."


def _format_app_status(app_status: Optional[dict]) -> str:
    """Compress the health-probe result into a single line."""
    if not app_status:
        return "App status: unknown"
    if "code" in app_status:
        code = app_status["code"]
        latency = app_status.get("latency_ms", 0)
        note = app_status.get("note")
        base = f"App status: HTTP {code} in {latency} ms"
        if note:
            base += f" ({note})"
        if 200 <= code < 400:
            return base
        return "⚠ " + base + " — reload may have broken the app"
    if "error" in app_status:
        return f"⚠ App status: {app_status['error']}"
    return "App status: unknown"


def render_preview_html(url: str, sandbox_id: str, session_id: str) -> str:
    """Full HTML document that navigates OpenWebUI's artifact iframe
    (or the Tool Server response iframe) to the running sandbox URL.

    Shared by both the ``run`` / ``preview`` MCP tools (which embed this
    in a ``` ```html ``` fenced block) and the OpenWebUI Tool Server
    route (which returns it as an ``HTMLResponse`` with
    ``Content-Disposition: inline``). Both consumers get dropped into a
    sandboxed iframe by OpenWebUI, so returning ``<iframe src=URL>``
    here would produce TWO iframe layers (OpenWebUI's srcdoc iframe
    wrapping our iframe wrapping the sandbox). Meta-refresh navigates
    OpenWebUI's iframe *itself* to the sandbox URL. One iframe, no
    wrapping.

    Three fallback layers, most-preferred first:
      1. ``<meta http-equiv="refresh">`` — no JS required.
      2. ``window.location.replace`` — for stricter sandbox flag
         combinations where meta-refresh is blocked.
      3. Visible ``<a target="_top">`` link — for the rare case both
         above are blocked (e.g. no-script, no-refresh sandbox).

    The leading HTML comment carries a per-response nonce so OpenWebUI's
    ``autoOpenedArtifactIds`` sees each update as a distinct artifact
    and re-opens the split panel if the user closed it between turns.
    """
    cache_bust = secrets.token_hex(4)
    sep = "&" if "?" in url else "?"
    nav_url = f"{url}{sep}v={cache_bust}"
    log.debug(
        "render_preview_html: session=%s sandbox=%s cache_bust=%s",
        session_id, sandbox_id, cache_bust,
    )
    safe_url_attr = html.escape(nav_url, quote=True)
    safe_sandbox = html.escape(sandbox_id)
    safe_session = html.escape(session_id)
    js_url = json.dumps(nav_url)
    return (
        f"<!-- preview session={safe_session} sandbox={safe_sandbox} -->\n"
        "<!doctype html>\n"
        "<html>\n"
        "<head>\n"
        '<meta charset="utf-8">\n'
        "<title>sandbox preview</title>\n"
        f'<meta http-equiv="refresh" content="0; url={safe_url_attr}">\n'
        "</head>\n"
        '<body style="margin:0;font-family:system-ui;padding:1.5rem;'
        'background:#0e1116;color:#e6edf3">\n'
        f'<p style="margin:0">Loading sandbox <code>{safe_sandbox}</code> '
        f"&middot; Session <code>{safe_session}</code>… If it does not "
        f'appear, <a href="{safe_url_attr}" target="_top" '
        'style="color:#8ab4f8">open in new tab</a>.</p>\n'
        f"<script>window.location.replace({js_url})</script>\n"
        "</body>\n"
        "</html>"
    )


def _download_url_for(url: str, session_id: str) -> str:
    """Derive the source-download URL from the preview URL.

    The Caddy route at ``/sandboxes/download/{session_id}`` reverse-proxies
    to the runner's session-download endpoint, so we assemble
    ``<proxy_base>/download/{session_id}`` from the returned preview URL.
    Session-based → survives self-heal.
    """
    proxy_base = url.rsplit("/", 2)[0]
    return f"{proxy_base}/download/{session_id}"


def _tool_result(text: str, structured: dict) -> ToolResult:
    """Assemble the standard ToolResult shape — one TextContent block
    (what the model reads) plus a machine-readable structured payload
    (what downstream tooling can parse without regex)."""
    return ToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=structured,
    )


def _handle_http_exception(exc: HTTPException, *, tool: str) -> ToolResult:
    """Convert an HTTPException raised by ``_do_*`` into a diagnostic
    ToolResult so the model sees a normal tool response instead of an
    MCP-level error."""
    detail = exc.detail
    if isinstance(detail, dict) and detail.get("error"):
        log.info(
            "%s: converting HTTPException(status=%d, error=%r) to tool response",
            tool, exc.status_code, detail.get("error"),
        )
        text = _format_diagnostic(detail)
        return _tool_result(
            text,
            {
                "ok": False,
                "status": exc.status_code,
                "error": detail.get("error"),
                "session_id": detail.get("session_id"),
                "detail": detail,
            },
        )
    # Non-structured detail — fall back to a plain error line.
    log.info(
        "%s: HTTPException(status=%d) with plain detail: %r",
        tool, exc.status_code, detail,
    )
    text = f"{tool} failed: {detail}"
    return _tool_result(
        text,
        {"ok": False, "status": exc.status_code, "error": str(detail)},
    )


# ── Build the MCP server ──────────────────────────────────────────────────

def build_mcp(
    run_callable: Callable[..., Any],
    logs_callable: Callable[..., Any],
    create_callable: Callable[..., Any],
    update_files_callable: Callable[..., Any],
    get_files_callable: Callable[..., Any],
    exec_callable: Callable[..., Any],
    patch_files_callable: Callable[..., Any],
    preview_callable: Callable[..., Any],
    close_callable: Callable[..., Any],
    list_sessions_callable: Callable[..., Any],
) -> FastMCP:
    """Wire up the tool surface. All callables live in ``app.py`` and get
    passed in to break the ``sandbox_mcp`` → ``app`` import cycle."""

    mcp = FastMCP(name="sandbox")

    # ── get_runtimes ──
    @mcp.tool()
    async def get_runtimes() -> ToolResult:
        """Return the sandbox runtimes available on this deployment.

        Each entry describes one runtime's summary, default entrypoint,
        pre-baked packages, and an example ``files`` map. **Call this
        FIRST if you're unsure which runtime fits the user's request** —
        it saves guessing and shows you which packages are already
        installed so you don't include them in requirements.txt.

        Flow: get_runtimes → create → update_files → preview → close.
        Or the one-shot: get_runtimes → run.
        """
        log.info("MCP tool call: get_runtimes")
        runtimes = describe_runtimes()
        lines = ["Available runtimes:"]
        for rt in runtimes:
            lines.append(f"- {rt['name']}: {rt['summary']}")
        return _tool_result(
            "\n".join(lines),
            {"runtimes": runtimes},
        )

    # ── create ──
    @mcp.tool()
    async def create(
        runtime: str = Field(
            description=(
                "Runtime for the empty warming container. One of the "
                "names in get_runtimes."
            )
        ),
        ttl_seconds: Optional[int] = Field(
            default=None,
            description="Idle TTL. Server clamps to SANDBOX_HARD_TTL_SECONDS.",
        ),
        entrypoint: Optional[str] = Field(
            default=None,
            description=(
                "Shell command bound to port 80. Leave unset for the "
                "runtime's default. Rejected for the 'static' runtime."
            ),
        ),
        env: Optional[dict[str, str]] = Field(
            default=None,
            description=(
                "Process env vars set inside the container. IMMUTABLE "
                "after create — self-heal respawn replays the same env. "
                "Reserved keys (HTTP_PROXY, PYTHONUNBUFFERED, TERM, "
                "etc.) are rejected."
            ),
        ),
    ) -> ToolResult:
        """Reserve an empty warming container and return the session
        handle. Use this FIRST when you know you'll need a preview but
        haven't written the code yet — the container starts warming
        while you finish thinking, so the follow-up ``update_files``
        hits a warm container and hot-reloads instantly.

        The URL returned serves a "sandbox warming" placeholder page
        (or Streamlit's own "warming" script for python) until you call
        ``update_files`` with your real code.

        Flow: get_runtimes → create → update_files (repeat) → preview → close.

        Consumes one slot from SANDBOX_MAX_CONCURRENT.
        """
        log.info("MCP tool call: create runtime=%s", runtime)
        try:
            result = await create_callable(runtime, ttl_seconds, entrypoint, env)
        except HTTPException as exc:
            return _handle_http_exception(exc, tool="create")
        sid = result["session_id"]
        text = (
            f"Sandbox created. Session id: {sid}. "
            f"Runtime: {result['runtime']}. "
            f"URL: {result['url']} (expires {result['expires_at']}).\n"
            "Status: warming — dev server is booting with placeholder files.\n"
            "Next: call update_files(session_id=\"" + sid + "\", files={...}) "
            "with your actual code."
        )
        return _tool_result(text, {"ok": True, **result})

    # ── update_files ──
    @mcp.tool()
    async def update_files(
        session_id: str = Field(
            description=(
                "The session_id from a previous create / run response."
            )
        ),
        files: dict = Field(
            default_factory=dict,
            description=(
                "Path → content OVERLAY. Files not listed are preserved. "
                "Values are UTF-8 str, or {\"encoding\":\"base64\","
                "\"content\":\"...\"} for binaries. Per-file cap: "
                "SANDBOX_MAX_FILE_BYTES; total cap: SANDBOX_MAX_PAYLOAD_BYTES."
            ),
        ),
        deletes: list[str] = Field(
            default_factory=list,
            description=(
                "Relative paths under /app to remove. Ignored on fresh spawns."
            ),
        ),
        recreate_if_gone: bool = Field(
            default=False,
            description=(
                "OPT-IN self-heal. If false (default) and the container is "
                "gone, this returns an error telling you to explicitly opt "
                "in. If true, the runner respawns fresh — files installed "
                "via exec and in-container state are LOST; env is preserved."
            ),
        ),
    ) -> ToolResult:
        """Overlay files into a live sandbox and hot-reload.

        Semantics:
          * ``files`` is an OVERLAY — unlisted paths are preserved.
          * ``deletes`` explicitly removes files under /app.
          * Same URL, same session_id — dev server hot-reloads.
          * Runs static Python lint on .py files before writing.
          * After the reload settles, runs a health probe against the
            app's root path and surfaces the result inline. If the reload
            broke the app, you'll see the HTTP status here without a
            second tool call.
          * Container gone? Default behavior is to error out with a clear
            "call again with recreate_if_gone=true" message. This prevents
            silent state loss — env is preserved on respawn, but files
            you installed via ``exec`` are LOST.

        See flow: get_runtimes → create → update_files → preview → close.
        """
        log.info(
            "MCP tool call: update_files session=%s n_files=%d recreate=%s",
            session_id, len(files or {}), recreate_if_gone,
        )
        try:
            result = await update_files_callable(
                session_id, files, deletes, recreate_if_gone,
            )
        except HTTPException as exc:
            return _handle_http_exception(exc, tool="update_files")
        text_lines = []
        recreated = result.get("recreated")
        if recreated:
            text_lines.append(
                "⚠ Sandbox was recreated. Previous in-container state was lost."
            )
        text_lines.append(
            f"Sandbox `{result['sandbox_id']}` updated. "
            f"{len(files or {})} file(s) written, {len(deletes or [])} deleted."
        )
        text_lines.append(f"URL: {result['url']} (unchanged).")
        text_lines.append(f"Session id: {result['session_id']}")
        text_lines.append(_format_app_status(result.get("app_status")))
        text_lines.append(_format_startup_output(
            result.get("runtime"), result.get("startup_output") or "",
        ))
        return _tool_result(
            "\n".join(text_lines),
            {"ok": True, **result},
        )

    # ── get_files ──
    @mcp.tool()
    async def get_files(
        session_id: str = Field(
            description="The session_id returned by create / run."
        ),
        paths: Optional[list[str]] = Field(
            default=None,
            description=(
                "Relative paths under /app. Omit to get a directory "
                "listing (paths + sizes only, no contents)."
            ),
        ),
        max_bytes_per_file: int = Field(
            default=8192,
            description=(
                "Truncate each file to this many bytes. Hard cap 65536."
            ),
        ),
    ) -> ToolResult:
        """Read files back from the running sandbox's ``/app``.

        Call this when you need to verify what's actually on disk — for
        instance before writing an overlay that references code you
        didn't author in this session, or after a ``⚠ recreated``
        notice from update_files, to rebuild your picture of /app.

        Binary files are returned as base64 with the ``encoding`` field
        set — the same shape you'd feed back to ``update_files``.
        """
        log.info(
            "MCP tool call: get_files session=%s paths=%s",
            session_id, paths,
        )
        try:
            result = await get_files_callable(session_id, paths, max_bytes_per_file)
        except HTTPException as exc:
            return _handle_http_exception(exc, tool="get_files")
        text_lines = [
            f"Files in sandbox `{result['sandbox_id']}` "
            f"(session `{result['session_id']}`):"
        ]
        for entry in result["files"]:
            marker = " (truncated)" if entry.get("truncated") else ""
            err = entry.get("error")
            if err:
                text_lines.append(f"- {entry['path']}: ERROR ({err})")
                continue
            text_lines.append(
                f"- {entry['path']} "
                f"({entry['size']:,} bytes, {entry['encoding']}){marker}"
            )
        if paths:
            # Include contents inline for path-specific reads.
            for entry in result["files"]:
                if entry.get("error"):
                    continue
                text_lines.append("")
                text_lines.append(f"--- {entry['path']} ---")
                text_lines.append(entry["content"])
        return _tool_result("\n".join(text_lines), {"ok": True, **result})

    # ── get_logs ──
    @mcp.tool()
    async def get_logs(
        session_id: str = Field(
            description=(
                "The session_id from a previous create / run / update_files "
                "response. Must match ^[A-Za-z0-9_-]{1,64}$."
            )
        ),
        lines: int = Field(
            default=100,
            description=(
                "Trailing log lines to return. Clamped 1..1000. Default 100 "
                "is enough for most tracebacks."
            ),
        ),
    ) -> ToolResult:
        """Fetch the last N lines of the running sandbox's combined
        stdout+stderr.

        Call this when the user reports the running app looks broken
        (rendered error card, "undefined is not a function", the button
        does nothing, etc.) but the last update_files response returned
        a healthy app_status. Flask, FastAPI, Express, Vite, and Next
        dev servers all print the offending traceback / stack to stdout
        before rendering the browser error. Streamlit's own exception
        handler is patched by the runner's bootstrap shim so its
        tracebacks land here too.

        Do NOT call this after a create / update_files / run FAILURE —
        those already include container logs in the tool response.
        """
        log.info(
            "MCP tool call: get_logs session=%s lines=%s",
            session_id, lines,
        )
        try:
            data = await logs_callable(session_id=session_id, lines=lines)
        except HTTPException as exc:
            return _handle_http_exception(exc, tool="get_logs")
        text = (data.get("logs") or "").rstrip()
        if not text:
            body = (
                f"No log output yet for session `{session_id}`. The "
                "container may have just spawned, or the app writes to "
                "a file instead of stdout."
            )
            return _tool_result(
                body,
                {"ok": True, "empty": True, **data},
            )
        formatted = (
            f"Container logs for session `{session_id}` "
            f"(sandbox `{data.get('sandbox_id')}`, "
            f"last {data.get('lines_requested')} lines):\n"
            "---\n"
            f"{text}\n"
            "---"
        )
        return _tool_result(
            formatted,
            {"ok": True, **data},
        )

    # ── exec ──
    @mcp.tool()
    async def exec(
        session_id: str = Field(
            description="The target session."
        ),
        command: str = Field(
            description=(
                "Shell command (via `sh -c`). NON-INTERACTIVE — stdin is "
                "closed, so anything that reads input hangs until timeout. "
                "Use -y / non-interactive flags."
            )
        ),
        timeout_seconds: int = Field(
            default=30,
            description="Command timeout in seconds. Hard cap 120.",
        ),
        working_dir: str = Field(
            default="/app",
            description=(
                "Working dir inside the container. Must be under /app; "
                "absolute paths outside /app and '..' traversal are rejected."
            ),
        ),
    ) -> ToolResult:
        """Run a non-interactive shell command inside a running sandbox.

        Use this for:
          * Installing a package the model forgot: `pip install requests`
          * Ad-hoc introspection: `ls -la /app`, `cat /tmp/streamlit.log`
          * One-off scripts: `python migrate.py`, `npx prisma generate`

        Important caveats:
          1. State DRIFT on respawn — packages installed via exec do NOT
             survive a self-heal respawn. If the model wants the dep to
             persist, ALSO write it to requirements.txt / package.json via
             update_files:
                 exec(sid, "pip install requests")             # immediate
                 update_files(sid, {"requirements.txt": "..."}) # persist
          2. No interactive commands — anything that reads stdin hangs.
          3. No long-running processes — a `&`-backgrounded process is
             killed when exec finishes. Long-running services belong in
             the runtime entrypoint set at create time.
          4. Egress allowlist still applies — a `pip install` from a
             non-allowlisted index gets a 403 from sandbox-egress. If the
             install fails with a network error, tell the user which
             registry needs to be added to tinyproxy.filter.

        Output is truncated to the last 8 KB with a marker; exit_code and
        duration are always populated.
        """
        log.info(
            "MCP tool call: exec session=%s cmd=%r timeout=%d",
            session_id, command, timeout_seconds,
        )
        try:
            result = await exec_callable(
                session_id, command, timeout_seconds, working_dir,
            )
        except HTTPException as exc:
            return _handle_http_exception(exc, tool="exec")
        head_lines = []
        if result.get("timed_out"):
            head_lines.append(
                f"Command `{command}` in sandbox `{result['sandbox_id']}` "
                f"KILLED after {timeout_seconds}s (timeout)."
            )
        else:
            head_lines.append(
                f"Command `{command}` in sandbox `{result['sandbox_id']}` "
                f"exited {result['exit_code']} in "
                f"{result['duration_ms']} ms."
            )
        head_lines.append("")
        trunc = " (last 8 KB)" if result.get("truncated") else ""
        head_lines.append(f"--- output{trunc} ---")
        head_lines.append(result["output"] or "(no output)")
        head_lines.append("---")
        return _tool_result("\n".join(head_lines), {"ok": True, **result})

    # ── patch_files ──
    @mcp.tool()
    async def patch_files(
        session_id: str = Field(
            description=(
                "The session_id from a previous create / run response."
            )
        ),
        patches: list[dict] = Field(
            description=(
                "List of hunks. Each hunk is a dict with fields: "
                "`path` (relative under /app), `start_line` (1-indexed "
                "inclusive), `end_line` (1-indexed inclusive), `expected` "
                "(the EXACT current text of those lines joined with \\n — "
                "byte-for-byte, no whitespace or trailing-newline lenience), "
                "`replacement` (what to write in place of `expected`), and "
                "optional `note` (operator-log only). All-or-nothing: if "
                "any patch fails validation, NO file is modified."
            ),
        ),
        recreate_if_gone: bool = Field(
            default=False,
            description=(
                "Accepted for interface consistency but has NO effect. "
                "patch_files depends on files that would not exist in a "
                "fresh container, so a dead container always returns an "
                "error. Call update_files first if you need to respawn."
            ),
        ),
    ) -> ToolResult:
        """Strict line-range replacement inside a running sandbox.

        Use this instead of update_files when:
          * You've just called get_files on the target file this turn AND
          * The edit changes a small fraction of the file (<20-30% is a
            good rule of thumb).

        Use update_files instead when:
          * The file is new (patch_files does NOT create files).
          * The rewrite covers most of the file.
          * You have not called get_files on the target file in this turn.

        # STRICT BYTE-FOR-BYTE MATCH

        `expected` must match `\\n`-join(current_lines[start_line-1:end_line])
        byte-for-byte. No whitespace or trailing-newline lenience. If it
        does not match, the tool returns 409 with the ACTUAL current
        content inline so you can retry in the same turn:

            patch_files failed: expected content mismatch at app.py:15-18.
            Your `expected`: ...
            Actual (currently in file): ...
            No files were modified. Call get_files(paths=["app.py"])...

        The safe pattern is: `get_files(paths=[path])` immediately before
        every `patch_files` on that file. Copy the range verbatim into
        `expected`. Do not paraphrase.

        # OVERLAP REJECTION

        Two patches on the SAME `path` whose [start_line, end_line]
        ranges intersect cause the entire call to be rejected — combine
        them into ONE patch on the merged range. This includes
        boundary-touching ranges: patches [10-15] and [15-20] overlap on
        line 15.

        # ATOMICITY

        Every patch validates in a dry-run pass BEFORE any file is
        touched. Only if every patch passes are the writes applied
        (bottom-up per file so earlier-line indices stay valid). If
        validation fails for any patch, no file is modified.

        # AFTER THE WRITE

        Post-write health probe runs (same as update_files). The
        response's `app_status` and `startup_output` tell you whether the
        edit broke the running app — act on ⚠ SUSPICIOUS immediately, in
        the same turn.

        # WORKED EXAMPLE

        You called `get_files(paths=["app.py"])` and read:
            10  def greet(name):
            11      return f"hi, {name}"

        You want to change the message. One patch:
            {
                "path": "app.py",
                "start_line": 11,
                "end_line": 11,
                "expected": "    return f\\"hi, {name}\\"",
                "replacement": "    return f\\"hello, {name}!\\"",
            }

        # NOT SELF-HEALING

        Dead container? Returns 409, tells you to call update_files with
        recreate_if_gone=true first. Fresh containers have no files to
        anchor on, so respawning inside patch_files would be wrong.

        See flow: get_runtimes → create → update_files → (patch_files |
        update_files loop) → preview → close.
        """
        log.info(
            "MCP tool call: patch_files session=%s n_patches=%d",
            session_id, len(patches or []),
        )
        try:
            result = await patch_files_callable(
                session_id, patches, recreate_if_gone,
            )
        except HTTPException as exc:
            return _handle_http_exception(exc, tool="patch_files")

        hunks = result.get("hunks_applied") or []
        files_touched = result.get("files_touched") or []
        text_lines = [
            f"Patched {len(files_touched)} file(s), {len(hunks)} hunk(s) applied. "
            f"Session `{result['session_id']}`.",
            f"URL: {result['url']} (unchanged).",
            _format_app_status(result.get("app_status")),
            _format_startup_output(
                result.get("runtime"), result.get("startup_output") or "",
            ),
            "",
            "Applied:",
        ]
        # Group hunk ranges by path so the summary reads "app.py: 2 hunks (…)".
        by_path: dict[str, list[str]] = {}
        for h in hunks:
            by_path.setdefault(h["path"], []).append(
                f"lines {h['start_line']}-{h['end_line']}"
            )
        for path in files_touched:
            ranges = by_path.get(path, [])
            n = len(ranges)
            text_lines.append(
                f"- {path}: {n} hunk(s) ({', '.join(ranges)})"
            )
        return _tool_result(
            "\n".join(text_lines),
            {"ok": True, **result},
        )

    # ── preview ──
    @mcp.tool()
    async def preview(
        session_id: str = Field(
            description="The session_id whose preview iframe you want."
        ),
    ) -> ToolResult:
        """Render the sandbox as an iframe artifact in the user's chat.

        Returns the fenced ```` ```html ```` block OpenWebUI promotes
        into its artifacts split-panel. **The model must include the
        returned string VERBATIM in the reply** — paraphrasing or
        dropping the block prevents the preview from rendering.

        Display-only: preview does NOT touch the container, does NOT
        self-heal, and does NOT count as activity (no last_used_at bump
        happens here — that's update_files / exec / get_logs). If the
        container is gone, this returns 404 with a hint to call
        update_files first with recreate_if_gone=true.

        Call this once per turn when you want the user to see the
        current state. Iterate with update_files silently between
        previews.
        """
        log.info("MCP tool call: preview session=%s", session_id)
        try:
            result = await preview_callable(session_id)
        except HTTPException as exc:
            return _handle_http_exception(exc, tool="preview")
        iframe = render_preview_html(
            result["url"], result["sandbox_id"], result["session_id"],
        )
        download_url = _download_url_for(result["url"], result["session_id"])
        text = (
            f"Preview ready. Sandbox `{result['sandbox_id']}` at "
            f"{result['url']} (expires {result['expires_at']}).\n"
            f"Session id: {result['session_id']}\n"
            f"Download source: {download_url}\n\n"
            "```html\n"
            f"{iframe}\n"
            "```"
        )
        return _tool_result(
            text,
            {
                "ok": True,
                **result,
                "download_url": download_url,
                "iframe_html": iframe,
            },
        )

    # ── close ──
    @mcp.tool()
    async def close(
        session_id: str = Field(
            description="The session_id to tear down."
        ),
    ) -> ToolResult:
        """Tear down a session's container early and release its slot.

        Idempotent — an unknown or already-closed session returns success
        with ``was_running: false``. Optional in every flow: if you
        never call it, the TTL reaper handles the cleanup on its own
        schedule. Call it explicitly when the user has said they're
        done with the preview, or when the model is moving to unrelated
        work.
        """
        log.info("MCP tool call: close session=%s", session_id)
        try:
            result = await close_callable(session_id)
        except HTTPException as exc:
            return _handle_http_exception(exc, tool="close")
        if result.get("was_running"):
            text = f"Sandbox `{result.get('sandbox_id')}` closed. Slot released."
        else:
            text = f"Session `{session_id}` was already closed."
        return _tool_result(text, {"ok": True, **result})

    # ── list_sessions ──
    @mcp.tool()
    async def list_sessions() -> ToolResult:
        """Enumerate live sandboxes globally.

        Returns one row per running sandbox: session_id, sandbox_id,
        runtime, url, timestamps, phase. Use this to recover a dropped
        session_id (e.g. the chat was compacted and the "Session id:"
        line is gone from context).

        **Multi-tenant caveat:** today this returns EVERY live session
        on the deployment, not just yours. Until per-user filtering
        lands, treat the list as global visibility.

        No slot cost. Cheap — reads from the Postgres session index.
        """
        log.info("MCP tool call: list_sessions")
        try:
            result = await list_sessions_callable()
        except HTTPException as exc:
            return _handle_http_exception(exc, tool="list_sessions")
        rows = result.get("sessions") or []
        if not rows:
            return _tool_result(
                "No live sandboxes.",
                {"ok": True, **result},
            )
        text_lines = [f"{len(rows)} live sandbox(es):"]
        for r in rows:
            text_lines.append(
                f"- session={r.get('session_id')} runtime={r.get('runtime')} "
                f"sandbox={r.get('sandbox_id')} url={r.get('url')} "
                f"expires={r.get('expires_at')} last_used={r.get('last_used_at')}"
            )
        return _tool_result("\n".join(text_lines), {"ok": True, **result})

    # ── run (one-shot: create + update + preview) ──
    async def _run_impl(
        runtime: str,
        files: dict,
        entrypoint: Optional[str],
        ttl_seconds: Optional[int],
        session_id: Optional[str],
        deletes: list[str],
        env: Optional[dict[str, str]],
        deprecated_note: Optional[str] = None,
    ) -> ToolResult:
        try:
            result = await run_callable(
                runtime=runtime,
                files=files,
                entrypoint=entrypoint,
                ttl_seconds=ttl_seconds,
                session_id=session_id,
                deletes=deletes,
                env=env,
                recreate_if_gone=True,
            )
        except HTTPException as exc:
            tr = _handle_http_exception(exc, tool="run")
            if deprecated_note:
                # Prepend deprecation note to whatever error text was produced.
                new_text = (
                    deprecated_note + "\n\n" +
                    "".join(getattr(b, "text", "") for b in tr.content)
                )
                return _tool_result(new_text, tr.structured_content or {})
            return tr

        url = result["url"]
        sandbox_id = result["sandbox_id"]
        session_id_out = result["session_id"]
        expires_at = result["expires_at"]
        reused = result.get("reused", False)
        iframe = render_preview_html(url, sandbox_id, session_id_out)
        download_url = _download_url_for(url, session_id_out)

        verb = "updated" if reused else "ready"
        n_files = len(files) if files else 0
        if reused:
            hint = (
                f"You updated {n_files} file(s) in session `{session_id_out}` "
                "— everything else in /app was preserved. To iterate silently, "
                "call update_files with the same session_id and ONLY the "
                "file(s) you change; call preview to hand the user a fresh "
                "iframe when you're ready."
            )
        else:
            hint = (
                f"To EDIT this preview on the next turn, prefer "
                f"update_files(session_id=\"{session_id_out}\", files={{...}}) "
                "with ONLY the file(s) you changed — do NOT re-send unchanged "
                "files. The container keeps running and hot-reloads on file "
                "change."
            )
        startup_section = _format_startup_output(
            result.get("runtime"), result.get("startup_output") or "",
        )
        app_status_line = _format_app_status(result.get("app_status"))
        head = (
            f"Preview {verb}. Sandbox `{sandbox_id}` at {url} "
            f"(expires {expires_at}).\n"
            f"Session id: {session_id_out}\n"
            f"Download source: {download_url}\n"
            f"{hint}\n"
            f"{app_status_line}\n"
            f"{startup_section}\n\n"
            "```html\n"
            f"{iframe}\n"
            "```"
        )
        if deprecated_note:
            head = deprecated_note + "\n\n" + head
        return _tool_result(
            head,
            {
                "ok": True,
                **result,
                "download_url": download_url,
                "iframe_html": iframe,
            },
        )

    @mcp.tool()
    async def run(
        runtime: str = Field(
            description=(
                "Runtime for the sandbox. One of the names in get_runtimes."
            )
        ),
        files: dict = Field(
            default_factory=dict,
            description=(
                "Path → content map. First call (no session_id): full "
                "initial file set. Follow-up (with session_id): overlay — "
                "only send changed files. Values are UTF-8 str or "
                "{encoding:'base64', content:'...'} for binaries."
            ),
        ),
        entrypoint: Optional[str] = Field(
            default=None,
            description=(
                "Shell command inside the sandbox, must bind port 80. "
                "Leave unset for the runtime's default. Rejected for "
                "runtimes with allows_custom_entrypoint=false. Ignored "
                "on follow-up calls."
            ),
        ),
        ttl_seconds: Optional[int] = Field(
            default=None,
            description="Idle TTL. Server clamps to SANDBOX_HARD_TTL_SECONDS.",
        ),
        session_id: Optional[str] = Field(
            default=None,
            description=(
                "Handle for a persistent preview. OMIT on the first call, "
                "PASS on follow-ups to update in place."
            ),
        ),
        deletes: list[str] = Field(
            default_factory=list,
            description=(
                "Relative paths under /app to remove on a follow-up call."
            ),
        ),
        env: Optional[dict[str, str]] = Field(
            default=None,
            description=(
                "Process env vars. Applied only on the first call. "
                "Reserved keys (HTTP_PROXY, PYTHONUNBUFFERED, etc.) rejected."
            ),
        ),
    ) -> ToolResult:
        """Convenience one-shot: create + update_files + preview.

        Use when you already have all the files ready at first mention
        and don't expect to iterate silently. The response ends with
        the fenced ```` ```html ```` iframe block — include it VERBATIM
        in your reply so OpenWebUI promotes it into the artifacts panel.

        Prefer this pattern when:
          * The user pasted code and just wants it running.
          * You have a small, complete app and no separate compose step.

        Prefer the get_runtimes → create → update_files → preview flow
        when:
          * The container should warm up while you finish writing code
            (pipelining wins on latency).
          * You want to update files silently across several turns and
            only surface the iframe once.

        Both paths produce the same URL semantics — session_id is stable,
        the container hot-reloads, self-heal is implicit here.
        """
        log.info(
            "MCP tool call: run runtime=%s session=%s n_files=%d",
            runtime, session_id, len(files or {}),
        )
        return await _run_impl(
            runtime, files, entrypoint, ttl_seconds, session_id, deletes, env,
        )

    # ── preview_app (deprecated alias for run) ──
    @mcp.tool(
        annotations={
            "title": "preview_app (deprecated — use sandbox.run)",
        },
    )
    async def preview_app(
        runtime: str = Field(
            description="Same as sandbox.run.runtime.",
        ),
        files: dict = Field(default_factory=dict),
        entrypoint: Optional[str] = Field(default=None),
        ttl_seconds: Optional[int] = Field(default=None),
        session_id: Optional[str] = Field(default=None),
        deletes: list[str] = Field(default_factory=list),
        env: Optional[dict[str, str]] = Field(default=None),
    ) -> ToolResult:
        """DEPRECATED alias for ``sandbox.run``. Kept for one release cycle
        so existing LiteLLM / model configurations don't break mid-rollout.

        Behaves identically to ``run``. Prefer ``sandbox.run`` in new code —
        this tool will be removed in a future release.
        """
        log.info("MCP tool call: preview_app (deprecated alias for run)")
        return await _run_impl(
            runtime, files, entrypoint, ttl_seconds, session_id, deletes, env,
            deprecated_note="Deprecated: use sandbox.run",
        )

    return mcp
