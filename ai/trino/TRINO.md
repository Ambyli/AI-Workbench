# Trino data lake

Trino coordinator + Hive Metastore + MinIO + Superset + a FastMCP shim,
brought up by a single compose file. Federates SQL over the three
existing Postgres instances (`litellm_db`, `roofix-db`, `sandbox-db`)
plus an Iceberg lakehouse on MinIO. Consumers:

- **Models** — LiteLLM registers `trino-mcp:8080/mcp` alongside the
  Phoenix MCP, so any tool-calling model can run federated SQL.
- **Humans** — Superset at `chat.zeoenergy.com/superset/` behind
  oauth2-proxy.
- **External BI tools** — Trino JDBC on `PORT_TRINO` (default 8013),
  LAN-only for now.

## Quick start

```bash
docker network create ai_shared    # once, if you haven't already
docker compose -f ai/trino/docker-compose.trino.yml up -d --build
# seed the Iceberg lakehouse with a demo table
docker compose -f ai/trino/docker-compose.trino.yml exec trino-mcp \
    python /app/ai/trino/bin/init_warehouse.py
```

Rough startup order: `hive-metastore-db` → `minio` → `minio-init`
(one-shot) → `hive-metastore` → `trino-coordinator` → `trino-mcp` /
`superset`. The compose file's `depends_on: service_healthy` /
`service_completed_successfully` conditions handle it; on a cold boot
the whole stack takes ~90 s.

## Endpoints

| Service | Endpoint | Notes |
|---|---|---|
| Trino web UI | `http://localhost:8013/ui/` | LAN-only; no auth yet |
| Trino JDBC | `jdbc:trino://localhost:8013?user=<yours>` | For DBeaver / DataGrip |
| MinIO API | `http://localhost:8014` | S3-compatible |
| MinIO console | `https://chat.zeoenergy.com/minio/` | Behind oauth2-proxy |
| Superset | `https://chat.zeoenergy.com/superset/` | Behind oauth2-proxy |
| trino-mcp | `http://trino-mcp:8080/mcp` | Internal only, registered with LiteLLM |
| HMS Postgres | `psql -h localhost -p 5436 -U hive metastore` | Operator inspection only |
| Superset Postgres | `psql -h localhost -p 5437 -U superset superset` | Operator inspection only |

## MCP tools

`trino-mcp` exposes five tools. Discovery first, then `run_query`:

| Tool | Purpose |
|---|---|
| `list_catalogs()` | Every catalog Trino sees — `iceberg`, `postgres_litellm`, `postgres_roofix`, `postgres_sandbox`, `system` |
| `list_schemas(catalog)` | Schemas under a catalog |
| `list_tables(catalog, schema)` | Tables under a schema |
| `describe_table(catalog, schema, table)` | `[{"name":…, "type":…}, …]` |
| `run_query(sql, max_rows?)` | SELECT only; clamped to `TRINO_MCP_MAX_ROWS` rows and `TRINO_MCP_MAX_RUNTIME_S` seconds |

The MCP loop is the same two-step pattern documented in
`CLAUDE.md § LiteLLM with Phoenix MCP` — LiteLLM does not execute the
tool call itself; the caller (OpenWebUI or a `curl` script) forwards the
tool_call, hits `trino-mcp`'s HTTP endpoint, and sends the result back.

## Adding a catalog

Drop a `.properties` file in `ai/trino/catalogs/`, then:

```bash
docker compose -f ai/trino/docker-compose.trino.yml restart trino-coordinator
```

Example — real S3:

```properties
# ai/trino/catalogs/s3_prod.properties
connector.name=iceberg
iceberg.catalog.type=hive_metastore
hive.metastore.uri=thrift://hive-metastore:9083
fs.native-s3.enabled=true
s3.region=us-east-1
s3.aws-access-key=${ENV:PROD_S3_ACCESS_KEY}
s3.aws-secret-key=${ENV:PROD_S3_SECRET_KEY}
```

Then add `PROD_S3_ACCESS_KEY` / `PROD_S3_SECRET_KEY` under the
Trino data lake block in `.env` and reference them from the coordinator's
`environment:` in `docker-compose.trino.yml` so they land in the
container ENV `${ENV:...}` resolves against.

## Header-auth trust boundary

Superset is configured with `AUTH_TYPE = AUTH_REMOTE_USER` in
`superset/superset_config.py`. It trusts `X-Auth-Request-Email` from
oauth2-proxy as the session user with no separate password check.

**This is only safe when Superset is unreachable from anywhere except
oauth2-proxy.** The compose file publishes `PORT_SUPERSET` (default
8016) on the host — leaving that on `0.0.0.0` lets anyone on the LAN
spoof the header and log in as any Superset account. Before exposing
this host on an untrusted network:

1. Bind the port to loopback in `docker-compose.trino.yml`:
   `"127.0.0.1:${PORT_SUPERSET:-8016}:8088"`, or
2. Drop the `ports:` block entirely and reach Superset only via
   `chat.zeoenergy.com/superset/`.

Same posture as Open WebUI's trusted-header SSO — see
`CLAUDE.md § Threading Model` (unrelated) and Open WebUI's
`WEBUI_AUTH_TRUSTED_EMAIL_HEADER` docs for the sibling pattern.

## Connecting to the metastore DB

```bash
psql -h localhost -p 5436 -U hive metastore
\dt              # DBS, TBLS, PARTITIONS, SDS, …
SELECT * FROM "DBS";
```

Read-only inspection — never edit HMS's tables by hand. The Iceberg
connector expects the invariants HMS maintains (SDS/TBLS pointer
integrity, serde info shape); a manual UPDATE will silently break
`SELECT * FROM iceberg.<schema>.<table>`.

## Volumes

| Volume | Purpose | Kill when |
|---|---|---|
| `hive_metastore_db_data` | HMS's own tables (DBS, TBLS, …) | Never — this maps schema names to Parquet locations |
| `minio_data` | The actual Parquet files under `warehouse/` | Only after export |
| `superset_db_data` | Superset dashboards, saved queries, user accounts | Only if you want to start over |

## Follow-ups

- **JDBC auth for the Trino coordinator port** — either TCP terminator in
  front of `PORT_TRINO`, or Trino's built-in password authentication.
- **Idle-state teardown for Superset queries** — cancel long-running
  queries when the user disconnects.
- **Iceberg maintenance** — periodic `optimize`, snapshot expiry,
  orphan-file cleanup jobs. Set up as a scheduled Superset SQL or a
  standalone tick service.
- **Second HMS Postgres backup** — HMS's metadata is the single point
  of failure for the whole lakehouse. WAL-shipping or scheduled
  pg_dumps before this is production.
- **Real ingestion pipelines** — the demo seed via `bin/init_warehouse.py`
  is placeholder. Add per-source ingestion jobs (Claude usage JSONL,
  Roofix event exports, etc.) under `ai/trino/bin/`.
