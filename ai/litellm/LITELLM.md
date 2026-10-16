# LiteLLM Docker

Run a LiteLLM proxy with PostgreSQL for model management and Prometheus for metrics.

### Quick start

```bash
docker compose -f ai/litellm/docker-compose.litellm.yml up -d
```

This launches three services:

| Container | Port | Purpose |
|---|---|---|
| `litellm` | `localhost:4001` | LiteLLM proxy — OpenAI-compatible API |
| `litellm_db` | `localhost:5432` | PostgreSQL — stores model configs in DB |
| `prometheus` | `localhost:9090` | Metrics scraping and storage |

The proxy is reachable at `http://localhost:4001`. Configuration is loaded from `litellm_config.yaml` (mounted into the container).

### Dependencies

- `HF_TOKEN` from `.env` — HuggingFace token for gated model downloads
- `LITELLM_DATABASE_URL` from `.env` — PostgreSQL connection string
- `litellm_config.yaml` — proxy config with model definitions and routing rules
- `overflow.py` — the chain-alias overflow hook and the `auto` footer, mounted at `/app/overflow.py` beside the config (see [§ Chain aliases and the overflow hook](#chain-aliases-and-the-overflow-hook) and [§ The `auto` footer](#the-auto-footer))

### Health checks

The LiteLLM service runs a liveliness probe against `/health/liveliness`. Prometheus scrapes metrics from the proxy on its default endpoint, using the key described below.

### Prometheus scrape key

`/metrics` exists only because `litellm_config.yaml` sets `success_callback: ["prometheus"]` (and `failure_callback`). LiteLLM mounts the endpoint only in that branch. Without it, a valid key gets a `404`, because the auth middleware runs before routing.

LiteLLM requires a key on `/metrics` (`require_auth_for_metrics_endpoint` is on by default). Without one, Prometheus's `litellm` job shows `401 Unauthorized`. Turning that setting off is not an option here: the cloudflared tunnel sends `api.zeoenergy.com` straight to LiteLLM, so `https://api.zeoenergy.com/metrics` would become public.

Instead, Prometheus sends a **virtual key that can only call `/metrics`**. LiteLLM supports this with the key's `allowed_routes` (exact or wildcard match, `proxy/auth/route_checks.py`). The key lives at `ai/litellm/secrets/prometheus_metrics.key`. That folder's own `.gitignore` keeps everything but itself out of git, and it is mounted read-only into `prometheus` at `/etc/prometheus/secrets`. The `litellm` job in `prometheus.yml` reads it with `authorization.credentials_file`.

**Create the key (on the box, from the repo root):**

```bash
MK=$(grep '^DEFAULT_LITELLM_MASTER_KEY=' .env | cut -d= -f2- | tr -d "\"'")
curl -sS http://localhost:4001/key/generate \
  -H "Authorization: Bearer $MK" -H 'Content-Type: application/json' \
  -d '{"key_alias": "prometheus-metrics", "allowed_routes": ["/metrics", "/metrics/*"],
       "metadata": {"purpose": "Prometheus scrape of litellm /metrics (ai/litellm/prometheus.yml)"}}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["key"], end="")' \
  > ai/litellm/secrets/prometheus_metrics.key
chmod 644 ai/litellm/secrets/prometheus_metrics.key   # prometheus runs as nobody
unset MK
```

The key is written straight to the file and never printed. `/metrics/*` covers the trailing-slash redirect LiteLLM's mounted metrics app can answer with.

**Check it, then deploy:**

```bash
K=$(cat ai/litellm/secrets/prometheus_metrics.key)
curl -s -o /dev/null -w '%{http_code}\n' -L -H "Authorization: Bearer $K" http://localhost:4001/metrics     # 200
curl -s -o /dev/null -w '%{http_code}\n'    -H "Authorization: Bearer $K" http://localhost:4001/v1/models   # 403: nothing but /metrics
unset K
make up litellm          # prometheus is a default service of this stack; the new mount recreates it once
```

In Prometheus (`http://<box>:9090/targets`), the `litellm` job should be **UP**.

**Rotate it:** create a new key the same way (it overwrites the file), run `docker restart prometheus` (it re-reads the file on each scrape, so this is only to be safe), then delete the old key in the Admin UI under Virtual Keys → `prometheus-metrics`, or with `POST /key/delete`.

A missing or empty key file only breaks the `litellm` job. Prometheus logs the read error and the other jobs keep scraping.

### Stopping

```bash
docker compose -f ai/litellm/docker-compose.litellm.yml down
```

Data in PostgreSQL is persisted in the `litellm_postgres_data` named volume and survives container restarts.

### `litellm_config.yaml` settings

The proxy configuration file supports a wide range of options for models, routing, rate limits, and more. See the full reference: [LiteLLM Config Settings](https://docs.litellm.ai/docs/proxy/config_settings)

### Pass-through auth

`general_settings.pass_through_endpoints` mounts six internal services — `/v1/classifier`, `/v1/detector`, `/v1/madlad`, `/v1/interceptor`, `/v1/roofix`, `/v1/sandbox` — and **every entry carries `auth: true`**. None of those services checks a bearer token itself, so this flag is the only thing between a caller and them.

- **Without it there is no check at all.** A pass-through declared in the config file with no `auth` key registers with no auth dependency (LiteLLM v1.95.0, `_register_pass_through_endpoint`): a keyless request is forwarded. Only endpoints created in the Admin UI default to `auth=True`; config-file entries do not.
- **With it, the master key and proxy-admin keys pass as before.**
- **A virtual key additionally needs the route allow-listed** in its own `metadata` or its team's, or it gets `403 Key/team not allowed to access passthrough route …`. How to grant it is below.
- **Side effect:** an auth-enforced pass-through is added to LiteLLM's `openai_routes`, so its calls show up per key in spend logs and count against key / team budgets.

Applying a change to the `pass_through_endpoints` block is `make up litellm`. Granting a key access is not a config change — it is a database write, effective on the key's next request, no restart.

#### Granting a virtual key access

What the key needs is one entry in its metadata (or its team's metadata):

```json
{"allowed_passthrough_routes": ["/v1/classifier", "/v1/detector"]}
```

Rules that apply whichever way you set it:

- **Use the paths exactly as mounted** — `/v1/classifier`, `/v1/detector`, `/v1/madlad`, `/v1/interceptor`, `/v1/roofix`, `/v1/sandbox`. Matching is exact or prefix, so `/v1/classifier` also covers `/v1/classifier/jobs/{id}/artifacts`; list only the services the key's owner actually needs.
- **It goes inside `metadata`, never as the top-level `allowed_passthrough_routes` field.** `/key/generate`, `/key/update` and the team endpoints all accept a top-level field of that name, but on an unlicensed proxy it fails with `403 … only available for LiteLLM Enterprise users` (`_premium_user_check`). Written into `metadata` it is stored as-is, and `metadata` is exactly what the route check reads (`route_checks.py::check_passthrough_route_access`).
- **Only a proxy admin may set it** — call these endpoints with `$DEFAULT_LITELLM_MASTER_KEY`. A non-admin caller gets `403 Only proxy admins can set metadata.allowed_passthrough_routes`.
- **Key and team lists are not combined.** If the key's own `metadata` has a non-empty list, that list is used and the team's is ignored; the team's list applies only to keys with none of their own.
- **`allowed_routes` still applies first.** A key with a non-empty `allowed_routes` must allow the route there too (most keys leave it empty).

##### A new key

Put the list in `metadata` on `/key/generate`:

```bash
curl -X POST http://localhost:4001/key/generate \
  -H "Authorization: Bearer $DEFAULT_LITELLM_MASTER_KEY" -H "Content-Type: application/json" \
  -d '{
        "key_alias": "classifier-batch",
        "models": ["muse-glimmer"],
        "metadata": {"allowed_passthrough_routes": ["/v1/classifier"]}
      }'
```

##### An existing key

`POST /key/update` **replaces** `metadata` wholesale — anything you leave out (other routes, budgets-related flags, notes) is deleted. So read it first, add the list, and send the whole object back:

```bash
KEY=sk-...   # the virtual key to grant
H=(-H "Authorization: Bearer $DEFAULT_LITELLM_MASTER_KEY" -H "Content-Type: application/json")

# 1. current metadata (the response is {"key": ..., "info": {..., "metadata": {...}}})
curl -s "http://localhost:4001/key/info?key=$KEY" "${H[@]}" | jq '.info.metadata'

# 2. merge in the routes and write the whole object back
META=$(curl -s "http://localhost:4001/key/info?key=$KEY" "${H[@]}" \
  | jq -c '.info.metadata // {} | .allowed_passthrough_routes = ((.allowed_passthrough_routes // []) + ["/v1/classifier", "/v1/detector"] | unique)')
curl -X POST http://localhost:4001/key/update "${H[@]}" \
  -d "{\"key\": \"$KEY\", \"metadata\": $META}"
```

Without `jq`, do step 1, edit the JSON by hand, and send it in step 2's `-d` body.

##### Every key in a team

Set it once on the team instead; it covers every key in the team that has no list of its own. `PATCH /team/{team_id}` **merges** `metadata` (RFC 7386 — omitted keys are kept), so no read-modify-write is needed:

```bash
curl -X PATCH http://localhost:4001/team/<team_id> \
  -H "Authorization: Bearer $DEFAULT_LITELLM_MASTER_KEY" -H "Content-Type: application/json" \
  -d '{"metadata": {"allowed_passthrough_routes": ["/v1/classifier", "/v1/detector"]}}'
```

The merge replaces the *list* as a whole, so send the full set of routes, not just the new one. Avoid `POST /team/update` for this — like `/key/update` it replaces `metadata`.

##### From the Admin UI

Virtual Keys (or Teams) → the key → edit → the **Metadata** JSON box: add `"allowed_passthrough_routes": [...]` alongside whatever is already there and save. Do not use a dedicated "allowed pass-through routes" control if your UI version shows one — it sends the top-level field and fails with the Enterprise error. You must be signed in as a proxy admin.

##### Check it

```bash
# 200 — the key reaches the service
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:4001/v1/classifier/health -H "Authorization: Bearer $KEY"
```

| Result | Meaning |
|---|---|
| `200` | Allowed. |
| `401` | Missing, wrong, expired or blocked key — LiteLLM never got as far as the route check. |
| `403 Key/team not allowed to access passthrough route …` | Key is valid but no matching entry in key or team `metadata.allowed_passthrough_routes` — check the spelling and leading `/v1/`, and that a key-level list isn't shadowing the team's. |
| `403 Virtual key is not allowed to call this route. Only allowed to call routes: …` | The key's `allowed_routes` is set and doesn't include the route. |
| `403 … only available for LiteLLM Enterprise users` (on the update call) | You sent the top-level field; move it into `metadata`. |

##### Revoking

Write the list back without the route (same read-modify-write as above for a key; `PATCH /team/{team_id}` with the shortened list for a team). To remove the entry entirely from a team, `PATCH` it with `{"metadata": {"allowed_passthrough_routes": null}}`.

### Chain aliases and the overflow hook

Four model groups exist for the semantic router ([`ai/semantic-router/SEMANTIC_ROUTER.md`](../semantic-router/SEMANTIC_ROUTER.md)) to point at. Each is a **chain**: several deployments under one `model_name`, each with `litellm_params.order`. The policy they encode is *local models first for cost; Claude only when both local models are busy or down; customer / PII data never reaches Claude.*

| Chain alias | order 1 | order 2 | order 3 (overflow only) |
|---|---|---|---|
| `local-general` | `qwen3.8-solo` | `muse-glimmer` | `claude-sonnet-5` |
| `local-code` | `qwen3.8-solo` | `muse-glimmer` | `claude-sonnet-5` |
| `local-reasoning` | `muse-glimmer` | `qwen3.8-solo` | `claude-opus-5-5` |
| `local-private` | `qwen3.8-solo` | `muse-glimmer` | **— none —** |

Every deployment calls its backend **directly** (`api_base: http://qwen3.8-solo:8000/v1`, `anthropic/claude-sonnet-5`, …); its `litellm_params` and `model_info` are copies of the standalone alias of the same name — except `local-code`'s, which add coding settings on top ([§ local-code: what it adds](#local-code-what-it-adds)). Never point a chain deployment at a LiteLLM alias: it would recurse through the proxy and inherit that alias's `fallbacks: → claude-sonnet-5`. The standalone aliases (`qwen3.8-solo`, `muse-glimmer`, `claude-*`) are unchanged and still callable directly — **when you change one of them, change its chain copies too.**

#### local-code: what it adds

`local-code` has the same ladder as `local-general`; what differs is how a request gets there and what each deployment sends.

**How a request gets there.** Two ways:

1. The semantic router's `code` decision — `code_kw`, a set of RE2 regexes over the latest user message ([SEMANTIC_ROUTER.md § Decisions](../semantic-router/SEMANTIC_ROUTER.md#decisions)).
2. **The code-tool remap** in `overflow.py`'s `async_pre_call_hook`. The router reads only the latest user message and cannot see tool *names* — only that tools exist — so a coding turn like "Looks the same" lands on `local-general`. When the conversation in a request for **`local-general`** contains a **call** to a tool whose function name matches `CODE_ROUTE_TOOL_REGEX` **in full** (`re.fullmatch`), the hook rewrites `data["model"]` to `local-code`. A call is an assistant's `tool_calls[].function.name` in the history — what Open WebUI replays from earlier rounds and turns — and also a legacy `function_call`, an Anthropic `tool_use` block, or a named `tool` / `function` result (`overflow.called_tool_names`). Tools that are only **offered** in `tools` do not count, so a chat with the sandbox attached but never used stays on `local-general`. The proxy routes on the rewritten model (`common_request_processing.py`: `pre_call_hook` → `route_request`), and the Router sets `metadata.model_group` from it, so the overflow filter, the spend log and the `auto` footer (`· local-code`) all see `local-code`.

| `CODE_ROUTE_TOOL_REGEX` | Effect |
|---|---|
| unset (default) | `sandbox-mcp_.+\|run_command\|get_process_status\|send_process_input\|kill_process\|replace_file_content\|read_user_terminal\|send_user_terminal_input` |
| empty | remap off |
| invalid | one `invalid CODE_ROUTE_TOOL_REGEX … remap disabled` warning at startup; remap off |

The default matches the tool names Open WebUI v0.11.4 actually sends. The sandbox MCP server's tools carry its server-id prefix (`sandbox-mcp_run`, `sandbox-mcp_write_files`, `sandbox-mcp_exec`, … — seen in a real chat). Open Terminal's tools are its OpenAPI **operationIds, unprefixed**: `utils/tools.py::get_terminal_tools` uses `function_name = spec['name']`, `convert_openapi_to_tool_payload` sets `name` to the `operationId`, and `utils/middleware.py` merges them with `{**tools_dict, **terminal_tools}`. Open Terminal 0.13.0's schema-visible operationIds are `run_command`, `get_process_status`, `send_process_input`, `kill_process`, `list_processes`, `replace_file_content`, `write_file`, `read_file`, `display_file`, `list_files`, `grep_search`, `glob_search`, `match_files`, `search_files`, `get_info`, `read_user_terminal`, `send_user_terminal_input` (`open_terminal/main.py`). The default lists only the ones another tool server is unlikely to share; `run_command` is always there when a terminal is attached. It needs no `^…$` — and a `$` in `.env` would have to be `$$`. Unset uses the default because the compose file passes `${CODE_ROUTE_TOOL_REGEX-<default>}` (`-`, not `:-`): an **empty** value in `.env` reaches the hook and turns the remap off. `.env.example` sets the default explicitly; `unit-tests/litellm/test_code_route.py` fails if the three copies drift.

- **Scope.** Only `local-general` is ever remapped — router-originated or a direct call. `local-private` (PII routing stays the router's decision), `local-reasoning`, `local-code`, `auto` and the concrete aliases are never touched. The remap runs after key auth: a key allowed `local-general` but not `local-code` is still served by `local-code` (same models). `SEMANTIC_ROUTER_LITELLM_KEY` already has all four chains.
- **Log.** One `[overflow] [route] local-general -> local-code (<reason>)` line per remap — `called sandbox-mcp_exec in this exchange`, or `called sandbox-mcp_write_files 2 exchange(s) ago; 1/3 irrelevant since`, or `message matches code_kw …` — and one `[route] local-general stays (cooldown: 3/3 irrelevant exchanges since the last one that …)` when a chat that was code has cooled down. They are throttled to one per kind (this exchange / an earlier one / cooldown) per 30 s with a `(+N similar)` count; `handler.state()["stats"]` counts `code_routes`, `code_route_cooldowns` and `code_route_errors`. **Never throws:** on any error the request is left exactly as it came in and a warning is logged.
- **The cooldown: when it starts and when it lets go.** The hook splits the history into **exchanges**, one per user message (the message plus every model round it started). An exchange is **relevant** when the model called a `CODE_ROUTE_TOOL_REGEX` tool during it, or when its user message matches the semantic router's own `code_kw` signal. LiteLLM reads that signal from the router's `config.yaml`, mounted read-only at `/app/semantic-router-config.yaml` (`CODE_ROUTE_ROUTER_CONFIG`), and compiles it with the Go router's rules (`method: regex` = raw pattern, otherwise a whole literal word; ASCII `\b`), so the two never disagree. A `local-general` request moves to `local-code` while the **current** exchange is relevant, or while fewer than `CODE_ROUTE_COOLDOWN` (default **3**) irrelevant exchanges in a row followed the last relevant one. Build a calculator, then ask about the weather: weather questions 1–3 are still `local-code`, the 4th is `local-general`. The moment an exchange is relevant again (a `code_kw` message, or the model calls the sandbox mid-exchange — every round after that call is `local-code`) the count restarts. Irrelevant tool calls (`search_web`, a generic `read_file`) don't count. Attaching the sandbox to a model card (as `office-model` does) changes nothing until the model actually calls it. Everything is recomputed from the request's history: there is no per-chat state, so both copies during a swap, and a restart, agree; Open WebUI's context compaction summarising old exchanges away also lets a chat cool down.

  | `CODE_ROUTE_COOLDOWN` | Effect |
  |---|---|
  | unset / empty | 3 |
  | `0` | only the current exchange counts |
  | a non-negative integer | that many irrelevant exchanges in a row before dropping |
  | anything else | 3, plus one `invalid CODE_ROUTE_COOLDOWN` warning at startup |

  If the router config can't be read (missing mount, no `code_kw`, a pattern Python can't compile), one warning is logged at startup and exchanges are judged by tool calls only.

**The prompt.** All three deployments carry the same `litellm_system_prompt`, written once with the YAML anchor `&code_system_prompt` and reused with `*code_system_prompt` (the proxy loads the config with `yaml.safe_load`, which resolves both):

```
You are working on code with tools.
- Your turn ends as soon as you reply without a tool call. Never announce a step ("let me...", "I'll now...") - make the tool call instead.
- Change existing files with edit_file (exact old_string -> new_string). Use write_files only for new files or complete rewrites. Read only the lines you need with read_file (offset/limit).
- When a tool returns an error, diagnose it and retry in the same turn.
- Run the code or its tests to verify before saying it works.
- Reply without a tool call only when the task is done (say what changed) or you need a decision from the user (ask it).
```

LiteLLM merges it **in front of** the first system message (Open WebUI's), or adds it as one, for that deployment only (`main.py`: `add_system_prompt_to_messages(..., merge_with_first_system=True)`), re-applies it on every retry and order fallback with that deployment's own value, and never sends the key to the provider. The overflow hook's `/tokenize` count includes it. It must **never** contain a `Reasoning strength:` line: Muse Glimmer's template would take that over the kwarg below.

**Per deployment.**

| Order | Deployment | Adds |
|---|---|---|
| 1 | `qwen3.8-solo` | the prompt; `extra_body.chat_template_kwargs: {reasoning_effort: high}` (the fixed template maps `high` → `xhigh`, `ai/jinja/qwen_fixed_chat_template.jinja`; its default is `medium`); `max_tokens: 32768` (Qwen's own SWE-bench setting); `max_input_tokens: 98304` |
| 2 | `muse-glimmer` | the prompt; `extra_body.chat_template_kwargs: {reasoning_strength: xhigh}` (the stock template defaults to `high`); `max_tokens: 32768`; `max_input_tokens: 229376` |
| 3 | `claude-sonnet-5` | the prompt only |

Sampling is unchanged — `qwen3.8-solo`'s already is Qwen's official Thinking preset, `muse-glimmer`'s is Meta's published defaults (`top_k` stays next to the new key in `extra_body`).

- **Context.** The overflow hook's context fit drops a deployment when `count + max_tokens > max_model_len`, so with a 32768-token answer `qwen3.8-solo` (131072) takes prompts up to 98304 and `muse-glimmer` (262144) up to 229376; `max_input_tokens` says the same so LiteLLM's backstop check agrees. A `local-code` prompt over 98304 goes to `muse-glimmer`; one between 229376 and `auto`'s 245760 goes to `local-code`'s Claude order (it is not the PII chain).
- **Client caps win.** A client-sent `max_tokens` overrides the deployment's 32768. That includes the `auto` alias's own default — a deployment default is sent in the request body, so it reached the inner LiteLLM as a client value — which is why `auto` now sets no `max_tokens` and each chain's deployment decides. **Operator step:** if an Open WebUI model card sets *Max Tokens* (the `office-model` card had 8192), clear it or raise it to 32768 (Workspace → Models → Advanced Params), or every coding answer is cut at that.
- **Client template kwargs replace, they do not merge.** A request `extra_body` replaces the deployment's whole `extra_body` (`top_k` included), and a top-level request `chat_template_kwargs` replaces `extra_body.chat_template_kwargs` — measured on the outgoing body at v1.93.0 and v1.95.0. An Open WebUI *custom parameter* `chat_template_kwargs` (e.g. `enable_thinking`) therefore drops `reasoning_effort` / `reasoning_strength`; add them to it if you set one. The hook's `/tokenize` call follows the same precedence.
- **Timeouts are unchanged, and that is a trade-off.** `xhigh` reasoning makes turns slower. The chain's per-read 240 s still applies: a `qwen3.8-solo` turn that goes silent for 240 s (before its first token, or mid-stream) fails over to `muse-glimmer`, and the whole turn still has to finish inside `auto`'s budget ([§ Chain timeouts](#chain-timeouts)). Streaming keeps a reasoning turn alive token by token, so long thinking is fine; a long *silent* prefill is what trips it. `unit-tests/litellm/test_timeouts.py` keeps the nesting.

**Verifying.** In a fresh Open WebUI chat on `auto` with the sandbox attached, send `Looks the same` first: no sandbox call yet, so the footer reads `· local-general`. Then ask for something that makes the model use the sandbox (`Make me a scientific calculator!`), and send `Looks the same` again: the footer reads `· local-code`. Then ask four unrelated questions (the weather): the first three still read `· local-code`, the fourth `· local-general`, with a `[route] local-general stays (cooldown: 3/3 …)` line and `make -s logs litellm FOLLOW=0 TAIL=200 2>&1 | grep '\[route\]'` shows the remap. Spend-log rows for `local-code` show `qwen3.8-solo` or `muse-glimmer`. To see that `muse-glimmer` renders `xhigh`, render a request with vLLM's tokenize endpoint from a container on `ai_shared` and look for `Reasoning strength: xhigh.` in the system text:

```bash
curl -s muse-glimmer:8000/tokenize -H 'content-type: application/json' \
  -d '{"model":"muse-glimmer","messages":[{"role":"user","content":"hi"}],"chat_template_kwargs":{"reasoning_strength":"xhigh"},"return_token_strs":true}'
```

#### How a deployment is picked (LiteLLM v1.95.0)

`router.py::async_get_healthy_deployments` (10606-10740) filters in this order:

1. **cooldown** — deployments LiteLLM has cooled down after failures are removed;
2. **overflow hook** — `async_callback_filter_deployments` (10686) calls `ai/litellm/overflow.py`, which first drops a local deployment the prompt does not fit (**context fit**, on vLLM's exact `/tokenize` count), then one that is busy or down. This is the real context gate, and why `max_input_tokens` is set per deployment: a prompt over 114688 tokens skips `qwen3.8-solo` for `muse-glimmer` (245760) with no error and no fallback hop;
3. **pre-call context check** (10694, `enable_pre_call_checks: true`) — drops any deployment whose `model_info.max_input_tokens` is smaller than the prompt **as LiteLLM counts it**: `litellm.token_counter(messages=messages)` (`router.py:10147`) — OpenAI's tiktoken, over `messages` only, with no tool definitions. That undercounts Qwen prompts, and Open WebUI's native tool calling sends the tool definitions on every turn: on 2026-10-12 a prompt vLLM counted at 122,881 tokens passed this check for `qwen3.8-solo` (114688) and was refused by vLLM. It stays on as the backstop for whatever the hook could not measure;
4. **order filter** (10716-10720) — keeps only the **lowest** `order` left.

A deployment that fails *at call time* (connection refused, 5xx) is retried `num_retries` times and then LiteLLM's **order-based fallback** walks the remaining order levels of the same group (`router.py:6132-6182`). A **timeout** is not retried at the same order: `router_settings.model_group_retry_policy` gives each chain `{TimeoutErrorRetries: 0}`, so a pre-header timeout goes straight to the next order (`router.py:6505-6521`), and a stall after headers takes the router's mid-stream fallback to the next order (`router.py:2066-2150`) — see [§ Chain timeouts](#chain-timeouts). So the chains need, and have, **no `fallbacks` entry**. Order-based fallback is skipped for `ContextWindowExceededError` (6134-6137) — so a prompt that reaches a backend too small for it is **not** retried at the next order. That is exactly why the context gate has to be exact, and why it lives in the hook (step 2) rather than relying on step 3's estimate.

`local-private` has no Claude deployment and no entry in `fallbacks` or `context_window_fallbacks`. Do not add one. `auto`'s two fallback entries point at `local-private`, because when the router is down nothing has checked the prompt for PII.

#### The overflow hook (`overflow.py`)

Mounted at `/app/overflow.py` next to `/app/config.yaml` and registered with `litellm_settings.callbacks: ["overflow.handler"]` (LiteLLM resolves the module relative to the config file's directory, `proxy/types_utils/utils.py:30-56`). It:

- **polls** `<api_base minus /v1>/metrics` on every local backend it has seen in a chain, **concurrently**, about once a second, and sums `vllm:num_requests_waiting` / `vllm:num_requests_running` across label sets;
- marks a backend **busy** once `waiting > 0` has held for `OVERFLOW_BUSY_AFTER_S`, and clears it once `waiting == 0` has held for `OVERFLOW_IDLE_AFTER_S`;
- marks a backend **down** when its probe fails (refused, timed out, non-200);
- treats a backend as **unknown → keep** when it has never been probed, when its data is older than 5 × the poll interval (the poller stopped), or when `/metrics` answers without the vLLM queue metric;
- per request, and only for a group with ≥ 2 distinct `order` values: drops every deployment with an `api_base` that is busy or down, **never** drops one without an `api_base` (Claude), and if that would leave nothing keeps the busy ones (the request queues in vLLM) or, all down, returns the list unchanged — which is what keeps `local-private` local;
- **before that, checks context fit** (same chain groups only) — see below;
- never throws (LiteLLM re-raises a filter's exception, `router.py:7366-7384`, which would fail the request) and returns the list unchanged on any internal error.

The poller starts lazily on the first chain request and is restarted if it ever dies. Until it has data, the hook changes nothing — LiteLLM behaves exactly as it would without it.

**Context fit.** LiteLLM's own context check (step 3 above) undercounts, so the hook counts for itself with each backend's own tokenizer: vLLM's `POST <api_base minus /v1>/tokenize` (server root, outside `/v1` and its API-key guard; verified at v0.27.1 and v0.29.0, the tags `qwen3.8-solo` and `muse-glimmer` run) renders `messages` + `tools` through the same chat template and `preprocess_chat` as `/v1/chat/completions` and returns `count` and `max_model_len` — the numbers vLLM itself checks.

- **Which deployments.** Every deployment with an `api_base` and an integer `model_info.max_input_tokens`, in a chain group. Claude deployments and plain aliases are never measured.
- **When it asks.** Only if a deliberately high estimate — `len(json(messages)) + len(json(tools))` characters ÷ `OVERFLOW_TOKENIZE_CHARS_PER_TOKEN` (2.5) — reaches `OVERFLOW_TOKENIZE_MIN_FRACTION` (0.6) × that deployment's `max_input_tokens`. A short chat never makes the call. Each distinct backend is asked at most once per request, all of them concurrently, and the answers sit in a 64-entry LRU keyed by backend + body hash, so `auto`'s `context_window_fallbacks → local-private` and the mid-stream order fallback, which re-enter with the same messages, do not ask again.
- **The body.** `model` (the deployment's `litellm_params.model` minus `openai/`), `messages` (with the deployment's `litellm_system_prompt` merged in front of the first system message, as LiteLLM will send them), `tools` when the request has them, `add_generation_prompt: true`, and the ONE `chat_template_kwargs` LiteLLM will send — the request's top-level `chat_template_kwargs`, else the request's `extra_body`'s, else (only when the request sent no `extra_body`) the deployment's. LiteLLM replaces rather than merges: `{**deployment, **request}` at the Router, then a top-level value written over `extra_body.chat_template_kwargs` whole ([§ local-code: what it adds](#local-code-what-it-adds)). The server's own `--chat-template` / `--default-chat-template-kwargs` apply on `/tokenize` exactly as on a chat completion. vLLM checks `model` against its `--served-model-name`; a mismatch is a tokenize failure (below).
- **The rule.** A deployment **fits** iff `count <= max_input_tokens` **and** `count + max_out <= max_model_len`, where `max_out` is the request's `max_completion_tokens` / `max_tokens`, else the deployment's `litellm_params.max_tokens` (vLLM refuses a chat completion whose input plus `max_tokens` exceeds `max-model-len`). One that does not fit is dropped as **`too-long`** — a **hard** drop: the busy/down rule then runs on what is left, so its "keep the busy ones" fallback can never bring it back.
- **Nothing local fits.** A group with a Claude deployment (`local-general` / `-code` / `-reasoning`) keeps only Claude — the prompt is past every local window, the same outcome as before. `local-private` gets the list back **unchanged**, so vLLM refuses it with its own error and the prompt never leaves the box.
- **Any tokenize failure keeps the deployment** — a timeout (`OVERFLOW_TOKENIZE_TIMEOUT_S`, wall clock), a non-200, an image part sent to a text-only model, a body without `count`. LiteLLM then behaves as it did before this step, with step 3 as the backstop. Failures are not cached.
- `/tokenize` returns every token id; for a 120k-token prompt that is about 1 MB of JSON per backend asked. There is no flag to omit them, which is one more reason the estimate gates the call.

#### Tuning

| Variable | Default | Effect |
|---|---|---|
| `OVERFLOW_BUSY_AFTER_S` | `2` | how long a waiting queue must persist before the backend counts as busy. Lower spills sooner (more Claude spend, less queueing); higher tolerates bursts. |
| `OVERFLOW_IDLE_AFTER_S` | `5` | how long the queue must stay empty before a busy backend is used again. Keep it above `BUSY_AFTER` so a backend does not flap. |
| `OVERFLOW_POLL_INTERVAL_S` | `1` | probe cadence (floor 0.2). Data older than 5 × this is ignored. |
| `OVERFLOW_PROBE_TIMEOUT_S` | `0.75` | per-probe timeout; a probe that exceeds it marks the backend **down**. Keep it below the poll interval. |
| `OVERFLOW_TOKENIZE_MIN_FRACTION` | `0.6` | context fit: `/tokenize` is asked only when the estimate reaches this fraction of a deployment's `max_input_tokens`. Lower asks for more (shorter) prompts; `0` asks on every chain request. |
| `OVERFLOW_TOKENIZE_CHARS_PER_TOKEN` | `2.5` | context fit: divisor of the estimate (JSON characters of `messages` + `tools`). Deliberately low so the estimate runs **high** — raising it toward English's ~4 makes the hook skip prompts it should have measured. Floor 0.5. |
| `OVERFLOW_TOKENIZE_TIMEOUT_S` | `5` | context fit: wall-clock limit on one `/tokenize` call; past it the deployment is kept. It adds to the request's latency, so keep it short. Floor 0.1. |

Set them in `.env` and `make up litellm` (they are passed through the compose `environment:` block, so a change to them is a compose change and `make up` recreates the container). Turning the hook off is removing the `callbacks:` line — the chains then fail over only on real errors; that is a config-file edit, so apply it as in [§ Applying a chain or hook change](#applying-a-chain-or-hook-change).

#### Changing a chain

- **Reorder** (e.g. make `muse-glimmer` go first in `local-general`): swap the two deployments' `order` values. Nothing else changes — the hook and the order filter read `order` per request.
- **Change the overflow model** (e.g. `local-code` → `claude-opus-5-5`): replace the order-3 deployment's `litellm_params` with a copy of the other Claude alias's. Never add a Claude deployment to `local-private`.
- **Change a model's settings** (sampling, `max_input_tokens`): edit the standalone alias **and** every chain copy of it — the chains do not reference the standalone alias, on purpose. **Timeouts are the exception**: chain deployments carry their own `stream_timeout` / `timeout` (the chain's budget, [§ Chain timeouts](#chain-timeouts)), not copies of the standalone alias's.
- The semantic router needs no change for any of these; it only knows the chain's name.

#### Chain timeouts

Every chain deployment sets **both** `stream_timeout` and `timeout` — 240 s on the local orders 1 and 2, 300 s on the Claude order 3 — and `auto` sets both to 870 s. They are one layer of a strictly nested budget: Open WebUI 900 s (`OPENWEBUI_AIOHTTP_CLIENT_TIMEOUT`, a hard total including streaming, on every chat completion) > `auto` 870 > Envoy listener 840 > the chain's per-read 240 / 300. `litellm_settings.request_timeout: 900` is the per-attempt default for anything that sets none of its own.

- **They are per read, not totals.** LiteLLM passes them as `httpx.Timeout(x)`; for `stream: true` the value used is request `stream_timeout` > deployment `stream_timeout` > `router_settings.stream_timeout` > `request_timeout`, otherwise `timeout` / `request_timeout` (`router.py:3143-3175`). On `auto` the response is buffered by Envoy and its headers held until the first chunk (`proxy/common_request_processing.py:460-472`), so `auto`'s per-read 870 is in practice a time-to-complete-answer limit.
- **A timeout goes straight to the next order.** `router_settings.model_group_retry_policy: {local-…: {TimeoutErrorRetries: 0}}` (field `RetryPolicy.TimeoutErrorRetries`, `types/router.py:105`; passed through by `proxy_server.py:5012-5016`, which keeps only valid `Router` args). Without it, `num_retries: 1` would re-pick order 1 — the order filter still prefers it — and spend a second 240 s there. Other errors keep `num_retries: 1`. `auto` has the same entry: its timeout firing means the routed turn used its whole budget, and a retry would restart it seconds before Open WebUI's 900 s cap — it goes to `fallbacks: auto → [local-private]` instead. Do **not** use `allowed_fails_policy` for this: it switches every deployment to legacy cooldown.
- **What falls back, measured against v1.95.0** (`unit-tests/litellm/test_timeouts.py`): a timeout before response headers → next order at once; a stall after headers (vLLM sends them before its first token) → the read timeout maps to `APIConnectionError`, the stream raises `MidStreamFallbackError`, and the Router's mid-stream fallback moves to the next order — re-prompting with the partial answer as an assistant prefix if some content had already streamed. A mid-stream error that maps to a 4xx is raised with no fallback (`streaming_handler.py:2202-2206`). The whole table is in [SEMANTIC_ROUTER.md § Timeout budget](../semantic-router/SEMANTIC_ROUTER.md#timeout-budget).
- The standalone aliases keep their own `stream_timeout` (300 / 600 / 1800) as stall detectors; a caller through Open WebUI is cut at 900 s total regardless.

#### Applying a chain or hook change

```bash
make up litellm
```

`litellm_config.yaml` and `overflow.py` are single-file bind mounts and LiteLLM reads both only at startup. `make up litellm` hashes their *content* into the container name (`litellm-<hash>`, see [EDGE.md § The swap](../edge/EDGE.md#the-swap)), so any edit produces a new name: a fresh copy starts beside the running one — reading the current files — and the old one retires once the new one is healthy, with no downtime (in-flight chats get the 900 s `stop_grace_period`). `docker restart` keeps the original mount, so an editor or `git pull` that replaced the file (new inode) leaves a restarted container reading the old one — use `make up litellm`. If the new copy fails its healthcheck (an `overflow` import error, a YAML typo), `make up` prints its last 100 log lines, removes it and leaves the old copy serving; otherwise check `make logs litellm` for warnings.

#### Seeing spills

- `make -s logs litellm FOLLOW=0 TAIL=all 2>&1 | grep '\[overflow\]'` — one INFO line per spill decision (alias, dropped deployment, reason, waiting count, next order), throttled to one per alias/deployment/reason per 30 s with a `(+N similar)` count. An `every deployment is busy/down … keeping …` line is `local-private` (or a chain whose Claude deployment was cooled down) choosing to queue.
- Context-fit lines use the same logger: `local-general: dropping openai/qwen3.8-solo@order1 (too-long, count=122881 > max_input_tokens=114688) -> next order 2` (or `count=… + max_tokens=… > max_model_len=…`); `no local deployment fits the prompt (…); keeping only anthropic/…` is a prompt past every local window going to Claude; `no deployment fits the prompt (…); list unchanged` is `local-private` refusing one; `/tokenize at … failed (…); keeping that backend's deployment(s)` means the hook could not measure and LiteLLM's own check decided.
- **LiteLLM Admin UI → Logs / spend by key**: the row's *model group* is the chain alias, its *model* the deployment that answered. Claude overflow is any row with a `local-*` model group and an `anthropic/…` model. A `local-private` row with a Claude model must never exist.
- In-process state: `handler.state()` returns the per-backend verdict, queue depths, timers and counters (`spills`, `kept_all_overloaded`, `errors`, `poller_restarts`, for context fit `context_drops`, `tokenize_calls`, `tokenize_cache_hits`, `tokenize_errors`, and for the code-tool remap `code_routes`, `code_route_errors`). It lives in the proxy process; it is there for debugging with a REPL or a temporary log line, not as an endpoint.

#### Adding a chain

1. Add one `model_list` entry per rung under a new `model_name` (e.g. `local-vision`), each with `order: N`, its own `api_base` and its own `model_info.max_input_tokens`. Copy the params from the standalone alias; do not reference it.
2. Decide whether it may reach Claude. If not, give it no Claude deployment and **no** `fallbacks` / `context_window_fallbacks` entry, like `local-private`. If it may, put Claude on the highest order only.
3. If the semantic router should use it, add it to `providers.models` + `routing.modelCards` in `ai/semantic-router/config.yaml`, re-render `envoy.yaml`, and add the alias to `SEMANTIC_ROUTER_LITELLM_KEY`'s model list in the Admin UI.
4. Nothing to configure in the hook — it learns backends from the deployments it sees. A non-vLLM local backend (llama.cpp) has no `vllm:num_requests_waiting`; the hook then treats it as **unknown** and never spills on its account, only failing over on real errors.
5. Apply it with the recreate in [§ Applying a chain or hook change](#applying-a-chain-or-hook-change) (not `make up litellm`), then add a Postman item under **Chain aliases** in `litellm.postman_collection.json`.

The overflow hook's unit tests live in [`unit-tests/litellm/test_overflow.py`](../../unit-tests/litellm/test_overflow.py) and drive the real v1.95.0 `Router` when `litellm` is installed:

```bash
uv venv /tmp/llvenv && UV_LINK_MODE=copy uv pip install --python /tmp/llvenv litellm==1.95.0 pytest
/tmp/llvenv/bin/python -m pytest unit-tests/litellm -q -p no:cacheprovider
```

### The `auto` footer

When — and only when — a client calls model **`auto`** (the semantic router), the same handler in `overflow.py` appends a footer to the assistant message naming the backend that actually answered:

```
<answer>

---
*qwen3.8-solo · local-general*
```

| Footer | Meaning |
|---|---|
| `*qwen3.8-solo · local-general*` | the router chose `local-general`; its order-1 deployment answered |
| `*muse-glimmer · local-code*` | order 1 was busy, down or failed; order 2 answered |
| `*claude-sonnet-5 · local-general · overflow (local busy)*` | a Claude deployment answered, and the hook *currently* sees that chain's local backends as busy |
| `… · overflow (local down)` | … every local backend of the chain is failing its probe |
| `… · overflow` | Claude answered but the hook has no busy/down verdict (a call-time failure, a stale poller, or a different proxy worker did the spilling) |
| `*qwen3.8-solo · local-private · router bypassed*` | the router itself failed and `fallbacks: auto -> [local-private]` answered |

**Scope.** Requests whose `model` is `auto`, nothing else: a direct call to a `local-*` chain or any other alias is untouched, and so is the router's own sub-request (it comes back into LiteLLM as `local-*`), so the footer is added exactly once, on the outer response. Provider prefixes are stripped (`anthropic/claude-sonnet-5` → `claude-sonnet-5`).

**How it is derived (LiteLLM v1.95.0).** The inner LiteLLM — the one serving the chain — puts `x-litellm-model-id`, `x-litellm-model-name` (`common_request_processing.py:938-948`) and `x-litellm-model-group` (`router.py:9388`) on its response; Envoy passes them through and the router adds `x-vsr-selected-decision` / `x-vsr-selected-model` (`processor_res_header_mutation.go:249-261`). The outer openai provider keeps every upstream header as `llm_provider-<name>` in `_hidden_params["additional_headers"]` — on the response for JSON (`core_helpers.py:318-319`) and on the stream wrapper for SSE (`openai.py:1072-1079` → `streaming_handler.py:164-171`; both regression-tested against a stand-in upstream). The model id is mapped back to its deployment through the proxy's own router (`llm_router.get_deployment`, `router.py:8577`), because inner and outer are the same proxy; it is the id of the deployment that actually answered, also after an in-group order fallback. The name / group headers are the fallback. **Router bypassed** is read from the outer deployment the proxy stores in `data["deployment"]` (`common_request_processing.py:1707`, resolved from the response's model id): its model group is `local-private`, not `auto` (the outer router also reports `x-litellm-model-group: local-private` and `x-litellm-attempted-fallbacks: 1`). `data["model"]` itself stays `auto` — `route_request` unpacks `**data` into the router (`route_llm_request.py:404`). Busy / down come from this handler's own backend state for the chain, so they are an approximation of *why* Claude answered, not a record.

**Hooks.** Three methods defined directly in the `OverflowHandler` class body — not on a mixin, because the proxy activates `async_post_call_streaming_iterator_hook` and `async_pre_call_hook` only when the name is in the leaf class's `__dict__` (`proxy/utils.py:1718-1745`):

- `async_post_call_success_hook` (non-streaming; `proxy/utils.py:2404-2410` keeps a non-`None` return) appends the footer to each qualifying `message.content`;
- `async_post_call_streaming_iterator_hook` (the proxy wraps the stream once, `proxy_server.py:7413-7419`, and writes `data: [DONE]` after it ends) yields the footer as an extra `delta.content` chunk right before each qualifying choice's finish chunk — or appends it to the finish chunk's own content when that chunk carries text, so it never lands in front of the last words. A stream that ends without a finish chunk gets it last;
- `async_pre_call_hook` strips footers from the incoming conversation (below). The same hook also does the code-tool remap (`local-general` → `local-code`, [§ local-code: what it adds](#local-code-what-it-adds)), which does not depend on `AUTO_FOOTER_ENABLED`.

**Edge cases.**

- **Skipped:** a choice with tool calls (or `content` empty / `None`); any request with `response_format`; reasoning-only output. `reasoning_content` is never touched.
- **Open WebUI background tasks are skipped.** v0.11.4 sends title / tags / follow-up / search-query / autocomplete / emoji requests to the *chat* model unless a Task Model is set, and a footer would land in chat titles and break the tags / follow-ups JSON. Its task payloads carry `metadata.task`, but `routers/openai.py` pops `metadata` before the request leaves Open WebUI and forwards no task header — so the prompt is the only signal. Detected: a last user message starting `### Task:` (the default title, tags, image-prompt, follow-up, query and autocomplete templates, `config.py:2211-2340`) or `Your task is to reflect the speaker's likely facial expression` (emoji, `config.py:2439`); a system message starting `Available Tools:` (legacy function calling, `utils/middleware.py:1351-1373`); and, for admin-customised templates, a last user message containing a `<chat_history>…</chat_history>` block. The user-message checks apply **only to non-streaming requests** — every task is sent with `stream: false` (`routers/tasks.py`) — because the default RAG template also starts `### Task:` and, with `RAG_SYSTEM_CONTEXT` unset, Open WebUI *prepends* it to the user's own message (`add_or_update_user_message(..., append=False)`); before that rule, every answer from a knowledge-backed model (e.g. `office-model`) silently lost its footer. MOA is a user-visible answer and is not skipped.
- **Belt and braces:** set **Admin Settings → Interface → Task Model** (local *and* external) to a non-`auto` model — e.g. `qwen3.8-solo`, or `local-private` if chats may carry customer data. A custom task template that matches none of the patterns above would otherwise get a footer.
- **Previous footers are stripped** from incoming assistant messages on `auto` requests (string content, or the last text part of list content; `role: "assistant"` only), so the model never sees — and imitates — them and the router's keyword signals never read them. The regex matches only the exact emitted form: a blank line, `---`, then one `*…*` line of at least two ` · `-separated parts, at the very end. A non-streaming answer that imitates the footer has it replaced, not doubled.
- `n > 1`: once per qualifying choice; never twice for the same response.
- **Never throws:** any internal error returns the response or stream unmodified. `handler.state()["stats"]` counts `footers`, `footers_stripped` and `footer_errors`.
- The footer is presentation only: spend logs, token counts and cost are those of the answer without it.

**Toggle.** `AUTO_FOOTER_ENABLED` (default `true`; accepts `true`/`false`/`1`/`0`, also `yes`/`no`/`on`/`off`), passed through the compose `environment:` like the `OVERFLOW_*` variables. Off disables all three footer hooks, stripping included (the code-tool remap keeps working; `CODE_ROUTE_TOOL_REGEX=` empty is its own off switch). A change to `overflow.py` itself needs the recreate in [§ Applying a chain or hook change](#applying-a-chain-or-hook-change).

**Verifying.**

```bash
# non-streaming: choices[0].message.content ends with the footer, and the
# llm_provider-x-litellm-model-name / -model-group response headers agree with it
curl -si http://localhost:4001/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"auto","messages":[{"role":"user","content":"Say hi in five words."}]}'

# streaming: the footer is the last content delta, before the finish chunk and [DONE]
curl -siN http://localhost:4001/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"auto","stream":true,"messages":[{"role":"user","content":"Say hi in five words."}]}'
```

Then confirm the negatives: the same call with `"model":"local-general"` has no footer, and a new Open WebUI chat's title, tags and follow-ups carry none. Unit tests: [`unit-tests/litellm/test_auto_footer.py`](../../unit-tests/litellm/test_auto_footer.py), run the same way as the overflow tests (the proxy-plumbing tests additionally need `litellm[proxy]==1.95.0`).
