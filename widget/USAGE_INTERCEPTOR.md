# cdp_interceptor

Site-agnostic Chrome DevTools Protocol interceptor. Launches an isolated Chrome, injects a `fetch`/`XHR` interceptor into a target page, and streams captured JSON response bodies to your callbacks. No Selenium, no ChromeDriver — connects directly to Chrome's built-in DevTools Protocol.

Callers control three things:

| Knob | What it controls |
|---|---|
| `target_url` (passed to `.launch(...)`) | The page Chrome loads. Not the API URL — the page URL. |
| `url_patterns` (constructor arg) | Regex list applied to each intercepted **request URL**. Isolates the specific network call(s) whose body you want. |
| `parse_fn` (constructor arg) | Callback that receives each matched `Capture(url, body)`. Return an extracted dict, or `None` to skip. |

You can use one, both, or neither of `url_patterns` / `parse_fn`.

---

## Requirements

- Windows + Google Chrome installed
- Python ≥ 3.10
- `requests` and `websocket-client` (already in `widget/pyproject.toml`)

Runs inside the widget's venv:

```powershell
cd widget
uv sync
.venv\Scripts\python.exe -m cdp_interceptor.spy --help
```

---

## Quickstart — CLI

Print every JSON response a page's fetch/XHR calls:

```powershell
.venv\Scripts\python.exe -m cdp_interceptor.spy `
    --url https://news.ycombinator.com `
    --profile-dir "$env:TEMP\my_spy"
```

Restrict to specific request URLs (regex — any match wins):

```powershell
.venv\Scripts\python.exe -m cdp_interceptor.spy `
    --url https://github.com/anthropics/claude-code `
    --profile-dir "$env:TEMP\my_spy" `
    --pattern "api\.github\.com" `
    --pattern "graphql"
```

Ctrl-C to stop.

### CLI flag reference

| Flag | Required | Default | Meaning |
|---|---|---|---|
| `--url URL` | yes | — | Page to load in Chrome. This is the browser tab's URL, not the API URL. |
| `--profile-dir PATH` | yes | — | Chrome `--user-data-dir` for the isolated session. Created if missing. Cookies/logins persist across runs in this dir. |
| `--port INT` | no | `9222` | Chrome remote-debugging port. Change if another Chrome instance is already using 9222. |
| `--pattern REGEX` | no | — (all captures pass through) | URL regex to isolate. Repeatable — any match forwards the capture. Matched against the intercepted `fetch`/`XHR` URL, not the page URL. |
| `--headless` | no | `False` | Force headless launch. Only useful if you've already logged in with this `--profile-dir` at least once (otherwise you can't complete login). |
| `--debug` | no | `False` | Verbose logging (interceptor JS `console.log` + Python DEBUG). |

---

## Quickstart — Python

```python
import re
from cdp_interceptor import InterceptorClient

def handle(cap):
    # cap.url is the request URL; cap.body is the parsed JSON.
    if "orders" in cap.url:
        return {"order_id": cap.body["id"], "status": cap.body["state"]}
    return None

