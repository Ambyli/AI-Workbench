# sandbox-runner — HTTP endpoints

Base URL from the host: `http://localhost:8012`
Base URL on the `ai_shared` Docker network: `http://sandbox-runner:8000`

Everything below is served by [`ai/sandbox/runner/app.py`](app.py). Jobs endpoints are contributed by the shared [`build_router`](../../shared/common/src/common/jobs/router.py) factory in `common.jobs`.

## Summary

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness + DB status |
| `POST` | `/run` | Spawn a sandbox — or update an existing one when `session_id` is passed. Runs behavioral tests before returning. JSON response has URL, session_id, `reused` flag, and a `tests` object with the test-run outcome |
| `DELETE` | `/session/{session_id}` | Explicitly tear down a session's running sandbox. Idempotent |
| `GET` | `/session/{session_id}/download` | Stream the running sandbox's `/app` as a tar archive. Follows self-heal (resolves session at request time). Also exposed publicly via Caddy at `/sandboxes/download/{session_id}` |
| `GET` | `/session/{session_id}/logs` | Return the last N lines (default 100, max 1000) of the running sandbox's stdout+stderr. Follows self-heal. Diagnostic for apps that render errors in-browser only (Flask/Vite/Next tracebacks; Streamlit does not use stdout for user errors) |
| `GET` | `/jobs` | List every sandbox in the registry (running + terminal) |
| `GET` | `/jobs/{sandbox_id}` | One sandbox's snapshot |
| `GET` | `/jobs/{sandbox_id}/download` | Direct tar download by internal id. Useful for operators; does not follow self-heal |
| `GET` | `/jobs/{sandbox_id}/logs` | Direct log fetch by internal id. Useful when session self-healed to a new container but you want the OLD one's output |
| `DELETE` | `/jobs/{sandbox_id}` | Tear a sandbox down early by internal id, release its slot |
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

Spawn a new sandbox — or update an existing one — running the caller's files under the given runtime. Returns the URL to iframe.

Two modes, driven by whether `session_id` is present:

- **First call (no `session_id`)** — server generates one, spawns a fresh container, returns `reused: false`.
- **Follow-up call (with `session_id`)** — server finds the running container for that session, overlays the file map onto its `/app` via `docker cp`, returns **the same URL** and `reused: true`. No respawn, no readiness probe. The dev server inside reloads on file change (Streamlit auto-reruns, Vite HMR, nginx serves live). If the session's container has already been reaped, the runner self-heals by respawning under the same `session_id`.

**Body:**
```json
{
  "runtime":     "static",
  "files":       { "index.html": "<h1>hello</h1>" },
  "entrypoint":  null,
  "ttl_seconds": 900,
  "session_id":  null,
  "deletes":     []
}
```

Field semantics:

