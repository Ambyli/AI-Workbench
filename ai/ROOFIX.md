# Roofix ↔ Phoenix Bridge

A single-container subsystem that keeps [Phoenix](https://phoenix-mcp.com) in sync with the Roofix roofing CRM by watching the notification-email stream Roofix produces.

| Container | Purpose |
|---|---|
| `roofix` | Background worker. Fetches Roofix email via direct Gmail API → parses → decides (rules-first, LiteLLM fallback) → writes to Phoenix Postgres via direct psycopg2. Runs its own APScheduler. |

Internal-only — no host ports published by default. For proposal-page fetches (Roofix has no public API) the bridge calls the sibling **`interceptor`** container (see [INTERCEPTOR_API.md](INTERCEPTOR_API.md)) — a generic CDP-driving service that owns the Roofix login session as a named `--user-data-dir` profile.

### Quick start

```bash
# 1. interceptor must be up first — the bridge depends on it for proposal fetches.
docker compose -f ai/docker-compose.interceptor.yml up -d

# 2. Then the bridge.
docker compose -f ai/docker-compose.roofix.yml up -d
```

Default `DRY_RUN=true` — the bridge fetches, parses, decides, and logs, but does **not** write to Phoenix. Flip to `false` in `.env` only after watching a full run.

Before the bridge can hydrate proposals, upload a Roofix profile to interceptor once — see [INTERCEPTOR_API.md § Refreshing a profile](INTERCEPTOR_API.md#refreshing-a-profile-operator-flow). The uploaded profile lives at `/data/profiles/roofix/` inside the interceptor container.

### Endpoints

**`roofix:8080`**

| Endpoint | Purpose |
|---|---|
| `GET /health` | Container healthcheck. |
| `GET /status` | Last-tick timestamp, per-action decision counts, escalation counts, error count, effective `DRY_RUN` / `AGENT_PHASE`. |
| `POST /tick` | Manually process one batch now. Body optionally accepts `{"raw_emails": [...]}` (Contract A shape) to process crafted samples without hitting Gmail. |
| `POST /execute/{message_id}` | Re-run one specific Gmail message through the pipeline. Fetches by id regardless of read/unread state, skips the `processed_store` dedup filter, otherwise runs the same orchestrator path a scheduled tick would (Phoenix writes, `mark_read`, escalation forward — all subject to `DRY_RUN`). Returns `404` if the id isn't visible to the OAuth token, `200 {records, count}` on success. |

Reach the bridge from another container on `ai_shared`:

```bash
docker exec -it litellm curl http://roofix:8080/status
docker exec -it litellm curl -X POST http://roofix:8080/tick
```

For interceptor endpoints (proposal capture, profile refresh), see [INTERCEPTOR_API.md](INTERCEPTOR_API.md).

### How it works

```
Gmail (Google API + OAuth) ──┐
                             │
Phoenix DB (psycopg2) ◄─────►│
                             │
                     ┌───────┴────────┐
                     │     roofix     │  ── OpenAI SDK ──► litellm
                     └───────┬────────┘
                             │
                             │  POST /capture
                             ▼
                     ┌────────────────┐
                     │ interceptor│  ── CDP ─► roofix.io
                     └────────────────┘
```

> **Note:** an earlier version routed Gmail and Phoenix through MCP servers. The MCP write tools weren't ready in time for end-to-end testing, so this deploy reverts to direct Gmail API + direct psycopg2. The Contract A / `PhoenixClient.Result` shapes are unchanged so we can swap back to MCP later without touching the orchestrator or brain.

Every `TICK_INTERVAL_SECONDS` (default 300s) the bridge:

1. Fetches unread Roofix emails via Gmail API (`is:unread from:no-reply@roofix.io`).
2. Parses each into a normalized event (event_type, project_id, customer_name, address, comment_text, ...).
3. For each event, resolves the corresponding Phoenix project (by Roofix id, else by name + address).
4. The brain decides: `update_chatter`, `update_milestone`, `ignore`, or `escalate`. Rules handle the clear cases; anything ambiguous escalates to LiteLLM (the "AI fallback"), which returns the same Decision shape.
5. In DRY_RUN mode, the intended SQL + params are logged. Otherwise the writes are executed via psycopg2.

Ambiguous or thin `Estimate` / `Estimate Complete` events cause the bridge to call `RoofixScraperClient.get_proposal(tracking_url)` (`ai/roofix/components/roofix_scraper_client.py`) — which POSTs the tracking URL + Roofix's Bubble init/data + elasticsearch/mget URL patterns to `interceptor`'s `/capture` and reshapes the response into the `init_data` + `mget_docs` dict that `proposal_extractor.extract_proposal` consumes.

### Execution matrix (what `_execute` does per action)

`orchestrator._execute` receives a Decision from the brain and dispatches on `decision["action"]`. Every branch does three things: (1) log to the CSV audit trail, (2) optionally write to Phoenix, (3) optionally record in `processed_store` and/or (via `app.py`) mark the source email read in Gmail. `mark_read` is decided in `app.py` after `_execute` returns, based on the flags `_execute` stamps on the decision dict.

| `action` | Phoenix write | `processed_store` | Gmail `mark_read` | Notes |
|---|---|---|---|---|
| `ignore` | — | `mark_ok({action, source, reasoning})` | only if `source=="rule"` | Terminal. AI-source ignores stay unread so an operator can review the model's judgment. Rule-source ignores are deterministic and safe to silence. |
| `escalate` / `needs_human=True` | — | `mark_ok({action, forwarded, source, reasoning})` | only if forward succeeded | If `gmail`, `ESCALATION_RECIPIENTS`, and the original email are all available, calls `gmail.forward_email(...)`. On success stamps `decision["_forwarded"]=True` (→ `app.py` marks read). On failure or when no recipients are configured, stamps `False` (→ stays unread for operator review in the Roofix inbox). |
| `update_chatter` | `phoenix.update_chatter(project_id, note_text)` | — | yes (default) | `target` is the Phoenix project id as str; cast to int. If `phoenix is None` or `DRY_RUN=true`, the write is short-circuited via the Result's `dry_run` flag; the audit row is prefixed `DRY_RUN`. |
| `update_milestone` | `phoenix.update_milestone(project_id, block_name, status_id)` | — | yes (default) | Looks up the milestone mapping using `event_type` as the key (`FIELD_MAPPING_PATH`). Missing mapping → log a "no milestone mapping for `<event>`" warning and skip the write. |
| `create_project` (accepted) | `phoenix.ensure_entity_and_project(payload)` | `mark_ok({roofix_project_id, phoenix_entity_id, phoenix_project_id, accepted:True})` on write success; `mark_error({error})` on write failure | yes on success (default) | Uses the `_extracted_payload` stashed by `_scrape_and_extract`. Bails if the payload or `roofix_project_id` is missing (audit row `orchestrator/create_project` with ok=False). |
| `create_project` (not accepted) | — | `mark_ok({roofix_project_id, accepted:False})` | yes (default) | Proposal wasn't accepted per the extractor's acceptance rule — log `orchestrator/not_accepted` and stop. |
| _anything else_ | — | `mark_error({error, source, reasoning})` | no (unread) | Most likely an AI hallucination (`"send_email"`, `"call_customer"`, etc.) or a Phase 1 action leaking into Phase 0. `mark_error` (not `mark_ok`) so `is_processed` returns False next tick — the brain gets another chance. Recurring errors here are a signal to tighten `SYSTEM_PROMPT`. |

Sub-cases that short-circuit before the action branches:
- **Offline dry-run** — when `phoenix is None`, every action other than `ignore` / `escalate` short-circuits with `orchestrator/{action}` ok=True and detail prefixed `offline dry-run:`. No Phoenix write, no `processed_store` write.
- **Scraper `no_docs`** (upstream of `_execute`, in `_scrape_and_extract`) — the raw email is `mark_error`'d with `{"error": "no_docs"}` so it retries next tick. `_execute` still runs but the `create_project` branch bails because `_extracted_payload` is missing.

### Configuration

| Variable | Default | Purpose |
|---|---|---|
| `DRY_RUN` | `true` | When true, decisions are logged but no Phoenix writes happen. |
| `AGENT_PHASE` | `0` | `0` = chatter + milestones only. `1` (future) = project creation + rep notifications. |
| `TICK_INTERVAL_SECONDS` | `300` | Scheduler cadence. |
| `ROOFIX_LLM_URL` | `http://litellm:4000` | Bridge's LiteLLM base URL (OpenAI-compatible). |
| `ROOFIX_LLM_API_KEY` | Auth for LiteLLM. |
| `BRAIN_MODEL` | `qwen3.6` | LiteLLM model alias used for AI fallback decisions. |
| `ROOFIX_SENDER` | `no-reply@roofix.io` | Gmail search-query sender. **Note the two `o`s.** |
| `LISTENER_QUERY` | `is:unread from:${ROOFIX_SENDER}` | Full Gmail search query. Override to narrow the fetch — e.g. to a single project during first live tests. |
| `ESCALATION_RECIPIENTS` | _(empty)_ | Comma-separated email addresses. When populated, escalate decisions are forwarded here ("[Roofix Escalation] …") and the original is marked read. When empty (or forward fails) the original stays unread so operators can review it in the Roofix inbox. Either way the email is marked ok in `processed_store` so it won't be re-processed. |
| `GMAIL_CREDENTIALS_PATH` | `/config/credentials.json` | OAuth 2.0 client secrets file from GCP. See [Gmail OAuth setup](#gmail-oauth-setup). |
| `GMAIL_TOKEN_PATH` | `/config/token.json` | Refresh-token file. Written by the first successful login; reused thereafter. |
| `ROOFIX_BRIDGE_CONFIG_DIR` | `./roofix/config` | Host dir containing `credentials.json` + `token.json`. Bind-mounted into the container at `/config` read-only. |
| `PHOENIX_DB_HOST` | _(required)_ | Phoenix Postgres hostname. |
| `PHOENIX_DB_PORT` | `5432` | Phoenix Postgres port. |
| `PHOENIX_DB_NAME` | _(required)_ | Database name. |
| `PHOENIX_DB_USER` | _(required)_ | DB user with read + write on the `phoenix` schema. |
| `PHOENIX_DB_PASSWORD` | _(required)_ | DB password. |
| `PHOENIX_DB_SSLMODE` | `require` | psycopg2 SSL mode (`require` / `verify-ca` / `verify-full` / `disable`). |
| `PHOENIX_AGENT_USER_ID` | _(unset — required for writes)_ | Dedicated Phoenix user id the bridge writes as. Provision manually. |
| `PHOENIX_ROOFIX_ID_COLUMN` | `migration_external_id` | Where the Roofix project id is stamped on the project row. |
| `ROOFIX_DB_USER` | `roofix` | User for the compose-managed `roofix-db` Postgres backing `ProcessedStore`. |
| `ROOFIX_DB_PASSWORD` | `roofix` | Password for `roofix-db`. Defaults to `roofix` in both `docker-compose.roofix.yml` and `ai/roofix/app.py`'s DSN, so the container comes up on a fresh clone. Override for anything past dev. |
| `ROOFIX_DB_NAME` | `roofix` | Database name inside `roofix-db`. |
| `PORT_ROOFIX_DB` | `5433` | Host port `roofix-db` binds to (container 5432 → host `5433`) so remote clients can connect. Lives in the `PORT REGISTRY` block at the top of `.env` alongside `PORT_POSTGRES` (litellm_db, 5432). |
| `INTERCEPTOR_URL` | `http://interceptor:8080` | Sibling generic CDP service the bridge POSTs `/capture` to. |
| `ROOFIX_PROFILE_NAME` | `roofix` | Which named profile inside interceptor holds Roofix session cookies. Refresh via `POST /profiles/roofix/refresh` on interceptor. |
| `ROOFIX_CAPTURE_WINDOW_SECONDS` | `30` | How long interceptor keeps Chrome alive per `/capture` call collecting XHRs. |
| `ROOFIX_LOGIN_TIMEOUT` | `300` | Seconds to wait for a login redirect to resolve before flagging `login_wall`. |
| `ROOFIX_MAX_MATCHES_PER_PATTERN` | `5` | Cap on captures per URL pattern returned in one `/capture` response. |
| `ROOFIX_INIT_DATA_URL_PATTERN` | `roofix\.io/api/1\.1/init/data` | Regex matched against captured XHR URLs to identify Bubble's page-hydrate endpoint. Override if Bubble ever renames it. |
| `ROOFIX_MGET_URL_PATTERN` | `roofix\.io/elasticsearch/mget` | Regex matched against captured XHR URLs to identify the batch-get endpoint (customer + HIC + job docs). |
| `FIELD_MAPPING_PATH` | `/app/config/field_mapping.json` | Roofix-event → Phoenix (block_name, status_id) map. |
| `LOG_DIR` | `/data` | Where per-tick logs live (mounted volume). Two files: `roofix.log` (stdlib text log; framework messages + compact per-decision echo) and `agent_log.csv` (structured audit trail via `common.logging_setup.CsvLogger`). |
| `DEBUG_LOGGING` | `false` | When true, promotes the stdlib file log to DEBUG level and the console to DEBUG. |

### Gmail OAuth setup

The bridge talks to Gmail directly (no MCP), so it needs an OAuth 2.0 client
secret file plus a saved refresh-token file. Both live in
`ROOFIX_BRIDGE_CONFIG_DIR` on the host, bind-mounted into `/config` in the
container.

**One-time setup:**

1. **Create an OAuth client in GCP.** Google Cloud Console → APIs & Services →
   Credentials → **Create Credentials → OAuth client ID → Desktop app**.
   Download the JSON, rename to `credentials.json`.
2. **Enable the Gmail API** on the same GCP project (APIs & Services →
   Library → Gmail API → Enable).
3. **Place `credentials.json`** in your host config dir (default:
   `ai/roofix/config/credentials.json`). Git-ignored.
4. **Run the interactive login once, locally**, so `token.json` gets written:

   ```powershell
   cd ai\roofix
   $env:GMAIL_CREDENTIALS_PATH = "$PWD\config\credentials.json"
   $env:GMAIL_TOKEN_PATH = "$PWD\config\token.json"
   uv run --package roofix python components\gmail_client.py
   ```

   A browser opens; sign in with the inbox account (`rufix@zeoenergy.com`),
   accept the scope. `token.json` is written next to `credentials.json`.

5. **Deploy.** The compose bind-mount at `${ROOFIX_BRIDGE_CONFIG_DIR}:/config:ro`
   makes both files visible to the container. Refresh tokens live for months
   — you re-run the interactive login only if the token is revoked, the
   scope changes, or Google expires it.

**Scopes:** the client requests `gmail.modify` — read messages + toggle labels.
Sufficient for polling and mark-as-read.

**Same file, another machine:** OAuth refresh tokens are portable — you can
capture `token.json` on your laptop and ship it into the container. Nothing
machine-bound about them (unlike Chrome's saved passwords).

### Session profile (Roofix on interceptor)

`interceptor` owns Roofix's login state as a named profile called `roofix` (configurable via `ROOFIX_PROFILE_NAME`). Every `/capture` call the bridge makes reuses that profile — no login prompt until the session expires.

**Canonical operator guide for capturing + uploading a profile: [INTERCEPTOR_API.md § Refreshing a profile](INTERCEPTOR_API.md#refreshing-a-profile-operator-flow).** In short:

```powershell
uv run cdp-spy --url https://roofix.io --profile-dir "C:\Users\<you>\.zeo\roofix_profile"   # log in visibly
tar czf roofix.tgz -C "C:\Users\<you>\.zeo\roofix_profile" .
curl -X POST -F "archive=@roofix.tgz" http://<host>:8080/profiles/roofix/refresh
```

The archive lands at `/data/profiles/roofix/` inside the interceptor container, backed by the `interceptor_data` volume. **Persistence:** survives `docker compose down`, restarts, rebuilds. **Destroyed by:** `docker compose -f ai/docker-compose.interceptor.yml down -v`.

Tracking URLs from Roofix notification emails redirect to the proposal without login, so many captures succeed even against an empty profile — but a warm profile is required for direct `roofix.io/project/{id}` fetches and covers you when a tokenized link expires.

#### When to re-capture

Sessions survive **days to weeks** on the fast path (single capture at a time refreshes cookies back to the base profile). Same-profile concurrent captures against interceptor use a temp clone and don't contribute cookie freshness to the base profile — under sustained same-profile burst load the base ages faster. Re-capture when:

1. **First time ever** — no `roofix` profile has been uploaded yet.
2. **Roofix expired the session** — the bridge log shows `login_wall: true` on `/capture` responses.
3. **You wiped the volume** — `docker compose -f ai/docker-compose.interceptor.yml down -v`.

#### Concurrency + cookie caveats

- **Concurrent `/capture` calls collide on the debug port.** interceptor serializes captures with a module-level lock (`ai/interceptor/app.py:63`) and returns **HTTP 409** if a capture is already running — the bridge should backoff-and-retry rather than fan out.
- **Cross-machine password caveat.** Cookies in a shipped profile work fine on Linux Docker. Chrome's saved-password blob (`Login Data`) is encrypted with a machine-bound key and won't decrypt inside the container — the container reuses cookies only. If they die you re-capture on your laptop and re-upload.

### Connecting to the processed-store DB

The bridge's dedup / audit store lives in a Postgres container (`roofix-db`) that comes up alongside `roofix` when you run the compose file. The port is exposed on the host so you can inspect the store from your laptop without `docker exec`.

**Connection details** (defaults; override via `.env`):

| Field | Value |
|---|---|
| Host | `<docker host>` (usually `localhost` if the compose stack runs on your box) |
| Port | `PORT_ROOFIX_DB` — default `5433` (in `.env`'s PORT REGISTRY block) |
| Database | `ROOFIX_DB_NAME` — default `roofix` |
| User | `ROOFIX_DB_USER` — default `roofix` |
| Password | `ROOFIX_DB_PASSWORD` — default `roofix` (change for anything past dev) |

**psql:**

```bash
psql "postgresql://roofix:$ROOFIX_DB_PASSWORD@localhost:5433/roofix"
```

**DBeaver / DataGrip / any Postgres GUI:** create a new Postgres connection with the values above.

**Schema** — single table:

```sql
CREATE TABLE processed (
    key           TEXT PRIMARY KEY,      -- Gmail message id
    processed_at  TIMESTAMPTZ NOT NULL,
    status        TEXT NOT NULL,         -- 'pending' | 'ok' | 'error'
    metadata      JSONB NOT NULL         -- action, source, reasoning, forwarded, ...
);
CREATE INDEX idx_processed_status ON processed(status);
```

**Useful queries** — `metadata` is JSONB, so use Postgres's native operators (not `json_extract`):

```sql
-- Everything, newest first
SELECT key, status, processed_at, metadata
FROM processed ORDER BY processed_at DESC LIMIT 50;

-- Counts by status
SELECT status, COUNT(*) FROM processed GROUP BY status;

-- All escalations, showing whether the forward succeeded
SELECT key,
       (metadata->>'forwarded')::bool AS forwarded,
       metadata->>'reasoning'         AS reasoning
FROM processed
WHERE metadata->>'action' = 'escalate';

-- Failed forwards (escalate but forward failed / no recipients)
SELECT * FROM processed
WHERE metadata->>'action'   = 'escalate'
  AND (metadata->>'forwarded')::bool = false;

-- Scrape errors (retry candidates)
SELECT key, processed_at, metadata->>'error' AS error
FROM processed WHERE status = 'error';

-- Everything that ran through create_project
SELECT key,
       metadata->>'roofix_project_id' AS roofix_id,
       metadata->>'phoenix_project_id' AS phoenix_id,
       (metadata->>'accepted')::bool  AS accepted
FROM processed
WHERE metadata ? 'roofix_project_id';

-- Reset a specific email so it gets reprocessed next tick
DELETE FROM processed WHERE key = '19f908fe4c0a892c';

-- Mass-retry all errors
DELETE FROM processed WHERE status = 'error';
```

**Concurrency note.** The bridge holds one open connection to this DB. Read-only queries from another client (psql, DBeaver) are safe any time — Postgres MVCC handles them. Bulk writes (`DELETE`, schema changes) are also safe but may briefly contend with a live tick; if you're making destructive changes, prefer stopping the roofix container first:

```bash
docker compose -f ai/docker-compose.roofix.yml stop roofix
# ...destructive query...
docker compose -f ai/docker-compose.roofix.yml start roofix
```

### Proposal extractor

The bridge takes the reshaped response from `RoofixScraperClient.get_proposal(tracking_url)` — the client wraps `interceptor`'s `/capture` and re-derives the legacy `init_data` + `mget_docs` shape — and turns it into a Phoenix-writable dataclass via `components.proposal_extractor.extract_proposal`. Pure function, no I/O.

**Why the extractor reads from both `init_data` AND `mget_docs`.** Roofix uses two different endpoints to hydrate a project page and each carries different data:

| Source | Contains | Why we need it |
|---|---|---|
| `init_data` (Bubble page-hydrate) | The **current** project's `custom.order1` doc — prices, funding type, progress-tracker stages, product config | mget doesn't reliably fetch the current order1; instead it returns the homeowner's OTHER past orders. init_data is where the current one lives. |
| `mget_docs` (elasticsearch batch-get) | `custom.homeowner` (customer + full address), `custom.hic` (signature + executed status), `custom.job1` (actual contract price + install date + status), `custom.warranty`, `custom.estimate1` | None of these are in init_data — Bubble only puts the current order1 there. |

If we only read init_data we'd be missing zip/city/state/street, contact info, actual signed contract price, install date, and the HIC signature. Direct answer to "why not just one endpoint."

**Doc types the extractor consumes:**

| Doc type | Field(s) extracted | Notes |
|---|---|---|
| `custom.order1` (from init_data) | `_id`, `external_project_id_text`, `display_text`, prices, `funding1_option_funding`, `financing_provider_option_loan_provider`, trade/type/product, sales_rep/estimator/office refs, progress-tracker stages | Filtered by URL project id — Bubble can return prior orders for the same homeowner, we need the current one. |
| `custom.homeowner` | name (first/last/full), full address (street/city/state/zip), `email_text`, `phone_nmber_text` (Bubble's typo), `stage_option_type__contact_` | The "customer address" data init_data was missing. |
| `custom.hic` | `status_option_contingency` ("executed" ⇒ signed), `signature_url_text` | Presence of "executed" is the primary acceptance signal. |
| `custom.job1` | `contract_price_number` (actual signed price, differs from estimate), `install_date_date`, `install_scheduled_date_date`, `status_option_job_status`, `shingle_color_v2_text`, `funding_source_option_funding` | Only present on accepted proposals — a job record only exists post-HIC-signing. |
| `custom.warranty` | Presence only (ref id) | Confirmation signal. |
| `custom.estimate1` | Not currently extracted (reserved) | |

**Acceptance rule.** Three-way OR — any one signal is sufficient:

1. `custom.hic` present AND `status_option_contingency == "executed"` — customer signed the contract. Primary and strongest.
2. `custom.job1` present AND `status_option_job_status` is set — a job record exists, which only happens post-HIC-signing.
3. `custom.homeowner.stage_option_type__contact_ == "customer"` — Roofix's own CRM classification. Reliable independent signal (unaccepted proposals show `"opportunity"`).

Redundancy is the point: if Roofix changes one field's behavior we still detect acceptance via the others. Individual signals are exposed on `ExtractedProposal.acceptance_signals` for audit / logging.

**Identity when order1 is absent.** For proposals that haven't been through a full lifecycle, init_data may still not contain a current-project order1 (rare edge case). In that case the extractor falls back to extracting the Bubble project id directly from the URL — `roofix.io/project/{id}` regex. Homeowner-only extraction is enough for the bridge to log "unaccepted, skipping."

**PII in fixtures.** The two committed fixtures in `tests/fixtures/proposal_{accepted,unaccepted}.json` are pruned + PII-redacted copies of real captures (email → `customer@example.test`, phone → `5555550100`, HIC signature URL → `//redacted-signature.example/...`). See `_regen_fixtures.py` if you need to refresh them from a new capture.

### Verifying it works

1. **Offline unit tests** (no Docker, no network):

   ```bash
   cd ai/roofix
   PYTHONPATH=. python tests/test_parser.py
   PYTHONPATH=. python tests/test_brain.py
   ```

2. **Bring up the stack**:

   ```bash
   docker compose -f ai/docker-compose.interceptor.yml up -d --build
   docker compose -f ai/docker-compose.roofix.yml up -d --build
   docker exec -it interceptor curl http://localhost:8080/health
   docker exec -it roofix curl http://localhost:8080/status
   ```

3. **Manual tick against real Gmail**:

   ```bash
   docker exec -it roofix curl -X POST http://localhost:8080/tick
   ```

   Watch `docker logs -f roofix` — you should see each stage: `listener fetch`, `parser parsed`, `brain <action>`, `phoenix <action>` with `DRY_RUN` prefix on write attempts.

4. **Brain fallback path** — send a crafted event to exercise LiteLLM:

   ```bash
   docker exec -it roofix curl -X POST -H "Content-Type: application/json" \
     -d '{"raw_emails":[{"sender":"RFX | Something Weird <no-reply@roofix.io>","subject":"Foo - Jane Doe - 1 Main St","body_text":"..."}]}' \
     http://localhost:8080/tick
   ```

   The response should contain a decision with `source: "ai"`, meaning the LiteLLM connection is working.

5. **Turn writes on for a single project** (advanced):

   Narrow `LISTENER_QUERY` to a specific project link, restart with `DRY_RUN=false`, run `/tick`, then flip back.

### Rebuilding

```bash
docker compose -f ai/docker-compose.roofix.yml up -d --build
```

### Project structure

```
ai/
  docker-compose.roofix.yml
  Dockerfile.roofix
  ROOFIX.md
  roofix/
    bridge/
      pyproject.toml
      app.py                          FastAPI + APScheduler entry point
      components/
        parser.py                     email → normalized event (Contract B)
        brain.py                      rules-first decision + LiteLLM fallback
        orchestrator.py               parse → resolve → decide → execute
        gmail_client.py               Gmail API listener (OAuth 2.0)
        phoenix_client.py             Phoenix Postgres client (psycopg2, reads + writes)
        roofix_scraper_client.py      HTTP client → interceptor /capture, reshapes to init_data+mget_docs
        notifier.py                   Phase 1 stub — CloudTalk / rep SMS
      config/
        field_mapping.json            Roofix event → Phoenix milestone map (Michael's file)
      tests/
        roofix_email_samples.py       real observed email shapes
        test_parser.py                offline parser suite
        test_brain.py                 offline brain/rules suite
```

Chrome-under-CDP lives in the sibling `ai/interceptor/` service (see `INTERCEPTOR_API.md`).

### Known limitations / TODOs

- **Phoenix writes are live via psycopg2** — no MCP dependency. The MCP client was reverted because the write tools weren't ready in time. `DRY_RUN=true` still short-circuits writes (returns Result with the intended SQL + params in `.data` for inspection).
- **`field_mapping.json` is a stub.** Michael owns the Roofix-event → Phoenix (block_name, status_id) mapping. `update_milestone` will log a "no milestone mapping" warning and skip until the file is filled in.
- **`PHOENIX_AGENT_USER_ID` must be provisioned manually.** Create a dedicated Phoenix agent user and set the env var so writes are attributable.
- **Phase 1** — `create_project` and `notify_rep` paths exist but are stubbed. Wire a CloudTalk MCP when needed.
- **Session refresh is manual.** interceptor cannot present a login UI — operators capture a profile on a laptop and upload the `.tgz` to `/profiles/roofix/refresh` (see [INTERCEPTOR_API.md § Refreshing a profile](INTERCEPTOR_API.md#refreshing-a-profile-operator-flow)).
