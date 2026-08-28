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
    treat them as the *same* artifact — panel wouldn't re-open."""
    safe_url_attr = html.escape(url, quote=True)
    safe_sandbox = html.escape(sandbox_id)
    safe_session = html.escape(session_id)
    js_url = json.dumps(url)
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
                "Map of relative path → file contents. On the FIRST call "
                "for a preview, include every file the app needs "
                "(requirements.txt / package.json if extra packages are "
                "required). On a FOLLOW-UP call with the same "
                "session_id, include ONLY the file(s) that changed — the "
                "rest are preserved in the running container. Save "
                "tokens by not resending unchanged files."
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
        """Build a live, interactive preview of a small app and return a
        fenced ```html block containing a self-contained webpage that
        OpenWebUI renders in its artifacts split-panel.

        **Updating an existing preview (this is the common case):** when
        the user asks you to change something in a preview you already
        showed them, call ``preview_app`` again with the SAME
        ``session_id`` from the previous response and ONLY the file(s)
        that changed. The dev server inside the sandbox hot-reloads on
        file changes (Streamlit watches mtimes, Vite has HMR, nginx
        serves live), so the iframe already in the chat updates
        automatically. Do NOT omit the ``session_id`` on follow-ups — a
        new session means a new URL, and the user loses their scroll
        position and any in-app state.

        **When you (the model) relay this tool result to the user,
        include the returned string VERBATIM in your response** — the
        ```html code block is what makes OpenWebUI promote the preview
        into its artifacts split-panel (see the OpenWebUI Artifacts
        docs). Paraphrasing or removing the block prevents the preview
        from rendering. The "Session id:" line above the block is what
        you (the model) grep back on the next turn — do not remove or
        reword it.

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
        # summary first, then the machine-readable session id, then the
        # fenced html block.
        verb = "updated" if reused else "ready"
        return (
            f"Preview {verb}. Sandbox `{sandbox_id}` at {url} "
            f"(expires {expires_at}).\n"
            f"Session id: {session_id_out}\n\n"
            "```html\n"
            f"{iframe}\n"
            "```"
        )

    return mcp
