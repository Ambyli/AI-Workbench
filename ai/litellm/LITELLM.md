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

The LiteLLM service runs a liveliness probe against `/health/liveliness`. Prometheus scrapes metrics from the proxy on its default endpoint.

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

Every deployment calls its backend **directly** (`api_base: http://qwen3.8-solo:8000/v1`, `anthropic/claude-sonnet-5`, …); its `litellm_params` and `model_info` are copies of the standalone alias of the same name. Never point a chain deployment at a LiteLLM alias: it would recurse through the proxy and inherit that alias's `fallbacks: → claude-sonnet-5`. The standalone aliases (`qwen3.8-solo`, `muse-glimmer`, `claude-*`) are unchanged and still callable directly — **when you change one of them, change its chain copies too.**

#### How a deployment is picked (LiteLLM v1.95.0)

`router.py::async_get_healthy_deployments` (10606-10740) filters in this order:

1. **cooldown** — deployments LiteLLM has cooled down after failures are removed;
2. **overflow hook** — `async_callback_filter_deployments` (10686) calls `ai/litellm/overflow.py`, which drops a local deployment that is busy or down;
3. **pre-call context check** (10694, `enable_pre_call_checks: true`) — drops any deployment whose `model_info.max_input_tokens` is smaller than the prompt. This is why `max_input_tokens` is set per deployment: a prompt over 114688 tokens skips `qwen3.8-solo` for `muse-glimmer` (245760) with no error and no fallback hop;
4. **order filter** (10716-10720) — keeps only the **lowest** `order` left.

