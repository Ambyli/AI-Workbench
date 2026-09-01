# n8n

Self-hosted [n8n](https://n8n.io) workflow engine — used here to prototype automations that stitch together LiteLLM (chat / tool models), the Roofix bridge, MCP servers, and any external HTTP / webhook surface without writing a bespoke service for each one.

Compose file: [`docker-compose.n8n.yml`](docker-compose.n8n.yml). Operator config lives in the root `.env` (`N8N_*`, `OAUTH2_PROXY_N8N_*`, `PORT_N8N_DB`); see the config-var table in the root `CLAUDE.md`.

## What runs

| Container | Purpose | Compose file | Network | Host port |
|---|---|---|---|---|
| `n8n` | Editor + workflow runtime (`n8nio/n8n`, port 5678 in-container) | this dir | `ai_shared` | _(none — reached via oauth2-proxy-n8n)_ |
| `n8n-db` | Postgres 16 backing workflows, credentials, execution history | this dir | `ai_shared` | `PORT_N8N_DB` (default `5435`) |
| `oauth2-proxy-n8n` | Dedicated Google-SSO gate on the `n8n.zeoenergy.com` subdomain | [`ai/oauth2-proxy/`](../oauth2-proxy/docker-compose.oauth2-proxy.yml) | `ai_shared` | _(none — cloudflared reaches it via container DNS)_ |

Single-process n8n runtime — no queue mode / Redis broker. If a workflow queue backlog becomes real, switch `EXECUTIONS_MODE=queue` and add a Redis service; not needed for prototyping.

**Stack ownership** — `make up n8n` brings up `n8n-db` + `n8n`; `make up oauth2-proxy` brings up the SSO gates (`oauth2-proxy` + `oauth2-assets` + `oauth2-proxy-n8n`). Bring up both stacks for a fresh install; either alone if you're iterating.

## Public routing (dedicated oauth2-proxy on subdomain)

n8n lives on its own subdomain — **`n8n.zeoenergy.com`** — with a dedicated `oauth2-proxy-n8n` instance in front of it. This is deliberate: n8n's built-in subpath routing (`N8N_PATH=/n8n/`) is **broken upstream** ([n8n-io/n8n#19635](https://github.com/n8n-io/n8n/issues/19635), [#18596](https://github.com/n8n-io/n8n/issues/18596)) as of 1.108.2 — static assets hit the SPA fallback and return HTML instead of JS/CSS. Subdomain deployment sidesteps the bug entirely.

Traffic path:

```
Browser ─▶ cloudflared ─▶ oauth2-proxy-n8n:4180 ─▶ n8n:5678
                (n8n.zeoenergy.com)
```

The `oauth2-proxy-n8n` instance is defined in [`ai/oauth2-proxy/docker-compose.oauth2-proxy.yml`](../oauth2-proxy/docker-compose.oauth2-proxy.yml) alongside the existing openwebui-side oauth2-proxy — all SSO gates live in one file. The two instances share:

- **The Google OAuth client** with the openwebui oauth2-proxy — one client, two registered redirect URIs (`https://chat.zeoenergy.com/oauth2/callback` + `https://n8n.zeoenergy.com/oauth2/callback`).
- **The access-gate group** (`zeoai.access@zeoenergy.com`) — same members reach both apps.
- **The service account JSON + branded templates** — bind-mounted read-only from `ai/oauth2-proxy/sa-key.json` and `ai/oauth2-proxy/templates/`. Edit those files in one place.

What differs per instance:

| | Shared oauth2-proxy (openwebui) | oauth2-proxy-n8n |
|---|---|---|
| Container | `oauth2-proxy` | `oauth2-proxy-n8n` |
| Public host | `chat.zeoenergy.com` | `n8n.zeoenergy.com` |
| Cookie name | `_oauth2_proxy` | `_oauth2_proxy_n8n` |
| Cookie secret | `OAUTH2_PROXY_COOKIE_SECRET` | `OAUTH2_PROXY_N8N_COOKIE_SECRET` |
| Cookie SameSite | `none` (sandbox iframes) | `lax` (n8n isn't iframed) |
| Upstreams | 3 (openwebui + assets + sandbox-proxy) | 1 (n8n) |

Users signing in are gated at oauth2-proxy-n8n; sessions are independent from openwebui (separate cookies) but the login flow is silent if you already have a Google Workspace session.

## First-run checklist

Do these **before** `make up n8n`:

1. **Generate the n8n encryption key.** Encrypts every stored credential at rest in Postgres. Losing it destroys all stored credentials — workflow rows survive, auth blobs become unreadable.

   ```bash
   openssl rand -hex 32
   ```

   Paste into `N8N_ENCRYPTION_KEY` in `.env` and back it up alongside your other secrets.

2. **Generate the oauth2-proxy-n8n cookie secret.** MUST be distinct from `OAUTH2_PROXY_COOKIE_SECRET`.

   ```bash
   openssl rand -base64 32 | tr -- '+/' '-_'
   ```

   Paste into `OAUTH2_PROXY_N8N_COOKIE_SECRET` in `.env`.

3. **Register the Google OAuth redirect URI.** Google Cloud Console → APIs & Services → Credentials → open the OAuth 2.0 client currently in use by openwebui (its client ID is `OPENWEBUI_GOOGLE_CLIENT_ID` in `.env`) → **Authorized redirect URIs** → add:

   ```
   https://n8n.zeoenergy.com/oauth2/callback
   ```

   Save. The openwebui redirect URI stays; you're only appending.

4. **Add the Cloudflare tunnel public hostname.** Cloudflare Zero Trust → Networks → Tunnels → open the tunnel currently serving `chat.zeoenergy.com` → **Public Hostname** → Add:

   | Field | Value |
   |---|---|
   | Subdomain | `n8n` |
   | Domain | `zeoenergy.com` |
   | Service type | `HTTP` |
   | URL | `oauth2-proxy-n8n:4180` |

   No TLS overrides needed — cloudflared terminates TLS on its side.

5. **Bring up the stacks** (from the repo root):

   ```bash
   make up n8n            # n8n + n8n-db
   make up oauth2-proxy   # rebuild picks up oauth2-proxy-n8n
   docker logs -f n8n
   ```

   Wait for `Editor is now accessible via: https://n8n.zeoenergy.com/`. If the shared oauth2-proxy is already running, `make up oauth2-proxy` is idempotent — it only starts new containers (i.e., `oauth2-proxy-n8n`).

6. **Sign in.** Open <https://n8n.zeoenergy.com/> in a browser. oauth2-proxy-n8n takes you through Google SSO if you don't already have a session cookie; then n8n's own owner-setup page asks you to create a local owner account.

## AI-node wiring

The compose file preconfigures two env vars on the `n8n` container:

| Env var (in-container) | Value | Purpose |
|---|---|---|
| `N8N_AI_OPENAI_API_BASE` | `http://litellm:4000/v1` | Base URL n8n's OpenAI + LangChain nodes default to |
| `N8N_AI_OPENAI_API_KEY` | `${DEFAULT_LITELLM_MASTER_KEY}` | Auth token for the same |

Effect: when an operator drops an OpenAI Chat Model / LangChain OpenAI Chat node into a workflow, the credential picker's defaults already point at LiteLLM — no need to paste the base URL or key per workflow. Model names come from LiteLLM's config (e.g. `qwen3.6-unsloth`, `claude-sonnet-5`).

Only override at the credential level when targeting an external provider (real OpenAI, Anthropic direct, etc.).

## Connecting to the n8n DB

`n8n-db` publishes on the host at `PORT_N8N_DB` (default `5435`) — reachable from any tool that speaks Postgres:

```bash
psql -h localhost -p 5435 -U n8n -d n8n
```

Credentials come from `N8N_DB_USER` / `N8N_DB_PASSWORD` / `N8N_DB_NAME` in `.env` (defaults `n8n` / `n8n` / `n8n`). **Change the password before exposing the port past the host** — the default is baked in for dev convenience only.

Tables of interest: `workflow_entity` (workflow definitions), `credentials_entity` (encrypted credential blobs), `execution_entity` (run history). Workflow rows are JSONB and readable directly; credential rows are encrypted with `N8N_ENCRYPTION_KEY` and only n8n itself can decrypt them.

## Common gotchas

| Symptom | Cause / fix |
|---|---|
| Editor logs `http://localhost:5678` instead of `https://n8n.zeoenergy.com/` | `N8N_HOST` / `N8N_PROTOCOL` didn't propagate. Check `.env` and `make clean n8n && make up n8n`. |
| Login loop on n8n.zeoenergy.com | Two likely causes: (a) redirect URI not registered on the Google OAuth client — see step 3 above; (b) `OAUTH2_PROXY_N8N_COOKIE_DOMAINS` doesn't match the URL host. |
| `403` from oauth2-proxy-n8n after Google sign-in | The signed-in user isn't in `OAUTH2_PROXY_GOOGLE_GROUPS` (`zeoai.access@zeoenergy.com`). Add them via Google Admin. |
| Cloudflare edge 502 at n8n.zeoenergy.com | Public hostname isn't pointing at `oauth2-proxy-n8n:4180` — see step 4 above. Also check `docker ps --filter name=oauth2-proxy-n8n` is running. |
| Assets return MIME 'text/html' | You're on the old subpath config. Confirm `N8N_PATH=/` and `N8N_HOST=n8n.zeoenergy.com` in `.env`, then `make clean n8n && make up n8n`. |
| Credentials all show as `unset` after a restart | `N8N_ENCRYPTION_KEY` changed between runs. Restore the original key from your secret store, or accept the loss and re-enter credentials. |
| Container refuses to start with `variable N8N_ENCRYPTION_KEY must be set` | The compose file uses `${N8N_ENCRYPTION_KEY:?...}` so it fails fast when unset — generate one with `openssl rand -hex 32` and paste it into `.env`. |

## Rollback

The wiring is additive across two compose files, `.env`, and Cloudflare. To back out:

1. Remove the `n8n.zeoenergy.com` public hostname in the Cloudflare tunnel dashboard.
2. Remove `https://n8n.zeoenergy.com/oauth2/callback` from the Google OAuth client's authorized redirect URIs.
3. Stop the n8n stack: `make clean n8n` (leave the DB and data volumes if you want to preserve workflows; add `CONFIRM=yes make very-clean n8n` to nuke them).
4. Delete the `oauth2-proxy-n8n` service entry from `ai/oauth2-proxy/docker-compose.oauth2-proxy.yml` (and the `OAUTH2_PROXY_N8N_*` block from `.env`), then `make up oauth2-proxy` to reconcile — the openwebui-side oauth2-proxy and oauth2-assets stay running.

Steps 1–3 alone are enough to stop serving n8n; step 4 is the clean removal.
