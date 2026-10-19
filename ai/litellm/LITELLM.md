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
- `hooks/` — this deployment's proxy hooks (the chain-alias overflow hook, the `auto` footer, the code-tool remap), mounted read-only at `/app/hooks`, with the two stdlib-only parts of `shared/common` they import mounted at `/app/common` (see [§ The hooks package](#the-hooks-package))
- `overflow.py` — the shim LiteLLM loads (`callbacks: ["overflow.handler"]`), mounted at `/app/overflow.py` beside the config; it imports `hooks` and builds `handler`

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

Four model groups exist for the route-policy engine ([`ai/route-policy/ROUTE_POLICY.md`](../route-policy/ROUTE_POLICY.md)) to route `auto` to ([§ Route policy: single-pass `auto`](#route-policy-single-pass-auto)); a client can also call each by name. Each is a **chain**: several deployments under one `model_name`, each with `litellm_params.order`. The policy they encode is *local models first for cost; Claude only when both local models are busy or down; customer / PII data never reaches Claude.*

| Chain alias | order 1 | order 2 | order 3 (overflow only) |
|---|---|---|---|
| `local-general` | `qwen3.8-solo` | `muse-glimmer` | `claude-sonnet-5` |
| `local-code` | `qwen3.8-solo` | `muse-glimmer` | `claude-sonnet-5` |
| `local-reasoning` | `muse-glimmer` | `qwen3.8-solo` | `claude-opus-5-5` |
| `local-private` | `qwen3.8-solo` | `muse-glimmer` | **— none —** |

Every deployment calls its backend **directly** (`api_base: http://qwen3.8-solo:8000/v1`, `anthropic/claude-sonnet-5`, …); its `litellm_params` and `model_info` are copies of the standalone alias of the same name — except `local-code`'s, which add coding settings on top ([§ local-code: what it adds](#local-code-what-it-adds)). Never point a chain deployment at a LiteLLM alias: it would recurse through the proxy and inherit that alias's `fallbacks: → claude-sonnet-5`. The standalone aliases (`qwen3.8-solo`, `muse-glimmer`, `claude-*`) are unchanged and still callable directly — **when you change one of them, change its chain copies too.**

#### local-code: what it adds

`local-code` has the same ladder as `local-general`; what differs is what each deployment sends.

**How a request gets there.** The route-policy engine routes `auto` here when its `default` ruleset says the chat is code: a code-like latest user message (`code-text`), a call to a sandbox / Open Terminal tool in the current exchange (`code-tools`), or such an exchange in the last few (`sticky-code`, a cooldown) — see [ROUTE_POLICY.md](../route-policy/ROUTE_POLICY.md). Or a client calls `local-code` by name. Only `auto` is decided: a request that names `local-general` stays on `local-general` (the hook's old code-tool remap for direct `local-general` calls is gone).

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

- **Context.** The overflow hook's context fit drops a deployment when `count + max_tokens > max_model_len`, so with a 32768-token answer `qwen3.8-solo` (131072) takes prompts up to 98304 and `muse-glimmer` (262144) up to 229376; `max_input_tokens` says the same so LiteLLM's backstop check agrees. A `local-code` prompt over 98304 goes to `muse-glimmer`; one over 229376 goes to `local-code`'s Claude order (it is not the PII chain).
- **Client caps win.** A client-sent `max_tokens` overrides the deployment's 32768. `auto` is an alias with no `litellm_params`, so it adds none: each chain's deployment decides. **Operator step:** if an Open WebUI model card sets *Max Tokens* (the `office-model` card had 8192), clear it or raise it to 32768 (Workspace → Models → Advanced Params), or every coding answer is cut at that.
- **Client template kwargs replace, they do not merge.** A request `extra_body` replaces the deployment's whole `extra_body` (`top_k` included), and a top-level request `chat_template_kwargs` replaces `extra_body.chat_template_kwargs` — measured on the outgoing body at v1.93.0 and v1.95.0. An Open WebUI *custom parameter* `chat_template_kwargs` (e.g. `enable_thinking`) therefore drops `reasoning_effort` / `reasoning_strength`; add them to it if you set one. The hook's `/tokenize` call follows the same precedence.
- **Timeouts are unchanged, and that is a trade-off.** `xhigh` reasoning makes turns slower. The chain's per-read 240 s still applies: a `qwen3.8-solo` turn that goes silent for 240 s (before its first token, or mid-stream) fails over to `muse-glimmer`, and the whole turn still has to finish inside Open WebUI's 900 s total ([§ Chain timeouts](#chain-timeouts)). Streaming keeps a reasoning turn alive token by token, so long thinking is fine; a long *silent* prefill is what trips it. `unit-tests/litellm/test_timeouts.py` keeps the nesting.

**Verifying.** In a fresh Open WebUI chat on `auto` with the sandbox attached, send `Looks the same` first: nothing code-like yet, so the footer reads `· local-general`. Then ask for something that makes the model use the sandbox (`Make me a scientific calculator!`): the footer reads `· local-code`. Send `Looks the same` again: still `· local-code` (the engine's `code-tools` / `sticky-code` evaluators). Each turn's decision, with every evaluator's vote, is in the route-policy service (`GET /chats/{chat_id}/decisions`, [§ Route policy: single-pass `auto`](#route-policy-single-pass-auto)). Spend-log rows for `local-code` show `qwen3.8-solo` or `muse-glimmer`. To see that `muse-glimmer` renders `xhigh`, render a request with vLLM's tokenize endpoint from a container on `ai_shared` and look for `Reasoning strength: xhigh.` in the system text:

```bash
curl -s muse-glimmer:8000/tokenize -H 'content-type: application/json' \
  -d '{"model":"muse-glimmer","messages":[{"role":"user","content":"hi"}],"chat_template_kwargs":{"reasoning_strength":"xhigh"},"return_token_strs":true}'
```

#### How a deployment is picked (LiteLLM v1.95.0)

`router.py::async_get_healthy_deployments` (10606-10740) filters in this order:

1. **cooldown** — deployments LiteLLM has cooled down after failures are removed;
2. **overflow hook** — `async_callback_filter_deployments` (10686) calls `Handler.async_filter_deployments` (`hooks/overflow/filter.py`), which first drops a local deployment the prompt does not fit (**context fit**, on vLLM's exact `/tokenize` count), then one that is busy or down. This is the real context gate, and why `max_input_tokens` is set per deployment: a prompt over 114688 tokens skips `qwen3.8-solo` for `muse-glimmer` (245760) with no error and no fallback hop;
3. **pre-call context check** (10694, `enable_pre_call_checks: true`) — drops any deployment whose `model_info.max_input_tokens` is smaller than the prompt **as LiteLLM counts it**: `litellm.token_counter(messages=messages)` (`router.py:10147`) — OpenAI's tiktoken, over `messages` only, with no tool definitions. That undercounts Qwen prompts, and Open WebUI's native tool calling sends the tool definitions on every turn: on 2026-10-12 a prompt vLLM counted at 122,881 tokens passed this check for `qwen3.8-solo` (114688) and was refused by vLLM. It stays on as the backstop for whatever the hook could not measure;
4. **order filter** (10716-10720) — keeps only the **lowest** `order` left.

A deployment that fails *at call time* (connection refused, 5xx) is retried `num_retries` times and then LiteLLM's **order-based fallback** walks the remaining order levels of the same group (`router.py:6132-6182`). A **timeout** is not retried at the same order: `router_settings.model_group_retry_policy` gives each chain `{TimeoutErrorRetries: 0}`, so a pre-header timeout goes straight to the next order (`router.py:6505-6521`), and a stall after headers takes the router's mid-stream fallback to the next order (`router.py:2066-2150`) — see [§ Chain timeouts](#chain-timeouts). So the chains need, and have, **no `fallbacks` entry**. Order-based fallback is skipped for `ContextWindowExceededError` (6134-6137) — so a prompt that reaches a backend too small for it is **not** retried at the next order. That is exactly why the context gate has to be exact, and why it lives in the hook (step 2) rather than relying on step 3's estimate.

`local-private` has no Claude deployment and no entry in `fallbacks` or `context_window_fallbacks`. Do not add one. `auto` itself is `router_settings.model_group_alias: {auto: local-private}`: an `auto` request nothing rewrote is served there, because nothing has checked such a prompt for PII.

#### The overflow hook

`hooks/overflow/` — `filter.py` (the rule below), `context_fit.py`, `poller.py`, `state.py` (`BackendState`), `vllm.py` (URLs, metric parsing, the httpx clients) — run from `Handler.async_filter_deployments`; how LiteLLM loads it is in [§ The hooks package](#the-hooks-package). It:

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
- **Nothing local fits.** A group with a Claude deployment (`local-general` / `-code` / `-reasoning`) called **by name** keeps only Claude — the prompt is past every local window. `local-private` gets the list back **unchanged**, so vLLM refuses it with its own error and the prompt never leaves the box. **A request the client sent as `auto`** gets its chain's local deployments back unchanged too, on any chain, so vLLM refuses it: Claude is *load* overflow for `auto` (both locals busy or down), never a bigger window. The hook knows the request was `auto` from the route-policy stash, which rides in the request's metadata into `request_kwargs` (`metadata`, or `litellm_metadata` on `/v1/messages`). This is what the old `auto` deployment's `max_input_tokens: 245760` enforced; `auto` is an alias now and has no window of its own. Busy / down overflow to Claude is unchanged for `auto`. `handler.state()["stats"]["context_auto_kept_local"]` counts these. Residual gap: when `/tokenize` cannot measure a local deployment, that deployment is kept and LiteLLM's tiktoken backstop decides — if the backstop then drops every local, Claude answers.
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
- The route-policy engine needs no change for any of these; it only knows the chain's name.

#### Chain timeouts

`auto` is served in one pass — the route-policy hook rewrites it to a chain alias and LiteLLM streams that chain's answer straight back — so the budget has one layer inside Open WebUI's:

- **Open WebUI 900 s > the chain's per-read 240 / 300 s.** Open WebUI's `OPENWEBUI_AIOHTTP_CLIENT_TIMEOUT` (900 s) is a hard total including streaming, on every chat completion. Every chain deployment sets **both** `stream_timeout` and `timeout` — 240 s on the local orders 1 and 2, 300 s on the Claude order 3 — so three pre-header timeouts in a row cost 240 + 240 + 300 = 780 s, still inside it. `litellm_settings.request_timeout: 900` is the per-attempt default for anything that sets none of its own.
- **`auto` adds no layer.** It is `router_settings.model_group_alias: {auto: local-private}`, not a deployment: no `timeout`, no retry policy, no fallbacks of its own (those are looked up by the model group actually called — after the rewrite, the chain). The awaited `/decide` adds at most `ROUTE_POLICY_DECIDE_TIMEOUT_S` (0.25 s) before the chain starts.
- **The edge sits outside.** LAN clients reach LiteLLM through `ai/edge`'s `listen litellm`, whose `timeout client` / `timeout server` are 900 s of *inactivity*. A non-streamed answer is silent until it is complete — at most the chain's 780 s of pre-header failover — so the edge never cuts a turn LiteLLM is still waiting on. Open WebUI calls `litellm:4000` directly, not through the edge.
- **They are per read, not totals.** LiteLLM passes them as `httpx.Timeout(x)`; for `stream: true` the value used is request `stream_timeout` > deployment `stream_timeout` > `router_settings.stream_timeout` > `request_timeout`, otherwise `timeout` / `request_timeout` (`router.py:3143-3175`). Now that `auto` streams, a long reasoning turn stays alive token by token; only 240 s of silence (a stuck prefill, a stalled backend) trips it.
- **A timeout goes straight to the next order.** `router_settings.model_group_retry_policy: {local-…: {TimeoutErrorRetries: 0}}` (field `RetryPolicy.TimeoutErrorRetries`, `types/router.py:105`; passed through by `proxy_server.py:5012-5016`, which keeps only valid `Router` args). Without it, `num_retries: 1` would re-pick order 1 — the order filter still prefers it — and spend a second 240 s there. Other errors keep `num_retries: 1`. Do **not** use `allowed_fails_policy` for this: it switches every deployment to legacy cooldown.
- **What falls back, measured against v1.95.0** (`unit-tests/litellm/test_timeouts.py`): a timeout before response headers → next order at once; a stall after headers (vLLM sends them before its first token) → the read timeout maps to `APIConnectionError`, the stream raises `MidStreamFallbackError`, and the Router's mid-stream fallback moves to the next order — re-prompting with the partial answer as an assistant prefix if some content had already streamed. A mid-stream error that maps to a 4xx is raised with no fallback (`streaming_handler.py:2202-2206`).
- The standalone aliases keep their own `stream_timeout` (300 / 600 / 1800) as stall detectors; a caller through Open WebUI is cut at 900 s total regardless.

`unit-tests/litellm/test_timeouts.py` keeps this nesting: it fails if a chain's worst-case failover no longer fits inside Open WebUI's total, if the edge's timers drop below it, or if `auto` grows a timeout of its own.

#### Applying a chain or hook change

```bash
make up litellm
```

`litellm_config.yaml` and `overflow.py` are single-file bind mounts, `hooks/` and `shared/common`'s `conversation` part are directory mounts, and LiteLLM reads all of them only at startup. `make up litellm` hashes their *content* into the container name (`litellm-<hash>`, see [EDGE.md § The swap](../edge/EDGE.md#the-swap)), so any edit produces a new name: a fresh copy starts beside the running one — reading the current files — and the old one retires once the new one is healthy, with no downtime (in-flight chats get the 900 s `stop_grace_period`). `docker restart` keeps the original single-file mounts, so an editor or `git pull` that replaced such a file (new inode) leaves a restarted container reading the old one — use `make up litellm`. If the new copy fails its healthcheck (a `hooks` import error, a YAML typo), `make up` prints its last 100 log lines, removes it and leaves the old copy serving; otherwise check `make logs litellm` for warnings.

#### Seeing spills

- `make -s logs litellm FOLLOW=0 TAIL=all 2>&1 | grep '\[overflow\]'` — one INFO line per spill decision (alias, dropped deployment, reason, waiting count, next order), throttled to one per alias/deployment/reason per 30 s with a `(+N similar)` count. An `every deployment is busy/down … keeping …` line is `local-private` (or a chain whose Claude deployment was cooled down) choosing to queue.
- Context-fit lines use the same logger: `local-general: dropping openai/qwen3.8-solo@order1 (too-long, count=122881 > max_input_tokens=114688) -> next order 2` (or `count=… + max_tokens=… > max_model_len=…`); `no local deployment fits the prompt (…); keeping only anthropic/…` is a prompt past every local window going to Claude (a direct chain call); `no local deployment fits the prompt (…); an auto request never goes to Claude for length -- keeping …` is an `auto` prompt kept local to be refused; `no deployment fits the prompt (…); list unchanged` is `local-private` refusing one; `/tokenize at … failed (…); keeping that backend's deployment(s)` means the hook could not measure and LiteLLM's own check decided.
- **LiteLLM Admin UI → Logs / spend by key**: the row's *model group* is the chain alias, its *model* the deployment that answered. Claude overflow is any row with a `local-*` model group and an `anthropic/…` model. A `local-private` row with a Claude model must never exist.
- In-process state: `handler.state()` returns the per-backend verdict, queue depths, timers and counters (`spills`, `kept_all_overloaded`, `errors`, `poller_restarts`, for context fit `context_drops`, `tokenize_calls`, `tokenize_cache_hits`, `tokenize_errors`, and for the route-policy client the `route_policy_*` counters of [§ Route policy: single-pass `auto`](#route-policy-single-pass-auto)). It lives in the proxy process; it is there for debugging with a REPL or a temporary log line, not as an endpoint.

#### Adding a chain

1. Add one `model_list` entry per rung under a new `model_name` (e.g. `local-vision`), each with `order: N`, its own `api_base` and its own `model_info.max_input_tokens`. Copy the params from the standalone alias; do not reference it.
2. Decide whether it may reach Claude. If not, give it no Claude deployment and **no** `fallbacks` / `context_window_fallbacks` entry, like `local-private`. If it may, put Claude on the highest order only.
3. Add it to the hook's `CHAIN_ROUTES` in `hooks/routing/invariants.py` — the only chain aliases the hook will ever rewrite `auto` to; `unit-tests/litellm/test_hook_invariants.py` fails until that tuple and the config's chain groups agree. If the engine should route `auto` to it, also add it to the route-policy ruleset's `routes` (a new ruleset version, [ROUTE_POLICY.md](../route-policy/ROUTE_POLICY.md)).
4. Nothing to configure in the overflow hook — it learns backends from the deployments it sees. A non-vLLM local backend (llama.cpp) has no `vllm:num_requests_waiting`; the hook then treats it as **unknown** and never spills on its account, only failing over on real errors.
5. Apply it with the recreate in [§ Applying a chain or hook change](#applying-a-chain-or-hook-change) (not `make up litellm`), then add a Postman item under **Chain aliases** in `litellm.postman_collection.json`.

The overflow hook's unit tests live in [`unit-tests/litellm/test_overflow.py`](../../unit-tests/litellm/test_overflow.py) and drive the real v1.95.0 `Router` when `litellm` is installed; [§ The hooks package](#the-hooks-package) has the venv recipe.

### The `auto` footer

When — and only when — a client calls model **`auto`** (routed by the route-policy engine, [§ Route policy: single-pass `auto`](#route-policy-single-pass-auto)), the same handler (`hooks/footer/`) appends a footer to the assistant message naming the chain and deployment that actually answered:

```
<answer>

---
*qwen3.8-solo · local-general*
```

| Footer | Meaning |
|---|---|
| `*qwen3.8-solo · local-general*` | the engine chose `local-general`; its order-1 deployment answered |
| `*muse-glimmer · local-code*` | order 1 was busy, down or failed; order 2 answered |
| `*claude-sonnet-5 · local-general · overflow (local busy)*` | a Claude deployment answered, and the hook *currently* sees that chain's local backends as busy |
| `… · overflow (local down)` | … every local backend of the chain is failing its probe |
| `… · overflow` | Claude answered but the hook has no busy/down verdict (a call-time failure, a stale poller, or a different proxy worker did the spilling) |
| `*qwen3.8-solo · local-private · route-policy fallback*` | the engine did not route the request — `/decide` timed out, failed or answered `apply: false`, or the client is off — so it went to `local-private` |

**Scope.** Requests the client sent as `auto`, nothing else: a direct call to a `local-*` chain or any other alias is untouched. Provider prefixes are stripped (`anthropic/claude-sonnet-5` → `claude-sonnet-5`).

**How it is derived (LiteLLM v1.95.0).** Everything comes from the one request that served the turn. The proxy resolves the deployment that answered from the response's model id and stores it in `data["deployment"]` (`common_request_processing.py:1707`) before the success and streaming hooks run: its `model_name` is the chain, its `litellm_params.model` the deployment (an order-2 or Claude answer names order 2 or Claude). The response's `x-litellm-model-group` (`router.py:9388`) is the cross-check and names the chain when the deployment is missing — but without a deployment there is no model name, and no footer. Whether the request was `auto` at all is the route-policy stash (`zeo_route_policy` in the proxy's metadata), because `data["model"]` is the chain by then; its `source` says whether the engine routed it or it fell back. A request still for `auto` (the hook never rewrote it, and the `auto` alias served it from `local-private`) gets the fallback tag too. The client still sees `"model": "auto"` in the response. Busy / down come from this handler's own backend state for the chain, so they are an approximation of *why* Claude answered, not a record.

**Hooks.** Three methods defined directly in the `Handler` class body (`hooks/handler.py`) — not on a mixin, because the proxy activates `async_post_call_streaming_iterator_hook` and `async_pre_call_hook` only when the name is in the leaf class's `__dict__` (`proxy/utils.py:1718-1745`). Each delegates: to `AutoFooter` (`hooks/footer/auto.py`) and, for streams, `FooterStream` (`hooks/footer/stream.py`):

- `async_post_call_success_hook` (non-streaming; `proxy/utils.py:2404-2410` keeps a non-`None` return) appends the footer to each qualifying `message.content`;
- `async_post_call_streaming_iterator_hook` (the proxy wraps the stream once, `proxy_server.py:7413-7419`, and writes `data: [DONE]` after it ends) yields the footer as an extra `delta.content` chunk right before each qualifying choice's finish chunk — or appends it to the finish chunk's own content when that chunk carries text, so it never lands in front of the last words. A stream that ends without a finish chunk gets it last;
- `async_pre_call_hook` strips footers from the incoming conversation (below) — first, so the route-policy engine never reads them — then routes the request ([§ Route policy: single-pass `auto`](#route-policy-single-pass-auto)), which does not depend on `AUTO_FOOTER_ENABLED`.

**Edge cases.**

- **Skipped:** a choice with tool calls (or `content` empty / `None`); any request with `response_format`; reasoning-only output; any response that is not an OpenAI chat completion (`/v1/messages` — routed all the same — and the responses API). `reasoning_content` is never touched.
- **Open WebUI background tasks are skipped.** v0.11.4 sends title / tags / follow-up / search-query / autocomplete / emoji requests to the *chat* model unless a Task Model is set, and a footer would land in chat titles and break the tags / follow-ups JSON. Its task payloads carry `metadata.task`, but `routers/openai.py` pops `metadata` before the request leaves Open WebUI and forwards no task header — so the prompt is the only signal. Detected: a last user message starting `### Task:` (the default title, tags, image-prompt, follow-up, query and autocomplete templates, `config.py:2211-2340`) or `Your task is to reflect the speaker's likely facial expression` (emoji, `config.py:2439`); a system message starting `Available Tools:` (legacy function calling, `utils/middleware.py:1351-1373`); and, for admin-customised templates, a last user message containing a `<chat_history>…</chat_history>` block. The user-message checks apply **only to non-streaming requests** — every task is sent with `stream: false` (`routers/tasks.py`) — because the default RAG template also starts `### Task:` and, with `RAG_SYSTEM_CONTEXT` unset, Open WebUI *prepends* it to the user's own message (`add_or_update_user_message(..., append=False)`); before that rule, every answer from a knowledge-backed model (e.g. `office-model`) silently lost its footer. MOA is a user-visible answer and is not skipped.
- **Belt and braces:** set **Admin Settings → Interface → Task Model** (local *and* external) to a non-`auto` model — e.g. `qwen3.8-solo`, or `local-private` if chats may carry customer data. A custom task template that matches none of the patterns above would otherwise get a footer.
- **Previous footers are stripped** from incoming assistant messages on `auto` requests (string content, or the last text part of list content; `role: "assistant"` only), so the model never sees — and imitates — them and the route-policy engine's keyword evaluators never read them. The regex matches only the exact emitted form: a blank line, `---`, then one `*…*` line of at least two ` · `-separated parts, at the very end. A non-streaming answer that imitates the footer has it replaced, not doubled.
- `n > 1`: once per qualifying choice; never twice for the same response.
- **Never throws:** any internal error returns the response or stream unmodified. `handler.state()["stats"]` counts `footers`, `footers_stripped` and `footer_errors`.
- The footer is presentation only: spend logs, token counts and cost are those of the answer without it.

**Toggle.** `AUTO_FOOTER_ENABLED` (default `true`; accepts `true`/`false`/`1`/`0`, also `yes`/`no`/`on`/`off`), passed through the compose `environment:` like the `OVERFLOW_*` variables. Off disables all three footer parts, stripping included; routing keeps working (`ROUTE_POLICY_ENABLED` is its own switch). A change to the hooks' code is applied as in [§ Applying a chain or hook change](#applying-a-chain-or-hook-change).

**Verifying.**

```bash
# non-streaming: choices[0].message.content ends with the footer, and the
# x-litellm-model-group response header names the same chain
curl -si http://localhost:4001/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"auto","messages":[{"role":"user","content":"Say hi in five words."}]}'

# streaming (really streamed now): the footer is the last content delta, before the finish chunk and [DONE]
curl -siN http://localhost:4001/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"auto","stream":true,"messages":[{"role":"user","content":"Say hi in five words."}]}'
```

Then confirm the negatives: the same call with `"model":"local-general"` has no footer, and a new Open WebUI chat's title, tags and follow-ups carry none. Unit tests: [`unit-tests/litellm/test_auto_footer.py`](../../unit-tests/litellm/test_auto_footer.py), run the same way as the overflow tests (the proxy-plumbing tests additionally need `litellm[proxy]==1.95.0`).

### The hooks package

`litellm_settings.callbacks: ["overflow.handler"]` loads one object, `handler`: a `Handler` doing three jobs.

| Path | What |
|---|---|
| `overflow.py` | the shim LiteLLM loads: imports `hooks`, builds `handler`, exports nothing else |
| `hooks/handler.py` | `Handler(CustomLogger)` — the five hooks LiteLLM dispatches on, each only delegating; `state()` |
| `hooks/settings.py` | every environment variable the hooks read (`OVERFLOW_*`, `AUTO_FOOTER_ENABLED`, `ROUTE_POLICY_*`), read once at import |
| `hooks/log.py` | the `zeo.overflow` logger (the `[overflow]` lines) and the 30 s per-key throttle |
| `hooks/deployments.py` | reading LiteLLM deployment dicts (`api_base`, `order`, chain detection) |
| `hooks/overflow/` | job 1, [§ The overflow hook](#the-overflow-hook) |
| `hooks/footer/` | job 2, [§ The `auto` footer](#the-auto-footer) |
| `hooks/routing/policy_client.py` | job 3, the route-policy client: routes every `auto` request ([§ Route policy: single-pass `auto`](#route-policy-single-pass-auto)) |
| `hooks/routing/invariants.py`, `hooks/routing/stash.py` | the hook's own copy of the two route invariants; the per-request record the footer and `/observe` read |
| `shared/common/src/common/conversation` | shared, stdlib-only: chat-history helpers (the footer's Open WebUI task detection reads messages with it) |

**How it loads.** LiteLLM's `get_instance_fn` (`proxy/types_utils/utils.py:8-61`, identical in 1.93 and 1.95) maps `overflow.handler` to `<config dir>/overflow.py` and loads it with `spec_from_file_location` + `exec_module`: it never registers the file in `sys.modules`, never adds `/app` to `sys.path` for it, and cannot load a package. Code loaded that way cannot use relative imports, and `@dataclass` crashes on it at proxy start (`'NoneType' object has no attribute '__dict__'`). So `overflow.py` is only a shim. It puts its own directory first on `sys.path`, imports `hooks.handler` the normal way, takes the entry back out, and builds `handler`. Because the import is normal, `hooks` and every submodule are in `sys.modules`, relative imports work, and `@dataclass` is safe (`BackendState` is one).

Taking the entry back out matters. The proxy CLI already *appends* its working directory (`/app`) to `sys.path`, and that low-precedence entry is how `import enterprise` finds the image's `/app/enterprise`. Leaving `/app` first would let anything under `/app` shadow an installed package for the rest of the process. The shim also refuses to load, with an `ImportError` naming the file, if a different top-level `hooks` was imported first. Neither `litellm[proxy]==1.95.0` nor the upstream Dockerfile's `/app` (`.venv`, `docker/`, `enterprise/`, …) has a top-level `hooks` or `common` (the second box check below confirms it for the running image); LiteLLM's own `litellm.proxy.hooks` is a subpackage, always imported by its full name.

**`common` in the container.** The image is upstream plus bind mounts, so `shared/common` is not installed there. The compose file mounts only what the hooks import, read-only under `/app/common`: `common/__init__.py` (a docstring) and `common/conversation`. The rest of `common` (documents, vision, jobs, …) is not in the container, and editing it does not change LiteLLM's deploy hash. [`unit-tests/litellm/test_hook_package.py`](../../unit-tests/litellm/test_hook_package.py) fails in three cases:

- the hooks import a `common.<x>` that has no mount line;
- a mounted part imports anything outside the stdlib;
- the hooks import a third-party package the image lacks (only `litellm`, `yaml` and `httpx` are allowed).

It also builds a fresh `/app` from the compose file's own mounts and loads the shim there in an isolated interpreter.

**The leaf-class rule.** The proxy activates `async_post_call_streaming_iterator_hook` and `async_pre_call_hook` only when the name is in the registered class's own `__dict__` (`proxy/utils.py:1718-1745`). So all five hooks stay in `Handler`'s class body and delegate from there (`async_post_call_failure_hook` is dispatched on any `CustomLogger`, `proxy/utils.py:2136-2166`, but follows the same rule). A hook moved onto a component or a base class would silently never run.

**Tests.** From the repo root:

```bash
UV_LINK_MODE=copy uv run --no-sync pytest unit-tests/litellm shared/common/tests/test_conversation.py -q
```

The workspace venv has `litellm` without its proxy extras, so the proxy-plumbing tests skip there. To run them, use a throwaway venv. Nothing else needs installing in it: `unit-tests/litellm/conftest.py` puts `ai/litellm` and `shared/common/src` on `sys.path`.

```bash
uv venv /tmp/llvenv && UV_LINK_MODE=copy uv pip install --python /tmp/llvenv "litellm[proxy]==1.95.0" "fastapi<0.119" pytest
/tmp/llvenv/bin/python -m pytest unit-tests/litellm -q -p no:cacheprovider
```

**On the box**, after `make up litellm`:

```bash
# from /app (cwd on sys.path): the mounted package and common
make exec litellm litellm CMD="python -c 'import importlib.util as u; print(u.find_spec(\"hooks\").origin, u.find_spec(\"common\").submodule_search_locations)'"
#   /app/hooks/__init__.py ['/app/common']
# isolated (-I: no cwd): nothing installed under either name
make exec litellm litellm CMD="python -I -c 'import importlib.util as u; print(u.find_spec(\"hooks\"), u.find_spec(\"common\"))'"
#   None None
make -s logs litellm FOLLOW=0 TAIL=300 2>&1 | grep -E 'Traceback|ImportError|\[overflow\]'
```

The last command should show no traceback. Then send one `auto` request ([§ The `auto` footer](#the-auto-footer) › Verifying) and check the answer ends with its footer.

#### Route policy: single-pass `auto`

Phase 3 of the route-policy engine (ZT-471). `auto` is not a deployment any more: on every request for model **`auto`**, the handler (`hooks/routing/policy_client.py`) asks the route-policy service (`ai/route-policy/`, `http://route-policy:8000` on `ai_shared`) which chain alias should serve it, rewrites `data["model"]` to that chain, and LiteLLM serves the chain in the **same pass, streamed**. The semantic router, its Envoy and the LiteLLM → router → LiteLLM loop are gone. The engine decides *intent* (which chain, from the conversation); the overflow hook still decides *capacity* (which deployment inside the chain).

| When | Call | What it sends |
|---|---|---|
| `async_pre_call_hook`, **awaited** | `POST /decide` | `decision_id` (a uuid4 the hook makes), `requested_model` (`auto`), `litellm_call_id`, `chat_id` / `message_id` / `user_id` (Open WebUI's `X-OpenWebUI-Chat-Id` / `-Message-Id` / `-User-Id` request headers, any case, `null` when absent), `key_alias`, `messages` (as LiteLLM will forward them, so after the footer strip, with inline base64 / `data:` payloads replaced by placeholders — see *What it costs*) and `tools`, and `backends` (the overflow poller's `{verdict, waiting, running}` per `api_base`; `verdict` is `ok`, `busy`, `down`, `stale` or `unknown`) |
| `async_post_call_success_hook` (non-streaming) | `POST /observe`, fired | `decision_id`, `litellm_call_id`, `response_id` (the response `id`, which is the spend log's `request_id`), `served_model_group` and `served_deployment` (from `data["deployment"]`, like the footer — e.g. `local-code` and `openai/muse-glimmer`), `overflow_reason` (`busy` / `down` when a Claude deployment answered, else `null`; the hook's *current* view, like the footer's tag), `finish_reason`, `tool_calls` (the function names in the answer; an Anthropic `/v1/messages` answer's `stop_reason` and `tool_use` names), `status: "success"`, `error: null` |
| `async_post_call_streaming_iterator_hook`, at stream end | `POST /observe`, fired | the same, read from the chunks: `response_id` is the first chunk's id, `tool_calls` the names streamed in `delta.tool_calls`. An upstream error mid-stream, or a client that disconnects, sends `status: "failure"` instead |
| `async_post_call_failure_hook` | `POST /observe`, fired | `status: "failure"` and `error: "<ExceptionType>: <message>"` (at most 500 characters); the served fields are `null` unless a stream had started |

**The route.** The answer's `route` is applied only when the answer says `apply: true` (an `enforce` ruleset whose own invariants held) **and** the hook's copy of the invariants allows it (`hooks/routing/invariants.py`: one of the four chain aliases — never a concrete alias, never `auto`; a requested `local-private` stays `local-private`). **Everything else goes to `local-private`**, the chain with no Claude deployment, because nothing has checked such a prompt for PII:

| Fallback (`source` in the stash) | When |
|---|---|
| `fallback:timeout` | no `/decide` answer within `ROUTE_POLICY_DECIDE_TIMEOUT_S` (LiteLLM puts no timeout around a pre-call hook, `proxy/utils.py:1444-1457`, so the client bounds it with `asyncio.wait_for`) |
| `fallback:error` | a transport error: refused, reset, DNS |
| `fallback:http-<status>` | a non-2xx answer (`404`: no ruleset bound to `auto`; `422`: a body the service refused; `503`: a ruleset this copy cannot compile) |
| `fallback:bad-response` | a 2xx that is not a JSON object, or that answers for another `decision_id` |
| `fallback:not-applied` | `apply` is not `true` — a `shadow` ruleset (the staged deploy: LiteLLM before the `enforce` ruleset) |
| `fallback:not-a-chain` | the route fails the hook's invariants |
| `fallback:disabled` | `ROUTE_POLICY_ENABLED=false` or an empty `ROUTE_POLICY_URL`: no `/decide`, no `/observe` |
| `fallback:no-messages` | the request has no `messages` list to decide on (the responses API) |
| `fallback:hook-error` | a bug in the client (logged) |

A request the hook never rewrote at all is served by `local-private` too: `auto` is `router_settings.model_group_alias: {auto: local-private}`, which LiteLLM resolves only at deployment selection (`router.py:10465`), after the pre-call hook (`common_request_processing.py:1274`) — so the hook still sees `"auto"`, `auto` is listed in `/v1/models` and passes key checks (`auth_checks.py:2978`). Requests that name a chain or any other alias are never decided and never rewritten. A routed `auto` request whose prompt fits no local deployment of its chain is **not** handed to that chain's Claude deployment: the overflow hook keeps it local and vLLM refuses it ([§ The overflow hook](#the-overflow-hook) › Nothing local fits) — Claude is load overflow only. **`/v1/messages`** (Claude Code) runs the same pre-call pipeline (`anthropic_endpoints/endpoints.py:95`), so its `auto` is routed the same way.

**Where the stash lives.** `zeo_route_policy` — `{requested_model: "auto", route, source: "engine" | "fallback:<why>", decision_id, litellm_call_id, observed}` — in the proxy's own metadata dict: `data["metadata"]`, or `data["litellm_metadata"]` on `/v1/messages`, `/v1/responses`, batches and files, where `metadata` is a parameter of the request itself (`litellm_pre_call_utils.py:133-139`). The client sees `"model": "auto"` in the response; the footer and `/observe` read the stash, never `data["model"]`. A stash the request body carried is dropped from every request first. LiteLLM v1.95.0 hands the success, streaming and failure hooks the same `data` dict (`common_request_processing.py` 1274 → 1919 / 1854-1859 → `proxy_server.py` 7413-7419, 7540; 2538-2542), and the metadata dict is created before `function_setup` copies the top level, so even `/v1/messages`' failure path — which passes the dict it read from the body — sees it. Neither dict reaches the upstream: the openai provider forwards `metadata` only with `litellm.enable_preview_features`, and then only string values (`main.py:2407`, `utils.py:9243`), while the stash is a dict; `litellm_metadata` is LiteLLM's own.

**`/observe` is fire and forget.**

- Each call is an asyncio task on the proxy's own loop, using one `httpx.AsyncClient` per event loop (shared with `/decide`).
- At most 64 are in flight; past that a call is dropped and counted.
- Each is bounded by `ROUTE_POLICY_TIMEOUT_S`. The service's answer is ignored.
- It is sent once per request whose `/decide` was attempted — applied or fallen back, so a fallback's outcome is in the decision log too — and never for one that sent no `/decide`.
- Nothing raises into LiteLLM, and the failure hook always returns `None`, so it never changes the error the client gets.

**What it costs.** One awaited round trip per `auto` turn: the service decides in well under a millisecond and logs the decision before it answers, so a few ms on `ai_shared`. A hung service costs at most `ROUTE_POLICY_DECIDE_TIMEOUT_S` per turn, then `local-private` answers. The body is the whole conversation, but every inline binary payload in a content part — an `image_url` / `input_image` `data:` URI, an Anthropic `image` / `document` `source.data`, `input_audio` data, a `file_data` URI, any other `data:` URI in a part — is replaced by a short placeholder (`data:image/png;base64,[omitted: 3000033 chars]`), so a chat full of images costs kilobytes, not megabytes. The part keeps its type, text parts and tool calls are sent verbatim, and the request LiteLLM forwards is untouched (`slim_messages`). There is no circuit breaker: every `auto` turn asks again.

**Spend.** Each `auto` request is ONE spend-log row under the caller's own key, with the chain as its model group and the deployment that answered as its model.

**Settings.** These are infrastructure settings only; routing rules live in the service's rulesets, never in env vars. They are passed through the compose `environment:`.

| Variable | Default | |
|---|---|---|
| `ROUTE_POLICY_ENABLED` | `true` | `false`: no `/decide`, no `/observe`, every `auto` request goes to `local-private` (`fallback:disabled`) |
| `ROUTE_POLICY_URL` | `http://route-policy:8000` | the service, by Docker DNS on `ai_shared`; an empty value also disables the client |
| `ROUTE_POLICY_DECIDE_TIMEOUT_S` | `0.25` | the awaited `/decide`, start to finish (floor 0.05); past it, `local-private` |
| `ROUTE_POLICY_TIMEOUT_S` | `2` | one background `/observe`, start to finish (floor 0.1) |

**Counters** in `handler.state()`:

- `stats.route_policy_decides`: `/decide` calls the service answered with a 2xx; `route_policy_applied`: routes applied; `route_policy_fallbacks`: requests sent to `local-private` instead, by reason in `route_policy_fallbacks_by_reason`;
- `route_policy_observes`: `/observe` calls answered with a 2xx;
- `route_policy_errors`: timeouts, refused connections, non-2xx answers, unusable answers and internal errors (`apply: false` is not an error);
- `route_policy_dropped`: `/observe` calls over the in-flight cap;
- `route_policy_in_flight` and `config.route_policy_*`.

Like the rest of `state()`, they live in the proxy process. The logs carry the same signals:

- one `[overflow] [route-policy] first decision applied: auto -> <chain> (ruleset <id>@<version>) -- single pass is live` line per process, and one `first POST …/observe answered HTTP 202`;
- `[route-policy] auto -> local-private (<why>)` per fallback, throttled to one per reason per 30 s with a `(+N similar)` count — e.g. `(no /decide answer in 0.25s)`, `(/decide answered HTTP 404)`, `(/decide chose 'local-code' but apply=False (ruleset default@1, mode 'shadow'))`;
- `[route-policy] POST …/observe failed: …` and `… dropped: N calls already in flight`, throttled the same way.

**On the box**, after `make up litellm`:

```bash
# the settings reached the container
make exec litellm litellm CMD="sh -c 'env | grep ^ROUTE_POLICY_'"
# "single pass is live" once, and no fallback lines
make -s logs litellm FOLLOW=0 TAIL=all 2>&1 | grep -E '\[route-policy\]|Traceback'
```

Deploy the service's `enforce` ruleset **before** this LiteLLM: until then every `auto` turn is served by `local-private` and logs `(… apply=False … mode 'shadow')`. Send one `auto` turn with a recognisable chat id and look it up in the service:

```bash
curl -s http://localhost:4001/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -H "X-OpenWebUI-Chat-Id: smoke-1" -H "x-litellm-call-id: smoke-call-1" \
  -d '{"model":"auto","messages":[{"role":"user","content":"Say hi in five words."}]}' > /dev/null
make exec litellm litellm CMD="python -c 'import urllib.request as u; print(u.urlopen(\"http://route-policy:8000/chats/smoke-1/decisions\", timeout=5).read().decode())'"
```

The decision should carry `litellm_call_id: smoke-call-1` and `apply: true`. Its outcome should name the chain and deployment the footer showed.
