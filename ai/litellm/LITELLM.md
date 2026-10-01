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
- `overflow.py` — the chain-alias overflow hook, mounted at `/app/overflow.py` beside the config (see [§ Chain aliases and the overflow hook](#chain-aliases-and-the-overflow-hook))

### Health checks

The LiteLLM service runs a liveliness probe against `/health/liveliness`. Prometheus scrapes metrics from the proxy on its default endpoint.

### Stopping

```bash
docker compose -f ai/litellm/docker-compose.litellm.yml down
```

Data in PostgreSQL is persisted in the `litellm_postgres_data` named volume and survives container restarts.

### `litellm_config.yaml` settings

The proxy configuration file supports a wide range of options for models, routing, rate limits, and more. See the full reference: [LiteLLM Config Settings](https://docs.litellm.ai/docs/proxy/config_settings)

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

A deployment that fails *at call time* (connection refused, 5xx, timeout) is retried `num_retries` times and then LiteLLM's **order-based fallback** walks the remaining order levels of the same group (`router.py:6132-6182`). So the chains need, and have, **no `fallbacks` entry**. Order-based fallback is skipped for `ContextWindowExceededError` (6134-6148), which is fine — the pre-call check already handled context.

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

Set them in `.env` and `make up litellm` (they are passed through the compose `environment:` block). Turning the hook off is removing the `callbacks:` line — the chains then fail over only on real errors.

#### Seeing spills

- `docker logs litellm 2>&1 | grep '\[overflow\]'` — one INFO line per spill decision (alias, dropped deployment, reason, waiting count, next order), throttled to one per alias/deployment/reason per 30 s with a `(+N similar)` count. An `every deployment is busy/down … keeping …` line is `local-private` (or a chain whose Claude deployment was cooled down) choosing to queue.
- **LiteLLM Admin UI → Logs / spend by key**: the row's *model group* is the chain alias, its *model* the deployment that answered. Claude overflow is any row with a `local-*` model group and an `anthropic/…` model. A `local-private` row with a Claude model must never exist.
- In-process state: `handler.state()` returns the per-backend verdict, queue depths, timers and counters (`spills`, `kept_all_overloaded`, `errors`, `poller_restarts`). It lives in the proxy process; it is there for debugging with a REPL or a temporary log line, not as an endpoint.

#### Adding a chain

1. Add one `model_list` entry per rung under a new `model_name` (e.g. `local-vision`), each with `order: N`, its own `api_base` and its own `model_info.max_input_tokens`. Copy the params from the standalone alias; do not reference it.
2. Decide whether it may reach Claude. If not, give it no Claude deployment and **no** `fallbacks` / `context_window_fallbacks` entry, like `local-private`. If it may, put Claude on the highest order only.
3. If the semantic router should use it, add it to `providers.models` + `routing.modelCards` in `ai/semantic-router/config.yaml`, re-render `envoy.yaml`, and add the alias to `SEMANTIC_ROUTER_LITELLM_KEY`'s model list in the Admin UI.
4. Nothing to configure in the hook — it learns backends from the deployments it sees. A non-vLLM local backend (llama.cpp) has no `vllm:num_requests_waiting`; the hook then treats it as **unknown** and never spills on its account, only failing over on real errors.
5. `make up litellm`, then add a Postman item under **Chain aliases** in `litellm.postman_collection.json`.

The overflow hook's unit tests live in [`unit-tests/litellm/test_overflow.py`](../../unit-tests/litellm/test_overflow.py) and drive the real v1.95.0 `Router` when `litellm` is installed:

```bash
uv venv /tmp/llvenv && UV_LINK_MODE=copy uv pip install --python /tmp/llvenv litellm==1.95.0 pytest
/tmp/llvenv/bin/python -m pytest unit-tests/litellm -q -p no:cacheprovider
```
