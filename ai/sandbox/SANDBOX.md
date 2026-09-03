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

## MCP tool flow

The sandbox subsystem exposes an MCP server named `sandbox` with 11 tools. Models compose them into a workflow that maps to the way a human dev works — warm the runtime, iterate silently, hand the user something to look at, tear down.

```
get_runtimes    ─── optional, first time on a deployment
      │
      ▼
create          ─── returns session_id + warming URL; env fixed here
      │
      ▼
update_files    ─── loop: overlay files; post-update health probe
      │  ▲              recreate_if_gone=false (default) surfaces respawn choice
      │  └─── get_files / get_logs / exec (read/modify on demand)
      │  └─── list_sessions (recover dropped handles)
      ▼
preview         ─── show the user the iframe artifact
      │
      ▼
close           ─── optional; TTL reaper handles the rest
```

Shortcut path when the model already has everything at first mention:

```
run             ─── create + update_files + preview in one call
                    (recreate_if_gone implicitly true)
```

Tool summary:

| Tool | Kind | Purpose |
|---|---|---|
| `get_runtimes` | read | Describe available runtimes on this deployment |
| `create` | write | Reserve an empty warming container, return session_id + URL |
| `update_files` | write | Overlay files into a live sandbox; opt-in self-heal; post-update health probe |
| `get_files` | read | Read files back from `/app` (dir listing when `paths` is omitted) |
| `get_logs` | read | Tail container stdout+stderr for the session |
| `exec` | write | Run a non-interactive shell command inside the container |
| `preview` | display | Return the iframe artifact for the user's chat |
| `close` | write | Tear down the session early, release the slot |
| `list_sessions` | read | Enumerate live sandboxes (recover dropped session_ids) |
| `run` | write | Convenience: create + update_files + preview in one call |
| `preview_app` | write (deprecated) | Alias for `run` — kept for one release cycle |

### Data shapes shared by every write tool

**`files` overlay.** Path → value map. Values are either UTF-8 strings (the common case) OR a discriminated dict `{"encoding": "base64", "content": "..."}` for binary content (images, PDFs, wheels). The pydantic validator enforces per-file (`SANDBOX_MAX_FILE_BYTES`) and total (`SANDBOX_MAX_PAYLOAD_BYTES`) caps at request-parse time — base64 values count their DECODED length. Absolute paths and `..` traversal are rejected downstream in the spawner.

**`env` at create time.** Process env vars set inside the container. Immutable after spawn — a self-heal respawn replays the same env from the job's metadata JSONB, but there is no update path. Reserved keys (`HTTP_PROXY`, `HTTPS_PROXY`, `PYTHONUNBUFFERED`, `NPM_CONFIG_LOGLEVEL`, `FORCE_COLOR`, `TERM`) are rejected at validation — those are runner-controlled invariants that keep the egress allowlist and log buffering working; the spawner re-applies them on top of the merged env even if a validation bypass ever landed.

**`recreate_if_gone` on `update_files`.** OPT-IN self-heal. Default `false` — if the container is gone (crashed, reaped, respawn refused), the tool returns a structured 409 telling the caller to opt in explicitly. Set `true` to accept the state loss: env is preserved, but packages installed via `exec` and any in-container files not in the current `files` map are LOST. `run` implicitly passes `true` because it's the one-shot convenience path.

### Structured tool responses

Every MCP tool returns a `ToolResult` with two channels:

- `content[0]` is a `TextContent` block (what the model sees, same shape as the pre-refactor tool responses).
- `structured_content` is a JSON payload the caller can parse without regex. Fields depend on the tool: `create` / `run` include `session_id`, `sandbox_id`, `url`, `expires_at`, `runtime`, `app_status`; `update_files` adds `files_written` / `files_deleted` / `recreated`; `exec` returns `exit_code`, `duration_ms`, `output`, `truncated`, `timed_out`; and so on. See [`sandbox_mcp.py`](runner/sandbox_mcp.py) for the exact shapes.

