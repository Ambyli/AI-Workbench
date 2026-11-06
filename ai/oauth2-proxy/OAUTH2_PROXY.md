# oauth2-proxy — Google group gating in front of Open WebUI and Superset

Sits between Cloudflare and the browser UIs. Every request to `chat.zeoenergy.com` or `superset.zeoenergy.com` must pass a Google sign-in AND belong to a specific Google Workspace group before it can reach Open WebUI or Superset. Access is controlled from **Google Admin console → Directory → Groups** — add or remove a user from the group to grant or revoke.

There is one oauth2-proxy instance per public hostname, each named after the service it fronts: `oauth2-proxy-openwebui` for `chat.zeoenergy.com` (Open WebUI plus the path upstreams), and `oauth2-proxy-superset` for `SUPERSET_PUBLIC_HOST` (Superset at the root, [§ Superset on its own hostname](#superset-on-its-own-hostname)). oauth2-proxy routes upstreams by path only, never by `Host`, so a second hostname needs a second instance. Never name an instance after the bare service (`openwebui`, `superset`): on `ai_shared` that is the real service's DNS name, and `superset:8088` would resolve to the proxy itself.

## Traffic flow

```
Browser  →  Cloudflare (TLS)  →  cloudflared tunnel  →  oauth2-proxy-openwebui:4180  →  openwebui:8080
  chat.zeoenergy.com                                         ↓                    (default upstream)
                                                       Google OAuth          ↘  oauth2-assets:80
                                                             +                    (for /assets/* — skip-auth)
                                                    Google Directory API
                                                    (group membership check)

Browser  →  Cloudflare (TLS)  →  cloudflared tunnel  →  oauth2-proxy-superset:4180   →  superset:8088
  superset.zeoenergy.com                                     (same Google check)  ↘  oauth2-assets:80 (/assets/*)
```

### Quick start

```bash
make up oauth2-proxy
```

Or explicitly:

```bash
docker compose -f ai/oauth2-proxy/docker-compose.oauth2-proxy.yml --env-file .env -p ai-oauth2-proxy up -d
```

| Container | Port | Purpose |
|---|---|---|
| `oauth2-proxy-openwebui` | `localhost:4180` (`PORT_OAUTH2_PROXY`, via the edge) → container `4180` | `chat.zeoenergy.com`: Google login + group membership check; proxies to `openwebui:8080` and the path upstreams on the `ai_shared` network |
| `oauth2-proxy-superset` | _(none — cloudflared only)_ | `SUPERSET_PUBLIC_HOST`: the same Google login + group check; proxies to `superset:8088` at the root |
| `oauth2-assets` | _(none — internal only)_ | `nginx:alpine` serving the repo's `assets/` folder at `http://oauth2-assets:80/assets/*`. Reached only by the two proxies over `ai_shared`. Powers the logo on the pre-auth sign-in and error pages (see § Branded sign-in and error pages) |

The containerised cloudflared reaches `oauth2-proxy-openwebui:4180` and `oauth2-proxy-superset:4180` by Docker DNS on `ai_shared` and never crosses the edge.

All three containers are swap-class (`com.zeo.deploy: swap`): `make up oauth2-proxy` starts a new copy beside the old one, so none has a `container_name` or `ports:` of its own. Host port 4180 (`PORT_OAUTH2_PROXY`) is published by the edge HAProxy (`listen oauth2-proxy-openwebui`, 900 s timeouts), and `oauth2-proxy-openwebui`'s `stop_grace_period: 900s` lets long chats finish on the retiring copy ([EDGE.md](../edge/EDGE.md)). `oauth2-proxy-superset` has no LAN port, so no edge section; its `stop_grace_period: 90s` covers Superset's longest request (its frontend gives up at 60 s).

---

## Access control

- **Grant access:** add the user to `zeoai.access@zeoenergy.com` in Google Admin → Directory → Groups.
- **Revoke access:** remove them. The change propagates on the next cookie refresh (default: every 15 minutes; hard expiry at 1 hour). To shorten, edit `OAUTH2_PROXY_COOKIE_REFRESH` in the compose file.

Suspending the user's entire Google account in Admin console also blocks them (Google OAuth refuses to issue a token), but breaks all their Google access, not just Open WebUI.

---

## One-time Google Cloud setup

### 1. OAuth 2.0 client

The existing OAuth client used by Open WebUI is reused by both instances. It needs one additional authorized redirect URI per hostname:

```
https://chat.zeoenergy.com/oauth2/callback
https://superset.zeoenergy.com/oauth2/callback
```

(the second is `https://<SUPERSET_PUBLIC_HOST>/oauth2/callback`). Add them at: [Google Cloud Console → APIs & Services → Credentials](https://console.cloud.google.com/apis/credentials) → click the OAuth 2.0 Client ID → **Authorized redirect URIs** → **+ Add URI** → paste → **Save**.

The existing OpenWebUI redirect URI (`.../oauth/google/callback`) stays; you are only appending.

### 2. Service account (for group membership lookups)

oauth2-proxy queries Google's Directory API to check whether the signed-in user is in the allowed group. That call is authenticated by a service account with **domain-wide delegation**.

1. Google Cloud Console → **IAM & Admin → Service Accounts** → **Create service account**.
   - Name: `oauth2-proxy-groups` (or similar).
   - Skip the "grant access to project" step.
2. On the created service account → **Keys** tab → **Add key → Create new key → JSON**. The JSON downloads to your machine.
3. Rename the file to `sa-key.json` and place it on the docker host at:
   ```
   ai/oauth2-proxy/sa-key.json
   ```
   The compose file mounts this path into the container. **Do not commit this file** — it is a credential.
4. On the same service account → **Details** tab → copy the **Unique ID** (a long numeric string). You need it for domain-wide delegation, below.

### 3. Enable the Admin SDK API

The service account's group-lookup call is made against the Admin SDK API, which is disabled by default on new Cloud projects. Without it enabled, oauth2-proxy returns 403 to every user with an error like `Admin SDK API has not been used in project <id> before or it is disabled`.

1. Google Cloud Console → **APIs & Services → Library** → search for **Admin SDK API** (or go directly to `https://console.developers.google.com/apis/api/admin.googleapis.com/overview?project=<project-id>`).
2. Click **Enable**.
3. Wait ~1 minute for propagation before retrying a login.

---

## One-time Google Admin console setup

Everything below requires super-admin on the Google Workspace tenant that contains `zeoenergy.com`. Both `gosunergy.com` and `zeoenergy.com` are on the same tenant, so `admin@gosunergy.com` is authoritative for both domains.

### 1. Domain-wide delegation

1. [Google Admin console](https://admin.google.com/) → **Security → Access and data control → API controls**.
2. Scroll to the bottom → **Manage Domain-wide Delegation**.
3. **Add new**:
   - **Client ID:** the numeric Unique ID from the service account.
   - **OAuth scopes:** `https://www.googleapis.com/auth/admin.directory.group.readonly`
4. **Authorize**.

This grants the service account read-only access to group membership across the Workspace, on behalf of the admin user it impersonates (see `OAUTH2_PROXY_GOOGLE_ADMIN_EMAIL` below).

### 2. Impersonated admin must be a **Super Admin**

The email in `OAUTH2_PROXY_GOOGLE_ADMIN_EMAIL` is the identity the service account impersonates when calling the Directory API. Google requires this account to hold the **Super Admin** role for Directory-wide group reads — a regular Admin role, custom admin roles, or "Groups Admin" alone are **not sufficient** and produce a misleading `403: Not Authorized to access this resource/api` at login.

1. Google Admin console → **Directory → Users** → click the account named in `OAUTH2_PROXY_GOOGLE_ADMIN_EMAIL` (e.g. `admin@gosunergy.com`).
2. Scroll to **Admin roles and privileges** → **Assign roles**.
3. Toggle **Super Admin** on → **Save**.
4. Wait ~30 seconds for propagation before retrying a login.

If the account you *want* to use isn't Super Admin and elevating it isn't acceptable, pick a different existing Super Admin (or create a dedicated `oauth2-proxy-svc@yourdomain.com` Super Admin) and update `OAUTH2_PROXY_GOOGLE_ADMIN_EMAIL` in `.env`, then `make clean oauth2-proxy && make up oauth2-proxy`.

### 3. Access group

1. Google Admin console → **Directory → Groups** → confirm `zeoai.access@zeoenergy.com` exists.
2. If creating fresh: **Access type** should be at least **Team** so the service account can enumerate members.
3. Add the users who should have access to Open WebUI.

---

## Configuration reference

This is `oauth2-proxy-openwebui`'s configuration. Every value is sourced from the root `.env` — edit there, never in the compose file. `oauth2-proxy-superset` reads the same values except the rows its compose block fixes ([§ Superset on its own hostname](#superset-on-its-own-hostname)). `PORT_OAUTH2_PROXY` (`4180`) is the host port the edge publishes for LAN clients; cloudflared reaches both instances over the `ai_shared` Docker network by service name.

| Compose env var | `.env` key | Default | Notes |
|---|---|---|---|
| `OAUTH2_PROXY_PROVIDER` | `OAUTH2_PROXY_PROVIDER` | `google` | OAuth provider — only google is exercised here |
| `OAUTH2_PROXY_HTTP_ADDRESS` | `OAUTH2_PROXY_HTTP_ADDRESS` | `0.0.0.0:4180` | Listener inside the container. Port must equal `PORT_OAUTH2_PROXY` |
| `OAUTH2_PROXY_REVERSE_PROXY` | `OAUTH2_PROXY_REVERSE_PROXY` | `true` | Trusts `X-Forwarded-*` headers from cloudflared |
| `OAUTH2_PROXY_PASS_USER_HEADERS` | `OAUTH2_PROXY_PASS_USER_HEADERS` | `true` | Forwards `X-Forwarded-Email` / `X-Forwarded-User` — Open WebUI signs users in from the former (trusted-header SSO) |
| `OAUTH2_PROXY_SET_XAUTHREQUEST` | `OAUTH2_PROXY_SET_XAUTHREQUEST` | `true` | Adds `X-Auth-Request-*` to **responses** only (nginx `auth_request` mode). No upstream receives them, and oauth2-proxy does not delete a client's own copy on the way in, so nothing may trust them — Superset reads `X-Forwarded-Email`. Nothing depends on it today |
| `OAUTH2_PROXY_PASS_ACCESS_TOKEN` | `OAUTH2_PROXY_PASS_ACCESS_TOKEN` | `true` | Forwards the user's Google access token as `X-Forwarded-Access-Token`. Open WebUI's patched trusted-header sign-in presents it to Google to **verify** the identity and fetch name + picture — see [OPENWEBUI.md § Single sign-on](../openwebui/OPENWEBUI.md#single-sign-on). Scopes are `profile email`; it never leaves the Docker network |
| `OAUTH2_PROXY_CLIENT_ID` | `OPENWEBUI_GOOGLE_CLIENT_ID` | _(reused)_ | Reused from Open WebUI's Google OAuth 2.0 client |
| `OAUTH2_PROXY_CLIENT_SECRET` | `OPENWEBUI_GOOGLE_CLIENT_SECRET` | _(reused)_ | Same client's secret |
| `OAUTH2_PROXY_COOKIE_SECRET` | `OAUTH2_PROXY_COOKIE_SECRET` | _(generate)_ | 32-byte URL-safe base64. Signs session cookies. See generator snippet in `.env.example` |
| `OAUTH2_PROXY_COOKIE_DOMAINS` | `OAUTH2_PROXY_COOKIE_DOMAINS` | `chat.zeoenergy.com` | Cookie is only sent to this hostname |
| `OAUTH2_PROXY_COOKIE_EXPIRE` | `OAUTH2_PROXY_COOKIE_EXPIRE` | `1h` | Hard session lifetime |
| `OAUTH2_PROXY_COOKIE_REFRESH` | `OAUTH2_PROXY_COOKIE_REFRESH` | `15m` | Re-check group membership at this interval within a session |
| `OAUTH2_PROXY_UPSTREAMS` | `OAUTH2_PROXY_UPSTREAMS` | `http://openwebui:8080` | Where to proxy authenticated requests. Docker service DNS, not localhost |
| `OAUTH2_PROXY_REDIRECT_URL` | `OAUTH2_PROXY_REDIRECT_URL` | `https://chat.zeoenergy.com/oauth2/callback` | Must match the URI added to the Google OAuth client |
| `OAUTH2_PROXY_WHITELIST_DOMAINS` | `OAUTH2_PROXY_WHITELIST_DOMAINS` | `chat.zeoenergy.com` | Allowed post-login redirect targets |
| `OAUTH2_PROXY_EMAIL_DOMAINS` | `OAUTH2_PROXY_EMAIL_DOMAINS` | `zeoenergy.com` | Rejects any sign-in outside this domain before the group check |
| `OAUTH2_PROXY_GOOGLE_GROUPS` | `OAUTH2_PROXY_GOOGLE_GROUPS` | `zeoai.access@zeoenergy.com` | User must belong to this group |
| `OAUTH2_PROXY_GOOGLE_ADMIN_EMAIL` | `OAUTH2_PROXY_GOOGLE_ADMIN_EMAIL` | `admin@gosunergy.com` | Workspace admin the service account impersonates to read group members |
| `OAUTH2_PROXY_GOOGLE_SERVICE_ACCOUNT_JSON` | `OAUTH2_PROXY_GOOGLE_SERVICE_ACCOUNT_JSON` | `/etc/oauth2-proxy/sa-key.json` | Path **inside the container**; host file is bind-mounted from `ai/oauth2-proxy/sa-key.json` |
| `OAUTH2_PROXY_CUSTOM_TEMPLATES_DIR` | `OAUTH2_PROXY_CUSTOM_TEMPLATES_DIR` | `/etc/oauth2-proxy/templates` | Path **inside the container**; host directory is bind-mounted from `ai/oauth2-proxy/templates/`. See § Branded sign-in and error pages |
| `OAUTH2_PROXY_SKIP_AUTH_ROUTES` | `OAUTH2_PROXY_SKIP_AUTH_ROUTES` | `^/assets/` | Regex(es) of paths served WITHOUT authentication. Currently: the `/assets/` prefix so the pre-login sign-in and error pages can load the Zeo logo. Widen with caution — anything matching is fully public, on both hostnames |
| _(oauth2-proxy-superset only)_ | `SUPERSET_PUBLIC_HOST` | `superset.zeoenergy.com` | Its cookie domain, whitelist and redirect URL (`https://<host>/oauth2/callback`). Must match the tunnel public hostname and the Google client's redirect URIs |

---

## Branded sign-in and error pages

The default oauth2-proxy sign-in prompt and 403 error page are replaced with two custom templates. They cover the two pages an unauthenticated (or denied) user can see before hitting Open WebUI:

- **`sign_in.html`** — the "Sign in to Zeo AI Chat" landing page shown before Google login.
- **`error.html`** — the 403/500/etc. page shown when auth fails after Google (CSRF cookie missing, group check refused, callback error, etc.). Includes a `mailto:it@zeoenergy.com` support link so locked-out users know who to contact.

Both files live at `ai/oauth2-proxy/templates/` on the host and are bind-mounted read-only into the container. Activation is a single env var:

```env
OAUTH2_PROXY_CUSTOM_TEMPLATES_DIR=/etc/oauth2-proxy/templates
```

The mount is added to the compose file next to the `sa-key.json` mount:

```yaml
volumes:
  - ./oauth2-proxy/sa-key.json:/etc/oauth2-proxy/sa-key.json:ro
  - ./oauth2-proxy/templates:/etc/oauth2-proxy/templates:ro
```

### How to edit

- **Product name / sign-in copy** → `sign_in.html`.
- **Support email / error copy** → `error.html`. The `mailto:` link is hardcoded — search for `it@zeoenergy.com`.
- **Colors and layout** → the `<style>` block at the top of each file. The two files share the same design language; keep them in sync.
- **Ship both files or neither.** If only one exists, oauth2-proxy silently falls back to the built-in default for the missing page, producing inconsistent styling.
- **Filenames are exact:** `sign_in.html` and `error.html` (lowercase, snake_case). `signin.html` will be ignored.

Templates are parsed once at container start. Both instances use them (so `superset.zeoenergy.com` also reads "Sign in to Zeo AI Chat"). After editing, swap both: `make up oauth2-proxy` hashes the bind-mounted `templates/` folder, so a template edit starts new copies.

```bash
make up oauth2-proxy
```

### How the logo gets served (the `oauth2-assets` sidecar)

Both templates reference the logo as a plain URL:

```html
<img class="logo" src="/assets/Zeo%20Energy%20Black.png">
<link rel="icon" href="/assets/Zeo%20Favicon%20Black.png">
```

That works despite the pages being served pre-authentication because three things line up:

1. **`oauth2-assets` sidecar** (defined in the same compose file) is `nginx:alpine` with the repo's top-level `assets/` folder bind-mounted at `/usr/share/nginx/html/assets`. It serves `/assets/<file>` at `http://oauth2-assets:80/assets/<file>` on the `ai_shared` network. It is NOT exposed on a host port — only the two oauth2-proxy instances talk to it.
2. **Multi-upstream routing** — `OAUTH2_PROXY_UPSTREAMS=http://openwebui:8080,http://oauth2-assets:80/assets/`. oauth2-proxy uses longest-prefix path matching, so `/assets/*` goes to the sidecar and everything else goes to Open WebUI. The deployed value (`.env.example`) adds more path upstreams the same way, each forwarded with its path unchanged: `/sandboxes/` (sandbox-proxy), `/rustfs/` (lake-s3 console) and `/edge/` — the edge HAProxy's read-only status page at `http://edge:8407/edge/` ([EDGE.md § Status page](../edge/EDGE.md#status-page)). After editing it, `make up oauth2-proxy`. Each path upstream has a partner that must agree with it: `/sandboxes/` ↔ `SANDBOX_PROXY_URL`; `/rustfs/` is fixed by RustFS (its console and admin API live under `/rustfs/`, so the prefix cannot be renamed). `oauth2-proxy-superset` has the same `/assets/` upstream next to `http://superset:8088/` (Superset's own files live under `/static/`, so the prefix shadows nothing). **Superset is not a path here** — it cannot run under a prefix ([§ Superset on its own hostname](#superset-on-its-own-hostname)). **n8n is not an upstream.** n8n is not exposed at all ([N8N.md § Access](../n8n/N8N.md#access-not-exposed)). Its subpath mode (`N8N_PATH=/n8n/`) is broken upstream (n8n-io/n8n#19635, #18596 — assets fall through to the SPA, login redirects to `/n8n/n8n`), so it never goes here; exposing it later means its own hostname behind another oauth2-proxy instance, built like `oauth2-proxy-superset` ([N8N.md § Exposing it later](../n8n/N8N.md#exposing-it-later)).
3. **Skip-auth allowlist** — `OAUTH2_PROXY_SKIP_AUTH_ROUTES=^/assets/`. Without this, oauth2-proxy would redirect the `<img>` request to Google login and the browser would get a redirected image response (broken image icon on the sign-in page). With it, the `/assets/*` upstream is reachable without a session cookie.

**Adding or replacing images.** Drop the file in the repo's top-level `assets/` folder and reference it by URL-encoded name (`/assets/My%20New%20Logo.png`). Nginx picks it up on the next request — no restart needed for asset changes, only for template changes. Filenames with spaces work, they just need URL encoding.

**Widening the skip-auth allowlist is a security decision.** Anything matching `OAUTH2_PROXY_SKIP_AUTH_ROUTES` bypasses group gating entirely. Keeping the regex tight (`^/assets/`) means only static branding is public — not, for example, arbitrary Open WebUI paths.

**Theme note.** The current logo asset is black-on-transparent and gets inverted to white via CSS `filter: invert(1)` on the dark background. If a designed white PNG is added later, drop `filter: invert(1)` from the `.logo` selector in both templates.

---

## Cloudflare tunnel routing

**First figure out which flavor of tunnel is actually serving `chat.zeoenergy.com`, then edit the right place.** Cloudflare tunnels come in two flavors and they are edited in completely different places — mixing them up is the single most common failure mode of this setup.

| Flavor | Where the config lives | How you edit it |
|---|---|---|
| **Locally-managed** | `/etc/cloudflared/config.yml` on the host (paired with a `credentials-file` JSON in `~/.cloudflared/`) | Edit the file, then `sudo systemctl restart cloudflared` (or the docker restart, if bind-mounted). Dashboard shows a read-only view — edits there are ignored |
| **Remotely-managed** | Cloudflare Zero Trust dashboard | Dashboard → tunnel → Public Hostnames. Cloudflared uses a `TUNNEL_TOKEN` and pulls config from the edge |

Identify which one is live on your host:

```bash
ps aux | grep -i cloudflared | grep -v grep
systemctl status cloudflared 2>/dev/null | head -5
```

- If a **systemd** `cloudflared.service` is `active (running)`, that's a **locally-managed** tunnel — the config file wins. Edit `/etc/cloudflared/config.yml`.
- If only a **docker container** (e.g. `ai-cloudflared`) is running and it was launched with `TUNNEL_TOKEN`, that's **remotely-managed**. Edit in the dashboard.
- If both are running, you have two tunnels and only one is actually receiving traffic for the hostname. Cloudflare DNS decides which — the tunnel whose UUID appears in the `chat.zeoenergy.com` CNAME record is the live one. The other tunnel's config changes have zero effect.

### Locally-managed edit

```yaml
# /etc/cloudflared/config.yml
tunnel: <tunnel-uuid>
credentials-file: /home/<user>/.cloudflared/<tunnel-uuid>.json

ingress:
  - hostname: chat.zeoenergy.com
    service: http://localhost:4180     # was http://localhost:8007 pre-oauth2-proxy
  - service: http_status:404
```

```bash
sudo systemctl restart cloudflared
```

### Remotely-managed edit

1. [Cloudflare Zero Trust dashboard](https://one.dash.cloudflare.com/) → **Networks → Tunnels**.
2. Click the tunnel that routes `chat.zeoenergy.com` → **Edit** → **Public Hostnames**.
3. Edit the row for `chat.zeoenergy.com`. Change **Service** to:
   - `http://<host-ip>:4180` (the edge's published host port for `oauth2-proxy-openwebui`), or
   - `http://oauth2-proxy-openwebui:4180` if cloudflared runs in a container on the `ai_shared` docker network (this repo's `ai/cloudflared`).
4. **Save**.

`superset.zeoenergy.com` is a separate row pointing at `http://oauth2-proxy-superset:4180` ([§ Superset on its own hostname](#superset-on-its-own-hostname)); it has no host port, so it needs the containerised cloudflared.

### Verify traffic is actually flowing through oauth2-proxy

Regardless of tunnel flavor, run this end-to-end check from any machine:

```bash
curl -sfL https://chat.zeoenergy.com/oauth2/ping && echo " → PASS" || echo " → FAIL"
curl -sfL https://superset.zeoenergy.com/oauth2/ping && echo " → PASS" || echo " → FAIL"
```

Expected: `OK → PASS` for each. If it returns HTML or a redirect to `/oauth/google/login` (chat) or Superset's login page, cloudflared is routing straight to the app and the group check is being bypassed.

---

## One gate

oauth2-proxy is the only sign-in. Open WebUI runs in trusted-header mode (`WEBUI_AUTH_TRUSTED_EMAIL_HEADER=X-Forwarded-Email`) and accepts the identity this proxy forwards — through a patch that makes it **verify** that identity with Google first, using the access token forwarded by `OAUTH2_PROXY_PASS_ACCESS_TOKEN=true`, and take the user's display name and profile picture from Google while it is there. Users see one Google prompt and land in the app.

Two things this proxy cannot do on its own, which is why the Open WebUI side is patched:

- It has no display name to forward. For the Google provider `X-Forwarded-User` is Google's `sub` claim (`providers/google.go`: `User: c.Subject`), a 21-digit number; `X-Auth-Request-Preferred-Username` is only populated under `--google-use-organization-id`. Stock Open WebUI used the number as the name — that is where the numeric usernames came from. `WEBUI_AUTH_TRUSTED_NAME_HEADER` is therefore deliberately blank.
- It has no picture to forward. Nothing in the session (`pkg/apis/sessions/session_state.go::GetClaim`) carries Google's `picture` claim.

The access token is the one thing it *can* forward that lets the upstream ask Google for both — and check the asserted e-mail against Google's answer, which stock trusted-header mode never does. Full write-up, verification, and the fallback to Open WebUI's own two-hop OAuth: [ai/openwebui/OPENWEBUI.md § Single sign-on](../openwebui/OPENWEBUI.md#single-sign-on).

Superset is the other upstream that trusts this identity — through its own instance, from `X-Forwarded-Email`, with no access-token check ([§ Superset on its own hostname](#superset-on-its-own-hostname)). Nothing uses `OAUTH2_PROXY_SET_XAUTHREQUEST` any more: it only adds `X-Auth-Request-*` to responses.

---

## Superset on its own hostname

Superset 4.0.2 cannot run under a path prefix. Its pages load `/static/…`, call `/api/v1/…` and redirect to `/login/`, and on `chat.zeoenergy.com` every path outside an upstream prefix belongs to Open WebUI, which uses `/static/` and `/api/` itself: `chat.zeoenergy.com/superset/` showed Superset's 404 page with a broken image. oauth2-proxy v7.15.5 matches upstreams by path only — its alpha config has no host-based matching — so one instance cannot serve two hostnames without a host router in front of Open WebUI, which was rejected. So Superset gets its own hostname, `SUPERSET_PUBLIC_HOST` (`superset.zeoenergy.com`), behind a second instance, `oauth2-proxy-superset`, and is served from the root. It is the one exception to "a new UI goes under `chat.zeoenergy.com/<path>/`"; a future UI with the same problem (n8n) follows this pattern.

| | `oauth2-proxy-openwebui` | `oauth2-proxy-superset` |
|---|---|---|
| Hostname | `chat.zeoenergy.com` (`OAUTH2_PROXY_COOKIE_DOMAINS`, `_REDIRECT_URL`, `_WHITELIST_DOMAINS`) | `SUPERSET_PUBLIC_HOST` |
| Upstreams | `OAUTH2_PROXY_UPSTREAMS` | `http://superset:8088/`, `http://oauth2-assets:80/assets/` |
| Cookie | `_oauth2_proxy`, SameSite `none` (sandbox iframes) | `_oauth2_proxy_superset`, SameSite `lax` |
| Sent to the upstream | `X-Forwarded-Email` / `-User` / `-Groups` + the Google access token | `X-Forwarded-Email` / `-User` / `-Groups` (`PASS_ACCESS_TOKEN=false`, `SET_XAUTHREQUEST=false`) |
| Upstream timeout | 30 s (oauth2-proxy's default) | 60 s (`OAUTH2_PROXY_UPSTREAM_TIMEOUT`, Superset's own frontend timeout) |
| LAN port | 4180, via the edge | none |
| `stop_grace_period` | 900 s | 90 s |
| From `.env`, shared | Google client, cookie secret / expire / refresh, e-mail domains, group, admin e-mail, service account, templates, skip-auth routes | the same |

The differences are fixed in the compose block (each with its reason); only the hostname is a variable. Superset needs no access token: Open WebUI presents it to Google to verify the identity, Superset has no such step, so forwarding it would only put a live Google credential into every Superset request.

**How a request signs in.** `oauth2-proxy-superset` deletes every `X-Forwarded-Email` the client sent — any spelling, `X_Forwarded_Email` included (`stripNormalizedHeader`, v7.15.5) — and sets the real one. Superset runs Flask-AppBuilder's `AUTH_REMOTE_USER`: its `/login/` view reads `request.environ[AUTH_REMOTE_USER_ENV_VAR]`, which `superset_config.py` sets to `HTTP_X_FORWARDED_EMAIL`, the key gunicorn files that header under. The first visit creates the account (role `Alpha`: no SQL Lab; pre-creating yourself as Admin and promoting an existing account are in [TRINO.md § Superset sign-in and roles](../trino/TRINO.md#superset-sign-in-and-roles)). `X-Auth-Request-*` plays no part: `SET_XAUTHREQUEST` puts them on responses only, and a client's own copy reaches the upstream untouched, so Superset must never trust them. The LAN side and the rest of the trust boundary: [TRINO.md § Header-auth trust boundary](../trino/TRINO.md#header-auth-trust-boundary).

**One-time setup outside the repo**, in this order:

1. Google OAuth client → **Authorized redirect URIs**: add `https://superset.zeoenergy.com/oauth2/callback` ([§ 1. OAuth 2.0 client](#1-oauth-20-client)).
2. Cloudflare → **Caching → Cache Rules**: add `superset.zeoenergy.com` to the bypass rule, before the hostname serves anything ([CLOUDFLARED.md § Cache rule](../cloudflared/CLOUDFLARED.md#cache-rule--bypass-for-the-authenticated-hostnames)).
3. `make up oauth2-proxy` starts `oauth2-proxy-superset` (during the rename, as step 3 of [§ Renaming / cutover](#renaming--cutover)).
4. Zero Trust → **Networks → Tunnels** → the tunnel → **Public Hostnames** → **Add a public hostname**: subdomain `superset`, domain `zeoenergy.com`, service type `HTTP`, URL `oauth2-proxy-superset:4180`. Add a row; never edit the `chat` one. Cloudflare creates the proxied DNS record itself.
5. Optional, before anyone's first visit: pre-create your account as Admin, or it starts as `Alpha` ([TRINO.md § Superset sign-in and roles](../trino/TRINO.md#superset-sign-in-and-roles)).
6. Verify: `curl -sfL https://superset.zeoenergy.com/oauth2/ping` prints `OK`; in a browser `https://superset.zeoenergy.com/` goes to Google, then to Superset signed in as your e-mail. The LiteLLM SGR dashboard is `https://superset.zeoenergy.com/superset/dashboard/litellm-sgrs/`.

Changing the hostname later: `SUPERSET_PUBLIC_HOST` in `.env`, the redirect URI, the tunnel public hostname and the cache rule together, then `make up oauth2-proxy`.

---

## Renaming / cutover

The chat instance was the compose service `oauth2-proxy`; it is now `oauth2-proxy-openwebui`. `scripts/deploy.py` finds containers by their compose service label, so the running `oauth2-proxy-<hash>` is an orphan to it: `make up` never swaps, retires or removes it. Two names must keep resolving during the move. The tunnel's `chat` rule still says `http://oauth2-proxy:4180` (in the dashboard): the new service carries the network alias `oauth2-proxy` for it. The edge's backend follows its `listen` section name, now `oauth2-proxy-openwebui`: that is why the order below matters.

Plain `make up oauth2-proxy` first does not work. The swap of `oauth2-proxy-openwebui` waits for the edge to mark the new copy UP in a backend of that name; the running edge still has `listen oauth2-proxy`, so deploy.py stops with "the running edge has no backend 'oauth2-proxy-openwebui' … run `make up edge`", removes the new copy and deploys nothing after it (`oauth2-assets`, `oauth2-proxy-superset`). Nothing breaks — the old container keeps serving. `FORCE=1` does not help: it only overrides the "edge is not running" refusal and the up-to-date shortcut, never the edge wait. The pre-swap edge check itself passes (the edge is running).

On the box, from the repo root, after pulling and editing `.env`:

1. `make up oauth2-proxy oauth2-proxy-openwebui RECREATE=1` — plain `docker compose up -d` of the new service, no edge wait. It starts as `ai-oauth2-proxy-oauth2-proxy-openwebui-1` and, through the alias, joins the DNS answers for `oauth2-proxy`: cloudflared and the not-yet-reloaded edge (`listen oauth2-proxy` resolves that name) spread chat over it and the old container.
2. `make up edge` — `haproxy -c`, then a hitless `USR2` reload. `listen oauth2-proxy-openwebui` resolves the copy from step 1, already healthy. The `listen superset` underscore-header rule ships in the same reload.
3. `make up oauth2-proxy` — swaps `oauth2-proxy-openwebui` to `oauth2-proxy-openwebui-<hash>` (the edge wait now passes) and retires the step-1 copy (`docker stop`, up to its 900 s grace while chats finish); `oauth2-assets` is up to date; `oauth2-proxy-superset` starts.
4. Retire the orphan gracefully — `docker stop` honours its own 900 s `stop_grace_period`, and the alias keeps `oauth2-proxy` resolving to the new copies:
   ```bash
   old=$(docker ps -aq --filter label=com.docker.compose.project=ai-oauth2-proxy \
                      --filter label=com.docker.compose.service=oauth2-proxy)
   [ -n "$old" ] && docker stop $old && docker rm $old
   ```
5. Zero Trust → the tunnel → **Public Hostnames** → `chat.zeoenergy.com` → service `http://oauth2-proxy-openwebui:4180` → **Save**. Check `curl -sfL https://chat.zeoenergy.com/oauth2/ping`.
6. In the repo: delete the `aliases:` lines from `oauth2-proxy-openwebui` (back to `- shared`), then `make up oauth2-proxy`.

Skipping step 1 also works (`make up edge`, then `make up oauth2-proxy`), but then LAN port 4180 answers `503` from the reload until the new copy is healthy and UP (about 30 s). Public chat is unaffected either way: cloudflared never crosses the edge.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| An oauth2-proxy container (either instance) exits immediately | `OAUTH2_PROXY_COOKIE_SECRET` is missing or not 32 bytes of URL-safe base64. Regenerate |
| Redirect to Google, then `redirect_uri_mismatch` error | The Google OAuth client is missing that hostname's `https://<host>/oauth2/callback` (`chat.zeoenergy.com` or `SUPERSET_PUBLIC_HOST`) in Authorized redirect URIs |
| Sign-in succeeds but 403 "You do not have access" from oauth2-proxy | The user is not in `zeoai.access@zeoenergy.com`, OR domain-wide delegation is misconfigured, OR the impersonated admin email doesn't exist on the tenant |
| oauth2-proxy logs `googleapi: Error 403: Not Authorized` | Domain-wide delegation missing the `admin.directory.group.readonly` scope, or admin email is not a Workspace admin |
| oauth2-proxy logs `unable to read service account key` | `sa-key.json` is missing from `ai/oauth2-proxy/` on the host, or the JSON is malformed |
| oauth2-proxy logs `Admin SDK API has not been used in project <id> before or it is disabled` (403) | The Google Cloud project owning the OAuth client + service account doesn't have the Admin SDK API enabled. Enable at `https://console.developers.google.com/apis/api/admin.googleapis.com/overview?project=<project-id>` — activation takes ~1 minute to propagate |
| oauth2-proxy logs `Not Authorized to access this resource/api, forbidden` (403) *after* Admin SDK API is enabled | The account in `OAUTH2_PROXY_GOOGLE_ADMIN_EMAIL` isn't a **Super Admin**. Directory-wide group reads require the Super Admin role — Admin or Groups Admin alone will fail with this exact error. Grant Super Admin (Admin console → Directory → Users → user → Admin roles → Super Admin), OR change `OAUTH2_PROXY_GOOGLE_ADMIN_EMAIL` to an account that already is |
| Users still hit Open WebUI directly, bypassing the proxy | Cloudflare tunnel is still routing `chat.zeoenergy.com` to `openwebui:8080` — update the tunnel's public hostname service URL |
| Revocation not taking effect | `OAUTH2_PROXY_COOKIE_REFRESH` interval hasn't elapsed. Reduce it, or have the user clear cookies for the hostname |
| Sign-in page renders blank (200 with empty body) | Template parse error. Check `make logs oauth2-proxy` at container startup — malformed Go template syntax fails silently at render time |
| Only one of sign-in / error pages picked up the custom branding | Both `sign_in.html` AND `error.html` must exist in `OAUTH2_PROXY_CUSTOM_TEMPLATES_DIR`. Missing one falls back to the built-in default for that page only |
| Anonymous `curl -sI https://chat.zeoenergy.com/static/favicon.png` (or a `superset.zeoenergy.com/static/…` file) returns `200` + `cf-cache-status: HIT` instead of `403`; Open WebUI's branded assets stay stale after a recreate | Not oauth2-proxy — Cloudflare's edge cached the authenticated response by file extension. Purge, then add the hostname to the bypass Cache Rule: [ai/cloudflared/CLOUDFLARED.md § Cache rule](../cloudflared/CLOUDFLARED.md#cache-rule--bypass-for-the-authenticated-hostnames) |
| `chat.zeoenergy.com/superset/` shows Open WebUI or a 404 | Superset moved to its own hostname, `https://superset.zeoenergy.com/` ([§ Superset on its own hostname](#superset-on-its-own-hostname)) |
| On `superset.zeoenergy.com`, Superset's own login page or "Invalid login" after Google | The identity header did not reach Superset: `SUPERSET_REMOTE_USER_HEADER` must be `X-Forwarded-Email` and `oauth2-proxy-superset` must have `PASS_USER_HEADERS=true`; a Superset image built before the `AUTH_REMOTE_USER_ENV_VAR` fix never signs anyone in — `make build trino superset && make up trino superset` |
| A slow Superset chart ends in `502` from oauth2-proxy | The query outlived `OAUTH2_PROXY_UPSTREAM_TIMEOUT` (60 s on `oauth2-proxy-superset`), which matches Superset's own frontend timeout; the query itself needs to get faster |
| `make up oauth2-proxy` fails with "the running edge has no backend 'oauth2-proxy-openwebui'" | The edge still runs the config with `listen oauth2-proxy`. `make up edge`, then retry ([§ Renaming / cutover](#renaming--cutover)) |

### Useful commands

```bash
# Follow the logs of all three services, or of one instance
make logs oauth2-proxy
make logs oauth2-proxy oauth2-proxy-superset

# Apply a config change — swaps side by side, no downtime (ai/edge/EDGE.md)
make up oauth2-proxy

# Verify each instance can reach its upstream over the shared network
make exec oauth2-proxy oauth2-proxy-openwebui CMD='wget -qO- http://openwebui:8080/health'
make exec oauth2-proxy oauth2-proxy-superset CMD='wget -qO- http://superset:8088/health'
```

---

## What NOT to commit

- `ai/oauth2-proxy/sa-key.json` — service account private key
- `.env` values for `OAUTH2_PROXY_COOKIE_SECRET`, `OPENWEBUI_GOOGLE_CLIENT_SECRET`

Both should be in `.gitignore` already.

## What TO commit

- `ai/oauth2-proxy/templates/sign_in.html` and `ai/oauth2-proxy/templates/error.html` — branded pages, no secrets. The `.gitignore` rule under `ai/oauth2-proxy/` is scoped narrowly to `sa-key.json` on purpose so these templates are tracked. If you widen the ignore to `ai/oauth2-proxy/*` again, the templates will silently disappear from git and the next deploy on a clean checkout will fall back to the default oauth2-proxy pages.
