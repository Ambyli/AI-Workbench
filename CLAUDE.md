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
| `ESCALATION_RECIPIENTS` | _(empty)_ | Roofix Bridge: comma-separated recipients that receive forwarded escalations. Empty disables forwarding — escalates stay unread in Gmail for direct operator review. |
| `GMAIL_CREDENTIALS_PATH` | `/config/credentials.json` | Roofix Bridge: OAuth client-secrets file |
| `GMAIL_TOKEN_PATH` | `/config/token.json` | Roofix Bridge: OAuth refresh-token file |
| `PHOENIX_DB_HOST` / `_PORT` / `_NAME` / `_USER` / `_PASSWORD` / `_SSLMODE` | _(secrets)_ | Roofix Bridge: direct psycopg2 connection to Phoenix Postgres |
| `ROOFIX_DB_USER` / `_PASSWORD` / `_NAME` | `roofix` / _(required)_ / `roofix` | Roofix Bridge: credentials for the compose-managed `roofix-db` Postgres backing `ProcessedStore`. Password has no default — compose fails without it. |
| `ROOFIX_DB_HOST_PORT` | `5433` | Roofix Bridge: host port `roofix-db` binds to for remote connections. Default avoids clashing with Phoenix / a local Postgres on 5432. |
| `PHOENIX_AGENT_USER_ID` | _(unset — required for writes)_ | Roofix Bridge: dedicated Phoenix user id |
| `PHOENIX_ROOFIX_ID_COLUMN` | `migration_external_id` | Roofix Bridge: column where Roofix ids are stamped |
| `INTERCEPTOR_API_URL` | `http://interceptor-api:8080` | Roofix Bridge → interceptor-api base URL |
| `ROOFIX_PROFILE_NAME` | `roofix` | Named profile inside interceptor-api holding Roofix session cookies |
| `INTERCEPTOR_PROFILES_ROOT` | `/data/profiles` | interceptor-api: root under which named `--user-data-dir` profiles live |
| `INTERCEPTOR_MAX_CONCURRENT` | `8` | interceptor-api: max simultaneous `/capture` calls (port pool size). Each slot ≈ one Chrome + optional profile clone — see `ai/INTERCEPTOR_API.md § Resource sizing` |

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

`ai/roofix/` bundles the Roofix ↔ Phoenix subsystem: `bridge/` (event-sourced worker) and `scraper/` (Playwright proposal fetcher). One compose file (`docker-compose.roofix.yml`) brings up both. See [`ai/ROOFIX.md`](ai/ROOFIX.md) for the operator guide; a few things worth calling out here:

- **Default `DRY_RUN=true`**: on first deploy the bridge fetches Gmail, parses, decides, and logs — but does **not** write to Phoenix. Flip to `false` only after inspecting a full tick.
- **Bridge talks to Phoenix directly (psycopg2), not via MCP**: the earlier MCP variant was reverted because Phoenix MCP write tools weren't ready in time. Bridge needs `PHOENIX_DB_*` env vars set. `DRY_RUN=true` still short-circuits writes.
- **Bridge talks to Gmail directly (Google API + OAuth), not via MCP**: same reason. Requires `credentials.json` + `token.json` in `ROOFIX_BRIDGE_CONFIG_DIR`. First-time login is interactive — run `python components/gmail_client.py` locally once before shipping the token file into the container. See [ROOFIX.md § Gmail OAuth setup](ai/ROOFIX.md#gmail-oauth-setup).
- **AI fallback via LiteLLM**: `roofix/components/brain.py::generate_ai_decision` uses the OpenAI SDK against `http://litellm:4000`. Swapping Claude for a local vLLM model is a LiteLLM config change, not a bridge code change.
- **Session refresh is a manual operator flow**: the scraper cannot present a login UI. Run `save_roofix_session.py` locally on a laptop with a visible browser, then POST the resulting JSON to the scraper's `/session/refresh`.
- **Michael's mapping**: `ai/roofix/config/field_mapping.json` is a stub. Milestone writes will log "no milestone mapping" and skip until the file is filled in — this is intentional.

## Shared Python code

Any Python package or module that could plausibly be reused across multiple projects — current or future — MUST live in `shared/common/`, not in the project directory that first needs it. This includes: scraping / CDP / browser helpers, MCP protocol clients, LiteLLM / model client wrappers, env + logging boilerplate, and cross-cutting utilities.

**Test:** before creating a new module inside `widget/`, `ai/roofix/`, `ai/interceptor-api/`, or any future project dir, ask *"could a second project want this in six months?"* If yes, it goes in `shared/common/` under an appropriate subpackage and the project imports it via the uv workspace (`common = { workspace = true }` in the project's `pyproject.toml`, backed by the root `pyproject.toml`'s `[tool.uv.workspace]` declaration).

Project-specific business logic (Roofix event parsing, brain decision rules, widget's tray UI, etc.) stays in the project directory — the test is reusability, not size.

Adding a new capability to `shared/common/`: create the subpackage under `shared/common/src/common/<name>/`, expose the public API from its `__init__.py`, add tests under `shared/common/tests/`. No pyproject changes needed in consuming projects unless a new external dep is introduced.

**Current shared subpackages:**
- `common.cdp_interceptor` — Chrome DevTools Protocol interceptor (used by `interceptor-api`, `widget`)
- `common.env` — walk-up `.env` loader
- `common.logging_setup` — CSV audit logger + stdlib configuration
- `common.processed_store` — Gmail message-id dedup cache (used by `roofix`)
- `common.jobs` — id-addressable job tracking with two backends: `InMemoryRegistry` (sync, ephemeral — used by `interceptor-api`) and `SqliteRegistry` (async, persistent — used by `classifier`), plus a `build_router` FastAPI factory for the standard `GET /jobs`, `GET /jobs/{id}`, `POST /jobs/{id}/cancel`, `DELETE /jobs/{id}` endpoints. See [`shared/common/src/common/jobs/__init__.py`](shared/common/src/common/jobs/__init__.py) for backend selection guidance.

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
