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
| `AUDIO_BASE_URL` | `http://localhost:8000` | Base URL returned by Kokoro `text_to_speech` MCP tool |

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
