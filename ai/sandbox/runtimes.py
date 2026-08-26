"""Runtime registry for the sandbox subsystem.

Each runtime maps to a base container image plus how to launch the user's
files inside it. All base images run their app on port 80 so sandbox-proxy
can statically route ``/{sandbox_id}/*`` to ``sandbox-{id}:80`` without
per-sandbox Caddy config.

Adding a new language is a single entry here plus a new Dockerfile in
``ai/Dockerfile.sandbox-<name>``. The runner enforces that ``runtime``
values from ``POST /run`` and the MCP ``preview_app`` tool are keys in
this dict — nothing else is spawnable.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Runtime:
    """One runtime the sandbox subsystem knows how to spawn.

    Attributes:
        image: Docker image tag. Built locally by ``docker compose build``
            from ``ai/Dockerfile.sandbox-<name>`` — no external registry
            pull, so a compromised upstream can't reach the sandbox subsystem.
        default_entrypoint: Command run inside the container if the caller
            didn't supply one. Must bind to port 80.
        internal_port: Always 80 in this codebase — see module docstring.
            Kept as an attribute so a future runtime that needs a different
            port doesn't require rewriting the routing layer.
        readiness_probe_path: HTTP path the runner GETs after spawn to
            decide the sandbox is ready to serve traffic. ``/`` works for
            most frameworks; static uses ``/index.html``.
        default_files: Files written into every new sandbox even if the
            caller didn't provide them. Useful for a runtime-specific
            wrapper script the entrypoint invokes.
    """

    image: str
    default_entrypoint: str
    internal_port: int = 80
    readiness_probe_path: str = "/"
    default_files: dict[str, str] | None = None


RUNTIMES: dict[str, Runtime] = {
    # nginx serves whatever HTML/CSS/JS/asset files the caller writes into
    # /app. No install step, no runtime — pure static hosting. The fastest
    # path when a model just needs to show an HTML page.
    "static": Runtime(
        image="sandbox-static:latest",
        default_entrypoint="nginx -g 'daemon off;'",
        readiness_probe_path="/",
    ),
    # Python 3.11-slim with the common web-app frameworks pre-installed.
    # Entrypoint script (baked into the image) runs ``pip install -r
    # requirements.txt`` if that file is present, then execs the caller's
    # command. ``pip install`` egresses via HTTP_PROXY=sandbox-egress.
    "python": Runtime(
        image="sandbox-python:latest",
        default_entrypoint=(
            "streamlit run app.py "
            "--server.port 80 --server.address 0.0.0.0 "
            "--server.headless true"
        ),
        readiness_probe_path="/",
    ),
    # Node 20-slim with vite/react/next/express pre-installed globally.
    # Same install-if-present entrypoint pattern for package.json.
    "node": Runtime(
        image="sandbox-node:latest",
        default_entrypoint="npx --yes serve -l 80 .",
        readiness_probe_path="/",
    ),
}


def get_runtime(name: str) -> Runtime:
    """Return a runtime by name, raising ``KeyError`` on unknown values.

    The runner catches this and returns HTTP 400 to the caller so a
    misspelled runtime is a clear user-facing error, not a 500.
    """
    return RUNTIMES[name]
