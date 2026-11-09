# ai/trino

Trino coordinator + Hive Metastore (+ Postgres) + `lake-s3` (RustFS S3 store) + Superset (+ Postgres) + `trino-mcp` (FastMCP shim registered with LiteLLM). Federates SQL over the Postgres instances this repo runs, off-box sources, and an Iceberg lakehouse. Guide: [`TRINO.md`](TRINO.md). Compose: `docker-compose.trino.yml`.

## Rules

- **A catalog is a `.properties` file in `catalogs/`, never a compose service.** Secrets as `${ENV:VAR}`, each VAR also in `trino-coordinator`'s `environment:` and in `.env` + `.env.example`; then `make up trino trino-coordinator` (recreate — the container env changed; `restart` only for a `.properties` edit with no env change). Hosted Postgres needs `?sslmode=require`; supabase.com projects use the session-pooler host (the direct host is IPv6-only); a `$` in `.env` is `$$`; underscores only in catalog names (`postgres_<stack>` for ours, `supabase_<project>` for supabase.com). Also update the `list_catalogs()` row in `TRINO.md § MCP tools` and the docstring in `mcp/server.py`. `TRINO.md § Adding a catalog`.
- **Never publish the coordinator's `:8080`.** It is plain HTTP, username-only (`allow-insecure-over-http=true`), used by `trino-mcp`, Superset, `init_warehouse.py` and the healthcheck by Docker DNS. `PORT_TRINO` maps the HTTPS `:8443` listener (password file from `trino-auth-init` in the `trino_auth` volume, self-signed cert). `TRINO.md § JDBC authentication`.
- **`trino-mcp` is SELECT-only.** `common.trino.TrinoClient` refuses all but `SELECT` / `WITH` / `SHOW` / `DESCRIBE` / `EXPLAIN` unless `allow_writes=True` — only `bin/init_warehouse.py` sets it. There is no per-catalog access control: every login and `trino-mcp` read every catalog (Phoenix production and staging, `auth.users` included).
- **Iceberg on RustFS uses `s3a://warehouse/`, not `s3://`,** and every `CREATE SCHEMA` carries `WITH (location = 's3a://warehouse/<schema>')`. `TRINO.md § Iceberg warehouse paths: s3a://, not s3://`.
- **Don't attach `trino-coordinator` to another stack's internal network** to reach its DB — use `host.docker.internal:<published port>` (`extra_hosts: host-gateway`). `TRINO.md § Reaching the database`.
- **Networks:** `analytics_net` (data plane, plain bridge on purpose) + `ai_shared` (only coordinator, `lake-s3`, `superset`, `trino-mcp`). `TRINO.md § Networks`.
- **RustFS** (`ghcr.io/rustfs/rustfs:1.0.1`, pinned, never `latest`): path-style only (`RUSTFS_SERVER_DOMAINS` stays unset); its console / admin API live under `/rustfs/` and cannot be moved. `TRINO.md § Object store: RustFS`.
- **Never edit HMS's tables by hand** (`hive-metastore-db`); the Iceberg connector relies on its invariants.
- **Superset is served from the root of its own hostname**, `https://superset.zeoenergy.com/` (`SUPERSET_PUBLIC_HOST`, behind `oauth2-proxy-superset` in `ai/oauth2-proxy`): 4.0.2 cannot run under a path prefix, so never add `APPLICATION_ROOT` / `STATIC_ASSETS_PREFIX` or a `/superset/` upstream on chat. Its sign-in is Flask-AppBuilder 4.4.1's `AUTH_REMOTE_USER`, which reads only `request.environ[AUTH_REMOTE_USER_ENV_VAR]`; `superset_config.py` derives that key from `SUPERSET_REMOTE_USER_HEADER` (`X-Forwarded-Email` → `HTTP_X_FORWARDED_EMAIL`). `PROXY_FIX_CONFIG` counts the writers of each `X-Forwarded-*` header on the public path: `x_for` 2, `x_proto` 1, the rest 0. `superset_config.py` is baked into the image: `make build trino superset && make up trino superset`. Tests: `unit-tests/trino/test_superset_sign_in.py`. `TRINO.md § Header-auth trust boundary`.
- **The LiteLLM SGR dashboard is provisioned on every Superset start**: the image's `CMD` runs `superset/bootstrap.sh` before gunicorn (the base image's never would), which runs `superset/provision_sgr.py` — the `litellm_sgr_monthly` dataset's SQL is re-synced from `superset/assets/litellm_sgr_monthly.sql` and its columns from `superset/sgr_spec.py` each time; charts / dashboard are created only when their fixed UUID is missing, so UI edits survive. SQL columns and buckets stay in step with `sgr_spec.py` (`unit-tests/trino`); ship with `make build trino superset && make up trino superset`. `TRINO.md § Counting LiteLLM SGRs`.

## Cross-folder rules

Also in the root `CLAUDE.md` § Cross-folder rules — read the named entry there for the full picture.

