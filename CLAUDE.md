# Claude Usage Observer

Windows system tray app that monitors Claude Code token usage. Reads local `~/.claude/projects/**/*.jsonl` logs and optionally scrapes account stats from claude.ai via Chrome DevTools Protocol (CDP).

## Commands

```bash
# Install deps
pip install -e .
# or
uv pip install -e .

# Run
python -m claude_observer
# or after install:
claude-usage-observer

# Debug CDP captures (requires Chrome on --remote-debugging-port=9222)
python -m claude_observer.browser.cdp_spy
```

## Configuration

All config lives in `config.json` (project root). Edit directly or via the Settings window (tray right-click → **Settings…**). Changes apply immediately via `config.apply_updates()`.

Key variables:

| Key | Default | Notes |
|---|---|---|
| `DEBUG_LOGGING` | `false` | Verbose CDP + widget logging |
| `REFRESH_INTERVAL_SECONDS` | `300` | Seconds between local token-stat refreshes |
| `CONSOLE_FETCHER_ENABLED` | `false` | Enable claude.ai account stats scraping |
| `BROWSER_DEBUG_PORT` | `9222` | Chrome remote-debugging port |
| `EXCLUDE_WEEKDAYS` | `"5,6"` | Days excluded from rolling averages (0=Mon) |
| `INCLUDE_PATHS` | _(empty)_ | Filter projects by path prefix |
| `LLM_URL` | `http://localhost:8001` | Local llama-server URL |
| `LLM_API_KEY` | `sk-no-key-required` | API key sent to local server |
| `LLM_MODEL` | _(empty)_ | Model alias passed to Claude Code |
| `LLM_LOG_MAX_LINES` | `200` | Max lines in server-output log box |
| `LLAMA_SERVER_CMD` | _(empty)_ | Full shell command to launch llama-server |
| `OPENWEBUI_VERSION` | `v0.11.3` | Open WebUI: upstream release tag used as the `FROM` base of `ai/openwebui/Dockerfile.openwebui` and in the local image name `openwebui-zeo:<tag>`. The container runs upstream **plus** the patches in `ai/openwebui/patches/` (currently one, `0002`: `WEBUI_AUTH_TRUSTED_ACCESS_TOKEN_HEADER` — in trusted-header SSO mode, verify the identity oauth2-proxy asserts against Google with the forwarded access token and take name + picture from Google, so one sign-in at oauth2-proxy is the only gate). Bumps need `make build openwebui` then `make up openwebui`; a patch that no longer applies fails the build by design — regenerate it per `ai/openwebui/OPENWEBUI.md § Custom image`, never force it. |
| `LLAMA_UNSLOTH_TAG` | `b10796-mix-659e406` | llama.cpp stack: [unslothai/llama.cpp release](https://github.com/unslothai/llama.cpp/releases) tag baked into `ai/llama/Dockerfile.llama-unsloth` for the `qwen3.8-flash` service. Needed because MTP speculative decoding for Qwen3.8-Flash-Next is not in mainline llama.cpp; the tag is also part of the local image name, so bumps need `up -d --build`. |
| `LLAMA_UNSLOTH_VARIANT` | `cuda13-portable` | llama.cpp stack: which prebuilt tarball flavour to install. `cuda13-portable` (toolkit 13.3, sm 75–120, RTX A6000 = 86) needs an R580+ driver; `cuda12-portable` (toolkit 12.8, sm 70–120) works on R525+. Must agree with `LLAMA_CUDA_RUNTIME_IMAGE` on the CUDA major. |
| `LLAMA_CUDA_RUNTIME_IMAGE` | `nvidia/cuda:13.3.1-runtime-ubuntu22.04` | llama.cpp stack: base image for `ai/llama/Dockerfile.llama-unsloth`. Provides `libcudart.so.<major>` / `libcublas.so.<major>` for the tarball; the Dockerfile fails the build via `ldd` if they don't resolve. |
| `LLAMA_UNSLOTH_CPU_TAG` | `b10840-mix-d5c17a0` | llama.cpp stack: Unsloth release tag for the **CPU-only** `glm5.3-flash` service (`linux-x64-cpu` tarball on `ubuntu:22.04`, no GPU backend). Pinned separately from `LLAMA_UNSLOTH_TAG` so bumping one never rebuilds the other. Needed because the GLM-5.3-Flash `glm5next` architecture is not merged in mainline llama.cpp (ggml-org/llama.cpp#27754 open); the mix also carries the arch's MTP draft graph. Part of the image name — bumps need `up -d --build --force-recreate glm5.3-flash`. |
| `LLAMA_GLM53_FLASH_QUANT` | `UD-Q3_K_XL` | llama.cpp stack: quant tag on `unsloth/GLM-5.3-Flash-GGUF` for `glm5.3-flash`. The whole file is mlocked in RAM: `UD-Q2_K_XL` 109 GB, `UD-Q3_K_XL` 148 GB (fits a 200 GB box), `UD-Q4_K_XL` 200 GB (needs 256 GB+). K-quants only — `IQ*` quants are ~2x slower on CPU. See `ai/llama/LLAMA.md § glm5.3-flash`. |
| `LLAMA_GLM53_FLASH_REASONING_EFFORT` | `low` | llama.cpp stack: `reasoning_effort` passed to GLM-5.3-Flash's chat template via `--chat-template-kwargs`. Accepts `low` \| `high`; anything else is treated as `max`. `low` keeps CPU-speed turns interactive; `--reasoning-budget 8192` in the compose is the hard cap on top. |
| `LLAMA_GLM53_FLASH_EXTRA_ARGS` | _(empty)_ | llama.cpp stack: extra llama-server args appended verbatim to the `glm5.3-flash` command — `--numa distribute -t 48` on a dual-socket host, `--flash-attn off` to A/B attention. |
| `AUDIO_BASE_URL` | `http://localhost:8004` | Base URL returned by Kokoro `text_to_speech` MCP tool |
| `MADLAD_APP_URL` | `http://madlad-app:8085` | URL the MADLAD proxy uses to reach the inference container |
| `MADLAD_MODEL` | `SoybeanMilk/madlad400-3b-mt-ct2-int8_float16` | HuggingFace repo ID for the pre-converted CTranslate2 MADLAD checkpoint |
| `CLASSIFIER_MAX_CONCURRENT` | `2` | Classifier: number of worker tasks claiming jobs from the SQLite-backed queue, i.e. max jobs analysed at once. Bounded by the vision model's capacity (`muse-glimmer` runs `--max-num-seqs 4`, so extra requests queue inside vLLM); a `compare` job with N live examples fans out into N+1 LLM calls of its own. `1` restores strictly serial processing. Jobs are durable: payloads sit in `PAYLOAD_DIR` (default `/data/payloads`) until a terminal phase, and `processing` rows are requeued on startup. See `ai/classifier/API.md § Async job pattern`. |
| `CLASSIFIER_REASONING_STRENGTH` | `low` | Classifier: Muse Glimmer reasoning depth for each scoring call, passed through as `VISION_LLM_REASONING_STRENGTH` and injected as a `Reasoning strength: <value>` system-prompt line. `low` \| `medium` \| `high` \| `xhigh`; empty disables the line for models that don't understand it. |
| `VISION_LLM_API` | `http://muse-glimmer:8000/v1/chat/completions` | Classifier: OpenAI-compatible chat-completions URL of the vision model that scores LLM-bound criteria. Default is the `muse-glimmer` vLLM container on `ai_shared`; `http://vllm-qwen-vl:8000/v1/chat/completions` restores the older Qwen2.5-VL-7B service (pair it with the matching `VISION_LLM_MODEL` and an empty `CLASSIFIER_REASONING_STRENGTH`). `muse-glimmer` shares its GPU pair with `qwen3.8`, so the classifier only works while muse-glimmer is the one running. |
| `VISION_LLM_MODEL` | `muse-glimmer` | Classifier: `model` field sent with every scoring call. Must match the vLLM container's `--served-model-name`, or the HF repo id (`Qwen/Qwen2.5-VL-7B-Instruct`) when that flag is unset. |
| `VISION_LLM_MAX_TOKENS` | `8192` | Classifier: completion budget per scoring call. Muse Glimmer's reasoning tokens count against it before the JSON answer; a budget exhausted mid-reasoning yields an empty answer and costs one of the `MAX_LLM_RETRIES` attempts. |
| `DRY_RUN` | `true` | Roofix Bridge: log decisions but skip Phoenix writes |
| `AGENT_PHASE` | `0` | Roofix Bridge: `0` = chatter+milestones only; `1` = +create/notify |
| `TICK_INTERVAL_SECONDS` | `300` | Roofix Bridge: APScheduler cadence |
| `BRAIN_MODEL` | `qwen3.6` | Roofix Bridge: LiteLLM alias for the AI-fallback brain |
| `ROOFIX_SENDER` | `no-reply@roofix.io` | Roofix Bridge: Gmail search-query sender (two `o`s) |
| `ROOFIX_PROCESSED_LABEL` | `roofix/processed` | Roofix Bridge: Gmail label applied to every message the bridge evaluates; excluded server-side from `LISTENER_QUERY` so already-processed emails don't fill the 25-message fetch window. Backfill existing rows with `POST /labels/backfill`. |
| `ESCALATION_RECIPIENTS` | _(empty)_ | Roofix Bridge: comma-separated recipients that receive forwarded escalations. Empty disables forwarding — escalates stay unread in Gmail for direct operator review. |
| `GMAIL_CREDENTIALS_PATH` | `/config/credentials.json` | Roofix Bridge: OAuth client-secrets file |
| `GMAIL_TOKEN_PATH` | `/config/token.json` | Roofix Bridge: OAuth refresh-token file |
| `PHOENIX_DB_HOST` / `_PORT` / `_NAME` / `_USER` / `_PASSWORD` / `_SSLMODE` | _(secrets)_ | Roofix Bridge: direct psycopg2 connection to Phoenix Postgres |
| `ROOFIX_DB_USER` / `_PASSWORD` / `_NAME` | `roofix` / `roofix` / `roofix` | Roofix Bridge: credentials for the compose-managed `roofix-db` Postgres backing `ProcessedStore`. All three fall back to `roofix` in both `docker-compose.roofix.yml` and `ai/roofix/app.py`'s DSN. Override the password for anything past dev — the DB only lives on `ai_shared` today, but don't ship the default if you expose it. |
| `PORT_ROOFIX_DB` | `5433` | Roofix Bridge: host port `roofix-db` binds to for remote connections. Lives in the `PORT REGISTRY` block at the top of `.env`. Both Postgres ports (`PORT_POSTGRES=5432` for litellm_db, `PORT_ROOFIX_DB=5433` for roofix-db) are grouped there. |
| `PHOENIX_AGENT_USER_ID` | _(unset — required for writes)_ | Roofix Bridge: dedicated Phoenix user id |
| `PHOENIX_ROOFIX_ID_COLUMN` | `migration_external_id` | Roofix Bridge: column where Roofix ids are stamped |
| `INTERCEPTOR_URL` | `http://interceptor:8080` | Roofix Bridge → interceptor base URL |
| `ROOFIX_PROFILE_NAME` | `roofix` | Named profile inside interceptor holding Roofix session cookies |
| `INTERCEPTOR_PROFILES_ROOT` | `/data/profiles` | interceptor: root under which named `--user-data-dir` profiles live |
| `INTERCEPTOR_MAX_CONCURRENT` | `8` | interceptor: max simultaneous `/capture` calls (port pool size). Each slot ≈ one Chrome + optional profile clone — see `ai/interceptor/INTERCEPTOR.md § Resource sizing` |
| `INTERCEPTOR_SCREENSHOT_WAIT_SECONDS` | `15` | interceptor: default seconds `POST /screenshot` / the `screenshot_url` MCP tool let a page render before capturing. Chrome spends the first ~4–5s booting and navigating, so values under ~8 mostly return blank pages. Screenshots ride a second CDP connection (`common.cdp_interceptor.screenshot`) so the XHR-capture session is untouched — see `ai/interceptor/INTERCEPTOR.md § Screenshots`. |
| `SANDBOX_MAX_CONCURRENT` | `8` | Sandbox: max simultaneous running sandboxes (`sandbox-runner` returns 429 past this). Each slot ≈ 512 MB RAM + 1 CPU + base-image disk footprint. |
| `SANDBOX_DEFAULT_TTL_SECONDS` | `900` | Sandbox: default idle TTL. Model can request shorter per-`create`/`run`, cannot request longer than `SANDBOX_HARD_TTL_SECONDS`. |
| `SANDBOX_IDLE_TTL_SECONDS` | _(inherits `SANDBOX_DEFAULT_TTL_SECONDS`)_ | Sandbox: reaper tears down a running sandbox whose `metadata->>'last_used_at'` is older than this. Distinct knob only when you want idle behavior to differ from the per-session default. |
| `SANDBOX_HARD_TTL_SECONDS` | `3600` | Sandbox: absolute cap on sandbox lifetime. Reaper (`ai/sandbox/runner/reaper.py`) sweeps expired containers every 60s. |
| `SANDBOX_EGRESS_ALLOWLIST` | _(empty)_ | Sandbox: additive to `ai/sandbox/proxies/tinyproxy.filter`. Prefer editing the filter file (source of truth); use this only for per-deployment tweaks. |
| `SANDBOX_MAX_FILE_BYTES` | `1000000` | Sandbox: per-file byte cap on the `files` map (POST /run, /create, /session/{id}/files, and the `run` / `write_files` MCP tools). Base64-encoded values count their DECODED length so callers can't smuggle a huge blob past the cap by encoding. Reject shape is a 413 with a specific hint that names the largest oversized path. |
| `SANDBOX_MAX_PAYLOAD_BYTES` | `10000000` | Sandbox: total byte cap on the `files` map in a single request. Enforced at pydantic validation before the tarball is built, so a hostile payload never touches the spawner. Shrink files, drop non-essential assets, or split across multiple `write_files` calls. |
| `SANDBOX_DB_USER` / `_PASSWORD` / `_NAME` | `sandbox` / `sandbox` / `sandbox` | Sandbox: credentials for the compose-managed `sandbox-db` Postgres backing `PostgresRegistry`. Same dev-default guidance as `ROOFIX_DB_*` — override before exposing anything past `sandbox_state`. |
| `PORT_SANDBOX_RUNNER` / `_PROXY` / `_DB` | `8012` / `8011` / `5434` | Sandbox: host ports. Runner is FastAPI + MCP; proxy is Caddy serving `/{sandbox_id}/*`; db is a Postgres exposed for operator inspection. Postgres ports are grouped: `5432` (litellm_db), `5433` (roofix-db), `5434` (sandbox-db). |
| `SANDBOX_PROXY_URL` | `https://chat.zeoenergy.com/sandboxes` | Sandbox: public URL prefix the runner returns as the iframe `src` for previews (via `POST /run`, `POST /tool/run`, and the `run` / `preview` MCP tools). This deployment routes sandbox traffic through the same origin as Open WebUI — `oauth2-proxy` has `http://sandbox-proxy:80/sandboxes/` in `OAUTH2_PROXY_UPSTREAMS`, so `chat.zeoenergy.com/sandboxes/{id}/*` is authenticated by the existing session cookie and reverse-proxied to sandbox-proxy. See `ai/sandbox/SANDBOX.md § Public iframe routing` for the full traffic path and how to change deployment topologies. |
| `N8N_ENCRYPTION_KEY` | _(unset — required)_ | n8n: symmetric key that encrypts every stored credential blob (API keys, OAuth secrets) in `n8n-db`. Generate with `openssl rand -hex 32`, paste into `.env`, and back it up alongside your other secrets — **losing it permanently destroys all stored credentials** (workflow rows survive, but their auth blobs become unreadable). Compose refuses to start when unset via `${N8N_ENCRYPTION_KEY:?...}` in `ai/n8n/docker-compose.n8n.yml`. |
| `N8N_DB_USER` / `_PASSWORD` / `_NAME` | `n8n` / `n8n` / `n8n` | n8n: credentials for the compose-managed `n8n-db` Postgres backing workflow, credential, and execution rows. Same dev-default guidance as `ROOFIX_DB_*` / `SANDBOX_DB_*` — override before exposing `PORT_N8N_DB` past the host. |
| `PORT_N8N_DB` | `5435` | n8n: host port `n8n-db` binds to for remote querying (psql / DBeaver / DataGrip). Lives in the `PORT REGISTRY` block at the top of `.env`, grouped with the other Postgres ports (`5432` litellm_db, `5433` roofix-db, `5434` sandbox-db, `5435` n8n-db). n8n itself has **no** host port — the editor is reached via oauth2-proxy at `chat.zeoenergy.com/n8n/`. |
| `OPEN_TERMINAL_VERSION` | `0.13.0` | Open Terminal: `ghcr.io/open-webui/open-terminal` tag used as the `FROM` base of `ai/open-terminal/Dockerfile.open-terminal` and in the local image name `open-terminal-zeo:<tag>`. The container runs upstream **plus** `patch_entrypoint.py`, which applies the unmerged upstream fix PR #118 for [issue #119](https://github.com/open-webui/open-terminal/issues/119): upstream's entrypoint runs `capsh --drop=cap_net_admin` as the unprivileged user and dies at boot with `unable to raise CAP_SETPCAP for BSET changes` whenever `OPEN_TERMINAL_ALLOWED_DOMAINS` is set (adding `SETPCAP` to `cap_add` does not help — the effective set is the problem, not the bounding set). **No leading `v`** even though the GitHub release has one — upstream's `.github/workflows/docker.yml` publishes `0.13.0` / `0.13` / `latest` from `pyproject.toml`'s version, so `v0.13.0` 404s. Must be the default (fat, ~4 GB) variant: `slim` / `alpine` support neither `OPEN_TERMINAL_MULTI_USER` nor runtime package installs. Never pin `latest`. Bump → `make build open-terminal && make up open-terminal`; a build that stops in `patch_entrypoint.py` means upstream changed the script or merged the fix — see `ai/open-terminal/OPEN_TERMINAL.md § Custom image`, never force it. |
| `OPEN_TERMINAL_API_KEY` | _(unset — required)_ | Open Terminal: bearer token the terminal verifies. The SAME variable is interpolated into `TERMINAL_SERVER_CONNECTIONS` in `ai/openwebui/docker-compose.openwebui.yml`, so the two can never drift. Compose fails fast via `${OPEN_TERMINAL_API_KEY:?...}`; leaving it unset would make open-terminal self-generate a key and the connection would silently 401. Anyone holding it has a root-capable shell — generate with `openssl rand -hex 32` and treat it as a production password. |
| `OPEN_TERMINAL_MULTI_USER` | `true` | Open Terminal: per-user Linux accounts and home directories inside the one container, keyed off the `X-User-Id` the openwebui backend forwards. Upstream is explicit that this is a **workspace separation, not a security boundary** (one kernel, one process list, one network stack; sudo is available). Real per-user isolation needs Open WebUI Enterprise "Terminals". Setting it `false` also means changing the volume mount from `/home` to `/home/user`. |
| `OPEN_TERMINAL_ALLOWED_DOMAINS` | `pypi.org,files.pythonhosted.org,github.com,githubusercontent.com,registry.npmjs.org,deb.debian.org,security.debian.org,huggingface.co,hf.co` | Open Terminal: DNS-based egress allowlist enforced inside the container (dnsmasq NXDOMAINs the rest, resolved IPs go in an ipset, iptables OUTPUT drops the rest, then `CAP_NET_ADMIN` is dropped). Three-way and **empty ≠ off**: unset = no firewall, `""` = block all outbound, `a,b,*.c` = allowlist — and compose always passes the variable. `*.x` matches `x` plus subdomains. Anything reached by bare IP is unreachable regardless. Extending it needs a recreate (`make up open-terminal`), not a restart. Requires `cap_add: [NET_ADMIN]`. |
| `OPEN_TERMINAL_MAX_SESSIONS` | `16` | Open Terminal: cap on concurrent interactive PTY sessions across all users; `POST /api/terminals` returns 429 past it. |
| `OPEN_TERMINAL_EXECUTE_TIMEOUT` | _(empty)_ | Open Terminal: seconds `POST /execute` and `GET /execute/{id}/status` block inline before returning a process id the model must poll. Empty = return immediately. Set 5–15 if models keep forgetting to poll; the API caps per-request `wait` at 300. The compose file reads it as `${OPEN_TERMINAL_EXECUTE_TIMEOUT:-0}` **on purpose**: `open_terminal/env.py` calls `float()` whenever the variable is present, so a present-but-empty value crashes the server at import, while `0` is falsy at its only use site and behaves exactly like unset. |
| `OPEN_TERMINAL_MEM_LIMIT` / `_CPUS` / `_PIDS_LIMIT` | `4g` / `2` / `512` | Open Terminal: resource ceilings. Plain `docker compose` **ignores** `deploy.resources.limits` (a Swarm key), so the compose file uses the v2 service keys `mem_limit` / `cpus` / `pids_limit`, which do apply. The box also runs vLLM and llama.cpp; `pids_limit` is the fork-bomb guard. |
| `OPEN_TERMINAL_PIP_PACKAGES` | _(empty)_ | Open Terminal: space-separated pip packages installed on **every** container start (fat image only) — this runs before the server binds, so a long list delays readiness on each recreate. Bake heavy deps into a derived image instead. The base image already ships numpy/pandas/scipy/scikit-learn/matplotlib/plotly/requests/bs4/sqlalchemy/openpyxl/python-docx/python-pptx/pypdf. |
| `OPEN_TERMINAL_INFO` | _(operator context string)_ | Open Terminal: appended to the system prompt served at `GET /system`, which Open WebUI fetches once at startup and injects for any model with the terminal attached. Also creates the `GET /info` route — the route does not exist at all when this is empty. Keep it short; it costs tokens every turn. |
| `OPENWEBUI_ENABLE_CODE_INTERPRETER` | `false` | Open WebUI: the legacy in-browser Pyodide code interpreter, turned off because Open Terminal replaces it (and the composer force-clears the Code Interpreter toggle when a terminal is selected, so leaving both on just adds a dead button). PersistentConfig key `code_interpreter.enable` — first-boot-only on an existing `openwebui_data` volume; flip it at Admin Settings → Code Execution on this install. Distinct from `ENABLE_CODE_EXECUTION` (`code_execution.enable`, the browser-side "Run" button on a code block), which is deliberately left alone. |
| `PORT_TRINO` | `8013` | Trino data lake: host port mapped to the coordinator's **HTTPS** listener (`:8443`). Serves the web UI, REST API, and JDBC endpoint with password-file auth (`TRINO_JDBC_USERS`) behind a self-signed cert. The plain-HTTP `:8080` listener is username-only and is deliberately never published — see [ai/trino/TRINO.md § JDBC authentication](ai/trino/TRINO.md#jdbc-authentication). |
| `PORT_MINIO_API` / `PORT_MINIO_CONSOLE` | `8014` / `8015` | Trino data lake: MinIO S3-compatible API and web console. Console is also fronted at `chat.zeoenergy.com/minio/` via oauth2-proxy. |
| `PORT_SUPERSET` | `8016` | Trino data lake: host port `superset` binds to. Also fronted at `chat.zeoenergy.com/superset/` via oauth2-proxy. Uses `AUTH_TYPE=AUTH_REMOTE_USER` — restrict this port to loopback before exposing the host on an untrusted network (see [ai/trino/TRINO.md § Header-auth trust boundary](ai/trino/TRINO.md#header-auth-trust-boundary)). |
| `PORT_HMS_DB` / `PORT_SUPERSET_DB` | `5436` / `5437` | Trino data lake: Postgres backing `hive-metastore-db` and `superset-db`. Grouped with the other Postgres ports (`5432` litellm_db, `5433` roofix-db, `5434` sandbox-db, `5435` n8n-db, `5436` hive-metastore-db, `5437` superset-db). |
| `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` | `trino` / `trino-dev-only` | Trino data lake: MinIO root credentials — also used by HMS to sign s3a requests and by Trino's Iceberg connector. Same dev-default guidance as `ROOFIX_DB_*`: rotate before exposing `PORT_MINIO_API` on an untrusted network. Rotating both env vars is enough — HMS reads them from `env` at container start. |
| `HMS_DB_USER` / `_PASSWORD` / `_NAME` | `hive` / `hive` / `metastore` | Trino data lake: credentials for the compose-managed `hive-metastore-db` Postgres. Only HMS talks to this DB (Trino goes through HMS over thrift). |
| `SUPERSET_DB_USER` / `_PASSWORD` / `_NAME` | `superset` / `superset` / `superset` | Trino data lake: credentials for the compose-managed `superset-db` Postgres. Backs dashboards, saved queries, and Superset accounts. |
| `SUPERSET_SECRET_KEY` | _(unset — required)_ | Trino data lake: Flask session-cookie signing key. Rotate with `openssl rand -hex 32` before real use. |
| `SUPERSET_ADMIN_PASSWORD` | _(unset — required)_ | Trino data lake: bootstrap admin password. Used only on first container start (`ai/trino/superset/bootstrap.sh`) — subsequent logins go through the oauth2-proxy header. |
| `TRINO_MCP_MAX_ROWS` | `10000` | Trino data lake: `trino-mcp` clamps `run_query` results to this many rows (spliced `LIMIT` if the SELECT doesn't include one). Belt-and-suspenders with `query.max-execution-time` in `ai/trino/config/config.properties`. |
| `TRINO_MCP_MAX_RUNTIME_S` | `30` | Trino data lake: seconds `trino-mcp` forwards as `session_properties.query_max_execution_time` on each `run_query`. |
| `TRINO_JDBC_USERS` | _(unset — required)_ | Trino data lake: `user:password[,user2:password2]` accounts for the HTTPS listener on `PORT_TRINO` (DBeaver / DataGrip / web UI). The one-shot `trino-auth-init` service bcrypt-hashes them into `password.db` in the `trino_auth` volume on every run; the coordinator re-reads the file every 5 s, so rotating is edit `.env` + `make up trino trino-auth-init`, no restart. No `,` `:` or whitespace in passwords. Compose fails fast via `${TRINO_JDBC_USERS:?...}`. |
| `TRINO_TLS_SANS` | `DNS:trino-coordinator,DNS:localhost,IP:127.0.0.1` | Trino data lake: subjectAltName list for the self-signed cert `trino-auth-init` generates. Add `IP:<lan-ip>` / `DNS:<hostname>` so BI clients can use `SSLVerification=FULL` against the exported `trino.crt`. Only read when `tls/trino.pem` doesn't exist yet — rotation steps in [ai/trino/TRINO.md § Rotating the TLS cert](ai/trino/TRINO.md#rotating-the-tls-cert). |
| `TRINO_SHARED_SECRET` | _(unset — required)_ | Trino data lake: node-to-node secret Trino mandates once any client authentication is enabled, even single-node. Referenced from `ai/trino/config/config.properties` as `${ENV:TRINO_SHARED_SECRET}`. Generate with `openssl rand -hex 32`. |
| `SEMANTIC_ROUTER_VERSION` | `v0.3.0` | Semantic Router: pins **both** halves of the subsystem. It is the GHCR tag for `vllm-sr-router` (and the profile-gated dashboard), and — with the leading `v` stripped by `ai/semantic-router/Dockerfile.models-init` — the `vllm-sr` PyPI version the init container installs. That CLI owns the config schema (`vllm-sr validate`) and the Envoy template + generator, so the two must never drift: a newer validator would pass a config the pinned router rejects at startup. **Never `latest`** — GHCR republishes it several times a day and `up -d` will not re-pull a cached tag; the CLI's own default in `cli/consts.py` IS `latest`, do not copy it. Bumps need `make build semantic-router` then `make up semantic-router`, plus a re-render of `envoy.yaml` — `vllm-sr-models-init` fails the stack if you forget. |
| `SEMANTIC_ROUTER_ENVOY_VERSION` | `v1.34.14` | Semantic Router: Envoy image for `vllm-sr-envoy`. Pin a **patch** tag. `cli/consts.py` defaults to `envoyproxy/envoy:v1.34-latest`, which moves — same trap as `latest`, and a cached moving tag means a fresh box runs a different build than the one `envoy.yaml` was rendered against. Stay on the 1.34 line: the checked-in config is rendered against that release's ext_proc and Lua-filter schemas, so a minor bump needs a re-render and a re-read, not just a tag change. The image is Ubuntu 22.04 + `ca-certificates`/`tzdata` and ships **no curl or wget**, which is why the healthcheck is a bash `/dev/tcp` connect. |
| `SEMANTIC_ROUTER_LISTENER_KEY` | _(unset — required)_ | Semantic Router: bearer token Envoy requires on `PORT_SEMANTIC_ROUTER`. Generate with `openssl rand -hex 32`; compose fails fast via `${VAR:?...}`. LiteLLM sends it as the `api_key` on the `auto` alias, so a chat client never holds it. It lives in `.env` only: Envoy has **no** environment substitution in config files and vllm-sr's generator inlines `listeners[].api_keys` verbatim into a Lua table, so the checked-in `ai/semantic-router/envoy.yaml` carries the placeholder `__SEMANTIC_ROUTER_LISTENER_KEY__` and `vllm-sr-models-init` substitutes the real value into the copy Envoy loads. If every request 401s, that substitution did not run — read the init service's logs first. Rotate with: edit `.env`, `make up semantic-router` (a recreate, so the init re-runs), `make up litellm`. |
| `SEMANTIC_ROUTER_LITELLM_KEY` | _(unset — required)_ | Semantic Router: LiteLLM virtual key the **router** uses for its candidate sub-requests, resolved from the container env by `backend_refs[].api_key_env` in `config.yaml` (so it is never in YAML). Scope it in the LiteLLM Admin UI to exactly `qwen3.6`, `qwen3.8`, `qwen3.8-solo`, `qwen3.8-flash`, `glm5.2`, `claude-sonnet-5` and **nothing else** — that scoping is half the recursion guard, since LiteLLM → router → LiteLLM is a loop by design and a key that could also call `auto` would let a mis-edited config spin until the 1800 s listener timeout. Put a monthly budget on it: one `remom` turn is six model calls and Anthropic is in the pool. It is **also** a valid bearer on `PORT_SEMANTIC_ROUTER`: the router's looper client signs its own sub-requests with the candidate's access key (this key), those sub-requests re-enter the Envoy listener, and the Lua filter would 401 them otherwise — so `vllm-sr-models-init` substitutes it into the Envoy key table next to the listener key. Compose fails fast via `${VAR:?...}`. |
| `PORT_SEMANTIC_ROUTER` / `_DASHBOARD` | `8021` / `8022` | Semantic Router: `8021` maps to Envoy's listener (container `8899`) and is the **only** published port in the stack. The router's `50051` (ext_proc gRPC), `8080` (management API) and `9190` (metrics) plus Envoy's admin `9901` are never mapped — `9901` alone serves `/quitquitquit` and a full config dump including the listener bearer token; prometheus reaches `9190` by service DNS over `ai_shared` instead. `8022` is the optional dashboard, bound to `127.0.0.1` only and gated behind a compose `profiles: [dashboard]`; it can edit the live router config and has no auth of its own, so reach it over an SSH tunnel or front it at `chat.zeoenergy.com/vllm-sr/` via `OAUTH2_PROXY_UPSTREAMS` the way `/n8n/` is done. |

## Threading Model — Read Before Touching Anything

This is the most likely place to introduce bugs. Three threads run concurrently:

1. **Main thread** — pystray event loop (`icon.run()`). Blocking this freezes the tray. All tray menu callbacks must spawn daemon threads immediately.
2. **Popup thread** — tkinter `mainloop()` in a daemon thread spawned on tray click. **All tkinter calls must happen on this thread.** Use `_win.after(0, fn)` to schedule from anywhere else — direct calls from other threads crash or hang.
3. **Fetcher thread** — `BrowserLinker._loop()` runs forever; when data arrives it calls `popup.update()`, which uses `after()` internally to stay safe.

## Browser / CDP — Non-Obvious Constraints

- CDP requires **an already-running Chrome instance** with `--remote-debugging-port=9222`. The app launches Chrome itself via `chrome_launcher.py`; it does not use Selenium.
- The 4-second sleep at the start of `_loop()` waits for Chrome to open the tab. Removing it causes reliable connection failures on startup.
- `interceptor.js` is read from disk **once at startup** and cached as a string. Editing the file while the app is running has no effect — restart required.
- **Do not reformat `interceptor.js`.** It is injected verbatim into the page as a CDP parameter. Reformatting can silently change behavior or break string injection.
- The interceptor uses `response.clone()` before reading the body. Removing this gives the page an empty body — the site breaks.
- The `_fetchInterceptorActive` guard prevents double-patching on re-injection. Do not remove it.
- If `requests` or `websocket-client` are uninstallable/missing, the entire account-stats feature silently disables — no error is raised.

## LLM Backend Toggle — Files Modified

`backend.py` modifies two files outside the repo:

- `~/.claude/settings.json` — adds/removes `env` overrides pointing at local llama-server
- `~/.claude.json` — swaps `primaryApiKey` to a dummy key

These are read-modify-write operations. If either file is open/locked by another process the operation may fail silently. After toggling, verify with `is_local_llm_active()`.

`stop_server()` calls `terminate()` but does not wait for exit — the process may briefly linger. There is no automatic cleanup on app quit; the llama-server process becomes orphaned if the user closes the tray without explicitly stopping it.

## State Files (Outside Repo)

| Path | Purpose |
|---|---|
| `~/.claude/projects/**/*.jsonl` | Claude Code session logs — read-only by this app |
| `~/.claude_widget/chrome_profile/` | Chrome profile used for account stats session |
| `~/.claude/settings.json` | Modified by LLM backend toggle |
| `~/.claude.json` | Modified by LLM backend toggle |

The Chrome profile directory contains a singleton lock file. If Chrome crashes without cleanup, the lock may persist and cause session reuse issues on next launch.

## Headless Session Logic

After a successful login, `fetcher.py` writes a sentinel file. On next launch, Chrome starts headless. If the headless session expires (login timeout), the code catches the error, deletes the sentinel, relaunches Chrome visibly, and sets status to `"waiting_login"`. Calling `go_headless()` before a successful login is a silent no-op.

## Stale / Unused Dependencies

`pyproject.toml` lists `selenium`, `trio`, and `trio-websocket` — none are used. The CDP approach replaced Selenium; `trio` is a legacy leftover. Safe to remove if cleaning up.

## No Tests

There is no test suite. Verify changes manually by running the app and checking the popup displays correct data. Use `cdp_spy.py` to verify CDP captures independently of the full app.

## LiteLLM with Phoenix MCP (Tool Calling)

The Phoenix MCP server exposes database tools via LiteLLM. The model receives tool definitions but LiteLLM does **not** execute the tool calls automatically — you must orchestrate the tool call loop.

### Step 1: Send the user message

```bash
curl http://localhost:4001/v1/chat/completions \
  -H "Authorization: Bearer sk-your-master-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3.6",
    "messages": [{"role": "user", "content": "List all tables in the database"}]
  }'
```

The response will have `"finish_reason": "tool_calls"` with a tool call object.

### Step 2: Send the tool result back

Use the `tool_call_id` from the response and call the MCP tool directly, then send the result back:

```bash
curl http://localhost:4001/v1/chat/completions \
  -H "Authorization: Bearer sk-your-master-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3.6",
    "messages": [
      {"role": "user", "content": "List all tables in the database"},
      {"role": "assistant", "tool_calls": [{"function": {"arguments": "{}", "name": "list_tables"}, "id": "CALL_ID_FROM_STEP_1", "type": "function"}]},
      {"role": "tool", "tool_call_id": "CALL_ID_FROM_STEP_1", "content": "[\"projects\", \"users\"]"}
    ]
  }'
```

Replace the `content` with the actual result from calling the tool on the Phoenix MCP server (`https://phoenix-mcp.com/mcp`).

### Phoenix MCP API Token

The Phoenix MCP server issues long-lived API tokens via a browser-based OAuth flow:

```bash
# Get a Google login URL
curl -s https://phoenix-mcp.com/api-token \
  -H "X-API-Key: your-shared-secret"
# Open the returned login_url in a browser, sign in, get your API token

# Or exchange a Google access token directly
curl -X POST https://phoenix-mcp.com/api-token \
  -H "X-API-Key: your-shared-secret" \
  -H "Content-Type: application/json" \
  -d '{"google_token": "<google-access-token>", "expires_in": 0}'
```

## Common Pitfalls

| Pitfall | Effect |
|---|---|
| Calling tkinter methods from background thread without `after()` | Crash or silent hang |
| Editing `interceptor.js` without restarting | No effect on running app |
| Reformatting `interceptor.js` | Breaks string injection |
| Removing the 4-second sleep in `_loop()` | CDP connection fails on startup |
| Calling `go_headless()` before first successful login | Silent no-op |
| Changing `LLM_URL` without re-toggling LLM mode | `is_local_llm_active()` returns false |
| Closing app without stopping llama-server | Orphaned server process |
| Editing `.env` while app is running | No effect until restart |

## Roofix ↔ Phoenix Bridge

`ai/roofix/` bundles the Roofix ↔ Phoenix subsystem: `bridge/` (event-sourced worker) and `scraper/` (Playwright proposal fetcher). One compose file (`docker-compose.roofix.yml`) brings up both. See [`ai/roofix/ROOFIX.md`](ai/roofix/ROOFIX.md) for the operator guide; a few things worth calling out here:

- **Default `DRY_RUN=true`**: on first deploy the bridge fetches Gmail, parses, decides, and logs — but does **not** write to Phoenix. Flip to `false` only after inspecting a full tick.
- **Bridge talks to Phoenix directly (psycopg2), not via MCP**: the earlier MCP variant was reverted because Phoenix MCP write tools weren't ready in time. Bridge needs `PHOENIX_DB_*` env vars set. `DRY_RUN=true` still short-circuits writes.
- **Bridge talks to Gmail directly (Google API + OAuth), not via MCP**: same reason. Requires `credentials.json` + `token.json` in `ROOFIX_BRIDGE_CONFIG_DIR`. First-time login is interactive — run `python components/gmail_client.py` locally once before shipping the token file into the container. See [ROOFIX.md § Gmail OAuth setup](ai/roofix/ROOFIX.md#gmail-oauth-setup).
- **AI fallback via LiteLLM**: `roofix/components/brain.py::generate_ai_decision` uses the OpenAI SDK against `http://litellm:4000`. Swapping Claude for a local vLLM model is a LiteLLM config change, not a bridge code change.
- **Session refresh is a manual operator flow**: the scraper cannot present a login UI. Run `save_roofix_session.py` locally on a laptop with a visible browser, then POST the resulting JSON to the scraper's `/session/refresh`.
- **Michael's mapping**: `ai/roofix/config/field_mapping.json` is a stub. Milestone writes will log "no milestone mapping" and skip until the file is filled in — this is intentional.

## Sandbox subsystem

`ai/sandbox/` runs untrusted, model-generated web apps (Streamlit, Gradio, Flask, FastAPI, Vite+React, Next, Express, static HTML) in short-lived isolated containers and exposes them to Open WebUI as iframe artifacts — the "artifacts panel" pattern, but for anything a real runtime can run. `sandbox-runner` is FastAPI + FastMCP; the model calls the `sandbox.run(runtime, files, entrypoint)` MCP tool (or the pipelined `create` → `write_files` → `preview` flow) via LiteLLM's MCP registration and gets back a URL to iframe. See [`ai/sandbox/SANDBOX.md`](ai/sandbox/SANDBOX.md) for the operator guide. A few things worth calling out here:

- **Network segmentation is the primary security control, not container hardening.** The subsystem uses FOUR Docker networks: `ai_shared` (external, so openwebui/litellm can reach the runner + proxy), `sandbox_net` (bridge, `internal: true` — sandboxed containers + the egress proxy), `sandbox_state` (bridge, `internal: true` — runner ↔ sandbox-db only), and `sandbox_egress_out` (bridge, NOT internal — the egress proxy's ONLY route to the public internet; nothing else attaches here). `internal: true` means Docker attaches no default gateway; a sandbox that tries to reach `litellm:4000` or `phoenix-mcp` gets no route, and the only escape hatch to the internet is `sandbox-egress`, which enforces the allowlist. If you add a service to `ai/sandbox/docker-compose.sandbox.yml`, keep the network memberships minimal and re-run the security-invariant checklist in SANDBOX.md.
- **`sandbox-runner` is the only container with `docker.sock` access.** It's the audit boundary. Any code that opens the socket must live in `ai/sandbox/runner/spawner.py` — that's the review focal point.
- **Egress is allowlisted, not blocked-by-default.** `sandbox-egress` (tinyproxy) reads [`ai/sandbox/proxies/tinyproxy.filter`](ai/sandbox/proxies/tinyproxy.filter) with `FilterDefaultDeny Yes`. Adding a new dep source is a filter-file edit + `docker compose restart sandbox-egress`. Never add `.*` — that defeats the model.
- **Base image port is always 80.** All runtime templates in `ai/sandbox/runner/runtimes.py` bind port 80 so `sandbox-proxy` (Caddy) can statically route `/{sandbox_id}/* → sandbox-{id}:80` with no dynamic config. Adding a new runtime that listens on something else means also adding dynamic Caddy admin-API management — don't unless you have to.
- **Job state is Postgres (`sandbox-db`), not SQLite.** Concurrent writers, JSONB metadata for operator queries via `psql`, and the DB lives on its own `sandbox_state` network so a container escape in a sandbox can't tamper with job records. `common.jobs.PostgresRegistry` is the reusable backend — any future service that wants Postgres-backed jobs can adopt it.
- **Sessions + idle-TTL.** `create` / `run` return a `session_id` on first call; pass it back on `write_files` / `patch_files` / `exec` / `preview` to reuse the same running container. The reaper enforces both `SANDBOX_HARD_TTL_SECONDS` (absolute) and `SANDBOX_IDLE_TTL_SECONDS` (bumped on every session-reuse call via `metadata->>'last_used_at'`). Explicit teardown via `close(session_id)` MCP tool, `DELETE /session/{id}`, or the existing `DELETE /jobs/{id}`.

## n8n subsystem

`ai/n8n/` runs a self-hosted [n8n](https://n8n.io) workflow engine — used for stitching together LiteLLM, Roofix, MCP servers, and external HTTP surfaces without writing bespoke code for each automation. Compose file: [`ai/n8n/docker-compose.n8n.yml`](ai/n8n/docker-compose.n8n.yml). Operator guide: [`ai/n8n/N8N.md`](ai/n8n/N8N.md). A few things worth calling out here:

- **Reached at `/n8n/` under the shared oauth2-proxy, not a dedicated hostname.** `OAUTH2_PROXY_UPSTREAMS` in `.env` includes `http://n8n:5678/n8n/` — oauth2-proxy's longest-prefix routing sends `/n8n/*` to the n8n container, same pattern the sandbox subsystem uses for `/sandboxes/*`. The n8n container's `N8N_PATH`, `N8N_EDITOR_BASE_URL`, and `N8N_WEBHOOK_URL` env vars (all in `.env`) tell it it's mounted at that subpath and MUST stay in sync with `OAUTH2_PROXY_UPSTREAMS` — changing one without the other silently breaks routing. The shared oauth2-proxy cookie means one Google sign-in covers both Open WebUI and n8n. **No Cloudflare dashboard change is needed** — same hostname, new path prefix.
- **All container config lives in `.env`, not the compose file.** `ai/n8n/docker-compose.n8n.yml` is a pure `${…}` template — same discipline as `ai/openwebui/docker-compose.openwebui.yml`. Edit `.env` (the `## n8n subsystem` block), not the compose YAML.
- **`N8N_ENCRYPTION_KEY` is load-bearing and must persist across restarts.** It encrypts every stored credential blob in `n8n-db`. Rotating or losing it does NOT destroy workflow rows but permanently corrupts the encrypted credential fields — every saved OAuth token, API key, and password becomes unreadable. Generate with `openssl rand -hex 32`, paste into `.env` before the first start, back it up. The compose file uses `${N8N_ENCRYPTION_KEY:?...}` to fail fast if unset.
- **AI nodes are preconfigured against LiteLLM.** `N8N_AI_OPENAI_API_BASE` (in `.env`) points at `http://litellm:4000/v1` and the API key is sourced from `DEFAULT_LITELLM_MASTER_KEY`, so the OpenAI / LangChain credential picker defaults to the local model list — operators don't paste the base URL or key per workflow. Only override at the credential level when targeting an external provider (real OpenAI, Anthropic direct, etc.).
- **No queue mode / Redis broker.** Single-process runtime is enough for the current prototype workload; if execution backlog becomes real, flip `EXECUTIONS_MODE=queue` and add a Redis service — the Postgres backing DB already supports that migration.

## Open Terminal subsystem

`ai/open-terminal/` runs [Open Terminal](https://github.com/open-webui/open-terminal) (`ghcr.io/open-webui/open-terminal`) as the code-execution backend for Open WebUI, replacing the legacy in-browser Pyodide code interpreter: a real Linux container with Python, Node, gcc, ffmpeg and LibreOffice, a persistent per-user `/home`, wired into the chat as native function-calling tools plus a file-browser sidebar, upload target, and interactive terminal pane. Compose file: [`ai/open-terminal/docker-compose.open-terminal.yml`](ai/open-terminal/docker-compose.open-terminal.yml). Operator guide: [`ai/open-terminal/OPEN_TERMINAL.md`](ai/open-terminal/OPEN_TERMINAL.md). A few things worth calling out here:

- **Never on `ai_shared`, and never with `docker.sock`.** This is a root-capable shell a model drives. It joins a dedicated plain bridge, `terminal_net` (created by `make network`), whose only other member is `openwebui`. From `ai_shared` it could reach `roofix-db` / `sandbox-db` / `n8n-db` / `minio` on their dev-default credentials, `litellm`, n8n's encrypted credential store, and `sandbox-runner` — which mounts `docker.sock`. The upstream image ships the Docker CLI and the README documents mounting the socket; doing that here would hand a model root on the host and make every other control decorative. `terminal_net` is NOT `internal: true` (unlike `sandbox_net`) because the fat image's whole point is runtime `pip` / `apt`; egress is narrowed inside the container by `OPEN_TERMINAL_ALLOWED_DOMAINS` instead, which needs `cap_add: [NET_ADMIN]`. Do **not** add `security_opt: [no-new-privileges:true]` — the image runs as an unprivileged user and needs passwordless sudo for the firewall, package installs, and account provisioning.
- **No host port, on purpose.** Every request is proxied by the Open WebUI *backend* (`backend/open_webui/routers/terminals.py`), which attaches `Authorization: Bearer` and `X-User-Id` server-side, so the API key never reaches a browser. Debug with `docker exec openwebui curl …`; the Postman collection documents the temporary-loopback and socat alternatives. Never bind `0.0.0.0` — the bearer token would be the only thing between the LAN and a shell.
- **Multi-user mode is a workspace separation, not a security boundary.** `OPEN_TERMINAL_MULTI_USER=true` maps each Open WebUI user id to a Linux account under `/home` (the `open_terminal_home` volume). Upstream says plainly that one kernel / one process list / one network stack means users are not protected from each other, and account provisioning needs privileges inside the container. Everyone with chat access must be trusted at the same level; real isolation is Open WebUI Enterprise "Terminals" (container-per-user, `server_type: "orchestrator"` + `policy_id` on the same connection shape).
- **Two first-boot-only PersistentConfig values.** `TERMINAL_SERVER_CONNECTIONS` (`terminal_server.connections`) and `ENABLE_CODE_INTERPRETER` (`code_interpreter.enable`) are read from env only on the first boot of a fresh `openwebui_data` volume — same trap as `ENABLE_WEB_SEARCH` and the `AUDIO_TTS_*` block. On this install set them at Admin Settings → Integrations → Open Terminal and Admin Settings → Code Execution. The connection JSON is built INLINE in `ai/openwebui/docker-compose.openwebui.yml` so `OPEN_TERMINAL_API_KEY` lives in exactly one place and the two containers can't drift.
- **Access control is `config.access_grants`, not `access_control`.** `utils/access_control.py::has_connection_access` treats a missing or empty list as *private, admin-only*, so a connection with no grants is invisible to regular users. Public read is `{"principal_type": "user", "principal_id": "*", "permission": "read"}`; group scoping swaps in `principal_type: "group"` with the group id.
- **Terminal tools are native function calling only.** A model configured with `function_calling: legacy` receives none of them — the terminal pill shows up and does nothing. Set those models to Native in Workspace → Models. Selecting a terminal also force-clears the Code Interpreter toggle in the composer, which is why `OPENWEBUI_ENABLE_CODE_INTERPRETER=false`; `ENABLE_CODE_EXECUTION` (the browser-side "Run" button on a code block) is deliberately left alone.
- **Adding `terminal_net` to `openwebui` changes its network list**, so applying it is `make up openwebui` (a recreate), not a restart.
- **The image is locally built (`open-terminal-zeo:<tag>`), same pattern as `openwebui-zeo`.** Upstream's entrypoint crashes at boot with `unable to raise CAP_SETPCAP for BSET changes` whenever the egress allowlist is set, because it calls `capsh` as the unprivileged user (open-terminal#119; fix PR #118 unmerged as of 0.13.0). `ai/open-terminal/Dockerfile.open-terminal` + `patch_entrypoint.py` apply that fix (`sudo -E capsh … --user=user`) and fail the build if the script has changed shape. When #118 ships, retire the Dockerfile and point the compose back at the upstream image — don't keep patching a fixed base.

## Trino data lake

`ai/trino/` bundles a Trino coordinator + Hive Metastore (+ its Postgres) + MinIO + Superset (+ its Postgres) + a FastMCP shim (`trino-mcp`) registered with LiteLLM. Federates SQL over the three existing Postgres instances (`litellm_db`, `roofix-db`, `sandbox-db`) plus an Iceberg lakehouse on MinIO. One compose file (`ai/trino/docker-compose.trino.yml`) brings up the whole stack. See [`ai/trino/TRINO.md`](ai/trino/TRINO.md) for the operator guide; a few things worth calling out here:

- **Two networks: `ai_shared` + `analytics_net`.** Data-plane traffic (Trino ↔ HMS ↔ HMS Postgres) stays on `analytics_net`. `ai_shared` carries only the surfaces external callers reach: `trino-coordinator` (JDBC / web UI), `minio` (S3 API), `superset` (UI), and `trino-mcp` (LiteLLM MCP endpoint). `analytics_net` is a plain bridge — not `internal: true` — because MinIO already publishes host ports on `ai_shared`, so hardening the data plane wouldn't gain anything and would complicate future ingestion jobs.
- **Catalogs are `.properties` files, not compose services.** Adding a new data source (real S3, GCS, BigQuery, or another Postgres) is: drop a file in `ai/trino/catalogs/`, reference its secrets as `${ENV:VAR}`, add those vars to `trino-coordinator`'s `environment:` block in the compose file AND to `.env` + `.env.example`, then `make up trino trino-coordinator` (recreate, not restart — the container env changed). Never add a compose *service* for a catalog. Hosted Postgres needs `?sslmode=require` on the URL; Supabase must use its session-pooler host (the direct host is IPv6-only); a `$` in any `.env` value must be written `$$`. Full recipe and templates in [ai/trino/TRINO.md § Adding a catalog](ai/trino/TRINO.md#adding-a-catalog).
- **Postgres federation reaches across subsystems via `host.docker.internal`, not Docker DNS.** `litellm_db` and `sandbox-db` don't live on `ai_shared`; the sandbox DB is deliberately isolated on `sandbox_state`. Trino reaches both through the host publish (`localhost:5432`, `localhost:5434`) to avoid breaching subsystem isolation. `roofix-db` is on `ai_shared` and uses service DNS. If you ever want to unpublish `PORT_SANDBOX_DB`, the sandbox catalog stops working — that's intentional; the operator flips a `.env` port to sever the read path.
- **`trino-mcp` is SELECT-only by default.** `common.trino.TrinoClient` rejects everything except `SELECT`/`WITH`/`SHOW`/`DESCRIBE`/`EXPLAIN` unless `allow_writes=True` is set — only `ai/trino/bin/init_warehouse.py` does that. Rows are clamped to `TRINO_MCP_MAX_ROWS` (spliced `LIMIT` if the SELECT doesn't include one); runtime is clamped to `TRINO_MCP_MAX_RUNTIME_S` via `session_properties.query_max_execution_time`. The coordinator's own `query.max-execution-time` in `ai/trino/config/config.properties` is the belt-and-suspenders backup.
- **The coordinator has two listeners with different trust models — never publish the HTTP one.** `:8443` (HTTPS, password-file auth, self-signed cert) is what `PORT_TRINO` maps to; `:8080` (plain HTTP) is re-enabled via `http-server.authentication.allow-insecure-over-http=true` and authenticates by username only, exactly like every other service on `ai_shared`. `trino-mcp`, Superset, `init_warehouse.py`, and the healthcheck all use 8080 by Docker DNS. Adding an 8080 `ports:` mapping would hand LAN users a password-free bypass. Cert + `password.db` come from the one-shot `trino-auth-init` service (`ai/trino/auth-init/`) into the `trino_auth` volume; users are `TRINO_JDBC_USERS` in `.env`.
- **Superset uses `AUTH_TYPE=AUTH_REMOTE_USER` — the header-auth trust boundary is load-bearing.** oauth2-proxy sets `X-Auth-Request-Email` on every authenticated request; Superset trusts it as the session user with NO password check. If `PORT_SUPERSET` (default 8016) is bound to `0.0.0.0`, anyone on the LAN can spoof the header and log in as any Superset account. Before exposing this on an untrusted network, bind the port to loopback in `docker-compose.trino.yml`, or drop the `ports:` block entirely and reach Superset only via `chat.zeoenergy.com/superset/`. Same trust posture as Open WebUI's `WEBUI_AUTH_TRUSTED_EMAIL_HEADER`.
- **Iceberg on MinIO uses `s3a://warehouse/`, not `s3://`.** The Hive Metastore's `hive.metastore.warehouse.dir` points at `s3a://warehouse/`, and every `CREATE SCHEMA` should include an explicit `WITH (location = 's3a://warehouse/<schema>')` — matches HMS's default filesystem, avoids the scheme-mismatch that would happen if Trino tried to write via the native S3 connector while HMS validated with s3a. `ai/trino/bin/init_warehouse.py` follows this convention.

## Semantic Router subsystem

`ai/semantic-router/` runs [vLLM Semantic Router](https://github.com/vllm-project/semantic-router) (`vllm-sr`) **beside** LiteLLM, not in front of it: LiteLLM gains one alias, `auto`, and the router turns each request into a *decision* (matched on signals extracted from the prompt) and then runs a bounded multi-model "looper" algorithm over the aliases this stack already serves. Two long-lived containers — `vllm-sr-router` (an Envoy ext_proc server) and `vllm-sr-envoy` (the only OpenAI surface) — plus a one-shot `vllm-sr-models-init`. Compose file: [`ai/semantic-router/docker-compose.semantic-router.yml`](ai/semantic-router/docker-compose.semantic-router.yml). Operator guide: [`ai/semantic-router/SEMANTIC_ROUTER.md`](ai/semantic-router/SEMANTIC_ROUTER.md). A few things worth calling out here:

- **LiteLLM → router → LiteLLM is a loop, and the recursion guard is two-sided.** Every candidate the router picks is an existing LiteLLM alias at `http://litellm:4000/v1`, and looper sub-requests go back through the Envoy listener so Envoy can resolve each candidate by the `x-selected-model` header. Guard one: `providers.models` in `config.yaml` lists **only** concrete aliases — never `auto`, never any `vllm-sr/*` name, because every entry there becomes an Envoy route and a router entrypoint listed there would route the router's own sub-requests into itself. Guard two: `SEMANTIC_ROUTER_LITELLM_KEY` is a virtual key scoped to the six concrete aliases and nothing else, so a mis-edit 401s instead of spinning to the 1800 s timeout. Direct-to-vLLM/llama.cpp was rejected deliberately — it would bypass LiteLLM's fallbacks (`qwen3.8` ↔ `muse-glimmer` are mutually exclusive on the GPU pair), spend logs, virtual keys, and per-model sampling defaults.
- **v0.3.0 ships three looper algorithms, not five. Fusion and Workflows do not exist.** `cli/validator.py` enumerates `{confidence, ratings, remom}` and `AlgorithmConfig` is `extra="forbid"`, so writing a `fusion:` or `workflows:` block is a hard validation failure, not an ignored key. There is also **no `entrypoints:` block and no `recipes:` block** — `UserConfig` is `extra="forbid"` over exactly `{version, listeners, providers, routing, global, setup}`. Exactly one model name triggers routing (`global.router.auto_model_name`, set to `vllm-sr/auto`); the algorithm is then chosen by which decision the request's signals match, which is why LiteLLM has `auto` and no `auto-fusion` / `auto-remom` / `auto-flow` / `auto-ratings` siblings. Context limits live on `routing.modelCards[].context_window_size`, not on `providers.models` — `Model` has no context field at all.
- **`envoy.yaml` is GENERATED, never hand-written.** It is rendered from `config.yaml` by the pinned release's own Jinja template (`cli/templates/envoy.template.yaml`). `vllm-sr-models-init` re-renders on every `make up semantic-router` and **fails the stack** if the checked-in file differs, so a `SEMANTIC_ROUTER_VERSION` bump or a `config.yaml` edit cannot ship a stale Envoy config. Re-render per `SEMANTIC_ROUTER.md § Re-rendering envoy.yaml`; never edit it by hand and never work around the check.
- **The init service does not download models — there is no such subcommand.** The full CLI is `serve / config / validate / model list / eval / status / logs / stop / dashboard / chat`; `vllm-sr serve` only `mkdir`s a models directory and mounts it. The **router image** pulls its own classifier bundles from Hugging Face on first start, which is why `HF_TOKEN` / `HF_HOME` go to `vllm-sr-router` and why its healthcheck has a 600 s `start_period`. `vllm-sr-models-init` instead validates the config, enforces the Envoy re-render, substitutes the listener key, and seeds the volume.
- **The listener bearer token cannot live in the config files.** Envoy has no environment substitution in config YAML, and the generator inlines `listeners[].api_keys` verbatim into a Lua filter — so both `config.yaml` and `envoy.yaml` carry the placeholder `__SEMANTIC_ROUTER_LISTENER_KEY__`, and the init service writes the substituted copy into the `vllm_sr_envoy` volume that Envoy actually mounts. That indirection is the whole reason `vllm-sr-envoy` mounts a volume rather than a bind mount. Universal 401s mean the init step did not run.
- **Time to first token is the slowest candidate, not the fastest.** Envoy runs ext_proc with `BUFFERED` request *and* response bodies, so a `ratings` or `remom` turn returns nothing at all until the whole round finishes. `stream_timeout: 1800` on the `auto` alias and `listeners[0].timeout: 1800s` must stay in step. Set expectations in the Open WebUI model card; `ratings` also returns several `choices` and Open WebUI renders only the first.
- **Confidence thresholds for `avg_logprob` are NEGATIVE.** Closer to zero means more confident (`ConfidenceAlgorithmConfig`, default `-1.0`). A positive threshold makes every answer look confident and the escalation ladder never fires. `claude-sonnet-5` sits last in the ladder on purpose — Anthropic returns no logprobs, so it can be arrived at but never scored; moving it off the tail requires switching `confidence_method` to `margin`.
- **`glm5.3-flash` and `muse-glimmer` are excluded from the pool on purpose.** `glm5.3-flash` is CPU-only with one slot and minutes to first token — it would block a parallel round for the whole timeout. `muse-glimmer` shares its GPU pair with `qwen3.8` and cannot co-run; the router health-checks LiteLLM, not the model behind it, so it would keep selecting something that is not running. Parallel fan-out is budgeted against real slot counts: `max_concurrent: 2` on the code decision exists because `qwen3.8-flash` has exactly 2 llama.cpp slots.
- **Only Envoy's listener is published.** `PORT_SEMANTIC_ROUTER` (8021) → container 8899. The router's `50051` / `8080` / `9190` and Envoy's admin `9901` are never mapped — `9901` serves `/quitquitquit` and a config dump containing the listener key. `router_net` (bridge, `internal: true`) carries the ext_proc hop, but note the honest caveat in the compose file: because both containers also sit on `ai_shared` (prometheus needs `9190`, the router needs a gateway for Hugging Face), it does not actually hide `50051` from `ai_shared` peers.

Any Python package or module that could plausibly be reused across multiple projects — current or future — MUST live in `shared/common/`, not in the project directory that first needs it. This includes: scraping / CDP / browser helpers, MCP protocol clients, LiteLLM / model client wrappers, env + logging boilerplate, and cross-cutting utilities.

**Test:** before creating a new module inside `widget/`, `ai/roofix/`, `ai/interceptor/`, or any future project dir, ask *"could a second project want this in six months?"* If yes, it goes in `shared/common/` under an appropriate subpackage and the project imports it via the uv workspace (`common = { workspace = true }` in the project's `pyproject.toml`, backed by the root `pyproject.toml`'s `[tool.uv.workspace]` declaration).

Project-specific business logic (Roofix event parsing, brain decision rules, widget's tray UI, etc.) stays in the project directory — the test is reusability, not size.

Adding a new capability to `shared/common/`: create the subpackage under `shared/common/src/common/<name>/`, expose the public API from its `__init__.py`, add tests under `shared/common/tests/`. No pyproject changes needed in consuming projects unless a new external dep is introduced.

**Current shared subpackages:**
- `common.cdp_interceptor` — Chrome DevTools Protocol interceptor (used by `interceptor`, `widget`)
- `common.env` — walk-up `.env` loader
- `common.logging_setup` — CSV audit logger + stdlib configuration
- `common.processed_store` — Gmail message-id dedup cache (used by `roofix`)
- `common.jobs` — id-addressable job tracking with three backends: `InMemoryRegistry` (sync, ephemeral — used by `interceptor`), `SqliteRegistry` (async, `aiosqlite`, persistent — used by `classifier`), and `PostgresRegistry` (async, `asyncpg`, persistent, JSONB metadata, real connection pool — used by `sandbox`). The two persistent backends also act as a durable FIFO work queue via `claim_next()` (atomic across tasks and processes), `reset_phase()` (startup crash recovery), and `count_by_phase()` (queue-depth gauges); `common.jobs.worker.WorkerPool` runs N claim loops against them (wake/poll, crash recovery, `on_finish` metrics hook) and `common.jobs.payloads.FilePayloadStore` keeps oversized job inputs on disk so queued work survives restarts — `classifier/workers.py` wires the two together. Plus a `build_router` FastAPI factory for the standard `GET /jobs`, `GET /jobs/{id}`, `POST /jobs/{id}/cancel`, `DELETE /jobs/{id}` endpoints (auto-detects sync vs async). See [`shared/common/src/common/jobs/__init__.py`](shared/common/src/common/jobs/__init__.py) for backend selection guidance.

## AI Infrastructure — Compose Topology

The Docker Compose services in `ai/` are documented in [`ai/AI_INFRA.md`](ai/AI_INFRA.md), which contains:

- A table linking every `docker-compose.*.yml` to its README.
- A Mermaid flow diagram showing how the products connect (traffic ingress → oauth2-proxy → openwebui → LiteLLM → vLLM/Kokoro/MADLAD/classifier, plus auxiliary flows).
- A consolidated host-port table.

**Maintenance rule — keep the diagram in sync.** Whenever a new `docker-compose.*.yml` file is added under `ai/` (or an existing one is renamed, removed, or has its services / ports / cross-service dependencies changed), update `ai/AI_INFRA.md` in the same change:

1. Add / update / remove the row in the **Compose files** table, with a link to the compose file and to its README (create the README if none exists).
2. Add / update / remove the corresponding node in the Mermaid **Flow diagram** — including edges for every runtime dependency (e.g. `service X calls service Y over ai_shared`).
3. Update the **Ports at a glance** table with the new host port.
4. If the service participates in the public traffic path (Cloudflare → oauth2-proxy → …), extend the "Reading the diagram" bullets so the new hop is called out.

The diagram is the single source of truth for how the AI infrastructure fits together — do not add a new compose file without updating it.

## Postman collections — one per API service

Every service under `ai/` that exposes an HTTP API ships a Postman v2.1 collection alongside its docs — importable directly into Postman for hands-on debugging. The canonical example is [`ai/sandbox/sandbox-runner.postman_collection.json`](ai/sandbox/sandbox-runner.postman_collection.json); use it as the structural template (collection-level bearer auth wired to a `virtual master key` variable, a `litellm` base-URL variable, one `item` per endpoint with a real request body, an `MCP` subfolder when the service exposes JSON-RPC over HTTP).

**Current collections:**

| Service | Collection | Endpoints doc |
|---|---|---|
| sandbox-runner | [`ai/sandbox/sandbox-runner.postman_collection.json`](ai/sandbox/sandbox-runner.postman_collection.json) | [`ai/sandbox/ENDPOINTS.md`](ai/sandbox/ENDPOINTS.md) |
| interceptor | [`ai/interceptor/interceptor.postman_collection.json`](ai/interceptor/interceptor.postman_collection.json) | [`ai/interceptor/INTERCEPTOR.md`](ai/interceptor/INTERCEPTOR.md) |
| classifier | [`ai/classifier/classifier.postman_collection.json`](ai/classifier/classifier.postman_collection.json) | [`ai/classifier/API.md`](ai/classifier/API.md) |
| roofix bridge | [`ai/roofix/roofix.postman_collection.json`](ai/roofix/roofix.postman_collection.json) | [`ai/roofix/ROOFIX.md`](ai/roofix/ROOFIX.md) |
| kokoro (TTS) | [`ai/kokoro/kokoro.postman_collection.json`](ai/kokoro/kokoro.postman_collection.json) | [`ai/kokoro/KOKORO.md`](ai/kokoro/KOKORO.md) |
| madlad (translate) | [`ai/madlad/madlad.postman_collection.json`](ai/madlad/madlad.postman_collection.json) | [`ai/madlad/MADLAD.md`](ai/madlad/MADLAD.md) |
| litellm proxy | [`ai/litellm/litellm.postman_collection.json`](ai/litellm/litellm.postman_collection.json) | [`ai/litellm/LITELLM.md`](ai/litellm/LITELLM.md), [`ai/litellm/LITELLM_MCP.md`](ai/litellm/LITELLM_MCP.md) |
| vllm (per model) | [`ai/vllm/vllm.postman_collection.json`](ai/vllm/vllm.postman_collection.json) | [`ai/vllm/VLLM.md`](ai/vllm/VLLM.md) |
| llama-server | [`ai/llama/llama.postman_collection.json`](ai/llama/llama.postman_collection.json) | [`ai/llama/LLAMA.md`](ai/llama/LLAMA.md) |
| searxng | [`ai/searxng/searxng.postman_collection.json`](ai/searxng/searxng.postman_collection.json) | [`ai/searxng/SEARXNG.md`](ai/searxng/SEARXNG.md) |
| open-terminal | [`ai/open-terminal/open-terminal.postman_collection.json`](ai/open-terminal/open-terminal.postman_collection.json) | [`ai/open-terminal/OPEN_TERMINAL.md`](ai/open-terminal/OPEN_TERMINAL.md) |
| semantic-router | [`ai/semantic-router/semantic-router.postman_collection.json`](ai/semantic-router/semantic-router.postman_collection.json) | [`ai/semantic-router/SEMANTIC_ROUTER.md`](ai/semantic-router/SEMANTIC_ROUTER.md) |

OpenWebUI is intentionally omitted — it is a UI, not an API. All model traffic it emits already lands on LiteLLM's collection.

**Maintenance rule — keep the collections in sync with the code.** Whenever an API endpoint is added, renamed, removed, or changes its request/response shape, path params, query params, auth mode, or headers, update the corresponding `*.postman_collection.json` in the SAME change:

1. Add / update / remove the `item` under the collection's `item` array (or the appropriate subfolder like `MCP`).
2. Match the `request.url.path`, `request.method`, `request.header`, and `request.body.raw` to the code exactly — bodies must be valid JSON matching the current Pydantic / FastAPI model.
3. **Path parameters use `:name`, not `{{name}}`.** Any URL path segment that's a dynamic identifier (session id, job id, message id, filename, etc.) MUST be written in Postman's path-variable syntax — `:sandboxSessionId`, `:jobId`, `:roofixMessageId` — in BOTH `request.url.raw` and each matching entry in `request.url.path`. Add a `request.url.variable` array on the URL object with one `{"key": "name", "value": "", "description": "..."}` entry per path variable so Postman renders the editable field. Keep the corresponding top-level collection `variable` in place too (so users have one default they paste into). The `{{host}}` variable at the start of the URL (`{{litellm}}`, `{{kokoro}}`, `{{roofix}}`, etc.) is the ONLY `{{…}}` form allowed in `url.raw` — it stays as-is. `{{…}}` is also fine in headers, request bodies, query-string values, and auth blocks — the rule is path segments only.
4. Update the item's `description` to say WHAT it does, WHAT it returns, WHAT errors are possible, and reference the endpoint's docs section (e.g. `ENDPOINTS.md § …`).
5. If a new collection-level variable is needed (e.g. a fresh path-param handle like `sessionId`), add it under the collection's top-level `variable` array with a `description` explaining how to populate it.
6. If routing changes (e.g. a service moves onto a new LiteLLM pass-through prefix, or leaves LiteLLM entirely for a direct-service URL), update the collection's base-URL variable AND the `info.description` note about how requests are authenticated.
7. If a NEW service under `ai/` starts exposing an HTTP API, create `{service}.postman_collection.json` alongside its docs, add a row to the **Current collections** table above, and structure it after `sandbox-runner.postman_collection.json`.

Collections are the single source of truth for how each service's API is called from outside — do not merge an API-shape change without updating them. The `*.md` endpoint doc and the `*.postman_collection.json` MUST agree; when they don't, the code is authoritative and both must be corrected.
