# Roofix Bridge

A background worker that keeps [Phoenix](https://phoenix-mcp.com) in sync with the Roofix roofing CRM by watching the notification-email stream Roofix produces. Reads Gmail via an MCP, decides what to do with each event (rules-first with LiteLLM as the AI fallback), and writes to Phoenix via the Phoenix MCP.

There is no public HTTP surface for callers — only healthcheck / status / manual-tick endpoints for operators. The bridge runs its own scheduler.

### Quick start

```bash
# First bring up the scraper it depends on
docker compose -f ai/docker-compose.roofix-scraper.yml up -d

# Then the bridge itself
docker compose -f ai/docker-compose.roofix-bridge.yml up -d
```

Default `DRY_RUN=true` — the bridge will fetch mail, parse it, decide what to do, and log everything, but no Phoenix writes will happen. Flip to `false` in `.env` only after watching a full run.

### Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /health` | Container healthcheck. |
| `GET /status` | Last-tick timestamp, per-action decision counts, escalation counts, error count, effective DRY_RUN / AGENT_PHASE. |
| `POST /tick` | Manually process one batch now. Body optionally accepts `{"raw_emails": [...]}` (Contract A shape) to process crafted samples without hitting Gmail. |

The service is internal-only by default — reach it from another container on the `ai_shared` network:

```bash
docker exec -it litellm curl http://roofix-bridge:8080/status
docker exec -it litellm curl -X POST http://roofix-bridge:8080/tick
```

### How it works

```
Gmail  →  Gmail MCP  →  ┐
                        │
Phoenix DB ↔  Phoenix MCP ┐
                        │
                ┌───────┴────────┐
                │  roofix-bridge  │  ── OpenAI SDK ──► litellm
                └───────┬────────┘
                        │
             roofix.io → roofix-scraper
```

Every `TICK_INTERVAL_SECONDS` (default 300s) the scheduler:

1. Fetches unread Roofix emails via the Gmail MCP (`is:unread from:no-reply@roofix.io`).
2. Parses each into a normalized event (event_type, project_id, customer_name, address, comment_text, ...).
3. For each event, resolves the corresponding Phoenix project (by Roofix id, else by name+address).
4. The brain decides: `update_chatter`, `update_milestone`, `ignore`, or `escalate`. Rules handle the clear cases; anything ambiguous escalates to LiteLLM (the "AI fallback"), which returns the same Decision shape.
5. In DRY_RUN mode, the intended tool + arguments are logged. Otherwise the Phoenix MCP write tools are called.

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
| `GMAIL_MCP_URL` | _(required)_ | Gmail MCP JSON-RPC endpoint. |
| `GMAIL_MCP_AUTH_VALUE` | _(required)_ | Bearer token for Gmail MCP. |
| `PHOENIX_MCP_URL` | _(from `DEFAULT_LITELLM_MCP_PHOENIX_URL`)_ | Phoenix MCP endpoint. |
| `PHOENIX_MCP_AUTH_VALUE` | _(from `DEFAULT_LITELLM_MCP_PHOENIX_AUTH_VALUE`)_ | Bearer token. |
| `PHOENIX_AGENT_USER_ID` | _(unset — required for writes)_ | Dedicated Phoenix user id the agent writes as. Provision manually. |
| `PHOENIX_ROOFIX_ID_COLUMN` | `migration_external_id` | Where the Roofix project id is stamped on the project row. |
| `PHOENIX_MCP_TOOL_QUERY` | `run_query` | Phoenix MCP read-only SQL tool name. |
| `PHOENIX_MCP_TOOL_INSERT_NOTE` | `insert_note` | Assumed name for the (future) Phoenix MCP note-insert tool. |
| `PHOENIX_MCP_TOOL_UPSERT_BLOCK` | `upsert_project_process_block` | Assumed name for the (future) milestone upsert tool. |
| `GMAIL_MCP_TOOL_SEARCH` | `search_threads` | Gmail MCP thread search tool. |
| `GMAIL_MCP_TOOL_GET` | `get_message` | Gmail MCP message fetch tool. |
| `GMAIL_MCP_TOOL_UNLABEL` | `unlabel_message` | Gmail MCP label-remove tool (used to mark-as-read). |
| `ROOFIX_SCRAPER_URL` | `http://roofix-scraper:8080` | Sibling scraper service. |
| `FIELD_MAPPING_PATH` | `/app/config/field_mapping.json` | Roofix-event → Phoenix (block_name, status_id) map. |
| `LOG_DIR` | `/data` | Where the per-tick CSV log lives (mounted volume). |

### Enabling Gmail access

The bridge does not talk to Gmail directly — it goes through a Gmail MCP server that must be set up in Google Cloud first. Follow Google's official guide to enable the Gmail API and configure the MCP:

**[Gmail API / MCP setup guide](https://developers.google.com/workspace/gmail/api/guides/configure-mcp-server#console)**

Do this against the Google Cloud project that owns `roofix@zeoenergy.com` (the listener inbox). Once the MCP is running, put its endpoint and bearer token into `.env` as `GMAIL_MCP_URL` and `GMAIL_MCP_AUTH_VALUE`, and mirror them into `DEFAULT_LITELLM_MCP_GMAIL_URL` / `..._AUTH_VALUE` if you also want LiteLLM to expose the MCP to other agents.

### Verifying it works

The order below matches the "known-good stack" verification path. Each step is checkable independently.

1. **Offline unit tests** (no Docker, no network):

   ```bash
   cd ai/roofix-bridge
   uv run --with pytest pytest tests/
   # or the standalone scripts:
   PYTHONPATH=. python tests/test_parser.py
   PYTHONPATH=. python tests/test_brain.py
   ```

2. **Bring up scraper**:

   ```bash
   docker compose -f ai/docker-compose.roofix-scraper.yml up -d --build
   docker exec -it roofix-scraper curl http://localhost:8080/health
   ```

3. **Bring up bridge with DRY_RUN=true**:

   ```bash
   DRY_RUN=true docker compose -f ai/docker-compose.roofix-bridge.yml up -d --build
   docker exec -it roofix-bridge curl http://localhost:8080/status
   ```

4. **Manual tick against real Gmail**:

   ```bash
   docker exec -it roofix-bridge curl -X POST http://localhost:8080/tick
   ```

   Watch `docker logs -f roofix-bridge` — you should see each stage: `listener fetch`, `parser parsed`, `brain <action>`, `phoenix <action>` with `DRY_RUN` prefix on the write attempts.

5. **Brain fallback path** — send a crafted event to exercise LiteLLM:

   ```bash
   docker exec -it roofix-bridge curl -X POST -H "Content-Type: application/json" \
     -d '{"raw_emails":[{"sender":"RFX | Something Weird <no-reply@roofix.io>","subject":"Foo - Jane Doe - 1 Main St","body_text":"..."}]}' \
     http://localhost:8080/tick
   ```

   The response should contain a decision with `source: "ai"`, meaning the LiteLLM connection is working.

6. **Turn writes on for a single project only** (advanced):

   Narrow `LISTENER_QUERY` to a specific project link, restart with `DRY_RUN=false`, run `/tick`, then flip back.

### Rebuilding

```bash
docker compose -f ai/docker-compose.roofix-bridge.yml up -d --build
```

### Project structure

```
ai/
  Dockerfile.roofix-bridge
  docker-compose.roofix-bridge.yml
  roofix-bridge/
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
```

### Known limitations / TODOs

- **Phoenix MCP writes.** The bridge assumes write tools land on the Phoenix MCP (see `PHOENIX_MCP_TOOL_INSERT_NOTE` / `..._UPSERT_BLOCK`). Until they do, keep `DRY_RUN=true` — the actual write calls will fail.
- **`field_mapping.json` is a stub.** Michael owns the Roofix-event → Phoenix (block_name, status_id) mapping. `update_milestone` will log a "no milestone mapping" warning and skip until the file is filled in.
- **`PHOENIX_AGENT_USER_ID` must be provisioned manually.** Create a dedicated Phoenix agent user and set the env var so writes are attributable.
- **`SIGNING_EVENTS` set** (`Job Approval Confirmed`, `HIC Executed`) needs Jonathan's confirmation.
- **Phase 1** — `create_project` and `notify_rep` paths exist but are stubbed. Wire the CloudTalk MCP when needed.
