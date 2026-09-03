# Claude Usage Observer

Windows system tray app that monitors Claude Code token usage. Reads local `~/.claude/projects/**/*.jsonl` logs and optionally scrapes account stats from claude.ai via Chrome DevTools Protocol (CDP).

## Commands

```bash
# Install deps
pip install -e .
# or
uv pip install -e .

# Run
python -m claude_observer
# or after install:
claude-usage-observer

# Debug CDP captures (requires Chrome on --remote-debugging-port=9222)
python -m claude_observer.browser.cdp_spy
```

## Configuration

All config lives in `config.json` (project root). Edit directly or via the Settings window (tray right-click → **Settings…**). Changes apply immediately via `config.apply_updates()`.

Key variables:

| Key | Default | Notes |
|---|---|---|
| `DEBUG_LOGGING` | `false` | Verbose CDP + widget logging |
| `REFRESH_INTERVAL_SECONDS` | `300` | Seconds between local token-stat refreshes |
| `CONSOLE_FETCHER_ENABLED` | `false` | Enable claude.ai account stats scraping |
| `BROWSER_DEBUG_PORT` | `9222` | Chrome remote-debugging port |
| `EXCLUDE_WEEKDAYS` | `"5,6"` | Days excluded from rolling averages (0=Mon) |
| `INCLUDE_PATHS` | _(empty)_ | Filter projects by path prefix |
| `LLM_URL` | `http://localhost:8001` | Local llama-server URL |
| `LLM_API_KEY` | `sk-no-key-required` | API key sent to local server |
| `LLM_MODEL` | _(empty)_ | Model alias passed to Claude Code |
| `LLM_LOG_MAX_LINES` | `200` | Max lines in server-output log box |
| `LLAMA_SERVER_CMD` | _(empty)_ | Full shell command to launch llama-server |
| `AUDIO_BASE_URL` | `http://localhost:8004` | Base URL returned by Kokoro `text_to_speech` MCP tool |
| `MADLAD_APP_URL` | `http://madlad-app:8085` | URL the MADLAD proxy uses to reach the inference container |
| `MADLAD_MODEL` | `SoybeanMilk/madlad400-3b-mt-ct2-int8_float16` | HuggingFace repo ID for the pre-converted CTranslate2 MADLAD checkpoint |
| `DRY_RUN` | `true` | Roofix Bridge: log decisions but skip Phoenix writes |
| `AGENT_PHASE` | `0` | Roofix Bridge: `0` = chatter+milestones only; `1` = +create/notify |
| `TICK_INTERVAL_SECONDS` | `300` | Roofix Bridge: APScheduler cadence |
| `BRAIN_MODEL` | `qwen3.6` | Roofix Bridge: LiteLLM alias for the AI-fallback brain |
| `ROOFIX_SENDER` | `no-reply@roofix.io` | Roofix Bridge: Gmail search-query sender (two `o`s) |
| `ROOFIX_PROCESSED_LABEL` | `roofix/processed` | Roofix Bridge: Gmail label applied to every message the bridge evaluates; excluded server-side from `LISTENER_QUERY` so already-processed emails don't fill the 25-message fetch window. Backfill existing rows with `POST /labels/backfill`. |
| `ESCALATION_RECIPIENTS` | _(empty)_ | Roofix Bridge: comma-separated recipients that receive forwarded escalations. Empty disables forwarding — escalates stay unread in Gmail for direct operator review. |
| `GMAIL_CREDENTIALS_PATH` | `/config/credentials.json` | Roofix Bridge: OAuth client-secrets file |
| `GMAIL_TOKEN_PATH` | `/config/token.json` | Roofix Bridge: OAuth refresh-token file |
| `PHOENIX_DB_HOST` / `_PORT` / `_NAME` / `_USER` / `_PASSWORD` / `_SSLMODE` | _(secrets)_ | Roofix Bridge: direct psycopg2 connection to Phoenix Postgres |
| `ROOFIX_DB_USER` / `_PASSWORD` / `_NAME` | `roofix` / `roofix` / `roofix` | Roofix Bridge: credentials for the compose-managed `roofix-db` Postgres backing `ProcessedStore`. All three fall back to `roofix` in both `docker-compose.roofix.yml` and `ai/roofix/app.py`'s DSN. Override the password for anything past dev — the DB only lives on `ai_shared` today, but don't ship the default if you expose it. |
| `PORT_ROOFIX_DB` | `5433` | Roofix Bridge: host port `roofix-db` binds to for remote connections. Lives in the `PORT REGISTRY` block at the top of `.env`. Both Postgres ports (`PORT_POSTGRES=5432` for litellm_db, `PORT_ROOFIX_DB=5433` for roofix-db) are grouped there. |
| `PHOENIX_AGENT_USER_ID` | _(unset — required for writes)_ | Roofix Bridge: dedicated Phoenix user id |
| `PHOENIX_ROOFIX_ID_COLUMN` | `migration_external_id` | Roofix Bridge: column where Roofix ids are stamped |
| `INTERCEPTOR_URL` | `http://interceptor:8080` | Roofix Bridge → interceptor base URL |
| `ROOFIX_PROFILE_NAME` | `roofix` | Named profile inside interceptor holding Roofix session cookies |
| `INTERCEPTOR_PROFILES_ROOT` | `/data/profiles` | interceptor: root under which named `--user-data-dir` profiles live |
| `INTERCEPTOR_MAX_CONCURRENT` | `8` | interceptor: max simultaneous `/capture` calls (port pool size). Each slot ≈ one Chrome + optional profile clone — see `ai/interceptor/INTERCEPTOR.md § Resource sizing` |
| `SANDBOX_MAX_CONCURRENT` | `8` | Sandbox: max simultaneous running sandboxes (`sandbox-runner` returns 429 past this). Each slot ≈ 512 MB RAM + 1 CPU + base-image disk footprint. |
| `SANDBOX_DEFAULT_TTL_SECONDS` | `900` | Sandbox: default idle TTL. Model can request shorter per-`preview_app`, cannot request longer than `SANDBOX_HARD_TTL_SECONDS`. |
| `SANDBOX_IDLE_TTL_SECONDS` | _(inherits `SANDBOX_DEFAULT_TTL_SECONDS`)_ | Sandbox: reaper tears down a running sandbox whose `metadata->>'last_used_at'` is older than this. Distinct knob only when you want idle behavior to differ from the per-session default. |
| `SANDBOX_HARD_TTL_SECONDS` | `3600` | Sandbox: absolute cap on sandbox lifetime. Reaper (`ai/sandbox/runner/reaper.py`) sweeps expired containers every 60s. |
| `SANDBOX_TESTS_REQUIRED` | `true` | Sandbox: mandatory-tests gate. When `true`, `POST /run` + `preview_app` return **400** if the caller didn't ship at least one file under `tests/`. When `false`, tests that ARE supplied still run — this knob only controls the gate. See `ai/sandbox/SANDBOX.md § Behavioral tests`. |
| `SANDBOX_TEST_TIMEOUT_SECONDS` | `60` | Sandbox: wall-clock deadline for the `sandbox-tester` companion. On timeout the tester is force-removed and the response is marked "⚠ TIMED OUT" — the preview URL is still returned (soft-fail). Playwright cold-starts eat ~10-15s of this. |
| `SANDBOX_EGRESS_ALLOWLIST` | _(empty)_ | Sandbox: additive to `ai/sandbox/proxies/tinyproxy.filter`. Prefer editing the filter file (source of truth); use this only for per-deployment tweaks. |
| `SANDBOX_DB_USER` / `_PASSWORD` / `_NAME` | `sandbox` / `sandbox` / `sandbox` | Sandbox: credentials for the compose-managed `sandbox-db` Postgres backing `PostgresRegistry`. Same dev-default guidance as `ROOFIX_DB_*` — override before exposing anything past `sandbox_state`. |
| `PORT_SANDBOX_RUNNER` / `_PROXY` / `_DB` | `8012` / `8011` / `5434` | Sandbox: host ports. Runner is FastAPI + MCP; proxy is Caddy serving `/{sandbox_id}/*`; db is a Postgres exposed for operator inspection. Postgres ports are grouped: `5432` (litellm_db), `5433` (roofix-db), `5434` (sandbox-db). |
| `SANDBOX_PROXY_URL` | `https://chat.zeoenergy.com/sandboxes` | Sandbox: public URL prefix the runner returns as the iframe `src` for previews (via `POST /run`, `/tool/preview_app`, and the `preview_app` MCP tool). This deployment routes sandbox traffic through the same origin as Open WebUI — `oauth2-proxy` has `http://sandbox-proxy:80/sandboxes/` in `OAUTH2_PROXY_UPSTREAMS`, so `chat.zeoenergy.com/sandboxes/{id}/*` is authenticated by the existing session cookie and reverse-proxied to sandbox-proxy. See `ai/sandbox/SANDBOX.md § Public iframe routing` for the full traffic path and how to change deployment topologies. |
| `N8N_ENCRYPTION_KEY` | _(unset — required)_ | n8n: symmetric key that encrypts every stored credential blob (API keys, OAuth secrets) in `n8n-db`. Generate with `openssl rand -hex 32`, paste into `.env`, and back it up alongside your other secrets — **losing it permanently destroys all stored credentials** (workflow rows survive, but their auth blobs become unreadable). Compose refuses to start when unset via `${N8N_ENCRYPTION_KEY:?...}` in `ai/n8n/docker-compose.n8n.yml`. |
| `N8N_DB_USER` / `_PASSWORD` / `_NAME` | `n8n` / `n8n` / `n8n` | n8n: credentials for the compose-managed `n8n-db` Postgres backing workflow, credential, and execution rows. Same dev-default guidance as `ROOFIX_DB_*` / `SANDBOX_DB_*` — override before exposing `PORT_N8N_DB` past the host. |
| `PORT_N8N_DB` | `5435` | n8n: host port `n8n-db` binds to for remote querying (psql / DBeaver / DataGrip). Lives in the `PORT REGISTRY` block at the top of `.env`, grouped with the other Postgres ports (`5432` litellm_db, `5433` roofix-db, `5434` sandbox-db, `5435` n8n-db). n8n itself has **no** host port — the editor is reached via oauth2-proxy at `chat.zeoenergy.com/n8n/`. |

