# Signals — fast CPU signals for route-policy

A small CPU sidecar that turns the texts of a conversation into **features**
route-policy's evaluators vote on, fast enough for the turn being decided:

| Task | Model (pinned in [`models.yaml`](models.yaml)) | Features per text |
|---|---|---|
| `pii` | `gravitee-io/bert-small-pii-detection` (BERT-small NER, ONNX fp32) | entity types → `{count, max_score}`, `complete`, `cached` |
| `nearest` | `intfloat/multilingual-e5-small` (embeddings, ONNX fp32) + kNN over labelled examples | `neighbours` `[{id, route, similarity}]`, `routes` `{route: {share, best, count}}`, `cached` |
| `injection` | `NeuralTrust/prompt-guard-oss-small` (mmBERT-small classifier, ONNX fp32) | `score` = P(jailbreak), `windows`, `cached` |
| `language` | fastText `lid.176.ftz` | `language`, `confidence`, `cached` |

Never text, spans or offsets: only types, counts, scores, example ids and
statuses come back.

```bash
curl -s http://localhost:8025/signals -H 'content-type: application/json' -d '{
  "deadline_ms": 1000,
  "texts": [{"id": "u1", "text": "Jane Smith lives at 1420 Oak Street, Springfield."}],
  "tasks": [{"task": "pii", "texts": ["u1"]}, {"task": "language", "texts": ["u1"]}]}' | jq .results
# → {"pii": {"status": "ok", "ms": 9.8, "texts": {"u1": {"entities": {"person": {"count": 2, "max_score": 0.923},
#                                                        "street_address": {"count": 1, "max_score": 0.953}}, ...}}},
#    "language": {"status": "ok", ...}}
```

| | |
|---|---|
| Compose file | [`docker-compose.signals.yml`](docker-compose.signals.yml) (one service, `signals`) |
| Image | built locally from [`Dockerfile`](Dockerfile) (build context = repo root), models baked in (~1.2 GB) |
| In-network URL | `http://signals:8000` on `ai_shared` (what route-policy uses: `ROUTE_POLICY_SIGNALS_URL`) |
| Host port | `PORT_SIGNALS`, default **8025** — published by [`ai/edge`](../edge/EDGE.md) (the service is swap-class: no `ports:` of its own), limited to `EDGE_ALLOWED_CIDRS` |
| Auth | none, like the classifier and the detector. No LiteLLM pass-through. |
| Postman | [`signals.postman_collection.json`](signals.postman_collection.json) |
| Wire format | `common.signals` ([`shared/common/src/common/signals/wire.py`](../../shared/common/src/common/signals/wire.py)), shared with route-policy's client |
| Plan | `ROUTE_POLICY_PHASE5_PLAN.md` (outside the repo) — phase 5 of ZT-471 |

## Where it sits

```
LiteLLM hook ── POST /decide (awaited, 0.25 s) ──► route-policy
                                                    load_context, concurrently:
                                                      ├─ verdict lookup (DB, 0.1 s)
                                                      ├─ turn pin       (DB, 0.1 s)
                                                      └─ ONE POST /signals (ROUTE_POLICY_SIGNALS_BUDGET_S = 0.1 s,
                                                            deadline_ms = budget − 10 ms) ──► signals
```

Every evaluator of a ruleset says which signals it will read
(`Evaluator.signal_requests`); route-policy merges them into **one** request
per decision, sends it beside its two database reads, and puts the answer on
the decision context. The sidecar's only failure mode, as far as routing is
concerned, is an **abstain**: down, slow or wrong, it never moves a turn off
`local-private`, and the `pii` evaluator still votes from `common.pii`'s
patterns (which run inside route-policy) without it. See
[`ai/route-policy/ROUTE_POLICY.md`](../route-policy/ROUTE_POLICY.md) for the
evaluators (`pii`, `nearest_examples`, `prompt_injection`, `language`) and
the `default-signals` shadow ruleset that reads them.

## API

### `POST /signals`

```json
{"request_id": "<decision id: logs only>",
 "deadline_ms": 80,
 "texts": [{"id": "u7", "text": "…"}, {"id": "t2", "text": "…"}, {"id": "u6", "text": "…"}],
 "tasks": [
   {"task": "pii",       "texts": ["u7", "t2", "u6"]},
   {"task": "nearest",   "texts": ["u7"], "max_tokens": 256, "k": 5,
    "examples": {"fingerprint": "<sha256>", "items": [{"id": "code-001", "route": "local-code", "text": "…"}]}},
   {"task": "injection", "texts": ["u7"], "max_tokens": 256, "windows": "head_tail"},
   {"task": "language",  "texts": ["u7"]}]}
```

