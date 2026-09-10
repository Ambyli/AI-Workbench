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
| `list_catalogs()` | Every catalog Trino sees — `aws_glue`, `iceberg`, `postgres_litellm`, `postgres_phoenix`, `postgres_roofix`, `postgres_sandbox`, `supabase_ai_agents`, `supabase_enerflo_leads`, `system` |
| `list_schemas(catalog)` | Schemas under a catalog |
| `list_tables(catalog, schema)` | Tables under a schema |
| `describe_table(catalog, schema, table)` | `[{"name":…, "type":…}, …]` |
| `run_query(sql, max_rows?)` | SELECT only; clamped to `TRINO_MCP_MAX_ROWS` rows and `TRINO_MCP_MAX_RUNTIME_S` seconds |

The MCP loop is the same two-step pattern documented in
`CLAUDE.md § LiteLLM with Phoenix MCP` — LiteLLM does not execute the
tool call itself; the caller (OpenWebUI or a `curl` script) forwards the
tool_call, hits `trino-mcp`'s HTTP endpoint, and sends the result back.

## Adding a catalog

A catalog is one `.properties` file in `ai/trino/catalogs/`. The
filename (minus `.properties`) becomes the catalog name in SQL, so
`supabase_enerflo_leads.properties` is queried as
`supabase_enerflo_leads.<schema>.<table>`. Every catalog is visible to every
Trino login and to `trino-mcp` — there is no per-catalog access control
yet (see [Follow-ups](#follow-ups)).

### Checklist

Four files change for a catalog that needs credentials. Do them together
— a missing step fails only at query time, with an unhelpful error.

| # | File | What |
|---|---|---|
| 1 | `ai/trino/catalogs/<name>.properties` | Connector + connection settings. Reference secrets as `${ENV:VAR}`, never inline. |
| 2 | `ai/trino/docker-compose.trino.yml` | Add each `VAR: ${VAR}` to `trino-coordinator` → `environment:`. `${ENV:VAR}` resolves against the **container** env, not `.env` directly. Skipping this makes the var resolve to empty → auth failure. |
| 3 | `.env` | Real values, in the `## Trino data lake` block. Note `.env` is gitignored — the box's copy must be edited too. |
| 4 | `.env.example` | Same keys, placeholder values (`change-me`). Keep the two files' variable lists identical. |

Then, because the container's environment changed, **recreate** rather
than restart:

```bash
make up trino trino-coordinator      # picks up env + new catalog file
```

`docker compose restart trino-coordinator` is enough only when you edit
an existing `.properties` file without touching env vars.

Also add the new name to the `list_catalogs()` row in
[MCP tools](#mcp-tools) and the docstring in `ai/trino/mcp/server.py` so
models get an accurate hint.

### Naming

- Underscores only. A hyphen (`my-db`) is legal on disk but
  `my-db.public.t` is a lex error in SQL.
- Don't shadow `system` or `information_schema`.
- Convention: `<source>_<dataset>` — name by where the data lives, then
  what it is. `postgres_<subsystem>` for Postgres instances we run or
  are handed directly (`postgres_litellm`, `postgres_roofix`,
  `postgres_sandbox`, `postgres_phoenix`); `supabase_<project>` for
  Supabase-hosted projects (`supabase_ai_agents`,
  `supabase_enerflo_leads`); `aws_glue` for the Glue Data Catalog. Don't
  name catalogs after the tool you used to reach the data before
  (`athena`) or the owner (`zeo_*`) — every catalog here is ours, so
  that carries no information.

### Reaching the database

Pick the host form by where the target lives. Getting this wrong is the
most common failure and shows up as `The connection attempt failed` with
either an unresolvable hostname or `Network is unreachable` at the
bottom of the stack trace.

| Target lives… | `connection-url` host | Example |
|---|---|---|
| On `ai_shared` (any compose service that joins it) | Docker service DNS | `roofix-db:5432` |
| On an isolated Docker network (`litellm`'s `internal`, sandbox's `sandbox_state`) | `host.docker.internal:<host-published-port>` | `host.docker.internal:5434` |
| Off-box (Supabase, RDS, a partner DB) | Public hostname | `aws-1-us-east-1.pooler.supabase.com:5432` |

`host.docker.internal` works because the coordinator has
`extra_hosts: host.docker.internal:host-gateway` — Linux Docker does not
define it by default. Do **not** attach `trino-coordinator` to another
subsystem's internal network just to reach its DB; that breaches the
isolation those networks exist for (see `ai/sandbox/SANDBOX.md`). The
operator can sever a `host.docker.internal` read path by unpublishing
the port in `.env`.

### Hosted Postgres gotchas

Learned the hard way on `postgres_phoenix`, `supabase_ai_agents`, and
`supabase_enerflo_leads`:

- **TLS.** Managed Postgres (Supabase, Phoenix, RDS) rejects plaintext.
  Append `?sslmode=require` to the JDBC URL — the driver defaults to no
  SSL. We parameterise it as `?sslmode=${ENV:<NAME>_DB_SSLMODE}` with a
  `require` default in compose.
- **Supabase is IPv6-only on the direct host.** `db.<ref>.supabase.co`
  publishes an AAAA record and nothing else; the box has no IPv6 route,
  so you get `Network is unreachable`. Use the **session pooler**
  instead: host `aws-N-<region>.pooler.supabase.com`, port `5432`, user
  `postgres.<project-ref>`. Not port 6543 — that is transaction mode and
  breaks the JDBC driver's prepared statements. The dashboard's
  Connect → Session pooler panel shows the exact values.
- **`$` in passwords.** Compose interpolates `.env` values, so `ab$cd`
  becomes `ab` plus an empty variable `cd` (with a
  `WARN … variable is not set` line). Write a literal `$` as `$$`.
- **`#` in usernames** (Phoenix's `zeo.mcp#ro-phoenix-prod`) is fine in
  `.properties` and compose. If you ever paste the URL into DBeaver by
  hand, encode it as `%23`.
- **Mixed-case table names.** Trino lowercases every identifier, quoted
  or not. A Prisma / ORM-built schema with tables like
  `"LiteLLM_ModelTable"` therefore fails with `Table … does not exist`
  even though `SHOW TABLES` lists it. Add
  `case-insensitive-name-matching=true` to the catalog — Trino then
  resolves the lowercase name against the remote catalog. Set on every
  externally-owned Postgres catalog by default; only errors if two remote
  names collide ignoring case.
- **Use a read-only role** in the properties file wherever the source
  offers one. `trino-mcp` is SELECT-only, but a DBeaver login is not —
  the DB-side role is what actually stops a write.

### Templates

**Postgres — compose-managed, on `ai_shared`:**

```properties
connector.name=postgresql
connection-url=jdbc:postgresql://<service>:5432/${ENV:X_DB_NAME}
connection-user=${ENV:X_DB_USER}
connection-password=${ENV:X_DB_PASSWORD}
```

**Postgres — hosted / off-box (Supabase, RDS, partner):**

```properties
connector.name=postgresql
connection-url=jdbc:postgresql://${ENV:X_DB_HOST}:${ENV:X_DB_PORT}/${ENV:X_DB_NAME}?sslmode=${ENV:X_DB_SSLMODE}
connection-user=${ENV:X_DB_USER}
connection-password=${ENV:X_DB_PASSWORD}
case-insensitive-name-matching=true
```

with, in `docker-compose.trino.yml` under `trino-coordinator` →
`environment:`:

```yaml
X_DB_HOST: ${X_DB_HOST}
X_DB_PORT: ${X_DB_PORT:-5432}
X_DB_NAME: ${X_DB_NAME:-postgres}
X_DB_USER: ${X_DB_USER}
X_DB_PASSWORD: ${X_DB_PASSWORD}
X_DB_SSLMODE: ${X_DB_SSLMODE:-require}
```

**MySQL:**

```properties
connector.name=mysql
connection-url=jdbc:mysql://${ENV:X_DB_HOST}:3306
connection-user=${ENV:X_DB_USER}
connection-password=${ENV:X_DB_PASSWORD}
```

**Iceberg on real S3** (shares our Hive Metastore):

```properties
connector.name=iceberg
iceberg.catalog.type=hive_metastore
hive.metastore.uri=thrift://hive-metastore:9083
fs.native-s3.enabled=true
s3.region=us-east-1
s3.aws-access-key=${ENV:PROD_S3_ACCESS_KEY}
s3.aws-secret-key=${ENV:PROD_S3_SECRET_KEY}
```

**AWS Glue / Athena** (Trino has no Athena connector — read Glue + S3
directly, which is what Athena itself does; see `aws_glue.properties`):

```properties
connector.name=hive
hive.metastore=glue
hive.metastore.glue.region=${ENV:X_REGION}
hive.metastore.glue.aws-access-key=${ENV:X_ACCESS_KEY_ID}
hive.metastore.glue.aws-secret-key=${ENV:X_SECRET_ACCESS_KEY}
fs.native-s3.enabled=true
s3.region=${ENV:X_REGION}
s3.aws-access-key=${ENV:X_ACCESS_KEY_ID}
s3.aws-secret-key=${ENV:X_SECRET_ACCESS_KEY}
```

Hive-format tables (Parquet, ORC, CSV, JSON) work as-is. Tables Athena
created as **Iceberg** fail with "not a Hive table"; for those add a
second catalog with `connector.name=iceberg` +
`iceberg.catalog.type=glue` on the same credentials and set
`hive.iceberg-catalog-name=<that catalog>` here so Trino redirects.

Other first-party connectors (BigQuery, Snowflake, Redshift, ClickHouse,
MongoDB, Kafka, Delta Lake, …) follow the same shape — `connector.name`
from the [Trino connector index](https://trino.io/docs/current/connector.html),
then per-connector keys. Anything that needs a credentials *file* (e.g.
BigQuery's service-account JSON) also needs a read-only volume mount on
the coordinator.

### Verifying

```bash
# env made it into the container (should print the real value)
docker exec trino-coordinator printenv X_DB_PASSWORD

# catalog loaded + reachable
docker exec -it trino-coordinator trino --execute "SHOW SCHEMAS FROM <name>"
```

If `SHOW CATALOGS` lists it but `SHOW SCHEMAS` fails, the file is fine
and the problem is network or credentials — read the last line of the
error, it names the real cause (`host.docker.internal` → missing
`extra_hosts`; `Network is unreachable` → IPv6-only host; `password
authentication failed` → wrong secret or unescaped `$`).

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

Superset is now the only service in the stack that trusts a proxy-set
identity header. Open WebUI used the same pattern via
`WEBUI_AUTH_TRUSTED_EMAIL_HEADER` and has since been moved to its own OIDC —
[ai/openwebui/OPENWEBUI.md § Single sign-on](../openwebui/OPENWEBUI.md#single-sign-on)
covers why, and the reasoning about reachability applies here unchanged.

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
- **Per-catalog access control** — every `TRINO_JDBC_USERS` login and
  `trino-mcp` can read every catalog, including Phoenix production and
  the Supabase projects. Trino's file-based access control
  (`access-control.name=file` + a `rules.json` mapping users/groups to
  catalogs and schemas) is the cheap fix once some sources need to be
  restricted to some people.
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
