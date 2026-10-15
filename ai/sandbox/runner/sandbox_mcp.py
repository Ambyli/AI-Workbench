"""FastMCP wrapper — the ``sandbox`` MCP server.

Tools exposed (all invoked as ``sandbox.<name>``):

    get_runtime_types()             — describe available runtime types (catalog)
    create(runtime, ...)            — warm an empty container
    write_files(session_id, ...)    — NEW files / full rewrites, health-probe after
    read_file(session_id, path, ...)— numbered line window of one text file
    edit_file(session_id, path, old_string, new_string, replace_all?)
                                    — exact-string edit: THE way to change a file
    get_files(session_id, paths?)   — dir listing, or raw/base64 contents
    get_logs(session_id, lines?)    — tail container stdout+stderr
    exec(session_id, command, ...)  — run a non-interactive shell command
    preview(session_id)             — return the iframe artifact HTML
    close(session_id)               — teardown and release slot
    list_sessions()                 — enumerate live sandboxes
    run(runtime, files, ...)        — convenience: create + update + preview

The flow every docstring teaches (Claude Code's file-tool contract):

    get_runtime_types → create → write_files (new files)
      → read_file / edit_file (changes) → exec → preview → close

Every tool returns a ``ToolResult`` — a list of ``TextContent`` (what the
model reads) plus a ``structured_content`` JSON payload (what any
programmatic caller can parse without regex). Results are kept short on
purpose: an edit returns a confirmation and a numbered snippet, never the
whole file; exec echoes one line of the command; and content that is
already in the text (exec output, get_files contents, the edit snippet)
is not repeated in ``structured_content``.

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

from constants import (
    EDIT_SNIPPET_CONTEXT_LINES,
    EXEC_COMMAND_ECHO_CHARS,
    EXEC_DEFAULT_TIMEOUT_S,
    EXEC_HARD_TIMEOUT_S,
    EXEC_MAX_OUTPUT_BYTES,
    GET_FILES_DEFAULT_BYTES,
    GET_FILES_HARD_CAP_BYTES,
    READ_FILE_DEFAULT_LINES,
    READ_FILE_MAX_BYTES,
    READ_FILE_MAX_LINE_CHARS,
    READ_FILE_MAX_LINES,
    WRITE_GUARD_MIN_LINES,
    WRITE_GUARD_SIMILARITY,
)
from runtimes import describe_runtimes


log = logging.getLogger("sandbox-runner.mcp")


# ── Rendering helpers (shared across tools) ───────────────────────────────

def _format_diagnostic(detail: dict) -> str:
    """Turn a runner HTTPException detail dict into a diagnostic string
    for the model: ``failed: <error>``, the session id, an error-specific
    body, then the ``hint``. Errors with their own body:

      "static lint failed" (400) — compiler-style lines with a caret
      "sandbox did not become ready" (504) — the container's log tail
      read_file / edit_file refusals (``tool`` + ``kind``) — the message,
          plus the numbered closest text of a not-found old_string
      "rewrite of existing file" (409) — which files, how similar
      "payload too large" (413) / "invalid arguments" (422) — the
          validator's message

    Anything else (e.g. "sandbox container is gone", "session not found")
    is dumped as JSON minus the hint, so no field is lost.
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
    elif detail.get("tool") in ("read_file", "edit_file") and detail.get("kind"):
        # read_file / edit_file refusals: the message, plus the closest
        # region of the file for a not-found old_string so the model can
        # copy the real text without another read. The hint (appended
        # below) names the exact next call.
        lines.append("")
        lines.append(
            f"{detail['tool']} {detail.get('path', '?')}: "
            f"{detail.get('message', '')}"
        )
        closest = detail.get("closest_text")
        if detail.get("kind") == "not_found" and closest:
            pct = round(float(detail.get("similarity") or 0) * 100)
            lines.append("")
            lines.append(
                f"Closest text in the file (lines "
                f"{detail.get('closest_start_line')}-"
                f"{detail.get('closest_end_line')}, {pct}% similar):"
            )
            lines.append(closest)
    elif detail.get("kind") == "rewrite_of_existing_file":
        lines.append("")
        lines.append(detail.get("message", ""))
    elif error in ("payload too large", "invalid arguments"):
        lines.append("")
        lines.append(detail.get("message", ""))
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
            "Fix the code with the SAME session_id BEFORE the user has "
            "to report it.\n\n"
            "If the traceback points at file:line, call "
            "read_file(path, offset=<line - 5>, limit=20) to see those "
            "numbered lines, then edit_file with the broken text as "
            "old_string and your fix as new_string. Do not re-send the "
            "whole file with write_files.\n"
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


