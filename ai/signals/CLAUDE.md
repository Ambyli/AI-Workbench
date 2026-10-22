# ai/signals

Route-policy's CPU sidecar: small ONNX / fastText models turn a conversation's texts into features (`pii` NER, `nearest` e5 embeddings + kNN, `injection` score, `language`) that route-policy's signal evaluators vote on, inside the turn being decided. One service, `signals` (FastAPI, `signals:8000` on `ai_shared`, no auth). Guide: [`SIGNALS.md`](SIGNALS.md). Compose: [`docker-compose.signals.yml`](docker-compose.signals.yml). Tests: `unit-tests/signals` (`SIGNALS.md § Tests`).

## Rules

- **It never logs or stores text.** Responses, logs, metric labels and error strings carry types, counts, scores, example ids and statuses — never text, spans or offsets; an exception becomes its TYPE name (a tokenizer / onnxruntime message can quote its input). FastAPI body validation is not used because its 422 echoes the value. `read_only: true`, tmpfs `/tmp`, no volume, no database, no outbound call, `cap_drop: [ALL]`. `unit-tests/signals/test_signals_privacy.py` checks every response, log record and metric with a sentinel — extend it for any new output path.
- **Models are pinned and baked.** `models.yaml` pins each by repo, revision (commit sha) and sha256 per file; the Dockerfile downloads at BUILD time into `/models/<lane>/` (not the HF cache — onnxruntime refuses external data that resolves outside the model dir), verifies every hash and runs `python -m signals.runtime --check`. Nothing downloads at run time (`HF_HUB_OFFLINE=1`). No torch, no transformers, no CUDA. int8 is not used (it broke the guards in the measurement).
- **Changing a model** = `models.yaml` edit + `make build signals` + a new route-policy ruleset version for every evaluator pinning that model id (`SIGNALS.md § Changing a model`).
- **fastText `lid.176.ftz` is CC-BY-SA-3.0**: its attribution lives in `SIGNALS.md § Models` AND the image's `/models/language/NOTICE.txt`. Keep both.
- **One batched call per decision; deadlines are hard.** Each model has one single-flight lane (one onnxruntime session, `SIGNALS_THREADS_*` threads). A job not started by its deadline is `skipped`, never run late (`test_a_burst_never_builds_a_backlog`); only a running PII job finishes after the answer, to fill the cache. A request with no deadline is *patient*: it yields the model to any request with a deadline between two steps, and a parked patient job is never replaced.
- **Keep `sys.setswitchinterval(0.001)`** (`config.GIL_SWITCH_INTERVAL_S`): at CPython's 5 ms default the event loop answered past route-policy's cut and every partial result was dropped.
- **Statuses** `ok | partial | deadline | skipped | warming | error | unavailable`; anything but `ok` / `partial` is an abstain. A failing text costs only that text. A slow, down or wrong sidecar never moves a turn.
- **Caps (413):** 20,000 chars per text, 200,000 per request, 64 texts — they live in `common.signals.wire`, not here.
- **`/health` is 503 until every model is loaded and warmed** (~10 s): it is the compose healthcheck, the swap gate and the edge's check.
- **No thresholds here** — `min_score`, `min_share`, the injection threshold, example sets are route-policy ruleset config.
- Measure on the box with `make exec signals signals CMD='python -m signals.bench'` (`--threads 1,2,4 --n 30`); it loads a second copy of the models on the same cores, so run it when the box is quiet.

## Cross-folder rules

Also in the root `CLAUDE.md` § Cross-folder rules — read the named entry there for the full picture.

- **CPU core split** — this container gets cores 24–31 (`SIGNALS_CPUSET`); `qwen3.5-4b` 0–15, everything else 16–23. A CPU model on these cores multiplies its latency. (root `CLAUDE.md` § Cross-folder rules)
- **Signals contract** — the wire format and client are `common.signals` (`shared/common/src/common/signals/`), shared with route-policy's client: change both sides together; a model id change needs new route-policy ruleset versions. (root `CLAUDE.md` § Cross-folder rules)
- **Swap-class services publish through the edge** — `com.zeo.deploy: swap`, no `container_name` / `ports:`; `PORT_SIGNALS` 8025 is the edge's `listen signals` (75 s timeouts). During a swap both copies get traffic (harmless with equal model ids). (root `CLAUDE.md` § Deploys (`make up`))
- **Deploy order across stacks** — `make up edge` (first time) → `make build signals && make up signals` → `make build route-policy && make up route-policy` (warms its example set; the Makefile lists `signals` first) → `docker restart prometheus` (job `signals`). Rollback: `SIGNALS.md § Deploy`. (root `CLAUDE.md` § Cross-folder rules)

## Env vars

One line each; full detail in `.env.example` and `SIGNALS.md § Configuration` / `§ CPU pinning`. All have compose defaults.

- `PORT_SIGNALS` (`8025`) — host port, published by `ai/edge`, limited to `EDGE_ALLOWED_CIDRS`; for LAN operators only (route-policy calls `signals:8000`).
- `SIGNALS_CPUSET` (`24-31`) — compose `cpuset: "${SIGNALS_CPUSET-24-31}"`, read **without the colon**: unset → `24-31`, EMPTY → no pinning. Leave it empty on a host with < 32 CPUs (the container refuses to start otherwise).
- `SIGNALS_THREADS_PII` / `_EMBED` / `_INJECTION` (`2` / `2` / `2`) — onnxruntime intra-op threads per model session; 4 each contend and short messages get slower.
- `SIGNALS_CACHE_ENTRIES` (`20000`) — in-memory LRU `sha256(task, model id, params, text)` → features (never text, ~40 MB worst); `0` disables; per copy, gone on restart.
- `SIGNALS_MEM_LIMIT` (`4g`) — compose `mem_limit`; ~1.8 GB per copy, two copies during a swap, and the bench loads a second set.
