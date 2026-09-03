# Plan: Session-stable preview URLs

**Status:** deferred — design captured, implementation not started.
**Related:** [SANDBOX.md § Public iframe routing](SANDBOX.md), the `create` / `update_files` / `run` MCP tools.

## Problem

Today the preview URL returned by `create` / `update_files` / `run` is

```
<SANDBOX_PROXY_URL>/{sandbox_id}/
```

where `sandbox_id` is the internal id of a specific container. When the runner self-heals a session (container crashed / was reaped / update_files respawned with `recreate_if_gone=true`), the new container gets a **new** `sandbox_id`. The URL changes, and:

- The `<iframe>` in the artifacts panel loads a new URL. In-flight in-app state is lost regardless (the app itself restarted), but the URL change also breaks any bookmark or link the user copied from the address bar.
- The download URL (`/sandboxes/download/{session_id}`) already uses `session_id`, so it stays valid across self-heal. The preview URL doesn't — inconsistent surface.
- A model that returned a preview URL to a user out-of-band (say, in a Slack message) has handed out a dead link the moment the sandbox respawns.

The goal: preview URLs of the shape

```
<SANDBOX_PROXY_URL>/session/{session_id}/
```

that resolve to the currently-live container for that session, transparently across respawns.

## Why it's not free

`sandbox-proxy` is Caddy configured for **static routing**. Its Caddyfile maps `/{sandbox_id}/* → sandbox-{id}:80` at container-start time and never touches its config again. That's the whole reason routing is cheap — no per-request lookups, no dynamic reconfiguration, one Caddy config file.

Session-stable URLs mean the routing layer needs to know "session X currently lives on container Y" at request time. That means either:

1. Per-request lookup (something reverse-proxies through the runner), or
2. Live config reload (the runner tells Caddy about session→container maps).

Both are viable. They have different failure modes.

## Two implementation paths

### Path 1 — Runner-side reverse proxy shim

Caddy grows one static route:

```
handle_path /session/* {
    reverse_proxy sandbox-runner:8000 {
        rewrite /internal/session-proxy{path}
    }
}
```

Runner exposes `GET /internal/session-proxy/{session_id}/{path...}` — an ASGI reverse proxy that:

1. Looks up `session_id` in the registry (Postgres) → gets the current `container_name`.
2. Streams the request to `http://{container_name}:80/{path...}` using `httpx.AsyncClient(stream=True)`.
3. Streams the response body back.
4. Proxies WebSocket upgrades (Streamlit's `/_stcore/stream`, Vite HMR's `/_ws`, Next.js `/_next/webpack-hmr`) by upgrading both connections and piping bytes bidirectionally with `websockets`.

Caching the session→container mapping in the runner (in-memory dict, invalidated on session lifecycle events) keeps the DB hit off the hot path.

**Pros:**
- Zero Caddy config churn — the runner is the only moving part.
- Works with the existing static Caddy setup unchanged.
- Same architectural pattern as the download URL — precedent proves the runner can do this.
- Session lookup is atomic with the registry (no config-drift race).
- Fails gracefully — if the runner is down, Caddy returns 502 from the reverse_proxy directive, same as any upstream outage. No orphaned "half-installed" routes.

**Cons:**
- Every asset request (HTML, CSS, JS, images, XHR, WebSocket frames) traverses the runner. Adds ~1 process-hop of latency per request (~1–3 ms on the loopback network — but iframe pages fire dozens of asset requests).
- Runner CPU/memory footprint grows with sandbox traffic — every viewer's requests land there.
- WebSocket proxying is non-trivial code — asyncio + `websockets` + backpressure + connection cleanup. Bugs here manifest as HMR silently failing.
- If the runner restarts, every in-flight request breaks (Caddy would just retry — this is not worse than a runner restart today, just now it affects more surface).

### Path 2 — Caddy admin-API dynamic routing

Enable Caddy's admin API (`caddy.admin.listen 0.0.0.0:2019` on the internal network). On every session lifecycle event, runner calls:

