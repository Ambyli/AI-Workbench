# Plan: Rewrite Claude Usage Observer as an Electron App

## Overview

Convert the Python/pystray/tkinter system-tray app into a cross-platform Electron app with a native-system-tray icon and a Chromium-based popup window. The core logic (token parsing, config, CDP browser scraping, LLM toggle) moves to Node.js; the UI moves from tkinter to React/TypeScript rendered in an `<app>` window.

---

## Architecture

```
electron-app/
  ├── package.json
  ├── tsconfig.json
  ├── electron-builder.json5       # packaging config
  ├── src/
  │   ├── main/
  │   │   ├── index.ts             # Electron app entry (BrowserWindow + Tray)
  │   │   ├── tray.ts              # System tray icon + menu
  │   │   ├── popup-window.ts      # Popup window lifecycle
  │   │   ├── config.ts            # config.json reader/writer (live)
  │   │   ├── usage-parser.ts      # .jsonl scanner (port of usage_parser.py)
  │   │   ├── browser-linker.ts    # Chrome CDP session (port of fetcher.py + cdp_client.py)
  │   │   ├── interceptor.ts       # JS injection (port of interceptor.js)
  │   │   ├── response-parser.ts   # Response normalization (port of response_parser.py)
  │   │   ├── llm-backend.ts       # Local LLM toggle + server management (child_process)
  │   │   ├── startup.ts           # Auto-start registry/launchd helpers
  │   │   └── ipc-handlers.ts      # IPC bridge between main and renderer
  │   ├── renderer/
  │   │   ├── index.html
  │   │   ├── main.tsx             # React entry
  │   │   ├── App.tsx              # Root component
  │   │   ├── components/
  │   │   │   ├── TokenUsage.tsx   # Today + Weekly bars
  │   │   │   ├── LastExecution.tsx
  │   │   │   ├── ProjectBreakdown.tsx
  │   │   │   ├── AccountStats.tsx  # claude.ai account data
  │   │   │   ├── LLMBackend.tsx    # Toggle + server log
  │   │   │   ├── Countdown.tsx     # Refresh timer
  │   │   │   ├── CollapsibleSection.tsx
  │   │   │   ├── ProgressBar.tsx
  │   │   │   └── SettingsDialog.tsx
  │   │   └── styles/
  │   │       └── dark.css          # Same color palette as tkinter
  │   └── preload/
  │       └── index.ts             # contextBridge exposure
  └── assets/
      └── tray-icon.svg            # Tray icon source
```

---

## Phase 1: Project Setup & Core Infrastructure

**Goal:** Bare-bones Electron app launches with a tray icon and an empty popup.

### Steps

1. **Initialize npm project**
   - `npm init -y`, add `"type": "module"`
   - Install: `electron`, `electron-builder`, `typescript`, `react`, `react-dom`, `@types/react`, `@types/react-dom`, `vite`, `@vitejs/plugin-react`
   - Create `tsconfig.json`, `vite.config.ts`, `electron-builder.json5`
   - Entry point: `electron` in `package.json.main` → `dist/main/index.js`

2. **Basic Electron main process** (`src/main/index.ts`)
   - Create `BrowserWindow` (the popup) with `nodeIntegration: false`, `contextIsolation: true`
   - Create `Tray` icon with menu (Show, Refresh, Settings, Quit)
   - Tray click → show popup window
   - App quit handler → cleanup (stop LLM server, close browser)
   - IPC handlers registered here

3. **Preload script** (`src/preload/index.ts`)
   - Expose via `contextBridge`: `getUsage()`, `refresh()`, `linkBrowser()`, `goHeadless()`, `goVisible()`, `openSettings()`, `toggleStartup()`, `toggleLLM()`, `launchServer()`, `stopServer()`, `applySettings()`
   - These call `ipcRenderer.invoke()` to the main process

4. **Basic renderer** (`src/renderer/App.tsx`)
   - Minimal React app with a "Hello" placeholder
   - Vite dev server for hot reload during development

### Key decisions
- **Use Vite + React** instead of plain HTML/JSX for fast dev iteration
- **Keep the same dark theme colors** (`#1e1e23`, `#50d490`, etc.) for visual continuity
- **IPC via `ipcRenderer.invoke`** — async, one-call response pattern, clean

---

## Phase 2: Config System & Usage Parser

**Goal:** App reads config.json and parses .jsonl logs, displaying token counts in the popup.

### Steps

5. **Config module** (`src/main/config.ts`)
   - Read/write `~/.claude/usage-observer/config.json` (migrate from project root)
   - Same schema as current `config.json` (all the same keys)
   - In-memory cache with `applyUpdates()` that writes to disk immediately
   - Computed fields: `CLAUDE_DIR`, `BROWSER_PROFILE_DIR`