def _format_read_file(result: dict) -> str:
    """Text of a read_file result: a header, the numbered lines, and a
    footer naming the next ``offset`` when the window stopped early."""
    path = result.get("path", "?")
    start = result.get("start_line", 1)
    end = result.get("end_line", 0)
    total = result.get("total_lines", 0)
    content = result.get("content") or ""
    file_truncated = result.get("file_truncated")
    total_desc = f"{total}+" if file_truncated else str(total)
    if total == 0:
        return f"{path} is empty (0 lines)."
    if not content:
        return (
            f"{path} has {total_desc} lines; offset {start} is past the end. "
            f"Call read_file with an offset between 1 and {total}."
        )
    out = [f"{path} (lines {start}-{end} of {total_desc}):", content]
    more = result.get("more_lines") or 0
    if more:
        out.append(
            f"… {more} more line{'s' if more != 1 else ''}; call read_file "
            f"with offset={end + 1}."
        )
    if file_truncated and end >= total:
        out.append(
            f"… the file is {result.get('size', 0):,} bytes and read_file "
            f"reads only the first {_size(READ_FILE_MAX_BYTES)} (lines "
            f"1-{total}). For later lines use exec, e.g. "
            f"sed -n '{total + 1},{total + 100}p' {path}"
        )
    cut = result.get("lines_cut") or 0
    if cut:
        out.append(
            f"({cut} line{'s' if cut != 1 else ''} longer than "
            f"{READ_FILE_MAX_LINE_CHARS} characters were cut.)"
        )
    return "\n".join(out)


def _command_echo(command: str, max_chars: int = EXEC_COMMAND_ECHO_CHARS) -> str:
    """What the exec result echoes of the command: its first non-empty
    line, cut to ``max_chars``, plus ``(+N more lines)``. The model
    already has the command — echoing a 200-line heredoc back put it in
    the context twice."""
    lines = (command or "").strip("\n").split("\n")
    first = lines[0].rstrip()
    if len(first) > max_chars:
        first = first[:max_chars] + "…"
    more = len(lines) - 1
    echo = f"`{first}`"
    if more:
        echo += f" (+{more} more line{'s' if more != 1 else ''})"
    return echo


def _format_exec(command: str, timeout_seconds: int, result: dict) -> str:
    """Text of an exec result: one-line command echo, exit code / timeout,
    then the output tail."""
    echo = _command_echo(command)
    if result.get("timed_out"):
        head = (
            f"Command {echo} TIMED OUT after {timeout_seconds}s "
            "(it may still be running in the container)."
        )
    else:
        head = (
            f"Command {echo} exited {result.get('exit_code')} in "
            f"{result.get('duration_ms')} ms."
        )
    trunc = (
        f" (last {_size(EXEC_MAX_OUTPUT_BYTES)} — earlier output dropped)"
        if result.get("truncated") else ""
    )
    return "\n".join([
        head,
        "",
        f"--- output{trunc} ---",
        result.get("output") or "(no output)",
        "---",
    ])


def _exec_structured(result: dict) -> dict:
    """exec's structured_content: status fields only — the command (the
    caller has it) and the output (already in the text) are left out."""
    return {
        "ok": True,
        **{k: v for k, v in result.items() if k not in ("command", "output")},
    }


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