- **`create`** → `POST /config/apps/http/servers/srv0/routes` with a new route object routing `/session/{sid}/*` to `sandbox-{sandbox_id}:80`.
- **Self-heal respawn in `update_files`** → `PATCH /config/apps/http/servers/srv0/routes/{route_id}` swapping the container id.
- **`close`** and reaper cleanup → `DELETE /config/apps/http/servers/srv0/routes/{route_id}`.

**Pros:**
- Zero per-request hop through the runner. Same performance profile as today (Caddy static routing).
- WebSocket / HMR / SSE / long-poll work natively — Caddy has been doing them for years.
- Runner CPU stays flat regardless of sandbox traffic volume.

**Cons:**
- Config drift. If the runner restarts without cleaning up, orphan routes accumulate in Caddy's config until an operator prunes them. If Caddy restarts, the runner doesn't know it lost state and stops installing routes correctly. Reconciliation needs a startup sweep both directions.
- Admin API is a control plane the sandbox subsystem hasn't needed before. Enabling it means someone else on the internal network can also poke it — tightening ACL for that becomes another thing to reason about.
- Every session lifecycle event now hits both Postgres AND Caddy. Two-phase failures possible: DB says session X is running, Caddy has no route. Runner has to reconcile.
- Every session adds a route to Caddy's in-memory config. At `SANDBOX_MAX_CONCURRENT=8` today this is nothing; at hundreds of concurrent sessions (unlikely under current knobs, possible under future ones) config size becomes something to watch.

## Recommendation (going into the discussion)

**Path 1 (runner-side reverse proxy shim).** The download-URL precedent proves the runner-as-reverse-proxy pattern works. WebSocket proxying is real work but bounded — one focused module, similar shape to what interceptor and roofix's Playwright code already do internally. Caddy admin-API config drift is the class of bug that eats weekends; the shim's failure modes are honest (runner down → 502, same as any upstream outage).

The per-request runner hop is real but small. It's not on the model's critical path (the URL is served to end users, not to the model). At current sandbox concurrency limits it's a rounding error on the runner's CPU budget.

## Detailed design (assuming Path 1)

### Caddyfile change

Small addition to `ai/sandbox/proxies/sandbox-proxy.Caddyfile` (or wherever it lives — TBD). Existing static routes for `/{sandbox_id}/*` **stay** as an operator-debugging path and don't change.

```caddy
:80 {
    # NEW: session-stable path, resolved by the runner
    handle_path /session/* {
        reverse_proxy sandbox-runner:8000 {
            rewrite /internal/session-proxy{path}
            transport http {
                keepalive 5m
                versions 1.1 2 h2c
            }
        }
    }

    # UNCHANGED: existing sandbox_id path
    handle_path /* {
        reverse_proxy sandbox-{$SANDBOX_ID}:80
    }
}
```

Wait — the existing config isn't quite this shape (each sandbox has its own container name). I'll re-read `sandbox-proxy.Caddyfile` when implementing to make sure the new route sits above the existing catch-all correctly.

### Runner endpoint

`GET|POST|PUT|DELETE|PATCH /internal/session-proxy/{session_id}/{path:path}`, plus WebSocket upgrade support.

Pseudocode:

```python
@app.api_route(
    "/internal/session-proxy/{session_id}/{full_path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
)
async def session_proxy(session_id: str, full_path: str, request: Request):
    container = await _lookup_session_container(session_id)
    if container is None:
        raise HTTPException(502, "sandbox for this session is not currently running")
    upstream = f"http://{container}:80/{full_path}"
    # Strip hop-by-hop headers, preserve everything else including
    # Cookie, Authorization (from oauth2-proxy), Accept-Encoding.
    headers = _copy_forwardable_headers(request.headers)
    if _is_websocket_upgrade(request.headers):
        return await _proxy_websocket(request, upstream, headers)
    async with httpx.AsyncClient(timeout=None) as client:
        upstream_req = client.build_request(
            request.method, upstream, headers=headers, content=request.stream()
        )
        upstream_resp = await client.send(upstream_req, stream=True)
        return StreamingResponse(
            upstream_resp.aiter_raw(),
            status_code=upstream_resp.status_code,
            headers=_copy_returnable_headers(upstream_resp.headers),
            background=BackgroundTask(upstream_resp.aclose),
        )
```

