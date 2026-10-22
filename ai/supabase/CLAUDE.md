# ai/supabase

A full self-hosted Supabase (Postgres 17, Envoy gateway, GoTrue, PostgREST, Realtime, Storage + imgproxy, postgres-meta, Studio, Edge Functions, Supavisor). It is the **service**, not an application: this repo creates no application tables in it. Guide: [`SUPABASE.md`](SUPABASE.md). Compose: `docker-compose.supabase.yml`.

## Rules

- **Every root `.env` var is `SUPABASE_`-prefixed** and mapped onto upstream's unprefixed names inside the compose file (upstream's `POSTGRES_*`, `JWT_SECRET`, `ANON_KEY`, … would collide in the shared `.env`). The compose file is a pure `${…}` template — edit `.env`, never the YAML.
- **Generate secrets with `python ai/supabase/bin/generate_keys.py --write-env .env`** (fills only empty `SUPABASE_*` lines; `--force` rotates; `--verify` self-checks). **Never** upstream's `utils/generate-keys.sh` — it `sed`s unprefixed names into `./.env` and clobbers other stacks. `SUPABASE.md § First-run checklist`.
- **Upstream service KEYS stay verbatim** (`auth`, `rest`, `storage`, `functions`, `meta`, `studio`, …): the vendored `volumes/api/envoy/cds.yaml` addresses them by name, so a rename 503s that route. Realtime needs the network alias `realtime-dev.supabase-realtime` on `supabase_net`. `SUPABASE.md § What runs`.
- **Four secrets are one-shot:** `SUPABASE_POSTGRES_PASSWORD`, `SUPABASE_JWT_SECRET`, `SUPABASE_VAULT_ENC_KEY`, `SUPABASE_REALTIME_DB_ENC_KEY` are baked (encrypted) into Supavisor / Realtime tenant rows on first boot; rotating them later needs those rows re-seeded by hand — documented, deliberately not automated. Everything else rotates with a recreate. `SUPABASE.md § Secrets that are one-shot`.
- **Legacy HS256 key mode.** `SUPABASE_PUBLISHABLE_KEY`, `_SECRET_KEY`, `_ANON_KEY_ASYMMETRIC`, `_SERVICE_ROLE_KEY_ASYMMETRIC`, `_JWT_KEYS`, `_JWT_JWKS` ship **empty on purpose**. A value in `SUPABASE_JWT_JWKS` flips PostgREST to asymmetric verification and every legacy key stops working. `SUPABASE.md § Asymmetric keys (follow-up)`.
- **`trino_reader` is the only DB object this repo creates** (`volumes/db/trino-reader.sql`, first init; idempotent — re-run with psql to retrofit or rotate). `BYPASSRLS` is load-bearing: without it every RLS-enabled table reads as zero rows in Trino, with no error.
- **`volumes/` is vendored upstream config and must stay LF** — a `\r` in the psql `\set` trick bakes a carriage return into role passwords. Named volumes replace upstream's bind mounts; `:z` / `:Z` dropped. `SUPABASE.md § Vendored config`.
- **Pooler login is `postgres.<SUPABASE_POOLER_TENANT_ID>`** (`postgres.zeo-local`) — the tenant id is part of the username; plain `postgres` fails with an unhelpful message. JDBC wants session mode (`8023`), not transaction mode.
- **`SUPABASE_PUBLIC_URL` is the exact browser origin, no trailing slash** (Envoy CORS exact match); its port agrees with `PORT_SUPABASE_API`.
- **LAN-facing only** — no oauth2-proxy / tunnel in front; gates are Envoy basic auth on Studio and the API keys. Public exposure is a follow-up, not an oversight.
- **Containers:** `supabase-db`, `supabase-pooler` (key `supavisor`) and `supabase-realtime` keep fixed names (recreate-class: Postgres, migrations + TCP ports, replication slot); the rest are `supabase-<svc>-<hash>` — use `make exec|logs supabase <svc>`.

## Cross-folder rules

