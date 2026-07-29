# Roofix ↔ Phoenix Bridge

A single-container subsystem that keeps [Phoenix](https://phoenix-mcp.com) in sync with the Roofix roofing CRM by watching the notification-email stream Roofix produces.

| Container | Purpose |
|---|---|
| `roofix` | Background worker. Fetches Roofix email via direct Gmail API → parses → decides (rules-first, LiteLLM fallback) → writes to Phoenix Postgres via direct psycopg2. Runs its own APScheduler. |

Internal-only — no host ports published by default. For proposal-page fetches (Roofix has no public API) the bridge calls the sibling **`interceptor-api`** container (see [INTERCEPTOR_API.md](INTERCEPTOR_API.md)) — a generic CDP-driving service that owns the Roofix login session as a named `--user-data-dir` profile.

### Quick start

```bash
# 1. interceptor-api must be up first — the bridge depends on it for proposal fetches.
docker compose -f ai/docker-compose.interceptor-api.yml up -d

# 2. Then the bridge.
docker compose -f ai/docker-compose.roofix.yml up -d
```

Default `DRY_RUN=true` — the bridge fetches, parses, decides, and logs, but does **not** write to Phoenix. Flip to `false` in `.env` only after watching a full run.

Before the bridge can hydrate proposals, upload a Roofix profile to interceptor-api once — see [INTERCEPTOR_API.md § Refreshing a profile](INTERCEPTOR_API.md#refreshing-a-profile-operator-flow). The uploaded profile lives at `/data/profiles/roofix/` inside the interceptor-api container.

### Endpoints

**`roofix:8080`**

| Endpoint | Purpose |
|---|---|
| `GET /health` | Container healthcheck. |
| `GET /status` | Last-tick timestamp, per-action decision counts, escalation counts, error count, effective `DRY_RUN` / `AGENT_PHASE`. |
| `POST /tick` | Manually process one batch now. Body optionally accepts `{"raw_emails": [...]}` (Contract A shape) to process crafted samples without hitting Gmail. |

Reach the bridge from another container on `ai_shared`:

```bash
docker exec -it litellm curl http://roofix:8080/status
docker exec -it litellm curl -X POST http://roofix:8080/tick
```

For interceptor-api endpoints (proposal capture, profile refresh), see [INTERCEPTOR_API.md](INTERCEPTOR_API.md).

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
                     │ interceptor-api│  ── CDP ─► roofix.io
                     └────────────────┘
```

> **Note:** an earlier version routed Gmail and Phoenix through MCP servers. The MCP write tools weren't ready in time for end-to-end testing, so this deploy reverts to direct Gmail API + direct psycopg2. The Contract A / `PhoenixClient.Result` shapes are unchanged so we can swap back to MCP later without touching the orchestrator or brain.

Every `TICK_INTERVAL_SECONDS` (default 300s) the bridge:

1. Fetches unread Roofix emails via Gmail API (`is:unread from:no-reply@roofix.io`).
2. Parses each into a normalized event (event_type, project_id, customer_name, address, comment_text, ...).
3. For each event, resolves the corresponding Phoenix project (by Roofix id, else by name + address).
4. The brain decides: `update_chatter`, `update_milestone`, `ignore`, or `escalate`. Rules handle the clear cases; anything ambiguous escalates to LiteLLM (the "AI fallback"), which returns the same Decision shape.
5. In DRY_RUN mode, the intended SQL + params are logged. Otherwise the writes are executed via psycopg2.

Ambiguous or thin `Estimate` / `Estimate Complete` events cause the bridge to call `RoofixScraperClient.get_proposal(tracking_url)` (`ai/roofix/components/roofix_scraper_client.py`) — which POSTs the tracking URL + Roofix's Bubble init/data + elasticsearch/mget URL patterns to `interceptor-api`'s `/capture` and reshapes the response into the `init_data` + `mget_docs` dict that `proposal_extractor.extract_proposal` consumes.

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
| `INTERCEPTOR_API_URL` | `http://interceptor-api:8080` | Sibling generic CDP service the bridge POSTs `/capture` to. |
| `ROOFIX_PROFILE_NAME` | `roofix` | Which named profile inside interceptor-api holds Roofix session cookies. Refresh via `POST /profiles/roofix/refresh` on interceptor-api. |
| `ROOFIX_CAPTURE_WINDOW_SECONDS` | `30` | How long interceptor-api keeps Chrome alive per `/capture` call collecting XHRs. |
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

### Session profile (Roofix on interceptor-api)

`interceptor-api` owns Roofix's login state as a named profile called `roofix` (configurable via `ROOFIX_PROFILE_NAME`). Every `/capture` call the bridge makes reuses that profile — no login prompt until the session expires.

**Canonical operator guide for capturing + uploading a profile: [INTERCEPTOR_API.md § Refreshing a profile](INTERCEPTOR_API.md#refreshing-a-profile-operator-flow).** In short:

```powershell
uv run cdp-spy --url https://roofix.io --profile-dir "C:\Users\<you>\.zeo\roofix_profile"   # log in visibly
tar czf roofix.tgz -C "C:\Users\<you>\.zeo\roofix_profile" .
curl -X POST -F "archive=@roofix.tgz" http://<host>:8080/profiles/roofix/refresh
```

The archive lands at `/data/profiles/roofix/` inside the interceptor-api container, backed by the `interceptor_api_data` volume. **Persistence:** survives `docker compose down`, restarts, rebuilds. **Destroyed by:** `docker compose -f ai/docker-compose.interceptor-api.yml down -v`.

Tracking URLs from Roofix notification emails redirect to the proposal without login, so many captures succeed even against an empty profile — but a warm profile is required for direct `roofix.io/project/{id}` fetches and covers you when a tokenized link expires.

#### When to re-capture

Sessions survive **days to weeks** in practice. Re-capture when:

1. **First time ever** — no `roofix` profile has been uploaded yet.
2. **Roofix expired the session** — the bridge log shows `login_wall: true` on `/capture` responses.
3. **You wiped the volume** — `docker compose -f ai/docker-compose.interceptor-api.yml down -v`.

#### Concurrency + cookie caveats

- **Concurrent `/capture` calls collide on the debug port.** interceptor-api serializes captures with a module-level lock (`ai/interceptor-api/app.py:63`) and returns **HTTP 409** if a capture is already running — the bridge should backoff-and-retry rather than fan out.
- **Cross-machine password caveat.** Cookies in a shipped profile work fine on Linux Docker. Chrome's saved-password blob (`Login Data`) is encrypted with a machine-bound key and won't decrypt inside the container — the container reuses cookies only. If they die you re-capture on your laptop and re-upload.

### Proposal extractor

The bridge takes the reshaped response from `RoofixScraperClient.get_proposal(tracking_url)` — the client wraps `interceptor-api`'s `/capture` and re-derives the legacy `init_data` + `mget_docs` shape — and turns it into a Phoenix-writable dataclass via `components.proposal_extractor.extract_proposal`. Pure function, no I/O.

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
   docker compose -f ai/docker-compose.interceptor-api.yml up -d --build
   docker compose -f ai/docker-compose.roofix.yml up -d --build
   docker exec -it interceptor-api curl http://localhost:8080/health
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
        roofix_scraper_client.py      HTTP client → interceptor-api /capture, reshapes to init_data+mget_docs
        notifier.py                   Phase 1 stub — CloudTalk / rep SMS
      config/
        field_mapping.json            Roofix event → Phoenix milestone map (Michael's file)
      tests/
        roofix_email_samples.py       real observed email shapes
        test_parser.py                offline parser suite
        test_brain.py                 offline brain/rules suite
```

Chrome-under-CDP lives in the sibling `ai/interceptor-api/` service (see `INTERCEPTOR_API.md`).

### Known limitations / TODOs

- **Phoenix writes are live via psycopg2** — no MCP dependency. The MCP client was reverted because the write tools weren't ready in time. `DRY_RUN=true` still short-circuits writes (returns Result with the intended SQL + params in `.data` for inspection).
- **`field_mapping.json` is a stub.** Michael owns the Roofix-event → Phoenix (block_name, status_id) mapping. `update_milestone` will log a "no milestone mapping" warning and skip until the file is filled in.
- **`PHOENIX_AGENT_USER_ID` must be provisioned manually.** Create a dedicated Phoenix agent user and set the env var so writes are attributable.
- **`SIGNING_EVENTS` set** (`Job Approval Confirmed`, `HIC Executed`) needs Jonathan's confirmation.
- **Phase 1** — `create_project` and `notify_rep` paths exist but are stubbed. Wire a CloudTalk MCP when needed.
- **Session refresh is manual.** interceptor-api cannot present a login UI — operators capture a profile on a laptop and upload the `.tgz` to `/profiles/roofix/refresh` (see [INTERCEPTOR_API.md § Refreshing a profile](INTERCEPTOR_API.md#refreshing-a-profile-operator-flow)).