## Threading Model — Read Before Touching Anything

This is the most likely place to introduce bugs. Three threads run concurrently:

1. **Main thread** — pystray event loop (`icon.run()`). Blocking this freezes the tray. All tray menu callbacks must spawn daemon threads immediately.
2. **Popup thread** — tkinter `mainloop()` in a daemon thread spawned on tray click. **All tkinter calls must happen on this thread.** Use `_win.after(0, fn)` to schedule from anywhere else — direct calls from other threads crash or hang.
3. **Fetcher thread** — `BrowserLinker._loop()` runs forever; when data arrives it calls `popup.update()`, which uses `after()` internally to stay safe.

## Browser / CDP — Non-Obvious Constraints

- CDP requires **an already-running Chrome instance** with `--remote-debugging-port=9222`. The app launches Chrome itself via `chrome_launcher.py`; it does not use Selenium.
- The 4-second sleep at the start of `_loop()` waits for Chrome to open the tab. Removing it causes reliable connection failures on startup.
- `interceptor.js` is read from disk **once at startup** and cached as a string. Editing the file while the app is running has no effect — restart required.
- **Do not reformat `interceptor.js`.** It is injected verbatim into the page as a CDP parameter. Reformatting can silently change behavior or break string injection.
- The interceptor uses `response.clone()` before reading the body. Removing this gives the page an empty body — the site breaks.
- The `_fetchInterceptorActive` guard prevents double-patching on re-injection. Do not remove it.
- If `requests` or `websocket-client` are uninstallable/missing, the entire account-stats feature silently disables — no error is raised.

