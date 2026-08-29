"""FastMCP wrapper exposing the ``preview_app`` tool.

Registered on the same FastAPI app under ``/mcp`` so LiteLLM (see
``ai/litellm/litellm_config.yaml`` — ``mcp_servers.sandbox``) can advertise
the tool to any Qwen/Claude/GPT model that supports tool calling.

## What the tool returns

Following the OpenWebUI Artifacts docs
(https://docs.openwebui.com/features/chat-conversations/chat-features/code-execution/artifacts/),
the tool result is a **string** containing a short summary plus a
fenced ``html`` code block with a self-contained webpage that
navigates its own srcdoc iframe to the sandbox URL via
``<meta http-equiv="refresh">`` + JS fallback. When the model relays
the tool result to the user, OpenWebUI's ContentRenderer picks up the
``html`` block (``['html','svg'].includes(normalizedLang)``) and
promotes it into the artifacts split-panel — no additional model
prompting or plugin config required.

The URL/sandbox_id/expires_at values are also present in plain text
inside the same string, so a text-only client (or a model that
paraphrases) still surfaces the useful information.

If you'd rather have OpenWebUI render the preview directly (bypassing
the model), configure it as a Tool Server pointed at
``http://sandbox-runner:8000/tool`` — see ``app.py``'s ``/tool``
sub-app which returns an ``HTMLResponse`` with ``Content-Disposition:
inline`` per the same OpenWebUI docs.
"""

from __future__ import annotations

import html
import json
import logging
import secrets
from typing import Optional

from fastapi import HTTPException
from fastmcp import FastMCP
from pydantic import Field

from runtimes import describe_runtimes


log = logging.getLogger("sandbox-runner.mcp")


def _format_diagnostic(detail: dict) -> str:
    """Turn a runner HTTPException detail dict into a diagnostic string
    for the model. Two shapes we handle explicitly:

    Static lint (400): detail = {error, session_id, errors[], hint}
        where each error is {path,line,offset,message,text}. We format
        as a compact `path:line:col: message` list plus the offending
        source line — same shape as a compiler / linter output the
        model has been trained on, so it can pattern-match a fix.

    Readiness failure (504): detail = {error, session_id, logs, hint}
        We include the log tail verbatim (that's where the traceback
        lives) plus the hint. If the log tail is empty, we say so
        explicitly rather than showing a bare header.

    Unknown shapes fall back to a JSON dump so we surface *something*
    rather than swallow the error into a bare-error tool response."""
    log.debug(
        "_format_diagnostic: error=%r session=%s keys=%s",
        detail.get("error"), detail.get("session_id"), sorted(detail.keys()),
    )
    error = detail.get("error", "spawn failed")
    session_id = detail.get("session_id")
    hint = detail.get("hint", "")
    lines = [f"preview_app failed: {error}"]
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
    else:
        # Unknown structured error — dump what we know.
        lines.append("")
        lines.append(json.dumps(detail, indent=2, default=str))

    if hint:
        lines.append("")
        lines.append(hint)
    return "\n".join(lines)


# Substrings that mean "something went wrong at startup" when they show
# up in the container's stdout tail. Curated to avoid false-positive
# matches on legitimate framework noise:
#   - "Warning" is EXCLUDED — npm and pip love to warn about deprecated
#     packages and semver ranges, and it's not actionable at spawn time
#   - "Error:" (with colon) is INCLUDED — this is the Python exception
#     pattern; a bare "error" or "ERROR" would match nginx's access log
#     entry format
#   - lowercase "error" is EXCLUDED — same reason as Warning; too noisy
#     for HTTP access-log style output
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
)


def _format_startup_output(runtime: Optional[str], output: str) -> str:
    """Render the "Startup output" section that rides on every
    successful ``preview_app`` response.

    Four cases, in order of precedence:
      1. ``static`` runtime → skip. nginx access logs are noise; there
         are no user tracebacks to surface.
      2. output contains a marker from _SUSPICIOUS_STARTUP_MARKERS →
         inline the whole tail with a ⚠ header so the model reads it
         and acts on it BEFORE handing back to the user.
      3. output is non-empty but clean → one-liner acknowledging the
         check ran. For python we add a hint that streamlit's absence
         from stdout doesn't imply the app is fine.
      4. output is empty → one-liner saying so. Container may have
         just started or the runtime writes to a file instead.
    """
    if runtime == "static":
        return (
            "Startup output: skipped (static runtime — nginx serves "
            "files, no application-level output to surface)."
        )
    stripped = (output or "").strip()
    if not stripped:
        return (
            "Startup output: (empty — container just spawned, or app "
            "writes to a file instead of stdout, or runtime is "
            "streamlit which never writes user tracebacks here)."
        )
    lowered = stripped
    hits = [m for m in _SUSPICIOUS_STARTUP_MARKERS if m in lowered]
    if hits:
        return (
            "Startup output: ⚠ SUSPICIOUS — found "
            f"{', '.join(hits[:3])}"
            f"{' (and more)' if len(hits) > 3 else ''}. "
            "Fix the code and call preview_app again with the SAME "
            "session_id BEFORE the user has to report it.\n"
            "```\n"
            f"{stripped}\n"
            "```"
        )
    n_lines = stripped.count("\n") + 1
    return f"Startup output: clean ({n_lines} lines)."


