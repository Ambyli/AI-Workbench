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
| `OAUTH_AUTO_REDIRECT` | `OPENWEBUI_OAUTH_AUTO_REDIRECT` | `true` | *Fallback path only* (trusted headers blank): skip the login page and redirect straight to Google |
| `ENABLE_LOGIN_FORM` | `OPENWEBUI_ENABLE_LOGIN_FORM` | `false` | Hides the email/password form. **Required** for `OAUTH_AUTO_REDIRECT` to do anything |
| `OAUTH_UPDATE_NAME_ON_LOGIN` | `OPENWEBUI_OAUTH_UPDATE_NAME_ON_LOGIN` | `true` | *Fallback path only*: re-read the `name` claim on every OAuth login (upstream default: `false`) |
| `OAUTH_UPDATE_PICTURE_ON_LOGIN` | `OPENWEBUI_OAUTH_UPDATE_PICTURE_ON_LOGIN` | `true` | *Fallback path only*: re-read the `picture` claim on every OAuth login (upstream default: `false`) |
| `WEBUI_AUTH_TRUSTED_EMAIL_HEADER` | `OPENWEBUI_WEBUI_AUTH_TRUSTED_EMAIL_HEADER` | `X-Forwarded-Email` | Trusted-header SSO — sign in as the identity oauth2-proxy verified. See [Single sign-on](#single-sign-on) |
| `WEBUI_AUTH_TRUSTED_NAME_HEADER` | `OPENWEBUI_WEBUI_AUTH_TRUSTED_NAME_HEADER` | _(blank — deliberate)_ | The only candidate header carries Google's numeric `sub`. Name comes from Google via the patch instead |
| `WEBUI_AUTH_TRUSTED_ACCESS_TOKEN_HEADER` | `OPENWEBUI_WEBUI_AUTH_TRUSTED_ACCESS_TOKEN_HEADER` | `X-Forwarded-Access-Token` | **Zeo patch, not upstream.** Verify the trusted identity with Google and take name + picture from it — see [Single sign-on](#single-sign-on) |
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

### Encryption keys

`WEBUI_SECRET_KEY` does double duty upstream: it signs login sessions **and**, by default, is the key for two things encrypted at rest in `openwebui_data`:

- **`OAUTH_CLIENT_INFO_ENCRYPTION_KEY`** — the dynamically registered OAuth *client* for each MCP tool server (the blob stored in `tool_server.connections`).
- **`OAUTH_SESSION_TOKEN_ENCRYPTION_KEY`** — each user's MCP access / refresh *tokens*.

Both default to `WEBUI_SECRET_KEY`. That coupling is a trap: rotating the session secret is a routine, low-stakes act (it just logs everyone out), but while the keys are coupled it *also* makes every stored MCP client blob and token undecryptable. Open WebUI doesn't surface this well — the tool still shows "connected" (that check just hits the MCP URL), but enabling it redirects to `GET /oauth/clients/<id>/authorize`, which **404s** because the backend caught an `InvalidToken` while loading the client and skipped it. The log line is:

```
Failed to lazily add OAuth client mcp:<id> from config: InvalidToken. Stored OAuth client data is invalid; reconnect this tool server.
```

Note that clicking **Save** on the tool server does **not** fix it. Save re-encrypts the connection from the blob it already has (`resolve_oauth_client_info` → `decrypt_data`), so a dead blob stays dead. Only a real re-registration writes a new one.

This deployment therefore sets both keys **independently** of the session secret (`OPENWEBUI_OAUTH_CLIENT_INFO_ENCRYPTION_KEY`, `OPENWEBUI_OAUTH_SESSION_TOKEN_ENCRYPTION_KEY` in `.env`). Generate each with `openssl rand -hex 32`, set once, and keep stable. The compose file falls back to `WEBUI_SECRET_KEY` when a key is unset, so a half-applied `.env` on the box behaves exactly like stock rather than breaking.

**Recovering after a key change** (or a past secret rotation that used the default):

1. Set both keys in `.env`, then `make up openwebui` (recreate, so the container reads them).
2. Re-register each MCP tool's OAuth client — a plain Save is not enough:
   - Admin Panel → Settings → External Tools → the MCP server → in its OAuth section use **Register / Reconnect** (calls `POST /oauth/clients/register`, which runs dynamic registration and writes a fresh blob), then Save.
   - If there is no such control in your build, delete the tool server connection and add it back. With no stored blob, dynamic registration runs automatically on first use.
3. Each user clicks **Authorize** once more the next time they enable the tool — their old tokens were encrypted under the old key and cannot be carried over.

Confirm the client loads afterwards:

```bash
docker logs openwebui 2>&1 | grep -i "Failed to lazily add OAuth client"   # should stop appearing
```

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

There is a second reason the mounts are single-file **and** read-only: `config.py` wipes and rebuilds `STATIC_DIR` on **every boot** — it unlinks each file, re-copies `/app/build/static/**` over the top, then copies `favicon.png` and `splash.png` a second time (`config.py:99-135`). A writable copy of the branded files would be overwritten seconds after start. With `:ro` bind mounts the unlink fails (a mount point can't be unlinked) and the copy fails with `[Errno 30] Read-only file system`, so the branded bytes survive. Expect roughly ten `An error occurred: [Errno 30] Read-only file system` lines in `docker logs openwebui` at startup — that is the mechanism working, not a fault.

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

Then `make up openwebui` to recreate — and then purge Cloudflare, or nothing visible changes.

#### Cloudflare caches `/static/*`

`chat.zeoenergy.com` is fronted by Cloudflare, and Cloudflare caches responses by file extension (`.png`, `.ico`, `.svg`, `.css`, `.js`, fonts) **regardless of cookies** unless the origin sends `Cache-Control: private` or `no-store`. Starlette's `StaticFiles` sends neither and oauth2-proxy adds nothing, so every `/static/*` asset sits at the edge for Cloudflare's default 4 h TTL (`Cache-Control: max-age=14400`, `cf-cache-status: HIT`). Recreating the container does nothing to that copy, and neither does a private window. When this branding first shipped, the files were on disk and byte-correct for hours while the edge kept serving the upstream defaults — and a cached **zero-byte** `custom.css`, which is why the in-app mark swap looked broken too.

Two consequences beyond stale branding: the edge serves those cached responses to **anonymous** clients (oauth2-proxy never sees the request), and browsers that loaded the old assets keep them for their own 4 h `max-age` even after a purge.

The permanent fix is a dashboard Cache Rule that bypasses the cache for the hostname — [ai/cloudflared/CLOUDFLARED.md § Cache rule](../cloudflared/CLOUDFLARED.md#cache-rule--bypass-for-chatzeoenergycom). Until that exists, purge after every asset change: Cloudflare dashboard → Caching → Configuration → Purge Everything.

To tell the layers apart:

```bash
# What the edge serves (run from anywhere). HIT + 21666 bytes is the stale upstream favicon.
curl -sI https://chat.zeoenergy.com/static/favicon.png | grep -iE 'cf-cache-status|content-length|^age:'

# What the origin serves (run on the box). Expect the sha256 of assets/openwebui/favicon.png.
docker exec openwebui python3 -c "import urllib.request,hashlib;print(hashlib.sha256(urllib.request.urlopen('http://localhost:8080/static/favicon.png').read()).hexdigest())"
```

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

One gate. **oauth2-proxy** at the edge does the Google sign-in and enforces the Workspace-domain and group checks. Open WebUI then accepts that identity through its trusted-header mode — with a patch that makes it **verify** the identity with Google rather than believe the headers, and that takes the display name and profile picture from Google while it is there. Users sign in once and land in Open WebUI with the right name and avatar: no second Google round-trip, no `/auth` page.

#### How it works

| Piece | Setting | What it does |
|---|---|---|
| oauth2-proxy | `OAUTH2_PROXY_PASS_USER_HEADERS=true` | Sends `X-Forwarded-Email` (and `X-Forwarded-User`, Google's numeric `sub`) on every proxied request |
| oauth2-proxy | `OAUTH2_PROXY_PASS_ACCESS_TOKEN=true` | Sends the user's live Google access token as `X-Forwarded-Access-Token` |
| Open WebUI | `WEBUI_AUTH_TRUSTED_EMAIL_HEADER=X-Forwarded-Email` | Upstream trusted-header mode: the frontend auto-signs-in, the backend uses this header as the identity |
| Open WebUI | `WEBUI_AUTH_TRUSTED_NAME_HEADER=` (blank) | Deliberately unset — `X-Forwarded-User` is a 21-digit number, not a name |
| Open WebUI | `WEBUI_AUTH_TRUSTED_ACCESS_TOKEN_HEADER=X-Forwarded-Access-Token` | **Zeo patch.** Verifies the token with Google and takes name + picture from it |

A trusted-header sign-in happens whenever Open WebUI has no session of its own for the browser — first visit, after its JWT expires, after logout. Each time, the patched `routers/auths.py` does:

1. `GET https://oauth2.googleapis.com/tokeninfo?access_token=…` — the token must be live, its `aud` must equal `GOOGLE_CLIENT_ID` (the one OAuth client both oauth2-proxy and Open WebUI use), its `email` must equal `X-Forwarded-Email`, and `email_verified` must be true. Any failure → HTTP 401, no account created, and a `Trusted-header sign-in refused: <reason>` warning in `docker logs openwebui`.
2. `GET https://openidconnect.googleapis.com/v1/userinfo` with the token — `name` and `picture`. If this call fails the sign-in still succeeds (identity was already verified), with the e-mail as the display name.
3. Create the account on first sight with that name and picture (fetched and stored base64 through upstream's own OAuth picture code, MIME allow-list included), or on later sign-ins update the stored name and picture when they differ. Accounts created under the old numeric-name behaviour heal on their next sign-in — no admin cleanup.

Code: `backend/open_webui/utils/trusted_proxy.py` (new) plus one hunk in `routers/auths.py` and one env var in `env.py` — `ai/openwebui/patches/0002-trusted-header-google-identity.patch`, applied by the [custom image](#custom-image-patches).

#### Why a patch is needed at all

Upstream's trusted-header mode has no source for a name or a picture. oauth2-proxy's Google provider can only forward `email`, `user` (Google's `sub`), `groups`, `preferred_username` (unset for Google), and the raw tokens — that is the whole list in `pkg/apis/sessions/session_state.go::GetClaim`. The first attempt at one-gate SSO here used `WEBUI_AUTH_TRUSTED_NAME_HEADER=X-Forwarded-User` and nothing for pictures:

```go
// oauth2-proxy providers/google.go
Email: c.Email,
User:  c.Subject,     // e.g. "117402938475019283746"
```
```python
# Open WebUI routers/auths.py (upstream)
name = request.headers.get(WEBUI_AUTH_TRUSTED_NAME_HEADER, email)
...
async def signup_handler(..., profile_image_url: str = '/user.png', ...)
```

So every account was created as a 21-digit number with the default avatar, and because upstream's signup path only runs once per e-mail, they never corrected themselves. The interim fix was to let Open WebUI run its own Google OIDC as a second hop — correct names and pictures, at the cost of a second Google round-trip and a visible bounce through `/auth`, plus Google's account chooser for anyone with a personal account also signed in. The access token is the one thing oauth2-proxy *can* forward that lets Open WebUI ask Google for the rest — and verify the asserted identity while doing so.

#### Security

This is **stronger** than upstream's trusted-header mode, not weaker. Upstream believes any `X-Forwarded-Email` it sees, which is why every container on `ai_shared` is inside the trust boundary and why `PORT_OPENWEBUI` must stay on `127.0.0.1`. With the patch, a sign-in also needs a live Google access token *for that same account, issued to our OAuth client* — something an attacker on the network does not have. The loopback bind stays regardless; it costs nothing and keeps stock behaviour safe if the patch is ever disabled.

- The Google access token transits `oauth2-proxy → openwebui` inside Docker only. Its scopes are `profile email`; it cannot read mail, Drive, or anything else.
- `utils/auth.py::get_current_user` still enforces upstream's rule that the session's user must match `X-Forwarded-Email` on every request, so an Open WebUI session cannot outlive a change of identity at the proxy.
- Password changes are disabled in trusted-header mode (upstream behaviour); accounts hold a random password nobody knows. Intended.
- If `GOOGLE_CLIENT_ID` were empty the `aud` check would be skipped. It is set here — don't blank it.
- **Never set `WEBUI_AUTH_TRUSTED_EMAIL_HEADER` without `WEBUI_AUTH_TRUSTED_ACCESS_TOKEN_HEADER`.** That is stock upstream behaviour: numeric names, no pictures, and header spoofing from anything on the network.

#### Verifying

```bash
# Patched code is in the running container; headers are wired.
docker exec openwebui grep -c WEBUI_AUTH_TRUSTED_ACCESS_TOKEN_HEADER /app/backend/open_webui/routers/auths.py   # expect 2
docker exec openwebui test -f /app/backend/open_webui/utils/trusted_proxy.py && echo patched
docker exec openwebui printenv WEBUI_AUTH_TRUSTED_EMAIL_HEADER WEBUI_AUTH_TRUSTED_ACCESS_TOKEN_HEADER
# A healthy sign-in logs nothing here; a refused one says why.
docker logs openwebui 2>&1 | grep -i "Trusted-header sign-in refused"
```

Then sign in from a private window: one Google prompt, straight into the app, and Admin Panel → Users shows the real name and avatar.

#### Fallback: two-hop OAuth

Blank `OPENWEBUI_WEBUI_AUTH_TRUSTED_EMAIL_HEADER` **and** `OPENWEBUI_WEBUI_AUTH_TRUSTED_ACCESS_TOKEN_HEADER`, then `make up openwebui`. Open WebUI reverts to its own Google OIDC after the oauth2-proxy gate: `OAUTH_AUTO_REDIRECT=true` + `ENABLE_LOGIN_FORM=false` send users straight to Google, and `OAUTH_UPDATE_NAME_ON_LOGIN` / `OAUTH_UPDATE_PICTURE_ON_LOGIN` keep names and pictures correct on that path. Those four settings are kept configured for exactly this reason. Expect a visible bounce through `/auth?redirect=%2F`, and Google's account chooser on the second hop for browsers signed into several Google accounts.

`OAUTH_AUTO_REDIRECT` alone does nothing — `src/routes/auth/+page.svelte` requires **all** of:

```js
$config?.oauth?.auto_redirect && !logout && !form && !error
  && providers.length === 1                        // only Google is configured
  && $config?.features?.auth !== false
  && $config?.features?.enable_login_form === false  // ← the easy one to miss
  && !$config?.features?.enable_ldap
  && !$config?.features?.auth_trusted_header         // ← auto-redirect and trusted-header
  && !$config?.onboarding                            //   SSO are mutually exclusive
  && !localStorage.token && !document.cookie…token=
```

If the fallback lands on Open WebUI's login page instead of bouncing to Google, dump the guard's inputs from inside the container — no oauth2-proxy or Cloudflare in the way:

```bash
docker exec openwebui python3 -c "import urllib.request,json;c=json.load(urllib.request.urlopen('http://localhost:8080/api/config'));o=c.get('oauth',{});f=c.get('features',{});print(json.dumps({'auto_redirect':o.get('auto_redirect'),'providers':list(o.get('providers',{})),'enable_login_form':f.get('enable_login_form'),'auth_trusted_header':f.get('auth_trusted_header'),'enable_ldap':f.get('enable_ldap'),'auth':f.get('auth'),'onboarding':c.get('onboarding')},indent=1))"
```

Expected in fallback mode: `auto_redirect: true`, `providers: ["google"]`, `enable_login_form: false`, `auth_trusted_header: false`, `enable_ldap: false`, `auth: true`, `onboarding: null`. (In normal one-gate mode `auth_trusted_header` is `true` and the frontend signs in immediately without consulting the rest.)

#### Break-glass if Google itself is unreachable

Neither mode can sign anyone in without Google. For a local admin login: set `OPENWEBUI_ENABLE_LOGIN_FORM=true`, blank both trusted headers, `make up openwebui`, and use an admin whose password you set in advance through Admin Panel → Users — OAuth- and trusted-header-created accounts have a random `uuid4()` password. Set that password *before* you need it.

> **Config lifetimes — don't assume.** The `OAUTH_*` settings and the `WEBUI_AUTH_TRUSTED_*` headers are read from the environment on every boot (`ENABLE_OAUTH_PERSISTENT_CONFIG` defaults to `false` upstream, and the trusted headers are plain `env.py` constants). `ENABLE_LOGIN_FORM` is a `ui.*` key, so a DB row *can* shadow it — `Config.get` returns the env-derived default only while no row exists. v0.11.3 itself has no Admin Panel toggle for it, but **this install had a row anyway**: the 0.11 reshape migration (`migrations/versions/3ff2c63645b8_reshape_config_to_per_key_rows.py`) flattens the pre-0.11 single-JSON config blob into per-key rows and keeps every key it finds, and older versions dumped the whole config on any admin save. Expect the same for the other `ui.*` keys (`ui.enable_signup`, `ui.default_user_role`, …) — `select key from config order by key` lists exactly which env vars are inert on this install. If `/api/config` reports `enable_login_form: true` while `printenv ENABLE_LOGIN_FORM` says `false`, drop the row:
>
> ```bash
> docker exec openwebui python3 -c "import sqlite3;c=sqlite3.connect('/app/backend/data/webui.db');print(c.execute(\"select key,value from config where key='ui.enable_login_form'\").fetchall())"
> # non-empty → delete it, then `make up openwebui`
> docker exec openwebui python3 -c "import sqlite3;c=sqlite3.connect('/app/backend/data/webui.db');c.execute(\"delete from config where key='ui.enable_login_form'\");c.commit()"
> ```

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

### Custom image (patches)

`openwebui` does not run the upstream image directly. [`ai/openwebui/Dockerfile.openwebui`](Dockerfile.openwebui) starts `FROM ghcr.io/open-webui/open-webui:${OPENWEBUI_VERSION}` and applies every [`ai/openwebui/patches/*.patch`](patches/) with `patch -p1` from `/app`, dry-running each first so a hunk that no longer matches **fails the build** instead of shipping unpatched. It then re-parses every patched module and greps for the feature, so a patch that applied to the wrong context also fails. Nothing else changes — same entrypoint, layout, and user. The result is tagged locally as `openwebui-zeo:<version>` with `pull_policy: build`, so compose never tries to pull it.

#### Current patches

| Patch | What | Files |
|---|---|---|
| `0002-trusted-header-google-identity.patch` | Adds `WEBUI_AUTH_TRUSTED_ACCESS_TOKEN_HEADER`. In trusted-header SSO mode, verify the asserted identity with Google using the access token oauth2-proxy forwards, and take display name + picture from Google's userinfo on every sign-in. Rationale and security in [Single sign-on](#single-sign-on) | `backend/open_webui/env.py`, `routers/auths.py`, `utils/trusted_proxy.py` (new) |

(`0001` was a `login_hint` patch for the two-hop OAuth path; it was retired when one-gate SSO replaced that path.)

#### Verifying a build

```bash
docker image inspect openwebui-zeo:v0.11.3 --format '{{index .Config.Labels "com.zeoenergy.openwebui.patches"}}'
docker exec openwebui test -f /app/backend/open_webui/utils/trusted_proxy.py && echo patched
```

#### Regenerating a patch for a new upstream version

Only needed when `make build openwebui` fails at the `--dry-run` step.

```bash
V=v0.12.0   # the tag you are moving to
curl -sL https://github.com/open-webui/open-webui/archive/refs/tags/$V.tar.gz | tar xz
cp -r open-webui-${V#v} pristine && cp -r open-webui-${V#v} patched
patch -p1 -d patched < ai/openwebui/patches/0002-trusted-header-google-identity.patch  # fix rejects by hand in patched/
{ diff -u pristine/backend/open_webui/env.py           patched/backend/open_webui/env.py
  diff -u pristine/backend/open_webui/routers/auths.py patched/backend/open_webui/routers/auths.py
  diff -u /dev/null                                    patched/backend/open_webui/utils/trusted_proxy.py
} | sed -e 's|^--- pristine/|--- a/|' -e 's|^+++ patched/|+++ b/|' > new.patch
```

Paste the header comment from the old patch file back on top, update its `Target:` line, replace the file, and re-run `make build openwebui`. Read what upstream changed around the rejected hunks before trusting the result — `routers/auths.py` moves between releases.

#### Adding another patch

Number it `0003-….patch`, keep the `a/` / `b/` path prefixes, list it in the Dockerfile header comment and the `com.zeoenergy.openwebui.patches` label, add its modules to the `ast.parse` list and a `grep -q` guard for something only the patched code contains. Patches apply in filename order.

### Updating the image

The container runs a locally built image — upstream's pinned release plus the patches above. The upstream tag is `OPENWEBUI_VERSION` in `.env`; the compose file reads it as a build arg and as part of the local image name (`openwebui-zeo:<tag>`).

To update:

1. Set `OPENWEBUI_VERSION` in `.env` to the desired release tag (releases: <https://github.com/open-webui/open-webui/releases>). Pin a release — `main` under a patch set means every rebuild has a different base.
2. Rebuild and recreate:
   ```bash
   make build openwebui   # pulls the new upstream tag, applies the patches
   make up openwebui      # recreates the container on the new image
   ```
3. If the build stops at `patch --dry-run`, upstream changed code a patch touches. Regenerate the patch (above) and read what changed around it — do not force it.

User data in the `openwebui_data` volume is preserved across updates.

**Upgrade notes:**

- **v0.11.1 → v0.11.3** (2026-09-04): fixes the frontend stall on reasoning models where the Thinking block stayed open and the reply froze until generation finished ([#29035](https://github.com/open-webui/open-webui/issues/29035), fixed in v0.11.2). v0.11.3 also makes a failed DB migration stop cleanly instead of starting half-updated — some upgrades from 0.11.0–0.11.2 hit this as a missing `chat.timer_at` column ([#29280](https://github.com/open-webui/open-webui/issues/29280)). Snapshot the `openwebui_data` volume before upgrading and check `docker logs openwebui` on first start.

### Notes

- The container does **not** request GPU access — it does no inference of its own, only proxies requests to LiteLLM.
- If LiteLLM is not running, Open WebUI loads but the model list is empty. Start LiteLLM first (`make up litellm`).