## LLM Backend Toggle — Files Modified

`backend.py` modifies two files outside the repo:

- `~/.claude/settings.json` — adds/removes `env` overrides pointing at local llama-server
- `~/.claude.json` — swaps `primaryApiKey` to a dummy key

These are read-modify-write operations. If either file is open/locked by another process the operation may fail silently. After toggling, verify with `is_local_llm_active()`.

`stop_server()` calls `terminate()` but does not wait for exit — the process may briefly linger. There is no automatic cleanup on app quit; the llama-server process becomes orphaned if the user closes the tray without explicitly stopping it.

## State Files (Outside Repo)

| Path | Purpose |
|---|---|
| `~/.claude/projects/**/*.jsonl` | Claude Code session logs — read-only by this app |
| `~/.claude_widget/chrome_profile/` | Chrome profile used for account stats session |
| `~/.claude/settings.json` | Modified by LLM backend toggle |
| `~/.claude.json` | Modified by LLM backend toggle |

The Chrome profile directory contains a singleton lock file. If Chrome crashes without cleanup, the lock may persist and cause session reuse issues on next launch.

## Headless Session Logic

After a successful login, `fetcher.py` writes a sentinel file. On next launch, Chrome starts headless. If the headless session expires (login timeout), the code catches the error, deletes the sentinel, relaunches Chrome visibly, and sets status to `"waiting_login"`. Calling `go_headless()` before a successful login is a silent no-op.