The text form preserves everything a model needs to reply — the fenced ```` ```html ```` block for `preview` / `run`, the `Session id:` line for follow-ups, the health-probe verdict, the startup-output tail. The structured payload is additive.

## HTTP endpoints

`sandbox-runner` exposes:

| Method + path | Purpose |
|---|---|
| `GET /health` | healthcheck; reports `db` up/down |
| `POST /run` | spawn a sandbox — or update an existing one when `session_id` is passed. Backing endpoint for `sandbox.run` |
| `POST /create` | reserve an empty warming container. Backing endpoint for `sandbox.create` |
| `GET /sessions` | enumerate live sessions. Backing endpoint for `sandbox.list_sessions` |
| `POST /session/{id}/files` | overlay files into a running sandbox. Backing endpoint for `sandbox.update_files` |
| `GET /session/{id}/files` | read files back from `/app`. Backing endpoint for `sandbox.get_files` |
| `POST /session/{id}/exec` | run a non-interactive shell command inside the container. Backing endpoint for `sandbox.exec` |
| `GET /session/{id}/logs` | tail container stdout+stderr. Backing endpoint for `sandbox.get_logs` |
| `GET /session/{id}/download` | stream `/app` as a tar archive (session-based; follows self-heal) |
| `DELETE /session/{id}` | tear a session's sandbox down early. Backing endpoint for `sandbox.close` |
| `GET /jobs` | list active + recent sandboxes |
| `GET /jobs/{id}` | one sandbox detail |
| `DELETE /jobs/{id}` | tear down a sandbox early, release its slot |
| `GET /jobs/{id}/logs` / `/download` | operator direct-access variants that don't follow self-heal |
| `/mcp` | FastMCP HTTP transport — exposes every tool listed above |
| `/tool/run` | OpenWebUI Tool Server — inline HTMLResponse |
| `/tool/preview_app` | deprecated alias for `/tool/run` |
| `/tool/get_runtimes` | describe available runtimes (Tool Server variant) |

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

### Model-facing tool surface

Every tool listed in the "MCP tool flow" section above is registered on the `sandbox` MCP server. LiteLLM advertises them via [`ai/litellm/litellm_config.yaml`](../litellm/litellm_config.yaml)'s `mcp_servers.sandbox` entry.

Return values are `ToolResult`s with a text block (what the model reads) plus a JSON `structured_content` payload. The text block for `run` / `preview` includes a fenced ` ```html ` code fence containing a self-contained HTML document — the model includes the returned string verbatim in its response and Open WebUI's markdown renderer promotes the block into a split-panel artifact. No system-prompt tweaks or per-model config required as long as the model follows the tool's docstring.

If model paraphrasing is a problem in practice, use the Tool Server integration below instead — that path bypasses the model entirely.

### OpenWebUI Tool Server

For direct rich-UI rendering (no model round-trip), sandbox-runner also exposes a purpose-built REST endpoint compatible with OpenWebUI's Tool Server integration ([docs](https://docs.openwebui.com/features/extensibility/plugin/development/rich-ui/)).

Register in **Admin Panel → Settings → Tools → Add Connection** with base URL:

- `http://sandbox-runner:8000/tool` (from Open WebUI, on `ai_shared`)
- `http://localhost:8012/tool` (from the host / local dev)

OpenWebUI discovers the tool schema at `GET /tool/openapi.json` and calls `POST /tool/run` (or the deprecated `POST /tool/preview_app` alias, kept for one release cycle so existing registrations don't break mid-rollout). The response uses `Content-Disposition: inline`, which OpenWebUI recognizes as a rich-UI embed and drops the returned HTML into a sandboxed artifact iframe. See [`ENDPOINTS.md § OpenWebUI Tool Server`](ENDPOINTS.md#openwebui-tool-server-tool) for the full contract.

### How the artifact renders (alignment with the OpenWebUI docs)

Both paths return the **same self-contained HTML document** (see `render_preview_html` in `runner/sandbox_mcp.py`). Design follows OpenWebUI's [Artifacts documentation](https://docs.openwebui.com/features/chat-conversations/chat-features/code-execution/artifacts/):

- **Fence tag is `html`.** OpenWebUI's `ContentRenderer.svelte` promotes only ` ```html ` and ` ```svg ` blocks into artifacts (`['html','svg'].includes(normalizedLang)`). No other tag qualifies.
- **Content is a "complete webpage."** The doc's supported artifact shapes are single-page HTML websites, SVG, complete webpages (HTML+JS+CSS in one artifact), and ThreeJS/D3 visualizations. We return a full `<!doctype html>` + `<html>` + `<head>` + `<body>` document to hit the "complete webpage" case cleanly.
- **Rendered via `srcdoc` (not `src`).** OpenWebUI's `Artifacts.svelte` uses `srcdoc={content}` — it does not fetch an external URL. That's why we DON'T put `<iframe src="…">` inside our block. Doing so would render two iframe layers (OpenWebUI's srcdoc wrapping our iframe wrapping the sandbox). Instead the document uses `<meta http-equiv="refresh">` + a JS `location.replace` fallback to navigate the srcdoc iframe itself to the sandbox URL — one iframe layer, no wrapping.
- **Per-response nonce for hot-reload re-promotion.** OpenWebUI dedupes with `autoOpenedArtifactIds`: once a given artifact opens the panel it won't re-open. A stable-URL follow-up call would otherwise produce byte-identical HTML → identical artifact id → no re-promote if the user closed the panel between turns. The leading `<!-- preview session=… sandbox=… -->` comment changes on every response (sandbox_id changes on self-heal spawns; the session_id nonce ensures a unique fingerprint per call) so each update looks like a fresh artifact.