client = InterceptorClient(
    profile_dir=r"C:\temp\my_scraper",
    url_patterns=[re.compile(r"api\.somesite\.com/v1")],
    parse_fn=handle,
    on_data=print,   # fires with whatever handle() returned
)
client.launch("https://somesite.com/dashboard")
input("Enter to stop... ")
client.quit()
```

---

## Public API

```python
from cdp_interceptor import (
    InterceptorClient,
    Capture,
    ClientState,
    find_chrome,
    ChromeNotFoundError,
    session_exists,
    mark_session_ok,
    clear_session,
)
```

Import-time side effects are limited to installing a `NullHandler` on `logging.getLogger("cdp_interceptor")` — the library never configures the root logger or writes log files.

### `InterceptorClient`

Thread-safe façade. All configuration flows through the constructor; callbacks fire on the background worker thread.

#### Constructor arguments

```python
InterceptorClient(
    profile_dir: str,
    debug_port: int = 9222,
    *,
    debug_logging: bool = False,
    url_patterns: list[str | re.Pattern] | None = None,
    parse_fn: Callable[[Capture], dict | None] | None = None,
    on_data: Callable[[dict], None] | None = None,
    on_capture: Callable[[Capture], None] | None = None,
    on_status: Callable[[str, str | None], None] | None = None,
    session_sentinel: bool = True,
    login_timeout: int = 300,
    capture_timeout: int = 30,
    capture_poll: float = 2.0,
    login_url_keywords: tuple[str, ...] = ("login", "signin", "/auth"),
    chrome_path: str | None = None,
    interceptor_script: str | None = None,
)
```

##### `profile_dir: str` — **required**

Path to an isolated Chrome `--user-data-dir`. Cookies, localStorage, and the login state live here — reusing the same path across runs means you only have to log in once. Different paths give you fully separate sessions. Created if it doesn't exist.

Recommended: something under the user profile, e.g. `os.path.expandvars(r"%USERPROFILE%\.my_app\chrome_profile")`.

##### `debug_port: int = 9222`

Chrome's remote-debugging port. Only reason to change this is if the port is already in use (e.g. another cdp_interceptor session is running).

##### `debug_logging: bool = False`

Enables verbose logging inside the injected JS interceptor. When true, every intercepted request URL and response body is `console.log`'d in Chrome DevTools. Handy for figuring out why captures aren't showing up. Set it, launch visible, press F12 in Chrome to see the output.

##### `url_patterns: list[str | re.Pattern] | None = None`

**Regex list applied to `Capture.url`** — the URL the page's `fetch`/`XHR` sent to, *not* the browser's page URL.

- `None` (default): every JSON response reaches `parse_fn` / `on_data`.
- Non-empty list: only responses whose URL matches at least one pattern reach `parse_fn` / `on_data`. Non-matching captures still fire `on_capture` (debug hook).

Strings are compiled with `re.compile`; `re.Pattern` values are passed through unchanged. Matches use `.search()` (partial match anywhere in the URL) — anchor with `^` / `$` if you want exact matches.

Use this when you know the exact API endpoint you want. If you don't know it yet, leave it `None` and use `on_capture` to discover it.

##### `parse_fn: Callable[[Capture], dict | None] | None = None`

Called with each URL-pattern-matched `Capture(url, body)`. Return:

- a truthy dict → forwarded to `on_data`
- `None` → skipped

If `parse_fn` is `None`, `on_data` fires with the raw `Capture.body` for each URL-pattern match.

Use this for body-shape filtering (independent of URL) or to extract just the fields you care about before they hit `on_data`.

##### `on_data: Callable[[dict], None] | None = None`

Called on the worker thread each time `parse_fn` returns a truthy dict (or, if `parse_fn` is `None`, each time a URL-pattern-matched body arrives). Argument is whatever `parse_fn` returned, or the raw body.

##### `on_capture: Callable[[Capture], None] | None = None`

**Unfiltered** raw stream — fires for every JSON response the interceptor sees, regardless of `url_patterns`. Useful for discovering which endpoints a page hits before deciding what to filter for. Not affected by `parse_fn`.

##### `on_status: Callable[[str, str | None], None] | None = None`

Called with `(status, error)` whenever the client's status changes. `status` is one of:

| Status | Meaning |
|---|---|
| `"unlinked"` | Constructed but `.launch()` not yet called. |
| `"loading"` | Chrome starting, or CDP session reconnecting. |
| `"waiting_login"` | Chrome is on a login page. Waits up to `login_timeout` seconds for the URL to leave the login-keyword zone. |
| `"ok"` | Data was successfully parsed and delivered. |
| `"error"` | Something failed. `error` is populated. |

`error` is `None` unless a message is present.

##### `session_sentinel: bool = True`

When `True`, writes a marker file into `profile_dir` after the first successful `on_data` delivery. On subsequent `.launch()` calls, Chrome starts headlessly (no window) as long as the marker exists. If the headless session times out at login, the marker is cleared and Chrome is relaunched visibly so you can log in again.

Set to `False` if you always want visible Chrome, or if you don't want the library writing anything into `profile_dir`.

##### `login_timeout: int = 300`

Seconds to wait for the user to complete login before giving up (raises `TimeoutError` inside the worker). Detected by checking whether `location.href` still contains any of `login_url_keywords`.

##### `capture_timeout: int = 30`

Seconds to poll `window._capturedResponses` after each navigation looking for a body that satisfies `url_patterns` + `parse_fn`. If nothing matches within this window, the session raises `RuntimeError` and the reconnect loop retries. Bump this for slow-loading SPAs.

##### `capture_poll: float = 2.0`

Seconds between poll attempts during the initial capture phase.

##### `login_url_keywords: tuple[str, ...] = ("login", "signin", "/auth")`

Substrings that, when present in `location.href`, mean "user hasn't finished logging in". Add your site's SSO / login path if the defaults miss it (e.g. `("login", "signin", "/auth", "sso", "okta")`).

##### `chrome_path: str | None = None`

Absolute path to `chrome.exe`. `None` uses `find_chrome()`, which checks Program Files, Program Files (x86), `%LOCALAPPDATA%`, and the `CHROME_PATHS_VAR` environment variable.

##### `interceptor_script: str | None = None`

Custom JS to inject instead of the bundled `interceptor.js`. The library prepends `const DEBUG_LOGGING = <bool>;` before injection. Only set this if you need to change what gets captured (e.g. add binary-body handling).

#### Methods

| Method | Purpose |
|---|---|
| `.launch(target_url: str) -> None` | Start Chrome, load `target_url`, begin the CDP loop. Non-blocking. Second call while running logs a warning and returns. Raises `ChromeNotFoundError` if Chrome isn't found. |
| `.fetch_now() -> None` | Ask the live session to reload the target page. |
| `.go_headless() -> None` | Relaunch Chrome headlessly. No-op if `session_sentinel=True` and no sentinel exists (would require interactive login). |
| `.go_visible() -> None` | Relaunch Chrome visibly. |
| `.quit() -> None` | Terminate Chrome, stop the worker. Safe to call multiple times. |
| `.get_state() -> ClientState` | Snapshot of current status (lock-guarded). |
| `.is_running` *(property)* | True while the worker thread is alive. |
| `InterceptorClient.is_available()` *(staticmethod)* | True if `requests` and `websocket-client` are importable. |

### `Capture` — dataclass

```python
@dataclass
class Capture:
    url: str    # request URL captured by the interceptor
    body: dict  # parsed JSON body
