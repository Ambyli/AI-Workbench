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
import secrets
from typing import Optional

from fastmcp import FastMCP
from pydantic import Field

from runtimes import describe_runtimes


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


def build_mcp(run_callable) -> FastMCP:
    """Build the FastMCP instance wired to the runner's spawn code path.

    ``run_callable`` is an async callable ``(RunRequest) → RunResponse``
    provided by ``app.py`` — kept as a parameter rather than an import
    to avoid a circular dependency between ``sandbox_mcp.py`` and ``app.py``.
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
        result = await run_callable(
            runtime=runtime,
            files=files,
            entrypoint=entrypoint,
            ttl_seconds=ttl_seconds,
            session_id=session_id,
            deletes=deletes,
        )
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
        return (
            f"Preview {verb}. Sandbox `{sandbox_id}` at {url} "
            f"(expires {expires_at}).\n"
            f"Session id: {session_id_out}\n"
            f"Download source: {download_url}\n"
            f"{hint}\n\n"
            "```html\n"
            f"{iframe}\n"
            "```"
        )

    return mcp
