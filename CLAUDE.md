# Zeo AI workbench

A uv workspace monorepo, mostly **AI infrastructure** for one Ubuntu box (`zeo@192.168.5.240`): Docker Compose stacks under `ai/` (LiteLLM, vLLM / llama.cpp, Open WebUI, route-policy, classifier, sandbox, Trino, Supabase, …) and the shared library `shared/common/`; plus `widget/`, the Windows tray "Claude Usage Observer". Containers run on the box: edit here, the operator runs `make up` there.

## Repo map

| Path | What |
|---|---|
| `ai/<stack>/` | One compose stack each: `docker-compose.<stack>.yml` + its guide (`<STACK>.md`, the classifier's `API.md`). Topology, ports and the flow diagram: [`ai/AI_INFRA.md`](ai/AI_INFRA.md). |
| `ai/jinja/` | Chat templates bind-mounted into several model servers. |
| `shared/common/` | The `common` package every Python project imports ([§ Shared Python code](#shared-python-code-sharedcommon)). |
| `scripts/deploy.py` | What `make up` runs ([§ Deploys](#deploys-make-up)). |
| `unit-tests/<service>/`, `shared/common/tests/` | Tests ([§ Testing](#testing)). |
| `widget/` | The tray widget (`claude_observer`); the root `config.json` is the widget's. |
| `.env.example` | Every variable with its comment (the box's copy is `.env`). |
| `Makefile` | `make up|build|logs|exec|network <stack> [service]`; `STACKS` order matters (`signals` before `route-policy`, `edge` last). |

## Subfolder CLAUDE.md files

Loaded only when you work in that folder; each holds its stack's rules, traps and env vars (one line each, pointing at its guide):

`ai/` + `route-policy`, `litellm`, `classifier`, `detector`, `signals`, `llama`, `vllm`, `edge`, `oauth2-proxy`, `cloudflared`, `openwebui`, `open-terminal`, `n8n`, `kokoro`, `madlad`, `sandbox`, `trino`, `supabase`, `roofix`, `interceptor`; `shared/common/`; `widget/`.

## Cross-folder rules

Each is also written into every subfolder CLAUDE.md it touches. Change all sides together.

- **Route invariants — two copies.** `ai/route-policy/route_policy/engine/invariants.py` (service) and `ai/litellm/hooks/routing/invariants.py` (hook, allowed routes = the four chain aliases) must agree; `unit-tests/litellm/test_hook_invariants.py` loads both by path and runs the same cases. Keep `enforce` / `fail_safe_route` / `PRIVATE_ROUTE` stable.
- **`/decide` / `/observe` contract.** Shared by `ai/litellm/hooks/routing/policy_client.py` and route-policy; key sets pinned by `unit-tests/route-policy/test_pipeline.py` / `test_api.py`. Both idempotent on the hook-made `decision_id`. New request fields are optional so route-policy deploys before LiteLLM.
- **`local-private` is where every failure ends.** It has no Claude deployment and no `fallbacks` / `context_window_fallbacks` entry — never add one. Every `auto` turn the engine did not route (timeout, error, `apply: false`, client off) goes there; the invariants never move a requested `local-private`; a turn pin never moves a turn off privacy; the overflow hook never empties it. Customer data never reaches Claude.
- **Open WebUI feeds routing.** `OPENWEBUI_ENABLE_FORWARD_USER_INFO_HEADERS=true` + patch `0005` (`X-OpenWebUI-Message-Id`) give the hook the chat / message ids the turn pin keys on; patch `0004`'s nudge arrives as an extra user message under the same message id; background-task detection reads Open WebUI's default task prompts; set Admin → Interface → Task Model to a non-`auto` model (`local-private`).
- **Timeout ladder.** Open WebUI `OPENWEBUI_AIOHTTP_CLIENT_TIMEOUT` 900 > chain deployments' per-read 240 (local) / 300 (Claude); the edge's `listen litellm` 900 s. `unit-tests/litellm/test_timeouts.py` enforces the order.
- **Local vLLM slots.** `muse-glimmer` `--max-num-seqs 8` (`ai/vllm`) vs the classifier's `max_concurrent: 6` (`ai/classifier/models.yaml`) leaves 2 for chat (all 8 busy → the overflow hook spills chat to Claude). `qwen3.8-solo` `--max-num-seqs 3`, its classifier entry `max_concurrent: 1`. `EDGE_LITELLM_MAX_CONCURRENT_PER_KEY` (8) ≥ 3 + 2.
- **Context windows.** LiteLLM `model_info.max_input_tokens` = vLLM `--max-model-len` − the deployment's `max_tokens` (`qwen3.8-solo` 131072 → 114688, 98304 on `local-code`; `muse-glimmer` 262144 → 245760, 229376 on `local-code`). Change both sides; a vLLM tag bump must keep `POST /tokenize` working (the overflow hook's context fit).
- **Image limit per prompt.** `muse-glimmer`'s `--limit-mm-per-prompt '{"image": 3}'` = its `max_images_per_prompt: 3` in `ai/classifier/models.yaml`.
- **`qwen3.5-4b` sizing.** llama.cpp `-c 32768 --parallel 2 --n-predict 512` = classifier entry `max_concurrent: 2`, `max_tokens.score: 512`; route-policy's `history.max_chars` ≤ the entry's `text_char_budget` (12000) and `ROUTE_POLICY_ANALYSIS_CONCURRENCY` (2) = `max_concurrent`.
- **CPU core split** (32 vCPUs). 0–15 `qwen3.5-4b` (`LLAMA_QWEN35_4B_THREADS=16` + `LLAMA_QWEN35_4B_EXTRA_ARGS="--cpu-range 0-15"`, set together), 24–31 `signals` (`SIGNALS_CPUSET`), 16–23 the rest; `glm5.3-flash` needs a range outside both. Smaller host: empty `SIGNALS_CPUSET`, lower both.
- **GPU sharing and driver floor.** `detector` shares GPU 2 with `qwen3.8-solo` (`--gpu-memory-utilization 0.90`). CUDA 13 builds (`LLAMA_UNSLOTH_VARIANT=cuda13-portable`, the detector's torch wheel) need an R580+ driver.
- **`common` inside LiteLLM.** `common.conversation` is bind-mounted into the container (which does not install `common`), so it stays stdlib-only; a new `common.<x>` import in `ai/litellm/hooks/` needs its own mount line. Route-policy routes through `common.conversation` / `keywordmatch` / `pii` / `net` / `signals` — a change there changes routing (`render_transcript` changes stored verdict keys).
- **Signals contract.** `common.signals` is both the sidecar's wire format and route-policy's client — change together. A model change in `ai/signals/models.yaml` needs a new route-policy ruleset version for every evaluator pinning that model id (until bound they abstain with `model mismatch`).
- **Classifier as route-policy's model runner.** The `llm_classifier` uses the classifier's `POST /assess`, `GET /models`, `GET` / `DELETE /jobs/{id}` (it always deletes); it submits only to a `models.yaml` entry that is `authenticated: false`, `available: true` and resolves to private addresses. A classifier API change breaks `route_policy/analysis/`. Route jobs share the classifier's FIFO with document jobs.
- **LiteLLM pass-throughs carry the auth.** `/v1/classifier`, `/detector`, `/madlad`, `/interceptor`, `/roofix`, `/sandbox`, `/route-policy` in `litellm_config.yaml` each keep `auth: true` — those services check no token.
- **Trino reads other stacks' Postgres.** `classifier-db`, `route-policy-db`, `supabase-db` each get a read-only `trino_reader` role from their db-init script on the FIRST start of an empty volume; its password goes in `.env` and `trino-coordinator`'s `environment:`, the catalog in `ai/trino/catalogs/`. `litellm_db`, `roofix-db`, `sandbox-db` log in with their stack's own `*_DB_USER` (rotating it needs `make up trino trino-coordinator`); `litellm_db` / `sandbox-db` via `host.docker.internal` + the published port (unpublishing `PORT_SANDBOX_DB` severs `postgres_sandbox`, by design).
- **Paths under `chat.zeoenergy.com`.** `OAUTH2_PROXY_UPSTREAMS` routes by longest prefix, path unchanged: `/sandboxes/` (↔ `SANDBOX_PROXY_URL`), `/superset/`, `/rustfs/` (fixed by RustFS), `/edge/` (status page), everything else Open WebUI. Change both sides.
- **n8n is not exposed.** No host port, no `OAUTH2_PROXY_UPSTREAMS` entry, no oauth2-proxy instance of its own, no tunnel hostname — only `ai_shared` reaches `n8n:5678`. Its subpath mode is broken upstream (n8n-io/n8n#19635), so exposing it means its own hostname at root, with its own oauth2-proxy instance and tunnel rule, and `N8N_HOST` / `N8N_PATH` / `N8N_EDITOR_BASE_URL` / `N8N_WEBHOOK_URL` / `N8N_PROXY_HOPS` set to match (`ai/n8n/N8N.md § Exposing it later`).
- **Superset trusts a header.** The edge's `listen superset` deletes `X-Auth-Request-*` and `X-Forwarded-Email` / `-User`; oauth2-proxy reaches `superset:8088` directly and must never go through the edge. A new `SUPERSET_REMOTE_USER_HEADER` needs a new `del-header`. Same posture for Open WebUI's trusted headers: it binds `127.0.0.1:8007` only.
- **Open Terminal ↔ Open WebUI.** `OPEN_TERMINAL_API_KEY` is used by both compose files (interpolated into `TERMINAL_SERVER_CONNECTIONS`); `open-terminal` sits on `terminal_net` with `openwebui` only (`make network`), never `ai_shared`.
- **`docker.sock` only in `sandbox-runner`.** It is the audit boundary (`ai/sandbox/runner/spawner.py`). Never mount it elsewhere — not in `open-terminal`, not via Supabase's Vector / logs override.
- **`api.zeoenergy.com` is split-horizon.** Public DNS: the cloudflared tunnel → `litellm:4000`; LAN: the router's override → the edge on 443 with `edge-certs`' Let's Encrypt cert. Never add an `A` record in Cloudflare. `CLOUDFLARE_DNS_API_TOKEN` (edge-certs) ≠ `CLOUDFLARE_TUNNEL_TOKEN`.
- **`cdp_interceptor` traps** (`widget`, `ai/interceptor`): `interceptor.js` is injected verbatim (never reformat) and read once per process; `shared/common/CLAUDE.md`.
- **Smaller couplings.** Classifier `DETECTOR_MAX_LABELS_PER_CALL` ≤ detector `DETECTOR_MAX_LABELS`; roofix captures through the interceptor's `roofix` profile; n8n's AI nodes default to `http://litellm:4000/v1` + `DEFAULT_LITELLM_MASTER_KEY`; the sandbox's edit contract is `common.textops`.
- **Swap-class services publish through the edge** — next section.
- **Deploy order across stacks.** `make up edge` before the first `make up` of a newly fronted service; `signals` before `route-policy`; `route-policy` before `litellm` when the contract grows; `make up trino trino-coordinator` after a catalog env change; `docker restart prometheus` after a new scrape job (`ai/litellm/prometheus.yml`); `docker restart classifier` after a `models.yaml` edit. Staged upgrades: the `§ Upgrading …` sections of `ai/route-policy/ROUTE_POLICY.md`.

## Deploys (`make up`)

`make up <stack> [service...]` runs [`scripts/deploy.py`](scripts/deploy.py). Guide: [`ai/edge/EDGE.md § How make up deploys`](ai/edge/EDGE.md#how-make-up-deploys).

- **swap** (`com.zeo.deploy: swap`): a new `<name>-<hash>` copy starts beside the old one and must turn healthy (and UP in the edge) before the old one stops (`stop_grace_period`). `<hash>` covers the compose config hash, the image ID **and every bind-mounted file inside the repo** — editing `ai/litellm/litellm_config.yaml` or `ai/litellm/hooks/**` then `make up litellm` deploys with no downtime. A failed wait removes the new copy; the old one keeps serving.
- **oneshot** (`restart` `no` / unset): `up -d --no-deps`, `docker wait`; non-zero stops the deploy.
- **recreate** — the default: plain `up -d --no-deps` (`recreate (downtime): <reason>`). Each has a YAML comment saying why.

Rules:
- **A swap service never gets `container_name` or `ports:`** (two copies run at once). Publish its port through `ai/edge`: a `listen <service-key>` section in `ai/edge/haproxy.cfg` + a `ports:` line in `ai/edge/docker-compose.edge.yml`, reusing its `PORT_*` variable. It needs a healthcheck and a `stop_grace_period` covering its longest request. `deploy.py` falls back to recreate (saying why) for `container_name`, `ports:`, a GPU, no healthcheck, `scale > 1`, `network_mode: host|container:|service:`, `pull_policy: always|build`.
- **New services are recreate by default.** Leave them unlabelled unless two copies can safely run at once.
- **Address services by service name, never container name**; swap services: `make exec|logs <stack> <svc>`.
- **A fronted swap checks the edge first** and refuses when the running edge lacks its backend (run `make up edge`) unless `FORCE=1`. The edge also fronts three recreate-class APIs (`classifier`, `sandbox-runner`, `detector`) so a restart answers `503` + `Retry-After`.
- `edge` is last in `STACKS`; editing `ai/edge/haproxy.cfg` then `make up edge` is a validated (`haproxy -c`), hitless `USR2` reload; a new edge `ports:` entry or an `EDGE_*` change recreates it (seconds of LAN downtime). Edge-specific rules: `ai/edge/CLAUDE.md`.
- Switches: `RECREATE=1`, `DRY_RUN=1`, `FORCE=1` (only the literal `1`). `uv run --no-sync pytest unit-tests/deploy -q` enforces all of the above.

## Env vars and the PORT REGISTRY

- `.env.example` documents every variable; a new one goes there (with its comment), into the compose `environment:` as `${VAR:-default}` (or `${VAR:?…}` when required), and onto its stack's CLAUDE.md list. A literal `$` in `.env` is `$$`. `openwebui`, `n8n`, `supabase` compose files are pure `${…}` templates — edit `.env`. A changed `.env` value needs `make up <stack>`, not a restart.
- Host ports live in the `PORT REGISTRY` block at the top of `.env` as `PORT_*`; never hard-code one in a compose file. Postgres group: `5432` litellm_db, `5433` roofix-db, `5435` n8n-db, `5436` hive-metastore-db, `5437` superset-db, `5438` supabase-db, `5439` classifier-db, `5440` sandbox-db, `5441` route-policy-db (`5434` is a host Postgres on the box). Next free HTTP ports: `8026`, then `8029`. A new port also goes into `ai/AI_INFRA.md § Ports at a glance`.
- Wiring tests check that a stack's variables are listed in its own CLAUDE.md (`unit-tests/deploy/test_deploy.py` → `ai/edge/CLAUDE.md`, `unit-tests/route-policy/test_wiring.py` → `ai/route-policy/CLAUDE.md`, which also checks route-policy's row in the Postman table below).

## Shared Python code (`shared/common`)

Any Python package or module a second project could plausibly want within six months — scraping / CDP / browser helpers, MCP or model clients, env + logging boilerplate, cross-cutting utilities — MUST live in `shared/common/`, not in the project dir that first needs it. Project-specific business logic stays put; the test is reusability, not size. Consumers import it through the uv workspace (`common = { workspace = true }`). New capability: `shared/common/src/common/<name>/`, public API in its `__init__.py`, tests in `shared/common/tests/`; consumers need no pyproject change unless a new external dep appears. Full descriptions: [`shared/common/CLAUDE.md`](shared/common/CLAUDE.md).

`cdp_interceptor` (Chrome CDP capture) · `conversation` (reading chat histories; stdlib, mounted into LiteLLM) · `documents` (load / OCR / text-match documents) · `env` (`.env` loader) · `jobs` (job registries, Postgres work queue, progress) · `keywordmatch` (Go-RE2 keyword rules) · `logging_setup` · `logprobs` (a JSON integer field's distribution from token logprobs; stdlib) · `net` (SSRF guard, `fetch_url`, locality check) · `pii` (pattern PII finder) · `processed_store` (Gmail dedup) · `signals` (signals sidecar wire + client) · `textops` (Claude-Code-style file edits) · `trino` (`TrinoClient`, SELECT-only by default) · `vision` (regions, layers, `ArtifactStore`).

## Result shapes declared beside the producer

Every structured result a service returns has a shape declared next to its producer (a decorator), registered under the key consumers look it up by, served by an endpoint, and checked by a test that runs the real producer (failing on an undeclared key, a missing required key or a wrong value). Adding a producer or a key updates its declaration in the same change. Today: the classifier's `@describes` / `@result_spec` (`ai/classifier/CLAUDE.md`) and route-policy's `@declares(OutputSpec)` (`ai/route-policy/CLAUDE.md`).

## `ai/AI_INFRA.md` stays in sync

When a `docker-compose.*.yml` under `ai/` is added, renamed or removed, or its services / ports / cross-service dependencies change, update [`ai/AI_INFRA.md`](ai/AI_INFRA.md) in the same change — the single source of truth for how the stacks fit: the **Compose files** row (compose file + README, create one if missing), the Mermaid **Flow diagram** node plus an edge per runtime dependency, **Ports at a glance**, and the "Reading the diagram" bullets if it joins the public path (Cloudflare → oauth2-proxy → …).

## Postman collections

Every `ai/` service with an HTTP API ships a Postman v2.1 collection beside its docs, modelled on sandbox-runner's (collection-level bearer auth on a `virtual master key` variable, a base-URL variable, one `item` per endpoint with a real body, an `MCP` subfolder for JSON-RPC).

| Service | Collection | Endpoints doc (same folder) |
|---|---|---|
| sandbox-runner | `ai/sandbox/sandbox-runner.postman_collection.json` | `ENDPOINTS.md` |
| interceptor | `ai/interceptor/interceptor.postman_collection.json` | `INTERCEPTOR.md` |
| classifier | `ai/classifier/classifier.postman_collection.json` | `API.md` |
| detector | `ai/detector/detector.postman_collection.json` | `DETECTOR.md` |
| roofix bridge | `ai/roofix/roofix.postman_collection.json` | `ROOFIX.md` |
| kokoro (TTS) | `ai/kokoro/kokoro.postman_collection.json` | `KOKORO.md` |
| madlad (translate) | `ai/madlad/madlad.postman_collection.json` | `MADLAD.md` |
| litellm proxy | `ai/litellm/litellm.postman_collection.json` | `LITELLM.md`, `LITELLM_MCP.md` |
| vllm (per model) | `ai/vllm/vllm.postman_collection.json` | `VLLM.md` |
| llama-server | `ai/llama/llama.postman_collection.json` | `LLAMA.md` |
| searxng | `ai/searxng/searxng.postman_collection.json` | `SEARXNG.md` |
| open-terminal | `ai/open-terminal/open-terminal.postman_collection.json` | `OPEN_TERMINAL.md` |
| supabase | `ai/supabase/supabase.postman_collection.json` | `SUPABASE.md` |
| route-policy | `ai/route-policy/route-policy.postman_collection.json` | `ROUTE_POLICY.md` |
| signals | `ai/signals/signals.postman_collection.json` | `SIGNALS.md` |

Open WebUI is omitted on purpose (a UI; its traffic is LiteLLM's).

**Keep them in sync with the code.** An endpoint added, renamed or removed, or a change to its request / response shape, path or query params, auth or headers, updates its collection in the SAME change:
1. Add / update / remove the `item` (or its `MCP` subfolder); `url.path`, `method`, `header`, `body.raw` match the code exactly (valid JSON for the current Pydantic model).
2. **Path parameters use `:name`, never `{{name}}`**, in both `url.raw` and `url.path`, with a `url.variable` entry per parameter (`{"key", "value": "", "description"}`) and a matching top-level collection `variable` as the default. The leading host variable (`{{litellm}}`, `{{kokoro}}`, …) is the only `{{…}}` allowed in `url.raw`; headers, bodies, query values and auth may use `{{…}}`.
3. The `description` says what it does, returns and which errors it can give, naming its doc section.
4. A new variable gets a top-level `variable` with a `description`; a routing change updates the base-URL variable and `info.description`'s auth note.
5. A new HTTP service gets `{service}.postman_collection.json` beside its docs and a row above.

The `*.md` endpoint doc and the collection must agree; when they don't, the code is authoritative and both are corrected.

## Testing

- Layout: `unit-tests/<service>/` per service and `shared/common/tests/`. No `make test`; one shared workspace `.venv`.
- Run each suite in **its own pytest process** (`unit-tests/classifier` and `unit-tests/detector` both ship a top-level `config.py`): `UV_LINK_MODE=copy uv run --no-sync python -m pytest unit-tests/<suite> -q -p no:cacheprovider`; the classifier / detector suites add `--with pytest --package classifier|detector`. Per-suite commands: each stack's guide and CLAUDE.md.
- Postgres-backed tests (classifier, route-policy, `common.jobs`) **skip** without `TEST_POSTGRES_DSN` — an admin DSN on which each session creates and drops its own database. A run that skipped them has not passed.
- Never `uv sync --package X` (it prunes the shared venv); use `--no-sync`. `UV_LINK_MODE=copy` fixes hardlink errors.
- The widget has no tests; verify by running it.