```

### `ClientState` — dataclass

```python
@dataclass
class ClientState:
    status: str                     # see on_status statuses above
    headless: bool                  # True if the current Chrome is headless
    error: str | None
    last_capture_at: float | None   # time.monotonic() of the last successful on_data
```

### Low-level helpers

- `find_chrome(extra_paths: list[str] | None = None) -> str | None` — returns the first existing Chrome path (built-ins + `CHROME_PATHS_VAR` + your extras), or `None`.
- `ChromeNotFoundError` — raised by `.launch()` when no Chrome executable is found.
- `session_exists(profile_dir)` / `mark_session_ok(profile_dir)` / `clear_session(profile_dir)` — the sentinel file primitives. Rarely needed directly; the client manages them when `session_sentinel=True`.

---

## Concepts

### URL patterns vs body parser — when to use which

| You want to… | Use |
|---|---|
| Extract a specific known endpoint (`/api/orders`) | `url_patterns=[r"/api/orders"]` — the fastest filter. |
| Extract from responses matching a body shape, endpoint unknown | Leave `url_patterns=None`, do shape-matching inside `parse_fn`. Slower but flexible. |
| Both — narrow by URL, then extract fields | Use both. `parse_fn` only sees URL matches. |
| Discover what endpoints exist | `on_capture=print` with no `url_patterns`. Watch and refine. |

### Threading model

- `.launch()` returns immediately; a daemon worker thread does the CDP work.
- All callbacks (`on_data`, `on_capture`, `on_status`, `parse_fn`) run on the worker thread. Don't do UI work directly — schedule it onto your UI thread.
- `.get_state()` is safe to call from any thread (lock-guarded).
- `.quit()` sets a stop event and joins with a 2s timeout.

### Session persistence (headless-after-first-success)

1. First `.launch()` — no sentinel yet → Chrome launches visibly. You log in. Data flows in. `on_data` fires. Sentinel is written to `profile_dir`.
2. Later `.launch()` (or restart of your app) — sentinel exists → Chrome launches headless. No window, but data still flows.
3. Session expires in headless mode → worker sees a login page → `TimeoutError` after `login_timeout` seconds → sentinel cleared → Chrome relaunches visibly → status becomes `"waiting_login"`.

Disable the whole flow with `session_sentinel=False`.

---

## Recipes

### Discover endpoints on an unknown site

```python
from cdp_interceptor import InterceptorClient

