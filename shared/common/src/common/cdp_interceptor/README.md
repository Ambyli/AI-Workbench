# common.cdp_interceptor

Site-agnostic Chrome DevTools Protocol interceptor. Launches an isolated browser (Chrome on Windows, Playwright chromium on Linux/mac), injects a `fetch`/`XHR` interceptor into a target page, and streams captured JSON response bodies to your callbacks. No Selenium, no ChromeDriver — connects directly to Chrome's built-in DevTools Protocol.

Callers control three things:

| Knob | What it controls |
|---|---|
| `target_url` (passed to `.launch(...)`) | The page the browser loads. Not the API URL — the page URL. |
| `url_patterns` (constructor arg) | Regex list applied to each intercepted **request URL**. Isolates the specific network call(s) whose body you want. |
| `parse_fn` (constructor arg) | Callback that receives each matched `Capture(url, body)`. Return an extracted dict, or `None` to skip. |

You can use one, both, or neither of `url_patterns` / `parse_fn`.

---

## Requirements

- Python ≥ 3.10
- One of:
  - **Windows** + Google Chrome installed, or
  - **Linux / mac** + Playwright chromium (`uv run playwright install chromium`)
- `requests` + `websocket-client` (declared by the `common` package)

Installed via the repo's uv workspace — every consuming package gets it transparently:

```powershell
uv sync            # from repo root
uv run cdp-spy --help
```

---

## Quickstart — CLI

Print every JSON response a page's fetch/XHR calls:

```powershell
uv run cdp-spy `
    --url https://news.ycombinator.com `
    --profile-dir "$env:TEMP\my_spy"
```

Restrict to specific request URLs (regex — any match wins):

```powershell
uv run cdp-spy `
    --url https://github.com/anthropics/claude-code `
    --profile-dir "$env:TEMP\my_spy" `
    --pattern "api\.github\.com" `
    --pattern "graphql"
```

Ctrl-C to stop.

### CLI flag reference

| Flag | Required | Default | Meaning |
|---|---|---|---|
| `--url URL` | yes | — | Page to load in the browser. This is the browser tab's URL, not the API URL. |
| `--profile-dir PATH` | yes | — | Browser `--user-data-dir` for the isolated session. Created if missing. Cookies/logins persist across runs in this dir. See [Profile management](#profile-management). |
| `--port INT` | no | `9222` | Remote-debugging port. Change if another cdp_interceptor session is already on 9222. |
| `--pattern REGEX` | no | — (all captures pass through) | URL regex to isolate. Repeatable — any match forwards the capture. Matched against the intercepted `fetch`/`XHR` URL, not the page URL. |
| `--headless` | no | `False` | Force headless launch. Only useful if you've already logged in with this `--profile-dir` at least once (otherwise you can't complete login). |
| `--debug` | no | `False` | Verbose logging (interceptor JS `console.log` + Python DEBUG). |

---

## Profile management

A **profile** is a Chrome/Chromium `--user-data-dir` — a directory containing cookies, localStorage, IndexedDB, and login state for one browser identity. Every tool in this repo that takes a `--profile-dir`, an `INTERCEPTOR_PROFILES_ROOT/<name>` slot, or a similar env var reads and writes into one of these directories. Reusing the same directory across launches means the browser stays logged in; using different directories gives you fully isolated identities.

### Creating a profile for a site

One-time step: log in interactively so cookies get written.

```powershell
# Any writable path works. Pick one you'll remember.
uv run cdp-spy `
    --url https://roofix.io `
    --profile-dir "C:\Users\<you>\.zeo\roofix_profile"

