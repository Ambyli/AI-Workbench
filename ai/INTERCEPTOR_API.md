# interceptor

Generic HTTP + MCP front-end for `common.cdp_interceptor`. Give it a URL and a list of URL regex patterns; it opens the URL in a headless Chrome under a named `--user-data-dir`, waits for a bounded window, and returns the JSON XHR/fetch bodies whose URLs matched any pattern.

- **Container**: `interceptor` (internal only, port 8080 on `ai_shared`)
- **Compose file**: `ai/docker-compose.interceptor.yml`
- **Dockerfile**: `ai/Dockerfile.interceptor`
- **Source**: `ai/interceptor/{app.py,profiles.py}`
- **LiteLLM integration**: both MCP (`interceptor.capture_url`) and pass-through (`/v1/interceptor/*`), configured in `ai/litellm_config.yaml`

## Bring it up

```powershell
docker network create ai_shared     # once, if not already present
docker compose -f ai/docker-compose.interceptor.yml up --build -d
docker compose -f ai/docker-compose.interceptor.yml logs -f interceptor
```

Health:

```powershell
docker exec interceptor curl -s http://localhost:8080/health
```

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness |
| `GET` | `/profiles` | List all named profiles |
| `GET` | `/profiles/{name}` | One profile's status (size, sentinel) |
| `POST` | `/profiles/{name}/refresh` | Upload a `.tgz` of a captured Chrome profile |
| `DELETE` | `/profiles/{name}` | Wipe one profile |
| `POST` | `/capture` | Run one capture (see request/response below) |
| `GET` | `/jobs` | Snapshot of the port pool + currently-running captures |
| `GET` | `/jobs/{job_id}` | Detail on one in-flight capture (404 if not found) |
| `POST` | `/jobs/{job_id}/cancel` | Abort an in-flight capture, reclaim its slot |
| — | `/mcp` | FastMCP HTTP transport (`capture_url` tool) |

### `POST /capture`

Request:

```json
{
  "url": "https://example.com/dashboard",
  "url_patterns": ["example\\.com/api/v1/items", "example\\.com/api/v1/user"],
  "profile": "example",
  "capture_window_seconds": 20,
  "keep_open": false,
  "login_timeout": 300,
  "max_matches_per_pattern": null,
  "debug_logging": false
}
```

Chrome always runs headless in the container. The service writes a `session_ok` sentinel into each uploaded profile so `InterceptorClient` boots straight into headless — an operator only uploads a profile *after* logging in on their laptop, so treating uploaded profiles as session-ready by definition matches reality. If the persisted session expires, `InterceptorClient` detects the login redirect, sets `status="waiting_login"`, and the response comes back with `login_wall: true`; refresh the profile via `POST /profiles/{name}/refresh` and retry.

Response:

```json
{
  "job_id": "a3f2b1c9d4e5",
  "url": "https://example.com/dashboard",
  "status": "ok",
  "login_wall": false,
  "error": null,
  "matches": {
    "example\\.com/api/v1/items": [ { "url": "https://…", "body": { … } } ],
    "example\\.com/api/v1/user":  [ { "url": "https://…", "body": { … } } ]
  },
  "captured_urls": [ "https://…", "…" ]
}
```

`job_id` is a 12-char hex identifier for the capture. During the request's lifetime it shows up in `GET /jobs` (see [Observability](#observability)) and is prefixed onto every log line emitted by that capture — useful for correlating interleaved logs when concurrent captures are running.

Patterns are `re.search`-matched against every JSON XHR/fetch URL the page emits. A capture whose URL matches multiple patterns lands in the bucket of the **first** matching pattern.

## Concurrency

The service handles many `/capture` calls in parallel. Two knobs shape the behavior:

