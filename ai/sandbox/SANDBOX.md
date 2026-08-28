# Sandbox Subsystem

Runs model-generated web apps in short-lived, network-segmented containers, and exposes them to Open WebUI as iframe artifacts. Think Claude.ai Artifacts, but for **anything** — Streamlit, Gradio, Flask, FastAPI, Vite+React, Next, Express, static HTML.

## When to reach for this

- The user asks for an **interactive** demo (a slider that redraws a chart, a form that hits a server, a small CRUD toy) — anything that a single HTML block cannot express.
- The model outputs code and you want to see it run *before* the user manually copies it into their editor.
- You need to run untrusted code (pasted from a bug report, an issue thread, or a webpage the model was asked to summarize) somewhere it cannot reach the rest of the stack.

If a fenced ` ```html ` block would do the job, prefer that — Open WebUI renders it in an iframe with zero infra. The sandbox is for cases where you need a real runtime.

## Security model (read this first)

The single security control that matters is **network segmentation**. Everything else is defense in depth.

Four Docker networks:

| Network | Type | Members |
|---|---|---|
| `ai_shared` | external bridge | openwebui, litellm, `sandbox-runner`, `sandbox-proxy` |
| `sandbox_net` | bridge, `internal: true` | `sandbox-runner`, `sandbox-proxy`, `sandbox-egress`, every `sandbox-{id}` |
| `sandbox_state` | bridge, `internal: true` | `sandbox-runner`, `sandbox-db` |
| `sandbox_egress_out` | bridge (NOT `internal`) | `sandbox-egress` **only** |

`internal: true` means Docker **does not attach a default gateway**. A container on `sandbox_net` or `sandbox_state` that tries to hit the host, the internet, or any container on `ai_shared` has no route — the packets go nowhere.

`sandbox_egress_out` is the exception: it is *not* internal, so it does have a default gateway and can reach the public internet. It exists solely to give `sandbox-egress` its outbound leg after the allowlist filter runs. No sandbox, no state store, and nothing else in the stack attaches to it. Sandboxes reach the internet ONLY by first hitting `sandbox-egress` on `sandbox_net`, passing the filter, and then being forwarded out via `sandbox_egress_out`.

Consequences:

- Sandboxed code cannot reach `litellm`, `phoenix-mcp`, `roofix-db`, `interceptor`, `openwebui`, or any other stack service by name.
- Sandboxed code cannot reach `sandbox-db` — the job store lives on `sandbox_state`, which sandboxes don't touch. Only `sandbox-runner` bridges to `sandbox_state`.
- Sandboxed code cannot reach the internet directly — `sandbox_net` has no default gateway, so packets have nowhere to go except `sandbox-egress:8888`, which enforces a domain allowlist (`pypi.org`, `files.pythonhosted.org`, `registry.npmjs.org`, `esm.sh`, `cdn.jsdelivr.net`). Anything else is refused with 403 Filtered.

Per-container hardening (all applied by `spawner.py`):

- `--cap-drop ALL` — no Linux capabilities.
- `--read-only` rootfs — writes only permitted to `/tmp`, `/app`, and `/home/sandbox` tmpfs mounts.
- `--memory 512m --cpus 1 --pids-limit 256` — resource caps.
- `--user 1000:1000` — non-root.
- No host bind-mounts. Caller files are injected via the Docker SDK's `put_archive` into `/app` (in-memory tarball → tmpfs).
- No `docker.sock` access. Only `sandbox-runner` has this.

## Security-invariant checklist

Every material change to `spawner.py`, `docker-compose.sandbox.yml`, or the base Dockerfiles MUST be validated against this list before merging. Run from inside a live sandbox: `docker exec -it sandbox-<id> sh`.

```
curl -v --max-time 3 http://litellm:4000/           → must fail (no route)
curl -v --max-time 3 http://phoenix-mcp/            → must fail
curl -v --max-time 3 http://roofix-db:5432/         → must fail
curl -v --max-time 3 http://interceptor:8080/       → must fail
curl -v --max-time 3 http://openwebui:8080/         → must fail
curl -v --max-time 3 http://sandbox-db:5432/        → must fail   ← justifies sandbox_state
curl -v --max-time 3 https://example.com/           → must fail   ← not on egress allowlist
curl -v --max-time 3 https://pypi.org/              → must succeed via egress
cat /proc/1/status | grep CapEff                    → CapEff: 0000000000000000
mount | grep " / "                                  → shows "ro"
docker inspect sandbox-<id> --format '{{.NetworkSettings.Networks}}'
                                                    → contains ONLY sandbox_net
