# sandbox-runner — HTTP endpoints

Base URL from the host: `http://localhost:8012`
Base URL on the `ai_shared` Docker network: `http://sandbox-runner:8000`

Everything below is served by [`ai/sandbox/runner/app.py`](app.py). Jobs endpoints are contributed by the shared [`build_router`](../../shared/common/src/common/jobs/router.py) factory in `common.jobs`.

## Summary

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness + DB status |
| `POST` | `/run` | Spawn a sandbox — or update an existing one when `session_id` is passed. Backing endpoint for `sandbox.run`. Returns JSON with URL, session_id, `reused`, `app_status`, `recreated` |
| `POST` | `/create` | Reserve an empty warming container of the chosen runtime. Backing endpoint for `sandbox.create` |
| `GET` | `/sessions` | Enumerate live sessions. Backing endpoint for `sandbox.list_sessions` |
| `POST` | `/session/{session_id}/files` | Overlay files into a live sandbox. Backing endpoint for `sandbox.write_files` |
| `GET` | `/session/{session_id}/files` | Read files back from `/app` — dir listing when `paths` is omitted, contents inline otherwise. Backing endpoint for `sandbox.get_files` |
| `POST` | `/session/{session_id}/exec` | Run a non-interactive shell command inside the container. Backing endpoint for `sandbox.exec` |
| `POST` | `/session/{session_id}/patch` | Strict line-range file edits (all-or-nothing). Backing endpoint for `sandbox.patch_files` |
| `DELETE` | `/session/{session_id}` | Tear down a session's running sandbox. Backing endpoint for `sandbox.close`. Idempotent |
| `GET` | `/session/{session_id}/download` | Stream the running sandbox's `/app` as a tar archive. Follows self-heal. Also exposed publicly via Caddy at `/sandboxes/download/{session_id}` |
| `GET` | `/session/{session_id}/logs` | Return the last N lines (default 100, max 1000) of the running sandbox's stdout+stderr. Follows self-heal. Backing endpoint for `sandbox.get_logs` |
| `GET` | `/jobs` | List every sandbox in the registry (running + terminal) |
| `GET` | `/jobs/{sandbox_id}` | One sandbox's snapshot |
| `GET` | `/jobs/{sandbox_id}/download` | Direct tar download by internal id. Does not follow self-heal |
| `GET` | `/jobs/{sandbox_id}/logs` | Direct log fetch by internal id. Useful when session self-healed but you want the OLD container's output |
| `DELETE` | `/jobs/{sandbox_id}` | Tear a sandbox down early by internal id, release its slot |
| `POST` | `/mcp/` | FastMCP JSON-RPC endpoint — see [MCP section](#mcp) |
| `GET` | `/tool/openapi.json` | OpenAPI schema for OpenWebUI's Tool Server discovery |
| `POST` | `/tool/run` | Spawn a sandbox and return an inline-rendered iframe (Content-Disposition: inline). Primary Tool Server endpoint |
| `GET` | `/tool/get_runtime_types` | Describe runtime types (Tool Server variant of `sandbox.get_runtime_types`) |
| `POST` | `/internal/browser-log/{sandbox_id}` | **INTERNAL.** Ingest browser-side console.error / console.warn / window.onerror / unhandledrejection events forwarded by the sandbox-proxy shim. Always 204. See [SANDBOX.md § Browser console capture](SANDBOX.md#browser-console-capture) |

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
  "runtime":            "static",
  "files":              { "index.html": "<h1>hello</h1>" },
  "entrypoint":         null,
  "ttl_seconds":        900,
  "session_id":         null,
  "deletes":            [],
  "env":                null,
  "recreate_if_gone":   true
}
```

Field semantics:

| Field | Type | Required | Notes |
|---|---|---|---|
| `runtime` | string | yes | One of `static`, `python`, `node`. See [`runtimes.py`](runner/runtimes.py) for the current registry. |
| `files` | `{ path: str \| {encoding, content} }` | no | Map of relative paths → contents. Values are either UTF-8 strings, or `{"encoding": "base64", "content": "..."}` for binary payloads (images, PDFs, wheels). On the first call: the initial file set. On a follow-up: an overlay — paths listed here overwrite files in the running container, unlisted files are preserved. Absolute paths and `..` are rejected. Per-file cap: `SANDBOX_MAX_FILE_BYTES`; total cap: `SANDBOX_MAX_PAYLOAD_BYTES`. |
| `entrypoint` | string | no | Shell command that must bind to port `80`. Leave `null` for the runtime's default. Ignored on follow-up calls — the entrypoint is fixed at spawn time. |
| `ttl_seconds` | int | no | Idle lifetime. Server clamps to `SANDBOX_HARD_TTL_SECONDS` (3600). Defaults to `SANDBOX_DEFAULT_TTL_SECONDS` (900). |
| `session_id` | string | no | Persistent handle across turns. Regex `^[A-Za-z0-9_-]{1,64}$`. Omit on first call — the server generates one. |
| `deletes` | `[path, …]` | no | Relative paths under `/app` to remove. Same sanitization as `files`. Ignored on the first call. |
| `env` | `{str: str}` | no | Process env vars set inside the container. Immutable after spawn (self-heal replays the recorded env). Reserved keys (`HTTP_PROXY`, `PYTHONUNBUFFERED`, `TERM`, etc.) are rejected. |
| `recreate_if_gone` | bool | no | Default `true` for backward compat with `POST /run`. If the session's container is gone, silently respawn. Set `false` to force the caller to reason about self-heal explicitly (recommended for direct `write_files` calls, but `/run` keeps the historical default). |

**Response (200):**
```json
{
  "sandbox_id": "a1b2c3d4e5f6",
  "session_id": "bfdYm3_H5SD4",
  "url":        "http://sandbox-proxy/a1b2c3d4e5f6/",
  "expires_at": "2026-08-27T18:15:32.114513+00:00",
  "reused":     false,
  "runtime":    "static",
  "startup_output": "",
  "app_status": { "code": 200, "latency_ms": 0, "note": "readiness ok" },
  "recreated":  false
}
```

`app_status` on the reuse branch is a live HTTP probe (single-shot GET against the runtime's readiness path, 3 s deadline) — `{ code, latency_ms }` for a real HTTP reply or `{ error }` for a network failure. On a fresh spawn it's a placeholder acknowledging the readiness_ok gate already passed. `recreated: true` means the session was found dead and respawned in place (only possible when `recreate_if_gone: true`).

The returned `url` is reachable at `http://sandbox-proxy` on the `ai_shared` Docker network (i.e. from OpenWebUI). From the host it's at `http://localhost:8011/{sandbox_id}/`. `reused: true` means the response came from the session-reuse path and the container was NOT respawned.

**Error responses:**

| Status | Meaning |
|---|---|
| `400` | Unknown `runtime` value, OR **static lint failed on a Python file** (see below). |
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
    "hint": "Fix the syntax errors above and call run again with the same session_id. No container was spawned."
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
    "hint": "Read the container logs above… Fix the code and call run again with the same session_id — the runner will spawn a fresh container."
  }
}
```

Both include a `session_id` so a retry with the same id transparently self-heals via the existing session-reuse path.

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

## `POST /create`

Reserve an empty warming container of the chosen runtime. Backs the `sandbox.create` MCP tool. Use this when a caller wants to warm a container while it's still composing the code — a subsequent `POST /session/{id}/files` hits a warm container and hot-reloads instantly.

**Body:**
```json
{
  "runtime":     "python",
  "ttl_seconds": 900,
  "entrypoint":  null,
  "env":         { "OPENAI_API_KEY": "sk-..." }
}
```

**Response (200):** same shape as `POST /run`. `startup_output` may already carry a Streamlit "You can now view your Streamlit app…" line because the warming file has been running long enough for the dev server to bind port 80.

**Errors:** same status codes as `POST /run` (400 unknown runtime / custom entrypoint on static, 429 pool full, 504 warming container didn't bind port 80).

---

## `GET /sessions`

Enumerate every currently-running sandbox. Backs `sandbox.list_sessions`.

**Response (200):**
```json
{
  "count": 2,
  "sessions": [
    {
      "session_id": "bfdYm3_H5SD4",
      "sandbox_id": "a1b2c3d4e5f6",
      "runtime":    "python",
      "url":        "https://chat.zeoenergy.com/sandboxes/a1b2c3d4e5f6/",
      "created_at": "...",
      "updated_at": "...",
      "expires_at": "...",
      "last_used_at": "...",
      "phase":      "running"
    }
  ]
}
```

Returns EVERY live session globally — no per-user filtering. Documented explicitly so callers don't assume isolation.

---

## `POST /session/{session_id}/files`

Overlay files into a live sandbox. Backs `sandbox.write_files`.

**Body:**
```json
{
  "files":            { "app.py": "import streamlit as st\nst.title('v2')" },
  "deletes":          ["stale-page.html"],
  "recreate_if_gone": false
}
```

Field semantics:

| Field | Type | Required | Notes |
|---|---|---|---|
| `files` | `{ path: str \| {encoding, content} }` | no | Same shape as `POST /run.files`. Absent paths preserved. |
| `deletes` | `[path, …]` | no | Relative paths under `/app` to remove. |
| `recreate_if_gone` | bool | no | Default **false** (opt-in self-heal). If true and the container is gone, respawn under the same session_id and reapply the recorded env — files installed via `exec` and non-listed in-container files are LOST. |

**Response (200):** same shape as `POST /run` — `sandbox_id`, `session_id`, `url` (unchanged if the container was alive), `reused: true`, `app_status` from the health probe, `recreated` set true if self-heal ran.

**Errors:**

| Status | Meaning |
|---|---|
| `400` | Static lint failed, invalid session_id, or unsafe path in `files`/`deletes`. |
| `404` | `session_id` never existed. |
| `409` | Container is gone AND `recreate_if_gone` was false. Body carries an `error`, `session_id`, and remediation hint. |
| `413` | Payload exceeded `SANDBOX_MAX_FILE_BYTES` / `SANDBOX_MAX_PAYLOAD_BYTES`. |

---

## `GET /session/{session_id}/files`

Read files back from the running sandbox's `/app`. Backs `sandbox.get_files`.

**Query params:**

| Param | Type | Default | Notes |
|---|---|---|---|
| `paths` | string | (unset) | Comma-separated relative paths under `/app`. Omit for a directory listing (paths + sizes only). |
| `max_bytes_per_file` | int | `8192` | Truncate each returned file to this many bytes. Hard cap 65536. |

**Response (200):**
```json
{
  "session_id": "bfdYm3_H5SD4",
  "sandbox_id": "a1b2c3d4e5f6",
  "files": [
    {"path": "app.py", "size": 1247, "encoding": "utf-8",
     "content": "import streamlit as st\n...", "truncated": false},
    {"path": "assets/logo.png", "size": 12000, "encoding": "base64",
     "content": "iVBOR...", "truncated": false}
  ]
}
```

Binary content is base64-encoded — same shape you can round-trip into `POST /session/{id}/files`. Missing paths get an `error: "not found"` entry rather than raising.

**Errors:** 400 on invalid session_id, 404 on no running sandbox.

---

## `POST /session/{session_id}/exec`

Run a non-interactive shell command inside the running container. Backs `sandbox.exec`.

**Body:**
```json
{
  "command":         "pip install requests",
  "timeout_seconds": 30,
  "working_dir":     "/app"
}
```

Field semantics:

| Field | Type | Required | Notes |
|---|---|---|---|
| `command` | string | yes | Run via `sh -c`. Stdin is closed. |
| `timeout_seconds` | int | no | Default 30, hard cap 120. On timeout the exec is left running; the response is marked `timed_out`. |
| `working_dir` | string | no | Default `/app`. Must be under `/app` — absolute paths outside and `..` traversal are rejected. |

**Response (200):**
```json
{
  "session_id":  "bfdYm3_H5SD4",
  "sandbox_id":  "a1b2c3d4e5f6",
  "command":     "pip install requests",
  "exit_code":   0,
  "duration_ms": 2417,
  "output":      "Collecting requests\n...\nSuccessfully installed requests-2.32.3\n",
  "truncated":   false,
  "timed_out":   false
}
```

Output is capped at 8 KB; when truncated, the last 8 KB are returned and `truncated: true` is set. See [`SANDBOX.md § Runtime introspection (exec)`](SANDBOX.md#runtime-introspection-exec) for the state-drift, allowlist, and interactivity caveats.

**Errors:** 400 on invalid session_id, empty command, or unsafe `working_dir`; 404 on no running sandbox.

---

## `POST /session/{session_id}/patch`

Strict line-range file edits inside a running sandbox. All-or-nothing across the batch — every patch validates in a dry-run pass first, only then are any files modified. Backs `sandbox.patch_files`.

**Body:**
```json
{
  "patches": [
    {
      "path":        "app.py",
      "start_line":  15,
      "end_line":    18,
      "expected":    "def foo(x):\n    return x + 1",
      "replacement": "def foo(x, y):\n    return x + y",
      "note":        "add y argument"
    }
  ],
  "recreate_if_gone": false
}
```

Field semantics:

| Field | Type | Required | Notes |
|---|---|---|---|
| `patches` | list of PatchSpec | yes | At least one patch required. |
| `patches[].path` | string | yes | Relative to `/app`. Absolute paths and `..` traversal rejected. |
| `patches[].start_line` | int | yes | 1-indexed inclusive. |
| `patches[].end_line` | int | yes | 1-indexed inclusive. Must satisfy `start_line <= end_line`. |
| `patches[].expected` | string | yes | EXACT current content of lines `[start_line, end_line]` joined with `\n`. Byte-for-byte match required. |
| `patches[].replacement` | string | yes | What to write in place of `expected`. |
| `patches[].note` | string | no | Free-text; logged for operator debugging, not applied. |
| `recreate_if_gone` | bool | no | Accepted for interface consistency but has NO effect. A dead container always returns 409 regardless — see behavior notes. |

**Payload limits:** each patch's `expected` + `replacement` UTF-8 bytes count toward `SANDBOX_MAX_FILE_BYTES` (default 1,000,000) individually, and the sum across all patches counts toward `SANDBOX_MAX_PAYLOAD_BYTES` (default 10,000,000). Rejected at pydantic validation before the runner touches the container.

**Response (200):**
```json
{
  "sandbox_id":     "a1b2c3d4e5f6",
  "session_id":    "bfdYm3_H5SD4",
  "url":           "https://chat.zeoenergy.com/sandboxes/a1b2c3d4e5f6/",
  "expires_at":    "2026-09-03T20:15:00+00:00",
  "runtime":       "python",
  "hunks_applied": [
    {"path": "app.py", "start_line": 15, "end_line": 18,
     "replaced_bytes": 27, "new_bytes": 41}
  ],
  "files_touched": ["app.py"],
  "startup_output": "",
  "app_status":    {"code": 200, "latency_ms": 47},
  "recreated":     false
}
```

Behavior:

- **Dry-run validation.** For each patch: path safety → file exists (else `missing_file` 409) → target must be UTF-8 (else `binary_file` 409) → line range in bounds (else `out_of_range` 409) → `expected` byte-for-byte matches `\n`-join(current_lines[start_line-1:end_line]) (else `content_mismatch` 409).
- **Overlap check.** Two patches on the SAME `path` whose `[start_line, end_line]` ranges intersect cause the entire call to be rejected with `overlap` 409. Boundary-touching ranges count as overlapping (e.g. `[10-15]` and `[15-20]`).
- **Apply pass** only runs if every patch passes. Per-file bottom-up so earlier-line indices stay valid; each file is written in a single `put_archive` so a mid-batch failure doesn't half-write.
- **Trailing newline preserved.** If the file ended with `\n` before, it does after.
- **`last_used_at` bumped** only on successful apply.
- **Post-write health probe** (same as `POST /session/{id}/files`): 500 ms settle → single HTTP probe against the runtime's `readiness_probe_path` on port 80 → result inline in `app_status`.
- **No self-heal.** `recreate_if_gone` is accepted but ignored — a fresh container has no files to anchor on, so a dead container always returns 409 with a hint to call `write_files` first.

**Structured 409 shape (`content_mismatch`):**
```json
{
  "detail": {
    "error":         "expected content mismatch",
    "session_id":    "bfdYm3_H5SD4",
    "kind":          "content_mismatch",
    "path":          "app.py",
    "patch_index":   1,
    "start_line":    15,
    "end_line":      18,
    "expected":      "...caller-supplied text...",
    "actual":        "...current file content of those lines...",
    "message":       "Expected content at app.py:15-18 did not match the current file.",
    "hint":          "The file has changed since you last read it. Call get_files with the affected path, copy the current bytes into `expected`, and reissue patch_files. No files were modified."
  }
}
```

Other `kind` values: `out_of_range` (adds `file_line_count`), `overlap` (adds `other_start_line`/`other_end_line`/`other_patch_index`), `missing_file`, `binary_file`, `unsafe_path`, `bad_expected_type`, `bad_replacement_type`. Each carries a `hint` telling the caller the exact next action.

**Errors:** 400 on invalid session_id or empty `patches`; 404 on session not found; 409 on any dry-run failure or dead container; 413 on payload cap exceeded.

See [`SANDBOX.md § Targeted edits (patch_files)`](SANDBOX.md#targeted-edits-patch_files) for the "when to use vs write_files" flow and worked examples.

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

**Public URL for end users.** The Caddy proxy exposes this at `${SANDBOX_PROXY_URL}/download/{session_id}` (e.g. `https://chat.zeoenergy.com/sandboxes/download/bfdYm3_H5SD4`) via a dedicated route in `ai/sandbox/proxies/Caddyfile`. The browser gets the same oauth2-proxy auth flow as the preview iframe — no separate credentials, no CORS surprise. `run` includes this URL as a `Download source:` line in its response so the model can share it whenever a user asks to save the code.

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

