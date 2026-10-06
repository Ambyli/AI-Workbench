# interceptor

Generic HTTP + MCP front-end for `common.cdp_interceptor`. Give it a URL and a list of URL regex patterns; it opens the URL in a headless Chrome under a named `--user-data-dir`, waits for a bounded window, and returns the JSON XHR/fetch bodies whose URLs matched any pattern. It can also return a **screenshot** of the rendered page — either alongside the captures (`screenshot` block on `POST /capture`) or on its own (`POST /screenshot` / the `screenshot_url` MCP tool), so an agent can *look at* a page it navigated to. See [Screenshots](#screenshots). And it can **drive** the page — run a `page_script`, fill inputs, click buttons — for pages that only fire the request you want after someone interacts with them; the responses those actions provoke come back through the same `matches`. See [Page scripts and actions](#page-scripts-and-actions). When a profile's session has expired it can **sign back in** by itself — `login_actions` fill the login form from server-side credentials the request only names (`${password}`). See [Login actions](#login-actions).

- **Container**: `interceptor` (internal only, port 8080 on `ai_shared`)
- **Compose file**: `ai/interceptor/docker-compose.interceptor.yml`
- **Dockerfile**: `ai/interceptor/Dockerfile.interceptor`
- **Source**: `ai/interceptor/{app.py,profiles.py,logins.py}`
- **Login credentials**: `ai/interceptor/logins/<profile>.json` (checked in, bind-mounted read-only; holds only `${ENV:INTERCEPTOR_LOGIN_…}` references to values in `.env` — see [Login actions](#login-actions))
- **LiteLLM integration**: both MCP (`interceptor.capture_url`) and pass-through (`/v1/interceptor/*`), configured in `ai/litellm/litellm_config.yaml`

## Bring it up

```powershell
docker network create ai_shared     # once, if not already present
docker compose -f ai/interceptor/docker-compose.interceptor.yml up --build -d
docker compose -f ai/interceptor/docker-compose.interceptor.yml logs -f interceptor
```

Health:

```powershell
docker exec interceptor curl -s http://localhost:8080/health
```

### Reaching the service

`docker-compose.interceptor.yml` declares **no `ports:` mapping** — port 8080 is reachable only from inside `ai_shared`. There are three ways in, and picking the wrong one is the single most common source of confusion:

| From | How |
|---|---|
| Another container on `ai_shared` | `http://interceptor:8080/...` — what `roofix` uses via `INTERCEPTOR_URL` |
| Your host | `http://localhost:4001/v1/interceptor/...` — the LiteLLM pass-through (`Authorization: Bearer $DEFAULT_LITELLM_MASTER_KEY`). Forwards GET/POST/DELETE including `multipart/form-data` uploads. |
| Your host, bypassing LiteLLM | `docker exec interceptor ...` |

**`http://localhost:8080` from the host does NOT reach the container.** If something answers there it's a *different* interceptor — e.g. a bare-metal `cd ai/interceptor && python app.py` run, which is how you'd drive a visible browser during profile capture. Both instances report `"root": "/data/profiles"` in `GET /profiles`, so responses look identical while pointing at completely separate storage: the bare-metal one uses the host directory, the container one uses the `interceptor_data` volume. Check `size_bytes` / `sentinel_present` to tell them apart, and kill the bare-metal process before uploading to the container.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness |
| `GET` | `/profiles` | List all named profiles (with each one's `login_keys` / `login_origins`) |
| `GET` | `/profiles/{name}` | One profile's status (size, sentinel, `login_keys`, `login_origins`) |
| `POST` | `/profiles/{name}/refresh` | Upload a `.tgz` of a captured Chrome profile |
| `DELETE` | `/profiles/{name}` | Wipe one profile |
| `POST` | `/capture` | Run one capture (see request/response below); optional `screenshot` block returns an image too; optional `page_script` / `actions` drive the page first (see [Page scripts and actions](#page-scripts-and-actions)) |
| `POST` | `/screenshot` | Navigate and return a screenshot only — no XHR patterns (see [Screenshots](#screenshots)) |
| `GET` | `/jobs` | Snapshot of the port pool + currently-running captures |
| `GET` | `/jobs/{job_id}` | Detail on one in-flight capture (404 if not found) |
| `POST` | `/jobs/{job_id}/cancel` | Abort an in-flight capture, reclaim its slot |
| — | `/mcp` | FastMCP HTTP transport (`capture_url`, `screenshot_url`, `list_profiles`, `list_jobs`, `get_job` tools) |

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
  "debug_logging": false,
  "login_url_patterns": ["login", "signin", "/auth"],
  "screenshot": null,
  "page_script": null,
  "actions": [],
  "actions_ready_timeout_seconds": null,
  "stop_when_matched": false,
  "login_actions": [],
  "login_actions_timeout_seconds": null
}
```

`page_script`, `actions`, `actions_ready_timeout_seconds` and `stop_when_matched` are covered in [Page scripts and actions](#page-scripts-and-actions), `login_actions` and `login_actions_timeout_seconds` in [Login actions](#login-actions); with all six at their defaults a capture behaves exactly as it always has.

`screenshot` is opt-in. Set it to an options object — `{"format": "jpeg", "quality": 80, "full_page": false, "scale": 1.0, "max_height": 8000}` (all keys optional) — and the response gains a `screenshot` field holding the image taken just before Chrome quits. With `screenshot` set, `url_patterns` may be empty (screenshot-only navigation); without it, an empty `url_patterns` is a 422. Details in [Screenshots](#screenshots).

`login_url_patterns` are regexes `re.search`-matched against the tab's `location.href` **after** navigation has settled, to detect a redirect to a login wall. Two things to know:

- **Supplying the field replaces the defaults** (`["login", "signin", "/auth"]`) — it does not merge. Include them yourself if you still want them.
- **They're full regexes, not substrings**, so you can anchor one at a bare domain that a substring couldn't tell apart from an in-app URL: `^https?://roofix\.io/?$` matches the logged-out root but not `roofix.io/project/…`. Anchor with `/?$` rather than `$` — `location.href` for a bare domain is always normalized with a trailing slash, so `^https?://roofix\.io$` never matches anything.

An empty list disables login detection entirely.

`capture_window_seconds` is a **hard wall**: `app.py` waits exactly that long and then quits Chrome, regardless of what stage the session is in — unless `stop_when_matched` ends it early, or it is cancelled. It must exceed `login_timeout` for a login to have any chance of resolving — otherwise the window closes first and the response comes back `login_wall: true` with `status="waiting_login"`.

Chrome always runs headless in the container. The service writes a `session_ok` sentinel into each uploaded profile so `InterceptorClient` boots straight into headless — an operator only uploads a profile *after* logging in on their laptop, so treating uploaded profiles as session-ready by definition matches reality. If the persisted session expires, `InterceptorClient` detects the login redirect, sets `status="waiting_login"`, and the response comes back with `login_wall: true`; refresh the profile via `POST /profiles/{name}/refresh` and retry — or, for a site with a plain username/password form, send [`login_actions`](#login-actions) and let the capture sign in itself.

`login_wall: true` does **not** always mean the session expired. A profile whose cookies were encrypted against a key the container doesn't have produces exactly the same result — Chrome reads the rows, silently fails to decrypt, and browses as an anonymous user. If a profile that demonstrably works on the capture machine hits a login wall in the container, check the cookie encryption version before re-capturing: see [Cookie encryption and portability](#cookie-encryption-and-portability).

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
  "captured_urls": [ "https://…", "…" ],
  "screenshot": null,
  "screenshot_error": null,
  "actions_report": null,
  "login_actions_report": null,
  "ended_early": false
}
```

`actions_report` is `null` unless `page_script` or `actions` were sent (shape in [Page scripts and actions § The report](#the-report)); `login_actions_report` (same shape) is `null` unless `login_actions` were sent **and** the capture hit a login wall; `ended_early` is `true` only when `stop_when_matched` closed the window before `capture_window_seconds` elapsed.

`screenshot` is `null` unless requested; `screenshot_error` is set (and `screenshot` stays `null`) when one was requested but could not be taken — the XHR captures are still returned, an image failure never fails the capture.

`job_id` is a 12-char hex identifier for the capture. During the request's lifetime it shows up in `GET /jobs` (see [Observability](#observability)) and is prefixed onto every log line emitted by that capture — useful for correlating interleaved logs when concurrent captures are running.

Patterns are `re.search`-matched against every JSON XHR/fetch URL the page emits. A capture whose URL matches multiple patterns lands in the bucket of the **first** matching pattern.

## Screenshots

Two ways to get an image of the page:

- **`POST /screenshot`** (MCP: `screenshot_url`) — navigate under a profile, wait `wait_seconds`, return the image. No `url_patterns`. Use it when the point is to *see* the page: layout, a chart, an error banner, whatever isn't in an XHR body.
- **`screenshot` block on `POST /capture`** (MCP: `capture_url` with `screenshot=true`) — same image, taken at the end of the capture window, returned next to the XHR matches.

### `POST /screenshot`

Request:

```json
{
  "url": "https://example.com/dashboard",
  "profile": "example",
  "wait_seconds": 15,
  "format": "jpeg",
  "quality": 80,
  "full_page": false,
  "scale": 1.0,
  "max_height": 8000,
  "login_timeout": 300,
  "login_url_patterns": ["login", "signin", "/auth"],
  "login_actions": [],
  "login_actions_timeout_seconds": null
}
```

| Field | Default | Notes |
|---|---|---|
| `wait_seconds` | `INTERCEPTOR_SCREENSHOT_WAIT_SECONDS` (15) | How long the page gets to render. Chrome spends the first ~4–5 s booting and navigating, so values under ~8 mostly return blank or half-painted pages. Hard wall, same as `capture_window_seconds`. |
| `login_actions` / `login_actions_timeout_seconds` | `[]` / `null` (60) | Same as on `POST /capture` — see [Login actions](#login-actions). `wait_seconds` must then cover the login, the redirect and the page load; the response gains `login_actions_report`. |
| `format` | `jpeg` | `jpeg` (~100–300 KB for a viewport), `png` (lossless, often 1–3 MB), `webp`. |
| `quality` | `80` | jpeg/webp only; ignored for png. |
| `full_page` | `false` | Whole scrollable document instead of the 1920×1080 headless viewport. |
| `scale` | `1.0` | Output scale, `0 < scale ≤ 2`. `0.5` halves each axis and roughly quarters the payload — the right default when the image is going into a model context. |
| `max_height` | `8000` | `full_page` height clamp in CSS px. Chrome refuses clips beyond 16384. |

Response:

```json
{
  "job_id": "a3f2b1c9d4e5",
  "url": "https://example.com/dashboard",
  "status": "loading",
  "login_wall": false,
  "error": null,
  "screenshot": {
    "format": "jpeg",
    "mime_type": "image/jpeg",
    "width": 1904,
    "height": 929,
    "full_page": false,
    "bytes": 21655,
    "page_url": "https://example.com/dashboard",
    "data_base64": "/9j/4AAQ…"
  },
  "screenshot_error": null,
  "login_actions_report": null
}
```

Things to know:

- **`status: "loading"` is normal here.** `status` is the interceptor's *capture* status; a page that fires no JSON XHRs (static pages, `example.com`) never reaches `"ok"`. Look at `screenshot` / `screenshot_error`, not `status`, to judge the screenshot.
- **`page_url` is where the tab actually ended up.** A redirect to a login page shows here even when `login_url_patterns` didn't recognise it — and the image is of that login page, which is useful evidence in itself.
- **`width`/`height` are the requested clip × `scale`**, not decoded from the image; Chrome's rounding can differ by a pixel. The headless viewport is `--window-size=1920,1080` minus browser chrome, so expect ~1904×929 at `scale: 1.0`.
- **Same slot accounting as `/capture`.** A screenshot job takes one port-pool slot for `wait_seconds`, shows up in `GET /jobs` (phase `capturing` → `screenshot` → `cleaning_up`), and can be cancelled the same way. Pool exhausted → 429.

### How it works

`run_session` owns the CDP WebSocket to the tab and runs it on a private worker thread, so nothing else can issue commands on it. Chrome allows many debugger clients per target and `Page.captureScreenshot` needs no `Page.enable`, so the screenshot opens its **own** short-lived WebSocket to the same tab (`shared/common/src/common/cdp_interceptor/screenshot.py`), waits up to 5 s for `document.readyState == "complete"`, reads `Page.getLayoutMetrics`, captures, and closes. The interceptor session never notices. `InterceptorClient.screenshot()` is the thread-safe entry point; `app.py` calls it after `capture_window_seconds` elapses and before `client.quit()`.

The helper connects by `127.0.0.1` rather than `localhost` (on Windows `localhost` resolves to `::1` first and Chrome only listens on IPv4 — a measured 2 s penalty per connection) while pinning `Origin: http://localhost:<port>` so `--remote-allow-origins` still accepts the handshake.

### On MCP

`capture_url` and `screenshot_url` return the image as an MCP **`ImageContent` block** next to the JSON text block, so a multimodal model can actually look at it. The JSON carries only the metadata (`format`, `mime_type`, `width`, `height`, `full_page`, `bytes`, `page_url`) — the base64 is *not* duplicated into the text. HTTP callers get `data_base64` inline instead.

Payload budgeting for a model context: a 1920×1080 viewport JPEG at quality 80 is ~100–300 KB; `scale: 0.5` brings it to ~30–80 KB with the layout still readable. Prefer `full_page: false` unless the content below the fold is the point.

## Page scripts and actions

A plain capture only *watches*: it returns the JSON the page fetches on its own. Some pages only make the call you want after a person types into a form and clicks a button — a lookup page, a search box, a "load more". Four optional `POST /capture` fields (also on the `capture_url` MCP tool) cover that:

| Field | Default | What it does |
|---|---|---|
| `page_script` | `null` | JS evaluated in the page once it is ready, before any action. Its JSON-serialisable result comes back as `actions_report.page_script.value`. Use it to *inspect* the page — e.g. list the form fields before writing `actions` (see the [discovery example](#worked-example-discover-the-form-then-run-the-lookup)). |
| `actions` | `[]` | Ordered steps — `wait_for`, `fill`, `click`, `press`, `select`, `wait`, `evaluate` — run once the page is ready. Stops at the first failed step. |
| `actions_ready_timeout_seconds` | `capture_window_seconds` | How long the [readiness gate](#readiness-gate) may wait before the steps are abandoned. |
| `stop_when_matched` | `false` | End the window as soon as every `url_patterns` bucket has at least one match and the actions (if any) have finished, instead of waiting out `capture_window_seconds`. Needs at least one pattern (422 otherwise). |

### Why drive the form instead of calling the API from `page_script`

It is tempting to have `page_script` call the endpoint directly with `fetch()`. On sites that sign their API calls this does not work: a Salesforce Experience Cloud (LWR) site's `webruntime/api/apex/execute` refuses a hand-made request with **401** because the `csrf-token` header is added by the LWR runtime itself, from state a page script cannot easily reach. Filling the input and clicking the button makes the page's *own* code send the request with every header it needs — and since `interceptor.js` has already wrapped `window.fetch` / XHR, the response lands in `matches` like any other capture. Nothing about capture changes; the actions only cause the request.

### Step types

| `type` | Fields (`?` = optional) | What happens |
|---|---|---|
| `wait_for` | `selector`, `text?`, `state?` (`visible` \| `attached`, default `visible`), `timeout_s?` | Polls the [deep query](#finding-elements) every 250 ms until a match is in that state. |
| `fill` | `selector`, `value`, `text?`, `clear?` (default `true`), `timeout_s?` | Waits for a visible match, focuses the real `<input>`/`<textarea>` (descending into a component host's shadow root when the selector hit the host, e.g. `lightning-input`), empties it (native value setter + `input` event), types `value` with CDP **`Input.insertText`** — trusted `beforeinput`/`input` events, which is what LWC and React listen to — then fires `change` (`bubbles`, `composed`) and blurs. Fails if focus doesn't land. |
| `click` | `selector`, `text?`, `method?` (`mouse` \| `js`, default `mouse`), `timeout_s?` | `mouse`: scrolls the element to the centre of the viewport and sends `Input.dispatchMouseEvent` moved / pressed / released (left button, `clickCount: 1`) at the centre of its box — a trusted click, so whatever is on top at that point receives it. `js`: `el.click()`, for an element a modal or overlay covers. |
| `press` | `key`, `selector?`, `text?`, `timeout_s?` | `Input.dispatchKeyEvent` keyDown + keyUp with the right `code` / `windowsVirtualKeyCode`. Keys: `Enter`, `Tab`, `Escape`, `Backspace`, `Delete`, `Space`, `ArrowUp/Down/Left/Right`, `Home`, `End`, `PageUp`, `PageDown`, or any single character. Goes to the focused element — `fill` blurs its field when done, so pass `selector` to focus a field before pressing `Enter` in it. |
| `select` | `selector`, `value`, `text?`, `timeout_s?` | Native `<select>` only: picks the option whose value — or, failing that, visible text — equals `value`, fires `input` + `change`. A `lightning-combobox` is not a `<select>`: `click` it open, then `click` the option by `text`. |
| `wait` | `seconds` | Sleeps (0–600 s, cancellable). |
| `evaluate` | `script`, `timeout_s?` | `Runtime.evaluate` with `awaitPromise`, `returnByValue`, `userGesture`. The value comes back in that step's `value`; a thrown error fails the step with its message. |

`timeout_s` defaults to **15** per step (max 600). Unknown step types and missing fields are a **422**; an unknown `press` key is a **400** — both before a port is taken.

**Scripts are expressions.** `page_script` and `evaluate.script` are evaluated as given, not wrapped. `document.title` works; several statements need an async IIFE — `(async () => { const x = …; return x; })()`. Promises are awaited. Return plain data (objects, arrays, strings): DOM nodes and functions don't survive `returnByValue`.

### Finding elements

`selector` is ordinary CSS, but it is matched in `document` **and inside every open shadow root**, recursively. Salesforce Lightning Web Components (and most custom-element design systems) render inside native shadow DOM, where `document.querySelector` finds nothing — `input`, `button`, `[placeholder="Serial Number"]`, `lightning-input` all work here because each shadow tree is searched on its own. Because each tree is searched **separately, a combinator never crosses a shadow boundary**: `lightning-input input` matches nothing (the `<input>` lives in the host's shadow tree, not under it), and `c-form lightning-input` only matches when both sit in the same tree. Select either the inner element with an attribute (`input[name="serial"]`) or the component host itself (`lightning-input`) — `fill`, `press` and `select` descend from a host into its shadow tree to the real control — and narrow with `text` rather than long descendant chains. The discovery script's `hosts` column tells you which host an element is inside.

- **`text`** narrows the matches: a substring of the element's trimmed, whitespace-collapsed text (including text rendered in its own shadow tree, so `lightning-button` + `text: "Check"` works), its `value`, `aria-label`, `placeholder` or `title`. Written `/…/flags` it is a JS regex: `"/check\\s+serial/i"`.
- **The first visible match wins.** Visible = a non-empty bounding box and not `display: none` / `visibility: hidden` (a `display: contents` host borrows its first visible child's box). `state: "attached"` on `wait_for` accepts a hidden match.
- **A miss says why**: `nothing matches selector "…"`, `3 matched the selector, 0 of those matched the text`, `2 matched the selector, none visible` — usually enough to fix the selector without re-running discovery.
- Closed shadow roots and iframes are out of reach.

### Readiness gate

No `page_script` and no step runs until **all** of these hold:

1. the tab URL is not `about:blank`;
2. it does not match `login_url_patterns` (the same regexes the capture uses — so nothing in `actions` or `page_script` is ever typed into a login form; the only way to act on a login page is [`login_actions`](#login-actions), which have their own gate);
3. `document.readyState === "complete"`;
4. `window._fetchInterceptorActive === true` — the guard `interceptor.js` sets when it wraps `fetch`/XHR, proving the capture hook is installed **before** anything is clicked.

The gate polls for up to `actions_ready_timeout_seconds`. In a visible run (a profile with no sentinel) this is what waits for a human to log in; headless against an expired session it simply times out, `actions_report.aborted_reason` reads `not ready: login page https://… (gave up after …s)`, and the response has `login_wall: true` as usual.

LWC components often render a beat after `readyState` reaches `complete`. Steps poll, so that is harmless for `wait_for`/`fill`/`click`; a `page_script` runs once, so poll inside it (as the discovery example does) or use a `wait_for` followed by an `evaluate` step.

### Timing, navigation, and `stop_when_matched`

The actions run on a helper thread started right after Chrome launches, on their **own** CDP connection to the tab (`shared/common/src/common/cdp_interceptor/actions.py`, the same second-socket pattern as [Screenshots](#how-it-works)) — so the capture session's socket is untouched. The job's phase is `actions` while they run (`GET /jobs` metadata shows `actions_done` / `actions_total`) and back to `capturing` after. The window keeps counting the whole time: **`capture_window_seconds` must cover the login (if any), the gate, and every step.**

- **A step that navigates** (a submit that reloads) is handled: the next lookup sees the old execution context vanish, re-waits the readiness gate once and retries; a dropped side socket is reopened on the next call. A `page_script` / `evaluate` whose document is destroyed while it runs is re-run once on the new document after the gate passes again — this matters after a visible login, where the gate can pass on the post-login landing page a moment before the capture session re-navigates to the target URL.
- **The first failure stops the run.** Later steps are reported `ok: false, error: "skipped"`. The capture itself still completes normally — matches, screenshot, cleanup.
- **When the window ends first**, the running step is stopped and `aborted_reason` reads `capture window ended before actions finished`. `POST /jobs/{id}/cancel` stops them too (`aborted_reason: "cancelled"`).
- **`stop_when_matched`** checks four times a second: actions done (or none requested) **and** every pattern has ≥ 1 match → the window ends, `ended_early: true`, and the screenshot (if requested) and cleanup run as normal. **A click is "done" when the mouse button is released, not when its request returns.** If the page also makes matching calls on its own during load — a Salesforce site's every Apex call goes to the same `webruntime/api/apex/execute` URL — a load-time match can satisfy the condition the moment the click returns, before the response you wanted arrives. End such `actions` with a `wait_for` on the element that renders the result, so the actions only finish once the answer is on screen.

### The report

```json
"actions_report": {
  "page_script": { "index": -1, "type": "page_script", "ok": true, "elapsed_ms": 412, "error": null, "value": [ … ] },
  "actions": [
    { "index": 0, "type": "fill",     "ok": true,  "elapsed_ms": 220, "error": null, "value": { "element": "<input id=\"input-12\" name=\"serial\">", "value_length": 12 } },
    { "index": 1, "type": "click",    "ok": true,  "elapsed_ms": 95,  "error": null, "value": { "element": "<button>", "x": 961.5, "y": 412.0 } },
    { "index": 2, "type": "wait_for", "ok": false, "elapsed_ms": 15004, "error": "timed out after 15s waiting for '.result' (visible): nothing matches selector \".result\"", "value": null },
    { "index": 3, "type": "evaluate", "ok": false, "elapsed_ms": 0, "error": "skipped", "value": null }
  ],
  "aborted_reason": "actions[2] (wait_for) failed: timed out after 15s waiting for '.result' (visible): …"
}
```

`index` is the step's position in `actions` (`-1` for `page_script`). `aborted_reason` is `null` when every step ran and succeeded. `value` for DOM steps describes the element acted on; for `evaluate` / `page_script` it is the script's return value.

### Login actions

A profile's SSO session eventually expires. Without help, the capture then lands on the login page, waits, returns `login_wall: true` — and every lookup on that profile fails until someone re-captures and re-uploads it. For a site with a plain username/password form (no verification code), `login_actions` let the capture sign back in by itself with a dedicated service account:

| Field | Default | What it does |
|---|---|---|
| `login_actions` | `[]` | Steps that run **only** if the tab hits a login wall (`login_url_patterns`), once per capture. A `fill` value may reference the profile's stored credentials as `${username}`, `${password}`, … Needs a non-empty `login_url_patterns` (422 otherwise). |
| `login_actions_timeout_seconds` | `null` (60) | Upper bound on one login run: the login page becoming ready plus every step. 1–600. |

Also on `POST /screenshot` and on both the `capture_url` and `screenshot_url` MCP tools (`login_actions` only there; the timeout keeps its default).

#### The credentials file

One file per profile, `ai/interceptor/logins/<profile>.json` on the host — bind-mounted **read-only** at `/config/logins` and found through `INTERCEPTOR_LOGINS_DIR`:

```json
{
  "allowed_origins": ["https://sso.enphaseenergy.com"],
  "values": {
    "username": "${ENV:INTERCEPTOR_LOGIN_ENPHASE_USERNAME}",
    "password": "${ENV:INTERCEPTOR_LOGIN_ENPHASE_PASSWORD}"
  }
}
```

with the credentials themselves in `.env`:

```bash
INTERCEPTOR_LOGIN_ENPHASE_USERNAME=svc-enphase@zeoenergy.com
INTERCEPTOR_LOGIN_ENPHASE_PASSWORD=…
```

- **`allowed_origins` is required and non-empty.** A `fill` in `login_actions` only types into a tab whose `location.origin` is on this list — the password-manager rule. Without it a request could point `login_url_patterns` at `.*` and have the password typed into any site's form. Write bare origins (`scheme://host[:port]`); they are normalised to what `location.origin` reports (lowercase, default port dropped).
- **`values`** maps reference names (letters, digits, `_`) to **environment references**, `${ENV:INTERCEPTOR_LOGIN_<NAME>}`. A literal credential is refused (the error never echoes it), and only the `INTERCEPTOR_LOGIN_` prefix is resolvable, so a logins file can't pull any other secret in the environment into a fill. The file holds no secrets, so it is **checked in**.
- **Each variable is set in `.env` and listed in the `environment:` block of `docker-compose.interceptor.yml`** (as `${VAR:-}`, so a missing pair only fails that profile). A variable that is unset or empty in the container is a 400 naming the variable. Write a literal `$` in a `.env` value as `$$` — compose interpolates it. Use an account that exists for this and nothing else.
- **Adding a profile's login:** add `ai/interceptor/logins/<profile>.json`, add its `INTERCEPTOR_LOGIN_<PROFILE>_*` lines to `.env` / `.env.example` and the compose `environment:` block, then `make up interceptor` (a recreate — the container env changed).
- **Re-read on every request** — editing the file needs no restart; changing a `.env` value needs `make up interceptor` to reach the container.
- **Not under `INTERCEPTOR_PROFILES_ROOT`**, deliberately: a profile refresh `rmtree`s the profile directory, and anything under the root is listed as a profile.

`GET /profiles` (and the `list_profiles` MCP tool) show each profile's `login_keys` (the reference names) and `login_origins` — never a value — so a caller can see which `${…}` it may use. An unusable file adds `login_error`.

#### References

- `${key}` is replaced by `values.key` — **only in the `value` of a `fill` step inside `login_actions`**.
- `$$` is a literal `$`; a `$` followed by anything other than `{` or `$` is literal too (so a CSS `[name$=user]` selector is fine).
- A `${` anywhere else in `login_actions` (a selector, a `text` filter, a `select` value) is a **400**.
- In the regular `actions` and `page_script`, `${…}` is plain text and is **never** resolved.

#### Allowed steps

`wait_for`, `fill`, `click`, `press`, `select`, `wait` — **no `evaluate`** (a 422): a script on the login page could read the password field back into the report.

#### What happens

1. The capture navigates; once the URL settles on one that matches `login_url_patterns`, the session reports `waiting_login` and runs `login_actions` on their own CDP connection, behind a **login gate**: a real URL and `document.readyState === "complete"` — unlike the [readiness gate](#readiness-gate) it does not refuse login URLs (that is the point) and does not wait for the capture hook.
2. Every `fill` first checks `location.origin` against `allowed_origins` — and again, atomically with focusing the field, right before typing. A mismatch fails the step with `origin <o> not allowed for this fill`; nothing is typed.
3. When the steps finish, the session's usual login wait starts (`login_timeout`, measured from then), sees the tab leave the login URL, and re-navigates to `url` — exactly as after a human login. `actions`, whose gate has been waiting out the login page, then run on the target page as usual.
4. **One attempt per capture, never retried.** A wrong password or a changed form fails the run once; the usual wait then runs out exactly as it does today, so a bad credential can't lock the account by looping.
5. Two captures on the same profile never submit the form at the same time (a per-profile login lock), whichever of the fast and slow paths they are on.

**Errors at submit** — all **400**, before a port is taken, and every one names a step or a key, never a value: the profile has no logins file; a value is not an `${ENV:INTERCEPTOR_LOGIN_…}` reference, or names a variable that is unset or empty; a referenced key is missing; `allowed_origins` is missing, empty or not an origin; a `${` outside a fill value; a malformed reference (`${pass`, `${1x}`); an invalid step (e.g. an unknown `press` key).

**Where the secret goes:** into the in-memory steps handed to the browser, and nowhere else. The request — and so every log line, `GET /jobs` record and FastAPI 422 echo — only ever contains `${password}`. A `fill` result reports `{element, value_length}`, never the text. The job log records one line, `login_actions ok=<bool> failed_at=<index/type>`.

#### The report

`login_actions_report` has the same shape as [`actions_report`](#the-report), with steps named `login_actions[i]` in `aborted_reason`. It is `null` unless `login_actions` were sent **and** the capture hit a login wall — so a `null` on a run that sent them is proof the stored session was still good. Typical `aborted_reason` values: `login_actions[1] (fill) failed: origin https://… not allowed for this fill`, `login_actions[0] (fill) failed: timed out after 15s waiting for 'input[type=email]' …` (the form changed — fix the selector), `login_actions did not finish within 60s`, `capture window ended before login_actions finished`.

`login_wall` keeps its meaning: `true` only if the session is still waiting on the login page when the window ends. After a login resolves the status goes back to `loading`, so a target page that fires no JSON no longer reads as a login wall.

#### Timing

Everything happens inside `capture_window_seconds`: the login page loading, the steps, the SSO redirect back, the target page loading, then `actions`. Budget **~120 s** for Enphase. `login_actions_timeout_seconds` caps the login run itself; the `login_timeout` wait after it only matters when a login fails.

#### Fast path vs slow path

On the **fast path** Chrome writes the new session cookies into the base profile, so the next capture finds a valid session and `login_actions_report` comes back `null`. On the **slow path** (same profile already in use, see [Concurrency](#concurrency)) the cookies land in the temporary clone and are discarded — that capture works, but the next one logs in again. A successful capture also rewrites the `session_ok` sentinel.

One caveat: a session expiry that happened **before** `login_actions` were in use has already deleted `session_ok` (the `login_timeout` path does that), and without it Chrome launches visibly — which in the container fails for lack of a display. Re-upload the profile once (any old cookie set will do; `login_actions` log it back in) to restore the sentinel. A failed login that sits out a `login_timeout` shorter than the window does the same thing, exactly as before.

#### Enphase

```json
"login_url_patterns": ["login", "signin", "/auth", "sso\\.enphaseenergy\\.com"],
"login_actions": [
  {"type": "fill", "selector": "#username", "value": "${username}"},
  {"type": "fill", "selector": "#password", "value": "${password}"},
  {"type": "click", "selector": "input[type=submit].button"}
]
```

with `ai/interceptor/logins/enphase.json` holding `allowed_origins: ["https://sso.enphaseenergy.com"]`. The selectors were read off the live SSO form (2026-10-06): `https://sso.enphaseenergy.com/login`, one page with `#username` ("Enlighten Username"), `#password` and an `<input type=submit class="button">` "Log in" — no iframe, no shadow DOM. If Enphase changes the form, a failed step's error says which selector missed. The full lookup with these steps is in the [worked example](#worked-example-discover-the-form-then-run-the-lookup).

### Worked example: discover the form, then run the lookup

**1 — Discovery.** Load the page with a `page_script` that waits (up to 15 s) for an input to render, walks every open shadow root, and returns each `input` / `textarea` / `select` / `button` / `[role=button]` with the details you need to write selectors — `hosts` is the chain of custom-element hosts it sits inside (e.g. `c-feoc-form > lightning-input`). Add a screenshot to see what you're looking at. For a profile that has never logged in, Chrome opens visibly: log in in that window and the gate waits for you (keep `login_timeout` below `capture_window_seconds`). The readable source:

```js
(async () => {
  const SEL = 'input, textarea, select, button, [role=button]';
  const roots = () => {
    const out = [document], stack = [document];
    while (stack.length) {
      for (const el of stack.pop().querySelectorAll('*')) {
        if (el.shadowRoot) { out.push(el.shadowRoot); stack.push(el.shadowRoot); }
      }
    }
    return out;
  };
  const find = () => roots().flatMap(r => Array.from(r.querySelectorAll(SEL)));
  const until = Date.now() + 15000;
  let els = find();
  while (!els.some(e => e.matches('input, textarea')) && Date.now() < until) {
    await new Promise(r => setTimeout(r, 500));
    els = find();
  }
  const txt = n => (n && (n.innerText || n.textContent) || '').replace(/\s+/g, ' ').trim();
  const labelOf = el => {
    if (el.labels && el.labels.length) return txt(el.labels[0]);
    const by = el.getAttribute('aria-labelledby');
    if (by) {
      const root = el.getRootNode();
      const t = by.split(' ').map(id => root.getElementById(id)).filter(Boolean).map(txt).join(' ');
      if (t) return t;
    }
    return el.getAttribute('aria-label') || '';
  };
  const hosts = el => {
    const out = [];
    for (let r = el.getRootNode(); r && r.host; r = r.host.getRootNode()) out.unshift(r.host.tagName.toLowerCase());
    return out.join(' > ');
  };
  return els.map(el => {
    const r = el.getBoundingClientRect();
    return {
      tag: el.tagName.toLowerCase(),
      type: el.getAttribute('type') || '',
      name: el.getAttribute('name') || '',
      id: el.id || '',
      label: labelOf(el),
      aria_label: el.getAttribute('aria-label') || '',
      placeholder: el.getAttribute('placeholder') || '',
      text: (txt(el) || el.value || '').slice(0, 80),
      visible: r.width > 0 && r.height > 0,
      hosts: hosts(el)
    };
  });
})()
```

The same script as a ready-to-send request body (the `page_script` value is that source, JSON-escaped):

```json
{
  "url": "https://support.enphase.com/feoc-compliance/",
  "url_patterns": ["webruntime/api/apex/execute"],
  "profile": "enphase",
  "capture_window_seconds": 300,
  "login_timeout": 240,
  "login_url_patterns": ["login", "signin", "/auth", "sso\\.enphaseenergy\\.com"],
  "screenshot": {"full_page": true, "scale": 0.5},
  "page_script": "(async () => {\n  const SEL = 'input, textarea, select, button, [role=button]';\n  const roots = () => {\n    const out = [document], stack = [document];\n    while (stack.length) {\n      for (const el of stack.pop().querySelectorAll('*')) {\n        if (el.shadowRoot) { out.push(el.shadowRoot); stack.push(el.shadowRoot); }\n      }\n    }\n    return out;\n  };\n  const find = () => roots().flatMap(r => Array.from(r.querySelectorAll(SEL)));\n  const until = Date.now() + 15000;\n  let els = find();\n  while (!els.some(e => e.matches('input, textarea')) && Date.now() < until) {\n    await new Promise(r => setTimeout(r, 500));\n    els = find();\n  }\n  const txt = n => (n && (n.innerText || n.textContent) || '').replace(/\\s+/g, ' ').trim();\n  const labelOf = el => {\n    if (el.labels && el.labels.length) return txt(el.labels[0]);\n    const by = el.getAttribute('aria-labelledby');\n    if (by) {\n      const root = el.getRootNode();\n      const t = by.split(' ').map(id => root.getElementById(id)).filter(Boolean).map(txt).join(' ');\n      if (t) return t;\n    }\n    return el.getAttribute('aria-label') || '';\n  };\n  const hosts = el => {\n    const out = [];\n    for (let r = el.getRootNode(); r && r.host; r = r.host.getRootNode()) out.unshift(r.host.tagName.toLowerCase());\n    return out.join(' > ');\n  };\n  return els.map(el => {\n    const r = el.getBoundingClientRect();\n    return {\n      tag: el.tagName.toLowerCase(),\n      type: el.getAttribute('type') || '',\n      name: el.getAttribute('name') || '',\n      id: el.id || '',\n      label: labelOf(el),\n      aria_label: el.getAttribute('aria-label') || '',\n      placeholder: el.getAttribute('placeholder') || '',\n      text: (txt(el) || el.value || '').slice(0, 80),\n      visible: r.width > 0 && r.height > 0,\n      hosts: hosts(el)\n    };\n  });\n})()"
}
```

Without `stop_when_matched` this run lasts the full 300 s (time to log in). Add `"stop_when_matched": true` once the profile is logged in, and it returns as soon as the page script is done and the page has made its first Apex call.

**2 — The lookup.** Discovery on the live page (2026-10-02) found the serial field as a `<textarea placeholder="Serial number">` inside `c-feoc-parent-comp > c-feoc-serial-num`, and a `<button type="submit">Submit</button>` in `c-feoc-parent-comp`. This body is **verified** — it returns the `submitSerialNumbers` result in ~10 s:

```json
{
  "url": "https://support.enphase.com/feoc-compliance/",
  "url_patterns": ["webruntime/api/apex/execute"],
  "profile": "enphase",
  "capture_window_seconds": 60,
  "login_timeout": 30,
  "login_url_patterns": ["login", "signin", "/auth", "sso\\.enphaseenergy\\.com"],
  "stop_when_matched": true,
  "actions": [
    {"type": "fill", "selector": "textarea[placeholder='Serial number']", "value": "532614044013"},
    {"type": "evaluate", "script": "(() => { window.__ciN0 = (window._capturedResponses || []).length; return window.__ciN0; })()"},
    {"type": "click", "selector": "button[type=submit]", "text": "Submit"},
    {"type": "evaluate", "script": "(async () => { const until = Date.now() + 30000; while (Date.now() < until) { const c = (window._capturedResponses || []).slice(window.__ciN0 || 0).filter(x => /webruntime\\/api\\/apex\\/execute/.test(x.url)); if (c.length) return {new_apex_responses: c.length}; await new Promise(r => setTimeout(r, 250)); } throw new Error('no new apex/execute response within 30s of Submit'); })()", "timeout_s": 35},
    {"type": "wait", "seconds": 2}
  ]
}
```

The page makes three Apex calls of its own on load (session check, current user, disclaimer text), all on the same `webruntime/api/apex/execute` URL — so `stop_when_matched` is satisfied before Submit is ever clicked, and the actions have to hold the window open until the lookup's own response exists. The two `evaluate` steps do that generically, without knowing anything about the result UI: the first records how many responses `interceptor.js` has captured so far (`window._capturedResponses`), the second waits for a new Apex one to appear after the click. The trailing `wait` gives the capture side time to receive it — the bytes are in the page at that point, but delivery to the service rides the CDP binding (near-instant) with a 5 s poll as fallback, and the window would otherwise close on the load-time matches first.

The answer is the match whose `returnValue` is a JSON **string** of an array (the Apex method returns serialised JSON). Clicking Submit also fetches the definitions text, so it is not simply the last match:

```json
{"returnValue": "[{\"sn\":\"532614044013\",\"feoc\":\"Yes\",\"sku\":\"IQ8HC-72-M-DOM-US\",\"message\":\"\",\"isChild\":false}]", "cacheable": false}
```

Several serials can go in one `fill` value separated by newlines or commas (the page says so); each comes back as one element of that array.

**3 — The lookup, signing in when the session has expired.** The same body plus [`login_actions`](#login-actions) and a window long enough for the SSO round trip. Needs `ai/interceptor/logins/enphase.json` (see [The credentials file](#the-credentials-file)); the login selectors were read off the live SSO form:

```json
{
  "url": "https://support.enphase.com/feoc-compliance/",
  "url_patterns": ["webruntime/api/apex/execute"],
  "profile": "enphase",
  "capture_window_seconds": 120,
  "login_timeout": 30,
  "login_url_patterns": ["login", "signin", "/auth", "sso\\.enphaseenergy\\.com"],
  "login_actions": [
    {"type": "fill", "selector": "#username", "value": "${username}"},
    {"type": "fill", "selector": "#password", "value": "${password}"},
    {"type": "click", "selector": "input[type=submit].button"}
  ],
  "stop_when_matched": true,
  "actions": [
    {"type": "fill", "selector": "textarea[placeholder='Serial number']", "value": "532614044013"},
    {"type": "evaluate", "script": "(() => { window.__ciN0 = (window._capturedResponses || []).length; return window.__ciN0; })()"},
    {"type": "click", "selector": "button[type=submit]", "text": "Submit"},
    {"type": "evaluate", "script": "(async () => { const until = Date.now() + 30000; while (Date.now() < until) { const c = (window._capturedResponses || []).slice(window.__ciN0 || 0).filter(x => /webruntime\\/api\\/apex\\/execute/.test(x.url)); if (c.length) return {new_apex_responses: c.length}; await new Promise(r => setTimeout(r, 250)); } throw new Error('no new apex/execute response within 30s of Submit'); })()", "timeout_s": 35},
    {"type": "wait", "seconds": 2}
  ]
}
```

Expect `login_actions_report.aborted_reason: null` and `login_wall: false` when it had to sign in, and `login_actions_report: null` on the next run (the fast path kept the new cookies). `actions_ready_timeout_seconds` defaults to the window, so the serial-number steps wait out the whole login.

## Concurrency

The service handles many `/capture` calls in parallel. Two knobs shape the behavior:

- **Different profiles fully parallel.** A `/capture` against `profile=roofix` and one against `profile=gmail` never contend on each other.
- **Same profile, fast + slow path.** The first same-profile request in flight takes the fast path — Chrome runs against the base `--user-data-dir` and refreshed session cookies persist to disk. Any concurrent same-profile request falls into the slow path — the service takes a live snapshot of the base profile via `shutil.copytree` into `PROFILES_ROOT/.temp/temp_profile_<uuid>/`, launches Chrome against the clone, and deletes the clone on completion. Raw copy of a live profile is safe: Chrome's SQLite (`Cookies`) and LevelDB (`Local Storage`, `IndexedDB`) stores use journal-based crash recovery, so a mid-write snapshot at worst yields a slightly-stale-but-consistent state, never corruption.
- **Port pool caps total concurrency.** A bounded pool of CDP debug ports (starting at `INTERCEPTOR_DEBUG_PORT`, sized by `INTERCEPTOR_MAX_CONCURRENT`) is the hard resource ceiling. Every capture — fast or slow — grabs one port from the pool at start and returns it at end. Pool exhausted → **HTTP 429 Too Many Requests** with `retry-later` semantics.

### Resource sizing

Each concurrent slot holds one running Chrome (~200–400 MB RAM) plus, when in a same-profile collision, a live profile-dir clone (typically 20–80 MB disk in `PROFILES_ROOT/.temp`). Setting `INTERCEPTOR_MAX_CONCURRENT=32` means budgeting ~10–13 GB of RAM and ~1–3 GB of ephemeral disk headroom for a fully-saturated fleet. Scale the container's memory limit and volume size accordingly.

### Crash recovery

If Chrome crashes mid-capture (OOM, segfault, container killed) and leaves a stale `SingletonLock` in the base profile dir, `InterceptorClient.launch` clears the lock before every next launch (`shared/common/src/common/cdp_interceptor/client.py:160`, `clear_singleton_locks()`). No manual cleanup needed — the next request self-heals.

If the interceptor process itself dies mid-request, temp-profile clones under `PROFILES_ROOT/.temp/` are left behind. They're swept on next startup by the FastAPI lifespan hook, so the volume doesn't accrue orphans across restarts.

### Cookie freshness caveat

Fast-path captures write refreshed session cookies back to the base profile — that's what keeps a `roofix` or `gmail` session warm across days-to-weeks. Slow-path captures write to a doomed clone, so their cookie updates are discarded. Under sustained same-profile burst load (multiple in flight at all times), the base profile stops receiving cookie refreshes and eventually the session expires — re-upload the profile when you see `login_wall: true` in responses, or have the requests carry [`login_actions`](#login-actions) (a fresh login is only persisted by a fast-path capture).

## Refreshing a profile (operator flow)

`interceptor` cannot present a login UI itself, so profiles are captured on an operator laptop and uploaded. The archive must be a **`.tar.gz`** — the endpoint uses `tarfile.open(mode="r:*")` (see `ai/interceptor/profiles.py:126`), which auto-detects gzip/bzip2/xz tar. Plain `.zip` will NOT work.

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

Archive the **whole `--user-data-dir`**, and archive its **contents** rather than the directory itself — the endpoint extracts into an already-created `/data/profiles/{name}/`. The `-C <dir> .` pattern does that.

Do **not** archive just `Default/`. Chrome is launched with `--user-data-dir=<profile dir>` and looks for `Default/` *inside* it; `Local State` sits at the root, and `sentinel.py` looks for `session_ok` at the root too. An archive rooted at `Default/` puts `Cookies` where Chrome never reads it, and Chrome silently creates a fresh empty `Default/` — you get a profile that boots cleanly and is simply logged out.

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

The endpoint expects `multipart/form-data` with a single field named exactly **`archive`**. Any other field name gives a FastAPI **422** with no unpack performed — and because a 422 leaves the existing profile untouched, it's easy to mistake for success if you don't read the response body.

From the host, via the LiteLLM pass-through (see [Reaching the service](#reaching-the-service) — `http://localhost:8080` is *not* the container):

```bash
curl -X POST http://localhost:4001/v1/interceptor/profiles/gmail/refresh \
  -H "Authorization: Bearer $DEFAULT_LITELLM_MASTER_KEY" \
  -F archive=@gmail.tgz
```

Or bypass LiteLLM entirely:

```bash
docker cp gmail.tgz interceptor:/tmp/gmail.tgz
docker exec interceptor python3 -c "
import profiles; print(profiles.unpack_profile('gmail', open('/tmp/gmail.tgz','rb')))"
```

> **Do not refresh while a capture is in flight against that profile.** `unpack_profile` `shutil.rmtree`s the profile directory without consulting the per-profile lock that `/capture` holds, so a refresh landing mid-capture deletes the `--user-data-dir` out from under a live Chrome and interleaves the extraction with that Chrome's own writes. Check `GET /jobs` first. Related: the wipe happens *before* the archive is validated, so a corrupt or wrong-format upload (a `.zip`, a truncated stream) destroys the working profile and leaves an empty directory behind — which for a session profile means a laptop re-login, not a retry.

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
| `Local State` | Profile metadata. On **Windows** it holds the DPAPI-wrapped cookie key; on **Linux/macOS** it holds no key at all (the key lives in the OS keyring) — see below. |

### Cookie encryption and portability

Read this before concluding a session expired. Chrome never stores cookie values in plaintext, and **the key is not always inside the profile**. Which backend it uses depends on the environment it runs in, and a mismatch between the machine that captured the profile and the machine that consumes it presents as a login wall that is indistinguishable from an expired session.

| Platform | Backend | Where the key lives | Value prefix | Portable? |
|---|---|---|---|---|
| Linux, desktop session | gnome-keyring / kwallet (via DBus + libsecret) | user's login keyring — **never in the profile** | `v11` | ❌ |
| Linux, no keyring (any container) | `basic` | derived from a constant hardcoded in Chrome's source | `v10` | ✅ |
| macOS | login Keychain | Keychain — not in the profile | `v10` | ❌ |
| Windows | DPAPI | `Local State`, wrapped against the Windows user account | — | ❌ |

`start_browser` therefore pins **`--password-store=basic`** on every launch (`shared/common/src/common/cdp_interceptor/launcher.py`), which forces the portable `v10` backend on both the capture machine and the container so profiles survive the trip. macOS would additionally need `--use-mock-keychain`; Windows ignores the flag and keeps using DPAPI.

Two consequences worth internalizing:

- **The flag only affects cookies as they are WRITTEN.** Pinning it does not convert `v11` values that already exist — those stay unreadable, so a profile captured before the flag was in place must be **re-captured with a fresh login** once. Re-uploading it unchanged will not help, no matter how it's packaged.
- **`v10`'s key is a constant**, so anyone holding the profile directory can decrypt its cookies. That is already true of any profile shipped around as a `.tgz` and unpacked into a shared volume, but it makes the archive credential material — store and transfer it accordingly.

**Windows → Linux** remains the hardest case: DPAPI is Windows-only, so a profile captured with real Chrome on a Windows laptop cannot be decrypted in the container at all. Capture inside **WSL2** or on a Linux host using Playwright's chromium instead — but note that a Linux desktop with a working keyring produces `v11` cookies, which are just as unusable in a container. Linux capture only helps *with* `--password-store=basic` in effect, which is why the flag is pinned in the launcher rather than left to the environment.

### Sanity check before uploading

**1. Check the cookie encryption version.** This is the cheap, decisive test — it catches the failure mode above before you spend a capture cycle discovering it:

```bash
python3 -c "
import sqlite3, collections
c = sqlite3.connect('/data/profiles/roofix/Default/Cookies')
print(collections.Counter(p for (p,) in c.execute('select substr(encrypted_value,1,3) from cookies')))"
```

Expect `v10` on the rows for your target's domain. **`v11` means the profile is not portable** — Chrome wrote those cookies against a keyring key that will not exist in the container. Re-capture with a fresh login (`--password-store=basic` is pinned in the launcher, so any capture through this library produces `v10`).

Reading the DB while Chrome has the profile open is fine — SQLite handles the concurrent read — but copy the file first if you want to be certain of a consistent snapshot.

**2. Spot-check the session itself** by re-running `cdp-spy` against the same profile-dir and hitting a page that requires auth:

```powershell
uv run cdp-spy --url https://gmail.com/inbox --profile-dir C:\tmp\gmail_profile
```

If you land on the inbox (not the login page), the profile is good to package.

### Alternative: re-capture in place through a local interceptor

When a profile already exists and just needs a fresh login (expired session, or `v11` cookies that must be rewritten as `v10`), you can re-log-in *through* the interceptor instead of setting up a separate `cdp-spy` run. Useful because it exercises the exact same launcher and flags the container will use.

Run the service bare-metal on a machine with a display, pointed at the profile root:

```bash
cd ai/interceptor && \
  DISPLAY=:1 XAUTHORITY=/run/user/1000/gdm/Xauthority \
  ../../.venv/bin/python3 app.py
```

Then:

1. **Delete the sentinel** so the gate picks a visible launch: `rm <PROFILES_ROOT>/roofix/session_ok`. Confirm with `GET /profiles/roofix` → `sentinel_present: false`.
2. **Fire `/capture`** with a window long enough to type in, and `login_timeout` **below** `capture_window_seconds` — e.g. `capture_window_seconds: 300`, `login_timeout: 240`. The default 20–30s window closes before you can finish logging in and returns `login_wall: true`.
3. **Log in** in the Chrome window that opens. The session re-navigates to the target and resumes capturing; you'll typically see the pre-login and post-login responses both captured, which is a handy confirmation that auth took effect.
4. On the first successful capture, `mark_session_ok` rewrites the sentinel (`client.py:456`) — the profile is session-ready again with no manual step.
5. **Verify `v10`**, then package and upload per steps 2–3 above.

Because the fast path writes cookies back to the base profile, the refreshed session persists in place — no copy-back needed.

## Configuration

| Env var | Default | Notes |
|---|---|---|
| `INTERCEPTOR_PROFILES_ROOT` | `/data/profiles` | Root under which named profiles live. Backed by the `interceptor_data` volume. Temp clones live under `<root>/.temp/`. |
| `INTERCEPTOR_DEBUG_PORT` | `9224` | Base of the CDP debug port pool. Pool spans `[base, base + INTERCEPTOR_MAX_CONCURRENT)`. |
| `INTERCEPTOR_MAX_CONCURRENT` | `8` | Max simultaneous `/capture` calls. Each slot = one Chrome (~200–400 MB RAM) + on same-profile collision one profile clone (~20–80 MB disk). See [Resource sizing](#resource-sizing). |
| `INTERCEPTOR_CAPTURE_WINDOW_SECONDS` | `20` | Default capture window when a request omits `capture_window_seconds`. |
| `INTERCEPTOR_SCREENSHOT_WAIT_SECONDS` | `15` | Default render wait for `POST /screenshot` / `screenshot_url` when a request omits `wait_seconds`. See [Screenshots](#screenshots). |
| `INTERCEPTOR_LOGINS_DIR` | `/config/logins` | Directory holding the per-profile credential files `<profile>.json` that `login_actions` references resolve from. The compose file bind-mounts `ai/interceptor/logins` there read-only; the files are re-read on every request. Kept outside `INTERCEPTOR_PROFILES_ROOT` on purpose. Changing the mount is a recreate: `make build interceptor && make up interceptor`. See [Login actions](#login-actions). |
| `INTERCEPTOR_LOGIN_<PROFILE>_USERNAME` / `_PASSWORD` | _(empty)_ | The credentials a logins file references as `${ENV:INTERCEPTOR_LOGIN_…}` — today `INTERCEPTOR_LOGIN_ENPHASE_USERNAME` / `_PASSWORD`. Set in `.env`; each must also be listed in the compose `environment:` block (as `${VAR:-}`). Only the `INTERCEPTOR_LOGIN_` prefix is resolvable. Unset or empty → that profile's `login_actions` are a 400 naming the variable. A literal `$` is written `$$`. Changing a value is `make up interceptor`. |

## Calling from LiteLLM

### Pass-through

```powershell
curl -X POST http://localhost:4001/v1/interceptor/capture `
  -H "Authorization: Bearer $env:DEFAULT_LITELLM_MASTER_KEY" `
  -H "content-type: application/json" `
  -d '{"url":"https://httpbin.org/json","url_patterns":["httpbin\\.org/json"],"profile":"httpbin"}'
```

### MCP tools

The `interceptor` MCP server (registered in `ai/litellm/litellm_config.yaml` `mcp_servers.interceptor`) exposes five model-invokable tools:

| Tool | Purpose | Args |
|---|---|---|
| `capture_url` | Run one capture — same core behavior as `POST /capture`; `screenshot=true` adds an image of the page; `page_script` / `actions` drive the page first (see [Page scripts and actions](#page-scripts-and-actions)) | `url`, `url_patterns`, `profile`, `capture_window_seconds`, `login_timeout`, `max_matches_per_pattern`, `screenshot`, `screenshot_full_page`, `screenshot_format`, `screenshot_scale`, `page_script`, `actions` (list of step objects), `stop_when_matched`, `login_url_patterns`, `actions_ready_timeout_seconds`, `login_actions` (see [Login actions](#login-actions)) |
| `screenshot_url` | Navigate and return a screenshot — same core behavior as `POST /screenshot`. Image arrives as an `ImageContent` block (see [Screenshots § On MCP](#on-mcp)) | `url`, `profile`, `wait_seconds`, `full_page`, `format`, `quality`, `scale`, `login_timeout`, `login_url_patterns`, `login_actions` |
| `list_profiles` | Discover which named profiles exist — call before `capture_url` if the LLM doesn't know the profile name. Each entry carries `login_keys` / `login_origins`: the `${…}` names its `login_actions` may use | *(none)* |
| `list_jobs` | Snapshot of the port pool + running captures — same shape as `GET /jobs` | *(none)* |
| `get_job` | Detail on one in-flight capture by id — same shape as `GET /jobs/{job_id}` | `job_id` |

The `keep_open` and `debug_logging` knobs from `POST /capture` are deliberately **not** exposed to MCP — both are operator-only debug flags (`keep_open` requires manual Chrome-kill cleanup; `debug_logging` writes to a DevTools console the LLM can't read).

`login_url_patterns` on both tools has the same semantics as on `POST /capture`: omit it (or pass `null`) to keep the defaults, pass a list to **replace** them, `[]` to disable detection. Pass the site's SSO host when the defaults don't match it — for Enphase, `["login", "signin", "/auth", "sso\\.enphaseenergy\\.com"]` — or an expired session comes back as an empty result rather than `login_wall: true`.

`login_actions` on both tools take the same step objects as `POST /capture`. The tool docstrings tell the model to write the credentials as references (`${username}`, `${password}` — the names `list_profiles` reports) and never to ask a user for a real username or password; the values never reach the model in either direction.

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

`phase` progresses: `"cloning"` (slow path only, during `shutil.copytree`) → `"capturing"` (Chrome running, XHRs being intercepted) → `"cleaning_up"` (temp rmtree + port release). Fast-path captures skip `"cloning"` and go straight to `"capturing"`. A capture with `page_script` / `actions` shows `"actions"` while they run (back to `"capturing"` when they finish), and its `metadata` carries `actions_done` / `actions_total`; one with a screenshot passes through `"screenshot"` just before `"cleaning_up"`.

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

There's no `headless` field on the capture request — the service always tries to launch headless in production. Headless is gated by `InterceptorClient` on `session_sentinel=True AND session_ok exists in profile_dir` (`shared/common/src/common/cdp_interceptor/client.py:182`). The service passes `session_sentinel=True` (`ai/interceptor/app.py:391`), so headless comes down to whether the `session_ok` sentinel file is present in the profile directory. Uploaded profiles get one written automatically by `unpack_profile()` (`ai/interceptor/profiles.py:131`).

**To force a visible launch when debugging locally**, delete the sentinel before firing `/capture`. This only works on a machine with a display — inside the Docker container there's no display, so the launch fails with **`Missing X server or $DISPLAY`** in `docker logs` and the capture returns having seen nothing.

That error string has two distinct causes, and the timing tells them apart:

| When it appears | Cause |
|---|---|
| Immediately, with `status="loading"` and `seen_urls=0` | No `session_ok` in the profile → the gate chose visible from the start. Usually means the upload never landed — check `GET /profiles` for `sentinel_present` and `size_bytes`, and confirm you uploaded to the instance you think you did. |
| After `login_timeout` seconds, following a `waiting_login` status | The headless session hit a login wall and `client.py:559-579` cleared the sentinel and **relaunched visibly** to let a human log in — which cannot work in a container. The underlying problem is the session, not the display. |

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

**One-shot flip.** The sentinel is a one-shot toggle — on the next successful data capture, `InterceptorClient._on_data_inner()` writes it back (`shared/common/src/common/cdp_interceptor/client.py:456`). So the sequence "delete sentinel → run visible capture → observe → next capture goes headless again" is automatic. To force multiple visible runs in a row, delete the sentinel between each call.

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
- Actions reach open shadow roots only — not closed shadow roots, not iframes — and `select` handles native `<select>` only. One `page_script` and one action list per capture; there is no conditional branching between steps (use an `evaluate` step for logic).
- `login_actions` handle a plain form login only — one attempt per capture, no verification codes / MFA / CAPTCHA, and (because the form has to be in the top document) not a login form inside an iframe.
- No MCP-side cancellation — cancel is HTTP-only. An LLM cannot reclaim a stuck capture it started; that's an operator's job.
- Screenshots are one-shot, taken at the end of the window (after any `actions`, so "fill, click, then screenshot" works) — but there is no mid-run screenshot and no element-level clipping. Viewport is fixed at the headless `1920×1080` window; full-page height is clamped to `max_height` (≤ 16384).
