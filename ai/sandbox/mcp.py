"""FastMCP wrapper exposing the ``preview_app`` tool.

Registered on the same FastAPI app under ``/mcp`` so LiteLLM (see
``ai/litellm_config.yaml`` — ``mcp_servers.sandbox``) can advertise the
tool to any Qwen/Claude/GPT model that supports tool calling. The model
returns the tool result URL wrapped in a fenced ``html`` iframe block,
which OpenWebUI renders as an artifact.

This module intentionally keeps the tool signature narrow — the shape
you'd sketch on a napkin — so a small model can pattern-match it:

    preview_app(runtime, files, entrypoint?, ttl_seconds?)
      → { url, sandbox_id, expires_at }

Any richer options belong on ``POST /run`` (which operators call from
curl / scripts) and are not exposed over MCP.
"""

from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP
from pydantic import BaseModel, Field


class PreviewAppResult(BaseModel):
    url: str
    sandbox_id: str
    expires_at: str


def build_mcp(run_callable) -> FastMCP:
    """Build the FastMCP instance wired to the runner's spawn code path.

    ``run_callable`` is an async callable ``(RunRequest) → RunResponse``
    provided by ``app.py`` — kept as a parameter rather than an import
    to avoid a circular dependency between ``mcp.py`` and ``app.py``.
    """
    mcp = FastMCP(name="sandbox")

    @mcp.tool()
    async def preview_app(
        runtime: str = Field(
            description=(
                "One of: 'static' (HTML/CSS/JS), 'python' (Streamlit/Gradio/"
                "Flask/FastAPI), or 'node' (Vite/React/Next/Express). Pick "
                "the one that fits the framework the user asked for."
            )
        ),
        files: dict[str, str] = Field(
            description=(
                "Map of relative path → file contents. Every file the app "
                "needs must be included. For Python, add requirements.txt "
                "if you need packages beyond the pre-baked set. For Node, "
                "add package.json."
            )
        ),
        entrypoint: Optional[str] = Field(
            default=None,
            description=(
                "Shell command run inside the sandbox. Must bind to port "
                "80. Leave unset to use the runtime's default (streamlit "
                "for python, serve for node, nginx for static)."
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
    ) -> PreviewAppResult:
        """Build a live, interactive preview of a small app.

        Returns a URL the user can iframe in the chat. The URL is served
        by sandbox-proxy on the local network — do not paste it to
        external users; it only works from this OpenWebUI instance.
        """
        result = await run_callable(
            runtime=runtime,
            files=files,
            entrypoint=entrypoint,
            ttl_seconds=ttl_seconds,
        )
        return PreviewAppResult(**result)

    return mcp
