# Sandbox Subsystem

Runs model-generated web apps in short-lived, network-segmented containers, and exposes them to Open WebUI as iframe artifacts. Think Claude.ai Artifacts, but for **anything** — Streamlit, Gradio, Flask, FastAPI, Vite+React, Next, Express, static HTML.

## When to reach for this

- The user asks for an **interactive** demo (a slider that redraws a chart, a form that hits a server, a small CRUD toy) — anything that a single HTML block cannot express.
- The model outputs code and you want to see it run *before* the user manually copies it into their editor.
- You need to run untrusted code (pasted from a bug report, an issue thread, or a webpage the model was asked to summarize) somewhere it cannot reach the rest of the stack.

If a fenced ` ```html ` block would do the job, prefer that — Open WebUI renders it in an iframe with zero infra. The sandbox is for cases where you need a real runtime.

## Security model (read this first)

The single security control that matters is **network segmentation**. Everything else is defense in depth.

Three Docker networks:

| Network | Type | Members |
|---|---|---|
| `ai_shared` | external bridge | openwebui, litellm, `sandbox-runner`, `sandbox-proxy` |
| `sandbox_net` | bridge, `internal: true` | `sandbox-runner`, `sandbox-proxy`, `sandbox-egress`, every `sandbox-{id}` |
| `sandbox_state` | bridge, `internal: true` | `sandbox-runner`, `sandbox-db` |

`internal: true` means Docker **does not attach a default gateway**. A container on `sandbox_net` that tries to hit the host, the internet, or any container on `ai_shared` has no route — the packets go nowhere.

Consequences:

- Sandboxed code cannot reach `litellm`, `phoenix-mcp`, `roofix-db`, `interceptor`, `openwebui`, or any other stack service by name.
- Sandboxed code cannot reach `sandbox-db` — the job store lives on `sandbox_state`, which sandboxes don't touch. Only `sandbox-runner` bridges to `sandbox_state`.
- Outbound HTTP from a sandbox must go through `sandbox-egress`, which enforces a domain allowlist (`pypi.org`, `files.pythonhosted.org`, `registry.npmjs.org`, `esm.sh`, `cdn.jsdelivr.net`). Anything else drops.

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
       │      sandbox-egress ──► internet             │             │
       │       (allowlist)                            │             │
       └──────────────────────────────────────────────┘             │
                                                                    │
                                                                    ▘
```

- **`sandbox-runner`** — FastAPI + MCP + `docker.from_env()`. On all three networks. Spawns and reaps `sandbox-{id}` containers. Writes job state to `sandbox-db`. See [`ai/sandbox/app.py`](sandbox/app.py).
- **`sandbox-proxy`** — Caddy. Routes `/{sandbox_id}/*` → `sandbox-{id}:80`. Strips `X-Frame-Options` and rewrites CSP so Open WebUI can iframe the result. See [`ai/sandbox/Caddyfile`](sandbox/Caddyfile).
- **`sandbox-egress`** — tinyproxy. Allowlist enforced by [`tinyproxy.filter`](sandbox/tinyproxy.filter) with `FilterDefaultDeny Yes`.
- **`sandbox-db`** — Postgres 16-alpine. Backing store for `common.jobs.PostgresRegistry`. See [`shared/common/src/common/jobs/postgres.py`](../shared/common/src/common/jobs/postgres.py).

## Runtimes

Add a new one by adding a Dockerfile + one entry in [`ai/sandbox/runtimes.py`](sandbox/runtimes.py).

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

Same shape, exposed for tool-calling models. LiteLLM advertises it via [`ai/litellm_config.yaml`](litellm_config.yaml)'s `mcp_servers.sandbox` entry. Models that use it should wrap the returned URL in a fenced ` ```html ` block containing an `<iframe src="...">` — Open WebUI's artifact renderer picks that up.

Add a system prompt on the Qwen model in Open WebUI's Admin → Models → *your model* → System Prompt:

> When the user asks for an interactive app (with a slider, form, or anything a static HTML block can't express), call `preview_app` with `runtime`, `files`, and an `entrypoint`. Emit a fenced html block containing `<iframe src="URL" width="100%" height="600px"></iframe>` using the returned `url`. Do not paraphrase the URL.

## Operator runbook

### First-time setup

```bash
# 1. Create the shared docker network if it's not already up.
docker network create ai_shared 2>/dev/null || true

# 2. Build the base images for the runtimes.
docker compose -f ai/docker-compose.sandbox.yml --profile build build

# 3. Bring up the four running services.
docker compose -f ai/docker-compose.sandbox.yml up -d

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

1. Create `ai/Dockerfile.sandbox-<name>` — non-root user `1000:1000`, `HOME=/home/sandbox`, entrypoint that handles the install-if-present pattern, app binds port 80.
2. Add an entry to `RUNTIMES` in [`ai/sandbox/runtimes.py`](sandbox/runtimes.py).
3. Add a `sandbox-<name>-image` service with `profiles: ["build"]` in [`docker-compose.sandbox.yml`](docker-compose.sandbox.yml).
4. Rebuild base images and restart the runner:
   ```bash
   docker compose -f ai/docker-compose.sandbox.yml --profile build build
   docker compose -f ai/docker-compose.sandbox.yml up -d --force-recreate sandbox-runner
   ```
5. Update this file's runtime matrix.

### Adding an egress destination

Edit [`ai/sandbox/tinyproxy.filter`](sandbox/tinyproxy.filter) — one anchored regex per line. Anchor with `^…$` so a typosquat like `pypi.org.attacker.com` is not accepted. Restart `sandbox-egress`:

```bash
docker compose -f ai/docker-compose.sandbox.yml restart sandbox-egress
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

The reaper (`ai/sandbox/reaper.py`) also runs a background sweep every 60s and reaps anything older than `SANDBOX_HARD_TTL_SECONDS`. Idle-TTL enforcement (based on last HTTP hit through `sandbox-proxy`) is a known follow-up — for now, `SANDBOX_DEFAULT_TTL_SECONDS` is stored on the job record but not yet acted on.

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
docker compose -f ai/docker-compose.sandbox.yml down sandbox-db
docker volume rm sandbox_db_data
docker compose -f ai/docker-compose.sandbox.yml up -d sandbox-db

# For prod-style rotation (no data loss), ALTER USER inside psql and update .env
# then restart just the runner:
docker exec -it sandbox-db psql -U postgres -c "ALTER USER sandbox WITH PASSWORD 'new-pw';"
docker compose -f ai/docker-compose.sandbox.yml up -d --force-recreate sandbox-runner
```

## Configuration

See the `SANDBOX_*` block in [`.env.example`](../.env.example) and the config table in [`CLAUDE.md`](../CLAUDE.md#configuration).