- **Different profiles fully parallel.** A `/capture` against `profile=roofix` and one against `profile=gmail` never contend on each other.
- **Same profile, fast + slow path.** The first same-profile request in flight takes the fast path — Chrome runs against the base `--user-data-dir` and refreshed session cookies persist to disk. Any concurrent same-profile request falls into the slow path — the service takes a live snapshot of the base profile via `shutil.copytree` into `PROFILES_ROOT/.temp/temp_profile_<uuid>/`, launches Chrome against the clone, and deletes the clone on completion. Raw copy of a live profile is safe: Chrome's SQLite (`Cookies`) and LevelDB (`Local Storage`, `IndexedDB`) stores use journal-based crash recovery, so a mid-write snapshot at worst yields a slightly-stale-but-consistent state, never corruption.
- **Port pool caps total concurrency.** A bounded pool of CDP debug ports (starting at `INTERCEPTOR_DEBUG_PORT`, sized by `INTERCEPTOR_MAX_CONCURRENT`) is the hard resource ceiling. Every capture — fast or slow — grabs one port from the pool at start and returns it at end. Pool exhausted → **HTTP 429 Too Many Requests** with `retry-later` semantics.

### Resource sizing

Each concurrent slot holds one running Chrome (~200–400 MB RAM) plus, when in a same-profile collision, a live profile-dir clone (typically 20–80 MB disk in `PROFILES_ROOT/.temp`). Setting `INTERCEPTOR_MAX_CONCURRENT=32` means budgeting ~10–13 GB of RAM and ~1–3 GB of ephemeral disk headroom for a fully-saturated fleet. Scale the container's memory limit and volume size accordingly.

### Crash recovery

If Chrome crashes mid-capture (OOM, segfault, container killed) and leaves a stale `SingletonLock` in the base profile dir, `InterceptorClient.launch` clears the lock before every next launch (`shared/common/src/common/cdp_interceptor/client.py:159`, `clear_singleton_locks()`). No manual cleanup needed — the next request self-heals.

If the interceptor process itself dies mid-request, temp-profile clones under `PROFILES_ROOT/.temp/` are left behind. They're swept on next startup by the FastAPI lifespan hook, so the volume doesn't accrue orphans across restarts.

### Cookie freshness caveat

Fast-path captures write refreshed session cookies back to the base profile — that's what keeps a `roofix` or `gmail` session warm across days-to-weeks. Slow-path captures write to a doomed clone, so their cookie updates are discarded. Under sustained same-profile burst load (multiple in flight at all times), the base profile stops receiving cookie refreshes and eventually the session expires — re-upload the profile when you see `login_wall: true` in responses.

## Refreshing a profile (operator flow)

`interceptor` cannot present a login UI itself, so profiles are captured on an operator laptop and uploaded. The archive must be a **`.tar.gz`** — the endpoint uses `tarfile.open(mode="r:*")` (see `ai/interceptor/profiles.py:64`), which auto-detects gzip/bzip2/xz tar. Plain `.zip` will NOT work.

Profile names must match `[a-z0-9][a-z0-9_-]{0,63}` — no path separators, no leading dots.

### 1. Capture a logged-in Chrome profile

Use `cdp-spy` — the CLI shipped with `shared/common` (`shared/common/pyproject.toml:17`, source at `shared/common/src/common/cdp_interceptor/spy.py`). Run from the repo root:

```powershell
uv run cdp-spy --url https://gmail.com --profile-dir C:\tmp\gmail_profile
```

- Point `--url` at a page that requires the login you want to persist.
- Point `--profile-dir` at a **fresh, empty** directory. `cdp-spy` passes this to Chrome as `--user-data-dir`, so cookies / localStorage / IndexedDB accumulate here.
- Chrome opens **visibly**. Log in in the window that appears. Navigate around enough to confirm the session sticks (e.g. reload — you should stay signed in).
- **Ctrl-C in the terminal** to stop `cdp-spy`. This closes Chrome cleanly so profile files aren't locked when you tar them up.

You can skip `cdp-spy` and use raw Chrome with `--user-data-dir=C:\tmp\gmail_profile` if you prefer — the only requirement is that the resulting directory contains a working Chrome profile.

### 2. Package the profile as `.tar.gz`

The archive should contain the profile-dir **contents**, not the directory itself — the endpoint extracts into an already-created `/data/profiles/{name}/`. The `-C <dir> .` pattern does that.

**Windows 10+ / PowerShell** (bsdtar bundled):

```powershell
tar czf gmail.tgz -C C:\tmp\gmail_profile .
```

**Linux / macOS:**

```bash
tar czf gmail.tgz -C /tmp/gmail_profile .
```

