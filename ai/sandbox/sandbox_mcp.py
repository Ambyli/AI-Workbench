"""FastMCP wrapper exposing the ``preview_app`` tool.

Registered on the same FastAPI app under ``/mcp`` so LiteLLM (see
``ai/litellm/litellm_config.yaml`` — ``mcp_servers.sandbox``) can advertise
the tool to any Qwen/Claude/GPT model that supports tool calling.

## What the tool returns

Following the OpenWebUI docs (Extensibility → Plugin Development → Rich
UI), the tool result is a **string** containing a short summary plus a
fenced ``html`` code block with an ``<iframe>`` pointing at the sandbox
URL. When the model relays the tool result to the user, OpenWebUI's
markdown renderer picks up the ``html`` block and turns it into a
sandboxed iframe artifact — no additional model prompting or plugin
config required.

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

from typing import Optional

from fastmcp import FastMCP
from pydantic import Field

from runtimes import describe_runtimes


# Responsive iframe height. Cannot use the OpenWebUI-recommended postMessage
# height-reporter here because the reporter would need to live *inside* the
# sandbox's HTML (Streamlit, Vite, etc.) — code we don't control. Instead
# scale to the viewport with a hard ceiling so a tall preview doesn't push
# the chat context off-screen.
_IFRAME_HEIGHT_CSS = "min(85vh, 900px)"


def _render_html_block(url: str) -> str:
    """Return an OpenWebUI-friendly HTML iframe block for the given URL."""
    return (
        f'<iframe src="{url}" '
        f'style="width:100%;height:{_IFRAME_HEIGHT_CSS};border:0;'
        f'border-radius:8px;background:#0e1116" '
        f'allow="clipboard-read; clipboard-write" '
        f'loading="lazy"></iframe>'
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
            description=(
                "Map of relative path → file contents. Every file the app "
                "needs must be included. For Python, add requirements.txt "
                "if you need packages beyond the pre-baked set. For Node, "
                "add package.json. list_runtimes returns an example "
                "files map for each runtime."
            )
        ),
        entrypoint: Optional[str] = Field(
            default=None,
            description=(
                "Shell command run inside the sandbox. Must bind to port "
                "80. Leave unset to use the runtime's default (streamlit "
                "for python, serve for node, nginx for static). NOTE: "
                "the 'static' runtime does NOT accept a custom "
                "entrypoint — nginx is fixed. Setting one for static "
                "will return a 400 error."
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
    ) -> str:
        """Build a live, interactive preview of a small app and return an
        HTML iframe block that renders it inline in the chat.

        **When you (the model) relay this tool result to the user, include
        the returned string VERBATIM in your response** — the ```html
        code block is what makes OpenWebUI render the preview as an
        embedded iframe artifact. Paraphrasing or removing the block
        prevents the preview from rendering.

        The iframe URL is served by ``sandbox-proxy`` on the local
        network. It is not reachable from outside this OpenWebUI
        deployment; do not paste the URL to external users.
        """
        result = await run_callable(
            runtime=runtime,
            files=files,
            entrypoint=entrypoint,
            ttl_seconds=ttl_seconds,
        )
        url = result["url"]
        sandbox_id = result["sandbox_id"]
        expires_at = result["expires_at"]
        iframe = _render_html_block(url)

        # Wrap in a fenced ```html block so OpenWebUI's markdown renderer
        # promotes it into an artifact iframe. The plain-text lines above
        # give the model + user useful context if the html renderer is
        # unavailable or the model decides to paraphrase.
        return (
            f"Preview ready. Sandbox `{sandbox_id}` at {url} "
            f"(expires {expires_at}).\n\n"
            "```html\n"
            f"{iframe}\n"
            "```"
        )

    return mcp
