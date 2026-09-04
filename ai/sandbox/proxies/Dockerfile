# sandbox-proxy — custom Caddy build with the replace-response plugin.
#
# Why a custom build: stock caddy:2-alpine cannot rewrite response bodies.
# We inject a `<script src="/__sandbox_shim__.js">` tag into every HTML
# page served through the proxy so the browser reports console.error,
# console.warn, window.onerror, and unhandled promise rejections back
# to sandbox-runner. Without the plugin, sandbox apps' UI-side failures
# stay in the user's DevTools and the agent never sees them.
#
# Plugin: https://github.com/caddyserver/replace-response
#   Module path: github.com/caddyserver/replace-response
#   Caddyfile directive: `replace` (streaming supported)
#
# Rebuild the image after any Caddyfile / shim / plugin change:
#   docker compose -f ai/sandbox/docker-compose.sandbox.yml build sandbox-proxy

# ── Builder stage ─────────────────────────────────────────────────────────
FROM caddy:2-builder-alpine AS builder

# xcaddy pulls the plugin, resolves versions, and produces a static caddy
# binary. --with without a version pin tracks the module's default
# branch — acceptable because we rebuild explicitly and the plugin is
# maintained by the caddyserver org itself. Pin to a tag here if you
# need reproducible builds across image rebuilds.
RUN xcaddy build \
    --with github.com/caddyserver/replace-response

# ── Runtime stage ─────────────────────────────────────────────────────────
FROM caddy:2-alpine

# Replace the stock caddy binary with the plugin-enabled one.
COPY --from=builder /usr/bin/caddy /usr/bin/caddy

# Bake the shim into the image so Caddy can serve it as a static asset
# under /__sandbox_shim__.js — no compose bind mount required. Build
# context is the repo root (see docker-compose.sandbox.yml).
COPY ai/sandbox/proxies/browser_shim.js /srv/shim/browser_shim.js

# Compose still bind-mounts the Caddyfile so operators can iterate on
# routing without rebuilding.
