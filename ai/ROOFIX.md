# Roofix ↔ Phoenix Bridge

A two-container subsystem that keeps [Phoenix](https://phoenix-mcp.com) in sync with the Roofix roofing CRM by watching the notification-email stream Roofix produces.

| Container | Purpose |
|---|---|
| `roofix-bridge` | Background worker. Fetches Roofix email via Gmail MCP → parses → decides (rules-first, LiteLLM fallback) → writes to Phoenix MCP. Runs its own APScheduler. |
| `roofix-scraper` | Playwright + Chromium. Fetches Roofix proposal pages on demand (Roofix has no public API). Owns the Roofix login session. |

Both are internal-only — no host ports published by default. The bridge depends on the scraper for hydrating thin `Estimate` / `Estimate Complete` events.

### Quick start

```bash
docker compose -f ai/docker-compose.roofix.yml up -d
```

Default `DRY_RUN=true` — the bridge fetches, parses, decides, and logs, but does **not** write to Phoenix. Flip to `false` in `.env` only after watching a full run.

### Endpoints

**`roofix-bridge:8080`**

| Endpoint | Purpose |
|---|---|
| `GET /health` | Container healthcheck. |
| `GET /status` | Last-tick timestamp, per-action decision counts, escalation counts, error count, effective `DRY_RUN` / `AGENT_PHASE`. |
| `POST /tick` | Manually process one batch now. Body optionally accepts `{"raw_emails": [...]}` (Contract A shape) to process crafted samples without hitting Gmail. |

**`roofix-scraper:8080`**

| Endpoint | Purpose |
|---|---|
| `GET /health` | Container healthcheck. |
| `GET /session` | Status of the persisted Roofix session file (present? size?). |
| `POST /session/refresh` | Accept a Playwright `storage_state` JSON body and persist it to the volume. |
| `GET /proposal/{project_id}?tracking_url=...` | Scrape a proposal. Optional `tracking_url` (from the notification email) is used instead of building `roofix.io/project/{id}` — useful when only the tokenized email link is available. |

Reach them from another container on `ai_shared`:

```bash
docker exec -it litellm curl http://roofix-bridge:8080/status
docker exec -it litellm curl -X POST http://roofix-bridge:8080/tick
docker exec -it roofix-bridge curl "http://roofix-scraper:8080/proposal/1782246308331x9098"
```

### How it works

```
Gmail  →  Gmail MCP  →  ┐
                        │
Phoenix DB ↔  Phoenix MCP ┐
                        │
                ┌───────┴────────┐
                │  roofix-bridge │  ── OpenAI SDK ──► litellm
                └───────┬────────┘
                        │
             roofix.io → roofix-scraper
```

Every `TICK_INTERVAL_SECONDS` (default 300s) the bridge:

1. Fetches unread Roofix emails via the Gmail MCP (`is:unread from:no-reply@roofix.io`).
2. Parses each into a normalized event (event_type, project_id, customer_name, address, comment_text, ...).
3. For each event, resolves the corresponding Phoenix project (by Roofix id, else by name + address).
4. The brain decides: `update_chatter`, `update_milestone`, `ignore`, or `escalate`. Rules handle the clear cases; anything ambiguous escalates to LiteLLM (the "AI fallback"), which returns the same Decision shape.
5. In DRY_RUN mode, the intended tool + arguments are logged. Otherwise the Phoenix MCP write tools are called.

Ambiguous or thin `Estimate` / `Estimate Complete` events cause the bridge to call the scraper's `/proposal/{id}` to hydrate the full field set from Roofix.

### Configuration

| Variable | Default | Purpose |
|---|---|---|
| `DRY_RUN` | `true` | When true, decisions are logged but no Phoenix writes happen. |
| `AGENT_PHASE` | `0` | `0` = chatter + milestones only. `1` (future) = project creation + rep notifications. |
| `TICK_INTERVAL_SECONDS` | `300` | Scheduler cadence. |
| `LITELLM_URL` | `http://litellm:4000` | Bridge's LiteLLM base URL (OpenAI-compatible). |
| `LITELLM_API_KEY` | _(from `DEFAULT_LITELLM_MASTER_KEY`)_ | Auth for LiteLLM. |
| `BRAIN_MODEL` | `qwen3.6` | LiteLLM model alias used for AI fallback decisions. |
| `ROOFIX_SENDER` | `no-reply@roofix.io` | Gmail search-query sender. **Note the two `o`s.** |
| `LISTENER_QUERY` | `is:unread from:${ROOFIX_SENDER}` | Full Gmail search query. Override to narrow the fetch — e.g. to a single project during first live tests. |
| `GMAIL_MCP_URL` | _(required)_ | Gmail MCP JSON-RPC endpoint. |
| `GMAIL_MCP_AUTH_VALUE` | _(required)_ | Bearer token for Gmail MCP. |
| `PHOENIX_MCP_URL` | _(from `DEFAULT_LITELLM_MCP_PHOENIX_URL`)_ | Phoenix MCP endpoint. |
| `PHOENIX_MCP_AUTH_VALUE` | _(from `DEFAULT_LITELLM_MCP_PHOENIX_AUTH_VALUE`)_ | Bearer token. |
| `PHOENIX_AGENT_USER_ID` | _(unset — required for writes)_ | Dedicated Phoenix user id the bridge writes as. Provision manually. |
| `PHOENIX_ROOFIX_ID_COLUMN` | `migration_external_id` | Where the Roofix project id is stamped on the project row. |
| `PHOENIX_MCP_TOOL_QUERY` | `run_query` | Phoenix MCP read-only SQL tool name. |
| `PHOENIX_MCP_TOOL_INSERT_NOTE` | `insert_note` | Assumed name for the (future) Phoenix MCP note-insert tool. |
| `PHOENIX_MCP_TOOL_UPSERT_BLOCK` | `upsert_project_process_block` | Assumed name for the (future) milestone upsert tool. |
| `GMAIL_MCP_TOOL_SEARCH` | `search_threads` | Gmail MCP thread search tool. |
| `GMAIL_MCP_TOOL_GET` | `get_message` | Gmail MCP message fetch tool. |
| `GMAIL_MCP_TOOL_UNLABEL` | `unlabel_message` | Gmail MCP label-remove tool (used to mark-as-read). |
| `ROOFIX_SCRAPER_URL` | `http://roofix-scraper:8080` | Sibling scraper service. |
| `ROOFIX_SESSION_PATH` | `/data/roofix_session.json` | Scraper's persisted Playwright storage_state. |
| `ROOFIX_HEADLESS` | `true` | Scraper's Chromium mode. Set `false` for local `uv run` sessions to watch the browser scrape in real time. |
| `FIELD_MAPPING_PATH` | `/app/config/field_mapping.json` | Roofix-event → Phoenix (block_name, status_id) map. |
| `LOG_DIR` | `/data` | Where the per-tick CSV log lives (mounted volume). |

### Session cookies (scraper)

Roofix logins can't happen inside a headless container — no browser UI to complete the flow. Refresh is a two-step operator action:

1. **On your laptop**, run [`save_roofix_session.py`](../../rufix-phoenix-bridge/save_roofix_session.py) from the source repo. A visible Chromium opens, you log into Roofix (including 2FA), then press Enter. It writes `roofix_session.json`.
2. **POST it to the scraper:**

   ```bash
   curl -X POST http://<host>:<published-port>/session/refresh \
     -H "Content-Type: application/json" \
     -d @roofix_session.json
   ```

   The file is persisted to the `roofix_scraper_data` volume so subsequent restarts reuse it.

Tracking URLs from Roofix notification emails redirect to the proposal without login, so many `/proposal/...` calls succeed without a session — but a session is preferred and required for the direct `roofix.io/project/{id}` path.

### Verifying it works

1. **Offline unit tests** (no Docker, no network):

   ```bash
   cd ai/roofix/bridge
   PYTHONPATH=. python tests/test_parser.py
   PYTHONPATH=. python tests/test_brain.py
   ```

2. **Bring up the stack**:

   ```bash
   docker compose -f ai/docker-compose.roofix.yml up -d --build
   docker exec -it roofix-scraper curl http://localhost:8080/health
   docker exec -it roofix-bridge curl http://localhost:8080/status
   ```

3. **Manual tick against real Gmail**:

   ```bash
   docker exec -it roofix-bridge curl -X POST http://localhost:8080/tick
   ```

   Watch `docker logs -f roofix-bridge` — you should see each stage: `listener fetch`, `parser parsed`, `brain <action>`, `phoenix <action>` with `DRY_RUN` prefix on write attempts.

4. **Brain fallback path** — send a crafted event to exercise LiteLLM:

   ```bash
   docker exec -it roofix-bridge curl -X POST -H "Content-Type: application/json" \
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
  Dockerfile.roofix-bridge
  Dockerfile.roofix-scraper
  ROOFIX.md
  roofix/
    bridge/
      pyproject.toml
      app.py                          FastAPI + APScheduler entry point
      components/
        parser.py                     email → normalized event (Contract B)
        brain.py                      rules-first decision + LiteLLM fallback
        orchestrator.py               parse → resolve → decide → execute
        logger.py                     CSV log to LOG_DIR
        gmail_client.py               Gmail MCP HTTP client
        phoenix_mcp_client.py         Phoenix MCP HTTP client (reads + planned writes)
        roofix_scraper_client.py      Sibling scraper HTTP client
        notifier.py                   Phase 1 stub — CloudTalk / rep SMS
      config/
        field_mapping.json            Roofix event → Phoenix milestone map (Michael's file)
      tests/
        roofix_email_samples.py       real observed email shapes
        test_parser.py                offline parser suite
        test_brain.py                 offline brain/rules suite
    scraper/
      pyproject.toml
      app.py                          FastAPI endpoints
      scraper.py                      Playwright fetch + response merging
      session.py                      storage_state load / save
```

### Known limitations / TODOs

- **Phoenix MCP writes are speculative.** The bridge assumes tools named `insert_note` and `upsert_project_process_block` will land on the Phoenix MCP. Until they do, keep `DRY_RUN=true` — the write calls will fail. Real names are configurable via env so no code change is needed when they land.
- **`field_mapping.json` is a stub.** Michael owns the Roofix-event → Phoenix (block_name, status_id) mapping. `update_milestone` will log a "no milestone mapping" warning and skip until the file is filled in.
- **`PHOENIX_AGENT_USER_ID` must be provisioned manually.** Create a dedicated Phoenix agent user and set the env var so writes are attributable.
- **`SIGNING_EVENTS` set** (`Job Approval Confirmed`, `HIC Executed`) needs Jonathan's confirmation.
- **Phase 1** — `create_project` and `notify_rep` paths exist but are stubbed. Wire a CloudTalk MCP when needed.
- **Session refresh is manual.** The scraper container cannot present a login UI.
