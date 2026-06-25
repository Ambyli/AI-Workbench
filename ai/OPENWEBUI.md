# Open WebUI

Browser-based chat interface for the LiteLLM proxy. Open WebUI sees LiteLLM as a single OpenAI-compatible provider, so every model defined in `litellm_config.yaml` automatically appears in the model picker.

### Quick start

```bash
docker compose -f ai/docker-compose.openwebui.yml --env-file .env up -d
```

Or via make:

```bash
make up-openwebui
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
| `ENABLE_SIGNUP` | `OPENWEBUI_ENABLE_SIGNUP` | `true` | New accounts can be created; pair with `DEFAULT_USER_ROLE=pending` for gated access |
| `DEFAULT_USER_ROLE` | `OPENWEBUI_DEFAULT_USER_ROLE` | `pending` | New signups land in the admin approval queue. First-ever account is always admin regardless of this value |
| `ENABLE_OAUTH_SIGNUP` | `OPENWEBUI_ENABLE_OAUTH_SIGNUP` | `true` | Master switch for OAuth login flows |
| `OAUTH_MERGE_ACCOUNTS_BY_EMAIL` | `OPENWEBUI_OAUTH_MERGE_ACCOUNTS_BY_EMAIL` | `true` | OAuth logins are merged into existing local accounts with the same email |
| `GOOGLE_CLIENT_ID` | `OPENWEBUI_GOOGLE_CLIENT_ID` | _(empty)_ | Google Cloud OAuth 2.0 client ID — see [Google OAuth setup](#google-oauth-setup) |
| `GOOGLE_CLIENT_SECRET` | `OPENWEBUI_GOOGLE_CLIENT_SECRET` | _(empty)_ | Matching client secret |
| `OPENID_PROVIDER_URL` | `OPENWEBUI_OPENID_PROVIDER_URL` | Google discovery doc | OIDC discovery document URL; required for clean provider-side logout |
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

### Health check

Polls `http://localhost:8080/health` inside the container every 30s with a 30s startup grace period.

### Stopping

```bash
docker compose -f ai/docker-compose.openwebui.yml down
# or
make down-openwebui
```

User data, chat history, and uploaded files are stored in the named volume `openwebui_data` and survive restarts. To wipe everything (and force re-reading the env vars on next launch):

```bash
docker compose -f ai/docker-compose.openwebui.yml down -v
```

### User signup & approval

The deployment is configured so anyone with the URL can register, but new accounts cannot chat until an admin approves them. Workflow:

1. A new user visits `http://localhost:8007` and clicks **Sign up** (or uses Google — see below).
2. The account is created with role `pending`. The user sees a "waiting for admin approval" screen.
3. An admin opens **Admin Panel → Users**, finds the pending row, and changes their role to **User**.
4. The user refreshes; they can now select a model and chat.

To revoke access, set the user's role back to `pending` (silent suspension) or delete the account.

> Want it fully open? Set `OPENWEBUI_DEFAULT_USER_ROLE=user` in `.env`. Want it fully locked? Set `OPENWEBUI_ENABLE_SIGNUP=false`. Either change requires either an Admin Settings toggle on the running container or a volume wipe (see the warning above the env table).

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
   make down-openwebui && make up-openwebui
   ```

A **Continue with Google** button appears on the login screen once both values are populated and `OPENWEBUI_ENABLE_OAUTH_SIGNUP=true`.

**First Google login behavior:**

- If a local account with the same email already exists, `OAUTH_MERGE_ACCOUNTS_BY_EMAIL=true` links them — same user, two sign-in methods.
- If not, a new account is created with role `pending` (per `OPENWEBUI_DEFAULT_USER_ROLE`) and must be approved.

> **About the running container:** Env vars are only read on the *very first* boot — the SQLite store in `openwebui_data` is authoritative afterwards. If you change OAuth settings after the container has been initialized, either toggle the equivalent setting in **Admin Panel → Settings → General** or wipe the volume with `docker compose -f ai/docker-compose.openwebui.yml down -v` and start fresh (this deletes all chat history and users).

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
   make up-cloudflared
   ```
   Stop with `make down-cloudflared`. Logs: `make logs-cloudflared`.
7. Update `OPENWEBUI_WEBUI_URL=https://chat.zeoenergy.com` and `CORS_ALLOW_ORIGIN=https://chat.zeoenergy.com` in `.env` (already set), then restart Open WebUI:
   ```bash
   make down-openwebui && make up-openwebui
   ```
8. Update the Google OAuth client's **Authorized redirect URIs** to include `https://chat.zeoenergy.com/oauth/google/callback`.

**Verifying:**

```bash
docker logs ai-cloudflared --tail 50   # should show "Registered tunnel connection"
curl -I https://chat.zeoenergy.com     # should return 200 from Open WebUI
```

If the tunnel is up but the site 502s, the origin URL (`openwebui:8080`) is unreachable from the `cloudflared` container — confirm Open WebUI is running (`make up-openwebui`) and on the same `ai_shared` network.

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

Restart the container — `make down-openwebui && make up-openwebui`. After login, only the models on the virtual key's allowlist appear in the chat picker. Adding or removing models later only needs the virtual-key allowlist to be edited; no Open WebUI restart is required (Open WebUI re-fetches `/v1/models` on every page load).

### Adding more LiteLLM models

Models are configured in `litellm_config.yaml`, not in Open WebUI. After editing that file and restarting LiteLLM, the new model appears in Open WebUI's model picker automatically — Open WebUI calls `GET /v1/models` against LiteLLM to populate the list.

### Notes

- The image (`ghcr.io/open-webui/open-webui:main`) tracks the rolling `main` tag. Pin to a specific version (e.g. `:v0.4.8`) before relying on this in production.
- The container does **not** request GPU access — it does no inference of its own, only proxies requests to LiteLLM.
- If LiteLLM is not running, Open WebUI loads but the model list is empty. Start LiteLLM first (`make up-litellm`).
