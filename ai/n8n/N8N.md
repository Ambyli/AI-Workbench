# n8n

Self-hosted [n8n](https://n8n.io) workflow engine — used here to prototype automations that stitch together LiteLLM (chat / tool models), the Roofix bridge, MCP servers, and any external HTTP / webhook surface without writing a bespoke service for each one.

Compose file: [`docker-compose.n8n.yml`](docker-compose.n8n.yml). Operator config lives in the root `.env` (`N8N_*`, `PORT_N8N_DB`), commented in `.env.example` and listed in [`CLAUDE.md`](CLAUDE.md) beside this guide. The compose file is a pure `${…}` template (same discipline as `ai/openwebui`): edit `.env`, never the YAML.

## What runs

| Container | Purpose | Network | Host port |
|---|---|---|---|
| `n8n` | Editor + workflow runtime (`n8nio/n8n`, port 5678 in-container) | `ai_shared` | _(none — not exposed, § Access)_ |
| `n8n-db` | Postgres 16 backing workflows, credentials, execution history | `ai_shared` | `PORT_N8N_DB` (default `5435`) |

Single-process runtime — no queue mode / Redis broker. If a workflow queue backlog becomes real, switch `EXECUTIONS_MODE=queue` and add a Redis service; not needed for prototyping.

## Access (not exposed)

n8n is **not reachable from outside the box's Docker network.** `n8n` publishes no host port, `OAUTH2_PROXY_UPSTREAMS` has no n8n entry, there is no oauth2-proxy instance of its own, and no Cloudflare tunnel hostname points at it — so no browser can open the editor and no outside service can call a webhook. Containers on `ai_shared` reach it at `http://n8n:5678`; `n8n-db` is reachable on `PORT_N8N_DB` (§ Connecting to the n8n DB).

Don't publish `5678` to get at the editor: that hands it to the whole LAN with no SSO in front.

### Exposing it later

Not done. Notes for whoever does it:

- **Its own hostname, at root — not a path on `chat.zeoenergy.com`.** n8n's subpath mode (`N8N_PATH=/n8n/`) is broken upstream (n8n-io/n8n#19635, #18596: assets fall through to the SPA, login redirects to `/n8n/n8n`); the maintainers recommend a subdomain. Run it with `N8N_PATH=/`.
- **SSO in front of that hostname.** oauth2-proxy routes by path on one hostname, so a second hostname needs its own oauth2-proxy instance (its own cookie name, secret and domain; its `https://<host>/oauth2/callback` added to the Google OAuth client's redirect URIs) and a new Cloudflare tunnel public hostname pointing at it — add a rule, never edit the `chat` one. Neither exists in the repo today.
- **Then set the URL n8n bakes into its pages**: `N8N_HOST`, `N8N_PROTOCOL`, `N8N_PATH` (keep the trailing slash), `N8N_EDITOR_BASE_URL` and `N8N_WEBHOOK_URL` (independent settings — set both) to the public URL, and `N8N_PROXY_HOPS` to the proxies in front (cloudflared + oauth2-proxy = 2); `make up n8n`.
- In the same change: `ai/AI_INFRA.md` (diagram edge, Ports at a glance, the n8n bullet), `ai/cloudflared/CLOUDFLARED.md`'s ingress table, and the n8n entries of the root and `ai/oauth2-proxy` / `ai/cloudflared` CLAUDE.md files.

`.env.example`'s URL values (`https://n8n.zeoenergy.com/`) are placeholders for that hostname; nothing serves them today.

## First-run checklist

1. **Generate the encryption key.** From any shell:

   ```bash
   openssl rand -hex 32
   ```

   Paste the value into `N8N_ENCRYPTION_KEY` in `.env`. This key encrypts every stored credential (API tokens, OAuth secrets, DB passwords) at rest in Postgres. **Losing it permanently destroys all stored credentials** — workflow rows survive, but their auth blobs become unreadable garbage. Back it up alongside your other secrets before running anything real through the platform.

2. **Ensure `DEFAULT_LITELLM_MASTER_KEY` is populated** in `.env`. n8n's AI-node defaults use it (`N8N_AI_OPENAI_API_KEY`) so LangChain / OpenAI nodes see the local LiteLLM models without per-workflow credential entry.

3. **Bring up the stack** (from the repo root):

   ```bash
   make up n8n          # recreate-class: two copies would fire every schedule twice
   docker logs -f n8n
   ```

   The `Editor is now accessible via: …` log line prints `N8N_EDITOR_BASE_URL` — nothing routes to it yet (§ Access).

4. **Owner account.** n8n's owner-setup page asks for a local owner account on the first browser visit to the editor, which needs n8n exposed first (§ Exposing it later). That account is the first admin inside n8n.

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
| Editor loads but assets 404 | `N8N_PATH` missing its trailing slash (`/`, never a bare `/x`) — or a subpath at all, which is broken upstream (§ Exposing it later). |
| Webhook URLs point at the wrong host | `N8N_WEBHOOK_URL` isn't the public URL. Editor URL and webhook URL are independent env vars in n8n. |
| Credentials all show as `unset` after a restart | `N8N_ENCRYPTION_KEY` changed between runs. Restore the original key from your secret store, or accept the loss and re-enter credentials. |
| Container refuses to start with `variable N8N_ENCRYPTION_KEY must be set` | The compose file uses `${N8N_ENCRYPTION_KEY:?...}` so it fails fast when unset — generate one with `openssl rand -hex 32` and paste it into `.env`. |

## Rollback

Nothing else depends on n8n, so removing it is one command: `docker compose -f ai/n8n/docker-compose.n8n.yml down -v` (the `-v` drops the `n8n_data` + `n8n_db` volumes; omit it to keep workflow state for a possible re-enable).
