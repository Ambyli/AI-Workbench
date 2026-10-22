# ai/roofix

The Roofix ↔ Phoenix bridge: one event-sourced worker (`roofix`, FastAPI + APScheduler, `app.py` + `components/`) that reads Roofix notification emails from Gmail, parses them, decides rules-first with a LiteLLM fallback (`components/brain.py`), and writes milestones / chatter to Phoenix's Postgres. Proposal pages are fetched through the sibling `ai/interceptor` service (there is no scraper service any more). Guide: [`ROOFIX.md`](ROOFIX.md). Compose: `docker-compose.roofix.yml` (`roofix-db` + `roofix`). Tests: `ai/roofix/tests/` (offline; `ROOFIX.md § Verifying it works`).

## Rules

- **`DRY_RUN=true` is the default and stays so on a fresh deploy**: the bridge fetches, parses, decides and logs, but writes nothing to Phoenix (a write returns the intended SQL + params). Flip to `false` only after inspecting a full tick; narrow `LISTENER_QUERY` to one project for first live writes.
- **Phoenix is written directly with psycopg2, Gmail read directly with the Google API + OAuth — not via MCP.** The MCP variants were reverted (Phoenix MCP write tools weren't ready). Don't reintroduce MCP clients here without being asked. `PHOENIX_DB_*` must be set; `PHOENIX_AGENT_USER_ID` is provisioned by hand.
- **Gmail OAuth is interactive once, locally**: `credentials.json` + `token.json` in `ROOFIX_BRIDGE_CONFIG_DIR` (bind-mounted read-only at `/config`); first login runs `components/gmail_client.py` on a laptop (`ROOFIX.md § Gmail OAuth setup`).
- **The AI fallback goes through LiteLLM** (`ROOFIX_LLM_URL`, default `http://litellm:4000`, `BRAIN_MODEL`). Swapping the model is a LiteLLM config / `BRAIN_MODEL` change, never a bridge code change.
- **Roofix session refresh is a manual operator flow**: the `roofix` profile lives in the interceptor; capture it on a laptop with a visible browser (`uv run cdp-spy`), tar it, `POST /profiles/roofix/refresh` (`INTERCEPTOR.md § Refreshing a profile (operator flow)`). Re-capture when `/capture` returns `login_wall: true`.
- **Keep the `-label:${ROOFIX_PROCESSED_LABEL}` clause in `LISTENER_QUERY`**, or processed emails refill the 25-message fetch window every tick. Backfill the label on old rows with `POST /labels/backfill`.
- **`config/field_mapping.json` is a stub on purpose** (Michael owns the mapping): milestone writes log "no milestone mapping" and skip until it is filled in. Not a bug to fix.
- **Both services are recreate-class**: two `roofix` copies would double-tick APScheduler, and the processed-email store upserts rather than claims. Never label it swap.
- Endpoint changes update `roofix.postman_collection.json` and `ROOFIX.md § Endpoints` together (root `CLAUDE.md` § Postman collections).

## Cross-folder rules

Also in the root `CLAUDE.md` § Cross-folder rules — read the named entry there for the full picture.

- **`cdp_interceptor` traps** — proposal fetches run `common.cdp_interceptor` inside `ai/interceptor`; the bridge only POSTs `/capture` (`INTERCEPTOR_URL`, `ROOFIX_PROFILE_NAME`). Bring the interceptor up first. (root `CLAUDE.md` § Cross-folder rules)
- **LiteLLM pass-throughs carry the auth** — `/v1/roofix` → `roofix:8080` in `ai/litellm/litellm_config.yaml` has `auth: true`; the bridge checks no token. (root `CLAUDE.md` § Cross-folder rules)
- **Trino reads other stacks' Postgres** — `postgres_roofix` reads `roofix-db:5432` by service DNS on `ai_shared` with the `ROOFIX_DB_*` credentials; changing them also needs `make up trino trino-coordinator`. (root `CLAUDE.md` § Cross-folder rules)
- **Postgres port group** — `PORT_ROOFIX_DB` is `5433` in the `PORT REGISTRY`. (root `CLAUDE.md` § Env vars and the PORT REGISTRY)

## Env vars

One line each; full detail in `.env.example` and `ROOFIX.md § Configuration`.

- `DRY_RUN` (`true`) — log decisions, skip Phoenix writes. Unrelated to `make up DRY_RUN=1` (make only honours the literal `1`).
- `AGENT_PHASE` (`0`) — `0` chatter + milestones only; `1` adds create / notify (stubbed).
- `TICK_INTERVAL_SECONDS` (`300`) — APScheduler cadence.
- `BRAIN_MODEL` (`qwen3.6`) — LiteLLM alias for the AI-fallback brain.
- `ROOFIX_SENDER` (`no-reply@roofix.io`) — Gmail search sender; two `o`s.
- `ROOFIX_PROCESSED_LABEL` (`roofix/processed`) — label put on every evaluated message, excluded in `LISTENER_QUERY`.
- `ESCALATION_RECIPIENTS` (empty) — forward escalations here; empty leaves them unread in Gmail.
- `GMAIL_CREDENTIALS_PATH` (`/config/credentials.json`) / `GMAIL_TOKEN_PATH` (`/config/token.json`) — OAuth client secret / refresh token.
- `PHOENIX_DB_HOST` / `_PORT` / `_NAME` / `_USER` / `_PASSWORD` / `_SSLMODE` (secrets) — direct psycopg2 connection to Phoenix.
- `PHOENIX_AGENT_USER_ID` (unset — required for writes) — the dedicated Phoenix user writes are attributed to.
- `PHOENIX_ROOFIX_ID_COLUMN` (`migration_external_id`) — where Roofix ids are stamped.
- `ROOFIX_DB_USER` / `_PASSWORD` / `_NAME` (`roofix`) — `roofix-db` (`ProcessedStore`); same default in compose and `app.py`'s DSN. On `ai_shared`: override the password before exposing it.
- `PORT_ROOFIX_DB` (`5433`) — host port of `roofix-db`.
- `INTERCEPTOR_URL` (`http://interceptor:8080`) — where `/capture` goes.
- `ROOFIX_PROFILE_NAME` (`roofix`) — interceptor profile holding the Roofix session.
