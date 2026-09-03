"""Runtime registry for the sandbox subsystem.

Each runtime maps to a base container image plus how to launch the user's
files inside it. All base images run their app on port 80 so sandbox-proxy
can statically route ``/{sandbox_id}/*`` to ``sandbox-{id}:80`` without
per-sandbox Caddy config.

Adding a new language is a single entry here plus a new Dockerfile in
``ai/sandbox/images/<name>.Dockerfile``. The runner enforces that ``runtime``
values from ``POST /run`` and the ``run`` / ``create`` / ``update_files``
MCP tools are keys in this dict — nothing else is spawnable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field


log = logging.getLogger("sandbox-runner.runtimes")


# Placeholder body served by every runtime while the model is still writing
# code. The runtime's ``warming_files`` overrides the specifics (title text,
# language shim) but the visual identity is intentionally shared so an
# operator opening the iframe knows immediately what state the sandbox is in.
_WARMING_HTML_BODY = (
    "<!doctype html><html><head><meta charset=\"utf-8\"><title>"
    "sandbox warming</title></head><body style=\""
    "margin:0;font-family:system-ui;padding:1.5rem;background:#0e1116;"
    "color:#e6edf3\"><h1 style=\"margin:0 0 0.5rem\">sandbox warming</h1>"
    "<p style=\"margin:0;opacity:0.8\">the model is preparing your app. "
    "this placeholder is replaced as soon as <code>update_files</code> "
    "runs.</p></body></html>"
)


@dataclass(frozen=True)
class Runtime:
    """One runtime the sandbox subsystem knows how to spawn.

    Attributes:
        image: Docker image tag. Built locally by ``docker compose build``
            from ``ai/sandbox/images/<name>.Dockerfile`` — no external registry
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
        warming_files: Placeholder files written into the container when
            ``create`` is called without user files. Once the model calls
            ``update_files`` on the same session, these are overlaid /
            deleted by the real code. Kept minimal so cold start under
            ``create`` is nearly indistinguishable from the empty case.
        summary: One-line human/model-readable description shown by the
            ``get_runtime_types`` tool. Used by models to decide which
            runtime fits the user's request.
        prebaked_packages: Packages already installed in the base image;
            callers don't need to include them in requirements.txt or
            package.json. Only tracked for documentation — kept in sync
            manually with the corresponding Dockerfile.
        allows_custom_entrypoint: If False, the runner rejects any
            caller-supplied ``entrypoint`` for this runtime. Used by
            ``static`` where nginx is the fixed process and models
            confusingly try to pass something like
            ``python3 -m http.server`` (which the image doesn't have).
        example_files: A minimal working ``files`` map the ``get_runtime_types``
            tool returns so a model can pattern-match a valid request.
    """

    image: str
    default_entrypoint: str
    summary: str
    prebaked_packages: tuple[str, ...] = ()
    allows_custom_entrypoint: bool = True
    example_files: dict[str, str] | None = None
    internal_port: int = 80
    readiness_probe_path: str = "/"
    default_files: dict[str, str] | None = None
    warming_files: dict[str, str] = field(default_factory=dict)


RUNTIMES: dict[str, Runtime] = {
    # nginx serves whatever HTML/CSS/JS/asset files the caller writes into
    # /app. No install step, no runtime — pure static hosting. The fastest
    # path when a model just needs to show an HTML page.
    "static": Runtime(
        image="sandbox-static:latest",
        default_entrypoint="nginx -g 'daemon off;'",
        summary=(
            "Static HTML/CSS/JS. nginx serves whatever is in the files "
            "map. No runtime, no install step, ~500ms cold start. Use "
            "this for single-page demos, hand-written calculators, "
            "React-via-esm.sh, anything a browser can render on its "
            "own. Hot reload: file overwrite is served on the next "
            "request — the user just refreshes the iframe."
        ),
        # nginx is fixed — this runtime has no way to run arbitrary
        # entrypoints, and setting one causes a hard-to-diagnose
        # readiness-timeout failure.
        allows_custom_entrypoint=False,
        example_files={
            "index.html": (
                "<!doctype html><html><body>"
                "<h1>hello sandbox</h1>"
                "</body></html>"
            ),
        },
        # nginx needs at least one file at /app/index.html to satisfy the
        # readiness probe. The warming file gets overwritten by the model's
        # own index.html on the first update_files call.
        warming_files={"index.html": _WARMING_HTML_BODY},
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
        summary=(
            "Python 3.11. Default entrypoint runs Streamlit against "
            "app.py; override entrypoint to run gradio, flask, "
            "fastapi, or any other server (must bind port 80). Ship "
            "requirements.txt for extra packages — pip install runs "
            "on boot via the egress allowlist. Hot reload: Streamlit "
            "watches mtimes and auto-reruns on file change — no "
            "browser refresh needed. Flask/FastAPI/Gradio only hot-"
            "reload when their `--reload` / `debug=True` flag is set; "
            "pick that entrypoint if you want live edits."
        ),
        prebaked_packages=(
            "streamlit", "gradio", "flask", "fastapi", "uvicorn",
            "pandas", "numpy", "matplotlib", "plotly", "requests", "pillow",
        ),
        example_files={
            "app.py": (
                "import streamlit as st\n"
                "st.title('hello')\n"
                "st.slider('n', 0, 100)\n"
            ),
        },
        # Streamlit needs an app.py that imports cleanly and renders SOMETHING
        # so the readiness probe passes. Minimal single-liner keeps warm-time
        # under a second.
        warming_files={
            "app.py": (
                "import streamlit as st\n"
                "st.set_page_config(page_title='warming')\n"
                "st.markdown('### sandbox warming')\n"
                "st.markdown('the model is preparing your app.')\n"
            ),
        },
    ),
    # Node 20-slim with vite/react/next/express pre-installed globally.
    # Same install-if-present entrypoint pattern for package.json.
    "node": Runtime(
        image="sandbox-node:latest",
        default_entrypoint="npx --yes serve -l 80 .",
        summary=(
            "Node 20. Default entrypoint serves the files map as a "
            "static site (`npx serve`); override entrypoint to run "
            "vite, next, express, or any custom server (must bind "
            "port 80). Ship package.json for extra packages — npm "
            "install runs on boot via the egress allowlist. Hot "
            "reload: `npx serve` reads from disk on every request so "
            "a browser refresh shows updates — no HMR. Override to "
            "`npm run dev` (vite / next) for full HMR without refresh."
        ),
        prebaked_packages=(
            "serve", "vite", "react", "react-dom", "express", "next",
        ),
        example_files={
            "server.js": (
                "const express = require('express');\n"
                "const app = express();\n"
                "app.get('/', (_, res) => res.send('<h1>hello</h1>'));\n"
                "app.listen(80, '0.0.0.0');\n"
            ),
        },
        # `npx serve -l 80 .` needs an index.html at the root to serve; a
        # missing file causes the readiness probe to see a 404 which
        # counts as ready (2xx-4xx) but confuses the user opening the URL.
        warming_files={"index.html": _WARMING_HTML_BODY},
    ),
}


def describe_runtimes() -> list[dict]:
    """Return every runtime as a plain-JSON list.

    Consumed by both the ``get_runtime_types`` MCP tool and its REST
    counterpart. Kept as a plain function (not a method) so both call
    sites can import it without pulling in ``FastMCP`` state.
    """
    log.debug("describe_runtimes: %d entries", len(RUNTIMES))
    return [
        {
            "name": name,
            "summary": rt.summary,
            "default_entrypoint": rt.default_entrypoint,
            "allows_custom_entrypoint": rt.allows_custom_entrypoint,
            "prebaked_packages": list(rt.prebaked_packages),
            "example_files": rt.example_files or {},
        }
        for name, rt in RUNTIMES.items()
    ]


def get_runtime(name: str) -> Runtime:
    """Return a runtime by name, raising ``KeyError`` on unknown values.

    The runner catches this and returns HTTP 400 to the caller so a
    misspelled runtime is a clear user-facing error, not a 500.
    """
    rt = RUNTIMES[name]
    log.debug("get_runtime(%s) → image=%s", name, rt.image)
    return rt