6. **Usage parser** (`src/main/usage-parser.ts`)
   - Port of `usage_parser.py`'s `get_usage_summary()`
   - Walk `~/.claude/projects/**/*.jsonl` with `fast-glob` or `globby`
   - Parse each JSONL line, filter `output_tokens > 0`
   - Apply `INCLUDE_PATHS` filter on `cwd`
   - Bucket by UTC date, compute:
     - Daily totals (input + output + cache tokens)
     - Weekly totals (Mon–today)
     - Per-project breakdown
     - Rolling 7-day average (excluding `EXCLUDE_WEEKDAYS`)
     - Rolling weekly average
   - Return same shape dict as the Python version

7. **Refresh loop** (in `src/main/index.ts`)
   - `setInterval` every `REFRESH_INTERVAL_SECONDS`
   - Calls `getUsageSummary()`, sends result to popup via `popup.webContents.send('usage-update', data)`
   - Tray icon updates with current token counts
   - Error handling: catch exceptions, show error state

8. **Hook up renderer**
   - `TokenUsage.tsx`: Today/Weekly sections with progress bars
   - `LastExecution.tsx`: Fresh/cache/output/total tokens
   - `ProjectBreakdown.tsx`: Per-project bars, sorted largest-first
   - `Countdown.tsx`: Timer until next refresh
   - Same collapsible section pattern as tkinter

### Data shape (identical to Python version)
```ts
interface UsageSummary {
  today: string;
  weekStart: string;
  daily: { input: number; output: number; total: number };
  weekly: { input: number; output: number; total: number };
  dailyLimit: number;
  weeklyLimit: number;
  lastExec: {
    ts: Date; input: number; cacheCreate: number;
    cacheRead: number; output: number;
  } | null;
  projectBreakdown: Record<string, { input: number; cacheCreate: number; cacheRead: number; output: number; total: number }>;
}
```

---

## Phase 3: CDP Browser Scraper

**Goal:** "Link Browser" button launches Chrome, intercepts claude.ai responses, displays account stats.

### Steps

9. **Port `interceptor.js` to TypeScript** (`src/main/interceptor.ts`)
   - Keep as a raw string template (same injection approach)
   - Patches `fetch` and `XMLHttpRequest` in the target page
   - Writes to `window._capturedResponses`, calls `window.__cdpNotify`
   - **Critical:** Do NOT run through a bundler minifier — inject verbatim

10. **CDP client** (`src/main/browser-linker.ts`)
    - Use `puppeteer-core` (already a mature CDP library) instead of raw `websocket-client`
    - `findChrome()` — scan known Chrome/Edge paths on Windows
    - `launchChrome()` — spawn with `--remote-debugging-port`, `--user-data-dir`, headless flag
    - Connect via `puppeteer.connect({ browserURL: 'http://127.0.0.1:9222' })`
    - Pre-register interceptor via `page.evaluateOnNewDocument()`
    - Register `__cdpNotify` binding via `page.exposeFunction()`
    - Navigate to `claude.ai/settings/usage`
    - Poll `page.evaluate(() => window._capturedResponses)` for data
    - Live update loop: listen for binding calls + fallback polling

11. **Response parser** (`src/main/response-parser.ts`)
    - Port of `response_parser.py`
    - Handles 3 formats: bucketed array, flat tokens, utilization blocks
    - Returns normalized dict