WebSocket proxying: bootstrap upstream WebSocket with `websockets.connect(...)`, then run two `asyncio.Task`s piping frames between the client WS and the upstream WS. Close both on either close event.

### Session→container cache

In-memory `dict[session_id, container_name]` with TTL invalidation. Populated on session lifecycle events (`create`, respawn, `close`, reaper events). Cache miss falls back to a registry read; failed registry read → 502. Prevents the DB from being on the hot path for every asset request.

Invalidation is straightforward because the runner is the ONLY writer of session state — no cross-process cache invalidation problem.

### URL returned by `create` / `update_files` / `run` / `preview`

Changes from:

```python
url = f"{PROXY_URL}/{sandbox_id}/"
```

to:

```python
url = f"{PROXY_URL}/session/{session_id}/"
```

Backward compat: `{PROXY_URL}/{sandbox_id}/` still works for the current sandbox that session is on. Old links go dead only when THAT specific sandbox is torn down — which is the current behavior. So the change is purely additive: session URLs are new; sandbox URLs work exactly as they did.

The `Download source:` line already uses session_id — no change there. Consistency win.

### Iframe UX on respawn

Even with a stable URL, mid-self-heal there's a window where the container isn't listening yet. Options:

1. **Blank/error page from the iframe** — Caddy returns 502, browser renders "This site can't be reached." Ugly.
2. **Warming-page passthrough** — during self-heal, the runner has a warming container up before it starts writing files. If the session-proxy returns the warming page during the gap, the iframe sees "Sandbox warming, please wait…" and auto-refreshes on load. This is a natural fit for Path 1.
3. **Runner-side retry** — the session-proxy detects connection-refused, waits up to 5 seconds for the container to come up, then retries. Passes on 502 only if the container really is gone.

Combination of #2 + #3 is best: the runner already writes warming files when spawning empty (from the recent redesign); extending that to keep the warming container alive during self-heal is a small change. And the retry gives us the graceful "loading" experience without a client-side change.

### WS reload channel specifics

Framework-by-framework check:

| Runtime | Reload endpoint | Notes |
|---|---|---|
| Streamlit | `/_stcore/stream` (WS) | Auto-reconnects on drop. Proxying is straightforward. |
| Vite | `/@vite/client` + `/__vite_ping` (WS + HTTP) | HMR client polls `/__vite_ping` before reconnecting the WS. Both need to proxy. |
| Next.js | `/_next/webpack-hmr` (WS) | Auto-reconnects. |
| Flask/FastAPI/Gradio | none (no HMR) | Plain HTTP; no special handling. |
| static (nginx) | none | Plain HTTP. |

All the WS paths are relative to `/session/{sid}/…` after the Caddy rewrite. The runner's session-proxy needs to preserve the path (already does — `full_path:path`).

## Trade-offs table

| Concern | Path 1 (runner shim) | Path 2 (Caddy admin API) |
|---|---|---|
| Per-request latency | +1–3 ms | 0 |
| Runner CPU under traffic | grows with viewers | flat |
| Config drift risk | none | needs bidirectional reconciliation |
| WebSocket proxy code | write and maintain | free from Caddy |
| Failure mode | 502 on runner outage | orphan routes on runner crash |
| Setup cost | one endpoint + Caddy line | admin API enable + ACL + reconciliation |
| Precedent in codebase | download URL uses same pattern | none |
| Config surface at scale | flat | grows with concurrent sessions |

## Open questions