# ── Tool descriptions built from the limits they quote ────────────────────
# A description that states a limit is formatted from constants.py at
# import time and passed as @mcp.tool(description=...), so what the model
# reads can never drift from what the runner enforces. (FastMCP uses the
# explicit description instead of the function docstring.)


def _size(n: int) -> str:
    """``262144`` → ``256 KiB``; a non-multiple of 1024 stays in bytes."""
    return f"{n // 1024} KiB" if n % 1024 == 0 else f"{n:,} bytes"


FLOW = (
    "get_runtime_types → create → write_files (new files) → "
    "read_file / edit_file (changes) → exec → preview → close"
)

_GUARD_PCT = round(WRITE_GUARD_SIMILARITY * 100)

OVERWRITE_FIELD_DESCRIPTION = (
    f"Set true ONLY to deliberately replace an existing text file of "
    f"{WRITE_GUARD_MIN_LINES}+ lines with content that keeps {_GUARD_PCT}% "
    "or more of its lines. Without it that write is refused "
    "(rewrite_of_existing_file) — use edit_file for such changes."
)

WRITE_FILES_DESCRIPTION = f"""Write NEW files (or completely rewrite one). **Only for new files or
complete rewrites. Changing part of an existing file? Use edit_file** —
write_files refuses to rewrite a mostly-unchanged file unless
overwrite=true.

Semantics:
  * ``files`` is a REPLACEMENT set — every listed path is overwritten
    byte-for-byte with what you supply. Unlisted paths in /app are
    preserved (this tool doesn't clear /app).
  * Rewrite guard: if a listed path already exists as a text file of
    {WRITE_GUARD_MIN_LINES}+ lines and your content keeps {_GUARD_PCT}%+ of its lines, the
    WHOLE call is refused (nothing is written) with
    ``rewrite_of_existing_file``. That is an edit — do it with read_file +
    edit_file. Pass overwrite=true only when you really mean to replace
    the file.
  * ``deletes`` explicitly removes files under /app.
  * Same URL, same session_id — dev server hot-reloads.
  * Runs static Python lint on .py files before writing.
  * After the reload settles, runs a health probe against the app's root
    path and surfaces the result inline. If the reload broke the app,
    you'll see the HTTP status here without a second tool call.
  * Container gone? Default behavior is to error out with a clear "call
    again with recreate_if_gone=true" message. This prevents silent state
    loss — env is preserved on respawn, but files you installed via
    ``exec`` are LOST.

Flow: {FLOW}.
"""

READ_FILE_DESCRIPTION = f"""Read part of a text file with line numbers — the way to look at code
before you change it with edit_file.

Output is ``cat -n`` style: each line is ``<number><TAB><text>``. The
number and TAB are NOT part of the file — when you copy a line into
edit_file's ``old_string``, copy only the text after the TAB.

Read only what you need: ``offset`` / ``limit`` select a window (default
the first {READ_FILE_DEFAULT_LINES} lines, at most {READ_FILE_MAX_LINES}). A traceback names file:line —
``read_file(path, offset=line-5, limit=20)`` shows that spot. When more
lines follow, the result says which ``offset`` to ask for next. Lines
longer than {READ_FILE_MAX_LINE_CHARS} characters are cut, and only the
first {_size(READ_FILE_MAX_BYTES)} of a file can be read this way.

Text only: a binary or non-UTF-8 file is refused — use ``get_files`` (it
returns base64) for those. For a listing of /app call ``get_files`` with
no paths.
"""

