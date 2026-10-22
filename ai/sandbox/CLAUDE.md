# ai/sandbox

Runs untrusted, model-generated web apps (Streamlit, Gradio, Flask, FastAPI, Vite+React, Next, Express, static HTML) in short-lived isolated containers and returns an iframe URL for Open WebUI. `sandbox-runner` (FastAPI + FastMCP, `runner/`) is driven through the `run` MCP tool or the pipelined `create` → `write_files` (new files) → `read_file` / `edit_file` (changes) → `exec` → `preview` → `close` flow, registered in LiteLLM. Guides: [`SANDBOX.md`](SANDBOX.md) (operator guide, security model), [`ENDPOINTS.md`](ENDPOINTS.md) (HTTP + MCP surface). Compose: `docker-compose.sandbox.yml`. Tests: `unit-tests/sandbox/` (runner) and `shared/common/tests/test_textops.py` (edit rules).

## Rules

- **Network segmentation is the primary security control**, not container hardening. Five networks (`SANDBOX.md § Security model (read this first)`): `ai_shared` (runner + proxy only), `sandbox_net` and `sandbox_state` (`internal: true` — no gateway), `sandbox_egress_out` (`sandbox-egress` ONLY: the one route to the internet), `sandbox_db_publish` (`sandbox-db` ONLY, masquerade off, exists solely so `PORT_SANDBOX_DB` publishes — Docker publishes nothing for a container on internal networks only). Adding a service or a network membership: keep memberships minimal and re-run `SANDBOX.md § Security-invariant checklist` before merging. Same for any change to `spawner.py` or the base Dockerfiles.
- **`sandbox-runner` is the only container with `docker.sock`.** Every line of code that touches the socket lives in `runner/spawner.py` — the audit focal point.
- **Egress is an allowlist.** `sandbox-egress` (tinyproxy, `FilterDefaultDeny Yes`) reads `proxies/tinyproxy.filter`, one `^…$`-anchored regex per line. A new dependency source is a filter edit + `make up sandbox sandbox-egress` (swap-class; its hash covers the filter). **Never add `.*`.** `SANDBOX_EGRESS_ALLOWLIST` is only an additive tweak.
- **Every runtime binds port 80**, so `sandbox-proxy` (Caddy) routes `/{sandbox_id}/* → sandbox-{id}:80` statically. A runtime on another port would need dynamic Caddy admin-API config — don't. New runtime: `SANDBOX.md § Adding a runtime`.
- **Job state is Postgres (`sandbox-db`) via `common.jobs.PostgresRegistry`**, not SQLite, on its own `sandbox_state` network so a container escape cannot reach job records.
- **File tools follow Claude Code's contract — keep it.** `read_file` (numbered `offset` / `limit` window), `edit_file` (exact `old_string` → `new_string`, unique match or `replace_all`, closest region on a miss), `write_files` for new files only (`409 rewrite_of_existing_file` for an existing 30+-line text file keeping ≥ 50 % of its lines, unless `overwrite=true`), short results (snippet for an edit, one echoed line for `exec`, no content repeated in `structured_content`). The edit / numbering / UTF-8 rules are `common.textops` (shared, pure); the rewrite-guard policy and tool wiring stay in `runner/operations.py`. Any guidance string telling a model which tool to use must teach this flow. `SANDBOX.md § Editing files`.
- **A new runner module goes into the `COPY` list of `runner/Dockerfile`**, or the image dies at boot with `ModuleNotFoundError`.
- **Sessions + idle TTL.** `create` / `run` return a `session_id`; passing it back reuses the container. The reaper enforces `SANDBOX_HARD_TTL_SECONDS` and `SANDBOX_IDLE_TTL_SECONDS` (via `metadata->>'last_used_at'`, bumped on every reuse call). Teardown: `close`, `DELETE /session/{id}`, `DELETE /jobs/{id}`.
- **Env passed to a sandbox is immutable after spawn**; runner-controlled keys (`HTTP_PROXY`, `HTTPS_PROXY`, `PYTHONUNBUFFERED`, …) are refused and re-applied by the spawner.
- **Classes:** `sandbox-proxy`, `sandbox-egress` swap; `sandbox-runner` recreate (slot semaphore, reaper), `sandbox-db` recreate. Use `make exec sandbox sandbox-proxy CMD='…'` for swap containers.
- API shape changes update `sandbox-runner.postman_collection.json` and `ENDPOINTS.md` in the same change (root `CLAUDE.md` § Postman collections).

