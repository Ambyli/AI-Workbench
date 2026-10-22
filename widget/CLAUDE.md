# widget/ — Claude Usage Observer

Windows tray app (`claude_observer` package) that totals Claude Code token usage from `~/.claude/projects/**/*.jsonl` and, optionally, scrapes claude.ai account stats over CDP; it can also switch Claude Code onto a local llama-server. Guide: [`USAGE_WIDGET.md`](USAGE_WIDGET.md). The CDP layer is `common.cdp_interceptor` (`shared/common/src/common/cdp_interceptor/`), not this folder.

## Commands

From the repo root (one shared workspace `.venv`): `uv run --no-sync python -m claude_observer` (or `uv run --no-sync python widget/claude_usage_widget.py`, or the `claude-usage-observer` script). CDP debugging: `uv run cdp-spy` (`common.cdp_interceptor.spy`, needs a Chrome with `--remote-debugging-port`). Never `uv sync --package claude-usage-observer` — it prunes the shared venv (root `CLAUDE.md` § Testing).

## Configuration

`config.json` at the **repo root** (`config.py`: `Path(__file__).parent.parent.parent`); `widget/config.json` is not read. Edit it or use tray right-click → **Settings…**; the Settings window applies changes live through `config.apply_updates()`, a hand edit needs a restart. `.env` is read only once, to migrate into a missing `config.json`. Full table: `USAGE_WIDGET.md § Configuration`.

- `DEBUG_LOGGING` (`false`) — verbose CDP + widget logging.
- `REFRESH_INTERVAL_SECONDS` (`300`) — seconds between local token-stat refreshes.
- `CONSOLE_FETCHER_ENABLED` (`false`) — claude.ai account-stats scraping.
- `BROWSER_DEBUG_PORT` (`9222`) — Chrome remote-debugging port.
- `EXCLUDE_WEEKDAYS` (`"5,6"`) — days left out of rolling averages (0 = Mon).
- `INCLUDE_PATHS` (empty) — count only sessions whose `cwd` starts with one of these comma-separated prefixes (case-insensitive).
- `LLM_URL` (`http://localhost:8001`) — local llama-server; `is_local_llm_active()` compares `~/.claude/settings.json`'s `ANTHROPIC_BASE_URL` with it, so changing it without re-toggling reads as "not active".
- `LLM_API_KEY` (`sk-no-key-required`) — key sent to the local server.
- `LLM_MODEL` (empty) — model alias passed to Claude Code; must match llama-server's `--alias`.
- `LLM_LOG_MAX_LINES` (`200`) — server-output log box length.
- `LLAMA_SERVER_CMD` (empty) — full command that launches llama-server.
- `KEEP_LLM_ACTIVE` (`false`) — on Quit, keep Claude Code pointed at the local LLM (the server is still stopped).
- `CHROME_PATHS_VAR` — a system environment variable (not `config.json`) naming another Chrome executable.

## Threading model — read before touching anything

Three threads; the likeliest place to introduce bugs.
1. **Main thread** — pystray `icon.run()`. Blocking it freezes the tray; every tray-menu callback spawns a daemon thread at once.
2. **Popup thread** — tkinter `mainloop()` in a daemon thread started on tray click. **Every tkinter call happens on this thread.** From anywhere else schedule with `_win.after(0, fn)` (`UsagePopup.schedule_on_tk`, `update`, `apply_console` already do); a direct call crashes or hangs.
3. **Fetcher thread** — `InterceptorClient._loop()` (`common.cdp_interceptor`, wrapped by `browser/fetcher.py::BrowserLinker`) runs forever and delivers data through `popup.update()` / `apply_console()`, which use `after()`.

## Account stats (CDP)

- CDP traps (the 4 s start-up sleep, `interceptor.js` read once and injected verbatim, `response.clone()`, the `_fetchInterceptorActive` guard): `shared/common/CLAUDE.md` § `cdp_interceptor` traps — also root `CLAUDE.md` § Cross-folder rules (**`cdp_interceptor` traps**).
- If `requests` or `websocket-client` is missing, `BrowserLinker.is_available()` is false and the whole account-stats feature is silently off — no error.
- Headless after first login: the client writes a `session_ok` sentinel into the profile dir after the first successful capture; later launches start headless; an expired headless session clears it, relaunches visibly, status `"waiting_login"`. `go_headless()` before the first successful login is a silent no-op. Detail: `shared/common/src/common/cdp_interceptor/README.md § Session persistence (headless-after-first-success)`.
- The Chrome profile is `~/.claude_widget/chrome_profile/`; a crashed Chrome can leave its singleton lock behind (`clear_singleton_locks()` cleans it).

## LLM backend toggle — files outside the repo

`llm/backend.py` read-modify-writes `~/.claude/settings.json` (adds / removes `env` overrides pointing at llama-server) and `~/.claude.json` (swaps `primaryApiKey` for a dummy). A file locked by another process can make that fail silently — verify with `is_local_llm_active()` after toggling. `stop_server()` calls `terminate()` without waiting, so the process may linger briefly. Tray **Quit** stops the server (and restores the Claude API unless `KEEP_LLM_ACTIVE`); any other exit orphans llama-server.

## State files (outside the repo)

| Path | Purpose |
|---|---|
| `~/.claude/projects/**/*.jsonl` | Claude Code session logs — read-only here |
| `~/.claude_widget/chrome_profile/` | Chrome profile for the account-stats session |
| `~/.claude/settings.json`, `~/.claude.json` | Modified by the LLM backend toggle |

## Stale dependencies

`widget/pyproject.toml` still lists `selenium`, `trio`, `trio-websocket`; none is used (CDP replaced Selenium). Safe to remove when cleaning up.

## Common pitfalls

| Pitfall | Effect |
|---|---|
| tkinter call from a background thread without `after()` | Crash or silent hang |
| Editing / reformatting `interceptor.js` | No effect until restart / breaks the injection |
| Removing the 4 s sleep in `InterceptorClient._loop()` | CDP connection fails on start-up |
| `go_headless()` before the first successful login | Silent no-op |
| Changing `LLM_URL` without re-toggling LLM mode | `is_local_llm_active()` returns false |
| Exiting other than through tray **Quit** | Orphaned llama-server |
| Hand-editing `config.json` while running | No effect until restart |

## Tests

None. Verify by running the app and checking the popup; check CDP captures alone with `uv run cdp-spy`.
