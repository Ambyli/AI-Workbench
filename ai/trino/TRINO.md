# Trino data lake

Trino coordinator + Hive Metastore + an S3 object store (`lake-s3`,
RustFS) + Superset + a FastMCP shim,
brought up by a single compose file. Federates SQL over the
Postgres instances this repo runs (`litellm_db`, `roofix-db`, `sandbox-db`,
`supabase-db`, the classifier's `classifier-db` — jobs, references and
per-call model usage, `postgres_classifier` — and the route-policy service's
`route-policy-db` — every routing decision and what answered it,
`postgres_route_policy`),
the off-box Phoenix production and staging Postgres (`postgres_phoenix_prod`
/ `postgres_phoenix_stage`, read-only roles, TLS required), plus an Iceberg
lakehouse on `lake-s3`. Consumers:

- **Models** — LiteLLM registers `trino-mcp:8080/mcp` alongside the
  Phoenix MCP, so any tool-calling model can run federated SQL. Claude
  Code connects through LiteLLM's `/mcp/trino` — see
  [§ Using it from Claude Code](#using-it-from-claude-code).
- **Humans** — Superset at `https://superset.zeoenergy.com/`
  (`SUPERSET_PUBLIC_HOST`), behind its own oauth2-proxy instance,
  `oauth2-proxy-superset` — see [§ Header-auth trust
  boundary](#header-auth-trust-boundary).
- **External BI tools** — Trino JDBC on `PORT_TRINO` (default 8013),
  HTTPS + password auth. See [§ JDBC authentication](#jdbc-authentication).

## Quick start

```bash
docker network create ai_shared    # once, if you haven't already
# .env must have TRINO_JDBC_USERS (user:password[,…]) and TRINO_SHARED_SECRET
# (openssl rand -hex 32) set — compose refuses to start without them.
docker compose -f ai/trino/docker-compose.trino.yml up -d --build
# seed the Iceberg lakehouse with a demo table — the script is not baked
# into the trino-mcp image, so it is piped in and run with that image's uv
# environment (which has `common.trino`)
docker compose -f ai/trino/docker-compose.trino.yml --env-file .env -p ai-trino \
    exec -T trino-mcp uv run --no-sync python - < ai/trino/bin/init_warehouse.py
```

Rough startup order: `hive-metastore-db` → `lake-s3` → `lake-s3-init`
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
| lake-s3 S3 API | `http://<host>:8014` | S3-compatible, path-style only; keys are `LAKE_S3_ACCESS_KEY` / `LAKE_S3_SECRET_KEY` |
| lake-s3 console | `https://chat.zeoenergy.com/rustfs/console/` | Behind `oauth2-proxy-openwebui`; LAN fallback `http://<host>:8015/rustfs/console/` — see [§ Object store: RustFS](#object-store-rustfs) |
| Superset | `https://superset.zeoenergy.com/` (`SUPERSET_PUBLIC_HOST`) | Behind `oauth2-proxy-superset`, served from the root; LAN `http://<host>:8016/` (edge, identity headers stripped, so it signs no one in) |
| trino-mcp | `http://trino-mcp:8080/mcp` | Internal only, registered with LiteLLM; external clients use `http://<host>:4001/mcp/trino` |
| HMS Postgres | `psql -h localhost -p 5436 -U hive metastore` | Operator inspection only |
| Superset Postgres | `psql -h localhost -p 5437 -U superset superset` | Operator inspection only |

Host ports are `PORT_TRINO` (8013 → `:8443`), `PORT_LAKE_S3_API` /
`PORT_LAKE_S3_CONSOLE` (8014 / 8015 → `9000` / `9001`), `PORT_SUPERSET`
(8016, published by `ai/edge`, not this compose file), and `PORT_HMS_DB` /
`PORT_SUPERSET_DB` (5436 / 5437), all in the `PORT REGISTRY` block of `.env`.

## Networks

The stack uses two networks:

- **`analytics_net`** — the data plane: Trino ↔ HMS ↔ HMS Postgres, plus
  `lake-s3`'s second leg so Trino reaches it by service DNS. It is a plain
  bridge, **not** `internal: true`: `lake-s3` already publishes host ports on
  `ai_shared`, so hardening the data plane would gain nothing and would
  complicate future ingestion jobs.
- **`ai_shared`** — only the surfaces external callers reach:
  `trino-coordinator` (JDBC / web UI), `lake-s3` (S3 API + console),
  `superset` (UI), and `trino-mcp` (LiteLLM's MCP endpoint).

Nothing outside `analytics_net` talks to HMS directly; Trino is its sole client.

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

Enabling client authentication also makes Trino demand a node-to-node
secret, even on a single node: `TRINO_SHARED_SECRET`, referenced from
`config/config.properties` as `${ENV:TRINO_SHARED_SECRET}` (generate with
`openssl rand -hex 32`; compose refuses to start without it).

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
| `list_catalogs()` | Every catalog Trino sees — `aws_glue`, `bigquery_supernova_dev`, `iceberg`, `postgres_classifier`, `postgres_litellm`, `postgres_phoenix_prod`, `postgres_phoenix_stage`, `postgres_roofix`, `postgres_route_policy`, `postgres_sandbox`, `postgres_supabase`, `supabase_ai_agents`, `supabase_enerflo_leads`, `supabase_operations_assistant`, `system` |
| `list_schemas(catalog)` | Schemas under a catalog |
| `list_tables(catalog, schema)` | Tables under a schema |
| `describe_table(catalog, schema, table)` | `[{"name":…, "type":…}, …]` |
| `run_query(sql, max_rows?)` | SELECT only; clamped to `TRINO_MCP_MAX_ROWS` rows and `TRINO_MCP_MAX_RUNTIME_S` seconds |

The row cap is enforced twice by `common.trino.TrinoClient`: a `LIMIT` is
spliced into a `SELECT` / `WITH … SELECT` that has none (or a larger one),
and every statement is fetched with `fetchmany(cap)`, the rest cancelled.
`SHOW` / `DESCRIBE` / `EXPLAIN` — what the discovery tools send, and also
accepted by `run_query` — are never rewritten: Trino's grammar has no
`LIMIT` on them (`DESCRIBE t LIMIT 10` is a `SYNTAX_ERROR`), so the fetch
cap alone bounds them.

**SELECT-only and clamped.** `common.trino.TrinoClient` rejects every
statement except `SELECT` / `WITH` / `SHOW` / `DESCRIBE` / `EXPLAIN` unless
it is built with `allow_writes=True`, which only `ai/trino/bin/init_warehouse.py`
does. Rows are clamped to `TRINO_MCP_MAX_ROWS` (above); runtime is clamped to
`TRINO_MCP_MAX_RUNTIME_S` by forwarding it as
`session_properties.query_max_execution_time` on each `run_query`. The
coordinator's own `query.max-execution-time` in `ai/trino/config/config.properties`
is the belt-and-suspenders backup.

The MCP loop is the same two-step pattern documented in
[`ai/litellm/LITELLM_MCP.md`](../litellm/LITELLM_MCP.md) — LiteLLM does not execute the
tool call itself; the caller (OpenWebUI or a `curl` script) forwards the
tool_call, hits `trino-mcp`'s HTTP endpoint, and sends the result back.

### Using it from Claude Code

`trino-mcp` publishes no host port, so Claude Code reaches it through
LiteLLM's MCP gateway. The path segment is the `mcp_servers.trino` key in
`ai/litellm/litellm_config.yaml`:

```bash
claude mcp add --transport http -s user trino http://192.168.5.233:4001/mcp/trino \
  --header "Authorization: Bearer sk-your-master-key"
```

Then run `/mcp` in Claude Code — `trino` should show as connected with the
five tools above. A virtual key works instead of the master key only if it
is allowed the `trino` MCP server. Use `-s project` to write the entry to the
repo's `.mcp.json` instead, and reference the key as `${VAR}` rather than
committing it.

Everything in [§ Adding a catalog](#adding-a-catalog) applies here too:
there is no per-catalog access control, so a Claude Code session with this
server can read every catalog, `postgres_phoenix_prod` (production) and
`postgres_supabase`'s `auth.users` included. The SELECT-only rule and the
row / runtime clamps are the only limits. Full list of LiteLLM-hosted MCP
servers: [`ai/litellm/LITELLM_MCP.md`](../litellm/LITELLM_MCP.md#add-mcp-servers-to-claude-code).

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
  `postgres_sandbox`, `postgres_phoenix_prod`, `postgres_phoenix_stage`, `postgres_supabase`,
  `postgres_classifier`, `postgres_route_policy`);
  `supabase_<project>` for Supabase-hosted projects (`supabase_ai_agents`,
  `supabase_enerflo_leads`, `supabase_operations_assistant`); `aws_glue` for the Glue Data Catalog;
  `bigquery_<project>` for a BigQuery project (`bigquery_supernova_dev` is
  GCP project `supernova-dev-zeo`). Note the
  split that `postgres_supabase` sits on: it is the Supabase instance **we**
  run (`ai/supabase`), so it is named for the Postgres we operate, not for
  the vendor — `supabase_*` is reserved for projects hosted on
  supabase.com. Don't
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
| On `ai_shared` (any compose service that joins it) | Docker service DNS | `roofix-db:5432`, `classifier-db:5432`, `route-policy-db:5432` (read-only `trino_reader` roles from `ai/classifier/db-init/10-trino-reader.sh` / `ai/route-policy/db-init/10-trino-reader.sh`) |
| On `ai_shared` via a subsystem that dual-homes its DB on purpose | Docker service DNS | `supabase-db:5432` (see [ai/supabase/SUPABASE.md](../supabase/SUPABASE.md); only the `postgres` database is federated — `_supabase`, Supavisor's metadata DB, is not) |
| On an isolated Docker network (`litellm`'s `internal`, sandbox's `sandbox_state`) | `host.docker.internal:<host-published-port>` | `host.docker.internal:5440` |
| Off-box (Supabase, RDS, a partner DB) | Public hostname | `aws-1-us-east-1.pooler.supabase.com:5432` |

`host.docker.internal` works because the coordinator has
`extra_hosts: host.docker.internal:host-gateway` — Linux Docker does not
define it by default. Do **not** attach `trino-coordinator` to another
subsystem's internal network just to reach its DB; that breaches the
isolation those networks exist for (see `ai/sandbox/SANDBOX.md`). The
operator can sever a `host.docker.internal` read path by unpublishing
the port in `.env`.

### Hosted Postgres gotchas

Learned the hard way on `postgres_phoenix_prod`, `supabase_ai_agents`, and
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

<a id="bigquery"></a>
**BigQuery** (see `bigquery_supernova_dev.properties`):

```properties
connector.name=bigquery
bigquery.project-id=${ENV:X_BQ_PROJECT_ID}
bigquery.credentials-key=${ENV:X_BQ_CREDENTIALS_KEY}
bigquery.case-insensitive-name-matching=true
```

- **The key goes in `.env`, not a mount.** `bigquery.credentials-key` takes
  the service account's JSON key file base64-encoded onto one line
  (`base64 -w0 key.json`), so no volume is needed. Delete the downloaded
  JSON once it is in the box's `.env`.
- **Roles.** BigQuery Data Viewer on the datasets (metadata + data) and
  BigQuery Read Session User on the billing project: table scans go through
  the Storage Read API, billed to `bigquery.parent-project-id` (default: the
  key's own project). A `PERMISSION_DENIED … readsessions.create` means the
  second role is missing.
- **Views are off** (connector default). Reading a view makes Trino
  materialize it into a temporary table, which needs `bigquery.tables.create`
  — more than a reader key has. Turn on `bigquery.views-enabled=true` only
  with a key that may write to a scratch dataset.
- **Arrow.** The connector reads Storage API pages as Arrow, which needs
  `--add-opens=java.base/java.nio=ALL-UNNAMED` in `config/jvm.config`
  (present); without it scans fail with an `InaccessibleObjectException`.
- **Datasets are schemas**, so a dataset in another project is not visible
  here — it needs a second catalog with that `bigquery.project-id`.

Other first-party connectors (Snowflake, Redshift, ClickHouse, MongoDB,
Kafka, Delta Lake, …) follow the same shape — `connector.name` from the
[Trino connector index](https://trino.io/docs/current/connector.html), then
per-connector keys. Anything that needs a credentials *file* also needs a
read-only volume mount on the coordinator.

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

Superset runs `AUTH_TYPE = AUTH_REMOTE_USER` (`superset/superset_config.py`)
and is served from the root of `SUPERSET_PUBLIC_HOST`
(`superset.zeoenergy.com`) by its own oauth2-proxy instance,
`oauth2-proxy-superset`
([OAUTH2_PROXY.md § Superset on its own hostname](../oauth2-proxy/OAUTH2_PROXY.md#superset-on-its-own-hostname)):
4.0.2 cannot run under a path prefix, so it is no longer at
`chat.zeoenergy.com/superset/`. It trusts `X-Forwarded-Email`
(`SUPERSET_REMOTE_USER_HEADER`) as the session user with no separate password
check; the first visit creates the account (username = the e-mail, role
`Alpha` — starting as Admin and changing roles:
[§ Superset sign-in and roles](#superset-sign-in-and-roles)).

**How the header becomes a login.** Flask-AppBuilder 4.4.1, the version
`apache/superset:4.0.2` pins, never reads request headers for
`AUTH_REMOTE_USER`: its `/login/` view (`AuthRemoteUserView.login` in
`flask_appbuilder/security/views.py`) logs in
`request.environ[AUTH_REMOTE_USER_ENV_VAR]`, default `REMOTE_USER`, which
nothing behind a proxy sets. So `superset_config.py` sets
`AUTH_REMOTE_USER_ENV_VAR` to the WSGI environ key of the header,
`HTTP_X_FORWARDED_EMAIL`. Superset sends a visitor without a session to
`/login/?next=…` itself, and the view returns them to the page. (The config
used to override a `get_user_identifier` method FAB 4.4.1 does not have, and
named `X-Auth-Request-Email`, which oauth2-proxy only ever puts on responses:
the header never signed anyone in.)

**This is only safe when nothing but `oauth2-proxy-superset` can hand Superset
that header:**

- `oauth2-proxy-superset` deletes every client copy of `X-Forwarded-Email` —
  any spelling, `X_Forwarded_Email` included — before it sets the real one
  (`PASS_USER_HEADERS`). It does **not** delete a client's own
  `X-Auth-Request-*`, which is why that family must never be trusted.
- `superset` is swapped side by side by `make up` (see
  [ai/edge/EDGE.md](../edge/EDGE.md)), so `PORT_SUPERSET` (default 8016) is
  published by the `ai/edge` HAProxy rather than by `docker-compose.trino.yml`.
  `oauth2-proxy-superset` reaches `superset:8088` directly over `ai_shared`,
  never through the edge, so the edge's `listen superset` deletes
  `X-Forwarded-Email` / `-User` and `X-Auth-Request-Email` / `-User` /
  `-Preferred-Username`, plus every header whose name contains an underscore:
  gunicorn 21.2.0 (Superset 4.0.2's) files `X_Forwarded_Email` under the same
  environ key as `X-Forwarded-Email`, and an exact-name delete would let that
  spelling through. A LAN client can neither spoof the header nor sign in on
  8016.
- Any container on `ai_shared` can still call `superset:8088` with any header —
  the same posture as Trino's username-only `:8080`
  ([§ JDBC authentication](#jdbc-authentication)).

Keep it that way:

- never route `oauth2-proxy-superset` (or anything that sets the header
  legitimately) through the edge;
- if `SUPERSET_REMOTE_USER_HEADER` changes, it must be a request header
  oauth2-proxy sets (`X-Forwarded-*`, never `X-Auth-Request-*`), and the edge
  needs a `del-header` for the new name in `ai/edge/haproxy.cfg`;
- before exposing this host on an untrusted network, still bind the edge's
  `PORT_SUPERSET` mapping to loopback in `ai/edge/docker-compose.edge.yml`
  (`"127.0.0.1:${PORT_SUPERSET:-8016}:8016"`), or drop it and reach Superset
  only via `https://superset.zeoenergy.com/`.

Superset's own session outlives a change of Google account in the same
browser: the old Superset user stays signed in until you log out of Superset
(top-right menu). Revocation is unaffected — `oauth2-proxy-superset` refuses
every request from someone removed from the group at the next cookie refresh.

**Behind the proxy.** `PROXY_FIX_CONFIG` counts the proxies that WRITE each
`X-Forwarded-*` header on the public path (Cloudflare → cloudflared →
`oauth2-proxy-superset` → superset): `x_for` 2 (Cloudflare sets the client,
oauth2-proxy appends cloudflared), `x_proto` 1 (only Cloudflare writes it; at
2 ProxyFix ignored the one value and Superset built `http://` URLs, its
post-login redirect among them), and `x_host` / `x_port` / `x_prefix` 0
(nobody sets them; both proxies keep the `Host` header). Served from the root,
so no `APPLICATION_ROOT` or `STATIC_ASSETS_PREFIX`.

Open WebUI is the other service that trusts `X-Forwarded-Email`; it also
verifies it with Google using the forwarded access token, and binds
`127.0.0.1:8007` only —
[ai/openwebui/OPENWEBUI.md § Single sign-on](../openwebui/OPENWEBUI.md#single-sign-on).

## Superset sign-in and roles

**Signing in.** Open `https://superset.zeoenergy.com/` and sign in with Google
on the usual Zeo AI sign-in page (the same `zeoai.access@zeoenergy.com` group
gate as chat). There is no Superset password and no Superset login form: the
first visit creates your account from the e-mail `oauth2-proxy-superset`
passes (username = the e-mail), and every later visit signs you straight in.

**What the first visit gives you: `Alpha`** (`AUTH_USER_REGISTRATION_ROLE` in
`superset/superset_config.py`). In Superset 4.0.2 that means:

| | `Alpha` | `Admin` |
|---|---|---|
| Dashboards, charts, datasets (view, explore, build your own) | yes | yes |
| Edit the provisioned SGR charts / dashboard in place (they have no owner) | no — "Save as" a copy | yes |
| SQL Lab | no — admin-only (`ADMIN_ONLY_VIEW_MENUS` in `superset/security/manager.py`) | yes |
| Users, roles, database connections | no | yes |

The bootstrap `admin` account (`SUPERSET_ADMIN_PASSWORD`, created by
`superset/bootstrap.sh`) cannot be used from a browser: under
`AUTH_REMOTE_USER` the identity is always the e-mail the proxy passes, and
there is no password form to sign in as anyone else.

**Starting as `Admin` — before your first visit.** Your sign-in finds an
existing account by username and keeps its roles, so pre-create the account
as an Admin once the database is set up (after the first `make up trino
superset`) and before you open Superset. On the box, from the repo root:

```bash
make exec trino superset CMD="superset fab create-admin \
  --username you@zeoenergy.com --firstname First --lastname Last \
  --email you@zeoenergy.com --password $(openssl rand -hex 16)"
```

The username must be the exact e-mail you sign in with. The password is
required by the command and never used. If the account already exists the
command stops with `Error! User already exists` and changes nothing — use the
next step instead.

**Making an existing account `Admin`.** Flask-AppBuilder's CLI has no
change-role command (`create-admin` / `create-user` refuse an existing
username), so the first Admin is a row in `superset-db`:

```bash
docker exec -it superset-db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

```sql
INSERT INTO ab_user_role (user_id, role_id)
SELECT u.id, r.id FROM ab_user u, ab_role r
WHERE u.username = 'you@zeoenergy.com' AND r.name = 'Admin'
ON CONFLICT DO NOTHING;   -- (user_id, role_id) is unique: safe to re-run
```

Then log out of Superset (top-right menu) and visit again so the session picks
up the role. Once one Admin exists, manage everyone else in the UI:
**Settings → List Users → edit** — e.g. add the built-in `sql_lab` role to give
an `Alpha` user SQL Lab without making them Admin.

**Removing access.** Take the person out of the Google group: the proxy refuses
them at the next cookie refresh, whatever their Superset role. Their Superset
account stays (inactive users can be set in **List Users**).

## Counting LiteLLM SGRs

LiteLLM Enterprise licensing meters **Successful Gateway Requests (SGRs)**:
every call LiteLLM logs with a success status, counted once per request; a
missing status counts as a success. Superset's **LiteLLM SGRs** dashboard
(`https://superset.zeoenergy.com/superset/dashboard/litellm-sgrs/`) counts them
per month from `LiteLLM_SpendLogs` (`postgres_litellm.public.litellm_spendlogs`,
one row per call since June 2026):

- **LiteLLM SGRs per month** — bars stacked by bucket, the month's total on
  top of each.
- **LiteLLM SGRs by month and bucket** — a pivot table (months × buckets, with
  totals) holding the exact numbers to copy into an email. Set its time grain
  to quarter or year in Explore for those totals.

What is counted (the SQL's header comment says the same):

- **Success** = `coalesce(nullif(status, ''), 'success') <> 'failure'`, one
  row per `request_id` (the table's primary key).
- **UTC months.** `starttime` is stored in UTC, so `date_trunc('month', …)`
  cuts at 00:00 UTC on the 1st; a US-local month boundary moves a few hours of
  traffic between months.
- **No JSON parsing** (`metadata` etc.): Trino timed out on JSON functions at
  this volume (~1M rows a month). `target` is the pass-through's `host:port`
  from `api_base`, else `model_group`.

| bucket | Rows |
|---|---|
| `model` | every other non-empty `call_type` (`acompletion`, `aresponses`, `anthropic_messages`, `aspeech`, …) |
| `mcp` | `call_mcp_tool`, `list_mcp_tools` |
| `a2a` | `asend_message`, `send_message` (LiteLLM 1.95.0's A2A call types) |
| `pass-through: job status` | `pass_through_endpoint` to `…/jobs/<id>` or `…/jobs/<id>/progress`, any query string: the `GET` status polls **and the one `DELETE /jobs/{id}` per job** — the method is not logged |
| `pass-through: other` | every other pass-through call, `…/jobs/<id>/artifacts/…` downloads included |
| `other` | empty `call_type` (so far only failures, so none) |

For scale, October 2026: 867,660 SGRs — `pass-through: job status` 650,896
(41,038 classifier jobs), `pass-through: other` 208,832 (165,283 of them
classifier artifact downloads), `model` 6,846, `mcp` 1,086.

### How it is provisioned

The image's `CMD` runs `superset/bootstrap.sh` before gunicorn on every start
(apache/superset's own `CMD` only starts gunicorn), and Superset starts
whether or not it succeeds. After registering the `trino` connection it runs
`superset/provision_sgr.py`:

| Object | Source | On every start |
|---|---|---|
| Virtual dataset `litellm_sgr_monthly` on `trino` | SQL: `superset/assets/litellm_sgr_monthly.sql`; columns: `COLUMNS` in `superset/sgr_spec.py` | SQL and columns re-synced from the repo; edits made in the UI are overwritten |
| Charts **LiteLLM SGRs per month** / **by month and bucket**, dashboard **LiteLLM SGRs** | `superset/sgr_spec.py`, fixed UUIDs | created only when missing, never updated, so edits made in the UI survive |

The provisioner never fails the boot: each step commits or rolls back on its
own and logs, under `[provision_sgr]` (`make logs trino superset`). It does
not run the SQL; the columns' types are the ones Superset records when it
syncs them from Trino. To get a shipped chart or the dashboard back, delete
it in the UI and restart.

### Changing it

1. Edit the SQL — one `SELECT`, no `;`, `%` or Jinja braces — and, if its
   columns or buckets change, `COLUMNS` / `BUCKETS` / `BUCKET_COLORS` in
   `superset/sgr_spec.py`.
2. Run it through the Trino MCP's `run_query`, then
   `UV_LINK_MODE=copy uv run --no-sync python -m pytest unit-tests/trino -q -p no:cacheprovider`
   (columns, buckets, chart params and the image wiring in step).
3. `make build trino superset && make up trino superset` (swap: the new copy
   provisions before it turns healthy).

A chart-param or layout change in `sgr_spec.py` only reaches an object that
does not exist yet: delete that chart or the dashboard in the UI first.

## Object store: RustFS

The warehouse bucket lives on `lake-s3`, a single-node
[RustFS](https://github.com/rustfs/rustfs) (Apache-2.0, S3-compatible).
It replaced MinIO in October 2026: MinIO deleted `minio/minio` and
`minio/mc` from Docker Hub and quay.io stopped serving them anonymously,
so the stack only ran while the old images stayed cached on the box. The
service is named for its role, not the product, so the next swap is an
image change rather than a rename.

- **Image:** `ghcr.io/rustfs/rustfs:1.0.1` — GHCR, not Docker Hub (the same
  build is also on Quay and Docker Hub), pinned. Never `latest`. The
  bucket is created by the one-shot `lake-s3-init`
  (`public.ecr.aws/aws-cli/aws-cli:2.37.12`, `head-bucket || create-bucket`),
  so nothing in this stack pulls from Docker Hub for object storage.
- **Credentials:** `LAKE_S3_ACCESS_KEY` / `LAKE_S3_SECRET_KEY` in `.env`
  (dev defaults `trino` / `trino-dev-only`) become `RUSTFS_ACCESS_KEY` /
  `RUSTFS_SECRET_KEY` on lake-s3, `${ENV:…}` in `catalogs/iceberg.properties`
  and `${env.…}` in `hive-metastore/metastore-site.xml` (bind-mounted
  read-only, so no rebuild is needed). The endpoint `http://lake-s3:9000` is
  fixed in the compose file, not `.env`. Rotating the pair is edit `.env` +
  `make up trino` — every consumer reads them from env at container start.
- **Path-style only.** `RUSTFS_SERVER_DOMAINS` is deliberately unset —
  setting it turns on virtual-host bucket routing, and Trino
  (`s3.path-style-access=true`) and HMS (`fs.s3a.path.style.access=true`)
  both address `http://lake-s3:9000/warehouse/…`.
- **Console:** RustFS serves its UI at `/rustfs/console/` and its admin API
  at `/rustfs/admin/v3`, both on port 9001, and does not support moving
  them under another prefix (rustfs#1427). So oauth2-proxy routes the
  `/rustfs/` prefix (`http://lake-s3:9001/rustfs/` in
  `OAUTH2_PROXY_UPSTREAMS`) and the console is at
  `https://chat.zeoenergy.com/rustfs/console/`; log in with the
  `LAKE_S3_*` pair. If it misbehaves behind the proxy (rustfs#966: its
  config can advertise the internal port), use the LAN console at
  `http://<host>:8015/rustfs/console/`. There is no redirect-URL setting
  like MinIO's `MINIO_BROWSER_REDIRECT_URL`.
- **Scanner off.** `RUSTFS_SCANNER_ENABLED=false`: the background data
  scanner does nothing useful on one disk with no lifecycle rules, and on
  single-disk deployments it is behind open issues #8015 / #8121 / #8377
  (latency spikes, a memory ratchet, constant background reads).
- **Logs go to stdout.** The image sets `RUSTFS_OBS_LOG_DIRECTORY=/logs`
  (rolling files inside the container); the compose file sets it to `""`,
  which both the entrypoint and the binary treat as stdout-only, so
  `docker logs lake-s3` works.
- **Health:** `GET /health/ready` on 9000. First start can take up to ~2
  minutes, which the healthcheck's retry budget allows for.
- **No metrics scrape.** RustFS exports OTLP only (no Prometheus endpoint);
  MinIO was not scraped either.

### Iceberg warehouse paths: `s3a://`, not `s3://`

The Hive Metastore's `hive.metastore.warehouse.dir` points at
`s3a://warehouse/`, and every `CREATE SCHEMA` should carry an explicit
`WITH (location = 's3a://warehouse/<schema>')`. That matches HMS's default
filesystem and avoids the scheme mismatch that would follow if Trino wrote
through its native S3 connector while HMS validated with s3a.
`ai/trino/bin/init_warehouse.py` follows this convention.

### Cutover from MinIO (one-time, on the box)

The new store starts **empty** — the only thing on MinIO was the
`init_warehouse.py` demo table, which is re-seeded rather than copied.

1. **While MinIO still runs**, drop the demo objects so HMS holds no rows
   pointing at files that are about to vanish:

   ```bash
   docker exec trino-coordinator trino --execute \
       "DROP TABLE IF EXISTS iceberg.demo.events; DROP SCHEMA IF EXISTS iceberg.demo"
   ```

   (The CLI's `--execute` runs each `;`-separated statement in turn.)
2. Pull the change. In the box's `.env`: rename `MINIO_ROOT_USER` →
   `LAKE_S3_ACCESS_KEY`, `MINIO_ROOT_PASSWORD` → `LAKE_S3_SECRET_KEY`,
   `PORT_MINIO_API` → `PORT_LAKE_S3_API`, `PORT_MINIO_CONSOLE` →
   `PORT_LAKE_S3_CONSOLE`; delete `MINIO_BROWSER_REDIRECT_URL`; in
   `OAUTH2_PROXY_UPSTREAMS` replace `http://minio:9001/minio/` with
   `http://lake-s3:9001/rustfs/`.
3. Remove the old containers (`make up` does not remove orphans), then
   bring the stack up — this pulls RustFS from GHCR and the AWS CLI from
   ECR Public, creates the bucket, and recreates HMS + the coordinator
   with the new env. No image builds are needed (`metastore-site.xml` is
   bind-mounted over the copy baked into the HMS image):

   ```bash
   docker rm -f minio minio-init
   make up trino
   ```
4. `make up oauth2-proxy` — its env changed, so it needs a recreate.
5. Re-seed the demo table:

   ```bash
   docker compose -f ai/trino/docker-compose.trino.yml --env-file .env -p ai-trino \
       exec -T trino-mcp uv run --no-sync python - < ai/trino/bin/init_warehouse.py
   ```

   (Run from the repo root. The script is piped in because the `trino-mcp`
   image only contains `server.py`.)
6. Once verified, drop what is left of MinIO:

   ```bash
   docker volume rm ai-trino_minio_data
   docker image rm minio/minio:latest minio/mc:latest
   ```

**If MinIO is already gone** before step 1, the `DROP TABLE` cannot read
the table's metadata. Do steps 2–4 first, then — before step 5 — remove
the table from HMS without touching its files, and drop the schema
(HMS's s3a client now reaches the empty lake-s3 instead of a dead host):

```bash
docker exec trino-coordinator trino --execute \
    "CALL iceberg.system.unregister_table('demo', 'events'); DROP SCHEMA IF EXISTS iceberg.demo"
```

Skipping this leaves `iceberg.demo.events` registered against missing
metadata, and `init_warehouse.py`'s `CREATE TABLE IF NOT EXISTS` then
fails on the stale entry.

**Verify:** `docker logs lake-s3-init` ends with `warehouse bucket ready`;
`lake-s3` is healthy; `docker logs hive-metastore` is clean;
`SELECT count(*) FROM iceberg.demo.events` returns 3; JDBC on 8013 is
unaffected; the console lists the `warehouse` bucket (check
`cf-cache-status` on its assets — Cloudflare caches static paths on the
chat host).

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
| `lake_s3_data` | The actual Parquet files under `warehouse/` (lake-s3's `/data`) | Only after export |
| `superset_db_data` | Superset dashboards, saved queries, user accounts | Only if you want to start over |
| `trino_auth` | TLS keystore + `password.db` for the coordinator | Anytime — regenerates on next `up`; BI clients re-trust the new cert |

## Follow-ups

- **Real TLS cert for `PORT_TRINO`** — the coordinator serves a
  self-signed cert from `trino-auth-init`, so clients need
  `SSLVerification=NONE` or a pinned `trino.crt`. Replace `tls/trino.pem`
  in the `trino_auth` volume with a CA-issued key+cert PEM when there is
  a stable hostname for the box.
- **Per-catalog access control** — every `TRINO_JDBC_USERS` login and
  `trino-mcp` can read every catalog, including Phoenix production / staging and
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