| Field | Type | Required | Notes |
|---|---|---|---|
| `runtime` | string | yes | One of `static`, `python`, `node`. See [`runtimes.py`](runner/runtimes.py) for the current registry. |
| `files` | `{ path: content }` | yes (see note) | Map of relative filesystem paths → text contents. On the first call: the initial file set. On a follow-up: an overlay — paths listed here overwrite files in the running container, unlisted files are preserved. Absolute paths and `..` are rejected. **At least one entry must be a test file under a top-level `tests/` directory** when `SANDBOX_TESTS_REQUIRED=true` — see [Behavioral tests](#behavioral-tests). |
| `entrypoint` | string | no | Shell command that must bind to port `80` inside the container. Leave `null` to use the runtime's default. Ignored on follow-up calls — the entrypoint is fixed at spawn time. |
| `ttl_seconds` | int | no | Idle lifetime. Server clamps to `SANDBOX_HARD_TTL_SECONDS` (3600). Defaults to `SANDBOX_DEFAULT_TTL_SECONDS` (900). |
| `session_id` | string | no | Persistent handle across turns. Regex `^[A-Za-z0-9_-]{1,64}$`. Omit on first call — the server generates one. Pass the value from the previous response to update in place. |
| `deletes` | `[path, …]` | no | Relative paths under `/app` to remove on a follow-up call. Same sanitization as `files`. Ignored on the first call. Paths under `tests/` remove entries from the persisted test map. |

**Response (200):**
```json
{
  "sandbox_id":     "a1b2c3d4e5f6",
  "session_id":     "bfdYm3_H5SD4",
  "url":            "http://sandbox-proxy/a1b2c3d4e5f6/",
  "expires_at":     "2026-08-27T18:15:32.114513+00:00",
  "reused":         false,
  "runtime":        "static",
  "startup_output": "",
  "tests": {
    "ok":         true,
    "exit_code":  0,
    "output":     "OK: landing page renders expected text\n",
    "runner":     "sh",
    "duration_s": 0.7,
    "timed_out":  false
  }
}
```

The returned `url` is reachable at `http://sandbox-proxy` on the `ai_shared` Docker network (i.e. from OpenWebUI). From the host it's at `http://localhost:8011/{sandbox_id}/`. `reused: true` means the response came from the session-reuse path and the container was NOT respawned. `tests` is `null` only when `SANDBOX_TESTS_REQUIRED=false` AND no `tests/` files were shipped; otherwise the runner always executes the test map and surfaces the outcome here. **A `tests.ok: false` result does NOT prevent the URL from being returned** — soft-fail is intentional so the operator can inspect a partially-broken preview; the model is expected to iterate on the same `session_id` before handing back to the user. See [Behavioral tests](#behavioral-tests).

**Error responses:**

| Status | Meaning |
|---|---|
| `400` | Unknown `runtime` value; **static lint failed on a Python file**; **tests missing** (no file under `tests/` when `SANDBOX_TESTS_REQUIRED=true`) — see below. |
| `429` | Concurrency cap reached (`SANDBOX_MAX_CONCURRENT`, default 8). |
| `500` | Docker spawn error — image missing, cgroup rejection, etc. |
| `504` | Sandbox spawned but readiness probe (`GET /` inside container) didn't reply within 30s. Container is torn down before the response returns; **logs are captured first** (see below). |

**Structured error bodies for feedback loops.** Two failure modes carry diagnostic detail in the response body so the caller (usually a model) can self-correct without a human relay:

Static lint (400) — every `.py` file in `files` is compiled with Python's built-in `compile()` before the runner touches Docker. SyntaxError catches trigger a 400 with:
```json
{
  "detail": {
    "error": "static lint failed",
    "session_id": "wVLFnur35Okv",
    "errors": [
      {"path": "app.py", "line": 3, "offset": 1,
       "message": "SyntaxError: invalid syntax",
       "text": "def foo("}
    ],
    "hint": "Fix the syntax errors above and call preview_app again with the same session_id. No container was spawned."
  }
}
```

Readiness failure (504) — container spawned but didn't bind port 80 within 30 s. Before teardown, the runner reads the tail of `/tmp/sandbox.log`:
```json
{
  "detail": {
    "error": "sandbox did not become ready within 30s",
    "session_id": "79FDwxzMkgr4",
    "logs": "Traceback (most recent call last):\n  File \"/app/app.py\"…\nModuleNotFoundError: No module named 'missing_pkg'\n",
    "hint": "Read the container logs above… Fix the code and call preview_app again with the same session_id — the runner will spawn a fresh container."
  }
}
```

Tests missing (400) — the request did not include any file under a top-level `tests/` directory AND the deployment has `SANDBOX_TESTS_REQUIRED=true`:
```json
{
  "detail": {
    "error": "tests missing",
    "session_id": "9m3lLXq2r0Pt",
    "runtime": "python",
    "hint": "This deployment requires model-supplied behavioral tests. Add at least one file under the top-level `tests/` directory that exercises the running preview (navigate, click, assert visible text/data)…",
    "example": {
      "tests/test_ui.py": "import os\nfrom playwright.sync_api import sync_playwright, expect\n…"
    }
  }
}
```

All three include a `session_id` so a retry with the same id transparently self-heals via the existing session-reuse path.

### Behavioral tests

Every call must ship at least one file under a top-level `tests/` directory (soft-disabled when `SANDBOX_TESTS_REQUIRED=false`). The runner:

1. Splits the `files` map: entries under `tests/` are stashed in the job's metadata; everything else goes into the sandbox's `/app`.
2. Spawns the sandbox as usual and awaits readiness.
3. Spawns an ephemeral `sandbox-tester-{id}` companion on `sandbox_net` from the `sandbox-tester:latest` image (pytest + jest + Playwright + chromium + curl + jq pre-baked). Puts the test files into the tester's `/tests` and execs the runtime's `test_command` with `PREVIEW_URL=http://sandbox-{id}:80/` in the environment.
4. Waits up to `SANDBOX_TEST_TIMEOUT_SECONDS` (default 60), tears the tester down (win, fail, or timeout — always), and inlines the result on the response `tests` field.

The tester container gets the same security posture as sandboxes: `sandbox_net` only, cap-drop, non-root 1000:1000, no `docker.sock`. See [`ai/sandbox/SANDBOX.md § Behavioral tests`](SANDBOX.md#behavioral-tests) for the full walkthrough.

On follow-up calls with the same `session_id`, prior tests persist. The delta rule applies to test files too: send only the ones that changed; use `deletes` with a `tests/…` path to remove a test that no longer belongs.

**Curl examples:**

Static HTML (with a curl-based test):
```bash
curl -X POST http://localhost:8012/run \
  -H 'Content-Type: application/json' \
  -d '{
    "runtime": "static",
    "files": {
      "index.html": "<h1>hello sandbox</h1>",
      "tests/run.sh": "#!/bin/sh\ncurl -sf \"$PREVIEW_URL\" | grep -q \"hello sandbox\"\n"
    }
  }'
```

Streamlit app (Python runtime) with a Playwright test:
```bash
curl -X POST http://localhost:8012/run \
  -H 'Content-Type: application/json' \
  -d '{
    "runtime": "python",
    "files": {
      "app.py": "import streamlit as st\nst.title(\"demo\")\nst.slider(\"n\", 0, 100)",
      "tests/test_ui.py": "import os\nfrom playwright.sync_api import sync_playwright, expect\n\ndef test_title():\n    with sync_playwright() as p:\n        b = p.chromium.launch()\n        page = b.new_page()\n        page.goto(os.environ[\"PREVIEW_URL\"])\n        expect(page.get_by_text(\"demo\")).to_be_visible(timeout=15000)\n        b.close()\n"
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

Follow-up update in the same session (paste the `session_id` from the first response):
```bash
curl -X POST http://localhost:8012/run \
  -H 'Content-Type: application/json' \
  -d '{
    "runtime": "static",
    "session_id": "bfdYm3_H5SD4",
    "files": {"index.html": "<h1>updated</h1>"}
  }'
# Response: same sandbox_id, same url, "reused": true.
```

Follow-up with a file delete:
```bash
curl -X POST http://localhost:8012/run \
  -H 'Content-Type: application/json' \
  -d '{
    "runtime": "static",
    "session_id": "bfdYm3_H5SD4",
    "files": {},
    "deletes": ["old-page.html"]
  }'