EDIT_FILE_DESCRIPTION = f"""**The preferred way to change a file.** Replaces ``old_string`` with
``new_string`` in one existing file — you send only the part that
changes, never the whole file.

Copy ``old_string`` exactly from ``read_file`` output, without the
line-number prefix (the number and TAB before each line). It must match
the file exactly — every space, tab and line — and occur exactly once
unless ``replace_all=true``. All or nothing: if it doesn't match,
nothing is written and the result says why:

  * not found → shows the closest text in the file, numbered, so you can
    copy the real text and retry without re-reading the file;
  * found N times → lists the lines; add surrounding lines to old_string
    to make it unique, or set replace_all=true.

The result is a short confirmation with the changed lines and
{EDIT_SNIPPET_CONTEXT_LINES} lines of context on each side — not the whole file — plus the
app's health probe and any new startup output. ``.py`` files are
syntax-checked first; a syntax error is reported and NOT written.
Creating a file is write_files' job.

# WORKED EXAMPLE

``read_file(session_id=sid, path="app.js", offset=10, limit=3)`` returned:
    10\tfunction greet(name) {{
    11\t  return "hi, " + name;
    12\t}}

To change the message:
    edit_file(session_id=sid, path="app.js",
              old_string='  return "hi, " + name;',
              new_string='  return `Hello, ${{name}}!`;')

Flow: {FLOW}.
"""

EXEC_DESCRIPTION = f"""Run a non-interactive shell command inside a running sandbox.

Use this for:
  * Installing a package the model forgot: `pip install requests`
  * Running tests / scripts: `node test.js`, `python migrate.py`
  * Ad-hoc introspection: `ls -la /app`, `cat /tmp/streamlit.log`

Don't use it to write or edit source files (heredocs, sed): new files go
through write_files, changes through edit_file — those are linted,
health-probed and keep file ownership right.

Important caveats:
  1. State DRIFT on respawn — packages installed via exec do NOT survive a
     self-heal respawn. If the dependency must persist, ALSO write it to
     requirements.txt / package.json:
         exec(sid, "pip install requests")      # immediate
         edit_file(sid, "requirements.txt", ...) # persist
  2. No interactive commands — anything that reads stdin hangs.
  3. No long-running processes — a `&`-backgrounded process is killed
     when exec finishes. Long-running services belong in the runtime
     entrypoint set at create time.
  4. Egress allowlist still applies — a `pip install` from a
     non-allowlisted index gets a 403 from sandbox-egress. If the install
     fails with a network error, tell the user which registry needs to be
     added to tinyproxy.filter.

Output: the LAST {_size(EXEC_MAX_OUTPUT_BYTES)} (errors are at the end), marked when earlier
output was dropped; exit_code and duration are always populated. The
result echoes only the first line of your command (at most
{EXEC_COMMAND_ECHO_CHARS} characters).
"""


# ── Build the MCP server ──────────────────────────────────────────────────