A deployment that fails *at call time* (connection refused, 5xx) is retried `num_retries` times and then LiteLLM's **order-based fallback** walks the remaining order levels of the same group (`router.py:6132-6182`). A **timeout** is not retried at the same order: `router_settings.model_group_retry_policy` gives each chain `{TimeoutErrorRetries: 0}`, so a pre-header timeout goes straight to the next order (`router.py:6505-6521`), and a stall after headers takes the router's mid-stream fallback to the next order (`router.py:2066-2150`) — see [§ Chain timeouts](#chain-timeouts). So the chains need, and have, **no `fallbacks` entry**. Order-based fallback is skipped for `ContextWindowExceededError` (6134-6148), which is fine — the pre-call check already handled context.

`local-private` has no Claude deployment and no entry in `fallbacks` or `context_window_fallbacks`. Do not add one. `auto`'s two fallback entries point at `local-private`, because when the router is down nothing has checked the prompt for PII.

#### The overflow hook (`overflow.py`)

Mounted at `/app/overflow.py` next to `/app/config.yaml` and registered with `litellm_settings.callbacks: ["overflow.handler"]` (LiteLLM resolves the module relative to the config file's directory, `proxy/types_utils/utils.py:30-56`). It:

- **polls** `<api_base minus /v1>/metrics` on every local backend it has seen in a chain, **concurrently**, about once a second, and sums `vllm:num_requests_waiting` / `vllm:num_requests_running` across label sets;
- marks a backend **busy** once `waiting > 0` has held for `OVERFLOW_BUSY_AFTER_S`, and clears it once `waiting == 0` has held for `OVERFLOW_IDLE_AFTER_S`;
- marks a backend **down** when its probe fails (refused, timed out, non-200);
- treats a backend as **unknown → keep** when it has never been probed, when its data is older than 5 × the poll interval (the poller stopped), or when `/metrics` answers without the vLLM queue metric;
- per request, and only for a group with ≥ 2 distinct `order` values: drops every deployment with an `api_base` that is busy or down, **never** drops one without an `api_base` (Claude), and if that would leave nothing keeps the busy ones (the request queues in vLLM) or, all down, returns the list unchanged — which is what keeps `local-private` local;
- never throws (LiteLLM re-raises a filter's exception, `router.py:7366-7384`, which would fail the request) and returns the list unchanged on any internal error.

The poller starts lazily on the first chain request and is restarted if it ever dies. Until it has data, the hook changes nothing — LiteLLM behaves exactly as it would without it.

#### Tuning

| Variable | Default | Effect |
|---|---|---|
| `OVERFLOW_BUSY_AFTER_S` | `2` | how long a waiting queue must persist before the backend counts as busy. Lower spills sooner (more Claude spend, less queueing); higher tolerates bursts. |
| `OVERFLOW_IDLE_AFTER_S` | `5` | how long the queue must stay empty before a busy backend is used again. Keep it above `BUSY_AFTER` so a backend does not flap. |
| `OVERFLOW_POLL_INTERVAL_S` | `1` | probe cadence (floor 0.2). Data older than 5 × this is ignored. |
| `OVERFLOW_PROBE_TIMEOUT_S` | `0.75` | per-probe timeout; a probe that exceeds it marks the backend **down**. Keep it below the poll interval. |

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
docker compose -f ai/litellm/docker-compose.litellm.yml --env-file .env -p ai-litellm up -d --force-recreate litellm
```

`litellm_config.yaml` and `overflow.py` are single-file bind mounts and LiteLLM reads both only at startup. `make up litellm` does not recreate a running container for a file *content* change, and `docker restart litellm` keeps the original mount — so an editor or `git pull` that replaced the file (new inode) leaves the container reading the old one. The recreate is the one path that always loads the current files. Then check `docker logs litellm` for an `overflow` import error.

#### Seeing spills

- `docker logs litellm 2>&1 | grep '\[overflow\]'` — one INFO line per spill decision (alias, dropped deployment, reason, waiting count, next order), throttled to one per alias/deployment/reason per 30 s with a `(+N similar)` count. An `every deployment is busy/down … keeping …` line is `local-private` (or a chain whose Claude deployment was cooled down) choosing to queue.
- **LiteLLM Admin UI → Logs / spend by key**: the row's *model group* is the chain alias, its *model* the deployment that answered. Claude overflow is any row with a `local-*` model group and an `anthropic/…` model. A `local-private` row with a Claude model must never exist.
- In-process state: `handler.state()` returns the per-backend verdict, queue depths, timers and counters (`spills`, `kept_all_overloaded`, `errors`, `poller_restarts`). It lives in the proxy process; it is there for debugging with a REPL or a temporary log line, not as an endpoint.

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
- `async_pre_call_hook` strips footers from the incoming conversation (below).

**Edge cases.**

- **Skipped:** a choice with tool calls (or `content` empty / `None`); any request with `response_format`; reasoning-only output. `reasoning_content` is never touched.
- **Open WebUI background tasks are skipped.** v0.11.4 sends title / tags / follow-up / search-query / autocomplete / emoji requests to the *chat* model unless a Task Model is set, and a footer would land in chat titles and break the tags / follow-ups JSON. Its task payloads carry `metadata.task`, but `routers/openai.py` pops `metadata` before the request leaves Open WebUI and forwards no task header — so the prompt is the only signal. Detected: a last user message starting `### Task:` (the default title, tags, image-prompt, follow-up, query and autocomplete templates, `config.py:2211-2340`) or `Your task is to reflect the speaker's likely facial expression` (emoji, `config.py:2439`); a system message starting `Available Tools:` (legacy function calling, `utils/middleware.py:1351-1373`); and, for admin-customised templates, a non-streaming request whose last user message contains a `<chat_history>…</chat_history>` block. MOA is a user-visible answer and is not skipped.
- **Belt and braces:** set **Admin Settings → Interface → Task Model** (local *and* external) to a non-`auto` model — e.g. `qwen3.8-solo`, or `local-private` if chats may carry customer data. A custom task template that matches none of the patterns above would otherwise get a footer.
- **Previous footers are stripped** from incoming assistant messages on `auto` requests (string content, or the last text part of list content; `role: "assistant"` only), so the model never sees — and imitates — them and the router's keyword signals never read them. The regex matches only the exact emitted form: a blank line, `---`, then one `*…*` line of at least two ` · `-separated parts, at the very end. A non-streaming answer that imitates the footer has it replaced, not doubled.
- `n > 1`: once per qualifying choice; never twice for the same response.
- **Never throws:** any internal error returns the response or stream unmodified. `handler.state()["stats"]` counts `footers`, `footers_stripped` and `footer_errors`.
- The footer is presentation only: spend logs, token counts and cost are those of the answer without it.

**Toggle.** `AUTO_FOOTER_ENABLED` (default `true`; accepts `true`/`false`/`1`/`0`, also `yes`/`no`/`on`/`off`), passed through the compose `environment:` like the `OVERFLOW_*` variables. Off disables all three hooks, stripping included. A change to `overflow.py` itself needs the recreate in [§ Applying a chain or hook change](#applying-a-chain-or-hook-change).

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