```

---

## `DELETE /session/{session_id}`

Explicitly tear down the running sandbox for a session. Idempotent — a session that never existed or has already expired still returns 204 so callers don't have to remember state to clean up.

**Request:**
```bash
curl -X DELETE http://localhost:8012/session/bfdYm3_H5SD4
```

**Response (204):** no body.

**Errors:**

| Status | Meaning |
|---|---|
| `400` | `session_id` doesn't match `^[A-Za-z0-9_-]{1,64}$`. |
| `500` | Runner not initialized (should not normally occur). |

Distinct from `DELETE /jobs/{sandbox_id}` — the jobs endpoint targets a single spawn event by its 12-char internal id, while `/session/{id}` targets whatever's currently running under the persistent session handle (may have been respawned via self-heal since the model last saw it).

---

## `GET /session/{session_id}/download`

Stream the running sandbox's `/app` directory back to the caller as a plain tar archive. The Docker daemon does the packing (via `container.get_archive`) so the runner never buffers the whole archive in memory — chunks pass through as they arrive.

Session-based, so this URL keeps working across self-heal spawns. If the session's original container was reaped between the download URL being minted and the request landing, the endpoint resolves the current running sandbox_id at request time and downloads THAT.

**Request:**
```bash
curl -o my-sandbox.tar http://localhost:8012/session/bfdYm3_H5SD4/download
```

**Response headers:**
```
HTTP/1.1 200 OK
content-type: application/x-tar
content-disposition: attachment; filename="sandbox-a1b2c3d4e5f6.tar"
transfer-encoding: chunked
```

**Response body:** raw tar stream. Extract with `tar -xf sandbox-*.tar` — the archive root is `app/`, so files land at `./app/index.html`, etc.

**Errors:**

| Status | Meaning |
|---|---|
| `400` | `session_id` doesn't match `^[A-Za-z0-9_-]{1,64}$`. |
| `404` | No running sandbox for that session — either it never existed, or it's been reaped and hasn't self-healed. |

**Public URL for end users.** The Caddy proxy exposes this at `${SANDBOX_PROXY_URL}/download/{session_id}` (e.g. `https://chat.zeoenergy.com/sandboxes/download/bfdYm3_H5SD4`) via a dedicated route in `ai/sandbox/proxies/Caddyfile`. The browser gets the same oauth2-proxy auth flow as the preview iframe — no separate credentials, no CORS surprise. `preview_app` includes this URL as a `Download source:` line in its response so the model can share it whenever a user asks to save the code.