**One sandbox flag matters for interactive apps.** Under **Admin Panel → Settings → Interface → Artifacts**, four sandbox toggles control what the srcdoc iframe can do:

| Toggle | Default | Sandbox behavior with the meta-refresh approach |
|---|---|---|
| iframe Sandbox Allow Scripts | On | Required — the `location.replace` fallback needs this. |
| iframe Sandbox Allow Forms | On | Fine either way. |
| iframe Sandbox Allow Downloads | On | Fine either way. |
| iframe Sandbox Allow Same Origin | **Off** | **Turn ON for Streamlit / Vite / Next / any app doing same-origin WebSocket, XHR, or localStorage.** With it off, the iframe (after navigation) is treated as an opaque origin and same-origin requests to `chat.zeoenergy.com/sandboxes/{id}/` behave cross-origin — Streamlit's hot-reload WebSocket can be rejected on Origin, Vite HMR can fail, apps that touch localStorage break. Static-runtime sandboxes are unaffected. |

There's no security cost to enabling **Allow Same Origin** in this deployment: sandbox URLs are already gated by `oauth2-proxy` and the containers are network-segmented via `internal: true` on `sandbox_net`. The flag only controls whether the *artifact iframe* trusts the origin it navigated to.

## Public iframe routing

Because the artifact iframe navigates to `${SANDBOX_PROXY_URL}/{sandbox_id}/`, the user's browser needs to reach that URL. This deployment routes sandbox traffic **under the same origin as Open WebUI**, so the auth cookie flows automatically and there is no cross-origin CSP surprise.

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

### Downloading source

Every `preview_app` response includes a `Download source:` URL of the form `${SANDBOX_PROXY_URL}/download/{session_id}` (e.g. `https://chat.zeoenergy.com/sandboxes/download/{session_id}`). When a user asks to save, keep, or export the code, the model just shares that URL — the browser downloads a plain tar of the container's `/app` directory, streamed straight from the Docker daemon (no in-memory buffering in the runner).

Auth is oauth2-proxy — same session cookie as the preview iframe — so users don't get a second login prompt. The URL is session-based, so it stays valid across self-heal spawns (resolves session_id → currently-running sandbox_id at request time). Under the hood: the Caddy route in [`proxies/Caddyfile`](proxies/Caddyfile) reverse-proxies `/sandboxes/download/{session_id}` to sandbox-runner's `/session/{session_id}/download` endpoint; the runner calls `Spawner.export_files` which wraps Docker SDK's `container.get_archive("/app")`. There's also a `/jobs/{sandbox_id}/download` variant for operator direct-download by internal id.

Archive shape: `sandbox-{sandbox_id}.tar` with `app/` at the root. Extract with `tar -xf sandbox-*.tar` (Windows 10+ ships `tar` in cmd; every Unix has it).

### Error feedback for models

Three layers, in order of cheapness:

1. **Static Python lint (before spawn, ~1 ms).** `_lint_python_files` in [`runner/app.py`](runner/app.py) walks every `.py` file in the request and calls `compile(source, "<sandbox:path>", "exec")`. SyntaxError catches trigger a 400 with a compiler-style diagnostic — path, line, offset, source line, caret — plus the generated `session_id` so a retry with the same id transparently self-heals. Angle-bracket display name stops Python from reading a same-named file off the runner's own disk.

2. **Container logs on readiness failure (30 s spawn timeout).** If the container spawns but never binds port 80 within `readiness_ok`'s deadline, the runner reads the tail of `/tmp/sandbox.log` before tearing the container down and returns it in the 504. Log capture is possible because [`runner/spawner.py`](runner/spawner.py) redirects the user command's stdout+stderr to that file — `container.logs()` only sees PID 1 (`sleep infinity`), so exec streams have to be routed through a file.

3. **On-demand log fetch (for apps that start but render errors).** `GET /session/{id}/logs` and its MCP counterpart `get_logs` tail `/tmp/sandbox.log` at request time. Model calls this when `update_files` / `run` returned a normal ready response but the user reports the running app looks broken. Flask / FastAPI / Vite / Next / Express dev servers all print tracebacks to stdout before rendering a browser error card. **Streamlit tracebacks land here too** thanks to the `_streamlit_bootstrap.py` shim: the runner writes it into `/app` at spawn time when the entrypoint is Streamlit, and it monkeypatches `streamlit.runtime.scriptrunner.script_runner.handle_uncaught_app_exception` to also print to stderr before delegating to the original browser-render path. If the shim import fails on a Streamlit version bump, it logs a WARNING and falls through — Streamlit stays functional, just without the tee.