## Cross-folder rules

Also in the root `CLAUDE.md` § Cross-folder rules — read the named entry there for the full picture.

- **`docker.sock` only in `sandbox-runner`** — this stack holds the one mount; nothing else in the repo may add one. (root `CLAUDE.md` § Cross-folder rules)
- **Paths under `chat.zeoenergy.com`** — `SANDBOX_PROXY_URL` (`https://chat.zeoenergy.com/sandboxes`) and the `http://sandbox-proxy:80/sandboxes/` entry in `OAUTH2_PROXY_UPSTREAMS` change together; the Caddyfile matches `/sandboxes/{id}/`. (root `CLAUDE.md` § Cross-folder rules)
- **Trino reads other stacks' Postgres** — `postgres_sandbox` reads `host.docker.internal:${PORT_SANDBOX_DB}` with the `SANDBOX_DB_*` credentials, so changing those also needs `make up trino trino-coordinator`; unpublishing `PORT_SANDBOX_DB` (or dropping `sandbox_db_publish`) severs it, by design. (root `CLAUDE.md` § Cross-folder rules)
- **Swap-class services publish through the edge** — `sandbox-proxy` (8011) has no `ports:`; `sandbox-runner` (8012, recreate-class) is fronted by the edge for its `503` + `Retry-After`. Both via `listen` sections in `ai/edge/haproxy.cfg`. (root `CLAUDE.md` § Cross-folder rules and § Deploys (`make up`))
- **LiteLLM pass-throughs carry the auth** — `/v1/sandbox` → `sandbox-runner:8000` in `ai/litellm/litellm_config.yaml` has `auth: true`; the runner checks no token. (root `CLAUDE.md` § Cross-folder rules)
- **Smaller couplings** — the file tools' edit contract is `common.textops` (`shared/common`): changing an edit rule changes it for every consumer; its tests are `shared/common/tests/test_textops.py`. (root `CLAUDE.md` § Cross-folder rules, § Shared Python code (`shared/common`))

## Env vars

One line each; full detail in `.env.example` (`SANDBOX_*` block) and `SANDBOX.md § Configuration`.

- `SANDBOX_MAX_CONCURRENT` (`8`) — running sandboxes at once; `429` past it. ~512 MB RAM + 1 CPU each.
- `SANDBOX_DEFAULT_TTL_SECONDS` (`900`) — default idle TTL; a model may ask shorter, never past the hard TTL.
- `SANDBOX_IDLE_TTL_SECONDS` (empty — inherits the default TTL) — reaper's idle threshold on `last_used_at`; keep it ≤ the hard TTL.
- `SANDBOX_HARD_TTL_SECONDS` (`3600`) — absolute lifetime; reaper sweeps every 60 s.
- `SANDBOX_EGRESS_ALLOWLIST` (empty) — additive to `tinyproxy.filter`; prefer the filter file.
- `SANDBOX_MAX_FILE_BYTES` (`1000000`) — per-file cap (decoded base64), also `edit_file`'s strings and result; 422 on HTTP, `payload too large` on MCP.
- `SANDBOX_MAX_PAYLOAD_BYTES` (`10000000`) — total `files` cap per request, checked before the tarball is built.
- `SANDBOX_DB_USER` / `_PASSWORD` / `_NAME` (`sandbox`) — dev defaults; override before exposing anything past `sandbox_state`. Also read by Trino's `postgres_sandbox`.
- `PORT_SANDBOX_RUNNER` (`8012`) / `PORT_SANDBOX_PROXY` (`8011`) — published by `ai/edge`, never by this compose file. `PORT_SANDBOX_DB` (`5440`) — published by `sandbox-db` itself; Postgres group (root `CLAUDE.md` § Env vars and the PORT REGISTRY).
- `SANDBOX_PROXY_URL` (this box: `https://chat.zeoenergy.com/sandboxes`) — iframe `src` prefix; coupled to the oauth2-proxy `/sandboxes/` upstream.