---

## `GET /session/{session_id}/logs`

Return the last `lines` lines of the running sandbox's combined stdout + stderr. Session-based so it follows self-heal spawns.

**How the logs get there.** The runner's two-phase spawn runs the user's command via `docker exec` (detached) rather than as PID 1 (`sleep infinity` is PID 1). `container.logs()` only sees PID 1's streams, so exec output is not captured there. The spawn redirects the user command's stdout + stderr into `/tmp/sandbox.log` inside the container (128 MB tmpfs), and this endpoint runs `tail -n N /tmp/sandbox.log` via a second exec to read them back.

**Which runtimes surface useful output here.** Flask (`app.run()`), FastAPI + uvicorn `--log-level debug`, Express, Vite, Next, and any bare `python app.py` all print tracebacks to stdout — the model gets them via this endpoint. **Streamlit is the exception**: it catches user exceptions and renders them in the browser rather than printing to stdout, so this endpoint shows only the "You can now view your Streamlit app…" banner. For Streamlit apps, the model has to inspect the rendered HTML from the sandbox URL to see error text.

**Query params:**

| Param | Type | Default | Notes |
|---|---|---|---|
| `lines` | int | `100` | Clamped 1..1000. 100 is enough for most tracebacks; bump if you need earlier request history. |

**Request:**
```bash
curl "http://localhost:8012/session/bfdYm3_H5SD4/logs?lines=50"
```

**Response (200):**
```json
{
  "session_id": "bfdYm3_H5SD4",
  "sandbox_id": "a1b2c3d4e5f6",
  "lines_requested": 50,
  "logs": "Traceback (most recent call last):\n  File \"/app/app.py\", line 2, in <module>\n    import missing_pkg\nModuleNotFoundError: No module named 'missing_pkg'\n"
}
```

Empty `logs` string means either the container just spawned and hasn't printed anything yet, or the app writes to a file inside the container rather than stdout. It is NOT a signal that the sandbox is broken.

**Errors:**

| Status | Meaning |
|---|---|
| `400` | `session_id` doesn't match `^[A-Za-z0-9_-]{1,64}$`. |
| `404` | No running sandbox for that session — never existed, or reaped without self-heal. |

---

## `GET /jobs/{sandbox_id}/download`

Direct download by internal sandbox_id — bypasses session resolution. Useful for operators who want to grab the archive from a specific spawn event even if the session self-healed to a different container since. Same tar shape as the session variant.

**Request:**
```bash
curl -o my-sandbox.tar http://localhost:8012/jobs/a1b2c3d4e5f6/download
```

**Errors:**

| Status | Meaning |
|---|---|
| `404` | No sandbox with that id. |
| `409` | Sandbox exists in the registry but never reached the `running` phase (no container yet, or it was reaped). |

