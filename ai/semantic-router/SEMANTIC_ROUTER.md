# Semantic Router

[vLLM Semantic Router](https://github.com/vllm-project/semantic-router) (`vllm-sr`) sitting **beside** LiteLLM. Open WebUI picks one model, `auto`; the router extracts signals from the request, matches a *decision*, and runs a bounded multi-model algorithm over the aliases this stack already serves.

| | |
|---|---|
| Compose file | [`docker-compose.semantic-router.yml`](docker-compose.semantic-router.yml) |
| Router config | [`config.yaml`](config.yaml) — hand-written, schema-checked |
| Envoy config | [`envoy.yaml`](envoy.yaml) — **generated**, never hand-edited |
| Init image | [`Dockerfile.models-init`](Dockerfile.models-init) + [`prepare.py`](prepare.py) |
| Postman | [`semantic-router.postman_collection.json`](semantic-router.postman_collection.json) |
| Stack | `make up semantic-router` → `vllm-sr-models-init vllm-sr-router vllm-sr-envoy` |
| Pinned by | `SEMANTIC_ROUTER_VERSION` (`v0.3.0`), `SEMANTIC_ROUTER_ENVOY_VERSION` (`v1.34.14`) |

**Read [§ What is still unverified](#what-is-still-unverified) before trusting anything here in production.** This subsystem was built from the shipped wheel's source, not from a running deployment.

---

## Request path

```
  Open WebUI / Claude Code / n8n
        │  model: "auto"
        ▼
  litellm:4000                       alias auto -> openai/vllm-sr/auto
        │  Authorization: Bearer SEMANTIC_ROUTER_LISTENER_KEY  (attached by LiteLLM)
        ▼
  vllm-sr-envoy:8899                 the ONLY OpenAI surface the router has
        │  1. Lua filter checks the bearer token        -> 401 if wrong
        │  2. strips x-vsr-looper-* / x-authz-* inbound -> no forged control headers
        │  3. ext_proc, BUFFERED request + response body
        ▼
  vllm-sr-router:50051 (gRPC)        signals -> decision -> algorithm
        │
        │  one sub-request per candidate, sent to
        │  global.integrations.looper.endpoint
        ▼
  vllm-sr-envoy:8899                 router sets x-selected-model: <candidate>
        │  Envoy matches that header to the candidate's cluster
        ▼
  litellm:4000                       Authorization: Bearer SEMANTIC_ROUTER_LITELLM_KEY
        ▼
  qwen3.6 / qwen3.8 / qwen3.8-solo / qwen3.8-flash / glm5.2   (vLLM, llama.cpp)
  claude-sonnet-5                                             (Anthropic, via LiteLLM)
```

Three things fall out of that shape:

1. **It is a loop.** LiteLLM calls the router, the router calls LiteLLM. See [§ Recursion guard](#recursion-guard).
2. **Bodies are buffered.** Nothing reaches the caller until the last candidate in the round finishes. Time to first token is the *slowest* candidate, not the fastest.
3. **Envoy is the enforcement point, not the router.** The bearer check and the inbound header stripping are both Envoy filters; the router trusts what reaches it.

---

## Model pool

Concurrency, not model count, is the binding constraint. Every parallel algorithm has to fit the slots the backends actually have.

### Concurrency budget

| LiteLLM alias | Backend | Parallel slots | Logprobs | Role |
|---|---|---|---|---|
| `qwen3.6` | vLLM, 1 GPU | 3 | yes | cheap first rung (confidence) |
| `qwen3.8` | vLLM, TP=2 | 16 | yes | workhorse; static target, panel member |
| `qwen3.8-solo` | vLLM, 1 GPU (device 2) | 3 | yes | declared, unused by current decisions — the stand-in when `muse-glimmer` holds the pair |
| `qwen3.8-flash` | llama.cpp, MTP | **2** | verify only | strong reasoner; the real ceiling on every parallel round |
| `glm5.2` | llama.cpp, CPU MoE offload | 1 | verify only | declared, unused by current decisions — escalation tail if you want one |
| `claude-sonnet-5` | Anthropic, via LiteLLM | API limits | **no** | last confidence rung |

`qwen3.8-flash`'s two slots are why `coding_ratings` sets `max_concurrent: 2`. Raising it does not buy parallelism — it queues inside llama.cpp and every request in the round waits.

### Excluded on purpose

| Alias | Why |
|---|---|
| `glm5.3-flash` | CPU-only, one slot, **minutes** to first token on a cold prompt. In a parallel round it blocks the whole round. |
| `muse-glimmer` | Shares its GPU pair with `qwen3.8`; the two cannot run together. The router health-checks *LiteLLM*, not the model behind it, so it would keep selecting a candidate that is not running. |

The mutual exclusion is also why the stack leans on LiteLLM's `fallbacks` and `on_error: skip` rather than router-side ejection: from the router's point of view every alias is always up.

---

## Recipe table

One entrypoint, `vllm-sr/auto`. Which algorithm runs is decided by which **decision** the request's signals match — highest `priority` first.

| Priority | Decision | Signal | Algorithm | Candidates | Cost per turn |
|---|---|---|---|---|---|
| 300 | `privacy_local` | keyword `privacy_kw` (ssn, credit card, date of birth, bank account, medical record, …) | `static` | `qwen3.8` | 1 call |
| 200 | `coding_ratings` | keyword `code_kw` (python, sql, docker, stack trace, regex, …) | `ratings`, `max_concurrent: 2` | `qwen3.8` + `qwen3.8-flash` | 2 calls, parallel |
| 150 | `reasoning_remom` | keyword `reasoning_kw` (prove, derive, theorem, integral, step by step, …) | `remom`, `breadth_schedule: [3, 2]`, `max_concurrent: 3` | `qwen3.8` + `qwen3.8-flash` | **6 calls** (3 + 2 + synthesis) |
| 10 | `default_confidence` | catch-all (`conditions: []`) | `confidence`, `avg_logprob`, threshold `-0.30` | `qwen3.6` → `qwen3.8` → `claude-sonnet-5` | 1–3 calls, sequential |

Notes that bite:

- **`privacy_local` is about where tokens do *not* go.** It exists so PII and customer data never reach Anthropic. Verify it by spend, not by output: after a PII prompt the router's virtual key should show `qwen3.8` and nothing against `claude-sonnet-5`.
- **`ratings` returns several `choices`.** Open WebUI renders only the first. Use Postman to see the rest. There is no judge and no synthesis in this algorithm — that was Fusion, which does not exist in v0.3.0.
- **`remom` is the expensive one.** `[3, 2]` plus synthesis is six model calls for one user turn. `include_intermediate_responses: true` means the per-round attempts come back in the body.
- **`avg_logprob` thresholds are negative.** Closer to zero = more confident (`ConfidenceAlgorithmConfig`, default `-1.0`). A positive threshold makes everything look confident and the ladder never escalates. Tune `-0.30` in [§ Phase 5 validation matrix](#phase-5-validation-matrix) toward a 20–40 % escalation rate.
- **`claude-sonnet-5` is last on purpose.** Anthropic returns no logprobs, so that rung can be *arrived at* but never *scored*. Moving it earlier means switching `confidence_method` to `margin` or `hybrid`, or the rung scores as a failure and `on_error: skip` silently swallows it.
- **Signals are keyword-only today (Step 2a).** Lexical, so they miss paraphrases. That is the accepted trade for a stack that routes correctly before any classifier weights are on the box. The Step 2b upgrade to the domain classifier is sketched in a commented block in `config.yaml`.

---

## How to add a decision

1. **Add the signal** under `routing.signals` in `config.yaml`. A keyword signal has exactly four fields — `name`, `operator` (`OR`/`AND`), `keywords`, `case_sensitive`. There is no `method: bm25` and no `bm25_threshold`; those do not exist in this release.
2. **Add the decision** under `routing.decisions`. `name`, `description` (**required** — a missing one fails validation), `priority`, `rules`, `modelRefs`, `algorithm`. Give it a priority that slots it correctly against the four above; ties are not worth discovering empirically.
3. **Pick an algorithm that exists**: `confidence`, `ratings`, `remom` (looper) or `static`, `elo`, `router_dc`, `automix`, `hybrid`, `knn`, `kmeans`, `svm`, `mlp`, `multi_factor`, `session_aware`, `latency_aware`, `rl_driven`, `gmtrouter` (selection). `AlgorithmConfig` is `extra="forbid"`, so anything else is a hard failure, and the config block must match the type (`type: remom` + `remom: {…}`).
4. **Budget the concurrency** against [§ Concurrency budget](#concurrency-budget) before adding a parallel algorithm.
5. **Validate locally** — see below. Then `make up semantic-router` (the init service validates again and gates the router on it).

A decision that changes `providers.models` — a new candidate, a new backend — also changes the Envoy render. Re-render.

### Validating a config edit without Docker

```bash
uv tool install vllm-sr==0.3.0          # must match SEMANTIC_ROUTER_VERSION minus the `v`
vllm-sr validate --config ai/semantic-router/config.yaml
```

The subcommand is `vllm-sr validate`. **There is no `vllm-sr config validate`** — `vllm-sr config` has only `envoy`, `router`, `migrate`, `import`.

---

## Re-rendering envoy.yaml

`envoy.yaml` is generator output. Upstream renders it from `cli/templates/envoy.template.yaml` and changes that template between releases; hand-editing it is how a subsystem drifts silently.

Re-render whenever you change `config.yaml` **or** bump `SEMANTIC_ROUTER_VERSION`:

```bash
uv tool install vllm-sr==0.3.0
ENVOY_EXTPROC_ADDRESS=vllm-sr-router \
ENVOY_ROUTER_API_ADDRESS=vllm-sr-router \
python -m cli.config_generator \
    ai/semantic-router/config.yaml /tmp/envoy.rendered.yaml
```

Then splice: keep the hand-written comment header at the top of `ai/semantic-router/envoy.yaml` (everything above the first `admin:` line), replace everything from `admin:` onward with the fresh render, and save with **LF** line endings (`.gitattributes` enforces this for `*.yaml`). `vllm-sr config envoy --config ai/semantic-router/config.yaml` prints the same content to stdout if you prefer to eyeball it first.

Both `ENVOY_*_ADDRESS` variables matter: they place the ext_proc cluster. If they are unset the render points at `127.0.0.1` and the init service's diff fails every time.

**You do not have to remember.** `vllm-sr-models-init` re-renders on every `make up semantic-router` and fails the stack with a unified diff if the checked-in file no longer matches. Never work around that check — it is the only thing standing between a version bump and an Envoy config that routes to the wrong place.

### What the render actually produces

- One **route** per `providers.models` entry, matched on `x-selected-model` exact-equals the model name.
- One **cluster** per model. All of them resolve to `litellm:4000` — the generator emits a cluster per model and there is no knob to collapse them into a shared `litellm_cluster`.
- `LOGICAL_DNS` + `dns_lookup_family: V4_ONLY`, because `litellm` is a name and not an IP.
- A **default route** (no `x-selected-model`) to `models[0]` — currently `qwen3.6`. Reordering `providers.models` changes the fallback target.
- `request_headers_to_remove` on the virtual host, dropping `x-vsr-looper-request` / `-secret` / `-decision` / `-iteration` and `x-authz-user-id` / `-groups` from every inbound request. This is upstream's own hardening and it is already correct — a caller on 8021 cannot forge a looper control header or spoof an identity.
- Route `timeout` from `listeners[0].timeout` (1800s). `idleTimeout` stays at the template's hard-coded **1200s** — it is not parameterised, so a single upstream that goes quiet for more than 20 minutes is cut regardless.

---

## Recursion guard

LiteLLM → router → LiteLLM is the design. Two independent guards keep it from becoming an infinite loop, and **both** must hold:

1. **`providers.models` lists only concrete LiteLLM aliases.** Never `auto`, never `vllm-sr/auto`, never any other router entrypoint name. Every entry there becomes an Envoy route keyed on `x-selected-model`, so a router entrypoint listed there would route the router's own sub-requests straight back into itself and spin until the 1800 s listener timeout. The entrypoint name is namespaced `vllm-sr/auto` precisely so a collision is visible.
2. **`SEMANTIC_ROUTER_LITELLM_KEY` is scoped to the candidates and nothing else.** Create it in the LiteLLM Admin UI restricted to `qwen3.6`, `qwen3.8`, `qwen3.8-solo`, `qwen3.8-flash`, `glm5.2`, `claude-sonnet-5`. With that scoping, a mis-edited `config.yaml` 401s on the first sub-request instead of looping. Give it a monthly budget too — a `remom` turn is six model calls and Anthropic is in the pool.

The LiteLLM-side `fallbacks` map deliberately does **not** list a router entrypoint as a fallback target:

```yaml
fallbacks:
  - {"auto": ["qwen3.8", "claude-sonnet-5"]}
context_window_fallbacks:
  - {"auto": ["qwen3.8-flash"]}
```

---

## Secrets

| Variable | Held by | Notes |
|---|---|---|
| `SEMANTIC_ROUTER_LISTENER_KEY` | Envoy (Lua filter) + LiteLLM (`api_key` on the `auto` alias) | Bearer token for `PORT_SEMANTIC_ROUTER`. `openssl rand -hex 32`. Anyone holding it can spend through the whole candidate pool, Anthropic included. |
| `SEMANTIC_ROUTER_LITELLM_KEY` | `vllm-sr-router` env, resolved via `backend_refs[].api_key_env` — **and** Envoy (Lua filter) | LiteLLM virtual key, scoped to the six candidates. Also a valid bearer on `PORT_SEMANTIC_ROUTER`, because the router's looper sub-requests carry it — see below. See [§ Recursion guard](#recursion-guard). |

Both fail the stack when unset (`${VAR:?…}` in the compose file).

### Why the Envoy key table holds BOTH keys

Looper sub-requests do not go from the router straight to LiteLLM. They go to `global.integrations.looper.endpoint`, which is this stack's own Envoy listener, so that Envoy can resolve each candidate by `x-selected-model`. The router's HTTP client (`pkg/looper/client.go::CallModel`) signs each of those sub-requests with `Authorization: Bearer <accessKey>`, where `accessKey` is the **candidate model's** backend `api_key` — i.e. `SEMANTIC_ROUTER_LITELLM_KEY` — and then sets `x-vsr-looper-request: true`. It does not forward the caller's bearer, and there is no looper bypass in the Lua filter.

So the Lua `VALID_KEYS` table must contain both keys, or every `confidence` / `ratings` / `remom` sub-request is 401'd by Envoy before it ever reaches LiteLLM and only the `static` decision keeps working. Upstream never hits this because its reference config has `api_keys: []`, which omits the Lua filter entirely. This is a code-reading conclusion, not a runtime-verified one — it is item 1 in [§ What is still unverified](#what-is-still-unverified).

### Why neither key is in envoy.yaml

Envoy has **no environment-variable substitution in config files**, and vllm-sr's generator inlines each `listeners[].api_keys` value verbatim into a Lua table. So a working config and a committable config are mutually exclusive. The resolution:

- `config.yaml` and `envoy.yaml` both carry the literal placeholders `__SEMANTIC_ROUTER_LISTENER_KEY__` and `__SEMANTIC_ROUTER_LITELLM_KEY__`.
- `vllm-sr-models-init` substitutes both real values and writes the result to the `vllm_sr_envoy` volume.
- `vllm-sr-envoy` mounts that volume read-only at `/etc/envoy` — which is why it is a volume mount and not a bind mount of `./envoy.yaml`.

**If every request 401s, that substitution did not run.** Read `docker logs vllm-sr-models-init` before anything else. **If direct calls work but every looper decision fails with a 401 in the router log**, the LiteLLM key did not make it into the table — same log.

Note that the router itself does not enforce listener auth — the `api_keys` field in `config.yaml` is consumed only by the Envoy generator.

### Rotating

```bash
# edit SEMANTIC_ROUTER_LISTENER_KEY in .env, then:
make up semantic-router      # recreate -> init re-runs -> new key substituted
make up litellm              # LiteLLM picks up the new env

# rotating SEMANTIC_ROUTER_LITELLM_KEY: mint the new virtual key in the
# LiteLLM Admin UI, edit .env, then the same recreate -- the init re-renders
# the Envoy key table AND the router re-reads api_key_env:
make up semantic-router
```

---

## Ports and exposure

| Port | Where | Published? |
|---|---|---|
| `8899` (Envoy listener) | `vllm-sr-envoy` | **yes** — `PORT_SEMANTIC_ROUTER`, default `8021` |
| `9901` (Envoy admin) | `vllm-sr-envoy` | never — serves `/quitquitquit` and a config dump **containing the listener bearer token** |
| `50051` (ext_proc gRPC) | `vllm-sr-router` | never |
| `8080` (management API) | `vllm-sr-router` | never — the healthcheck hits it inside the container |
| `9190` (metrics) | `vllm-sr-router` | never — prometheus scrapes `vllm-sr-router:9190` over `ai_shared` |
| `8700` (dashboard) | `vllm-sr-dashboard` | `127.0.0.1` only, behind the `dashboard` compose profile |

`8021` is **not** behind oauth2-proxy. The listener bearer token is the only thing in front of it. Do not expose it on an untrusted network; if you want it browser-reachable, front it at `chat.zeoenergy.com/vllm-sr/` through `OAUTH2_PROXY_UPSTREAMS` the way `/n8n/` and `/sandboxes/*` are done.

The dashboard can edit the live router config and has no auth of its own. Start it deliberately:

```bash
docker compose -f ai/semantic-router/docker-compose.semantic-router.yml \
  --env-file .env -p ai-semantic-router --profile dashboard up -d
```

### `router_net` — what it is and is not

`router_net` is a `bridge` with `internal: true` carrying the Envoy → router gRPC hop. **It does not hide `50051` from `ai_shared`.** Both containers also sit on `ai_shared` — the router needs it so prometheus can scrape `9190` and so it has a gateway to Hugging Face — and container ports are reachable across any shared bridge whether or not they are published. `router_net` is a dedicated segment for the hop and a statement of intent, not a control. Taking the router off `ai_shared` *would* hide it, at the cost of the metrics scrape and the model-download path.

---

## Reading `route_diagnostics.looper`

The response body carries a `route_diagnostics` object, and its `looper` section is the only way to tell the decision paths apart from outside the container. Check it for:

- **which decision matched** — if a code prompt shows `default_confidence`, the keyword signal missed; add the term to `code_kw`.
- **which algorithm ran** — should be one of `confidence` / `ratings` / `remom` / `static`, matching [§ Recipe table](#recipe-table).
- **which candidates were called, and which failed** — `on_error: skip` means a dead candidate is silently dropped, so a `remom` round that quietly degraded to two responses looks fine in the answer and only shows up here.
- **how far confidence escalated** — a `default_confidence` turn that always reaches `claude-sonnet-5` means the threshold is wrong (remember: negative, closer to zero is more confident).

Cross-check against the router's own logs (`make logs semantic-router vllm-sr-router`) and against LiteLLM spend-by-key in the Admin UI, which is the authoritative record of what was actually called.

> The exact field names and nesting inside `route_diagnostics.looper` are **not** verified — see [§ What is still unverified](#what-is-still-unverified). Capture one real response during the Phase 0 spike and paste its shape here.

---

## Phase 0 spike checklist

Everything below was derived from the shipped `vllm-sr` 0.3.0 wheel's source, not from a running router. Run these on a laptop with the pip CLI before trusting the deployment, and fold the answers back into this file.

- [ ] `uv tool install vllm-sr==0.3.0`; confirm `vllm-sr --help` matches the command set assumed here (`serve config validate model eval status logs stop dashboard chat`).
- [ ] `vllm-sr serve --config <a copy of config.yaml>` against a throwaway LiteLLM virtual key, and confirm the router **starts** with this config — the CLI validates the pydantic surface, the Go router validates the `global:` block, and only the second one can reject `global.router.auto_model_name`, `global.integrations.looper`, `global.services.*.enabled: false` and `global.stores.semantic_cache.enabled: false`.
- [ ] Confirm `global.router.auto_model_name: vllm-sr/auto` really is the trigger, and that a request with a concrete model name passes through unrouted.
- [ ] Hit `/v1/models` and record exactly what it lists.
- [ ] One request per decision (plain / PII / code / math). Confirm the intended decision matches and capture a full `route_diagnostics.looper` payload.
- [ ] Confirm logprobs survive LiteLLM for the vLLM aliases and for `qwen3.8-flash` (`"logprobs": true` on a plain LiteLLM call). If they do not, `confidence_method: avg_logprob` is dead and the catch-all decision needs `margin` or a self-verify method instead.
- [ ] Note how the router treats `reasoning_content` from the `qwen3` / `muse_glimmer` reasoning parsers — whether it counts toward the confidence score.
- [ ] `docker inspect` the CLI-started router and Envoy containers and diff the mounts / env / entrypoint against `docker-compose.semantic-router.yml`.
- [ ] List `~/.vllm-sr/models` (or wherever the router put them) and record **what** it downloaded and **how big** — this file currently cannot name the bundles or their sizes.
- [ ] Confirm the router does not need Redis / Postgres / Milvus once `global.services` and `global.stores` are disabled as they are here.

**Exit criteria:** all four decisions return a response on the pinned tag; a real `route_diagnostics.looper` payload is captured; logprob pass-through is known per backend family.

---

## Phase 5 validation matrix

Manual — there is no test suite anywhere in this repo.

| Check | How | Pass when |
|---|---|---|
| Stack comes up clean | `make up semantic-router` on a box with no `vllm_sr_*` volumes | init exits 0; router reports ready; envoy healthy |
| Init guards work | Break a field in `config.yaml`, then `make up semantic-router` | init exits non-zero with the CLI's validation error and the router never starts |
| Envoy drift guard works | Edit `config.yaml` without re-rendering, then `make up semantic-router` | init fails with a unified diff naming the stale file |
| Each decision answers | Postman `Envoy listener (direct)` folder, all four prompts | 200 with content; `route_diagnostics.looper` names the intended decision and candidates |
| Decision matching | One prompt per decision | router log shows the intended decision; the PII prompt shows **no** `claude-sonnet-5` spend on the router's virtual key |
| Confidence escalation | A trivial prompt vs. a hard one | trivial stops at `qwen3.6`; hard escalates; tune `threshold` toward a 20–40 % escalation rate |
| Concurrency budget | 3 simultaneous `coding_ratings` chats while watching vLLM + llama.cpp logs | no slot starvation, no 5xx; LiteLLM retries stay at 0 |
| Streaming in Open WebUI | Chat on `auto` | UI shows a waiting state (not an error) while the round runs; the reply renders in full |
| LiteLLM fallback | `make down semantic-router vllm-sr-envoy`, then call `auto` | LiteLLM serves it from `qwen3.8`; no 5xx |
| Context-window fallback | Send a prompt over 114688 input tokens to `auto` | routed to `qwen3.8-flash`, not rejected |
| Cost visibility | LiteLLM Admin UI → spend by key | sub-requests attributed to the router's virtual key; the Anthropic share is visible |
| Metrics | `curl localhost:9090` → prometheus targets | the `semantic-router` job is UP against `vllm-sr-router:9190` |
| Exposure | `ss -ltnp` on the box | only `8021` (and `8022` on loopback if enabled); `50051` / `8080` / `9190` / `9901` absent |
| Key rotation | Change `SEMANTIC_ROUTER_LISTENER_KEY`, `make up semantic-router && make up litellm` | old key 401s at Envoy; `auto` through LiteLLM still works |

---

## What is still unverified

Written from the v0.3.0 wheel's source. **No part of this has been run against a live router.** In descending order of how much it would hurt:

1. **That the looper's sub-requests pass the Envoy bearer check.** `pkg/looper/client.go` (v0.3.0) sets `Authorization: Bearer <accessKey>` from the candidate's backend `api_key`, so the LiteLLM virtual key is in the Lua `VALID_KEYS` table alongside the listener key. What is *not* confirmed by running it: that the Go router really resolves `api_key_env` into that `accessKey` for a `base_url`-style `backend_refs` entry, and that nothing else in the sub-request path (the `x-vsr-looper-*` headers, the per-route Lua) rejects it. If the router log shows 401s from `vllm-sr-envoy` on looper calls, this is where to look. Diagnostic: `docker exec vllm-sr-router curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $SEMANTIC_ROUTER_LITELLM_KEY" -H 'x-selected-model: qwen3.6' -H 'content-type: application/json' -d '{"model":"qwen3.6","messages":[{"role":"user","content":"hi"}]}' http://vllm-sr-envoy:8899/v1/chat/completions` should be `200`, not `401`.
2. **The whole `global:` block.** `UserConfig.global_` is `Dict[str, Any]` in the CLI — `vllm-sr validate` does not check inside it, so `global.router.auto_model_name`, `global.router.include_config_models_in_list`, `global.integrations.looper.{endpoint,timeout_seconds}`, `global.services.{response_api,router_replay,startup_status}` and `global.stores.semantic_cache` are all validated only by the **Go router at startup**. The key names come from `cli/config_migration.py::_place_global_block`, which is the CLI's own inventory of canonical keys, but their accepted *shapes* are inferred. If the router refuses to start, this block is the first suspect.
3. **That `auto_model_name` is really the entrypoint mechanism.** v0.3.0 has no `entrypoints:` block, `UserConfig` is `extra="forbid"`, and this is the only key that looks like "the name that triggers routing". Not confirmed by running it.
4. **`route_diagnostics.looper`'s field names.** Referenced throughout this document; the shape is not in the wheel.
5. **Logprob pass-through through LiteLLM**, per backend family. `confidence_method: avg_logprob` needs token logprobs. vLLM emits them, llama.cpp's OpenAI endpoint does when asked, Anthropic does not. If they do not survive, the catch-all decision is scoring noise.
6. **The `-0.30` confidence threshold.** A starting point read off the field's documented sign convention, not a tuned value.
7. **Keyword-signal matching semantics.** `KeywordSignal` carries `operator` / `keywords` / `case_sensitive` and nothing else; whether `OR` means substring, token, or something else is owned by the Go side.
8. **What the router downloads into `/app/models`, and how big.** Nothing in the CLI names the bundles. The 600 s healthcheck `start_period` is a guess; if the download is larger than that, first boot will look like a crash-loop.
9. **Whether `HF_HOME=/app/models` is the right knob** for the router image, as opposed to a vllm-sr-specific path variable. It is in the CLI's env passthrough list, which is suggestive, not conclusive.
10. **The Envoy healthcheck's usefulness.** A `/dev/tcp` connect proves the listener is bound; it says nothing about the ext_proc cluster behind it.
11. **`ulimits: nofile 65536`.** Copied from the CLI's default; not measured against a real `remom` fan-out.

### Deliberate departures from the original plan

The plan was written against the upstream article, which describes five looper algorithms and a recipe/entrypoint config surface. v0.3.0 ships neither.

| Planned | Reality in v0.3.0 | What was built |
|---|---|---|
| Five algorithms incl. Fusion + Workflows | `cli/validator.py` enumerates `{confidence, ratings, remom}`; `AlgorithmConfig` is `extra="forbid"` | `coding_fusion` became `coding_ratings`; no workflows decision |
| `entrypoints:` + `recipes:` blocks | `UserConfig` is `extra="forbid"` over `{version, listeners, providers, routing, global, setup}` | one entrypoint via `global.router.auto_model_name`; decisions live at `routing.decisions` |
| `vllm-sr/fusion`, `/remom`, `/flow`, `/ratings` slugs and `auto-*` LiteLLM aliases | no multi-entrypoint mechanism exists | only `auto`; the algorithm is chosen by signal, not by alias |
| `min_successful_responses`, `round_timeout_seconds`, `quorum_failure_policy`, `quorum_fallback_target` | not fields on `ReMoMAlgorithmConfig` or `RatingsAlgorithmConfig` | `max_concurrent` + `on_error` + the 1800 s listener timeout; quorum failure is covered by LiteLLM `fallbacks` |
| `minimum_candidates`, `escalation_order: declared`, `token_filter` | not fields on `ConfidenceAlgorithmConfig` | escalation order *is* the declared `modelRefs` order |
| `threshold: 0.72` for `avg_logprob` | the field is negative-valued, default `-1.0` | `-0.30` |
| `method: bm25` / `bm25_threshold` on keyword signals | `KeywordSignal` has four fields, none of them these | plain keyword lists |
| Context limits on `providers.models` | `Model` has no context field | `routing.modelCards[].context_window_size` |
| Tool-bearing requests → static `qwen3.8` | no verified request-shape signal for `tools` | not implemented; **`auto` is a chat model, not an agent model** — keep MCP flows on `qwen3.8` directly |
| `models-init` downloads the Vela bundles | no download subcommand exists in the CLI | the init service validates the config, enforces the Envoy re-render, substitutes the listener key, and seeds the volume; the router image downloads |
| `vllm-sr config validate` | does not exist | `vllm-sr validate --config …` |
| Envoy routes to one shared `litellm_cluster` (`STRICT_DNS`) | the generator emits one cluster per model, `LOGICAL_DNS` | left as generated |
| `models-init` needs egress | it has nothing to download | `network_mode: none` |
| `envoy.yaml` bind-mounted from the repo | the listener key would have to be committed | rendered + substituted into the `vllm_sr_envoy` volume by the init service |
