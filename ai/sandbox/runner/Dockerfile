# Build context is the REPO ROOT (see ai/sandbox/docker-compose.sandbox.yml:
#   context: ../..) so we can copy the shared workspace package alongside the
# service.

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_NO_CACHE=1 \
    PYTHONPATH=/app/ai/sandbox/runner

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Workspace bootstrap: root pyproject declares the members; shared/common
# lands first so the service's `common` dep resolves via the workspace.
# uv.lock ships with the build so the container gets the same version
# resolution as the host — without it, uv resolves fresh at build time
# and transitive extras (e.g. fastmcp-slim[server]) can silently get
# dropped, surfacing at runtime as "FastMCP server support is not
# installed" ImportError.
COPY pyproject.toml uv.lock .
COPY shared/common ./shared/common
COPY ai/sandbox/runner/pyproject.toml ./ai/sandbox/runner/

# --frozen refuses to touch the lockfile: build fails loudly if the
# pyproject drifted from the lock instead of silently re-resolving.
RUN uv sync --package sandbox --no-dev --frozen

# Service code last so a code-only change doesn't invalidate the (slow)
# sync layer.
COPY ai/sandbox/runner/app.py \
     ai/sandbox/runner/spawner.py \
     ai/sandbox/runner/runtimes.py \
     ai/sandbox/runner/reaper.py \
     ai/sandbox/runner/sandbox_mcp.py \
     ./ai/sandbox/runner/

WORKDIR /app/ai/sandbox/runner
EXPOSE 8000

CMD ["uv", "run", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