Also in the root `CLAUDE.md` § Cross-folder rules — read the named entry there for the full picture.

- **Trino reads other stacks' Postgres** — `supabase-db` is dual-homed onto `ai_shared` on purpose so `trino-coordinator` reaches `supabase-db:5432` by service DNS (catalog `postgres_supabase`, only the `postgres` database; `_supabase` is not federated). Anything on `ai_shared` can open a TCP connection to it, so authentication is the only gate. `trino_reader` = `pg_read_all_data` + `BYPASSRLS` + read-only; it can read `auth.users` and `vault.secrets`. A reader change needs `make up trino trino-coordinator`. `SUPABASE.md § Trino federation`. (root `CLAUDE.md` § Cross-folder rules)
- **`docker.sock` only in `sandbox-runner`** — so upstream's `logs` override (Logflare + Vector, which mounts the socket) is not layered; Studio's Logs pages have no backend (`ENABLED_FEATURES_LOGS_ALL=false`). `volumes/db/logs.sql` is still mounted for parity. `SUPABASE.md § Excluded: analytics`. (root `CLAUDE.md` § Cross-folder rules)
- **Swap-class services publish through the edge** — `supabase-api` and seven data-plane services (`auth`, `rest`, `storage`, `imgproxy`, `meta`, `studio`, `functions`) swap; `supabase-api` has no `ports:` — `PORT_SUPABASE_API` is the edge's `listen supabase-api`, which also carries the Realtime websockets. (root `CLAUDE.md` § Deploys (`make up`))

## Env vars

One line each; full detail in `.env.example` (`## Supabase subsystem` block) and `SUPABASE.md`. Ports: root `CLAUDE.md` § Env vars and the PORT REGISTRY.

- `PORT_SUPABASE_API` (`8022`) — the Envoy gateway, the only LAN HTTP surface (`/auth/v1`, `/rest/v1`, `/realtime/v1`, `/storage/v1`, `/functions/v1`, Studio at `/`); published by the edge; must match `SUPABASE_PUBLIC_URL`'s origin.
- `PORT_SUPABASE_POOLER_SESSION` / `_TX` (`8023` / `8024`) — Supavisor session (`5432`) / transaction (`6543`) listeners. Upstream overloads `POSTGRES_PORT` as both the in-container port and the pooler's host port; this repo splits them, so `SUPABASE_POSTGRES_PORT` stays `5432`.
- `PORT_SUPABASE_DB` (`5438`) — direct psql / DBeaver to `supabase-db`, bypassing the pooler.
- `SUPABASE_POSTGRES_PASSWORD`, `_JWT_SECRET`, `_ANON_KEY`, `_SERVICE_ROLE_KEY`, `_DASHBOARD_PASSWORD`, `_SECRET_KEY_BASE`, `_REALTIME_DB_ENC_KEY`, `_VAULT_ENC_KEY`, `_PG_META_CRYPTO_KEY`, `_S3_PROTOCOL_ACCESS_KEY_ID` / `_SECRET`, `_TRINO_READER_PASSWORD` (required, `${VAR:?…}`) — from `generate_keys.py`. Lengths: `SECRET_KEY_BASE` ≥ 64, `REALTIME_DB_ENC_KEY` exactly 16, `VAULT_ENC_KEY` exactly 32, `PG_META_CRYPTO_KEY` ≥ 32, `JWT_SECRET` ≥ 32. Four are one-shot (above). Regenerate both API keys whenever `SUPABASE_JWT_SECRET` changes.
- `SUPABASE_TRINO_READER_USER` (`trino_reader`) — the read-only role for `ai/trino/catalogs/postgres_supabase.properties`, passed through `trino-coordinator`'s env; treat its password as production-grade.
- `SUPABASE_PUBLIC_URL` / `_API_EXTERNAL_URL` / `_SITE_URL` — exact origins; see Rules.
- `SUPABASE_POOLER_TENANT_ID` (`zeo-local`) — part of the pooler username.
