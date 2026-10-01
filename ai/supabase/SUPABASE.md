# Supabase (self-hosted)

A full Supabase instance running on the AI box as a first-class compose
subsystem: Postgres 17, an Envoy API gateway, GoTrue auth, PostgREST, Realtime,
Storage + imgproxy, postgres-meta, Studio, Edge Functions, and the Supavisor
connection pooler.

It is the **service**, not an application. This repo creates no application
tables in it — the only database object we add is a read-only `trino_reader`
role so the [Trino data lake](../trino/TRINO.md) can federate it.

Compose file: [`docker-compose.supabase.yml`](docker-compose.supabase.yml).
Postman collection: [`supabase.postman_collection.json`](supabase.postman_collection.json).

- [What runs](#what-runs)
- [First-run checklist](#first-run-checklist)
- [Access](#access)
- [Trino federation](#trino-federation)
- [Secrets that are one-shot](#secrets-that-are-one-shot)
- [Asymmetric keys (follow-up)](#asymmetric-keys-follow-up)
- [Excluded: analytics](#excluded-analytics)
- [Vendored config](#vendored-config)
- [Upgrading](#upgrading)
- [Verifying](#verifying)
- [Common gotchas](#common-gotchas)

## What runs

| Container | Service key | Purpose | Networks | Host port |
|---|---|---|---|---|
| `supabase-db` | `supabase-db` | Postgres 17 (`supabase/postgres`) | `supabase_net`, `ai_shared` | `5438` (direct psql) |
| `supabase-pooler` | `supavisor` | Supavisor connection pooler | `supabase_net`, `ai_shared` | `8023` session / `8024` transaction |
| `supabase-api` | `supabase-api` | Envoy API gateway — the one LAN-facing surface | `supabase_net`, `ai_shared` | `8022` |
| `supabase-auth` | `auth` | GoTrue — `/auth/v1/*` | `supabase_net` | — |
| `supabase-rest` | `rest` | PostgREST — `/rest/v1/*` | `supabase_net` | — |
| `supabase-realtime` | `realtime` | Realtime — `/realtime/v1/*` | `supabase_net` | — |
| `supabase-storage` | `storage` | Storage API — `/storage/v1/*` | `supabase_net` | — |
| `supabase-imgproxy` | `imgproxy` | Image transforms for Storage | `supabase_net` | — |
| `supabase-meta` | `meta` | postgres-meta — the schema API Studio drives | `supabase_net` | — |
| `supabase-studio` | `studio` | The dashboard, behind Envoy basic auth at `/` | `supabase_net` | — |
| `supabase-functions` | `functions` | Deno edge runtime — `/functions/v1/*` | `supabase_net` | — |

**`container_name` deliberately does not equal the service key** for the eight
data-plane services. The vendored Envoy cluster config
(`volumes/api/envoy/cds.yaml`) addresses them by upstream's short service names
(`auth`, `rest`, `realtime-dev.supabase-realtime`, `storage`, `functions`,
`meta`, `studio`), so the keys must stay verbatim; the container names follow
upstream's `supabase-<svc>` convention. Only the three that also join
`ai_shared` get a distinctive name for both.

`supabase-db` is dual-homed onto `ai_shared` on purpose, exactly like
`roofix-db`: it is how `trino-coordinator` reaches it by Docker DNS without
the coordinator having to join `supabase_net`. That does mean anything on
`ai_shared` can open a TCP connection to Postgres — authentication is the only
gate, so treat `SUPABASE_POSTGRES_PASSWORD` accordingly. (`open-terminal`, the
model-driven shell, is on `terminal_net` and is **not** on `ai_shared`.)

## First-run checklist

1. **Generate the secrets.** Every `(R)` variable in the
   `## Supabase subsystem` block of `.env` ships empty; compose refuses to
   start until they are filled.

   ```bash
   # writes only the EMPTY SUPABASE_* lines, in place, touching nothing else
   python ai/supabase/bin/generate_keys.py --write-env .env
   ```

   Or print a paste-ready block with no arguments. `--force` rotates values
   that are already set — read [Secrets that are one-shot](#secrets-that-are-one-shot)
   first. Do **not** use upstream's `utils/generate-keys.sh`: it `sed`s
   *unprefixed* names (`POSTGRES_PASSWORD=`, `MINIO_ROOT_PASSWORD=`) into
   `./.env`, and in this repo's shared `.env` that would clobber Trino's MinIO
   credentials.

2. **Set the URLs.** In the same block:
   `SUPABASE_PUBLIC_URL`, `SUPABASE_API_EXTERNAL_URL`, `SUPABASE_SITE_URL`.
   `SUPABASE_PUBLIC_URL` must be the **exact browser origin, no trailing
   slash** — Envoy's entrypoint `sed`s it into a CORS *exact match*.

3. **Bring it up.**

   ```bash
   make network && make up supabase
   ```

   First boot takes a couple of minutes: `supabase-db` runs ~60 image
   migrations plus the mounted init scripts, which is why its healthcheck has
   `start_period: 90s`.

4. **Wire up Trino** (optional, but it is why the `trino_reader` role exists):

   ```bash
   make up trino trino-coordinator   # recreate — the container env changed
   ```

## Access

Everything except direct Postgres goes through the gateway on
`${PORT_SUPABASE_API}` (default `8022`).

| What | Where |
|---|---|
| Studio (dashboard) | `http://<box>:8022/` — basic auth, `SUPABASE_DASHBOARD_USERNAME` / `_PASSWORD` |
| Auth | `http://<box>:8022/auth/v1/…` |
| REST (PostgREST) | `http://<box>:8022/rest/v1/…` — send `apikey: $SUPABASE_ANON_KEY` |
| Realtime | `ws://<box>:8022/realtime/v1/…` |
| Storage | `http://<box>:8022/storage/v1/…` |
| Edge Functions | `http://<box>:8022/functions/v1/hello` |
| Pooler — session mode | `postgres://postgres.zeo-local:<pw>@<box>:8023/postgres` |
| Pooler — transaction mode | `postgres://postgres.zeo-local:<pw>@<box>:8024/postgres` |
| Postgres — direct | `postgres://postgres:<pw>@<box>:5438/postgres` |

**The pooler tenant id is part of the username, not the host.** Supavisor
expects `postgres.<SUPABASE_POOLER_TENANT_ID>` — `postgres.zeo-local` with the
shipped default. Connecting as plain `postgres` to `:8023` fails
authentication with a message that does not explain why.

Session mode (`8023`) keeps one server connection per client and supports
prepared statements; transaction mode (`8024`) multiplexes and does not. JDBC
drivers generally want session mode — that is the same trap documented for the
hosted Supabase projects in [TRINO.md § Hosted Postgres gotchas](../trino/TRINO.md#hosted-postgres-gotchas).

This subsystem is **LAN-facing only**. There is no oauth2-proxy, Cloudflare
tunnel, or Google OAuth in front of it; the only gates are Envoy's basic auth
on Studio and the API keys on everything else. Public exposure via
`supabase.zeoenergy.com` is a deliberate follow-up, not an oversight.

## Trino federation

The catalog is [`postgres_supabase`](../trino/catalogs/postgres_supabase.properties)
— `postgres_<subsystem>` because this is a Postgres instance **we run**, as
opposed to the `supabase_ai_agents` / `supabase_enerflo_leads` catalogs, which
are projects hosted on supabase.com. See [TRINO.md § Naming](../trino/TRINO.md#naming).

Reads use a dedicated role created at first DB init by
[`volumes/db/trino-reader.sql`](volumes/db/trino-reader.sql):

- `pg_read_all_data` — read every table without per-table grants.
- `BYPASSRLS` — **load-bearing.** Without it every RLS-enabled table
  (`storage.objects` today, any future app table) reads as **zero rows** in
  Trino, with no error at all. Upstream's own `supabase_read_only_user` does
  the same thing.
- `default_transaction_read_only = on` and `statement_timeout = '300s'` —
  `trino-mcp` is SELECT-only, but a DBeaver login through the same catalog is
  not; the DB-side role is what actually stops a write.

Because `pg_read_all_data` covers `auth.users` and `vault.secrets`,
`SUPABASE_TRINO_READER_PASSWORD` is a production-grade credential.

Only the `postgres` database is federated. `_supabase` — the metadata database
Supavisor (and the analytics override) uses — is a separate database and is
deliberately not exposed.

The SQL is idempotent, so retrofitting an existing data volume or rotating the
password is a re-run:

```bash
docker compose -f ai/supabase/docker-compose.supabase.yml --env-file .env -p ai-supabase \
  exec supabase-db psql -v ON_ERROR_STOP=1 -U supabase_admin -d postgres \
  -f /docker-entrypoint-initdb.d/init-scripts/99-trino-reader.sql
```

## Secrets that are one-shot

Some values are written into the database, encrypted, on first boot. Changing
them in `.env` afterwards does **not** re-seed what is already stored, and the
failure looks like an authentication or decryption error far from the cause.

| Variable | Baked into | Effect of rotating after first boot |
|---|---|---|
| `SUPABASE_POSTGRES_PASSWORD` | Supavisor's tenant row (encrypted under `VAULT_ENC_KEY`), plus every role password set by `roles.sql` | Pooler can no longer log in to Postgres |
| `SUPABASE_VAULT_ENC_KEY` | The encryption of that tenant row | Supavisor cannot decrypt its own tenant config |
| `SUPABASE_JWT_SECRET` | Realtime's tenant `jwt_secret` (encrypted under `DB_ENC_KEY`) | Realtime rejects otherwise valid tokens; also invalidates `ANON_KEY` / `SERVICE_ROLE_KEY`, which are signed with it |
| `SUPABASE_REALTIME_DB_ENC_KEY` | The encryption of that Realtime tenant row | Realtime cannot decrypt its tenant's secret |

This is documented rather than automated on purpose — re-seeding is a
deliberate operator action. The two blunt options are: delete the affected
tenant row and let `SEED_SELF_HOST=true` / the Supavisor migration recreate it,
or `make very-clean supabase CONFIRM=yes` and start from an empty volume.
Rotating `SUPABASE_ANON_KEY` / `SUPABASE_SERVICE_ROLE_KEY` *together with*
`SUPABASE_JWT_SECRET` is fine on a fresh instance and is what
`generate_keys.py` does.

Everything else — `SUPABASE_DASHBOARD_PASSWORD`, the S3-protocol keys,
`SUPABASE_PG_META_CRYPTO_KEY`, `SUPABASE_TRINO_READER_PASSWORD` — is safe to
rotate with a recreate (`SUPABASE_TRINO_READER_PASSWORD` also needs the psql
re-run above).

## Asymmetric keys (follow-up)

This deployment runs in **legacy HS256 API key mode**: `SUPABASE_ANON_KEY` and
`SUPABASE_SERVICE_ROLE_KEY` are HS256 JWTs signed with `SUPABASE_JWT_SECRET`.
The newer ES256 / opaque `sb_publishable_…` key pair is optional — Envoy's
entrypoint logs `Envoy running in legacy API key mode (sb_ keys disabled)` and
carries on — so these six variables ship **empty on purpose**:

`SUPABASE_PUBLISHABLE_KEY`, `SUPABASE_SECRET_KEY`,
`SUPABASE_ANON_KEY_ASYMMETRIC`, `SUPABASE_SERVICE_ROLE_KEY_ASYMMETRIC`,
`SUPABASE_JWT_KEYS`, `SUPABASE_JWT_JWKS`.

`SUPABASE_JWT_JWKS` is the sharp one. PostgREST reads
`${SUPABASE_JWT_JWKS:-${SUPABASE_JWT_SECRET}}` — that nested default is the
whole mechanism. Putting a value there switches PostgREST to asymmetric
verification and every legacy key stops working.

The migration path is upstream's `utils/add-new-auth-keys.sh`, plus
uncommenting the `GOTRUE_JWT_KEYS` / `API_JWT_JWKS` / `JWT_JWKS` /
`SUPABASE_JWKS` lines already present (commented) in the compose file.

## Excluded: analytics

Upstream ships Logflare + Vector as a **layered override**, not as part of the
base compose file. We do not add that override, because Vector needs
`/var/run/docker.sock` mounted, and in this repo `sandbox-runner` is the only
container permitted to mount the Docker socket — it is the audit boundary (see
[SANDBOX.md](../sandbox/SANDBOX.md)). Mounting it into a second service would
make that boundary decorative.

Consequences: Studio's Logs pages have no backend, which is why
`ENABLED_FEATURES_LOGS_ALL` is `false` (upstream already defaults it so). Use
`make logs supabase <service>` instead.

`volumes/db/logs.sql` **is** still mounted, for parity with upstream. It only
creates the `_analytics` schema inside the `_supabase` database, so adding the
override later needs no manual schema step.

## Vendored config

`volumes/` is a verbatim copy of `supabase/supabase@master`'s `docker/volumes/`,
tracked in this repo and mounted `:ro`, with two additions of our own:

| Path | Source |
|---|---|
| `api/envoy/{envoy.yaml,cds.yaml,lds.template.yaml,docker-entrypoint.sh}` | verbatim |
| `db/{_supabase,jwt,logs,pooler,realtime,roles,webhooks}.sql` | verbatim |
| `db/trino-reader.sql` | **ours** |
| `pooler/pooler.exs` | verbatim |
| `functions/deno.jsonc`, `functions/main/index.ts` | verbatim |
| `functions/hello/index.ts` | **ours** |

`functions/hello` is ours because upstream's sample imports `@supabase/server`
and authenticates with the new publishable / secret keys, which 401 in legacy
mode. A plain `Deno.serve` handler proves the runtime and the Envoy route
without depending on either key style.

**Everything here must stay LF.** The Postgres init scripts use psql's
`` \set x `echo "$VAR"` `` trick; a trailing `\r` bakes a carriage return into
the value, producing (for example) a role password that silently does not
match. `.gitattributes` forces `eol=lf` on `*.sql`, `*.exs`, `*.ts`, `*.jsonc`,
`*.yaml` and `*.sh` for exactly this reason.

Named volumes are used throughout instead of upstream's `./volumes/db/data`
and `./volumes/storage` bind mounts (repo convention — and a bind mount there
would need new ignore rules). Upstream's `:z` / `:Z` SELinux flags are dropped.

## Upgrading

1. Diff `docker/docker-compose.yml` on `supabase/supabase@master` against
   this compose file and re-pin every `image:` line (each carries a
   "Pinned release tag" comment).
2. Re-fetch `docker/volumes/**` and diff against `volumes/` — an upstream
   change to `cds.yaml` or `lds.template.yaml` can change routing or the env
   the entrypoint expects.
3. Re-check `docker/.env.example` for new or renamed variables, and add them
   to the `## Supabase subsystem` block of `.env` **and** `.env.example` with
   the `SUPABASE_` prefix.
4. `make build supabase && make up supabase`.

Note that upstream's base compose has no `analytics` / `vector` services any
more (they moved to overrides), and that `db/data` is a bind mount upstream but
a named volume here — both are expected diffs, not drift.

## Verifying

On the box:

```bash
make network && make up supabase
make logs supabase supabase-db     # expect 99-trino-reader.sql to run once
make logs supabase supabase-api    # expect "legacy API key mode"

curl -s http://localhost:8022/auth/v1/health
curl -s -H "apikey: $SUPABASE_ANON_KEY" http://localhost:8022/rest/v1/
curl -s http://localhost:8022/storage/v1/status
curl -s http://localhost:8022/functions/v1/hello

# Trino federation
make up trino trino-coordinator
docker exec -it trino-coordinator trino --execute "SHOW SCHEMAS FROM postgres_supabase"
```

Expected: schemas `public`, `auth`, `storage`, `realtime`, `_realtime`,
`extensions`, `vault`, … are listed, and
`SHOW TABLES FROM postgres_supabase.public` is empty — this instance holds no
application tables.

The [Postman collection](supabase.postman_collection.json) carries the same
requests with the gateway base URL and keys as collection variables.

## Common gotchas

| Gotcha | Effect |
|---|---|
| CRLF anywhere under `volumes/` | psql `\set` bakes a `\r` into role passwords; auth fails with no useful message |
| `SUPABASE_PUBLIC_URL` with a trailing slash, or not the literal browser origin | Envoy's CORS *exact* match never fires; Studio fails in the browser, logs look clean |
| Connecting to the pooler as plain `postgres` | Supavisor wants `postgres.<POOLER_TENANT_ID>`; auth failure does not say so |
| Setting `POSTGRES_USER` on `supabase-db` | The image defaults it to `supabase_admin` and the vendored SQL reads `$POSTGRES_USER` for schema ownership |
| Rotating `SUPABASE_JWT_SECRET` without regenerating both API keys | `ANON_KEY` / `SERVICE_ROLE_KEY` are signed with it — every request 401s |
| Putting a value in `SUPABASE_JWT_JWKS` | Flips PostgREST to asymmetric verification; legacy keys stop working |
| Renaming a data-plane service key | `volumes/api/envoy/cds.yaml` addresses them by name — the gateway 503s that route |
| Deleting `supabase_db_config` but keeping `supabase_db_data` | Loses the pgsodium decryption key the image seeded; encrypted columns become unreadable |
| Editing the compose file instead of `.env` | The compose file is a pure `${SUPABASE_*}` template by design |