---

## `GET /jobs/{sandbox_id}/logs`

Direct log fetch by internal sandbox_id. Useful when the session self-healed to a new container but you want the previous container's output. Same shape as `/session/{id}/logs`.

**Query params:** same `lines` param (default 100, clamped 1..1000).

**Request:**
```bash
curl "http://localhost:8012/jobs/a1b2c3d4e5f6/logs?lines=200"
```

**Response (200):** same JSON shape as the session variant, minus the `session_id` field.

**Errors:**

| Status | Meaning |
|---|---|
| `404` | No sandbox with that id. |
| `409` | Sandbox exists but never reached the `running` phase — no container to read logs from. |

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

### Available tools

Three tools are registered — `list_runtimes` (discovery), `preview_app` (spawn / update), and `get_sandbox_logs` (diagnostic).

**`preview_app`** — spawn a sandbox from the shape the model can produce inline.

Parameters (same field semantics as `POST /run`):

| Param | Type | Required |
|---|---|---|
| `runtime` | string | yes |
| `files` | `{ path: content }` | no (empty overlay on follow-up calls is valid) |
| `entrypoint` | string | no |
| `ttl_seconds` | int | no |
| `session_id` | string | no |
| `deletes` | `[path, …]` | no |

**Failure returns.** When the runner raises a 400 (static lint failed) or 504 (readiness failure with logs), the MCP wrapper catches the HTTPException and formats the detail dict into a compiler-style diagnostic string — the tool returns a normal text response, not an MCP-level error, so the model reads it as tool output and can call `preview_app` again with the same `session_id` to retry.

**`get_sandbox_logs`** — fetch the tail of a running sandbox's stdout+stderr on demand.

Parameters:

| Param | Type | Required | Notes |
|---|---|---|---|
| `session_id` | string | yes | Must match `^[A-Za-z0-9_-]{1,64}$` |
| `lines` | int | no | Default 100, clamped 1..1000 |

Use case: `preview_app` returned a normal "ready" response but the user reports the running app looks broken ("the button doesn't work", "undefined is not a function", a Streamlit error card, etc). Flask / FastAPI / Vite / Next dev servers all print the offending Python traceback / JS stack to stdout before rendering an error in the browser. Fetching the logs lets the model diagnose the bug and re-issue `preview_app` without the user having to copy-paste error text back into the chat.

Do NOT call this after a `preview_app` FAILURE — those already include the container logs in the tool response.

**`list_runtimes`** — inventory of installed runtimes, their pre-baked packages, and example `files` maps. No parameters. Model should call this before `preview_app` if it's unsure which runtime fits the user's request.

**Result — a single string (not a JSON object)**:

```
Preview ready. Sandbox `a1b2c3d4e5f6` at http://sandbox-proxy/a1b2c3d4e5f6/ (expires 2026-08-27T18:15:32.114513+00:00).

```html
<iframe src="http://sandbox-proxy/a1b2c3d4e5f6/" style="width:100%;height:min(85vh, 900px);border:0;border-radius:8px;background:#0e1116" allow="clipboard-read; clipboard-write" loading="lazy"></iframe>
```
```

The string is what MCP delivers to the model as `content[0].text`. When the model relays the tool result to the user, OpenWebUI's `ContentRenderer` picks up the ` ```html ` fenced block and promotes it to the artifacts split-panel (see [Artifacts docs](https://docs.openwebui.com/features/chat-conversations/chat-features/code-execution/artifacts/)) — no additional model prompting required. The block is a self-contained HTML document (`<!doctype html>` + meta-refresh to the sandbox URL); OpenWebUI's srcdoc iframe navigates itself to the sandbox rather than nesting another `<iframe>` inside. See [SANDBOX.md § How the artifact renders](SANDBOX.md#how-the-artifact-renders-alignment-with-the-openwebui-docs) for the design rationale and the one admin toggle (`iframe Sandbox Allow Same Origin`) that matters for interactive apps. The plain-text lines above the block give the model + user useful context if HTML rendering is disabled or the model paraphrases.

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