1. **Do we drop the old `/{sandbox_id}/` URL?** Recommendation: keep it as an operator debugging path (`docker ps` gives you the sandbox_id; pasting it into the browser reaches the container directly). No harm in leaving Caddy's existing static route in place.
2. **Cache invalidation on runner restart.** In-memory session→container cache is empty after restart. First few requests per session pay the registry-lookup cost. Acceptable — this is startup thundering-herd territory, not steady state.
3. **oauth2-proxy interaction.** `chat.zeoenergy.com/sandboxes/session/{sid}/*` needs to be in `OAUTH2_PROXY_UPSTREAMS` so the cookie carries through. It already will be if `/sandboxes/*` is a prefix match — verify in implementation.
4. **Path collisions.** `/session/*` in the sandbox-proxy Caddyfile must not conflict with any future route the runner or Caddy needs. Reserving `/session/` prefix for this feature; document it.
5. **HTTP method coverage.** GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS. WebSocket upgrade. Server-Sent Events (Streamlit uses `text/event-stream` for some app patterns) — verify `httpx` streaming doesn't buffer the whole response. May need to disable response buffering explicitly.
6. **Request size limits.** File-upload apps in sandboxes need to accept multi-MB POST bodies. `httpx` handles this by default via streaming, but confirm Caddy's `handle_path` doesn't cap request size in the reverse_proxy directive.
7. **`Host` header handling.** Streamlit and Vite are sometimes picky about the Host header. Runner should preserve `Host: sandbox-{id}` when talking to the upstream, not pass through `chat.zeoenergy.com`.

## Implementation footprint (if we go with Path 1)

- `ai/sandbox/proxies/sandbox-proxy.Caddyfile` — add `handle_path /session/*` route pointing at runner.
- `ai/sandbox/runner/app.py` — new `/internal/session-proxy/{session_id}/{path:path}` route + WebSocket support. New helper module `session_proxy.py` for the streaming/WS code.
- `ai/sandbox/runner/session_cache.py` (new) — in-memory session→container cache with lifecycle-event invalidation.
- `ai/sandbox/runner/app.py` — hook `create`, `_do_update_files` respawn branch, `close`, reaper events to invalidate the cache and emit warming-page during respawns.
- URL generation in `_do_create` / `_apply_files_to_running` / `_respawn_session` — swap `sandbox_id` → `session_id` in the returned `url`. Preserve the sandbox_id form in structured content for operators.
- `SANDBOX.md § Public iframe routing` — new subsection explaining `/session/` vs `/{sandbox_id}/`.
- `ENDPOINTS.md` — new row for `/internal/session-proxy` (internal-only, not called by external clients directly).
- Postman collection — probably NOT — this endpoint is internal to Caddy → runner, not something operators hit directly.
- Docker compose — no changes.

Estimated size: comparable to the exec + get_files sweep. Self-contained.

## Prerequisites

None strictly required, but:

- The Streamlit stderr-tee shim just shipped and includes a monkeypatch that survives Streamlit version bumps. Session-proxy WS work has no interaction with this but shares the "handle a moving framework surface gracefully" theme — the same fallback mindset applies to WS proxy code.
- The recent redesign added `warming_files` per runtime. That's the natural home for the "warming page during self-heal gap" behavior.

## Follow-up work not in scope of this plan

- Per-user session ownership (multi-tenant). Session-stable URLs make this MORE urgent — a leaked URL is now durable across respawns. Design that alongside OAuth-cookie-user-id plumbing from oauth2-proxy through to the runner.
- Public sharing of preview URLs (a user hands a link to someone outside the org). Today the oauth2-proxy cookie blocks external access. Any change to that model interacts with these session URLs.
- Rate-limiting session-proxy traffic. If a sandbox app has a runaway XHR loop, the runner absorbs that traffic. Bounded by container resources today, but session-proxy makes it more visible as runner CPU.

## Decision needed to start

- Confirm Path 1 (runner shim) vs Path 2 (admin API). Recommendation is Path 1 for the reasons above.
- Confirm keeping `/{sandbox_id}/` URLs as a debugging back-door.
- Confirm `/session/` as the URL prefix (alternatives: `/live/`, `/s/`) — `/session/` is verbose but self-documenting in logs.