```

If any of the "must fail" checks succeeds, revert the change and investigate before shipping.

## Architecture

```
       ai_shared (external)                sandbox_state (internal)
 ┌────────────────────────────┐        ┌────────────────────────────┐
 │  openwebui ─┐              │        │      sandbox-db            │
 │  litellm ───┼─► sandbox-runner ─────┼──── (postgres)             │
 │             │        │              │                            │
 │       sandbox-proxy  │ docker.sock  │                            │
 │             │        │              │                            │
 └─────────────┼────────┼──────────────┘                            │
               │        │                                           │
               ▼        ▼                                           │
       ┌──────────────────────────────────────────────┐             │
       │            sandbox_net (internal)            │             │
       │                                              │             │
       │    sandbox-{id-1}  sandbox-{id-2}  ...       │             │
       │           │             │                    │             │
       │           ▼             ▼                    │             │
       │      sandbox-egress  (filter here)           │             │
       │           │                                  │             │
       └───────────┼──────────────────────────────────┘             │
                   │                                                │
                   ▼                                                │
       ┌──────────────────────────────────────────────┐             │
       │  sandbox_egress_out (bridge, NOT internal)   │             │
       │                                              │             │
       │       sandbox-egress ──► internet            │             │
       │                          (allowlisted)       │             │
       └──────────────────────────────────────────────┘             │
                                                                    │
                                                                    ▘