client = InterceptorClient(
    profile_dir=r"C:\temp\discover",
    on_capture=lambda cap: print(cap.url),  # just URLs, no bodies
    session_sentinel=False,
)
client.launch("https://target-site.com/dashboard")
input("Explore the site in the Chrome window, then Enter to stop... ")
client.quit()
```

### Extract from a known API by URL

```python
client = InterceptorClient(
    profile_dir=r"C:\temp\orders",
    url_patterns=[r"api\.example\.com/v1/orders"],
    on_data=lambda body: print(f"got {len(body['items'])} orders"),
)
client.launch("https://example.com/orders")
```

### Match by body shape (unknown endpoint)

```python
def looks_like_a_project(cap):
    body = cap.body
    if isinstance(body, list) and body and "Project ID" in body[0]:
        return {"count": len(body), "projects": body}
    if isinstance(body, dict):
        for k in ("data", "results", "items", "projects"):
            v = body.get(k)
            if isinstance(v, list) and v and "Project ID" in v[0]:
                return {"count": len(v), "projects": v}
    return None

client = InterceptorClient(
    profile_dir=r"C:\temp\projects",
    parse_fn=looks_like_a_project,
    on_data=lambda p: print(f"{p['count']} projects"),
)
client.launch("https://phoenix.zeoenergy.com/projects")
```

(See `test_phoenix_spy.py` for a full working version.)

### Long-running poller — refresh every 5 minutes

```python
import threading, time

client = InterceptorClient(...)
client.launch("https://...")
stop = threading.Event()
try:
    while not stop.wait(300):
        client.fetch_now()
except KeyboardInterrupt:
    pass
client.quit()
```

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `ChromeNotFoundError` on `.launch()` | Set `chrome_path=r"C:\path\to\chrome.exe"` or point `CHROME_PATHS_VAR` at it. |
| Status stays `"loading"` forever, no captures | Endpoint might not be JSON, or `url_patterns` doesn't match. Turn on `debug_logging=True`, run visible, open Chrome DevTools (F12), watch console. |
| `TimeoutError: Login timed out` | Site's login URL doesn't contain any of the default `login_url_keywords`. Add your site's login/SSO substring to `login_url_keywords`. |
| Data arrives once then never again | Normal for one-shot GETs. The page has to fire the request again — call `.fetch_now()` to force a reload, or interact with the page in visible mode. |
| Second `.launch()` in the same process is a no-op | Expected — one client, one Chrome. Call `.quit()` first, or use a second `InterceptorClient` with a different `debug_port`. |
| Chrome window opens as a new tab in my regular Chrome instead of a new window | Known limitation of the current launcher — happens on some recent Chrome versions when the URL argument is passed on the command line. Not addressed in this pass. |
| `test_phoenix_spy.py` (or any script) says "session_ok not found" and won't go headless | You haven't had a successful `on_data` delivery yet. Log in, wait for the first parsed hit, sentinel is written automatically. |

Verbose logging when everything else fails:

```python
import logging
logging.basicConfig(level=logging.DEBUG)
# and pass debug_logging=True to InterceptorClient
```

---

## Files

| File | Purpose |
|---|---|
| `cdp_interceptor/__init__.py` | Public API re-exports; installs `NullHandler`. |
| `cdp_interceptor/client.py` | `InterceptorClient` façade, `Capture`/`ClientState` dataclasses. |
| `cdp_interceptor/launcher.py` | `find_chrome`, `start_chrome`, `clear_singleton_locks`. |
| `cdp_interceptor/cdp_session.py` | `run_session` — the WebSocket loop that talks to Chrome. |
| `cdp_interceptor/sentinel.py` | Session-marker file helpers. |
| `cdp_interceptor/spy.py` | CLI entry point (`python -m cdp_interceptor.spy`). |
| `cdp_interceptor/interceptor.js` | Injected JS that patches `fetch`/`XHR`. |