* **Texts** are addressed by id (route-policy: `u<k>` = the user text of
  user message k, `t<k>` = the k-th tool result) and listed **newest first**;
  every task scans its texts in the order it lists them.
* **Each task at most once** — results are keyed by task name.
* **`deadline_ms`** — the answer goes out by then, measured from the parsed
  request. Omitted / `null`: no cut, every task runs to the end (route-policy's
  `/evaluate`), bounded by the wire's 60 s — and never at the expense of a
  request that has a deadline: its jobs yield the model between two steps
  (§ Lanes and deadlines).
* **Params** (each task takes only its own; another is a 422):
  `nearest` — `examples` (required), `k` (default 5, ≤ 50), `max_tokens`
  (default 256, ≤ 512); `injection` — `max_tokens` (per window, default 256,
  ≤ 512), `windows` (`head_tail`, the only mode); `pii`, `language` — none.

→ `200`:

```json
{"request_id": "…", "latency_ms": 41.8,
 "models": {"pii": "gravitee-io/bert-small-pii-detection@f8c27a85c51c",
            "embed": "intfloat/multilingual-e5-small@614241f622f5",
            "injection": "NeuralTrust/prompt-guard-oss-small@40e5c56b68a1",
            "language": "fasttext/lid.176@8f3472cfe873"},
 "results": {
   "pii":       {"status": "ok", "ms": 8.1, "texts": {"u7": {"entities": {"person": {"count": 1, "max_score": 0.97},
                                                                       "street_address": {"count": 1, "max_score": 0.95}},
                                                         "complete": true, "cached": false}}},
   "nearest":   {"status": "ok", "ms": 15.2, "texts": {"u7": {"neighbours": [{"id": "code-003", "route": "local-code", "similarity": 0.871}],
                                                             "routes": {"local-code": {"share": 0.8, "best": 0.871, "count": 4}},
                                                             "cached": false}}},
   "injection": {"status": "deadline", "ms": null, "texts": {}},
   "language":  {"status": "ok", "ms": 0.1, "texts": {"u7": {"language": "en", "confidence": 0.98, "cached": false}}}}}
```

**Task statuses:**

| Status | Meaning | `texts` |
|---|---|---|
| `ok` | every text done | all |
| `partial` | some texts done: the deadline came first (a PII text cut mid-scan is included with `complete: false`), or a text raised — then `error` is the first failure's exception type (`ValueError`), never its message | some |
| `deadline` | the job was running at the deadline, nothing finished yet | none |
| `skipped` | the job was still queued at its deadline: it never runs | none |
| `warming` | `nearest` only: the example set is being embedded (§ Example sets) | none |
| `error` | every text it reached raised and none has features; `error` is the first exception's type name | none |
| `unavailable` | the model is not loaded: `error` is `loading`, or the type its load failed with | none |

A text that fails costs only that text: the others' features still come
back as `partial` (route-policy reads them as usual and counts the failed
text as not scanned), so one history text a model chokes on never makes a
task unusable for the whole chat.

`ms` is the time from the request's arrival until the task's last text was
ready (until the cut, for `partial`); `null` when nothing came back.
`models` keys follow `common.signals.TASK_MODELS` (`nearest` → `embed`); the
id is `<repo>@<first 12 hex of the revision>` (fastText: of its sha256).
Evaluators pin it: route-policy's `model_matches` takes a pin without
`@<revision>` as "any revision of that repo".

**Errors** (no body field ever quotes the request):