def render_preview_html(url: str, sandbox_id: str, session_id: str) -> str:
    """Full HTML document that navigates OpenWebUI's artifact iframe
    (or the Tool Server response iframe) to the running sandbox URL.

    Shared by both the MCP ``preview_app`` tool (which embeds this in a
    ``` ```html ``` fenced block) and the OpenWebUI Tool Server route
    (which returns it as an ``HTMLResponse`` with
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

    The leading HTML comment carries a per-response nonce (sandbox_id +
    session_id) so OpenWebUI's ``autoOpenedArtifactIds`` sees each
    update as a distinct artifact and re-opens the split panel if the
    user closed it between turns. Without the nonce, follow-up updates
    with the same URL would produce identical HTML and OpenWebUI would
    treat them as the *same* artifact — panel wouldn't re-open.

    A separate cache-busting nonce goes into the navigation URL's query
    string. Sandbox_id stays constant across in-place updates, so a
    byte-identical URL would let the browser serve the previous
    document from its cache (or the artifact iframe's bfcache) — even
    with ``Cache-Control: no-store`` set by Caddy, some browsers still
    reuse a prior document when navigating to the exact same URL.
    Salting ``?v={hex}`` per response forces a fresh navigation every
    time. The sandbox app doesn't care about the query string; nginx,
    Streamlit, Vite, and Express all ignore unknown query params."""
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