12. **Browser linker lifecycle**
    - `BrowserLinker` class: `launch()`, `fetchNow()`, `goHeadless()`, `goVisible()`, `quit()`
    - Background `setInterval` loop (like Python's `_loop()`)
    - On data: `popup.webContents.send('console-update', state)`
    - Sentinel file for headless session persistence

### Key changes from Python
- **Replace `websocket-client` + raw CDP protocol** with `puppeteer-core` (mature, handles reconnection, tab management, etc.)
- **Replace `requests`** with Node's built-in `fetch` (Node 18+) or `undici`
- **Chrome profile management** stays the same (singleton lock file in `~/.claude_widget/chrome_profile/`)

---

## Phase 4: LLM Backend Toggle

**Goal:** Toggle between Claude API and local llama-server, manage server process.

### Steps

13. **LLM backend** (`src/main/llm-backend.ts`)
    - Read/write `~/.claude/settings.json` — add/remove `env` block
    - Read/write `~/.claude.json` — swap `primaryApiKey`
    - Use `fs.promises.readFile`/`writeFile` with JSON parsing
    - Guard: if file is locked/in-use, return error

14. **Server management**
    - `child_process.spawn()` for llama-server (port of `launch_server()`)
    - Capture stdout/stderr, emit `onLine` events
    - Forward log lines to popup via IPC
    - `stopServer()` → `proc.kill('SIGTERM')`

### Key changes
- Same file paths, same JSON modifications
- `child_process.spawn` replaces `subprocess.Popen`
- Stdout reading uses Node streams instead of a daemon thread

---

## Phase 5: Settings Dialog & UI Polish

**Goal:** Full settings dialog, startup toggle, tray icon states, all sections working.

### Steps

15. **Settings dialog** (`src/renderer/components/SettingsDialog.tsx`)
    - Separate `BrowserWindow` (like the tkinter `Toplevel`)
    - Editable fields for all config keys, grouped sections
    - Save → `ipcRenderer.invoke('applySettings', updates)`
    - Same layout as current Python settings window

16. **Tray icon**
    - Generate SVG/PNG icons programmatically (green/yellow/red status dots)
    - Use `nativeImage` from Electron
    - Update title with token counts

17. **Startup toggle**
    - Windows: registry write to `HKCU\...\Run\ClaudeUsageWidget` (same as Python)
    - Mac (future): `launchd` plist
    - Linux (future): `~/.config/autostart/` desktop file

18. **Window positioning**
    - Anchor popup above system tray (bottom-right)
    - Use `screen.getPrimaryDisplay().workAreaSize` for positioning
    - Persist position to config

19. **UI state persistence**
    - Save section open/closed state + window position
    - File: `~/.claude/usage-observer/ui_state.json`

---

## Phase 6: Packaging & Distribution

**Goal:** Build distributable Windows installer (portable + NSIS).

### Steps

20. **electron-builder config**
    - Target: NSIS installer + portable
    - App ID: `com.claude-observer.usage`
    - Files: include `node_modules`, `src/` compiled output
    - Icon: tray icon as app icon

21. **Code signing** (optional, for distribution)
    - EV certificate for Windows SmartScreen

22. **Auto-updater** (optional, later)
    - `electron-updater` with GitHub Releases

---

## Migration Mapping

| Python | Electron/Node |
|---|---|
| `pystray.Icon` | `Tray` (Electron native) |
| `tkinter.Tk` | `BrowserWindow` (renderer with React) |
| `tkinter.StringVar` + canvas | React state + HTML/CSS |
| `widget.after(0, fn)` | IPC `invoke`/`send` (async, no thread issues) |
| `subprocess.Popen` | `child_process.spawn` |
| `os.scandir` + glob | `fast-glob` or `globby` |
| `websocket-client` | `puppeteer-core` |
| `requests` | Node `fetch` / `undici` |
| `PIL.Image` + `ImageDraw` | `nativeImage` or SVG |
| `ctypes.windll` workarea | `screen.getPrimaryDisplay().workAreaSize` |
| `winreg` startup | `HKCU\...\Run` registry (same API via `regenerator` or `winreg` npm) |
| `config.json` read/write | `fs.promises` JSON read/write |
| `.jsonl` parsing | `fs.promises` line-by-line read + `JSON.parse` |

---

## Dependencies (Node)

```json
{
  "devDependencies": {
    "electron": "^33.0.0",
    "electron-builder": "^25.0.0",
    "typescript": "^5.6.0",
    "react": "^18.3.0",
    "react-dom": "^18.3.0",
    "@types/react": "^18.3.0",
    "@types/react-dom": "^18.3.0",
    "@vitejs/plugin-react": "^4.3.0",
    "vite": "^6.0.0",
    "concurrently": "^9.0.0"
  },
  "dependencies": {
    "puppeteer-core": "^23.0.0",
    "fast-glob": "^3.3.0",
    "undici": "^7.0.0"
  }
}
```

---

## Risk Areas & Mitigations

| Risk | Mitigation |
|---|---|
| `interceptor.js` breaks when ported to TS | Keep as raw string template; test against live claude.ai |
| Puppeteer vs raw CDP behavior differences | Test all 3 response formats; puppeteer is well-documented |
| File locking on `~/.claude/settings.json` | Wrap reads/writes in try/catch with retry |
| Chrome profile lock file on crash | Add cleanup on startup (delete stale lock) |
| Electron app size (~150MB) | Acceptable for a desktop app; `electron-builder` compresses well |
| No tkinter = no native Windows DPI scaling issues | Electron handles DPI better on Windows 10/11 |

---

## What Stays the Same

- **Config schema** — identical `config.json` keys and defaults
- **Data shape** — `getUsageSummary()` returns the same structure
- **File paths** — reads from `~/.claude/projects/`, writes settings to same locations
- **Chrome profile path** — `~/.claude_widget/chrome_profile/`
- **Color palette** — same dark theme colors
- **Refresh interval** — same `setInterval` approach (no threading needed in Electron main process)
- **CDP interceptor logic** — same JS behavior, just injected differently

## What Changes

- **Threading model** — Electron's main process is single-threaded event loop (like Python's main thread). No `after(0, fn)` needed; IPC is async by design.
- **UI framework** — React replaces tkinter. No canvas-drawn bars; use HTML/CSS divs with width %.
- **CDP library** — `puppeteer-core` replaces raw `websocket-client` protocol.
- **Popup behavior** — `BrowserWindow` instead of `tk.Tk`. Always-on-top via `setAlwaysOnTop(true)`.
- **Tray icon** — Electron's `Tray` instead of `pystray`. Same menu structure.
- **Process management** — `child_process.spawn` instead of `subprocess.Popen`.
- **Config location** — Move from project root to `~/.claude/usage-observer/config.json` (proper app data dir).
