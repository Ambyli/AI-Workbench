# Trino data lake

Trino coordinator + Hive Metastore + MinIO + Superset + a FastMCP shim,
brought up by a single compose file. Federates SQL over the three
existing Postgres instances (`litellm_db`, `roofix-db`, `sandbox-db`),
the off-box Phoenix production Postgres (`postgres_phoenix`, read-only
role, TLS required), plus an Iceberg lakehouse on MinIO. Consumers:

- **Models** — LiteLLM registers `trino-mcp:8080/mcp` alongside the
  Phoenix MCP, so any tool-calling model can run federated SQL.
- **Humans** — Superset at `chat.zeoenergy.com/superset/` behind
  oauth2-proxy.
- **External BI tools** — Trino JDBC on `PORT_TRINO` (default 8013),
  HTTPS + password auth. See [§ JDBC authentication](#jdbc-authentication).

## Quick start

```bash
docker network create ai_shared    # once, if you haven't already
# .env must have TRINO_JDBC_USERS (user:password[,…]) and TRINO_SHARED_SECRET
# (openssl rand -hex 32) set — compose refuses to start without them.
docker compose -f ai/trino/docker-compose.trino.yml up -d --build
# seed the Iceberg lakehouse with a demo table
docker compose -f ai/trino/docker-compose.trino.yml exec trino-mcp \
    python /app/ai/trino/bin/init_warehouse.py
```

Rough startup order: `hive-metastore-db` → `minio` → `minio-init`
(one-shot) → `hive-metastore` → `trino-auth-init` (one-shot) →
`trino-coordinator` → `trino-mcp` / `superset`. The compose file's `depends_on: service_healthy` /
`service_completed_successfully` conditions handle it; on a cold boot
the whole stack takes ~90 s.

## Endpoints

| Service | Endpoint | Notes |
|---|---|---|
| Trino web UI | `https://<host>:8013/ui/` | Self-signed cert; log in with a `TRINO_JDBC_USERS` account |
| Trino JDBC | `jdbc:trino://<host>:8013?SSL=true&SSLVerification=NONE` | For DBeaver / DataGrip — see [§ JDBC authentication](#jdbc-authentication) |
| Trino HTTP (internal) | `http://trino-coordinator:8080` | Container networks only, never published; username-only auth for `trino-mcp` / Superset |
| MinIO API | `http://localhost:8014` | S3-compatible |
| MinIO console | `https://chat.zeoenergy.com/minio/` | Behind oauth2-proxy |
| Superset | `https://chat.zeoenergy.com/superset/` | Behind oauth2-proxy |
| trino-mcp | `http://trino-mcp:8080/mcp` | Internal only, registered with LiteLLM |
| HMS Postgres | `psql -h localhost -p 5436 -U hive metastore` | Operator inspection only |
| Superset Postgres | `psql -h localhost -p 5437 -U superset superset` | Operator inspection only |

## JDBC authentication

The coordinator runs two listeners with different trust models:

| Listener | Published? | Auth | Who uses it |
|---|---|---|---|
| `:8443` HTTPS | yes — `PORT_TRINO` (8013) | password file (bcrypt) | DBeaver, DataGrip, web UI, laptops |
| `:8080` HTTP | **no** | username only (`allow-insecure-over-http`) | `trino-mcp`, Superset, `init_warehouse.py`, healthcheck |

Trino disables HTTP entirely once HTTPS + an authenticator are on;
`http-server.authentication.allow-insecure-over-http=true` in
`config/config.properties` re-enables it with the insecure (username-only)
authenticator. That is the same trust posture every other service on
`ai_shared` already has, and it is only safe because 8080 is never
published on the host. **Do not add an 8080 port mapping.**

### Accounts

Users live in `TRINO_JDBC_USERS` in `.env` as
`user:password[,user2:password2]`. The one-shot `trino-auth-init`
service bcrypt-hashes them into `password.db` inside the `trino_auth`
volume every time it runs. To add or rotate:

```bash
# edit TRINO_JDBC_USERS in .env, then re-run just the init container
make up trino trino-auth-init
```

The coordinator re-reads `password.db` every 5 s
(`file.refresh-period`) — no restart. Passwords may not contain `,`, `:`
or whitespace.

### DBeaver / DataGrip

New connection → **Trino** driver:

| Field | Value |
|---|---|
| Host | the Docker host's LAN IP or hostname |
| Port | `8013` |
| Username / Password | an entry from `TRINO_JDBC_USERS` |
| Driver property `SSL` | `true` |
| Driver property `SSLVerification` | `NONE` (self-signed cert) |

Equivalent URL form:

```
jdbc:trino://<host>:8013?SSL=true&SSLVerification=NONE
```

The JDBC driver refuses to send a password over plain HTTP, so `SSL=true`
is mandatory — a connection without it fails with "Authentication using
username/password requires SSL to be enabled".

To verify the cert instead of skipping verification, export it and point
the driver at it (PEM is accepted directly):

```bash
docker cp trino-coordinator:/etc/trino/auth/tls/trino.crt ./trino.crt
# DBeaver → Driver properties: SSLVerification=FULL, SSLTrustStorePath=/path/to/trino.crt
```

Full verification only works if the host you connect to is in the cert's
SANs — add `IP:<lan-ip>` / `DNS:<hostname>` to `TRINO_TLS_SANS` *before*
the first `up`, or rotate the cert (below).

### Trino CLI

```bash
# inside the container, over the internal HTTP listener (no password)
docker exec -it trino-coordinator trino

# from anywhere on the LAN, over HTTPS
docker exec -it trino-coordinator trino \
    --server https://localhost:8443 --insecure --user analyst --password
```

### Rotating the TLS cert

`trino-auth-init` generates `tls/trino.pem` once and reuses it. To
regenerate (e.g. after adding SANs):

```bash
docker compose -f ai/trino/docker-compose.trino.yml run --rm --entrypoint sh \
    trino-auth-init -c 'rm -f /auth/tls/trino.pem /auth/tls/trino.crt'
make up trino trino-auth-init
docker compose -f ai/trino/docker-compose.trino.yml restart trino-coordinator
```

Clients that pinned the old fingerprint must re-trust.

## MCP tools

`trino-mcp` exposes five tools. Discovery first, then `run_query`:

| Tool | Purpose |
|---|---|
| `list_catalogs()` | Every catalog Trino sees — `ai_agents`, `iceberg`, `postgres_litellm`, `postgres_phoenix`, `postgres_roofix`, `postgres_sandbox`, `system` |
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
| `trino_auth` | TLS keystore + `password.db` for the coordinator | Anytime — regenerates on next `up`; BI clients re-trust the new cert |

## Follow-ups

- **Real TLS cert for `PORT_TRINO`** — the coordinator serves a
  self-signed cert from `trino-auth-init`, so clients need
  `SSLVerification=NONE` or a pinned `trino.crt`. Replace `tls/trino.pem`
  in the `trino_auth` volume with a CA-issued key+cert PEM when there is
  a stable hostname for the box.
- **Move internal clients to HTTPS** — `trino-mcp`, Superset, and
  `init_warehouse.py` still use the username-only HTTP listener. Giving
  them a service account in `TRINO_JDBC_USERS` and `verify=/etc/trino/auth/tls/trino.crt`
  would let `allow-insecure-over-http` be turned off entirely.
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
