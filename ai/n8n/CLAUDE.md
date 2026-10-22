# ai/n8n

Self-hosted n8n workflow engine (+ its Postgres `n8n-db`) for prototyping automations across LiteLLM, Roofix, MCP servers and external HTTP. Guide: [`N8N.md`](N8N.md). Compose: `docker-compose.n8n.yml`.

## Rules

- **All container config lives in `.env`.** The compose file is a pure `${…}` template (same discipline as `ai/openwebui`); edit the `## n8n subsystem` block of `.env`, never the YAML.
- **`N8N_ENCRYPTION_KEY` is load-bearing.** It encrypts every stored credential blob in `n8n-db`; losing or rotating it leaves workflow rows intact but makes every credential unreadable. Set it before the first start, back it up, never change it. Compose fails fast via `${N8N_ENCRYPTION_KEY:?…}`.
- **Recreate-class**, both containers: `n8n-db` is a Postgres data dir, and two `n8n` copies would fire every schedule twice. Fixed names `n8n` / `n8n-db`.
- **Not exposed.** No host port for `n8n`, no `OAUTH2_PROXY_UPSTREAMS` entry, no oauth2-proxy instance of its own, no tunnel hostname — only `ai_shared` reaches `http://n8n:5678`, so browsers can't open the editor and outside services can't call webhooks. Never publish 5678 to get at it (the LAN would get the editor with no SSO). Exposing it is a planned change: `N8N.md § Exposing it later`.
- **When it is exposed: its own hostname, at root.** The subpath mode (`N8N_PATH=/n8n/`) is broken upstream (n8n-io/n8n#19635, #18596), so never route it under a path on `chat.zeoenergy.com`.
- **Path details:** `N8N_PATH` keeps its trailing slash (drop it and assets 404); the editor URL and the webhook URL are independent settings and both must be the public URL; `N8N_PROXY_HOPS` = the proxies in front (cloudflared + oauth2-proxy = 2 once exposed; harmless while nothing is).
- **No queue mode / Redis.** Single process is enough; if a backlog becomes real, `EXECUTIONS_MODE=queue` + a Redis service (Postgres already supports it).
- `N8N_DB_HOST` is the service DNS name — don't change it without renaming the service in the compose file.

## Cross-folder rules

Also in the root `CLAUDE.md` § Cross-folder rules — read the named entry there for the full picture.

- **n8n is not exposed** — nothing in `ai/oauth2-proxy` or `ai/cloudflared` routes to it. Exposing it adds its own hostname there (a second oauth2-proxy instance + a tunnel rule) and sets `N8N_HOST` / `N8N_PATH` / `N8N_EDITOR_BASE_URL` / `N8N_WEBHOOK_URL` / `N8N_PROXY_HOPS` to match. (root `CLAUDE.md` § Cross-folder rules)
- **Smaller couplings** (n8n's AI nodes → `ai/litellm`) — `N8N_AI_OPENAI_API_BASE=http://litellm:4000/v1` (service DNS on `ai_shared`, never `localhost`) and the key from `DEFAULT_LITELLM_MASTER_KEY`, so the OpenAI / LangChain credential picker lists LiteLLM's models; `DEFAULT_LITELLM_MASTER_KEY` must be populated. Override per credential only for an external provider. (root `CLAUDE.md` § Cross-folder rules)
- **Postgres port group** — `PORT_N8N_DB` 5435 is in the `PORT REGISTRY` Postgres group. (root `CLAUDE.md` § Env vars and the PORT REGISTRY)

## Env vars

One line each; full detail in `.env.example` and `N8N.md`.

- `N8N_ENCRYPTION_KEY` (unset — required) — credential encryption key; `openssl rand -hex 32`, back it up, never rotate (rules above).
- `N8N_DB_USER` / `_PASSWORD` / `_NAME` (`n8n` / `n8n` / `n8n`) — `n8n-db` credentials; dev defaults — override the password before exposing `PORT_N8N_DB` past the host.
- `PORT_N8N_DB` (`5435`) — host port of `n8n-db` for psql / DBeaver (`N8N.md § Connecting to the n8n DB`).
- `N8N_HOST` / `N8N_PROTOCOL` / `N8N_PATH` / `N8N_EDITOR_BASE_URL` / `N8N_WEBHOOK_URL` — the URL n8n bakes into its editor and webhook payloads; placeholders until it is exposed (`N8N.md § Exposing it later`).
- `N8N_AI_OPENAI_API_BASE` (`http://litellm:4000/v1`) — AI-node default base URL (`N8N.md § AI-node wiring`).
- `N8N_PROXY_HOPS` (`2`) — trusted proxy hops once exposed; topology-coupled.
- `N8N_DB_TYPE` / `N8N_DB_HOST` / `N8N_DB_INTERNAL_PORT`, `N8N_RUNNERS_ENABLED`, `N8N_TIMEZONE`, `N8N_DIAGNOSTICS_ENABLED`, `N8N_HIRING_BANNER_ENABLED`, `N8N_USER_MANAGEMENT_DISABLED` — see their `.env.example` comments.
