# Base image for runtime="node" sandboxes — Node 20 slim with common
# scaffolding tools pre-baked so `npm run dev` doesn't need to fetch the
# framework every time.
#
# Built by:   docker compose -f ai/sandbox/docker-compose.sandbox.yml --profile build build
# Tag:        sandbox-node:latest  (referenced from runtimes.py)

FROM node:20-slim

# Package set pre-baked into a global node_modules. Keep in sync with
# ai/sandbox/SANDBOX.md § "Pre-baked Node packages".
#
# Reasoning:
#   * serve                — quick static server, the default entrypoint.
#   * vite, react, react-dom — the two most-requested "make me a UI" combo.
#   * express              — server-side demos.
#   * next                 — model likes to reach for this; big cold-start
#                            cost if not pre-baked.
#
# Global install so `npx --yes X` finds them without pulling from the
# network. Local (per-app) node_modules can still be added by shipping
# package.json — the entrypoint handles that below.
RUN npm install -g \
    serve@14.2.3 \
    vite@5.4.8 \
    react@18.3.1 \
    react-dom@18.3.1 \
    express@4.21.0 \
    next@14.2.13

# Entrypoint wrapper — if the caller included package.json, install its
# dependencies before running their command.
COPY <<'ENTRY' /usr/local/bin/sandbox-entrypoint.sh
#!/bin/sh
set -e
cd /app
if [ -f package.json ]; then
    echo "[sandbox] running npm install via HTTP_PROXY=${HTTP_PROXY}"
    # --no-audit --no-fund keeps output small; --prefer-offline uses the
    # package cache the entrypoint reuses across sandbox restarts (tmpfs
    # per-container though, so this only matters mid-run).
    npm install --no-audit --no-fund --prefer-offline || {
        echo "[sandbox] npm install failed — is the package on the allowlist?" >&2
        exit 1
    }
fi
exec "$@"
ENTRY

RUN chmod +x /usr/local/bin/sandbox-entrypoint.sh

# node:20-slim already ships a `node` user at uid 1000, so `useradd
# --uid 1000` fails "UID is not unique". We only need uid 1000 to
# exist and /home/sandbox to be writable by it — the name is irrelevant
# since the runner's `USER 1000:1000` references the uid, not the login
# name. Skip creation, just prepare the home dir.
RUN mkdir -p /home/sandbox && \
    chown 1000:1000 /home/sandbox

USER 1000:1000
ENV HOME=/home/sandbox \
    NPM_CONFIG_CACHE=/home/sandbox/.npm \
    PATH=/home/sandbox/.local/bin:/usr/local/bin:/usr/bin:/bin

WORKDIR /app

ENTRYPOINT ["/usr/local/bin/sandbox-entrypoint.sh"]
EXPOSE 80
