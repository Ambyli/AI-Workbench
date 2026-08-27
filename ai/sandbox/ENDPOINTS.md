# sandbox-runner — HTTP endpoints

Base URL from the host: `http://localhost:8012`
Base URL on the `ai_shared` Docker network: `http://sandbox-runner:8000`

Everything below is served by [`ai/sandbox/app.py`](app.py). Jobs endpoints are contributed by the shared [`build_router`](../../shared/common/src/common/jobs/router.py) factory in `common.jobs`.

## Summary

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness + DB status |
| `POST` | `/run` | Spawn a sandbox; returns JSON with URL + id |
| `GET` | `/jobs` | List every sandbox in the registry (running + terminal) |
| `GET` | `/jobs/{sandbox_id}` | One sandbox's snapshot |
| `DELETE` | `/jobs/{sandbox_id}` | Tear a sandbox down early, release its slot |
| `POST` | `/mcp/` | FastMCP JSON-RPC endpoint — see [MCP section](#mcp) |
| `GET` | `/tool/openapi.json` | OpenAPI schema for OpenWebUI's Tool Server discovery |
| `POST` | `/tool/preview_app` | Spawn a sandbox and return an inline-rendered iframe (Content-Disposition: inline) — see [Tool Server section](#openwebui-tool-server-tool) |

There is no `PATCH`/`PUT`/`OPTIONS` surface on this service.

---

## `GET /health`

Liveness check with a lightweight `sandbox-db` probe.

**Request:**
```bash
curl http://localhost:8012/health
```

**Response (200):**
```json
{
  "status": "ok",
  "db": "up"
}
```

**Degraded response (still 200):**
```json
{
  "status": "degraded",
  "db": "down"
}
```

Used by the compose healthcheck and by `sandbox-runner`'s own lifecycle checks. No auth.

---

## `POST /run`

Spawn a new sandbox running the caller's files under the given runtime. Returns the URL to iframe.

**Body:**
```json
{
  "runtime":     "static",
  "files":       { "index.html": "<h1>hello</h1>" },
  "entrypoint":  null,
  "ttl_seconds": 900
}
```

Field semantics:

| Field | Type | Required | Notes |
|---|---|---|---|
| `runtime` | string | yes | One of `static`, `python`, `node`. See [`runtimes.py`](runtimes.py) for the current registry. |
| `files` | `{ path: content }` | yes | Map of relative filesystem paths → text contents. Paths must be relative and are extracted under `/app/` in the sandbox. |
| `entrypoint` | string | no | Shell command that must bind to port `80` inside the container. Leave `null` to use the runtime's default (nginx for `static`, streamlit for `python`, `serve` for `node`). |
| `ttl_seconds` | int | no | Idle lifetime. Server clamps to `SANDBOX_HARD_TTL_SECONDS` (3600). Defaults to `SANDBOX_DEFAULT_TTL_SECONDS` (900). |

**Response (200):**
```json
{
  "sandbox_id": "a1b2c3d4e5f6",
  "url":        "http://sandbox-proxy/a1b2c3d4e5f6/",
  "expires_at": "2026-08-27T18:15:32.114513+00:00"
}
```

The returned `url` is reachable at `http://sandbox-proxy` on the `ai_shared` Docker network (i.e. from OpenWebUI). From the host it's at `http://localhost:8011/{sandbox_id}/`.

**Error responses:**

| Status | Meaning |
|---|---|
| `400` | Unknown `runtime` value. |
| `429` | Concurrency cap reached (`SANDBOX_MAX_CONCURRENT`, default 8). |
| `500` | Docker spawn error — image missing, cgroup rejection, etc. |
| `504` | Sandbox spawned but readiness probe (`GET /` inside container) didn't reply within 30s. Container is torn down before the response returns. |

**Curl examples:**

Static HTML:
```bash
curl -X POST http://localhost:8012/run \
  -H 'Content-Type: application/json' \
  -d '{
    "runtime": "static",
    "files": {"index.html": "<h1>hello sandbox</h1>"}
  }'
```

Streamlit app (Python runtime):
```bash
curl -X POST http://localhost:8012/run \
  -H 'Content-Type: application/json' \
  -d '{
    "runtime": "python",
    "files": {
      "app.py": "import streamlit as st\nst.title(\"demo\")\nst.slider(\"n\", 0, 100)"
    }
  }'
```

Vite + React app (Node runtime):
```bash
curl -X POST http://localhost:8012/run \
  -H 'Content-Type: application/json' \
  -d '{
    "runtime": "node",
    "entrypoint": "npx --yes vite --host 0.0.0.0 --port 80",
    "files": {
      "package.json": "{\"name\":\"demo\",\"type\":\"module\",\"scripts\":{\"dev\":\"vite\"}}",
      "index.html": "<!doctype html><html><head><title>x</title></head><body><div id=root></div><script type=module src=/main.jsx></script></body></html>",
      "main.jsx": "import React from \"react\";import ReactDOM from \"react-dom/client\";ReactDOM.createRoot(document.getElementById(\"root\")).render(<h1>hi</h1>);"
    }
  }'
```

---

## `GET /jobs`

List every sandbox in the registry. Includes recently-terminated ones — `sandbox-runner` doesn't auto-purge terminal rows; use `DELETE /jobs/{id}` for that.

**Query params:** none (limit defaults to 20 inside `PostgresRegistry.list_all`).

**Request:**
```bash
curl http://localhost:8012/jobs
```

**Response (200):**
```json
{
  "active_count": 2,
  "jobs": [
    {
      "job_id": "a1b2c3d4e5f6",
      "phase": "running",
      "created_at": "2026-08-27T18:00:12.331+00:00",
      "updated_at": "2026-08-27T18:00:14.884+00:00",
      "elapsed_seconds": 2.553,
      "metadata": {
        "runtime": "static",
        "entrypoint": "nginx -g 'daemon off;'",
        "ttl_seconds": 900,
        "expires_at": "2026-08-27T18:15:12.331+00:00"
      },
      "result": {
        "url": "http://sandbox-proxy/a1b2c3d4e5f6/",
        "container_name": "sandbox-a1b2c3d4e5f6"
      },
      "error": null
    }
  ],
  "max_concurrent": null,
  "available": null
}
```

`max_concurrent` / `available` are `null` here because `PostgresRegistry` doesn't track a pool ceiling (the runner enforces it via an asyncio.Semaphore, not the registry). Interceptor's in-memory registry populates these fields; this one doesn't.

`phase` values used by the sandbox subsystem:

| Phase | Meaning |
|---|---|
| `spawning` | Container being created via docker.sock. |
| `starting` | Container started, readiness probe running. |
| `running` | Sandbox ready and serving on `sandbox-proxy`. |
| `expired` | Reaper tore it down at hard-TTL. |
| `failed` | Spawn or readiness failed. `error` field is populated. |
| `cancelled` | `cancel()` was called (not surfaced via HTTP today — placeholder). |

---

## `GET /jobs/{sandbox_id}`

One sandbox's snapshot. Same shape as an item in the `/jobs` list.

**Request:**
```bash
curl http://localhost:8012/jobs/a1b2c3d4e5f6
```

**Response (200):** single `JobBase` object (same shape as elements in the `jobs` array above).

**Errors:**

| Status | Meaning |
|---|---|
| `404` | No sandbox with that id (never existed, or already deleted). |

---

## `DELETE /jobs/{sandbox_id}`

Tear a sandbox down early. Stops the container, removes it, deletes the row from the registry, and releases the concurrency slot if the sandbox was still holding one.

**Request:**
```bash
curl -X DELETE http://localhost:8012/jobs/a1b2c3d4e5f6
```

**Response (204):** no body.

**Errors:**

| Status | Meaning |
|---|---|
| `404` | No sandbox with that id. |
| `500` | Runner not initialized (should not normally occur). |

---

## MCP

FastMCP HTTP transport, mounted at `/mcp` (with a trailing-slash redirect from `/mcp` → `/mcp/`). This is the endpoint that LiteLLM's `mcp_servers.sandbox` entry points at.

Base URL from the host: `http://localhost:8012/mcp/`
Base URL on `ai_shared`: `http://sandbox-runner:8000/mcp/`

All requests are JSON-RPC 2.0 over HTTP POST. Response bodies come back as either JSON or Server-Sent Events (`Content-Type: text/event-stream`). Clients must send `Accept: application/json, text/event-stream`.

### Available tool

Exactly one:

**`preview_app`** — spawn a sandbox from the shape the model can produce inline.

Parameters (same field semantics as `POST /run`):

| Param | Type | Required |
|---|---|---|
| `runtime` | string | yes |
| `files` | `{ path: content }` | yes |
| `entrypoint` | string | no |
| `ttl_seconds` | int | no |

**Result — a single string (not a JSON object)**:

```
Preview ready. Sandbox `a1b2c3d4e5f6` at http://sandbox-proxy/a1b2c3d4e5f6/ (expires 2026-08-27T18:15:32.114513+00:00).

```html
<iframe src="http://sandbox-proxy/a1b2c3d4e5f6/" style="width:100%;height:min(85vh, 900px);border:0;border-radius:8px;background:#0e1116" allow="clipboard-read; clipboard-write" loading="lazy"></iframe>
```
```

The string is what MCP delivers to the model as `content[0].text`. When the model relays the tool result to the user, OpenWebUI's markdown renderer picks up the ` ```html ` fenced block and promotes it to an inline iframe artifact — no additional model prompting required. The plain-text lines above the block give the model + user useful context if HTML rendering is disabled or the model paraphrases.

**Important:** the tool's docstring instructs the model to include the returned string VERBATIM in its response. If the model paraphrases or drops the ` ```html ` block, the preview won't render. If you find this happens often, consider using the [`/tool/preview_app` REST endpoint](#openwebui-tool-server-tool) instead — that path bypasses the model and lets OpenWebUI render the iframe directly.

### JSON-RPC method examples

**`initialize`** — handshake. Every client must send this first.
```bash
curl -X POST http://localhost:8012/mcp/ \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
      "protocolVersion": "2024-11-05",
      "capabilities": {},
      "clientInfo": {"name": "curl-probe", "version": "0.0"}
    }
  }'
```

**`tools/list`** — enumerate available tools.
```bash
curl -X POST http://localhost:8012/mcp/ \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/list",
    "params": {}
  }'
```

**`tools/call`** — invoke `preview_app`.
```bash
curl -X POST http://localhost:8012/mcp/ \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{
    "jsonrpc": "2.0",
    "id": 3,
    "method": "tools/call",
    "params": {
      "name": "preview_app",
      "arguments": {
        "runtime": "static",
        "files": {"index.html": "<h1>from mcp</h1>"}
      }
    }
  }'
```

Session semantics: the first `initialize` returns a session id in the response headers (`Mcp-Session-Id`). Subsequent calls should include it as a request header. FastMCP handles this automatically for clients that follow the streamable-HTTP spec (LiteLLM, Claude Code, etc.).

---

## OpenWebUI Tool Server (`/tool`)

A separate FastAPI sub-app mounted at `/tool`, purpose-built for OpenWebUI's **rich UI Tool Server** integration (docs: [Extensibility → Plugin Development → Rich UI](https://docs.openwebui.com/features/extensibility/plugin/development/rich-ui/)).

**Register in OpenWebUI:** Admin Panel → Settings → Tools → Add Connection.
Base URL (on `ai_shared`): `http://sandbox-runner:8000/tool`
Base URL (from the host): `http://localhost:8012/tool`

OpenWebUI fetches `GET /tool/openapi.json` to discover the tool schema, then calls `POST /tool/preview_app` when the model wants to embed a preview. The response has `Content-Disposition: inline` so OpenWebUI renders it as a sandboxed iframe under the tool call indicator.

### `GET /tool/openapi.json`

Auto-generated OpenAPI 3.0 schema. Used by OpenWebUI at Tool-Server registration time.

**Request:**
```bash
curl http://localhost:8012/tool/openapi.json
```

### `POST /tool/preview_app`

Spawn a sandbox and return the iframe HTML directly (not JSON). Same body as `POST /run`.

**Request:**
```bash
curl -X POST http://localhost:8012/tool/preview_app \
  -H 'Content-Type: application/json' \
  -d '{
    "runtime": "static",
    "files": {"index.html": "<h1>hello from tool server</h1>"}
  }'
```

**Response headers:**
```
Content-Type: text/html; charset=utf-8
Content-Disposition: inline
Access-Control-Expose-Headers: Content-Disposition
```

**Response body:**
```html
<iframe src="http://sandbox-proxy/abc123def456/" style="width:100%;height:600px;border:0;border-radius:8px;background:#0e1116" allow="clipboard-read; clipboard-write" loading="lazy"></iframe>
```

**Why `Content-Disposition: inline` matters:** without it, OpenWebUI treats the response as a plain text blob and dumps it into the chat instead of rendering the iframe as a rich-UI artifact. Same story for `Access-Control-Expose-Headers` — some OpenWebUI builds check the header from a cross-origin fetch, and browsers hide it from JS unless the server exposes it explicitly.

**How the URL renders:** the outer OpenWebUI iframe is sandboxed with `allow-scripts allow-downloads` by default. The inner `<iframe src="http://sandbox-proxy/...">` is a nested iframe that loads the actual sandbox output. Because OpenWebUI's outer sandbox defaults to `allowSameOrigin=OFF`, dynamic auto-resizing via the `postMessage` height reporter would require a script inside the sandbox app itself — which we don't control. The iframe uses a fixed `height:600px` as a pragmatic default.

**Errors:** same status codes as `POST /run` (`400` unknown runtime, `429` pool full, `500` spawn failure, `504` readiness timeout). Errors return JSON rather than HTML.

---

## LiteLLM pass-through

The `/v1/sandbox/*` pass-through registered in [`ai/litellm_config.yaml`](../litellm_config.yaml) forwards to this service, so operators can hit these endpoints via LiteLLM with a virtual key:

```bash
curl http://localhost:4001/v1/sandbox/health \
  -H 'Authorization: Bearer sk-your-litellm-key'
```

Same request/response shapes as the direct endpoints — just prefixed with `/v1/sandbox` and gated by LiteLLM auth.

---

## What is NOT implemented

Called out here so operators don't hunt for endpoints that don't exist.

- **No `POST /jobs/{id}/cancel`.** `common.jobs.router` supports it, but the sandbox runner mounts with `include_cancel=False` — teardown goes through `DELETE /jobs/{id}` because there is no cooperative-cancel loop inside a sandbox.
- **No auth on any endpoint.** Access control is enforced at the network level (`sandbox-runner` is only reachable on `ai_shared`) and at LiteLLM (for the pass-through routes).
- **No idle-TTL polling.** The reaper enforces `SANDBOX_HARD_TTL_SECONDS` only; `SANDBOX_DEFAULT_TTL_SECONDS` is recorded on the job row but not acted on. See [SANDBOX.md § Tearing down a stuck sandbox](SANDBOX.md#tearing-down-a-stuck-sandbox).
- **No pagination on `/jobs`.** Fixed limit of 20 in the registry; add a `limit` query param and thread it through `list_all()` if you need more.