## Stale / Unused Dependencies

`pyproject.toml` lists `selenium`, `trio`, and `trio-websocket` — none are used. The CDP approach replaced Selenium; `trio` is a legacy leftover. Safe to remove if cleaning up.

## No Tests

There is no test suite. Verify changes manually by running the app and checking the popup displays correct data. Use `cdp_spy.py` to verify CDP captures independently of the full app.

## LiteLLM with Phoenix MCP (Tool Calling)

The Phoenix MCP server exposes database tools via LiteLLM. The model receives tool definitions but LiteLLM does **not** execute the tool calls automatically — you must orchestrate the tool call loop.

### Step 1: Send the user message

```bash
curl http://localhost:4001/v1/chat/completions \
  -H "Authorization: Bearer sk-your-master-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3.6",
    "messages": [{"role": "user", "content": "List all tables in the database"}]
  }'
```

The response will have `"finish_reason": "tool_calls"` with a tool call object.

### Step 2: Send the tool result back

Use the `tool_call_id` from the response and call the MCP tool directly, then send the result back:

```bash
curl http://localhost:4001/v1/chat/completions \
  -H "Authorization: Bearer sk-your-master-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3.6",
    "messages": [
      {"role": "user", "content": "List all tables in the database"},
      {"role": "assistant", "tool_calls": [{"function": {"arguments": "{}", "name": "list_tables"}, "id": "CALL_ID_FROM_STEP_1", "type": "function"}]},
      {"role": "tool", "tool_call_id": "CALL_ID_FROM_STEP_1", "content": "[\"projects\", \"users\"]"}
    ]
  }'
```

Replace the `content` with the actual result from calling the tool on the Phoenix MCP server (`https://phoenix-mcp.com/mcp`).

### Phoenix MCP API Token

The Phoenix MCP server issues long-lived API tokens via a browser-based OAuth flow:

```bash
# Get a Google login URL
curl -s https://phoenix-mcp.com/api-token \
  -H "X-API-Key: your-shared-secret"
# Open the returned login_url in a browser, sign in, get your API token

# Or exchange a Google access token directly
curl -X POST https://phoenix-mcp.com/api-token \
  -H "X-API-Key: your-shared-secret" \
  -H "Content-Type: application/json" \
  -d '{"google_token": "<google-access-token>", "expires_in": 0}'
```

## Common Pitfalls

| Pitfall | Effect |
|---|---|
| Calling tkinter methods from background thread without `after()` | Crash or silent hang |
| Editing `interceptor.js` without restarting | No effect on running app |
| Reformatting `interceptor.js` | Breaks string injection |
| Removing the 4-second sleep in `_loop()` | CDP connection fails on startup |
| Calling `go_headless()` before first successful login | Silent no-op |
| Changing `LLM_URL` without re-toggling LLM mode | `is_local_llm_active()` returns false |
| Closing app without stopping llama-server | Orphaned server process |
| Editing `.env` while app is running | No effect until restart |

## Roofix ↔ Phoenix Bridge