# Chrome opens. Log in, complete 2FA, wait until you see your dashboard.
# Ctrl-C in the terminal to stop.
```

That directory now holds a warm session for that site. On the next run against the same `--profile-dir`, the browser skips the login page.

### Reusing a profile across tools

Anything that accepts a browser data directory can point at the same path:

| Tool | How to point at a profile |
|---|---|
| `cdp-spy` CLI | `--profile-dir <path>` |
| `common.cdp_interceptor.InterceptorClient` (Python) | `profile_dir=<path>` constructor arg |
| `interceptor` service | Named profile — `POST /profiles/{name}/refresh` uploads a `.tgz` into `INTERCEPTOR_PROFILES_ROOT/{name}/` |
| Any future tool built on `common.cdp_interceptor` | Whatever env var / arg it exposes |

Same directory + same site + valid cookies → no login prompt. Different sites in the same profile is fine (Chrome scopes cookies per-domain) but conventionally you'd keep one profile per site to keep concerns clean.

### Inspecting a profile

Profile directories are just directories — you can `ls` / `Get-ChildItem` them. Key entries:

| File / dir | What's in it |
|---|---|
| `Default/Cookies` | SQLite DB of cookies. This is what keeps you logged in. |
| `Default/Local Storage/` | localStorage from each origin. Often holds session tokens for SPAs. |
| `Default/IndexedDB/` | Same idea, larger structured storage. |
| `Default/Login Data` | Saved passwords, **encrypted with a machine-bound key** — not portable across machines. |
| `Default/Preferences` | Chrome settings for this profile. Rarely relevant. |
| `SingletonLock` / `SingletonCookie` / `SingletonSocket` | Lockfiles Chrome writes on launch and removes on clean shutdown. `common.cdp_interceptor.clear_singleton_locks()` cleans stale ones. |
| `session_ok` | Marker written by `InterceptorClient` after first successful capture (see [`session_sentinel`](#session_sentinel-bool--true)). Not present unless the library was told to write it. |

### When to re-capture

Only three scenarios need a fresh login:

1. **First time ever** — no profile dir exists yet.
2. **Site expired the session** — you'll see the browser land on the site's login page instead of the target URL. In `interceptor` this shows up as `login_wall: true` in the `POST /capture` response.
3. **You deleted the profile dir** (or ran `docker compose down -v` on a service whose volume held it).

For most sites the cookie TTL is days to weeks — you re-capture rarely.

### Sharing / shipping a profile

Profiles are portable across machines with one caveat (see below). To move one:

```powershell
# Package (contents of the profile dir at the archive root — note the trailing `.`)
tar czf profile.tgz -C "C:\Users\<you>\.zeo\<site>_profile" .

# Ship it via whatever channel (POST it to a service, scp to a server, etc.)
# Then unpack on the other side into the target profile-dir path.
tar xzf profile.tgz -C "/data/profiles/<name>"
```

`interceptor`'s `POST /profiles/{name}/refresh` endpoint accepts this tar as a multipart upload and unpacks it into `INTERCEPTOR_PROFILES_ROOT/{name}/`.

**Cross-machine caveat:** cookies and localStorage travel fine. `Login Data` (saved passwords) is encrypted with an OS-level key bound to the originating machine, so imported profiles can't auto-fill on a fresh login page from a different host. This rarely matters because the session cookies alone keep the browser logged in. Just don't rely on "log in from inside the container using an imported saved password."

### Resetting / deleting a profile

Wipe the directory. On next launch a fresh profile is created.

```powershell
Remove-Item -Recurse -Force "C:\Users\<you>\.zeo\roofix_profile"
```

For Docker services: `docker compose down -v` (or the appropriate `make very-clean <service> CONFIRM=yes`) removes the volume the profile lived on.

### Concurrent access

Only **one** browser process can hold a given profile dir at a time — the singleton lock enforces this. If two `.launch()` calls (or two `cdp-spy` runs, or two overlapping HTTP requests against `interceptor`) target the same profile-dir concurrently, the second will either:

- Hand its URL off to the first (Chrome's IPC behavior) and exit, or
- Fail to open if `--remote-debugging-port` collides.

Serialize access at the application layer if you fan out — a mutex around `client.launch(...)`, or one profile-dir per concurrent request.

---

## Quickstart — Python

```python
import re
from common.cdp_interceptor import InterceptorClient