- **Trino reads other stacks' Postgres** — this side: one catalog file per DB, its password in `.env` and `trino-coordinator`'s `environment:`; `roofix-db`, `classifier-db`, `route-policy-db`, `supabase-db` by service DNS on `ai_shared` (the last three via their read-only `trino_reader` roles); `litellm_db` / `sandbox-db` via `host.docker.internal:5432` / `:5440` — unpublishing `PORT_SANDBOX_DB` severs `postgres_sandbox` by design. (root `CLAUDE.md` § Cross-folder rules)
- **Superset trusts a header** — `AUTH_TYPE=AUTH_REMOTE_USER` trusts `X-Forwarded-Email` (`SUPERSET_REMOTE_USER_HEADER`) with no password check; safe only because `oauth2-proxy-superset` deletes the client's copies before setting it, the edge's `listen superset` deletes the identity headers and every underscore-named header, and `oauth2-proxy-superset` reaches `superset:8088` directly. Never `X-Auth-Request-*` (response-only in oauth2-proxy, not stripped from requests). A new `SUPERSET_REMOTE_USER_HEADER` needs a new `del-header` in `ai/edge/haproxy.cfg`. `TRINO.md § Header-auth trust boundary`. (root `CLAUDE.md` § Cross-folder rules)
- **A UI that cannot run under a path prefix gets its own hostname** — Superset: `SUPERSET_PUBLIC_HOST` ↔ the tunnel public hostname (`ai/cloudflared`) ↔ the Google OAuth client's redirect URI ↔ the cache-bypass rule ↔ `PROXY_FIX_CONFIG` here. (root `CLAUDE.md` § Cross-folder rules)
- **Paths under `chat.zeoenergy.com`** — `/rustfs/` (`http://lake-s3:9001/rustfs/`, fixed by RustFS) is in `OAUTH2_PROXY_UPSTREAMS`; Superset is not. (root `CLAUDE.md` § Cross-folder rules)
- **Swap-class services publish through the edge** — `trino-mcp` and `superset` swap; `superset` has no `ports:`, `PORT_SUPERSET` is the edge's. Everything else here is recreate-class (Postgres, `lake-s3` data dir, HMS migrates on start, one coordinator); the two `*-init` services are oneshot. (root `CLAUDE.md` § Deploys (`make up`))
- **Deploy order across stacks** — after another stack adds a catalog env var: `make up trino trino-coordinator`; rotating a `TRINO_JDBC_USERS` entry: `make up trino trino-auth-init` (re-read in 5 s, no restart). (root `CLAUDE.md` § Cross-folder rules)

## Env vars

One line each; full detail in `.env.example` (`## Trino data lake` block) and `TRINO.md`. Ports: root `CLAUDE.md` § Env vars and the PORT REGISTRY.

- `PORT_TRINO` (`8013`) — HTTPS `:8443` listener (web UI, REST, JDBC, password auth). Never map `:8080`.
- `PORT_LAKE_S3_API` / `PORT_LAKE_S3_CONSOLE` (`8014` / `8015`) — `lake-s3` S3 API (container `9000`, path-style) and console (`9001`, `/rustfs/console/`; also behind oauth2-proxy).
- `PORT_SUPERSET` (`8016`) — published by `ai/edge`, which strips the identity headers; bind it to loopback before exposing the host on an untrusted network.
- `PORT_HMS_DB` / `PORT_SUPERSET_DB` (`5436` / `5437`) — operator psql only.
- `LAKE_S3_ACCESS_KEY` / `LAKE_S3_SECRET_KEY` (`trino` / `trino-dev-only`) — RustFS root keys, also used by `lake-s3-init`, HMS (`${env.…}` in `metastore-site.xml`) and the Iceberg catalog. Dev defaults; rotate with `.env` + `make up trino`.
- `HMS_DB_USER` / `_PASSWORD` / `_NAME` (`hive` / `hive` / `metastore`) — `hive-metastore-db`; only HMS talks to it.
- `SUPERSET_DB_USER` / `_PASSWORD` / `_NAME` (`superset` ×3) — `superset-db` (dashboards, saved queries, accounts).
- `SUPERSET_SECRET_KEY` (required) — Flask session signing; `openssl rand -hex 32`.
- `SUPERSET_ADMIN_PASSWORD` (required) — bootstrap admin, first start only (`superset/bootstrap.sh`); unusable from a browser (no password form under `AUTH_REMOTE_USER`), later sign-ins come via `oauth2-proxy-superset`'s `X-Forwarded-Email`. New accounts are `Alpha` (no SQL Lab); the first Admin is `superset fab create-admin` before that person's first visit, or an `ab_user_role` row after — `TRINO.md § Superset sign-in and roles`.
- `TRINO_MCP_MAX_ROWS` (`10000`) — `run_query` row clamp (spliced `LIMIT` + `fetchmany`).
- `TRINO_MCP_MAX_RUNTIME_S` (`30`) — sent as `session_properties.query_max_execution_time`; the coordinator's `query.max-execution-time` is the backup.
- `TRINO_JDBC_USERS` (required) — `user:password[,…]` for `:8443`; no `,` `:` or whitespace in passwords; `make up trino trino-auth-init` to rotate.
- `TRINO_TLS_SANS` (`DNS:trino-coordinator,DNS:localhost,IP:127.0.0.1`) — SANs of the self-signed cert, read only when `tls/trino.pem` does not exist yet. `TRINO.md § Rotating the TLS cert`.
- `TRINO_SHARED_SECRET` (required) — node-to-node secret Trino mandates once client auth is on, even single-node.
