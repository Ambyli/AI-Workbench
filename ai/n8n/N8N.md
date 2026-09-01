# n8n

Self-hosted [n8n](https://n8n.io) workflow engine — used here to prototype automations that stitch together LiteLLM (chat / tool models), the Roofix bridge, MCP servers, and any external HTTP / webhook surface without writing a bespoke service for each one.

Compose file: [`docker-compose.n8n.yml`](docker-compose.n8n.yml). Operator config lives in the root `.env` (`N8N_*`, `PORT_N8N_DB`); see the config-var table in the root `CLAUDE.md`.

## What runs

| Container | Purpose | Network | Host port |
|---|---|---|---|
| `n8n` | Editor + workflow runtime (`n8nio/n8n`, port 5678 in-container) | `ai_shared` | _(none — reached via oauth2-proxy)_ |
| `n8n-db` | Postgres 16 backing workflows, credentials, execution history | `ai_shared` | `PORT_N8N_DB` (default `5435`) |

Single-process runtime — no queue mode / Redis broker. If a workflow queue backlog becomes real, switch `EXECUTIONS_MODE=queue` and add a Redis service; not needed for prototyping.

## Public routing (same-origin under oauth2-proxy)

n8n rides on the existing `chat.zeoenergy.com` hostname at the `/n8n/` subpath. No new Cloudflare tunnel hostname, no new oauth2-proxy instance, no new Google OAuth client.

The traffic path mirrors what `sandbox-proxy` already does under `/sandboxes/`:

```
Browser ─▶ cloudflared ─▶ oauth2-proxy ──▶ n8n:5678   (path prefix /n8n/*)
                                     └──▶ openwebui:8080  (everything else)
                                     └──▶ sandbox-proxy:80/sandboxes/  (path prefix /sandboxes/*)
                                     └──▶ oauth2-assets:80/assets/    (skip-auth)
```

Wiring lives in `.env`:

- `OAUTH2_PROXY_UPSTREAMS` includes `http://n8n:5678/n8n/` — oauth2-proxy's longest-prefix match sends `/n8n/*` requests to n8n with the path preserved.
- On the n8n side, `N8N_PATH=/n8n/`, `N8N_EDITOR_BASE_URL=https://chat.zeoenergy.com/n8n/`, and `WEBHOOK_URL=https://chat.zeoenergy.com/n8n/` tell it it's mounted at that prefix so the editor's HTML and webhook payloads use the correct URLs.

Access gate reuses the existing `zeoai.access@zeoenergy.com` Google-group check on oauth2-proxy — anyone who can reach Open WebUI can reach n8n; the shared session cookie means one Google sign-in covers both.

**No Cloudflare dashboard change is needed.** The tunnel already terminates at `oauth2-proxy:4180`, and `chat.zeoenergy.com/n8n/*` is just another path on the same hostname.

## First-run checklist

1. **Generate the encryption key.** From any shell:

   ```bash
   openssl rand -hex 32
   ```

   Paste the value into `N8N_ENCRYPTION_KEY` in `.env`. This key encrypts every stored credential (API tokens, OAuth secrets, DB passwords) at rest in Postgres. **Losing it permanently destroys all stored credentials** — workflow rows survive, but their auth blobs become unreadable garbage. Back it up alongside your other secrets before running anything real through the platform.

2. **Ensure `DEFAULT_LITELLM_MASTER_KEY` is populated** in `.env`. n8n's AI-node defaults use it (`N8N_AI_OPENAI_API_KEY`) so LangChain / OpenAI nodes see the local LiteLLM models without per-workflow credential entry.

3. **Bring up the stack** (from the repo root):

   ```bash
   docker compose -f ai/n8n/docker-compose.n8n.yml up -d
   docker logs -f n8n
   ```

   Wait for `Editor is now accessible via: https://chat.zeoenergy.com/n8n/`.

4. **Reload oauth2-proxy** so it picks up the new `OAUTH2_PROXY_UPSTREAMS` entry:

   ```bash
   docker compose -f ai/oauth2-proxy/docker-compose.oauth2-proxy.yml up -d --force-recreate
   ```

5. **Sign in.** Open <https://chat.zeoenergy.com/n8n/> in a browser. oauth2-proxy takes you through Google SSO if you don't already have a session cookie; then n8n's own owner-setup page asks you to create a local owner account. That account is the first admin inside n8n.

## AI-node wiring

The compose file preconfigures two env vars:

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
| `Editor is now accessible via: http://localhost:5678` in the logs | `N8N_HOST` / `N8N_PROTOCOL` / `N8N_PATH` didn't propagate. Check `.env` and recreate the container. |
| Login loop at `/n8n/` | oauth2-proxy wasn't restarted after adding `http://n8n:5678/n8n/` to `OAUTH2_PROXY_UPSTREAMS`. |
| Editor loads but assets 404 | `N8N_PATH` missing its trailing slash. It must be `/n8n/`, not `/n8n`. |
| Webhook URLs come out as `chat.zeoenergy.com/webhook/...` (no `/n8n/`) | `WEBHOOK_URL` wasn't set to the subpath variant. Editor URL and webhook URL are independent env vars in n8n. |
| Credentials all show as `unset` after a restart | `N8N_ENCRYPTION_KEY` changed between runs. Restore the original key from your secret store, or accept the loss and re-enter credentials. |
| Container refuses to start with `variable N8N_ENCRYPTION_KEY must be set` | The compose file uses `${N8N_ENCRYPTION_KEY:?...}` so it fails fast when unset — generate one with `openssl rand -hex 32` and paste it into `.env`. |

## Rollback

The wiring is fully additive. To back out:

1. Remove `,http://n8n:5678/n8n/` from `OAUTH2_PROXY_UPSTREAMS` in `.env`.
2. Recreate oauth2-proxy: `docker compose -f ai/oauth2-proxy/docker-compose.oauth2-proxy.yml up -d --force-recreate`.
3. Tear down n8n: `docker compose -f ai/n8n/docker-compose.n8n.yml down -v` (the `-v` drops the `n8n_data` + `n8n_db` volumes; omit if you want to keep workflow state around for a possible re-enable).