**No `tar` available? Use 7-Zip in two steps:**

```powershell
7z a -ttar gmail.tar C:\tmp\gmail_profile\*
7z a -tgzip gmail.tgz gmail.tar
Remove-Item gmail.tar
```

Verify entries land at the archive root (`./Default/Cookies`, `./First Run`, …), not nested under a parent dir:

```powershell
tar tzf gmail.tgz | Select-Object -First 20
```

### 3. Upload

The endpoint expects `multipart/form-data` with a single field named `archive`.

```powershell
curl -X POST -F "archive=@gmail.tgz" http://<host>:8080/profiles/gmail/refresh
```

Successful response:

```json
{
  "unpacked": true,
  "name": "gmail",
  "path": "/data/profiles/gmail",
  "present": true,
  "size_bytes": 12345678,
  "sentinel_present": true
}
```

The endpoint wipes any existing `PROFILES_ROOT/gmail` before extracting, then writes a `session_ok` sentinel so the next `/capture` call boots straight to headless. `InterceptorClient.launch` clears any stray `SingletonLock` before opening Chrome, so freshly-uploaded profiles are safe to use immediately.

### What must be inside the archive

The important pieces of a Chrome user-data-dir for auth persistence:

| Path | What it holds |
|---|---|
| `Default/Cookies` (SQLite) | Session cookies |
| `Default/Local Storage/` | localStorage entries |
| `Default/IndexedDB/` | IndexedDB stores (some sites store tokens here) |
| `Default/Session Storage/` | sessionStorage |
| `Local State` | Encryption key ref (needed to decrypt `Cookies` on the same machine) |

### Portability caveat (Windows → Linux)

On Windows, Chrome encrypts cookies with a key that's itself encrypted with **DPAPI** (tied to the Windows user account). Cookies captured on a Windows laptop **will not decrypt inside the Linux container**, because DPAPI is Windows-only. Empirically Playwright's bundled chromium tolerates a lot of profile portability, but if a login refuses to stick after a capture-and-upload cycle, DPAPI is the usual cause.

Workaround: capture the profile inside **WSL2** (or on a Linux host) using Playwright's chromium — that produces a Linux-native profile the container can read directly.

### Sanity check before uploading

Spot-check that the profile actually holds the session by re-running `cdp-spy` against the same profile-dir and hitting a page that requires auth:

```powershell
uv run cdp-spy --url https://gmail.com/inbox --profile-dir C:\tmp\gmail_profile
```

If you land on the inbox (not the login page), the profile is good to package.

## Configuration

