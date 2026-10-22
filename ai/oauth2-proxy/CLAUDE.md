# ai/oauth2-proxy

The one sign-in for `chat.zeoenergy.com`: Google login + Workspace-group check (`zeoai.access@zeoenergy.com`), then routing by path to Open WebUI and the other browser UIs. Guide: [`OAUTH2_PROXY.md`](OAUTH2_PROXY.md). Compose: `docker-compose.oauth2-proxy.yml` (`oauth2-proxy`, `oauth2-assets` — the nginx sidecar serving the repo's `assets/` for the pre-auth pages). Config is the `OAUTH2_PROXY_*` block of `.env` (commented in `.env.example`); branded pages in `templates/`.

## Rules

- **Traffic path:** cloudflared → `oauth2-proxy:4180` (Docker DNS, never through the edge) → upstreams on `ai_shared`. LAN clients reach host port 4180 through the edge. Grant / revoke access in Google Admin → Groups; revocation lands on the next `OAUTH2_PROXY_COOKIE_REFRESH` (15 m, hard expiry `OAUTH2_PROXY_COOKIE_EXPIRE` 1 h).
- **`OAUTH2_PROXY_UPSTREAMS` is longest-prefix, path forwarded unchanged.** `.env.example` today: `openwebui:8080` (default), `/assets/` (`oauth2-assets`), `/sandboxes/` (`sandbox-proxy`), `/superset/` (`superset:8088`), `/rustfs/` (`lake-s3:9001` console — prefix fixed by RustFS), `/edge/` (`edge:8407` status page). Apply with `make up oauth2-proxy`. A new browser UI goes under `chat.zeoenergy.com/<path>/` here rather than its own hostname, so it inherits the Google cookie (`ai/cloudflared/CLOUDFLARED.md § Current ingress rules`).
- **n8n is not an upstream, and has no instance of its own.** n8n is not exposed at all (`ai/n8n/N8N.md § Access (not exposed)`). Never add `http://n8n:5678/n8n/` to `OAUTH2_PROXY_UPSTREAMS`: n8n's subpath mode is broken upstream (n8n-io/n8n#19635, #18596). Exposing it means a second oauth2-proxy instance on its own hostname (`N8N.md § Exposing it later`).
- **Upstreams trust headers, with no password.** `OAUTH2_PROXY_PASS_USER_HEADERS` → `X-Forwarded-Email` (Open WebUI trusted-header SSO), `OAUTH2_PROXY_SET_XAUTHREQUEST` → `X-Auth-Request-*` (Superset), `OAUTH2_PROXY_PASS_ACCESS_TOKEN` → the Google access token Open WebUI's patch `0002` verifies the identity with (`OPENWEBUI.md § Single sign-on`). So a header-trusting upstream must be reachable only through this proxy: Open WebUI is bound to `127.0.0.1:8007`, Superset's LAN port strips the headers at the edge. Keep all three `true`.
- **`OAUTH2_PROXY_SKIP_AUTH_ROUTES=^/assets/`** is the only public path (the logo on the sign-in page). Widening it bypasses group gating — a security decision.
- **`OAUTH2_PROXY_COOKIE_SAMESITE=none`** is load-bearing: sandbox iframes inside Open WebUI load from an opaque origin and `lax` would drop the cookie there (iframe → sign-in page). The cookie stays `HttpOnly` + `Secure`.
- **`OAUTH2_PROXY_COOKIE_SECRET`** must be 32 bytes of URL-safe base64, or the container exits at start.
- **Cloudflare caches authenticated responses** on `chat.zeoenergy.com` by file extension unless the dashboard bypass rule exists — check `cf-cache-status` before blaming oauth2-proxy (`ai/cloudflared/CLOUDFLARED.md` § Cache rule — bypass for `chat.zeoenergy.com`). Verify the gate with `curl -sfL https://chat.zeoenergy.com/oauth2/ping`.
- **Never commit** `sa-key.json` or the `.env` secrets (`OAUTH2_PROXY.md § What NOT to commit`).

## Cross-folder rules

Also in the root `CLAUDE.md` § Cross-folder rules — read the named entry there for the full picture.

- **Paths under `chat.zeoenergy.com`** — each path upstream has a partner that must change with it: `/sandboxes/` ↔ `SANDBOX_PROXY_URL` (`ai/sandbox`); `/superset/` ↔ Superset's `/superset` prefix (`ai/trino`); `/rustfs/` is RustFS's own; `/edge/` ↔ `frontend status` in `ai/edge/haproxy.cfg`.
- **n8n is not exposed** — no upstream here routes to it. Exposing it adds a second instance on its own hostname, and the n8n URL variables (`ai/n8n`) must match it. (root `CLAUDE.md` § Cross-folder rules)
- **Superset trusts a header** — this proxy reaches `superset:8088` directly; never point the upstream at the edge's 8016, whose listener deletes the identity headers. A new `SUPERSET_REMOTE_USER_HEADER` also needs a `del-header` in the edge.
- **Swap-class services publish through the edge** — `oauth2-proxy` and `oauth2-assets` are swap-class: no `container_name`, no `ports:`; host port 4180 (`PORT_OAUTH2_PROXY`) is the edge's `listen oauth2-proxy` (900 s timeouts). `stop_grace_period: 900s` lets long chats finish on the retiring copy. Root `CLAUDE.md` § Deploys (`make up`).
- **Timeout ladder** — 900 s here (edge listen and grace period) matches Open WebUI's outermost limit; don't shorten one side alone.

## Env vars

All in the oauth2-proxy block of `.env.example`, with comments; the table is `OAUTH2_PROXY.md § Configuration reference`. The ones with traps are above (`_UPSTREAMS`, `_SKIP_AUTH_ROUTES`, `_PASS_USER_HEADERS`, `_SET_XAUTHREQUEST`, `_PASS_ACCESS_TOKEN`, `_COOKIE_SAMESITE`, `_COOKIE_SECRET`, `_COOKIE_REFRESH` / `_EXPIRE`). `OAUTH2_PROXY_HTTP_ADDRESS` must keep `0.0.0.0:4180` (cloudflared reaches it on `ai_shared`); `OAUTH2_PROXY_COOKIE_DOMAINS` / `_REDIRECT_URL` must match the Google OAuth client's redirect URI; `OAUTH2_PROXY_GOOGLE_ADMIN_EMAIL` must be a Super Admin the service account impersonates (`OAUTH2_PROXY.md § One-time Google Admin console setup`). `PORT_OAUTH2_PROXY` (`4180`) is in the PORT REGISTRY (root `CLAUDE.md` § Env vars and the PORT REGISTRY).