def build_mcp(run_callable, logs_callable) -> FastMCP:
    """Build the FastMCP instance wired to the runner's spawn code path.

    ``run_callable`` is an async callable ``(RunRequest) → RunResponse``
    and ``logs_callable`` is an async callable
    ``(session_id, lines) → dict``. Both are provided by ``app.py`` —
    kept as parameters rather than imports to avoid a circular
    dependency between ``sandbox_mcp.py`` and ``app.py``.
    """
    mcp = FastMCP(name="sandbox")

    @mcp.tool()
    async def list_runtimes() -> list[dict]:
        """Return the sandbox runtimes available on this deployment.

        Each entry describes one runtime's summary, default entrypoint,
        pre-baked packages, and an example ``files`` map. **Call this
        FIRST if you're unsure which runtime fits the user's request** —
        it saves guessing (and the resulting readiness-timeout errors)
        and shows you which packages are already installed so you don't
        wastefully include them in requirements.txt.
        """
        log.info("MCP tool call: list_runtimes")
        return describe_runtimes()

    @mcp.tool()
    async def preview_app(
        runtime: str = Field(
            description=(
                "One of the names returned by list_runtimes. Currently: "
                "'static' (HTML/CSS/JS via nginx), 'python' (Streamlit/"
                "Gradio/Flask/FastAPI), or 'node' (Vite/React/Next/"
                "Express). Call list_runtimes first if unsure — it "
                "returns the full runtime schema including which one "
                "fits a given framework."
            )
        ),
        files: dict[str, str] = Field(
            default_factory=dict,
            description=(
                "Map of relative path → file contents. "
                "\n\n"
                "FIRST call (no session_id): include every file the app "
                "needs — code, requirements.txt / package.json if extra "
                "packages are required, assets. "
                "\n\n"
                "FOLLOW-UP call (with session_id): include ONLY the "
                "file(s) that changed. Anything you do not list stays in "
                "the running container's /app and continues to be served. "
                "Re-sending unchanged files is wasted tokens AND wasted "
                "container CPU — the file gets rewritten with identical "
                "bytes but the dev server (Streamlit / Vite) still fires "
                "a mtime-based reload for it. Ship the diff, not the whole "
                "project."
            ),
        ),
        entrypoint: Optional[str] = Field(
            default=None,
            description=(
                "Shell command run inside the sandbox. Must bind to port "
                "80. Leave unset to use the runtime's default (streamlit "
                "for python, serve for node, nginx for static). NOTE: "
                "the 'static' runtime does NOT accept a custom "
                "entrypoint — nginx is fixed. Setting one for static "
                "will return a 400 error. Ignored on follow-up calls "
                "(the entrypoint was fixed at spawn time)."
            ),
        ),
        ttl_seconds: Optional[int] = Field(
            default=None,
            description=(
                "How long the preview should stay alive if idle. Server "
                "clamps to SANDBOX_HARD_TTL_SECONDS. Default: server "
                "picks a sensible value (~15 min)."
            ),
        ),
        session_id: Optional[str] = Field(
            default=None,
            description=(
                "Handle for a persistent preview. OMIT on the first "
                "call — the server generates one and returns it. PASS "
                "the same value back on follow-up calls to update files "
                "in place. Same URL, no respawn, dev server "
                "hot-reloads. The value is printed in the 'Session id: "
                "…' line of the previous tool response."
            ),
        ),
        deletes: list[str] = Field(
            default_factory=list,
            description=(
                "Relative paths (under /app) to REMOVE from the sandbox. "
                "Use on follow-up calls when the model renames or drops "
                "a file — otherwise the old file stays in place. "
                "Ignored on the first call."
            ),
        ),
    ) -> str:
        """Spawn — or update — a live app preview and return a fenced
        ```html block that OpenWebUI renders in its artifacts split-panel.

        Every successful response also inlines a "Startup output" tail
        of the container's stdout+stderr so you catch silent failures
        (import errors, warning-only misconfigurations, tracebacks
        that surfaced on the first HTTP hit) without a second tool
        call. If the tail contains error signals, act on them in the
        SAME turn before the user has to report the problem. If the
        tail is empty or explicitly labelled "clean", proceed as
        usual. ``get_sandbox_logs`` still exists for the case where
        the app breaks later on a user interaction the initial fetch
        couldn't have triggered.

        # DELTA UPDATES — this is how most calls should work

        This tool has TWO modes, chosen by whether ``session_id`` is
        present:

        1. FIRST call (no session_id) — you're starting a new preview.
           Send every file the app needs. The server generates a
           session_id and returns it.

        2. FOLLOW-UP call (WITH session_id) — you're editing a preview
           that's already open in the user's artifacts panel. Send ONLY
           the file(s) that changed. The container keeps running, files
           you don't list stay in /app untouched, the same URL keeps
           serving, and the dev server hot-reloads (Streamlit reruns on
           mtime change, Vite fires HMR, nginx serves the new bytes).
           The iframe in the user's chat updates in place — same
           artifact, no new URL, no lost scroll position, no lost in-app
           state.

        # WORKED EXAMPLE — one initial call, one edit

        User: "Build me a red counter."
        You:
            preview_app(
                runtime="static",
                files={
                    "index.html": "<html>...<div id=n>0</div>...</html>",
                    "style.css":  "#n { color: red; font-size: 4rem; }",
                    "app.js":     "let n = 0; document.getElementById('n')..."
                }
            )
        Server → "Session id: abc123 …"

        User: "Make it blue."
        You (correct — DELTA):
            preview_app(
                runtime="static",
                session_id="abc123",       # reuse!
                files={
                    "style.css": "#n { color: blue; font-size: 4rem; }"
                }
                # DO NOT re-send index.html or app.js.
                # They are already in /app and unchanged. Sending them
                # again is wasted tokens and forces a spurious reload.
            )

        You (WRONG — full rewrite):
            preview_app(
                runtime="static",
                # forgot session_id → new container, new URL, user loses
                # their state and the panel may not re-open
                files={"index.html": "...", "style.css": "...", "app.js": "..."}
            )

        # DELETING or RENAMING files

        The delta model preserves everything you don't list. If the user
        asks you to rename ``old.html`` to ``new.html``, send
        ``files={"new.html": "..."}`` AND ``deletes=["old.html"]``.
        Otherwise both files will exist in /app.

        # WHEN THE PREVIEW LOOKS BROKEN TO THE USER

        Two failure modes and how to debug each:

        1. ``preview_app`` returned an ERROR (400 with lint errors, or
           504 with container logs). The error response ALREADY contains
           the diagnostic — read it, fix the code, call ``preview_app``
           again with the same session_id. No need to call any other
           tool.

        2. ``preview_app`` returned a normal ready/updated response but
           the user says the rendered app looks wrong ("undefined is not
           a function", "the button does nothing", a Streamlit error
           card in the preview). Call ``get_sandbox_logs(session_id=…)``
           to fetch the container's stdout+stderr. Streamlit / Flask /
           Vite dev servers print the Python traceback / JS stack there
           before rendering an error card. Read the logs, fix the code,
           call ``preview_app`` again with the same session_id. This
           saves the user having to copy-paste the error back to you.

        # DOWNLOADING THE SOURCE

        Every response includes a ``Download source:`` line with a URL
        that streams the sandbox's ``/app`` back as a plain tar archive.
        If the user asks to save/download/keep/export the code, share
        that URL — it's authenticated by the same oauth2-proxy cookie as
        the preview iframe and stays valid across self-heal spawns
        because it resolves the session at request time.

        # RELAYING THE RESULT

        Include the returned string VERBATIM in your response to the
        user. The ```html block is what makes OpenWebUI promote the
        preview into its artifacts split-panel. Paraphrasing or removing
        it prevents the preview from rendering. The "Session id: …" line
        above the block is what you grep back on the next turn — do not
        remove or reword it.

        The iframe URL is served by ``sandbox-proxy`` on the local
        network. It is not reachable from outside this OpenWebUI
        deployment; do not paste the URL to external users.
        """
        # Catch HTTPException from _reuse_or_spawn so the model sees a
        # structured diagnostic tool response rather than an MCP-level
        # error. Two specific failures we care about:
        #   400 with detail.error == "static lint failed"
        #       → SyntaxError caught before spawn. detail.errors is a
        #         list of {path,line,offset,message,text}.
        #   504 with detail.error == "sandbox did not become ready"
        #       → container spawned but didn't bind port 80.
        #         detail.logs is the tail of container stdout/stderr.
        # Everything else falls through as-is (500s, 400s from schema,
        # etc.) since those are already meaningful.
        try:
            result = await run_callable(
                runtime=runtime,
                files=files,
                entrypoint=entrypoint,
                ttl_seconds=ttl_seconds,
                session_id=session_id,
                deletes=deletes,
            )
        except HTTPException as exc:
            detail = exc.detail
            if isinstance(detail, dict) and detail.get("error"):
                log.info(
                    "preview_app: converting HTTPException(status=%d, error=%r) "
                    "to diagnostic tool response",
                    exc.status_code, detail.get("error"),
                )
                return _format_diagnostic(detail)
            raise
        url = result["url"]
        sandbox_id = result["sandbox_id"]
        session_id_out = result["session_id"]
        expires_at = result["expires_at"]
        reused = result.get("reused", False)
        iframe = render_preview_html(url, sandbox_id, session_id_out)

        # Session id lives on its own line in a stable "Session id: …"
        # format so the model can regex it out on the next turn without
        # depending on the tool-response object being intact. Order of
        # lines is load-bearing: the caller-visible "Preview ready"
        # summary first, then the machine-readable session id, then a
        # one-line reminder of the delta-update rule, then the fenced
        # html block. The reminder rides on every turn (not just the
        # first) because models forget between calls — cheap tokens
        # spent here save far more tokens in resent unchanged files.
        verb = "updated" if reused else "ready"
        log.info(
            "MCP tool call: preview_app OK — session=%s sandbox=%s reused=%s "
            "n_files=%d n_deletes=%d",
            session_id_out, sandbox_id, reused,
            len(files or {}), len(deletes or []),
        )
        n_files = len(files) if files else 0
        if reused:
            hint = (
                f"You updated {n_files} file(s) in session `{session_id_out}` "
                "— everything else in /app was preserved. To change more, "
                f"call preview_app again with session_id=\"{session_id_out}\" "
                "and ONLY the file(s) that changed."
            )
        else:
            hint = (
                f"To EDIT this preview on the next turn, call preview_app "
                f"again with session_id=\"{session_id_out}\" and ONLY the "
                "file(s) you changed — do NOT re-send unchanged files. "
                "The container keeps running and hot-reloads on file change."
            )
        # Download URL is derived from the preview URL — the Caddy route
        # at /sandboxes/download/{session_id} reverse-proxies to the
        # runner's session-download endpoint. Uses session_id (not
        # sandbox_id) so the same URL keeps working across self-heal
        # spawns. Shape: given SANDBOX_PROXY_URL=https://host/sandboxes,
        # download is https://host/sandboxes/download/{session_id}.
        proxy_base = url.rsplit("/", 2)[0]
        download_url = f"{proxy_base}/download/{session_id_out}"
        # Proactively inline the container's stdout tail so the model
        # sees startup diagnostics WITHOUT a follow-up tool call. The
        # runner reads /tmp/sandbox.log after readiness passes; the
        # helper here decides how to render it: skip for static (nginx
        # access logs aren't useful signal), a streamlit-specific note
        # (streamlit hides user tracebacks from stdout, so a "clean"
        # log doesn't mean much for that runtime), otherwise either
        # flag as suspicious or acknowledge it's clean.
        startup_section = _format_startup_output(
            result.get("runtime"), result.get("startup_output") or ""
        )
        return (
            f"Preview {verb}. Sandbox `{sandbox_id}` at {url} "
            f"(expires {expires_at}).\n"
            f"Session id: {session_id_out}\n"
            f"Download source: {download_url}\n"
            f"{hint}\n"
            f"{startup_section}\n\n"
            "```html\n"
            f"{iframe}\n"
            "```"
        )

    @mcp.tool()
    async def get_sandbox_logs(
        session_id: str = Field(
            description=(
                "The session_id from a previous preview_app response. "
                "Must match ^[A-Za-z0-9_-]{1,64}$."
            )
        ),
        lines: int = Field(
            default=100,
            description=(
                "How many trailing log lines to return. Clamped 1..1000. "
                "Default 100 is enough for most Python/JS tracebacks."
            ),
        ),
    ) -> str:
        """Fetch the last N lines of the running sandbox's combined
        stdout+stderr for the given session_id.

        # WHEN TO CALL

        The user reports the running app looks broken (a rendered error
        card, "undefined is not a function", the button does nothing,
        etc.) but ``preview_app`` returned a normal ready response.
        Flask, FastAPI, Express, Vite, and Next dev servers all print
        the offending Python traceback / JS stack / import error to
        stdout before rendering the error in the browser — this
        endpoint surfaces that traceback.

        Do NOT call this after a ``preview_app`` FAILURE — those
        already include the container logs in the tool response.

        # STREAMLIT IS THE MAJOR EXCEPTION — DO NOT MISDIAGNOSE

        Streamlit **catches every user exception and renders it in the
        browser only**. It does not write user tracebacks to stdout.
        Calling this endpoint on a broken Streamlit app will return an
        almost-empty log ("You can now view your Streamlit app…" and
        nothing else). That's NOT evidence the app is healthy — it is
        evidence Streamlit swallowed the error. If the user reports a
        Streamlit app is broken:

        1. DO NOT tell the user "there are no errors in the logs".
        2. DO ask the user to paste the error text the browser is
           showing them (or a screenshot). Streamlit renders a red
           box with the Python traceback in it.
        3. Alternatively, wrap the risky code in
           ``try: … except Exception as e: st.exception(e); raise``
           and reissue ``preview_app`` — that pushes the traceback
           both to the browser AND to stdout so this endpoint sees it.

        For every non-Streamlit Python runtime (Flask, FastAPI,
        Gradio, bare ``python app.py``) tracebacks land here. Same for
        every Node runtime.

        # RETURN VALUE

        Returns a formatted string with the log tail. Returns an
        explicit "no log output yet" message if the file is empty —
        possible causes: container just spawned and hasn't printed
        anything yet, the app writes to a file instead of stdout, OR
        the app is Streamlit (see above)."""
        log.info(
            "MCP tool call: get_sandbox_logs session=%s lines=%s",
            session_id, lines,
        )
        try:
            data = await logs_callable(session_id=session_id, lines=lines)
        except HTTPException as exc:
            detail = exc.detail
            log.warning(
                "get_sandbox_logs: HTTPException %d for session=%s: %s",
                exc.status_code, session_id, detail,
            )
            if isinstance(detail, str):
                return f"logs unavailable: {detail}"
            return f"logs unavailable: {json.dumps(detail, default=str)}"
        text = (data.get("logs") or "").rstrip()
        if not text:
            log.debug("get_sandbox_logs: no output for session=%s", session_id)
            return (
                f"No log output yet for session `{session_id}`. The "
                "container may have just spawned, or the app writes to "
                "a file instead of stdout."
            )
        log.debug(
            "get_sandbox_logs: session=%s returned %d bytes",
            session_id, len(text),
        )
        return (
            f"Container logs for session `{session_id}` "
            f"(sandbox `{data.get('sandbox_id')}`, last {data.get('lines_requested')} lines):\n"
            "---\n"
            f"{text}\n"
            "---"
        )

    return mcp