def handle(cap):
    # cap.url is the request URL; cap.body is the parsed JSON.
    if "orders" in cap.url:
        return {"order_id": cap.body["id"], "status": cap.body["state"]}
    return None

client = InterceptorClient(
    profile_dir=r"C:\Users\me\.myapp\profile",
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
from common.cdp_interceptor import (
    InterceptorClient,
    Capture,
    ClientState,
    find_browser,          # OS-adaptive: Windows Chrome or Playwright chromium
    find_chrome,           # backwards-compat alias for find_browser
    BrowserNotFoundError,
    ChromeNotFoundError,   # backwards-compat alias for BrowserNotFoundError
    session_exists,
    mark_session_ok,
    clear_session,
    # side channels — each opens its own CDP connection to the tab
    capture_screenshot, Screenshot, ScreenshotError,
    run_actions, parse_actions, Action, ActionResult, ActionsReport, ActionError,
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
    login_actions: Sequence[Action] = (),
    login_fill_origins: Sequence[str] = (),
    login_actions_timeout_s: float = 60.0,
    login_lock: threading.Lock | None = None,
)
```

##### `profile_dir: str` — **required**

Path to an isolated Chrome `--user-data-dir`. See [Profile management](#profile-management). Created if missing.

##### `debug_port: int = 9222`

Remote-debugging port. Only reason to change this is if the port is already in use.

##### `debug_logging: bool = False`

Enables verbose logging inside the injected JS interceptor. When true, every intercepted request URL and response body is `console.log`'d in DevTools. Handy for figuring out why captures aren't showing up.

##### `url_patterns: list[str | re.Pattern] | None = None`

**Regex list applied to `Capture.url`** — the URL the page's `fetch`/`XHR` sent to.

- `None` (default): every JSON response reaches `parse_fn` / `on_data`.
- Non-empty list: only responses whose URL matches at least one pattern reach `parse_fn` / `on_data`. Non-matching captures still fire `on_capture` (debug hook).

Strings are compiled with `re.compile`; `re.Pattern` values pass through. Matches use `.search()` — anchor with `^` / `$` for exact match.

##### `parse_fn: Callable[[Capture], dict | None] | None = None`

Called with each URL-pattern-matched `Capture(url, body)`. Return a truthy dict to forward to `on_data`, or `None` to skip.

If `parse_fn` is `None`, `on_data` fires with the raw `Capture.body`.

##### `on_data: Callable[[dict], None] | None = None`

Called on the worker thread each time `parse_fn` returns a truthy dict (or, if `parse_fn` is `None`, each time a URL-pattern-matched body arrives).

##### `on_capture: Callable[[Capture], None] | None = None`

**Unfiltered** raw stream — fires for every JSON response the interceptor sees, regardless of `url_patterns`. Useful for discovering which endpoints a page hits.

##### `on_status: Callable[[str, str | None], None] | None = None`

Called with `(status, error)` on state transitions. Statuses:

| Status | Meaning |
|---|---|
| `"unlinked"` | Constructed, `.launch()` not called. |
| `"loading"` | Browser starting, or CDP session reconnecting. |
| `"waiting_login"` | Browser is on a login page. Waits up to `login_timeout` seconds for the URL to leave the login-keyword zone. |
| `"ok"` | Data was successfully parsed and delivered. |
| `"error"` | Something failed. `error` is populated. |

##### `session_sentinel: bool = True`

When `True`, writes a `session_ok` marker into `profile_dir` after the first successful `on_data`. Subsequent `.launch()` calls start headless as long as the marker exists. If the headless session times out at login, the marker is cleared and the browser is relaunched visibly.

Set to `False` if you always want visible mode, or if you don't want the library writing anything into `profile_dir`.

##### `login_timeout: int = 300`

Seconds to wait for the user to complete login before giving up. Detected by checking whether `location.href` still contains any of `login_url_keywords`.

##### `capture_timeout: int = 30`

Seconds to poll `window._capturedResponses` after each navigation looking for a body that satisfies `url_patterns` + `parse_fn`.

##### `capture_poll: float = 2.0`

Seconds between poll attempts during the initial capture phase.

##### `login_url_keywords: tuple[str, ...] = ("login", "signin", "/auth")`

Substrings in `location.href` that mean "still logging in". Add your site's SSO path if the defaults miss it.

##### `chrome_path: str | None = None`

Absolute path to the browser executable. `None` uses `find_browser()` (Windows Chrome or Playwright chromium).

##### `interceptor_script: str | None = None`

Custom JS to inject instead of the bundled `interceptor.js`.

##### `login_actions`, `login_fill_origins`, `login_actions_timeout_s`, `login_lock`

Automated login. When `login_actions` is non-empty, the first time a session hits a login wall (right after status `"waiting_login"`) the worker runs them with `run_actions(..., gate="login", fill_origins=login_fill_origins)` — on the login page itself — then waits for the URL to leave the login zone exactly as it would for a human (`login_timeout` counts from when the steps finish). One attempt per client, never retried, so a bad password can't lock an account by looping. `login_actions_timeout_s` caps the whole run (gate + steps); `login_lock`, if given, is held while the steps run so callers can stop two clients on the same account submitting the form at once. Pass actions whose credential values are already resolved — this class never logs or reports a fill's value (a fill's result is `{element, value_length}`). Read the outcome with `get_login_report()`. `ai/interceptor` builds these from `login_actions` + a per-profile credentials file; see [INTERCEPTOR.md § Login actions](../../../../../ai/interceptor/INTERCEPTOR.md#login-actions).

#### Methods

| Method | Purpose |
|---|---|
| `.launch(target_url: str) -> None` | Start the browser, load `target_url`, begin the CDP loop. Non-blocking. Raises `BrowserNotFoundError` if no browser is found. |
| `.fetch_now() -> None` | Ask the live session to reload the target page. |
| `.go_headless() -> None` | Relaunch headless. No-op if `session_sentinel=True` and no sentinel exists. |
| `.go_visible() -> None` | Relaunch visibly. |
| `.quit() -> None` | Terminate the browser, stop the worker. Safe to call multiple times. |
| `.get_state() -> ClientState` | Snapshot of current status (lock-guarded). |
| `.screenshot(...) -> Screenshot` | Image of the page tab, over a second CDP connection. Raises `ScreenshotError`. |
| `.run_actions(actions, *, page_script=None, ready_timeout_s=60, ...) -> ActionsReport` | Fill / click / press / evaluate in the page tab over a second CDP connection — see [Driving the page](#driving-the-page-run_actions). Never raises. |
| `.get_login_report() -> ActionsReport \| None` | What `login_actions` did; `None` if none were configured or no login wall was hit. A placeholder report while they are still running. |
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
    headless: bool                  # True if the current browser is headless
    error: str | None
    last_capture_at: float | None   # time.monotonic() of the last successful on_data
```

### Low-level helpers

- `find_browser(extra_paths=None) -> str | None` — OS-adaptive. Windows: first installed Chrome path (with `CHROME_PATHS_VAR` + caller extras). Linux/mac: Playwright's bundled chromium.
- `find_chrome(...)` — backwards-compat alias for `find_browser`.
- `BrowserNotFoundError` — raised by `.launch()` when no browser is found. `ChromeNotFoundError` is aliased to this.
- `session_exists(profile_dir)` / `mark_session_ok(profile_dir)` / `clear_session(profile_dir)` — sentinel file primitives.

---

## Concepts

### URL patterns vs body parser — when to use which

| You want to… | Use |
|---|---|
| Extract a specific known endpoint (`/api/orders`) | `url_patterns=[r"/api/orders"]` — fastest filter. |
| Extract from responses matching a body shape, endpoint unknown | Leave `url_patterns=None`, do shape-matching inside `parse_fn`. |
| Both — narrow by URL, then extract fields | Use both. `parse_fn` only sees URL matches. |
| Discover what endpoints exist | `on_capture=print` with no `url_patterns`. Watch and refine. |

### Threading model

- `.launch()` returns immediately; a daemon worker thread does the CDP work.
- All callbacks (`on_data`, `on_capture`, `on_status`, `parse_fn`) run on the worker thread. Don't do UI work directly — schedule it onto your UI thread.
- `.get_state()` is safe to call from any thread (lock-guarded).
- `.quit()` sets a stop event and joins with a 2s timeout.

### Session persistence (headless-after-first-success)

1. First `.launch()` — no sentinel → browser launches visibly. You log in. Data flows in. `on_data` fires. Sentinel is written to `profile_dir`.
2. Later `.launch()` (or restart of your app) — sentinel exists → browser launches headless.
3. Session expires in headless mode → worker sees a login page → `TimeoutError` after `login_timeout` seconds → sentinel cleared → browser relaunches visibly → status becomes `"waiting_login"`.

Disable with `session_sentinel=False`.

---

## Recipes

### Discover endpoints on an unknown site

```python
from common.cdp_interceptor import InterceptorClient

client = InterceptorClient(
    profile_dir=r"C:\temp\discover",
    on_capture=lambda cap: print(cap.url),
    session_sentinel=False,
)
client.launch("https://target-site.com/dashboard")
input("Explore the site in the browser, then Enter to stop... ")
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

(See `widget/test_phoenix_spy.py` for a full working version.)

### Driving the page (`run_actions`)

For a page that only fires the request you want after someone fills a form and clicks a button, let the page's own code send it — the interceptor captures the response as usual. `run_actions` opens its own short-lived CDP socket to the tab (the worker's socket is untouched), waits for a readiness gate (tab off `about:blank`, not on a login URL, `document.readyState === "complete"`, `window._fetchInterceptorActive === true`), runs an optional `page_script`, then each step in order, stopping at the first failure (a failed `screenshot` step excepted — see the table below).

```python
from common.cdp_interceptor import InterceptorClient, parse_actions

client = InterceptorClient(profile_dir=r"C:\temp\enphase",
                           url_patterns=[r"webruntime/api/apex/execute"],
                           on_capture=lambda cap: print(cap.url, cap.body))
client.launch("https://support.enphase.com/feoc-compliance/")
report = client.run_actions(
    parse_actions([
        {"type": "fill", "selector": "textarea[placeholder='Serial number']", "value": "532614044013"},
        {"type": "click", "selector": "button[type=submit]", "text": "Submit"},
        {"type": "wait", "seconds": 10},
    ]),
    ready_timeout_s=60,
)
print(report.to_dict())   # per-step ok / error / value, plus aborted_reason
client.quit()
```

Two options exist for login steps (what `InterceptorClient`'s `login_actions` use): `gate="login"` swaps the readiness gate for one that only needs a real URL and `readyState === "complete"` — it acts *on* a login page instead of refusing it, and does not wait for the capture hook — and `fill_origins=[...]` makes every `fill` check `location.origin` against the list first, failing with `origin <o> not allowed for this fill` before anything is typed. `parse_actions(..., label="login_actions", allowed_types=[...])` names the list in errors and refuses step types outside the set.

Step types:

| Type | Fields | Does |
|---|---|---|
| `wait_for` | `selector`, `text?`, `state` (`visible`\|`attached`), `timeout_s` (15) | Poll until the element is there. |
| `fill` | `selector`, `value`, `text?`, `clear` (true), `timeout_s` | Focus the real input, type with `Input.insertText`, fire `change`. |
| `click` | `selector`, `text?`, `method` (`mouse`\|`js`), `timeout_s` | Trusted mouse click at the element's centre (or `el.click()`). |
| `press` | `key`, `selector?`, `text?`, `timeout_s` | One key down + up, optionally after focusing an element. |
| `select` | `selector`, `value`, `text?`, `timeout_s` | Pick a native `<select>` option by value or text. |
| `wait` | `seconds` | Cancellable sleep. |
| `evaluate` | `script`, `timeout_s` | Run JS, return its JSON value. |
| `screenshot` | `format` (`jpeg`\|`png`\|`webp`), `quality` (80), `full_page` (false), `scale` (1.0, max 2), `max_height` (8000), `timeout_s` (**30**) | Settle (best-effort, ~5 s), then `Page.captureScreenshot` on the same side socket. `value` is `{format, mime_type, width, height, full_page, bytes, page_url, data_base64}`. **A failed screenshot is recorded and the run continues** — every other type stops the run on failure. |
| `scroll` | `selector?`, `text?`, `to?` (`top`\|`bottom`), `by?` (px, ±100000), `block` (`start`\|`center`\|`end`\|`nearest`; selector-only form), `timeout_s` (15, bounds the lookup) | At least one of `selector` / `to` / `by`; `to` and `by` exclusive. `selector` alone: `scrollIntoView` (`block: start` — the element at the top of the frame). `to` / `by` act on the element's scroll container, or with no selector the page's **main scroller** — `document.scrollingElement` when it scrolls, else the largest visible `overflow-y: auto\|scroll\|overlay` element in the document or any open shadow root (Salesforce Lightning scrolls an inner `<div>`). Always `behavior: "instant"`. Nothing to scroll is a no-op (`target: null`). `value` is `{target, element?, scroll_top, scroll_height, client_height, at_bottom}`. `click` / `fill` scroll their element to the centre, so put a `scroll` right before a `screenshot` to frame it. |

Element lookup searches the document **and every open shadow root** (each tree separately — a descendant combinator never crosses a shadow boundary), so Lightning Web Components are reachable; `fill` / `click` / `press` use trusted CDP `Input.*` events. Full reference: [`ai/interceptor/INTERCEPTOR.md` § Page scripts and actions](../../../../../ai/interceptor/INTERCEPTOR.md#page-scripts-and-actions).

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
| `BrowserNotFoundError` on `.launch()` | Windows: set `chrome_path=r"..."` or `CHROME_PATHS_VAR`. Linux/mac: `uv run playwright install chromium`. |
| Status stays `"loading"` forever, no captures | Endpoint might not be JSON, or `url_patterns` doesn't match. Turn on `debug_logging=True`, run visible, open DevTools (F12), watch console. |
| `TimeoutError: Login timed out` | Site's login URL doesn't contain any of the default `login_url_keywords`. Add your site's login/SSO substring to `login_url_keywords`. |
| Data arrives once then never again | Normal for one-shot GETs. Call `.fetch_now()` or interact with the page in visible mode. |
| Second `.launch()` in the same process is a no-op | Expected — one client, one browser. Call `.quit()` first, or use a second `InterceptorClient` with a different `debug_port`. |
| "Session_ok not found" and won't go headless | You haven't had a successful `on_data` delivery yet. Log in, wait for the first parsed hit — sentinel writes automatically. |
| Second concurrent launch hands off URL and exits | Singleton lock on the profile dir. Serialize launches or use separate profile dirs. See [Concurrent access](#concurrent-access). |

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
| `__init__.py` | Public API re-exports; installs `NullHandler`. |
| `client.py` | `InterceptorClient` façade, `Capture`/`ClientState` dataclasses. |
| `launcher.py` | OS-adaptive `find_browser`, `start_browser`, `clear_singleton_locks`, `kill_chrome_by_profile` (Windows-only). |
| `cdp_session.py` | `run_session` — the WebSocket loop that talks to the browser. |
| `screenshot.py` | `capture_screenshot` — page image over a second, short-lived CDP connection. |
| `actions.py` | `run_actions` / `parse_actions` — readiness gate, `page_script`, and shadow-DOM-piercing fill / click / press / select / evaluate / screenshot / scroll steps over a second CDP connection. |
| `sentinel.py` | Session-marker file helpers. |
| `spy.py` | CLI entry point (`cdp-spy` script, or `python -m common.cdp_interceptor.spy`). |
| `interceptor.js` | Injected JS that patches `fetch`/`XHR`. **DO NOT reformat — injected verbatim.** |