```

- **`sandbox-runner`** — FastAPI + MCP + `docker.from_env()`. On `ai_shared`, `sandbox_net`, and `sandbox_state`. Spawns and reaps `sandbox-{id}` containers. Writes job state to `sandbox-db`. See [`ai/sandbox/runner/app.py`](runner/app.py).
- **`sandbox-proxy`** — Caddy. Routes `/{sandbox_id}/*` → `sandbox-{id}:80`. Strips `X-Frame-Options` and rewrites CSP so Open WebUI can iframe the result. See [`ai/sandbox/proxies/Caddyfile`](proxies/Caddyfile).
- **`sandbox-egress`** — tinyproxy, dual-homed on `sandbox_net` (inbound from sandboxes) and `sandbox_egress_out` (outbound to the internet). Allowlist enforced by [`tinyproxy.filter`](proxies/tinyproxy.filter) with `FilterDefaultDeny Yes`. Built from [`Dockerfile.sandbox-egress`](proxies/egress.Dockerfile) (alpine + `apk add tinyproxy`) rather than a pulled image, so we own the entrypoint + permission model.
- **`sandbox-db`** — Postgres 16-alpine. Backing store for `common.jobs.PostgresRegistry`. See [`shared/common/src/common/jobs/postgres.py`](../shared/common/src/common/jobs/postgres.py).

## Runtimes

Add a new one by adding a Dockerfile + one entry in [`ai/sandbox/runner/runtimes.py`](runner/runtimes.py).

| Runtime | Base image | Pre-baked packages | Default entrypoint |
|---|---|---|---|
| `static` | `sandbox-static:latest` (nginx:alpine) | — | `nginx -g 'daemon off;'` |
| `python` | `sandbox-python:latest` (python:3.11-slim) | streamlit, gradio, flask, fastapi, uvicorn, pandas, numpy, matplotlib, plotly, requests, pillow | `streamlit run app.py --server.port 80 --server.address 0.0.0.0 --server.headless true` |
| `node` | `sandbox-node:latest` (node:20-slim) | serve, vite, react, react-dom, express, next | `npx --yes serve -l 80 .` |

If the caller ships `requirements.txt` (python) or `package.json` (node), the base image's entrypoint runs `pip install` / `npm install` via `sandbox-egress` before executing the command — first render takes 20–90s, subsequent HTTP requests are hot.

All base images normalize the app port to **80** so `sandbox-proxy` can statically route with no per-sandbox Caddy config.

## Endpoints

`sandbox-runner` exposes:

| Method + path | Purpose |
|---|---|
| `GET /health` | healthcheck; reports `db` up/down |
| `POST /run` | spawn a sandbox, return its URL — see request shape below |
| `GET /jobs` | list active + recent sandboxes |
| `GET /jobs/{id}` | one sandbox detail |
| `DELETE /jobs/{id}` | tear down a sandbox early, release its slot |
| `/mcp` | FastMCP HTTP transport — exposes the `preview_app` tool |

### `POST /run` request

```json
{
  "runtime":   "python",
  "files":     {
    "app.py":           "import streamlit as st\nst.title('hi')\nst.slider('n', 0, 100)",
    "requirements.txt": ""
  },
  "entrypoint":   null,
  "ttl_seconds":  900
}
```

Response:

```json
{ "sandbox_id": "a1b2c3d4e5f6", "url": "http://sandbox-proxy/a1b2c3d4e5f6/", "expires_at": "..." }
```

### `preview_app` MCP tool

Same fields as `POST /run`, exposed to tool-calling models. LiteLLM advertises it via [`ai/litellm/litellm_config.yaml`](../litellm/litellm_config.yaml)'s `mcp_servers.sandbox` entry.

The tool's return value is a **string** containing a short summary and a fenced ` ```html ` block with the iframe pre-rendered — the model just needs to include the returned string verbatim in its response and Open WebUI's markdown renderer promotes the block into an iframe artifact. No system-prompt tweaks or per-model config required as long as the model follows the tool's docstring.

If model paraphrasing is a problem in practice, use the Tool Server integration below instead — that path bypasses the model entirely.

### OpenWebUI Tool Server

For direct rich-UI rendering (no model round-trip), sandbox-runner also exposes a purpose-built REST endpoint compatible with OpenWebUI's Tool Server integration ([docs](https://docs.openwebui.com/features/extensibility/plugin/development/rich-ui/)).

Register in **Admin Panel → Settings → Tools → Add Connection** with base URL:

- `http://sandbox-runner:8000/tool` (from Open WebUI, on `ai_shared`)
- `http://localhost:8012/tool` (from the host / local dev)

OpenWebUI discovers the tool schema at `GET /tool/openapi.json` and calls `POST /tool/preview_app`. The response uses `Content-Disposition: inline`, which OpenWebUI recognizes as a rich-UI embed and renders the returned `<iframe>` as an inline sandboxed artifact. See [`ENDPOINTS.md § OpenWebUI Tool Server`](ENDPOINTS.md#openwebui-tool-server-tool) for the full contract.

## Public iframe routing

The tool returns an `<iframe src="…">`, and the user's browser is the one that has to fetch that URL. This deployment routes sandbox traffic **under the same origin as Open WebUI**, so the auth cookie flows automatically and there is no cross-origin CSP surprise.

**Traffic path:**

```
browser ──HTTPS── cloudflared ──HTTP── oauth2-proxy ──HTTP── sandbox-proxy ──HTTP── sandbox-{id}:80
   (chat.zeoenergy.com/sandboxes/{id}/*)                       (Caddy)          (sandbox_net)
```

Concretely:

- **`SANDBOX_PROXY_URL=https://chat.zeoenergy.com/sandboxes`** (`.env`) — the runner uses this as the iframe `src` prefix. Set to a public URL under the same host as Open WebUI.
- **`OAUTH2_PROXY_UPSTREAMS`** (`.env`) includes `http://sandbox-proxy:80/sandboxes/` alongside the openwebui and oauth2-assets upstreams. oauth2-proxy does longest-prefix match — `/sandboxes/*` gets forwarded to sandbox-proxy (path prefix preserved), everything else still goes to openwebui.
- **`ai/sandbox/proxies/Caddyfile`** has a route matcher for `^/sandboxes/(?P<id>[a-f0-9]{12})(/.*)?$` that strips the `/sandboxes/{id}` prefix and reverse-proxies to `sandbox-{id}:80` on `sandbox_net`. The bare `^/(?P<id>…)$` route is kept alongside it so `curl http://localhost:8011/{id}/` still works for host-side debugging.

**Auth behavior:** the `/sandboxes/*` path is **not** in `OAUTH2_PROXY_SKIP_AUTH_ROUTES`, so oauth2-proxy gates it exactly like the rest of Open WebUI. An authenticated browser at `chat.zeoenergy.com` has the `_oauth2_proxy` cookie for the whole domain, so the iframe fetch is silently authorized — the user sees a rendered preview with no extra sign-in step. An anonymous request to `/sandboxes/{id}/` gets the branded Google sign-in page.

**When to change any of this:**

- Different chat host? Update both `SANDBOX_PROXY_URL` and the cookie/redirect settings you already have for that host.
- Want the sandbox on a separate subdomain (e.g. `sandboxes.zeoenergy.com` via its own cloudflared tunnel, no auth gate)? Set `SANDBOX_PROXY_URL` to that URL, remove the `/sandboxes/` upstream from `OAUTH2_PROXY_UPSTREAMS`, and drop the `/sandboxes/{id}/` route from the Caddyfile (or leave it — it's harmless if unused). This trades single-origin ergonomics for the ability to serve sandboxes without an auth cookie.

**Verifying end-to-end:**

```bash
# From the host — spawn a sandbox and confirm the returned URL is the public one.
curl -X POST http://localhost:8012/run \
  -H 'Content-Type: application/json' \
  -d '{"runtime":"static","files":{"index.html":"<h1>hi</h1>"}}'
# Response url should look like https://chat.zeoenergy.com/sandboxes/<id>/

# From any container on ai_shared — confirm both routes serve the same content.
docker exec <any-ai_shared-container> sh -c \
  "wget -qO- http://sandbox-proxy/sandboxes/<id>/"
# → serves the sandbox's HTML.

# From a browser signed in at chat.zeoenergy.com — open the returned URL directly.
# → serves the sandbox's HTML, no extra sign-in required.
```

## Operator runbook

### First-time setup

```bash
# 1. Create the shared docker network if it's not already up.
docker network create ai_shared 2>/dev/null || true

# 2. Build the base images for the runtimes.
docker compose -f ai/sandbox/docker-compose.sandbox.yml --profile build build

# 3. Bring up the four running services.
docker compose -f ai/sandbox/docker-compose.sandbox.yml up -d

# 4. Verify the segmentation invariants — see checklist above.
docker exec -it $(docker ps -q --filter label=sandbox.managed=true | head -1) sh
```

Nothing spawns a sandbox container until an operator or model calls `POST /run` or `preview_app`. To smoke-test without a model:

```bash
curl -X POST http://localhost:8012/run -H 'Content-Type: application/json' -d '{
  "runtime": "static",
  "files": {"index.html": "<h1>hello sandbox</h1>"}
}'
```

Then open the returned URL in a browser (from the host — reach `sandbox-proxy` at `http://localhost:8011/{sandbox_id}/`).

### Adding a runtime

1. Create `ai/sandbox/images/<name>.Dockerfile` — non-root user `1000:1000`, `HOME=/home/sandbox`, entrypoint that handles the install-if-present pattern, app binds port 80.
2. Add an entry to `RUNTIMES` in [`ai/sandbox/runner/runtimes.py`](runtimes.py).
3. Add a `sandbox-<name>-image` service with `profiles: ["build"]` in [`docker-compose.sandbox.yml`](docker-compose.sandbox.yml).
4. Rebuild base images and restart the runner:
   ```bash
   docker compose -f ai/sandbox/docker-compose.sandbox.yml --profile build build
   docker compose -f ai/sandbox/docker-compose.sandbox.yml up -d --force-recreate sandbox-runner
   ```
5. Update this file's runtime matrix.

### Adding an egress destination

Edit [`ai/sandbox/proxies/tinyproxy.filter`](proxies/tinyproxy.filter) — one anchored regex per line. Anchor with `^…$` so a typosquat like `pypi.org.attacker.com` is not accepted. Restart `sandbox-egress`:

```bash
docker compose -f ai/sandbox/docker-compose.sandbox.yml restart sandbox-egress
```

Never add `.*` — that defeats the entire egress model. Add the specific domain the sandbox needs.

### Tearing down a stuck sandbox

```bash
# by sandbox_id (what /jobs returns)
curl -X DELETE http://localhost:8012/jobs/<sandbox_id>

# or if the runner is unreachable, direct docker
docker ps --filter label=sandbox.managed=true --filter label=sandbox.id=<sandbox_id>
docker rm -f sandbox-<sandbox_id>
```

The reaper (`ai/sandbox/runner/reaper.py`) also runs a background sweep every 60s and reaps anything older than `SANDBOX_HARD_TTL_SECONDS`, plus any *running* sandbox whose `metadata->>'last_used_at'` is older than `SANDBOX_IDLE_TTL_SECONDS` (defaults to `SANDBOX_DEFAULT_TTL_SECONDS`, 15 min). `last_used_at` is stamped at spawn and bumped on every session-reuse call, so an active chat keeps its preview alive while a silent one is torn down on its own.

### Sessions (persistent previews across turns)

`preview_app` accepts an optional `session_id` argument. On the first call the runner generates one and returns it in the response — both as the JSON `session_id` field and as a `Session id: XYZ` line in the rendered text block, so the model can grep it back on the next turn.

On a follow-up call with the same `session_id`, the runner writes the new file map into the running container's `/app` via `container.put_archive` — no respawn, same URL. The dev server inside detects the file change and reloads:

| Runtime | Default entrypoint | Hot-reload behavior |
|---|---|---|
| `static` | nginx | file overwrite → served on next request. User refreshes the iframe. |
| `python` | `streamlit run app.py` | Streamlit watches mtimes → auto-reruns. No refresh needed. |
| `node` | `npx serve -l 80 .` | Serves from disk each request. Refresh shows updates. Override to `npm run dev` (vite/next) for HMR without refresh. |

Follow-up calls treat `files` as an **overlay** — only paths that changed need to be re-sent; the rest are preserved. Pass `deletes: [...]` to explicitly remove files (relative paths under `/app`; absolute paths and `..` are rejected).

If the session's container has already been reaped when the model calls back (idle-TTL expired, or the container crashed), the runner **self-heals**: it respawns under the same session_id and returns a new URL. Callers don't need to distinguish the two cases; the model gets a working preview either way.

Explicit close: `DELETE /session/{session_id}` tears the container down and returns 204 whether or not the session existed. Useful in tests; models rarely need it because the idle-TTL sweeps handle silent chats.

### Auditing what a sandbox did

The runner writes to a CSV audit log at `/data/audit.log` inside `sandbox-runner`. Every `/run` and `/mcp preview_app` call is recorded with the runtime, file-name hashes, entrypoint, and returned sandbox_id.

```bash
docker exec sandbox-runner cat /data/audit.log
```

For deeper forensics on a specific sandbox:

```bash
# stdout/stderr of the app process
docker logs sandbox-<sandbox_id>

# job record (phase transitions, timestamps)
docker exec -it sandbox-db psql -U sandbox -d sandbox \
  -c "SELECT id, phase, jsonb_pretty(metadata), jsonb_pretty(result), error, created_at, updated_at FROM jobs WHERE id = '<sandbox_id>';"

# egress requests this sandbox made (via tinyproxy access log)
docker logs sandbox-egress | grep '<sandbox_id or IP>'
```

### Rotating the sandbox-db password

```bash
# 1. Set SANDBOX_DB_PASSWORD in .env to the new value.
# 2. Bring the DB down, remove the volume, bring it back up (DEV ONLY — data loss).
docker compose -f ai/sandbox/docker-compose.sandbox.yml down sandbox-db
docker volume rm sandbox_db_data
docker compose -f ai/sandbox/docker-compose.sandbox.yml up -d sandbox-db

# For prod-style rotation (no data loss), ALTER USER inside psql and update .env
# then restart just the runner:
docker exec -it sandbox-db psql -U postgres -c "ALTER USER sandbox WITH PASSWORD 'new-pw';"
docker compose -f ai/sandbox/docker-compose.sandbox.yml up -d --force-recreate sandbox-runner
```

## Configuration

See the `SANDBOX_*` block in [`.env.example`](../.env.example) and the config table in [`CLAUDE.md`](../CLAUDE.md#configuration).
