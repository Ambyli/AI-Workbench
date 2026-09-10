# Open WebUI

Browser-based chat interface for the LiteLLM proxy. Open WebUI sees LiteLLM as a single OpenAI-compatible provider, so every model defined in `litellm_config.yaml` automatically appears in the model picker.

### Quick start

```bash
docker compose -f ai/openwebui/docker-compose.openwebui.yml --env-file .env up -d
```

Or via make:

```bash
make up openwebui
```

Then open `http://localhost:8007`. The first account created becomes the admin. Subsequent sign-ups land in an approval queue (see [User signup & approval](#user-signup--approval) below).

> Prefer a native window over a browser tab? A standalone desktop client is available at <https://github.com/open-webui/desktop> — point it at `http://localhost:8007` after the container is up.

| Container | Port | Purpose |
|---|---|---|
| `openwebui` | `localhost:8007` | Chat UI — talks to LiteLLM over the `ai_shared` Docker network |

### How it connects to LiteLLM

Both containers are attached to the `ai_shared` network, so Open WebUI reaches the proxy via the Docker service name — `http://litellm:4000/v1` — not via the host port. LiteLLM does **not** need to be exposed on the host for this to work; it is exposed at `localhost:4001` only for direct API use.

The connection settings are passed in once at first launch:

Every value is sourced from `.env` so configuration lives in one file.

| Open WebUI env var | `.env` key | Default | Notes |
|---|---|---|---|
| `OPENAI_API_BASE_URL` | `OPENWEBUI_OPENAI_API_BASE_URL` | `http://litellm:4000/v1` | Must be the Docker service DNS name in compose, not localhost |
| `OPENAI_API_KEY` | `OPENWEBUI_OPENAI_API_KEY` | _(empty — set to a virtual key)_ | LiteLLM virtual key scoped to the chat models Open WebUI should see. See [Restricting visible models](#restricting-visible-models) |
| `ENABLE_OLLAMA_API` | `OPENWEBUI_ENABLE_OLLAMA_API` | `false` | Disables the Ollama discovery probe |
| `WEBUI_SECRET_KEY` | `OPENWEBUI_SECRET_KEY` | _(placeholder — rotate)_ | Signs sessions; stable value required to avoid log-outs on restart |
| `WEBUI_URL` | `OPENWEBUI_WEBUI_URL` | `http://localhost:8007` | Public base URL; used to build OAuth callback URLs |
| `WEBUI_NAME` | `OPENWEBUI_WEBUI_NAME` | `Zeo AI Chat` | Tab title, PWA manifest name, OpenSearch descriptor. Renders with a forced ` (Open WebUI)` suffix — see [Branding](#branding) |
| `ENABLE_SIGNUP` | `OPENWEBUI_ENABLE_SIGNUP` | `true` | New accounts can be created; pair with `DEFAULT_USER_ROLE=pending` for gated access |
| `DEFAULT_USER_ROLE` | `OPENWEBUI_DEFAULT_USER_ROLE` | `pending` | New signups land in the admin approval queue. First-ever account is always admin regardless of this value |
| `ENABLE_OAUTH_SIGNUP` | `OPENWEBUI_ENABLE_OAUTH_SIGNUP` | `true` | Master switch for OAuth login flows |
| `OAUTH_MERGE_ACCOUNTS_BY_EMAIL` | `OPENWEBUI_OAUTH_MERGE_ACCOUNTS_BY_EMAIL` | `true` | OAuth logins are merged into existing local accounts with the same email |
| `GOOGLE_CLIENT_ID` | `OPENWEBUI_GOOGLE_CLIENT_ID` | _(empty)_ | Google Cloud OAuth 2.0 client ID — see [Google OAuth setup](#google-oauth-setup) |
| `GOOGLE_CLIENT_SECRET` | `OPENWEBUI_GOOGLE_CLIENT_SECRET` | _(empty)_ | Matching client secret |
| `OPENID_PROVIDER_URL` | `OPENWEBUI_OPENID_PROVIDER_URL` | Google discovery doc | OIDC discovery document URL; required for clean provider-side logout |
| `OAUTH_AUTO_REDIRECT` | `OPENWEBUI_OAUTH_AUTO_REDIRECT` | `true` | Skip the login page and redirect straight to Google — see [Single sign-on](#single-sign-on) |
| `ENABLE_LOGIN_FORM` | `OPENWEBUI_ENABLE_LOGIN_FORM` | `false` | Hides the email/password form. **Required** for `OAUTH_AUTO_REDIRECT` to do anything |
| `OAUTH_UPDATE_NAME_ON_LOGIN` | `OPENWEBUI_OAUTH_UPDATE_NAME_ON_LOGIN` | `true` | Re-read the `name` claim on every login, not just at account creation (upstream default: `false`) |
| `OAUTH_UPDATE_PICTURE_ON_LOGIN` | `OPENWEBUI_OAUTH_UPDATE_PICTURE_ON_LOGIN` | `true` | Re-read the `picture` claim on every login (upstream default: `false`) |
| `WEBUI_AUTH_TRUSTED_EMAIL_HEADER` | `OPENWEBUI_WEBUI_AUTH_TRUSTED_EMAIL_HEADER` | _(blank — off)_ | Trusted-header SSO. Deliberately disabled; see [Single sign-on](#single-sign-on) |
| `WEBUI_AUTH_TRUSTED_NAME_HEADER` | `OPENWEBUI_WEBUI_AUTH_TRUSTED_NAME_HEADER` | _(blank — off)_ | ″ |
| `USER_AGENT` | `OPENWEBUI_USER_AGENT` | `OpenWebUI/1.0 (+github.com/open-webui/open-webui)` | User-Agent applied to outbound HTTP from RAG / web loaders (langchain_community); silences the "USER_AGENT not set" warning |
| `MCP_INITIALIZE_TIMEOUT` | `OPENWEBUI_MCP_INITIALIZE_TIMEOUT` | `30` | Seconds to wait for an MCP server's initialize handshake; raise for slow cold-starts (upstream default: 10) |
| `CORS_ALLOW_ORIGIN` | `CORS_ALLOW_ORIGIN` | `*` | Tighten to a specific origin if another web app calls Open WebUI's API from the browser |
| `HF_TOKEN` | `HF_TOKEN` | _(shared with vLLM)_ | Used for gated embedding / RAG model downloads. Same token also drives vLLM gated model downloads |

> **Important:** Open WebUI only reads these env vars on the **first launch**. Once the SQLite store under `/app/backend/data` is initialized, further changes must be made through **Admin Settings → Connections** in the UI, or by deleting the `openwebui_data` volume and starting fresh.

### `WEBUI_SECRET_KEY`

Sessions are signed with this key. If it changes between container restarts, every user is logged out. Generate one with:

```bash
openssl rand -hex 32
```

Paste the output into `OPENWEBUI_SECRET_KEY` in `.env`. The default placeholder (`change-me-run-openssl-rand-hex-32`) is fine for a first boot but should be rotated before any real use.

### Branding

Two independent surfaces: the **name** (one env var) and the **icons** (file mounts). Both are wired up already — this section explains what drives what, so a version bump doesn't silently un-brand the deployment.

#### Name

`OPENWEBUI_WEBUI_NAME` in `.env`. One caveat, from `backend/open_webui/env.py`:

```python
WEBUI_NAME = os.getenv('WEBUI_NAME', 'Open WebUI')
if WEBUI_NAME != 'Open WebUI':
    WEBUI_NAME += ' (Open WebUI)'
```

So `Zeo AI Chat` renders as **"Zeo AI Chat (Open WebUI)"** everywhere the name appears. The suffix is deliberate and license-backed; removing it means patching the image.

Unlike `ENABLE_WEB_SEARCH`, the `AUDIO_TTS_*` block, and most of this compose file, `WEBUI_NAME` is **not** a first-boot-only `PersistentConfig` value — `main.py` assigns `app.state.WEBUI_NAME` from the env on every boot. A plain `make up openwebui` (recreate) applies a change; no need to wipe `openwebui_data`.

#### Icons — only `STATIC_DIR` matters

`src/app.html` requests `/static/favicon.png`, `/static/favicon-96x96.png`, `/static/favicon.svg`, `/static/favicon.ico`, `/static/apple-touch-icon.png`, `/static/loader.js`, and `/static/custom.css`. In `main.py`:

```python
app.mount('/static', StaticFiles(directory=STATIC_DIR), name='static')   # ~2989
...
app.mount('/', SPAStaticFiles(directory=FRONTEND_BUILD_DIR, html=True))  # ~3037
```

`STATIC_DIR` resolves to **`/app/backend/open_webui/static`** (the image's `WORKDIR` is `/app/backend` and the Dockerfile does `COPY ./backend .`). Because `/static` is mounted ahead of the SPA catch-all, overwriting `/app/build/static/` — the recipe most blog posts and issue threads give — has **no effect**. Only the backend directory is served.

The compose file bind-mounts individual files read-only rather than mounting the whole directory: `STATIC_DIR` also contains `fonts/`, `swagger-ui/`, `assets/`, `user.png`, and `user-import.csv`, all of which a directory mount would hide.

#### Which file drives which surface

| File | Mark | Surface |
|---|---|---|
| `favicon.png` (512²) | black | Browser tab — **and** the in-app mark (see below) |
| `favicon-96x96.png`, `favicon.svg`, `favicon.ico` | black | Browser tab, other formats. `favicon.svg` must be overridden too, or a browser that prefers SVG shows the upstream logo |
| `apple-touch-icon.png` (180², opaque white) | black | iOS home screen. Opaque on purpose — iOS composites alpha to black |
| `logo.png` (500², opaque `#171717`) | white | PWA install icon. Hardcoded in the `/manifest.json` route in `main.py`, not read from `site.webmanifest` |
| `splash.png` (580×500) | black | Load splash, light theme |
| `splash-dark.png` (580×500) | white | Load splash, dark theme — Open WebUI's default when no theme is stored |
| `custom.css` | — | Upstream's own hook, empty by default. Repoints the in-app mark |
| `zeo-mark-white.png` (512²) | white | Target of the `custom.css` rule; not referenced by upstream |

#### Why there's a `custom.css`

`/static/favicon.png` does double duty in v0.11.x — it is both the browser-tab icon (`app.html`, and re-injected at runtime by `src/routes/+layout.svelte`, which is the one the browser actually settles on) **and** the in-app mark: sidebar logo, default assistant avatar, auth and onboarding screens, notification toasts, and the Models editor default profile image.

One file, two backgrounds. The file itself is the **black** mark so the tab reads correctly; `custom.css` repoints only the in-app `<img>` uses at the **white** mark:

```css
img[src$="/static/favicon.png"] {
	content: url('/static/zeo-mark-white.png');
}
```

CSS cannot reach the tab icon, so the split is clean. `content: url()` on a regular element needs Chrome/Edge 68+, Safari 10+, or Firefox 137+; older browsers just show the black mark in-app, with no layout change.

To collapse back to one mark everywhere, drop the `custom.css` and `zeo-mark-white.png` mounts and point `favicon.png` at whichever variant you prefer.

#### Regenerating the assets

Everything under `assets/openwebui/` is derived from `assets/Zeo Favicon Black.png` and `assets/Zeo Favicon White.png` (770×663 RGBA). To rebuild after a logo change, re-run the generator — it trims the transparent border, then fits each canvas:

```bash
python ai/openwebui/bin/generate_branding.py
```

Needs Pillow, which the uv workspace already pulls in via `widget/pyproject.toml`; outside the workspace venv, `pip install Pillow`. The script is deterministic — re-running it with unchanged sources rewrites byte-identical files.

Then `make up openwebui` to recreate. Browsers cache favicons aggressively; hard-reload or check in a private window.

> The same `assets/` folder is bind-mounted by the `oauth2-assets` sidecar and served unauthenticated at `/assets/*` (see [ai/oauth2-proxy/OAUTH2_PROXY.md](../oauth2-proxy/OAUTH2_PROXY.md)), which is how the sign-in page shows the Zeo logo pre-login. `assets/openwebui/*` is therefore also reachable at `/assets/openwebui/*` — harmless, these are public branding files, but don't put anything private there.

#### License

Open WebUI's license (clause 4) prohibits altering or removing its branding, with an exemption for deployments serving **50 or fewer end users** in any rolling 30-day period — plus separate exemptions for written permission from the copyright holder or an executed enterprise license. Keep an eye on the headcount in Admin Panel → Users; crossing 50 puts this configuration outside the exemption. The forced ` (Open WebUI)` name suffix is left intact regardless.

### Health check

Polls `http://localhost:8080/health` inside the container every 30s with a 30s startup grace period.

### Stopping

```bash
docker compose -f ai/openwebui/docker-compose.openwebui.yml down
# or
make down openwebui
```

User data, chat history, and uploaded files are stored in the named volume `openwebui_data` and survive restarts. To wipe everything (and force re-reading the env vars on next launch):

```bash
docker compose -f ai/openwebui/docker-compose.openwebui.yml down -v
```

### User signup & approval

The deployment is configured so anyone with the URL can register, but new accounts cannot chat until an admin approves them. Workflow:

1. A new user visits `http://localhost:8007` and clicks **Sign up** (or uses Google — see below).
2. The account is created with role `pending`. The user sees a "waiting for admin approval" screen.
3. An admin opens **Admin Panel → Users**, finds the pending row, and changes their role to **User**.
4. The user refreshes; they can now select a model and chat.

To revoke access, set the user's role back to `pending` (silent suspension) or delete the account.

> Want it fully open? Set `OPENWEBUI_DEFAULT_USER_ROLE=user` in `.env`. Want it fully locked? Set `OPENWEBUI_ENABLE_SIGNUP=false`. Either change requires either an Admin Settings toggle on the running container or a volume wipe (see the warning above the env table).

### Single sign-on

Users hit two gates on the way in: **oauth2-proxy** at the edge (which enforces the Workspace-group membership check) and then **Open WebUI's own Google OIDC**. Both use the same Google account.

That looks redundant, and it briefly wasn't — Open WebUI was switched to trusted-header SSO (`WEBUI_AUTH_TRUSTED_EMAIL_HEADER=X-Forwarded-Email`, `WEBUI_AUTH_TRUSTED_NAME_HEADER=X-Forwarded-User`) so it would accept the identity oauth2-proxy had already verified. **That has been reverted.** It works, but it costs user identity in three ways.

#### Why trusted-header SSO was reverted

**Display names became 21-digit numbers.** oauth2-proxy's Google provider puts Google's `sub` claim in the session user field:

```go
// providers/google.go
Email:        c.Email,
User:         c.Subject,     // e.g. "117402938475019283746"
```

`PASS_USER_HEADERS=true` forwards that as `X-Forwarded-User`, and Open WebUI uses the name header verbatim:

```python
# routers/auths.py
name = request.headers.get(WEBUI_AUTH_TRUSTED_NAME_HEADER, email)
```

`X-Forwarded-User` is an account identifier, not a display name, and no oauth2-proxy header carries the real one. `X-Auth-Request-Preferred-Username` would, but `google.go` only wires `setPreferredUsername` under `--google-use-organization-id`, which this deployment does not set (it uses the service-account JSON purely for the group gate).

**Profile pictures were impossible.** The trusted-header signup path calls `signup_handler()` with no image argument, so every account took the default:

```python
async def signup_handler(..., profile_image_url: str = '/user.png', ...)
```

No header carries Google's `picture` claim, and that code path never fetches one. This was not a misconfiguration — it cannot work in that mode.

**Names were write-once.** `signup_handler` only runs the first time an email is seen, so a bad name was never corrected on a later login.

#### What runs now

Open WebUI's own OIDC reads `OAUTH_USERNAME_CLAIM` (`name`) and `OAUTH_PICTURE_CLAIM` (`picture`) properly. Three settings make it seamless and self-repairing:

| Setting | Why |
|---|---|
| `OAUTH_AUTO_REDIRECT=true` | No "Continue with Google" click — the login page redirects immediately |
| `ENABLE_LOGIN_FORM=false` | **Required by the above** — see the precondition list below |
| `OAUTH_UPDATE_NAME_ON_LOGIN=true` | Existing accounts pick up the real name on next sign-in |
| `OAUTH_UPDATE_PICTURE_ON_LOGIN=true` | Existing accounts pick up the avatar on next sign-in |

`OAUTH_AUTO_REDIRECT` alone does nothing. The frontend refuses to bounce a user to SSO unless the deployment is unambiguously SSO-only — `src/routes/auth/+page.svelte` requires **all** of:

```js
$config?.oauth?.auto_redirect && !logout && !form && !error
  && providers.length === 1                        // only Google is configured
  && $config?.features?.auth !== false
  && $config?.features?.enable_login_form === false  // ← the easy one to miss
  && !$config?.features?.enable_ldap
  && !$config?.features?.auth_trusted_header         // ← so it also can't coexist
  && !$config?.onboarding                            //   with trusted-header SSO
  && !localStorage.token && !document.cookie…token=
```

Note the `auth_trusted_header` condition: auto-redirect and trusted-header SSO are mutually exclusive by design, which is another reason the two approaches don't mix.

If sign-in lands on Open WebUI's login page instead of bouncing to Google, work down that list — `enable_login_form` is the usual culprit.

#### Break-glass if Google OAuth breaks

`/auth?form=true` suppresses the auto-redirect *and* re-renders the hidden email/password form — `+page.svelte` gates the form on `enable_login_form || enable_ldap || form`. That gets you a login box, but only helps if a local password actually exists: OAuth-created accounts are given a random `uuid4()` as their password and nobody knows it.

So the real recovery path is `OPENWEBUI_ENABLE_LOGIN_FORM=true` in `.env` + `make up openwebui`. If you want a genuine standing break-glass account, set a password on one admin user through Admin Panel → Users *before* you need it.

The last two default to `false` upstream. They are what repairs accounts created while trusted-header SSO was on — those have a numeric name and the placeholder avatar, and heal the next time each person signs in. No admin cleanup, no account deletion.

The second hop is **not** a second login prompt. The user already holds a live Google session and prior consent from clearing oauth2-proxy, so Google returns immediately; with auto-redirect on, the whole thing is a redirect bounce.

> **Two different config lifetimes here — don't assume.** The `OAUTH_*` settings are read from the environment on every boot: `ENABLE_OAUTH_PERSISTENT_CONFIG` defaults to `false` upstream, and `models/config.py::persistent_enabled_for` short-circuits any key starting with `oauth.` before it ever reaches the DB. `ENABLE_LOGIN_FORM` is a `ui.*` key and *is* PersistentConfig-backed, like `ENABLE_WEB_SEARCH` and the `AUDIO_TTS_*` block — but the fallback is per-key, not first-boot-only: `Config.get` returns the env-derived default whenever no DB row exists, and rows are only written when a setting is saved through the Admin UI or API. So it applies on recreate on an install where nobody has touched it. If it doesn't take effect, a row exists — set it at Admin Panel → Settings → General instead.

#### Security note

While the trusted headers were set, anyone able to reach `openwebui:8080` directly could send `X-Forwarded-Email: someone@zeoenergy.com` and land in that account with no Google prompt. That is why `PORT_OPENWEBUI` binds to `127.0.0.1` rather than `0.0.0.0`. With the headers blank the bypass is disarmed, but **the loopback bind stays** — as defense in depth, and so that re-enabling the headers can never silently re-open the hole.

If you do re-enable them, re-read this section first, and verify the bind is still on loopback.

### Google OAuth setup

Open WebUI supports Google sign-in for either of two reasons: skipping password creation, or restricting access to specific Google Workspace domains.

**Create the OAuth client:**

1. Open <https://console.cloud.google.com/apis/credentials> and select (or create) a project.
2. Click **Create Credentials → OAuth client ID**. App type: **Web application**.
3. Under **Authorized redirect URIs** add exactly the value matching `OPENWEBUI_WEBUI_URL` with `/oauth/google/callback` appended. For this deployment:
   ```
   https://chat.zeoenergy.com/oauth/google/callback
   ```
   The path `/oauth/google/callback` is fixed. Google will reject any redirect URI that points at a private IP (`192.168.x.x`, `10.x.x.x`, …) — use a public hostname (via Cloudflare Tunnel, see below) or `localhost` for testing.
4. Copy the generated **Client ID** and **Client secret** into `.env`:
   ```
   OPENWEBUI_GOOGLE_CLIENT_ID=...
   OPENWEBUI_GOOGLE_CLIENT_SECRET=...
   ```
5. `OPENWEBUI_OPENID_PROVIDER_URL` is preset to Google's discovery document — leave it alone unless you're swapping providers. Without it, Open WebUI logs `OPENID_PROVIDER_URL not set - logout will not work!` and the logout flow only clears the local cookie.
6. Restart the container so the new values take effect:
   ```bash
   make down openwebui && make up openwebui
   ```

A **Continue with Google** button appears on the login screen once both values are populated and `OPENWEBUI_ENABLE_OAUTH_SIGNUP=true`.

**First Google login behavior:**

- If a local account with the same email already exists, `OAUTH_MERGE_ACCOUNTS_BY_EMAIL=true` links them — same user, two sign-in methods.
- If not, a new account is created with role `pending` (per `OPENWEBUI_DEFAULT_USER_ROLE`) and must be approved.

> **About the running container:** Env vars are only read on the *very first* boot — the SQLite store in `openwebui_data` is authoritative afterwards. If you change OAuth settings after the container has been initialized, either toggle the equivalent setting in **Admin Panel → Settings → General** or wipe the volume with `docker compose -f ai/openwebui/docker-compose.openwebui.yml down -v` and start fresh (this deletes all chat history and users).

### Public hostname via Cloudflare Tunnel

For LAN-wide or off-LAN access, Open WebUI is fronted by a Cloudflare Tunnel rather than exposed directly. Cloudflare terminates TLS and routes `https://chat.zeoenergy.com` to `http://localhost:8007` on the host, so:

- No `/etc/hosts` edits on any client.
- No router port-forwarding.
- HTTPS for free (Google OAuth requires HTTPS for non-localhost callbacks).
- Public hostname → Google OAuth accepts the redirect URI.

**Setup (one-time):**

1. In the Cloudflare dashboard go to **Zero Trust → Networks → Tunnels → Create a tunnel**. Pick **Cloudflared** as the connector.
2. Name it something like `openwebui`, save, then copy the **install token** from the *Linux / Docker* tab.
3. Paste the token into `.env`:
   ```
   CLOUDFLARE_TUNNEL_TOKEN=eyJh...
   ```
4. Add a public hostname route on the tunnel:
   - **Subdomain:** `chat`
   - **Domain:** `zeoenergy.com`
   - **Service type:** `HTTP`
   - **URL:** `openwebui:8080`
   > **Important:** use the Docker service name + internal port, not `localhost:8007`. The `cloudflared` container is on the `ai_shared` network and reaches Open WebUI via Docker DNS; `localhost` would resolve to the cloudflared container itself.
5. Cloudflare will auto-create the CNAME DNS record for `chat.zeoenergy.com` pointing at the tunnel.
6. Start the tunnel container:
   ```bash
   make up cloudflared
   ```
   Stop with `make down cloudflared`. Logs: `make logs cloudflared`.
7. Update `OPENWEBUI_WEBUI_URL=https://chat.zeoenergy.com` and `CORS_ALLOW_ORIGIN=https://chat.zeoenergy.com` in `.env` (already set), then restart Open WebUI:
   ```bash
   make down openwebui && make up openwebui
   ```
8. Update the Google OAuth client's **Authorized redirect URIs** to include `https://chat.zeoenergy.com/oauth/google/callback`.

**Verifying:**

```bash
docker logs ai-cloudflared --tail 50   # should show "Registered tunnel connection"
curl -I https://chat.zeoenergy.com     # should return 200 from Open WebUI
```

If the tunnel is up but the site 502s, the origin URL (`openwebui:8080`) is unreachable from the `cloudflared` container — confirm Open WebUI is running (`make up openwebui`) and on the same `ai_shared` network.

#### Two tunnel flavors — pick one and know which

Cloudflare tunnels come in two flavors that are edited in completely different places. Getting these confused wastes hours because the "wrong" tunnel's edits silently do nothing.

| Flavor | Config lives in | Edit via |
|---|---|---|
| **Locally-managed** | `/etc/cloudflared/config.yml` on the host, paired with a `credentials-file` JSON in `~/.cloudflared/` | Edit the file, then `sudo systemctl restart cloudflared` (systemd) or restart the container (docker w/ bind mount). Dashboard view is **read-only** — edits there do nothing |
| **Remotely-managed** | Cloudflare Zero Trust dashboard | Dashboard → tunnel → Public Hostnames. Cloudflared connects with a `TUNNEL_TOKEN` and pulls config from the edge |

Which one is live? Check the host:

```bash
ps aux | grep -i cloudflared | grep -v grep
systemctl status cloudflared 2>/dev/null | head -5
```

- Systemd service running → **locally-managed**. Config file is authoritative. If oauth2-proxy is deployed in front of Open WebUI, the `service:` line for `chat.zeoenergy.com` must point at `http://localhost:${PORT_OAUTH2_PROXY}` (typically `4180`) instead of `http://localhost:${PORT_OPENWEBUI}` (typically `8007`). See [`OAUTH2_PROXY.md`](OAUTH2_PROXY.md).
- Only a `cloudflared` container using `TUNNEL_TOKEN` → **remotely-managed**. Dashboard is authoritative.
- **Both running** → you have two tunnels. Cloudflare DNS decides which serves `chat.zeoenergy.com` (whichever tunnel UUID is in the CNAME). The other tunnel's config changes have zero effect. Diagnose with `docker logs -f ai-cloudflared` while hitting the site — if no request activity appears, you're editing the wrong tunnel.

#### End-to-end smoke test

```bash
curl -sfL https://chat.zeoenergy.com/oauth2/ping && echo " → PASS" || echo " → FAIL"
```

If oauth2-proxy is in the path, this returns `OK → PASS`. If it returns HTML or a 302 to `/oauth/google/login`, the tunnel is bypassing oauth2-proxy — the wrong tunnel's config was edited, or the service URL still points at Open WebUI's port.

### MCP tools (Phoenix)

Open WebUI v0.6.31+ supports MCP servers over **Streamable HTTP** natively — no `mcpo` proxy or bridge needed. Phoenix already speaks that transport, so connecting it is purely an admin-UI action.

**Register Phoenix as an external tool:**

1. Log in as admin → **Admin Panel → Settings → External Tools** (or `Tools` in some versions).
2. Click **+ Add Server**.
3. Fill in:
   - **Type:** `MCP (Streamable HTTP)`
   - **Server URL:** `https://phoenix-mcp.com/mcp` (the value of `DEFAULT_LITELLM_MCP_PHOENIX_URL` in `.env`)
   - **Auth type:** `Bearer`
   - **Key:** the value of `DEFAULT_LITELLM_MCP_PHOENIX_AUTH_VALUE` in `.env`
   - **Name:** `phoenix` (or anything memorable)
4. Save. Open WebUI calls `initialize` against the server, lists its tools, and they become available to chats.

**Using it in a chat:**

In a chat, click the **Tools** icon (paperclip-like) → toggle **phoenix** on. The model can now invoke Phoenix's database tools mid-response.

> **Heads-up:** Open WebUI's MCP support is parallel to the `mcp_servers` block in `litellm_config.yaml`. The LiteLLM block lets *models routed through LiteLLM* (e.g. Claude Code via the proxy) call Phoenix tools server-side. The Open WebUI registration lets the *Open WebUI chat itself* call Phoenix tools client-side. Both can coexist using the same URL + token; they're not exclusive.

**If the handshake times out** (you see "MCP server failed to initialize" in the logs), bump `OPENWEBUI_MCP_INITIALIZE_TIMEOUT` in `.env` (currently `30s`, upstream default is `10s`) and restart Open WebUI.

### Restricting visible models

Open WebUI populates its model picker by calling `GET /v1/models` against LiteLLM, using whatever API key it's been given. The LiteLLM **master key** sees every model defined in `litellm_config.yaml` — including non-chat entries like `kokoro` (TTS), which would clutter the picker. The fix is to give Open WebUI a **virtual key** scoped to just the chat models you want it to see.

**Create a virtual key in LiteLLM:**

1. Open the LiteLLM Admin UI at `http://localhost:4001/ui/` and sign in with `DEFAULT_LITELLM_MASTER_KEY`.
2. Go to **Virtual Keys → Create Key**.
3. Under **Models**, select only the chat-capable models Open WebUI should expose (e.g. `qwen3.6-unsloth`, `qwen3.6`, `qwen2.5-vl`). Leave audio-only models like `kokoro` unchecked.
4. (Optional) Give the key a friendly name like `openwebui`, set a budget, set TTL.
5. Copy the generated key.

Or via the API:

```bash
curl -X POST http://localhost:4001/key/generate \
  -H "Authorization: Bearer $DEFAULT_LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "models": ["qwen3.6-unsloth", "qwen3.6", "qwen2.5-vl"],
    "key_alias": "openwebui"
  }'
```

**Use it in Open WebUI:**

```
OPENWEBUI_OPENAI_API_KEY=sk-...
```

Restart the container — `make down openwebui && make up openwebui`. After login, only the models on the virtual key's allowlist appear in the chat picker. Adding or removing models later only needs the virtual-key allowlist to be edited; no Open WebUI restart is required (Open WebUI re-fetches `/v1/models` on every page load).

### Adding more LiteLLM models

Models are configured in `litellm_config.yaml`, not in Open WebUI. After editing that file and restarting LiteLLM, the new model appears in Open WebUI's model picker automatically — Open WebUI calls `GET /v1/models` against LiteLLM to populate the list.

### Voice (TTS via Kokoro)

Open WebUI's read-aloud button and **Call** mode (the headphones icon in the composer) need a text-to-speech engine. This stack uses the Kokoro-82M service in `ai/kokoro/` — `kokoro-api` exposes an OpenAI-compatible `POST /v1/audio/speech`, so Open WebUI's built-in **OpenAI** TTS engine works against it unchanged. Speech-to-text (your microphone in Call mode) stays on the faster-whisper model bundled inside the Open WebUI image; nothing extra to deploy.

**Why direct to `kokoro-api`, not through LiteLLM.** LiteLLM does route `model: kokoro` to the same endpoint, but Open WebUI's `OPENWEBUI_OPENAI_API_KEY` is a virtual key scoped to chat models only (see [Restricting visible models](#restricting-visible-models)) — adding `kokoro` to it would put a TTS entry in the chat model picker, and a second key just for audio is a manual step with nothing to show for it. `kokoro-api` has no auth and is only reachable inside `ai_shared`, so Open WebUI talks to `http://kokoro-api:8000/v1` directly. Same pattern as SearXNG. If you'd rather have TTS calls in LiteLLM's spend logs, create a virtual key scoped to just `kokoro` and set the base URL to `http://litellm:4000/v1` instead.

**Settings** (all in `.env`, passed through by the compose file):

| `.env` key | Value | Notes |
|---|---|---|
| `OPENWEBUI_AUDIO_TTS_ENGINE` | `openai` | Open WebUI's OpenAI-compatible engine |
| `OPENWEBUI_AUDIO_TTS_OPENAI_API_BASE_URL` | `http://kokoro-api:8000/v1` | Open WebUI appends `/audio/speech` |
| `OPENWEBUI_AUDIO_TTS_OPENAI_API_KEY` | `none` | Any non-empty string; kokoro-api ignores it |
| `OPENWEBUI_AUDIO_TTS_MODEL` | `kokoro` | Passed as `model`; kokoro-api ignores it |
| `OPENWEBUI_AUDIO_TTS_VOICE` | `af_heart` | OpenAI alias (`alloy`…`shimmer`, all English) or any Kokoro voice — `curl http://localhost:8004/languages` lists them by language |
| `OPENWEBUI_AUDIO_TTS_SPLIT_ON` | `punctuation` | One request per sentence. Keep it — Kokoro returns only the first pipeline chunk of a long input, so `none` would truncate long replies |
| `OPENWEBUI_WHISPER_MODEL` | `base` | STT model size for the bundled faster-whisper (CPU) |

Kokoro only emits WAV. Open WebUI reads the upstream `Content-Type`, sees it isn't MP3, and transcodes with pydub/ffmpeg before caching — no `response_format` handling is needed on either side.

**These are PersistentConfig values.** Like the web-search block, Open WebUI reads them into its database on the **first boot of a fresh `openwebui_data` volume only**. On the existing install they will not take effect from `.env`; set them once in the UI instead:

1. Admin Panel → **Settings → Audio**.
2. **Text-to-Speech Engine**: `OpenAI`. API Base URL `http://kokoro-api:8000/v1`, API Key `none`.
3. **TTS Model**: `kokoro`. **TTS Voice**: `af_heart` (or any voice from `/voices`). **Response splitting**: `Punctuation`.
4. Leave **Speech-to-Text Engine** on the default (`Whisper (Local)`), model `base`.
5. Save, then open any chat and click the speaker icon under a response — audio should start after a second or two. The very first request is slow while `kokoro-app` lazy-loads the model.

**Smoke test from the box** — proves `kokoro-api` is reachable on `ai_shared` with the exact payload Open WebUI sends:

```bash
docker exec openwebui python3 -c "import urllib.request,json; r=urllib.request.urlopen(urllib.request.Request('http://kokoro-api:8000/v1/audio/speech', data=json.dumps({'model':'kokoro','input':'Hello from Kokoro','voice':'af_heart'}).encode(), headers={'Content-Type':'application/json','Authorization':'Bearer none'})); print(r.status, r.headers['Content-Type'], len(r.read()))"
# expect: 200 audio/wav <some tens of KB>
```

If Open WebUI shows "Server Connection Error" on play, `make logs openwebui` — a `502` from kokoro-api means `kokoro-app` is down or still downloading weights (`make logs kokoro`); a name-resolution error means `kokoro-api` isn't on `ai_shared` (`docker network inspect ai_shared`).

### Updating the image

The image tag is pinned in `ai/openwebui/docker-compose.openwebui.yml`:

```yaml
image: ghcr.io/open-webui/open-webui:v0.11.3
```

To update:

1. Edit that line to the desired tag (a specific release like `v0.11.3`, or `main` for the rolling upstream tag). Releases are at <https://github.com/open-webui/open-webui/releases>.
2. Pull the new image and recreate the container:
   ```bash
   make build openwebui   # runs `docker compose pull` under the hood
   make down openwebui && make up openwebui
   ```

`make build openwebui` re-pulls whatever tag is currently pinned — useful when the tag is `main` (rolling) or when a release is re-tagged. User data in the `openwebui_data` volume is preserved across updates.

**Upgrade notes:**

- **v0.11.1 → v0.11.3** (2026-09-04): fixes the frontend stall on reasoning models where the Thinking block stayed open and the reply froze until generation finished ([#29035](https://github.com/open-webui/open-webui/issues/29035), fixed in v0.11.2). v0.11.3 also makes a failed DB migration stop cleanly instead of starting half-updated — some upgrades from 0.11.0–0.11.2 hit this as a missing `chat.timer_at` column ([#29280](https://github.com/open-webui/open-webui/issues/29280)). Snapshot the `openwebui_data` volume before upgrading and check `docker logs openwebui` on first start.

### Notes

- The container does **not** request GPU access — it does no inference of its own, only proxies requests to LiteLLM.
- If LiteLLM is not running, Open WebUI loads but the model list is empty. Start LiteLLM first (`make up litellm`).