`ai/roofix/` bundles the Roofix ↔ Phoenix subsystem: `bridge/` (event-sourced worker) and `scraper/` (Playwright proposal fetcher). One compose file (`docker-compose.roofix.yml`) brings up both. See [`ai/roofix/ROOFIX.md`](ai/roofix/ROOFIX.md) for the operator guide; a few things worth calling out here:

- **Default `DRY_RUN=true`**: on first deploy the bridge fetches Gmail, parses, decides, and logs — but does **not** write to Phoenix. Flip to `false` only after inspecting a full tick.
- **Bridge talks to Phoenix directly (psycopg2), not via MCP**: the earlier MCP variant was reverted because Phoenix MCP write tools weren't ready in time. Bridge needs `PHOENIX_DB_*` env vars set. `DRY_RUN=true` still short-circuits writes.
- **Bridge talks to Gmail directly (Google API + OAuth), not via MCP**: same reason. Requires `credentials.json` + `token.json` in `ROOFIX_BRIDGE_CONFIG_DIR`. First-time login is interactive — run `python components/gmail_client.py` locally once before shipping the token file into the container. See [ROOFIX.md § Gmail OAuth setup](ai/roofix/ROOFIX.md#gmail-oauth-setup).
- **AI fallback via LiteLLM**: `roofix/components/brain.py::generate_ai_decision` uses the OpenAI SDK against `http://litellm:4000`. Swapping Claude for a local vLLM model is a LiteLLM config change, not a bridge code change.
- **Session refresh is a manual operator flow**: the scraper cannot present a login UI. Run `save_roofix_session.py` locally on a laptop with a visible browser, then POST the resulting JSON to the scraper's `/session/refresh`.
- **Michael's mapping**: `ai/roofix/config/field_mapping.json` is a stub. Milestone writes will log "no milestone mapping" and skip until the file is filled in — this is intentional.

## Sandbox subsystem

`ai/sandbox/` runs untrusted, model-generated web apps (Streamlit, Gradio, Flask, FastAPI, Vite+React, Next, Express, static HTML) in short-lived isolated containers and exposes them to Open WebUI as iframe artifacts — the "artifacts panel" pattern, but for anything a real runtime can run. `sandbox-runner` is FastAPI + FastMCP; the model calls `preview_app(runtime, files, entrypoint)` via LiteLLM's MCP registration and gets back a URL to iframe. See [`ai/sandbox/SANDBOX.md`](ai/sandbox/SANDBOX.md) for the operator guide. A few things worth calling out here:

- **Network segmentation is the primary security control, not container hardening.** The subsystem uses FOUR Docker networks: `ai_shared` (external, so openwebui/litellm can reach the runner + proxy), `sandbox_net` (bridge, `internal: true` — sandboxed containers + the egress proxy), `sandbox_state` (bridge, `internal: true` — runner ↔ sandbox-db only), and `sandbox_egress_out` (bridge, NOT internal — the egress proxy's ONLY route to the public internet; nothing else attaches here). `internal: true` means Docker attaches no default gateway; a sandbox that tries to reach `litellm:4000` or `phoenix-mcp` gets no route, and the only escape hatch to the internet is `sandbox-egress`, which enforces the allowlist. If you add a service to `ai/sandbox/docker-compose.sandbox.yml`, keep the network memberships minimal and re-run the security-invariant checklist in SANDBOX.md.
- **`sandbox-runner` is the only container with `docker.sock` access.** It's the audit boundary. Any code that opens the socket must live in `ai/sandbox/runner/spawner.py` — that's the review focal point.
- **Egress is allowlisted, not blocked-by-default.** `sandbox-egress` (tinyproxy) reads [`ai/sandbox/proxies/tinyproxy.filter`](ai/sandbox/proxies/tinyproxy.filter) with `FilterDefaultDeny Yes`. Adding a new dep source is a filter-file edit + `docker compose restart sandbox-egress`. Never add `.*` — that defeats the model.
- **Base image port is always 80.** All runtime templates in `ai/sandbox/runner/runtimes.py` bind port 80 so `sandbox-proxy` (Caddy) can statically route `/{sandbox_id}/* → sandbox-{id}:80` with no dynamic config. Adding a new runtime that listens on something else means also adding dynamic Caddy admin-API management — don't unless you have to.
- **Job state is Postgres (`sandbox-db`), not SQLite.** Concurrent writers, JSONB metadata for operator queries via `psql`, and the DB lives on its own `sandbox_state` network so a container escape in a sandbox can't tamper with job records. `common.jobs.PostgresRegistry` is the reusable backend — any future service that wants Postgres-backed jobs can adopt it.
- **Sessions + idle-TTL.** `preview_app` accepts an optional `session_id` — omit on the first call, reuse on follow-ups to update files in the running container. The reaper enforces both `SANDBOX_HARD_TTL_SECONDS` (absolute) and `SANDBOX_IDLE_TTL_SECONDS` (bumped on every session-reuse call via `metadata->>'last_used_at'`). Explicit teardown via `DELETE /session/{id}` or the existing `DELETE /jobs/{id}`.

## n8n subsystem

`ai/n8n/` runs a self-hosted [n8n](https://n8n.io) workflow engine — used for stitching together LiteLLM, Roofix, MCP servers, and external HTTP surfaces without writing bespoke code for each automation. Compose file: [`ai/n8n/docker-compose.n8n.yml`](ai/n8n/docker-compose.n8n.yml). Operator guide: [`ai/n8n/N8N.md`](ai/n8n/N8N.md). A few things worth calling out here:

- **Reached at `/n8n/` under the shared oauth2-proxy, not a dedicated hostname.** `OAUTH2_PROXY_UPSTREAMS` in `.env` includes `http://n8n:5678/n8n/` — oauth2-proxy's longest-prefix routing sends `/n8n/*` to the n8n container, same pattern the sandbox subsystem uses for `/sandboxes/*`. The n8n container's `N8N_PATH`, `N8N_EDITOR_BASE_URL`, and `N8N_WEBHOOK_URL` env vars (all in `.env`) tell it it's mounted at that subpath and MUST stay in sync with `OAUTH2_PROXY_UPSTREAMS` — changing one without the other silently breaks routing. The shared oauth2-proxy cookie means one Google sign-in covers both Open WebUI and n8n. **No Cloudflare dashboard change is needed** — same hostname, new path prefix.
- **All container config lives in `.env`, not the compose file.** `ai/n8n/docker-compose.n8n.yml` is a pure `${…}` template — same discipline as `ai/openwebui/docker-compose.openwebui.yml`. Edit `.env` (the `## n8n subsystem` block), not the compose YAML.
- **`N8N_ENCRYPTION_KEY` is load-bearing and must persist across restarts.** It encrypts every stored credential blob in `n8n-db`. Rotating or losing it does NOT destroy workflow rows but permanently corrupts the encrypted credential fields — every saved OAuth token, API key, and password becomes unreadable. Generate with `openssl rand -hex 32`, paste into `.env` before the first start, back it up. The compose file uses `${N8N_ENCRYPTION_KEY:?...}` to fail fast if unset.
- **AI nodes are preconfigured against LiteLLM.** `N8N_AI_OPENAI_API_BASE` (in `.env`) points at `http://litellm:4000/v1` and the API key is sourced from `DEFAULT_LITELLM_MASTER_KEY`, so the OpenAI / LangChain credential picker defaults to the local model list — operators don't paste the base URL or key per workflow. Only override at the credential level when targeting an external provider (real OpenAI, Anthropic direct, etc.).
- **No queue mode / Redis broker.** Single-process runtime is enough for the current prototype workload; if execution backlog becomes real, flip `EXECUTIONS_MODE=queue` and add a Redis service — the Postgres backing DB already supports that migration.

## Shared Python code

Any Python package or module that could plausibly be reused across multiple projects — current or future — MUST live in `shared/common/`, not in the project directory that first needs it. This includes: scraping / CDP / browser helpers, MCP protocol clients, LiteLLM / model client wrappers, env + logging boilerplate, and cross-cutting utilities.

**Test:** before creating a new module inside `widget/`, `ai/roofix/`, `ai/interceptor/`, or any future project dir, ask *"could a second project want this in six months?"* If yes, it goes in `shared/common/` under an appropriate subpackage and the project imports it via the uv workspace (`common = { workspace = true }` in the project's `pyproject.toml`, backed by the root `pyproject.toml`'s `[tool.uv.workspace]` declaration).

Project-specific business logic (Roofix event parsing, brain decision rules, widget's tray UI, etc.) stays in the project directory — the test is reusability, not size.

Adding a new capability to `shared/common/`: create the subpackage under `shared/common/src/common/<name>/`, expose the public API from its `__init__.py`, add tests under `shared/common/tests/`. No pyproject changes needed in consuming projects unless a new external dep is introduced.

**Current shared subpackages:**
- `common.cdp_interceptor` — Chrome DevTools Protocol interceptor (used by `interceptor`, `widget`)
- `common.env` — walk-up `.env` loader
- `common.logging_setup` — CSV audit logger + stdlib configuration
- `common.processed_store` — Gmail message-id dedup cache (used by `roofix`)
- `common.jobs` — id-addressable job tracking with three backends: `InMemoryRegistry` (sync, ephemeral — used by `interceptor`), `SqliteRegistry` (async, `aiosqlite`, persistent — used by `classifier`), and `PostgresRegistry` (async, `asyncpg`, persistent, JSONB metadata, real connection pool — used by `sandbox`). Plus a `build_router` FastAPI factory for the standard `GET /jobs`, `GET /jobs/{id}`, `POST /jobs/{id}/cancel`, `DELETE /jobs/{id}` endpoints (auto-detects sync vs async). See [`shared/common/src/common/jobs/__init__.py`](shared/common/src/common/jobs/__init__.py) for backend selection guidance.

## AI Infrastructure — Compose Topology

The Docker Compose services in `ai/` are documented in [`ai/AI_INFRA.md`](ai/AI_INFRA.md), which contains:

- A table linking every `docker-compose.*.yml` to its README.
- A Mermaid flow diagram showing how the products connect (traffic ingress → oauth2-proxy → openwebui → LiteLLM → vLLM/Kokoro/MADLAD/classifier, plus auxiliary flows).
- A consolidated host-port table.

**Maintenance rule — keep the diagram in sync.** Whenever a new `docker-compose.*.yml` file is added under `ai/` (or an existing one is renamed, removed, or has its services / ports / cross-service dependencies changed), update `ai/AI_INFRA.md` in the same change:

1. Add / update / remove the row in the **Compose files** table, with a link to the compose file and to its README (create the README if none exists).
2. Add / update / remove the corresponding node in the Mermaid **Flow diagram** — including edges for every runtime dependency (e.g. `service X calls service Y over ai_shared`).
3. Update the **Ports at a glance** table with the new host port.
4. If the service participates in the public traffic path (Cloudflare → oauth2-proxy → …), extend the "Reading the diagram" bullets so the new hop is called out.

The diagram is the single source of truth for how the AI infrastructure fits together — do not add a new compose file without updating it.

## Postman collections — one per API service

Every service under `ai/` that exposes an HTTP API ships a Postman v2.1 collection alongside its docs — importable directly into Postman for hands-on debugging. The canonical example is [`ai/sandbox/sandbox-runner.postman_collection.json`](ai/sandbox/sandbox-runner.postman_collection.json); use it as the structural template (collection-level bearer auth wired to a `virtual master key` variable, a `litellm` base-URL variable, one `item` per endpoint with a real request body, an `MCP` subfolder when the service exposes JSON-RPC over HTTP).

**Current collections:**

| Service | Collection | Endpoints doc |
|---|---|---|
| sandbox-runner | [`ai/sandbox/sandbox-runner.postman_collection.json`](ai/sandbox/sandbox-runner.postman_collection.json) | [`ai/sandbox/ENDPOINTS.md`](ai/sandbox/ENDPOINTS.md) |
| interceptor | [`ai/interceptor/interceptor.postman_collection.json`](ai/interceptor/interceptor.postman_collection.json) | [`ai/interceptor/INTERCEPTOR.md`](ai/interceptor/INTERCEPTOR.md) |
| classifier | [`ai/classifier/classifier.postman_collection.json`](ai/classifier/classifier.postman_collection.json) | [`ai/classifier/API.md`](ai/classifier/API.md) |
| roofix bridge | [`ai/roofix/roofix.postman_collection.json`](ai/roofix/roofix.postman_collection.json) | [`ai/roofix/ROOFIX.md`](ai/roofix/ROOFIX.md) |
| kokoro (TTS) | [`ai/kokoro/kokoro.postman_collection.json`](ai/kokoro/kokoro.postman_collection.json) | [`ai/kokoro/KOKORO.md`](ai/kokoro/KOKORO.md) |
| madlad (translate) | [`ai/madlad/madlad.postman_collection.json`](ai/madlad/madlad.postman_collection.json) | [`ai/madlad/MADLAD.md`](ai/madlad/MADLAD.md) |
| litellm proxy | [`ai/litellm/litellm.postman_collection.json`](ai/litellm/litellm.postman_collection.json) | [`ai/litellm/LITELLM.md`](ai/litellm/LITELLM.md), [`ai/litellm/LITELLM_MCP.md`](ai/litellm/LITELLM_MCP.md) |
| vllm (per model) | [`ai/vllm/vllm.postman_collection.json`](ai/vllm/vllm.postman_collection.json) | [`ai/vllm/VLLM.md`](ai/vllm/VLLM.md) |
| llama-server | [`ai/llama/llama.postman_collection.json`](ai/llama/llama.postman_collection.json) | [`ai/llama/LLAMA.md`](ai/llama/LLAMA.md) |
| searxng | [`ai/searxng/searxng.postman_collection.json`](ai/searxng/searxng.postman_collection.json) | [`ai/searxng/SEARXNG.md`](ai/searxng/SEARXNG.md) |

OpenWebUI is intentionally omitted — it is a UI, not an API. All model traffic it emits already lands on LiteLLM's collection.

**Maintenance rule — keep the collections in sync with the code.** Whenever an API endpoint is added, renamed, removed, or changes its request/response shape, path params, query params, auth mode, or headers, update the corresponding `*.postman_collection.json` in the SAME change:

1. Add / update / remove the `item` under the collection's `item` array (or the appropriate subfolder like `MCP`).
2. Match the `request.url.path`, `request.method`, `request.header`, and `request.body.raw` to the code exactly — bodies must be valid JSON matching the current Pydantic / FastAPI model.
3. **Path parameters use `:name`, not `{{name}}`.** Any URL path segment that's a dynamic identifier (session id, job id, message id, filename, etc.) MUST be written in Postman's path-variable syntax — `:sandboxSessionId`, `:jobId`, `:roofixMessageId` — in BOTH `request.url.raw` and each matching entry in `request.url.path`. Add a `request.url.variable` array on the URL object with one `{"key": "name", "value": "", "description": "..."}` entry per path variable so Postman renders the editable field. Keep the corresponding top-level collection `variable` in place too (so users have one default they paste into). The `{{host}}` variable at the start of the URL (`{{litellm}}`, `{{kokoro}}`, `{{roofix}}`, etc.) is the ONLY `{{…}}` form allowed in `url.raw` — it stays as-is. `{{…}}` is also fine in headers, request bodies, query-string values, and auth blocks — the rule is path segments only.
4. Update the item's `description` to say WHAT it does, WHAT it returns, WHAT errors are possible, and reference the endpoint's docs section (e.g. `ENDPOINTS.md § …`).
5. If a new collection-level variable is needed (e.g. a fresh path-param handle like `sessionId`), add it under the collection's top-level `variable` array with a `description` explaining how to populate it.
6. If routing changes (e.g. a service moves onto a new LiteLLM pass-through prefix, or leaves LiteLLM entirely for a direct-service URL), update the collection's base-URL variable AND the `info.description` note about how requests are authenticated.
7. If a NEW service under `ai/` starts exposing an HTTP API, create `{service}.postman_collection.json` alongside its docs, add a row to the **Current collections** table above, and structure it after `sandbox-runner.postman_collection.json`.

Collections are the single source of truth for how each service's API is called from outside — do not merge an API-shape change without updating them. The `*.md` endpoint doc and the `*.postman_collection.json` MUST agree; when they don't, the code is authoritative and both must be corrected.