def build_mcp(
    run_callable: Callable[..., Any],
    logs_callable: Callable[..., Any],
    create_callable: Callable[..., Any],
    write_files_callable: Callable[..., Any],
    read_file_callable: Callable[..., Any],
    edit_file_callable: Callable[..., Any],
    get_files_callable: Callable[..., Any],
    exec_callable: Callable[..., Any],
    preview_callable: Callable[..., Any],
    close_callable: Callable[..., Any],
    list_sessions_callable: Callable[..., Any],
) -> FastMCP:
    """Wire up the tool surface. All callables live in ``app.py`` and get
    passed in to break the ``sandbox_mcp`` → ``app`` import cycle."""

    mcp = FastMCP(name="sandbox")

    # ── get_runtime_types ──
    @mcp.tool()
    async def get_runtime_types() -> ToolResult:
        """Describe the RUNTIME TYPES this deployment supports (static,
        python, node, ...). This is a CATALOG, not a session status
        check — it does not know about your session, your container, or
        whether anything is running. It answers "what stacks can I ask
        `create` or `run` to spawn?"

        Each entry describes one runtime type's summary, default
        entrypoint, pre-baked packages, and an example ``files`` map.
        **Call this FIRST if you're unsure which runtime fits the
        user's request** — it saves guessing and shows you which
        packages are already installed so you don't include them in
        requirements.txt.

        For the state of a running sandbox, use ``get_logs``,
        ``get_files``, ``read_file`` or ``list_sessions`` — NOT this tool.

        Flow: get_runtime_types → create → write_files (new files) →
        read_file / edit_file (changes) → exec → preview → close.
        Or the one-shot: get_runtime_types → run.
        """
        log.info("MCP tool call: get_runtime_types")
        runtimes = describe_runtimes()
        lines = ["Available runtime types:"]
        for rt in runtimes:
            lines.append(f"- {rt['name']}: {rt['summary']}")
        return _tool_result(
            "\n".join(lines),
            {"runtime_types": runtimes},
        )

    # ── create ──
    @mcp.tool()
    async def create(
        runtime: str = Field(
            description=(
                "Runtime for the empty warming container. One of the "
                "names in get_runtime_types."
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
        chat_id: Optional[str] = Field(
            default=None,
            description=(
                "Id of the chat you're answering in. Pass it and the "
                "runner enforces ONE sandbox per chat: if this chat "
                "already created one, create returns THAT session instead "
                "of spawning a duplicate (no extra slot used). Use the "
                "OpenWebUI chat id, or any stable per-conversation string. "
                "Strongly recommended — it stops a chat from leaking "
                "containers across turns when the session_id is lost."
            ),
        ),
    ) -> ToolResult:
        """Reserve an empty warming container and return the session
        handle. Use this FIRST when you know you'll need a preview but
        haven't written the code yet — the container starts warming
        while you finish thinking, so the follow-up ``write_files``
        hits a warm container and hot-reloads instantly.

        The URL returned serves a "sandbox warming" placeholder page
        (or Streamlit's own "warming" script for python) until you call
        ``write_files`` with your real code.

        Pass ``chat_id`` to guarantee one sandbox per conversation: a
        second create for the same chat returns the existing session
        (``duplicate_create_prevented``) rather than spawning again.

        Flow: get_runtime_types → create → write_files (new files) →
        read_file / edit_file (changes) → exec → preview → close.

        Consumes one slot from SANDBOX_MAX_CONCURRENT (except when a
        duplicate create is short-circuited — that costs nothing).
        """
        log.info("MCP tool call: create runtime=%s chat=%s", runtime, chat_id)
        try:
            result = await create_callable(
                runtime, ttl_seconds, entrypoint, env, chat_id,
            )
        except HTTPException as exc:
            return _handle_http_exception(exc, tool="create")
        sid = result["session_id"]
        if result.get("duplicate_create_prevented"):
            text = (
                f"This chat already has a sandbox — reusing it instead of "
                f"creating a second one. Session id: {sid}. "
                f"Runtime: {result['runtime']}. URL: {result['url']}.\n"
                "Change existing files with read_file / edit_file(session_id=\""
                + sid + "\", ...), add new ones with write_files, or call "
                "preview(session_id=\"" + sid + "\") to show it."
            )
        else:
            text = (
                f"Sandbox created. Session id: {sid}. "
                f"Runtime: {result['runtime']}. "
                f"URL: {result['url']} (expires {result['expires_at']}).\n"
                "Status: warming — dev server is booting with placeholder files.\n"
                "Next: call write_files(session_id=\"" + sid + "\", files={...}) "
                "with your actual code. After that, change files with "
                "read_file + edit_file — not by re-sending them."
            )
        return _tool_result(text, {"ok": True, **result})

    # ── write_files ──
    @mcp.tool(description=WRITE_FILES_DESCRIPTION)
    async def write_files(
        session_id: str = Field(
            description=(
                "The session_id from a previous create / run response."
            )
        ),
        files: dict = Field(
            default_factory=dict,
            description=(
                "Path → FULL content of NEW files (or of files you are "
                "rewriting from scratch). Every listed file is replaced "
                "wholesale; unlisted files are preserved. Values are UTF-8 "
                "str, or {\"encoding\":\"base64\",\"content\":\"...\"} for "
                "binaries. Per-file cap: SANDBOX_MAX_FILE_BYTES; total cap: "
                "SANDBOX_MAX_PAYLOAD_BYTES. To change part of an existing "
                "file use edit_file instead."
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
        overwrite: bool = Field(
            default=False,
            description=OVERWRITE_FIELD_DESCRIPTION,
        ),
    ) -> ToolResult:
        # Description: WRITE_FILES_DESCRIPTION (built from constants.py).
        log.info(
            "MCP tool call: write_files session=%s n_files=%d recreate=%s "
            "overwrite=%s",
            session_id, len(files or {}), recreate_if_gone, overwrite,
        )
        try:
            result = await write_files_callable(
                session_id, files, deletes, recreate_if_gone, overwrite,
            )
        except HTTPException as exc:
            return _handle_http_exception(exc, tool="write_files")
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

    # ── read_file ──
    @mcp.tool(description=READ_FILE_DESCRIPTION)
    async def read_file(
        session_id: str = Field(
            description="The session_id returned by create / run."
        ),
        path: str = Field(
            description="Relative path of ONE text file under /app.",
        ),
        offset: int = Field(
            default=1,
            description="1-based line number to start reading at.",
        ),
        limit: int = Field(
            default=READ_FILE_DEFAULT_LINES,
            description=(
                f"Number of lines to return (max {READ_FILE_MAX_LINES})."
            ),
        ),
    ) -> ToolResult:
        # Description: READ_FILE_DESCRIPTION (built from constants.py).
        log.info(
            "MCP tool call: read_file session=%s path=%s offset=%s limit=%s",
            session_id, path, offset, limit,
        )
        try:
            result = await read_file_callable(session_id, path, offset, limit)
        except HTTPException as exc:
            return _handle_http_exception(exc, tool="read_file")
        text = _format_read_file(result)
        structured = {
            "ok": True,
            "session_id": result.get("session_id"),
            "sandbox_id": result.get("sandbox_id"),
            "path": result.get("path"),
            "start_line": result.get("start_line"),
            "end_line": result.get("end_line"),
            "total_lines": result.get("total_lines"),
            "truncated": result.get("truncated"),
        }
        return _tool_result(text, structured)

    # ── edit_file ──
    @mcp.tool(description=EDIT_FILE_DESCRIPTION)
    async def edit_file(
        session_id: str = Field(
            description="The session_id returned by create / run."
        ),
        path: str = Field(
            description="Relative path under /app of an EXISTING text file.",
        ),
        old_string: str = Field(
            description=(
                "The exact text to replace, copied from read_file output "
                "WITHOUT the line-number prefix. Include enough lines to "
                "make it unique in the file."
            ),
        ),
        new_string: str = Field(
            description="The text to put in its place (must differ).",
        ),
        replace_all: bool = Field(
            default=False,
            description=(
                "Replace every occurrence of old_string (e.g. renaming a "
                "variable). Default: old_string must occur exactly once."
            ),
        ),
    ) -> ToolResult:
        # Description: EDIT_FILE_DESCRIPTION (built from constants.py).
        log.info(
            "MCP tool call: edit_file session=%s path=%s replace_all=%s",
            session_id, path, replace_all,
        )
        try:
            result = await edit_file_callable(
                session_id, path, old_string, new_string, replace_all,
            )
        except HTTPException as exc:
            return _handle_http_exception(exc, tool="edit_file")
        n = result.get("replacements", 1)
        if n == 1:
            head = (
                f"Edited {result['path']}: 1 replacement, lines "
                f"{result['start_line']}-{result['end_line']}."
            )
        else:
            where = ", ".join(str(x) for x in result.get("match_lines") or [])
            head = (
                f"Edited {result['path']}: {n} replacements (starting "
                f"on lines {where}); the first is shown."
            )
        text_lines = [
            head,
            result.get("snippet") or "",
            _format_app_status(result.get("app_status")),
            _format_startup_output(
                result.get("runtime"), result.get("startup_output") or "",
            ),
        ]
        structured = {
            k: v for k, v in result.items() if k != "snippet"
        }
        return _tool_result("\n".join(text_lines), {"ok": True, **structured})

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
            default=GET_FILES_DEFAULT_BYTES,
            description=(
                "Truncate each file to this many bytes. Hard cap "
                f"{GET_FILES_HARD_CAP_BYTES}."
            ),
        ),
    ) -> ToolResult:
        """List ``/app``, or fetch files raw (binary as base64).

        With no ``paths``: a listing of every file under /app with its
        size — the way to see what exists. With ``paths``: each file's
        raw content (first ``max_bytes_per_file`` bytes), binary files as
        base64 — the same shape ``write_files`` accepts.

        To read SOURCE code before changing it, use ``read_file``
        instead: it returns numbered lines for a window of the file,
        which is what ``edit_file`` needs, without pulling the whole file
        into the conversation.
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
            encoding = entry.get("encoding")
            kind = f", {encoding}" if encoding else ""
            text_lines.append(
                f"- {entry['path']} ({entry.get('size', 0):,} bytes{kind}){marker}"
            )
        structured = {"ok": True, **result}
        if paths:
            # Contents go inline in the text for path-specific reads — and
            # only there: structured_content carries the metadata, not a
            # second copy of every file.
            for entry in result["files"]:
                if entry.get("error"):
                    continue
                text_lines.append("")
                text_lines.append(f"--- {entry['path']} ---")
                text_lines.append(entry.get("content", ""))
                if entry.get("truncated"):
                    shown = len((entry.get("content") or "").encode("utf-8"))
                    text_lines.append(
                        f"--- truncated: first {shown:,} of "
                        f"{entry.get('size', 0):,} bytes (use read_file for "
                        "numbered windows of a long text file) ---"
                    )
            structured["files"] = [
                {k: v for k, v in entry.items() if k != "content"}
                for entry in result["files"]
            ]
        return _tool_result("\n".join(text_lines), structured)

    # ── get_logs ──
    @mcp.tool()
    async def get_logs(
        session_id: str = Field(
            description=(
                "The session_id from a previous create / run / write_files "
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
        stdout+stderr AND the interleaved browser-side events forwarded
        by the sandbox-proxy shim.

        Call this when the user reports the running app looks broken
        (rendered error card, "undefined is not a function", the button
        does nothing, etc.) but the last write_files response returned
        a healthy app_status. Flask, FastAPI, Express, Vite, and Next
        dev servers all print the offending traceback / stack to stdout
        before rendering the browser error. Streamlit's own exception
        handler is patched by the runner's bootstrap shim so its
        tracebacks land here too.

        Browser-side events surface as lines prefixed ``[browser]``:
        ``console.error``, ``console.warn``, ``window.onerror``, and
        unhandled promise rejections are captured unconditionally.
        ``console.log`` / ``console.info`` / ``console.debug`` are
        captured ONLY when the user opened the preview with ``?_debug=1``
        in the URL — kept off by default so a chatty React app doesn't
        drown out the server-side output. Browser events are
        rate-limited to 100/minute per sandbox; overage collapses to a
        single ``[browser rate-limited: N events dropped]`` line so the
        agent can see they're being lost.

        Known browser-capture gaps: React error boundaries (they render
        their own UI without hitting window.onerror), Web Worker errors
        (window-scoped listeners only), errors inside cross-origin
        iframes the app embeds, and network failures the app catches
        and swallows. Apps that ship a strict
        ``Content-Security-Policy: script-src 'self'`` refuse the shim
        entirely — no browser events will appear for those.

        Do NOT call this after a create / write_files / run FAILURE —
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
    @mcp.tool(description=EXEC_DESCRIPTION)
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
            default=EXEC_DEFAULT_TIMEOUT_S,
            description=(
                f"Command timeout in seconds. Hard cap {EXEC_HARD_TIMEOUT_S}."
            ),
        ),
        working_dir: str = Field(
            default="/app",
            description=(
                "Working dir inside the container. Must be under /app; "
                "absolute paths outside /app and '..' traversal are rejected."
            ),
        ),
    ) -> ToolResult:
        # Description: EXEC_DESCRIPTION (built from constants.py).
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
        return _tool_result(
            _format_exec(command, timeout_seconds, result),
            _exec_structured(result),
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
        happens here — that's write_files / read_file / edit_file /
        exec). If the container is gone, this returns 404 with a hint to
        call write_files first with recreate_if_gone=true.

        Call this once per turn when you want the user to see the
        current state. Iterate silently between previews — edit_file for
        changes, exec to run tests.
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
        chat_id: Optional[str] = None,
        overwrite: bool = False,
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
                chat_id=chat_id,
                overwrite=overwrite,
            )
        except HTTPException as exc:
            return _handle_http_exception(exc, tool="run")

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
                "— everything else in /app was preserved. To iterate "
                "silently: read_file + edit_file for changes to existing "
                "files, write_files only for new ones, exec to run tests. "
                "Call preview to hand the user a fresh iframe when you're "
                "ready."
            )
        else:
            hint = (
                f"To CHANGE this app: read_file(session_id=\"{session_id_out}\", "
                "path=...) to see the numbered lines, then "
                f"edit_file(session_id=\"{session_id_out}\", path=..., "
                "old_string=..., new_string=...) — send only the text that "
                f"changes. Add NEW files with write_files(session_id=\"{session_id_out}\", "
                "files={...}). Never re-send unchanged files. The container "
                "keeps running and hot-reloads."
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
                "Runtime for the sandbox. One of the names in get_runtime_types."
            )
        ),
        files: dict = Field(
            default_factory=dict,
            description=(
                "Path → content map. First call (no session_id): full "
                "initial file set. Follow-up (with session_id): overlay — "
                "only NEW files; change existing ones with edit_file. "
                "Values are UTF-8 str or {encoding:'base64', content:'...'} "
                "for binaries."
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
        chat_id: Optional[str] = Field(
            default=None,
            description=(
                "Id of the chat you're answering in. With no session_id, "
                "the runner reuses this chat's existing container instead "
                "of spawning a new one — so repeated run calls in one chat "
                "update the same preview even if you lost the session_id. "
                "Pass it on every run for the chat."
            ),
        ),
        overwrite: bool = Field(
            default=False,
            description="Follow-up calls only. " + OVERWRITE_FIELD_DESCRIPTION,
        ),
    ) -> ToolResult:
        """Convenience one-shot: create + write_files + preview.

        Use when you already have all the files ready at first mention
        and don't expect to iterate silently. The response ends with
        the fenced ```` ```html ```` iframe block — include it VERBATIM
        in your reply so OpenWebUI promotes it into the artifacts panel.

        Prefer this pattern when:
          * The user pasted code and just wants it running.
          * You have a small, complete app and no separate compose step.

        Prefer the flow get_runtime_types → create → write_files (new
        files) → read_file / edit_file (changes) → exec → preview → close
        when:
          * The container should warm up while you finish writing code
            (pipelining wins on latency).
          * You want to update files silently across several turns and
            only surface the iframe once.

        After the first call, change existing files with read_file +
        edit_file, not by calling run again with the whole file: a
        follow-up run that re-sends a mostly-unchanged existing file is
        refused (rewrite_of_existing_file) unless overwrite=true.

        Both paths produce the same URL semantics — session_id is stable,
        the container hot-reloads, self-heal is implicit here.
        """
        log.info(
            "MCP tool call: run runtime=%s session=%s chat=%s n_files=%d "
            "overwrite=%s",
            runtime, session_id, chat_id, len(files or {}), overwrite,
        )
        return await _run_impl(
            runtime, files, entrypoint, ttl_seconds, session_id, deletes, env,
            chat_id, overwrite,
        )

    return mcp