| Env var | Default | Notes |
|---|---|---|
| `INTERCEPTOR_PROFILES_ROOT` | `/data/profiles` | Root under which named profiles live. Backed by the `interceptor_data` volume. Temp clones live under `<root>/.temp/`. |
| `INTERCEPTOR_DEBUG_PORT` | `9224` | Base of the CDP debug port pool. Pool spans `[base, base + INTERCEPTOR_MAX_CONCURRENT)`. |
| `INTERCEPTOR_MAX_CONCURRENT` | `8` | Max simultaneous `/capture` calls. Each slot = one Chrome (~200–400 MB RAM) + on same-profile collision one profile clone (~20–80 MB disk). See [Resource sizing](#resource-sizing). |
| `INTERCEPTOR_CAPTURE_WINDOW_SECONDS` | `20` | Default capture window when a request omits `capture_window_seconds`. |

## Calling from LiteLLM

### Pass-through

```powershell
curl -X POST http://localhost:4001/v1/interceptor/capture `
  -H "Authorization: Bearer $env:DEFAULT_LITELLM_MASTER_KEY" `
  -H "content-type: application/json" `
  -d '{"url":"https://httpbin.org/json","url_patterns":["httpbin\\.org/json"],"profile":"httpbin"}'
```

### MCP tools

The `interceptor` MCP server (registered in `ai/litellm_config.yaml` `mcp_servers.interceptor`) exposes four model-invokable tools:

| Tool | Purpose | Args |
|---|---|---|
| `capture_url` | Run one capture — same core behavior as `POST /capture` | `url`, `url_patterns`, `profile`, `capture_window_seconds`, `login_timeout`, `max_matches_per_pattern` |
| `list_profiles` | Discover which named profiles exist — call before `capture_url` if the LLM doesn't know the profile name | *(none)* |
| `list_jobs` | Snapshot of the port pool + running captures — same shape as `GET /jobs` | *(none)* |
| `get_job` | Detail on one in-flight capture by id — same shape as `GET /jobs/{job_id}` | `job_id` |

The `keep_open` and `debug_logging` knobs from `POST /capture` are deliberately **not** exposed to MCP — both are operator-only debug flags (`keep_open` requires manual Chrome-kill cleanup; `debug_logging` writes to a DevTools console the LLM can't read).

MCP tools return dicts and never raise — errors surface inside the payload (e.g. `{"error": "no active job …"}` or a `capture_url` response with `status="error"` and an `error` field describing the HTTP-layer failure).

Enable the `interceptor` MCP server on your chat / completion request and the model can call these directly.

## Observability

Every `/capture` invocation gets a 12-char hex `job_id` and shows up in `GET /jobs` for the duration of its run. Two endpoints:

**`GET /jobs`** — snapshot of the port pool + all currently-running captures:

```json
{
  "max_concurrent": 8,
  "active_count": 2,
  "available": 6,
  "jobs": [
    {
      "job_id": "a3f2b1c9d4e5",
      "profile": "roofix",
      "url": "https://roofix.io/project/1234x5678",
      "started_at": "2026-07-29T15:00:00.123456+00:00",
      "elapsed_seconds": 12.4,
      "port": 9224,
      "used_base_profile": true,
      "temp_dir": null,
      "phase": "capturing"
    }
  ]
}
```

**`GET /jobs/{job_id}`** — same shape as one element of `jobs[]`, or **HTTP 404** if the id isn't currently in flight. Completed captures aren't retained — a 404 means either the id never existed or the capture finished.

`phase` progresses: `"cloning"` (slow path only, during `shutil.copytree`) → `"capturing"` (Chrome running, XHRs being intercepted) → `"cleaning_up"` (temp rmtree + port release). Fast-path captures skip `"cloning"` and go straight to `"capturing"`.

Typical workflow — see what's running, then drill in:

```powershell
curl http://<host>:8080/jobs                           # count + list all
curl http://<host>:8080/jobs/a3f2b1c9d4e5              # detail on one
```

### Cancelling a stuck or long-running capture

**`POST /jobs/{job_id}/cancel`** signals the running capture to wake early, quit Chrome, run cleanup, and return. Operator-only — the MCP surface does NOT expose this (an LLM would need to coordinate across two agents to use it usefully; when a stuck capture happens, an operator handles it).

```powershell
curl -X POST http://<host>:8080/jobs/a3f2b1c9d4e5/cancel
# → {"job_id": "a3f2b1c9d4e5", "cancelled": true, "was_phase": "capturing"}
```

Status codes:
- **200** — cancel signal delivered; the `POST /capture` caller receives a normal `CaptureResponse` with `status="cancelled"` plus any partial `matches` / `captured_urls` collected before the abort
- **404** — job unknown or already completed (nothing to cancel)
- **409** — job already in `cleaning_up` phase (too late — the finally block is running)

Cancel is also the way to reclaim a hung **`keep_open=true`** capture. Normally `keep_open` deliberately skips cleanup (Chrome stays running, port + profile lock stay held, job stays in `/jobs`). Cancel bypasses that: it terminates Chrome and runs full cleanup regardless of `keep_open`.

## Debugging

- **`keep_open: true`** on a `/capture` request leaves Chrome running after the capture window. Handy for inspecting the DevTools console live. Cleanup is skipped for that request — the port stays held, the profile lock (fast path) or temp dir (slow path) stays allocated, and the job stays in `GET /jobs` with `phase="capturing"` — until the operator manually kills the container's chromium (or restarts the container). Concurrent captures against a different profile still work; concurrent captures against the same profile will fall into the slow path.
- **`debug_logging: true`** prepends `const DEBUG_LOGGING = true;` to the injected `interceptor.js`, so `[interceptor]` traces appear in the browser console (visible via `--remote-debugging-port` if you attach a debugger, or in container logs if the JS logs escape via CDP).
- **`captured_urls`** in the response lists every JSON XHR/fetch URL the interceptor saw — use it to reverse-engineer the right regex when a page fires unfamiliar endpoints.
- **`job_id` prefix in logs.** Every log line emitted during a capture is tagged `[interceptor] [<job_id>] …` — grep for a specific job_id to isolate one capture's timeline out of interleaved concurrent output.

### Running a capture in visible (non-headless) mode

There's no `headless` field on the capture request — the service always tries to launch headless in production. Headless is gated by `InterceptorClient` on `session_sentinel=True AND session_ok exists in profile_dir` (see `shared/common/src/common/cdp_interceptor/client.py:177`). The service passes `session_sentinel=True` (`ai/interceptor/app.py:206`), so headless comes down to whether the `session_ok` sentinel file is present in the profile directory. Uploaded profiles get one written automatically by `unpack_profile()` (`ai/interceptor/profiles.py:69`).

**To force a visible launch when debugging locally**, delete the sentinel before firing `/capture`. This only works on a machine with a display — inside the Docker container there's no display, so a "visible" launch there will fail to open a window.

1. Confirm the sentinel is present (means the next capture will be headless):

   ```powershell
   curl http://localhost:8080/profiles/roofix
   # → { …, "sentinel_present": true }
   ```

2. Delete the sentinel file. The path is `<PROFILES_ROOT>/<name>/session_ok` — literally a file called `session_ok` with no extension, at the top level of the profile dir (same level as `Default/`, `Local State`, etc.).

   ```powershell
   # Local run — default PROFILES_ROOT resolves to C:\data\profiles\<name> on Windows
   Remove-Item C:\data\profiles\roofix\session_ok

   # Custom PROFILES_ROOT
   Remove-Item "$env:INTERCEPTOR_PROFILES_ROOT\roofix\session_ok"

   # Container
   docker exec interceptor rm /data/profiles/roofix/session_ok
   ```

3. Verify — the response should now show `sentinel_present: false`:

   ```powershell
   curl http://localhost:8080/profiles/roofix
   # → { …, "sentinel_present": false }
   ```

4. Fire your `/capture`. A Chrome window will pop up so you can watch the navigation and any injected `interceptor.js` console logs.

   Pair with `keep_open: true` to keep the window open after the capture window ends — useful for opening DevTools and poking around after the request completes:

   ```json
   {
     "url": "https://roofix.io/project/abc123",
     "url_patterns": ["roofix\\.io/api/1\\.1/init/data"],
     "profile": "roofix",
     "keep_open": true,
     "debug_logging": true
   }
   ```

**One-shot flip.** The sentinel is a one-shot toggle — on the next successful data capture, `InterceptorClient._on_data_inner()` writes it back (`shared/common/src/common/cdp_interceptor/client.py:448`). So the sequence "delete sentinel → run visible capture → observe → next capture goes headless again" is automatic. To force multiple visible runs in a row, delete the sentinel between each call.

**Comparing behavior against `cdp-spy` directly.** For iterating on regex patterns or verifying a profile end-to-end without the API in the loop, run `cdp-spy` against the same profile-dir directly:

```powershell
uv run cdp-spy --url https://roofix.io/project/abc123 --profile-dir C:\data\profiles\roofix `
  --pattern "roofix\.io/api/1\.1/init/data"
```

`cdp-spy` always launches visibly (its `session_sentinel=False` bypasses the gate — see `shared/common/src/common/cdp_interceptor/spy.py:72`) and prints every matched capture to stdout, so you can diff its output against what your `/capture` call returns.

## Limits

- Port pool caps concurrency; over the cap → HTTP 429.
- No server-side `parse_fn` — callers get raw response bodies and extract themselves.
- No streaming captures — `/capture` is one-shot, bounded by `capture_window_seconds`.
- No public auth on the HTTP surface — the service is only reachable via `ai_shared`.
- No completed-job history — `GET /jobs/{id}` returns 404 as soon as a capture finishes.
- No MCP-side cancellation — cancel is HTTP-only. An LLM cannot reclaim a stuck capture it started; that's an operator's job.