4. **Post-update HTTP health probe.** `update_files` (and the reuse branch of `run`) runs a single-shot GET against the app's readiness path 500 ms after writing files, and inlines the result — `HTTP 200 in 47 ms` / `HTTP 500` / `connection refused` / `timeout after 3.0s` — in the tool response. Catches "the reload broke everything" in the same tool call without waiting for the user to report anything.

The MCP tool wrappers in [`runner/sandbox_mcp.py`](runner/sandbox_mcp.py) catch HTTPExceptions from the runner's spawn path and format them into text responses instead of MCP-level errors, so the model reads them as normal tool output and can call the tool again with the same `session_id` without any error-handling logic.

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

### Runtime introspection (exec)

The `exec` MCP tool (backed by `POST /session/{id}/exec`) runs a non-interactive shell command inside a running sandbox and streams the combined stdout+stderr back with the exit code and duration. It exists to close a real gap in the tool set: when the model forgets a dependency in `requirements.txt`, when a `pip install` needs to run without rebuilding the container, when the model wants to introspect `/app` (`ls -la`, `cat`, `curl http://localhost:80`), or when it needs to invoke framework tooling (`npx prisma generate`, `python migrate.py`).

Design constraints:

- **Non-interactive.** stdin is closed by the SDK's exec_start. A command that prompts for input hangs until the timeout kicks in — models MUST use non-interactive flags (`-y`, `--yes`, `PIP_YES=1`).
- **Timeout hard-capped at 120 s.** Default 30 s. Enforced by wall-clock deadline on the exec_start stream — the exec is left dangling on timeout (Docker cleans it up when the container is torn down); the tool response is marked `timed_out`.
- **Output truncated to 8 KB.** Prefixed with `(last 8 KB)` when truncation happened. Enough for pip/npm summaries and most tracebacks; anything longer belongs in `get_logs`.
- **Runs as 1000:1000.** Matches the base image's non-root user. `sudo` is not available.
- **State drift on respawn.** Packages installed via `exec` do NOT survive a self-heal respawn — the container is rebuilt from the base image and only `env` is preserved. Models should ALSO write persistent deps to `requirements.txt` / `package.json` via `update_files` when appropriate:
  ```
  exec(sid, "pip install requests")                # immediate fix
  update_files(sid, {"requirements.txt": "..."})   # persist for future respawn
  ```
- **Egress allowlist still applies.** A `pip install` from a non-allowlisted index gets a 403 from `sandbox-egress` (tinyproxy). If a network error surfaces here, the fix is to add the source to `ai/sandbox/proxies/tinyproxy.filter`, not to retry.
- **Counts as activity.** Every `exec` call bumps `last_used_at` so the idle reaper resets.

Security analysis: `exec` opens no new escape path. The sandbox container is isolated by network segmentation, not by command-layer sandboxing — anything the container can do, the model could already do via a `python -c` embedded in the entrypoint. What `exec` opens is CPU/RAM inside the sandbox, already bounded by cgroups and the reaper's TTL.

### Env vars

`create` and `run` accept an optional `env` dict merged into the container environment at spawn time. Two things worth calling out:

- **Immutable after spawn.** There is no update-env path. A self-heal respawn replays the same env from the job's metadata JSONB — that's why env preserves across `update_files(recreate_if_gone=true)` even when in-container files don't. If a caller wants to change env, they close the session and create a new one.
- **Reserved keys refused at validation.** `HTTP_PROXY`, `HTTPS_PROXY`, `http_proxy`, `https_proxy`, `PYTHONUNBUFFERED`, `NPM_CONFIG_LOGLEVEL`, `FORCE_COLOR`, and `TERM` are runner-controlled. The spawner re-applies them on top of the caller's env even if a validation bypass ever landed, so the egress allowlist and log-buffering invariants can't be turned off by a caller. Anything else is passed through.
- **Visible in `docker inspect`.** Any operator with access to `sandbox-runner`'s Docker socket can read env values. That's the same access boundary that already holds the docker.sock — no new surface. Secrets in env should still be per-user and time-scoped, not shared long-lived keys.

### Payload limits

Two knobs, both env-tunable:

- `SANDBOX_MAX_FILE_BYTES` (default `1000000`) — per-file cap on every value in the `files` map (post-base64-decode).
- `SANDBOX_MAX_PAYLOAD_BYTES` (default `10000000`) — total cap across all files in one request.

Enforcement lives in `_validate_files_map` at pydantic validation, so a rejected payload never touches the tarball builder or the container. The 413 response names the largest offending path and the total submitted size so the caller can shrink or split across multiple `update_files` calls. Base64 values count their DECODED length so a client can't smuggle a large blob past the cap by encoding it.

## Configuration

See the `SANDBOX_*` block in [`.env.example`](../.env.example) and the config table in [`CLAUDE.md`](../CLAUDE.md#configuration).