| Code | When | Body |
|---|---|---|
| 413 | `Content-Length` over 4 MB (before the body is read), or a cap of `SignalsRequest.cap_problem()`: a text over 20,000 characters, texts over 200,000 in all, more than 64 texts, more than 1,000 examples, an example over 300 characters | `{"detail": "<what> has <n> …"}` — ids and sizes |
| 422 | not JSON; an unknown key; a param a task does not take; a text id no text has; a task named twice; `deadline_ms` ≤ 0; … (the wire's strict `SignalsRequest.from_json`) | `{"detail": "<field>: <rule>"}` — a `WireError` message never carries a value |
| 500 | a bug | `{"detail": "internal error (<ExceptionType>)"}` |

FastAPI's own body validation is deliberately not used: its 422 echoes the
offending value (`input`), which would be a text.

### `GET /models`

The pins and nothing to tune: per lane its `id`, `repo`, `revision` (or
`url`), `licence`, every file's sha256, its token caps and load `state`;
which model serves each task; the PII vocabulary (`common.pii.ENTITY_TYPES`,
the `SENSITIVE_TYPES`, and how the NER's labels map onto them); the request
caps. **No thresholds** — `min_score`, `min_share`, the injection threshold
are ruleset config in route-policy.

### `GET /health`

`200 {"status": "ok", …}` once every model is loaded **and** has run one
warm-up inference; `503` with `status: "loading"` before (about 5–10 s after
a start), or `"failed"` for good when a model failed to load (its `error` is
the exception type). The body also carries the cache size, the example
sets' states and each lane's queue depth. It is the compose healthcheck,
`scripts/deploy.py`'s swap gate and the edge's `httpchk`. While loading, the
app already answers `/signals` — Docker DNS can hand a fresh copy a request
— with `unavailable` for the models not ready yet.

### `GET /metrics`

| Metric | Labels | What |
|---|---|---|
| `signals_requests_total` | `outcome` = ok \| rejected \| error | POST /signals by outcome (rejected = 413 / 422) |
| `signals_request_seconds` | | server time of one answered request |
| `signals_tasks_total` | `task`, `status` | task results |
| `signals_task_seconds` | `task` | arrival → the task's last text |
| `signals_cache_total` | `task`, `result` = hit \| miss | feature-cache lookups |
| `signals_cache_entries` | | |
| `signals_inference_seconds` | `lane` | model compute for one text (all windows) |
| `signals_queue_depth` | `lane`, `queue` = foreground \| background | jobs waiting for a model |
| `signals_jobs_total` | `lane`, `outcome` = finished \| stopped \| skipped \| parked \| replaced | how lane jobs ended |
| `signals_example_sets` | `state` = warming \| ready | |
| `signals_example_set_warmings_total` | `outcome` = ok \| failed \| evicted | |
| `signals_model_ready`, `signals_model_load_seconds` | `lane` | |

Plus prometheus-fastapi-instrumentator's HTTP metrics. Label values come
from fixed lists, never from a request. Scraped by the litellm stack's
prometheus (job `signals`, `signals:8000`).

## Inside

```
POST /signals ─► parse (strict) ─► caps ─► Engine.run
                                            ├─ cache lookups (event loop): hits answered at once
                                            ├─ one job per task with misses ─► its model's Lane
                                            │     pii lane ─ 1 thread ─ ORT session (SIGNALS_THREADS_PII)
                                            │     embed lane ─ 1 thread ─ ORT session (SIGNALS_THREADS_EMBED)
                                            │     injection lane ─ 1 thread ─ ORT session (SIGNALS_THREADS_INJECTION)
                                            ├─ language inline (fastText, microseconds)
                                            └─ wait until the deadline, snapshot every job, answer
```

Code ([`signals/`](signals/)): `app.py` (lifespan, background load),
`api.py` (routes, error shapes), `schemas.py` (the wire on HTTP),
`scheduler.py` (lanes, jobs, `Engine.run`), `cache.py`, `runtime.py`
(models.yaml, sessions, load state, the build check), `tasks/` (one module
per model), `metrics.py`, `config.py`, `fetch_models.py` (build time),
`bench.py`.

### Lanes and deadlines

* **Single flight per model.** Each ONNX model has one worker thread in front
  of one onnxruntime session, so a model never runs two inferences at once
  and CPU use is bounded by the thread knobs (the detector's lock, per
  model). A slow model never delays another.
* **A job that has not started by its deadline is `skipped`, never run late.**
  The plan's burst test showed why: jobs that ran late built a backlog
  without bound, and then every task — even the microsecond language one —
  missed its deadline. `test_a_burst_never_builds_a_backlog` is that
  regression test.
* **Waiting requests are served newest first**: under a burst the request
  most likely to still meet its deadline goes first; the stale ones are
  skipped.
* **A running non-PII job stops between texts at its deadline** (between
  windows too, when another request waits for the model). The text it was on
  finishes and is cached.
* **A running PII job finishes its texts after the answer** and caches them,
  so the same conversation's next call — one turn later, or the next tool
  round — sees its whole history scanned. Past its deadline it hands the
  model to any waiting request between two windows (it is *parked* in the
  lane's background queue), and a lane keeps **at most one** parked PII job
  whose request has its answer: a newer one replaces it, so the backlog
  stays bounded.
* **A request with no deadline is patient** (`deadline_ms` omitted or
  `null`: route-policy's `/evaluate`, the bench). Its jobs queue in the
  background, run while no request with a deadline waits, and hand the model
  to one between two steps — parked, then resumed where they stopped — until
  every text is done. A parked patient job is never replaced while its
  request waits (each is bounded by its own request, and the wire's 60 s).
  Before this, a 20-text `/evaluate` held the lanes for its whole run, and a
  `/decide` arriving meanwhile answered `skipped` for every task.
* **Background work** (a parked PII job, a patient job, an example set being
  embedded) runs only while no request waits, oldest first.

### Token windows

| Task | Window |
|---|---|
| `pii` | the whole text, in windows of 256 tokens overlapping by 16; spans one entity in both windows are merged |
| `nearest` | `"query: " + text`, the first `max_tokens` (default 256), mean-pooled, L2-normalised |
| `injection` | the head and the **tail** `max_tokens` (left truncation) — one window when the text fits; the score is the larger |
| `language` | the whole text, whitespace collapsed (fastText reads one line) |

### PII: labels onto the vocabulary

The NER's 25 labels map onto `common.pii.ENTITY_TYPES`
([`signals/tasks/pii.py`](signals/tasks/pii.py)):

| Model label | Type |
|---|---|
| `LOCATION` **with an ASCII digit** in the span | `street_address` |
| `LOCATION` without one | `location` (never forces) |
| `PERSON` | `person` (forces only with sensitive data — the ruleset's `with_person`; never alone) |
| `EMAIL_ADDRESS`, `PHONE_NUMBER` | `email`, `phone` |
| `US_SSN`, `US_PASSPORT`, `US_DRIVER_LICENSE`, `US_ITIN` | `gov_id` |
| `CREDIT_CARD` | `card_number` |
| `IBAN_CODE`, `US_BANK_NUMBER` | `bank_account` |
| `FINANCIAL` | `account_number` |
| everything else (`DATE_TIME`, `AGE`, `ORGANIZATION`, `URL`, `IP_ADDRESS`, `PASSWORD`, …) | `other` |

`DATE_TIME` is any date ("this weekend"), so it is not `date_of_birth`: a
birth date needs context, which `common.pii`'s patterns read. Token decoding
follows what the plan measured (`LOCATION` + digit took the model's recall
from 0.58 to 0.89 on the plan's PII set): a token joins a span when its
label's probability is ≥ 0.5 (`config.PII_TOKEN_MIN_PROB`, model
post-processing, not routing) and it carries `I-` of the same entity; the
reported `max_score` is the span's best token, which a ruleset's `min_score`
filters on top. `FINANCIAL` → `account_number` matches the measured mapping;
if Trino shows it firing on amounts, drop `account_number` from the
ruleset's `force` list rather than change the mapping (that would be a new
calibration).

### Cache

An in-memory LRU of `sha256(task, model id, params, text)` → features
(`SIGNALS_CACHE_ENTRIES`, default 20,000, ~40 MB at worst; 0 disables it).
The key is a digest, so the text is never kept; a `nearest` entry is the
query vector (the kNN against the current example set is recomputed —
microseconds). `cached: true` in a feature means it came from here. Gone on
restart, and each copy has its own: during a swap a request may hit the
copy that has not seen the history yet, which costs time, not correctness.

### Example sets

A `nearest` task carries its example set (`fingerprint` + `items`, the
route-policy ruleset's `examples_ref`). The sidecar keys embedded sets by
the fingerprint **and** a digest of the items under the model it runs
(`common.signals.fingerprint_examples`), so the same fingerprint sent with
other items is embedded again, never answered from the wrong set.

* The first request naming an unknown set starts embedding it on the embed
  lane, in the background, 16 examples per batch (a waiting request takes the
  model between two batches), and answers `warming` for that task.
* Later requests vote from it. At most 8 sets are held, the least recently
  used ready set evicted first.
* route-policy warms the bound rulesets' sets at startup with a request that
  carries no texts, so a new route-policy copy is ready before it serves.
* A failed warming is forgotten (`signals_example_set_warmings_total{outcome="failed"}`);
  the next request tries again.

## Models

Pinned in [`models.yaml`](models.yaml) by repo, **revision (commit sha)** and
**sha256 per file**. The Dockerfile downloads every file at build time into a
plain directory, `/models/<lane>/` — not the Hugging Face cache: onnxruntime
refuses external data (`model.onnx.data`, the injection model's weights) that
resolves outside the model's own directory, which the cache's symlinks make
it do — verifies each hash, writes `/models/<lane>/NOTICE.txt`, and then runs
`python -m signals.runtime --check`: every hash again, every model loaded with
the default threads, one warm-up inference each, and a fixed set of
fabricated sentences with known answers (`runtime.smoke`: a name + address +
phone → `person`, `street_address`, `phone`; an attack scores ≥ 0.5 and a
benign sentence below; a code question is nearer a code example than a
weather one; Spanish is `es`). A broken pin fails the **build**, never the
first request. At run time the container downloads nothing
(`HF_HUB_OFFLINE=1`; nothing imports `huggingface_hub`).

| Lane | Model | Revision | Licence | Size | Resident |
|---|---|---|---|---|---|
| `pii` | [`gravitee-io/bert-small-pii-detection`](https://huggingface.co/gravitee-io/bert-small-pii-detection) | `f8c27a85c51c…` | Apache-2.0 | 114 MB | ~210 MB |
| `embed` | [`intfloat/multilingual-e5-small`](https://huggingface.co/intfloat/multilingual-e5-small) | `614241f622f5…` | MIT | 470 MB | ~1.2 GB (250k-token vocabulary) |
| `injection` | [`NeuralTrust/prompt-guard-oss-small`](https://huggingface.co/NeuralTrust/prompt-guard-oss-small) | `40e5c56b68a1…` | MIT | 565 MB | ~355 MB |
| `language` | fastText `lid.176.ftz` | sha256 `8f3472cfe873…` | CC-BY-SA-3.0 | 0.9 MB | — |

Why these (the plan's § 2 measured the alternatives): bert-small-pii plus
the stdlib patterns reached P 0.95 / R 0.97 on the force rule, far ahead of
GLiNER and Presidio at a fraction of `openai/privacy-filter`'s cost; e5's
kNN beat a zero-shot classifier by 37 points and handles Spanish against
English examples; NeuralTrust had the best AUC (0.958) and the lowest benign
false-positive rate (1.3 %) of the injection guards, MIT-licensed and
ungated; fastText got 15/16 at microseconds. **int8 quantisation is not used:**
it broke the DeBERTa guards in the measurement and buys little here.

**Licences.** Each Hugging Face lane keeps its model card (`README.md`, a
pinned file) beside the weights, which carries the authors' licence
declaration, and `NOTICE.txt` names the model, its pin and its licence.

**fastText attribution** (CC-BY-SA-3.0): *lid.176.ftz, the fastText language
identification model by Facebook AI Research (A. Joulin, E. Grave,
P. Bojanowski, T. Mikolov, "Bag of Tricks for Efficient Text
Classification", 2016; "FastText.zip: Compressing text classification
models", 2016), from
<https://fasttext.cc/docs/en/language-identification.html>, licensed under
the Creative Commons Attribution-Share-Alike License 3.0
(<https://creativecommons.org/licenses/by-sa/3.0/>). Used unmodified.* The
same text is in `/models/language/NOTICE.txt`. Share-alike binds modified
copies that are distributed; this image uses the file unmodified, internally.
`lingua` (Apache-2.0, 14/16 in the measurement) is the alternative with no
share-alike, should that ever matter.

### Changing a model

1. Find the new revision and every file's sha256 (`curl -s
   "https://huggingface.co/api/models/<repo>?blobs=true"` lists the LFS
   sha256s; small files: download and `sha256sum`).
2. Edit `models.yaml` (revision + hashes; files and token caps if they change).
3. `make build signals` — the build fails if a hash or the smoke check is off
   (a model whose answers moved there is not the one the rulesets were
   calibrated for).
4. A **new route-policy ruleset version** for every evaluator that pins the
   model id: its thresholds belong to the old model. Until it is bound, the
   pinned evaluators abstain with `model mismatch` — never route.

## CPU pinning

The box is a Xen VM: 32 vCPUs (Xeon W-2295, one NUMA node), and the guest
sees only `avx2 fma f16c` — the hypervisor hides AVX-512 / VNNI, so every CPU
model there runs below the hardware's speed (passing the host features
through is a hypervisor change, outside this repo). Measured there
(`box_bench.py`, 2026-10-20, p50 ms at 20 / 200 / 2,000 characters):

| Model | 1 thread | **2 threads** | 4 threads |
|---|---|---|---|
| pii | 4.9 / 15.5 / 151 | 3.1 / 9.0 / 82 | 2.3 / 5.9 / 44 |
| injection | 19 / 59 / 614 | 11.6 / 36 / 337 | 8.9 / 24 / 191 |
| embed | 11 / 32 / 152 | 7.2 / 19 / 85 | 4.9 / 12 / 45 |
| all three at once (one call) | 20 / 60 / 648 | **13 / 38 / 339** | 26 / 68 / 281 |

Four threads each (12 in all) contend, so short messages get slower: **2 per
model** (`SIGNALS_THREADS_PII` / `_EMBED` / `_INJECTION`), at most 6 busy
threads plus the event loop. A turn up to ~1,000 characters gets every
signal inside the 80 ms route-policy sends. At 2,000 characters PII and topic
take ~82 / 85 ms alone, so they are at the edge: PII answers with the windows
it scanned (`complete: false`) and finishes late into the cache for the next
turn, topic may abstain, and injection (~340 ms) misses its deadline and
abstains, which is harmless while it is `observe`-only.

**Cores.** The sidecar gets cores **24–31** of its own
(`SIGNALS_CPUSET`, compose `cpuset: "${SIGNALS_CPUSET-24-31}"`);
`qwen3.5-4b` runs on 0–15 (`LLAMA_QWEN35_4B_THREADS=16`,
`LLAMA_QWEN35_4B_EXTRA_ARGS="--cpu-range 0-15"`, see
[`ai/llama/LLAMA.md`](../llama/LLAMA.md)) and everything else on 16–23. A
148 GB CPU model using every core (`glm5.3-flash`, off today) would otherwise
multiply the sidecar's latency — give it a `--cpu-range` that excludes 24–31
before turning it on. `cpu_shares: 2048` favours the sidecar when it is not
pinned.

The variable is read **without the colon** on purpose: unset → `24-31`; an
**empty** `SIGNALS_CPUSET=` in `.env` → no pinning (an empty cpuset is
Docker's default, every CPU). Use the empty value on a host with fewer than
32 CPUs, where `24-31` does not exist and the container would refuse to
start. Check what a running copy got:

```bash
docker inspect $(docker ps -qf label=com.docker.compose.service=signals) \
  --format '{{.Name}} cpuset={{.HostConfig.CpusetCpus}} shares={{.HostConfig.CPUShares}}'
lscpu -e | head -40        # the core layout, to choose the ranges
```

## The bench

`python -m signals.bench` is the box measurement productised: the pinned
models from `/models`, through the sidecar's own code, on synthetic text.

```bash
make exec signals signals CMD='python -m signals.bench'                 # the configured threads
make exec signals signals CMD='python -m signals.bench --threads 1,2,4 --n 30'
```

It prints the CPU (model, the SIMD flags onnxruntime cares about, the cores
the process may use — `SIGNALS_CPUSET`), each model alone (p50 / p95 per text
of 20, 200, 2,000 characters), and one `/signals` call with all four tasks on
a cold cache: p50 / p95 with no deadline, then how often each task is `ok`
inside 80 ms. It loads a **second** copy of the models (~1.8 GB, inside
`mem_limit`) on the same cores as the live service: run it when the box is
quiet, or it measures the contention.

## Privacy

* **No text in responses, logs, metric labels or error strings.** Features
  are types, counts, scores, example ids and statuses. A 422 carries the
  wire's `WireError` message (field + rule), a 413 ids and sizes, a 500 and
  a task's `error` only an exception **type** — a tokenizer or onnxruntime
  message can quote its input, so messages and tracebacks are never logged
  either. The one INFO line per request has the request id (only when it
  looks like an id), text and character counts, each task's status and ms.
* **No spans or offsets** leave the process: spans live inside one PII scan
  and come out as type counts.
* **No disk writes:** `read_only: true`, a tmpfs `/tmp` (also `HOME`), no
  volume; the only state is the in-memory cache (digests → features) and
  the embedded example sets.
* **It reaches nothing:** `ai_shared` only, no outbound call at run time, no
  database. Text gets here only from route-policy, whose `common.signals`
  client refuses a sidecar URL that resolves outside the box's networks.
* Unprivileged user, `cap_drop: [ALL]`, `no-new-privileges`.

`unit-tests/signals/test_signals_privacy.py` puts a sentinel string in every
text, example and fake model exception, and checks it is in no response
(200 / 413 / 422 / 500), no log record at any level, no `/health`,
`/models` or `/metrics` output, and that handling requests opens no file for
writing.

## Deploy

Swap-class (`com.zeo.deploy: swap`, see [`ai/edge/EDGE.md`](../edge/EDGE.md)):
stateless apart from the cache, CPU-only, and on the `/decide` path, so a
recreate would leave NER and topic down for its load time. `make up signals`
starts `signals-<hash>` beside the old copy, waits for `/health` (all four
models warm) and for the edge to mark it UP, then stops the old one
(`stop_grace_period: 15s`). During the swap Docker DNS gives both copies
traffic; equal model ids make that harmless, and a model change between
copies makes the pinned evaluators abstain for those seconds.

Order on the box (first time):

1. `make up edge` — the edge gains the `listen signals` on 8025.
2. `make build signals && make up signals` (the build downloads ~1.2 GB and
   runs the model check).
3. `make build route-policy && make up route-policy` — seeds the
   `default-signals` shadow ruleset and warms its example set.
4. `docker restart prometheus` — the `signals` scrape job.

```bash
curl -s localhost:8025/health | jq .status          # "ok"
curl -s localhost:8025/models | jq '.models | map_values(.id)'
make logs signals signals                             # one [signals] line per request
```

RAM: ~1.8 GB per copy, two during a swap (`mem_limit`, `SIGNALS_MEM_LIMIT`,
default `4g`). Rollback: `git revert`, `make up route-policy` (its binding
loses the shadow slot), then `docker compose -p ai-signals down`.

### Configuration

Infrastructure only — thresholds and routes are ruleset config.

| Variable | Default | |
|---|---|---|
| `PORT_SIGNALS` | `8025` | host port, published by `ai/edge` |
| `SIGNALS_CPUSET` | `24-31` | cores the container may use; **empty = no pinning** (§ CPU pinning) |
| `SIGNALS_THREADS_PII` / `_EMBED` / `_INJECTION` | `2` / `2` / `2` | onnxruntime intra-op threads per model |
| `SIGNALS_CACHE_ENTRIES` | `20000` | feature-cache entries; `0` disables it |
| `SIGNALS_MEM_LIMIT` | `4g` | compose `mem_limit` (two copies run during a swap) |
| `SIGNALS_MODELS_DIR` | `/models` | where the baked models are; set in the image, only changed to run the code against a local download |
| `LOG_LEVEL` | `INFO` | |

## Tests

```bash
UV_LINK_MODE=copy uv run --no-sync python -m pytest unit-tests/signals -q -p no:cacheprovider
```

Everything runs on fake models (`unit-tests/signals/sig_helpers.py`) through
the real scheduler, cache, API and privacy code: the wire contract and caps,
deadlines / skips / single flight / newest first / PII finishing late / the
burst backlog / no-deadline requests yielding / a failed text costing only
itself, label mapping and span rules, example-set warming, the
sentinel checks, the compose / Dockerfile / Postman wiring.
`test_signals_real_models.py` loads the pinned files and **skips** unless
they are present and the Python has the service's dependencies (the shared
workspace venv lacks `fasttext-predict`):

```bash
python ai/signals/signals/fetch_models.py --dest /tmp/signals-models    # ~1.2 GB, verified
SIGNALS_MODELS_DIR=/tmp/signals-models PYTHONPATH=shared/common/src \
  <a venv with ai/signals' dependencies>/bin/python -m pytest unit-tests/signals -q -p no:cacheprovider
```

The same model checks run in every image build (`python -m signals.runtime --check`).