## `POST /internal/browser-log/{sandbox_id}`

**INTERNAL — not called by end users or by any MCP tool.** Called only by the shim [`ai/sandbox/proxies/browser_shim.js`](proxies/browser_shim.js) via `sandbox-proxy`. See [SANDBOX.md § Browser console capture](SANDBOX.md#browser-console-capture) for the end-to-end story.

**Path parameter:** `sandbox_id` must match `^[a-f0-9]{12}$` — same shape sandbox-proxy's routing enforces. A mismatch returns 204 silently so an attacker probing at the runner can't distinguish "wrong id shape" from "no such sandbox."

**Body:**

```json
{
  "entries": [
    {
      "level": "error",
      "ts": 1735689600123,
      "message": "TypeError: Cannot read property 'foo' of undefined",
      "source": "app.js",
      "line": 42,
      "col": 8,
      "stack": "TypeError: Cannot read property 'foo' of undefined\n    at bar (app.js:42:8)\n    at ..."
    }
  ]
}
```

Fields:

| Field | Required | Notes |
|---|---|---|
| `level` | yes | One of `error`, `warn`, `log`, `info`, `debug`. Anything else is dropped. |
| `ts` | yes | Epoch milliseconds. Non-numeric values are replaced with the runner's own clock. |
| `message` | yes | Free-form string. Multi-line messages are folded onto one line; use `stack` for the full trace. |
| `source` | no | Script URL or filename (from `window.onerror`). |
| `line`, `col` | no | Location within `source`. Rendered as `(at source:line:col)` on the log line. |
| `stack` | no | Multi-line stack trace. Rendered indented under the log line. |

**Response:** always `204 No Content`. The browser is not going to retry regardless of outcome; surfacing failures would only add network-tab noise for end users.

**Body cap:** requests larger than `BROWSER_LOG_MAX_BODY_BYTES` (default 64 KiB) return `413 Payload Too Large` before JSON parsing.

**Rate limit:** `BROWSER_LOG_RATE_LIMIT_PER_MIN` (default 100) per sandbox_id per rolling 60-second window. Overage is dropped and collapsed into ONE synthetic `[browser rate-limited: N events dropped]` line so the agent can see events are being lost.

**Ingest behavior:** validated entries are formatted as `[browser] {ISO8601-ts} {level}: {message}` and appended to the sandbox container's `/tmp/sandbox.log` via one `docker exec` per POST. From there they surface through the existing `get_logs` tool — no separate MCP tool. Ingest is fire-and-forget: the endpoint returns 204 before the exec completes, so the browser sees a fast response even when the docker socket is momentarily busy.

**Postman.** Included under `POST /internal/browser-log/{sandboxId}` for operators debugging the ingest path directly — the shim inside a sandbox app is the normal caller.

---

## MCP

FastMCP HTTP transport, mounted at `/mcp` (with a trailing-slash redirect from `/mcp` → `/mcp/`). This is the endpoint that LiteLLM's `mcp_servers.sandbox` entry points at.

Base URL from the host: `http://localhost:8012/mcp/`
Base URL on `ai_shared`: `http://sandbox-runner:8000/mcp/`

All requests are JSON-RPC 2.0 over HTTP POST. Response bodies come back as either JSON or Server-Sent Events (`Content-Type: text/event-stream`). Clients must send `Accept: application/json, text/event-stream`.

### Available tools

Eleven tools registered on the `sandbox` MCP server. Every tool returns a `ToolResult` with a `TextContent` block (what the model reads) plus a `structured_content` payload (machine-readable JSON — see the field breakdowns per tool).

| Tool | Backing HTTP endpoint | Purpose |
|---|---|---|
| `get_runtime_types` | (in-process; no HTTP backing) | Describe runtime types (catalog — not a session status check) |
| `create` | `POST /create` | Reserve an empty warming container |
| `write_files` | `POST /session/{id}/files` | Overlay files, run health probe |
| `get_files` | `GET /session/{id}/files` | Read files back from `/app` |
| `get_logs` | `GET /session/{id}/logs` | Tail combined stdout+stderr |
| `exec` | `POST /session/{id}/exec` | Run a non-interactive shell command |
| `patch_files` | `POST /session/{id}/patch` | Strict line-range file edits (all-or-nothing) |
| `preview` | (in-process; reads from registry only) | Return the iframe artifact HTML |
| `close` | `DELETE /session/{id}` | Teardown and slot release |
| `list_sessions` | `GET /sessions` | Enumerate live sandboxes |
| `run` | `POST /run` (with `recreate_if_gone=true`) | One-shot: create + update + preview |

**Failure returns.** When the runner raises a 400 (static lint failed), 404 (session not found), 409 (container gone without `recreate_if_gone`), 504 (readiness failure with logs), or similar, the MCP wrapper catches the HTTPException and formats the detail dict into a diagnostic string. The tool returns a normal text response, not an MCP-level error, so the model reads it as tool output and can call the tool again with the same `session_id` to retry.

**Text-shape stability.** `run` and `preview` include a `Session id: <sid>` line and a fenced ```` ```html ```` block — both are load-bearing. Models are instructed to relay the response verbatim so OpenWebUI's `ContentRenderer` promotes the block into the artifacts panel. The `Session id:` line is what the model greps back on the next turn.

**Structured payload highlights:**

- `create` / `run` → `{ok, session_id, sandbox_id, url, expires_at, runtime, app_status, reused, recreated}`
- `write_files` → adds `startup_output`, `app_status`, `recreated` (self-heal flag)
- `get_files` → `{ok, session_id, sandbox_id, files: [{path, size, encoding, content, truncated, error?}, …]}`
- `get_logs` → `{ok, session_id, sandbox_id, lines_requested, logs, empty?}`
- `exec` → `{ok, session_id, sandbox_id, command, exit_code, duration_ms, output, truncated, timed_out}`
- `patch_files` → `{ok, session_id, sandbox_id, url, expires_at, runtime, hunks_applied: [{path, start_line, end_line, replaced_bytes, new_bytes}], files_touched, startup_output, app_status, recreated: false}` — on failure the payload carries `{ok: false, status: 409, error, session_id, detail: {kind, path, patch_index, start_line?, end_line?, expected?, actual?, file_line_count?, other_start_line?, other_end_line?, other_patch_index?, message, hint}}`
- `preview` → `{ok, session_id, sandbox_id, url, iframe_html, download_url}`
- `close` → `{ok, session_id, sandbox_id?, was_running}`
- `list_sessions` → `{ok, count, sessions: [...]}`

**Result — a single string (not a JSON object)**:

```
Preview ready. Sandbox `a1b2c3d4e5f6` at http://sandbox-proxy/a1b2c3d4e5f6/ (expires 2026-08-27T18:15:32.114513+00:00).

```html
<iframe src="http://sandbox-proxy/a1b2c3d4e5f6/" style="width:100%;height:min(85vh, 900px);border:0;border-radius:8px;background:#0e1116" allow="clipboard-read; clipboard-write" loading="lazy"></iframe>
```
```

The string is what MCP delivers to the model as `content[0].text`. When the model relays the tool result to the user, OpenWebUI's `ContentRenderer` picks up the ` ```html ` fenced block and promotes it to the artifacts split-panel (see [Artifacts docs](https://docs.openwebui.com/features/chat-conversations/chat-features/code-execution/artifacts/)) — no additional model prompting required. The block is a self-contained HTML document (`<!doctype html>` + meta-refresh to the sandbox URL); OpenWebUI's srcdoc iframe navigates itself to the sandbox rather than nesting another `<iframe>` inside. See [SANDBOX.md § How the artifact renders](SANDBOX.md#how-the-artifact-renders-alignment-with-the-openwebui-docs) for the design rationale and the one admin toggle (`iframe Sandbox Allow Same Origin`) that matters for interactive apps. The plain-text lines above the block give the model + user useful context if HTML rendering is disabled or the model paraphrases.

**Important:** the tool's docstring instructs the model to include the returned string VERBATIM in its response. If the model paraphrases or drops the ` ```html ` block, the preview won't render. If you find this happens often, consider using the [`/tool/run` REST endpoint](#openwebui-tool-server-tool) instead — that path bypasses the model and lets OpenWebUI render the iframe directly.

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

**`tools/call`** — invoke `run` (the one-shot).
```bash
curl -X POST http://localhost:8012/mcp/ \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{
    "jsonrpc": "2.0",
    "id": 3,
    "method": "tools/call",
    "params": {
      "name": "run",
      "arguments": {
        "runtime": "static",
        "files": {"index.html": "<h1>from mcp</h1>"}
      }
    }
  }'
```

Or the pipelined pattern — warm the container first, then send code:
```bash
# 1. Reserve a warming Python container
curl -X POST http://localhost:8012/mcp/ ... \
  -d '{... "method": "tools/call",
        "params": {"name": "create",
                   "arguments": {"runtime": "python"}}}'
# → returns session_id X

# 2. Overlay code once you've finished writing it (opt-in self-heal off)
curl -X POST http://localhost:8012/mcp/ ... \
  -d '{... "method": "tools/call",
        "params": {"name": "write_files",
                   "arguments": {"session_id": "X",
                                 "files": {"app.py": "import streamlit as st..."}}}}'

# 3. Show the user
curl -X POST http://localhost:8012/mcp/ ... \
  -d '{... "method": "tools/call",
        "params": {"name": "preview",
                   "arguments": {"session_id": "X"}}}'
```

Session semantics: the first `initialize` returns a session id in the response headers (`Mcp-Session-Id`). Subsequent calls should include it as a request header. FastMCP handles this automatically for clients that follow the streamable-HTTP spec (LiteLLM, Claude Code, etc.).

---

## OpenWebUI Tool Server (`/tool`)

A separate FastAPI sub-app mounted at `/tool`, purpose-built for OpenWebUI's **rich UI Tool Server** integration (docs: [Extensibility → Plugin Development → Rich UI](https://docs.openwebui.com/features/extensibility/plugin/development/rich-ui/)).

**Register in OpenWebUI:** Admin Panel → Settings → Tools → Add Connection.
Base URL (on `ai_shared`): `http://sandbox-runner:8000/tool`
Base URL (from the host): `http://localhost:8012/tool`

OpenWebUI fetches `GET /tool/openapi.json` to discover the tool schema, then calls `POST /tool/run` when the model wants to embed a preview. The response has `Content-Disposition: inline` so OpenWebUI renders it as a sandboxed iframe under the tool call indicator.

### `GET /tool/openapi.json`

Auto-generated OpenAPI 3.0 schema. Used by OpenWebUI at Tool-Server registration time.

**Request:**
```bash
curl http://localhost:8012/tool/openapi.json
```

### `POST /tool/run`

Spawn (or update) a sandbox and return the iframe HTML directly (not JSON). Same body as `POST /run`, minus `recreate_if_gone` (implicitly `true` — the Tool Server path is always the one-shot convenience).

**Request:**
```bash
curl -X POST http://localhost:8012/tool/run \
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

**Errors:** same status codes as `POST /run` (`400` unknown runtime, `413` payload too large, `429` pool full, `500` spawn failure, `504` readiness timeout). Errors return JSON rather than HTML.

### `GET /tool/get_runtime_types`

Describe the runtime types (`static`, `python`, `node`, …) this deployment supports. Same JSON payload as the `sandbox.get_runtime_types` MCP tool. This is a catalog, not a session status check — it does not know about any specific sandbox. For a running sandbox's state, use `/session/{id}/logs`, `/session/{id}/files`, or `/sessions`.

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
