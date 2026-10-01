# Semantic Router

[vLLM Semantic Router](https://github.com/vllm-project/semantic-router) (`vllm-sr`) sitting **beside** LiteLLM. Open WebUI picks one model, `auto`; the router reads signals from the prompt, matches a *decision*, and hands the request to one LiteLLM **chain alias**. LiteLLM then decides which backend answers — local first, Claude only as overflow.

| | |
|---|---|
| Compose file | [`docker-compose.semantic-router.yml`](docker-compose.semantic-router.yml) |
| Router config | [`config.yaml`](config.yaml) — hand-written, schema-checked |
| Envoy config | [`envoy.yaml`](envoy.yaml) — **generated**, never hand-edited |
| Init image | [`Dockerfile.models-init`](Dockerfile.models-init) + [`prepare.py`](prepare.py) |
| Chain aliases + overflow hook | [`../litellm/litellm_config.yaml`](../litellm/litellm_config.yaml), [`../litellm/overflow.py`](../litellm/overflow.py) — see [LITELLM.md § Chain aliases and the overflow hook](../litellm/LITELLM.md#chain-aliases-and-the-overflow-hook) |
| Postman | [`semantic-router.postman_collection.json`](semantic-router.postman_collection.json) |
| Stack | `make up semantic-router` → `vllm-sr-models-init vllm-sr-router vllm-sr-envoy` |
| Pinned by | `SEMANTIC_ROUTER_VERSION` (`v0.3.0`), `SEMANTIC_ROUTER_ENVOY_VERSION` (`v1.34.14`) |

**Read [§ What is still unverified](#what-is-still-unverified) before trusting anything here in production.** This subsystem was built from the shipped wheel's source, not from a running deployment.

---

## The policy

1. **Local models first, for cost.** `qwen3.8-solo` and `muse-glimmer` answer everything they can.
2. **Claude only as overflow.** It is reached only when **both** local models in a chain are busy or down — never because an answer looked unsure.
3. **Customer / PII data never reaches Claude.** Not as overflow, not as a fallback, not when the router is down.

Everything below is how those three rules are enforced.

---

## Two layers

| Layer | Where | Decides | Mechanism |
|---|---|---|---|
| 1 — **which chain** | the semantic router (`config.yaml`) | the *kind* of prompt | keyword signals → one `static` decision → one chain alias |
| 2 — **which backend** | LiteLLM (`litellm_config.yaml` + `overflow.py`) | local vs. Claude | each chain alias is a model group whose deployments carry `order: 1 / 2 / 3`; the overflow hook drops a busy or dead local deployment so LiteLLM's order filter moves to the next order |

The router never sees a backend and never chooses Claude. LiteLLM never sees the prompt's *kind*, only the chain it was given. That split is what makes the privacy rule enforceable: `local-private` has no Claude deployment at all, so no amount of load or failure can route PII to Anthropic.

### The chains (layer 2)

| Chain alias | order 1 | order 2 | order 3 (overflow only) |
|---|---|---|---|
| `local-general` | `qwen3.8-solo` | `muse-glimmer` | `claude-sonnet-5` |
| `local-code` | `qwen3.8-solo` | `muse-glimmer` | `claude-sonnet-5` |
| `local-reasoning` | `muse-glimmer` | `qwen3.8-solo` | `claude-opus-5-5` |
| `local-private` | `qwen3.8-solo` | `muse-glimmer` | **— none —** |

Each deployment calls its backend **directly** (`http://qwen3.8-solo:8000/v1`, `http://muse-glimmer:8000/v1`, `anthropic/…`) with `litellm_params` copied from the standalone alias — never through the standalone alias, which would drag in that alias's own `fallbacks: → claude-sonnet-5`. How LiteLLM walks the orders, and how the hook decides "busy", is in [LITELLM.md § Chain aliases and the overflow hook](../litellm/LITELLM.md#chain-aliases-and-the-overflow-hook).

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
  vllm-sr-router:50051 (gRPC)        signals -> decision -> chain alias
        │  rewrites body "model" to the chain alias, sets x-selected-model,
        │  replaces Authorization with Bearer SEMANTIC_ROUTER_LITELLM_KEY
        ▼
  vllm-sr-envoy:8899                 routes on x-selected-model to that chain's cluster
        ▼
  litellm:4000                       model group local-general / -code / -reasoning / -private
        │  cooldown -> overflow hook -> context check -> lowest `order` left
        ▼
  qwen3.8-solo / muse-glimmer        (vLLM, direct)            orders 1-2
  claude-sonnet-5 / claude-opus-5-5  (Anthropic, direct)       order 3, overflow only
```

One user turn is **one** request into a chain — no fan-out. The `Authorization` swap on the forwarded request is the router's own (`pkg/extproc/processor_req_body_routing.go::appendCredentialHeaders`, v0.3.0): it injects `Bearer <backend api key>`, i.e. the router's LiteLLM virtual key.

Three things fall out of that shape:

1. **It is a loop.** LiteLLM calls the router, the router's request lands back in LiteLLM. See [§ Recursion guard](#recursion-guard).
2. **Bodies are buffered.** Nothing reaches the caller until the chain's answer is complete — including any time `local-private` spends queued inside vLLM.
3. **Envoy is the enforcement point, not the router.** The bearer check and the inbound header stripping are both Envoy filters; the router trusts what reaches it.

---

## Decisions

One entrypoint, `vllm-sr/auto`. Decisions are matched highest `priority` first; every one is `static` with exactly one `modelRefs` entry.

| Priority | Decision | Signal | Chain alias | Calls per turn | Can reach Claude? |
|---|---|---|---|---|---|
| 300 | `privacy_local` | keyword `privacy_kw` (ssn, credit card, date of birth, bank account, medical record, customer address, …) | `local-private` | 1 | **never** |
| 200 | `code` | keyword `code_kw` (python, sql, docker, stack trace, regex, …) | `local-code` | 1 | `claude-sonnet-5`, only when both locals are busy/down |
| 150 | `reasoning` | keyword `reasoning_kw` (prove, derive, theorem, integral, step by step, …) | `local-reasoning` | 1 | `claude-opus-5-5`, only when both locals are busy/down |
| 10 | `general` | catch-all (`conditions: []`) | `local-general` | 1 | `claude-sonnet-5`, only when both locals are busy/down |

`providers.models` lists exactly these four chain aliases; `local-general` is `models[0]` (Envoy's default route) and `providers.defaults.default_model`. The concrete aliases (`qwen3.8-solo`, `muse-glimmer`, `claude-*`) are deliberately **not** in the pool — they carry LiteLLM `fallbacks` to `claude-sonnet-5`.

Notes that bite:

- **`privacy_local` is about where tokens do *not* go.** It is enforced by `local-private` having no Claude deployment and no `fallbacks` / `context_window_fallbacks` entry. Verify it by spend, not by output — see [§ Verification checklist](#verification-checklist).
- **The privacy signal is lexical.** A PII prompt that trips none of `privacy_kw` lands on `general` and *can* overflow to Claude under load. Widen `privacy_kw`, or — for a hard guarantee — have the caller (an n8n flow, a script) call `local-private` directly rather than `auto`.
- **Signals are keyword-only today (Step 2a).** They miss paraphrases. A miss is cheap now — every chain is local-first — except for the privacy case above. The Step 2b upgrade to the domain classifier is sketched in a commented block in `config.yaml`.
- **`routing.modelCards[].context_window_size` is not a gate.** At v0.3.0 the Go router uses it only as a scoring input to selection algorithms (`extproc/req_filter_classification.go::modelContextWindows` → `selection/cache_affinity.go`); a `static` decision never reads it. It is set to 245760 on all four chains (the largest *local* window each can reach, via `muse-glimmer`) for documentation and for any future selection algorithm. The real context gate is LiteLLM's `enable_pre_call_checks`.

### Why there are no loopers any more

v0.3.0 still ships three looper algorithms — `confidence`, `ratings`, `remom` (`cli/validator.py`) — and this config used to run all three. They were removed for **cost**:

| Removed decision | What it did | Why it went |
|---|---|---|
| `default_confidence` | `qwen3.8-solo`, escalate to `claude-opus-5-5` while the answer's `avg_logprob` was below `-0.30` | escalating on an answer's confidence is a *quality judgement* buying Claude — exactly what the policy forbids. It also cost a Sonnet **and** an Opus call per turn whenever `qwen3.8-solo` was down (LiteLLM's fallback answered rung one without logprobs, which scored as a failure). |
| `coding_ratings` | `ratings`, `qwen3.8-solo` + `muse-glimmer` in parallel | 2 local calls per turn, and Open WebUI rendered only the first `choice` anyway |
| `reasoning_remom` | `remom`, `breadth_schedule: [3, 2]` + synthesis | **6** local calls per turn |

Fan-out doubles (or sextuples) the load on the two local models, which is precisely what fills their vLLM queues and makes the overflow hook spill to Claude. One call per turn keeps local capacity for local answers. Re-adding a looper means re-reading [§ Two layers](#two-layers) first: any looper candidate must still be a chain alias, never a concrete alias.

---

## Concurrency budget

Concurrency, not model count, is the binding constraint.

| Backend | Parallel slots | Context (`max_input_tokens`) | In chains |
|---|---|---|---|
| `qwen3.8-solo` — vLLM, 1 GPU (device 2, shared with the OWLv2 `detector`) | **3** (`--max-num-seqs 3`) | 114688 | order 1 of `local-general` / `-code` / `-private`; order 2 of `-reasoning` |
| `muse-glimmer` — vLLM, TP=2 on the GPU pair + DFlash | 4 (`--max-num-seqs 4`) — **shared with the classifier** | 245760 | order 2 of `local-general` / `-code` / `-private`; order 1 of `-reasoning` |
| `claude-sonnet-5` / `claude-opus-5-5` — Anthropic | API limits | not declared | order 3 only |

- **Every turn is one call.** A turn costs one slot on whichever backend the chain lands on. There is no per-decision `max_concurrent` to budget any more.
- **A full vLLM queue is the spill signal.** vLLM accepts more requests than it has sequences and queues the rest (`vllm:num_requests_waiting`). The overflow hook treats a waiting queue held for `OVERFLOW_BUSY_AFTER_S` (default 2 s) as **busy** and drops that deployment from the chain; it clears after `OVERFLOW_IDLE_AFTER_S` (default 5 s) with an empty queue.
- **`muse-glimmer` is contended by the classifier.** The classifier calls `http://muse-glimmer:8000` directly — not through LiteLLM — with up to `CLASSIFIER_MAX_LLM_CALLS=4` requests in flight, which is every one of its 4 sequences. A classifier batch can therefore make `muse-glimmer` "busy" for the chains, and with `qwen3.8-solo` also busy, `local-general` / `-code` / `-reasoning` spill to Claude for the duration. `local-private` queues instead.
- **`muse-glimmer` and `qwen3.8` share the GPU pair.** When an operator brings `qwen3.8` up, `muse-glimmer` is down: its `/metrics` probe fails, the hook marks it **down**, and every chain degrades to `qwen3.8-solo` alone — spilling to Claude (or queueing, for `local-private`) whenever `qwen3.8-solo` is busy.

### Not in the pool

| Alias | Why |
|---|---|
| `qwen3.8-solo`, `muse-glimmer`, `claude-sonnet-5`, `claude-opus-5-5`, `claude-opus-4-8` | **Concrete aliases are not on the router's key.** They are still LiteLLM aliases and still callable directly by other clients; the router reaches the same backends only as chain deployments. |
| `qwen3.6`, `qwen3.8`, `qwen3.8-flash`, `glm5.2` | Not in any chain. Adding one is: a new deployment in a chain group in `litellm_config.yaml` (with its own `order`), no router change. |
| `glm5.3-flash` | CPU-only, one slot, **minutes** to first token on a cold prompt. As a chain deployment it would hold a turn for most of the 1800 s timeout. |

---

## The privacy guarantee — and the router-down path

The old config had a **known gap**: `privacy_local` routed to `qwen3.8-solo`, and LiteLLM's `fallbacks: qwen3.8-solo → claude-sonnet-5` meant a dead `qwen3.8-solo` sent the PII prompt to Anthropic. **That gap is closed.**

| Situation | What happens to a PII prompt |
|---|---|
| Router up, `privacy_kw` matched | `local-private`: `qwen3.8-solo`, else `muse-glimmer`. Both busy → it queues locally. Both down → it **fails**. Never Claude. |
| Router up, `privacy_kw` **missed** | lands on `general` → `local-general`, which *can* overflow to `claude-sonnet-5`. This is the remaining soft spot: the keyword list. |
| Router / Envoy **down** | `fallbacks: auto → [local-private]` — LiteLLM cannot know whether an unrouted prompt carries PII, so it goes local-only. Also the cheapest answer. |
| Prompt over `auto`'s 245760-token window | `context_window_fallbacks: auto → [local-private]`, which refuses it too. Never Claude. |

**Why not fall back to `qwen3.8-solo` / `muse-glimmer` when the router is down?** Because LiteLLM *chains* fallbacks: `run_async_fallback` re-enters `async_function_with_fallbacks` with the fallback model group, which re-reads the map and finds `qwen3.8-solo → claude-sonnet-5` / `muse-glimmer → claude-sonnet-5` (LiteLLM v1.95.0 `router_utils/fallback_event_handlers.py:120-137`; depth cap `ROUTER_MAX_FALLBACKS=5`). An unclassified prompt would reach Anthropic one hop later. `local-private` has no entry in either map, so the chain stops there.

**Why 245760 on `auto`?** It is the largest prompt every path out of `auto` can serve *locally*: all four chains contain `muse-glimmer`, LiteLLM's pre-call check skips `qwen3.8-solo` (114688) for anything longer, and `local-private` — where both `auto` fallbacks land — reaches `muse-glimmer` too. A higher number would admit prompts only a Claude deployment could take.

---

## How to add a decision

1. **Add the signal** under `routing.signals` in `config.yaml`. A keyword signal has exactly four fields — `name`, `operator` (`OR`/`AND`), `keywords`, `case_sensitive`. There is no `method: bm25` and no `bm25_threshold`; those do not exist in this release.
2. **Point it at a chain alias.** If none of the four fits, add a new chain in `litellm_config.yaml` first ([LITELLM.md § Adding a chain](../litellm/LITELLM.md#adding-a-chain)), add it to `providers.models` + `routing.modelCards`, and scope `SEMANTIC_ROUTER_LITELLM_KEY` to it in the LiteLLM Admin UI. Never point a decision at a concrete alias (`qwen3.8-solo`, `claude-*`): those carry Claude fallbacks.
3. **Add the decision** under `routing.decisions`. `name`, `description` (**required** — a missing one fails validation), `priority`, `rules`, `modelRefs`, `algorithm: {type: static}`. Give it a priority that slots it correctly against the four above.
4. **Validate locally** — see below. Then `make up semantic-router` (the init service validates again and gates the router on it).

A change to `providers.models` changes the Envoy render. Re-render.

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

- One **route** per `providers.models` entry, matched on `x-selected-model` exact-equals the chain alias.
- One **cluster** per chain. All four resolve to `litellm:4000` — the generator emits a cluster per model and there is no knob to collapse them into a shared `litellm_cluster`.
- `LOGICAL_DNS` + `dns_lookup_family: V4_ONLY`, because `litellm` is a name and not an IP.
- A **default route** (no `x-selected-model`) to `models[0]` — `local-general`. Reordering `providers.models` changes the fallback target; keep `local-general` first.
- `request_headers_to_remove` on the virtual host, dropping `x-vsr-looper-request` / `-secret` / `-decision` / `-iteration` and `x-authz-user-id` / `-groups` from every inbound request. This is upstream's own hardening and it is already correct — a caller on 8025 cannot forge a looper control header or spoof an identity.
- Route `timeout` from `listeners[0].timeout` (1800s). `idleTimeout` stays at the template's hard-coded **1200s** — it is not parameterised, so a single upstream that goes quiet for more than 20 minutes is cut regardless.

---

## Recursion guard

LiteLLM → router → LiteLLM is the design. Two independent guards keep it from becoming an infinite loop, and **both** must hold:

1. **`providers.models` lists only chain aliases.** Never `auto`, never `vllm-sr/auto`, never any other router entrypoint name. Every entry there becomes an Envoy route keyed on `x-selected-model`, so a router entrypoint listed there would route the router's own requests straight back into itself and spin until the 1800 s listener timeout. The entrypoint name is namespaced `vllm-sr/auto` precisely so a collision is visible.
2. **`SEMANTIC_ROUTER_LITELLM_KEY` is scoped to the four chain aliases and nothing else**: `local-general`, `local-code`, `local-reasoning`, `local-private`. With that scoping, a mis-edited `config.yaml` 401s on the first request instead of looping — and the router cannot reach a concrete alias that carries a Claude fallback. Give the key a monthly budget too: three of the four chains end in Claude, and a sustained local overload spends it.

The chain deployments call their backends directly, so a chain can never recurse back into `auto` either.

---

## Secrets

| Variable | Held by | Notes |
|---|---|---|
| `SEMANTIC_ROUTER_LISTENER_KEY` | Envoy (Lua filter) + LiteLLM (`api_key` on the `auto` alias) | Bearer token for `PORT_SEMANTIC_ROUTER`. `openssl rand -hex 32`. Anyone holding it can spend through all four chains, Claude overflow included. |
| `SEMANTIC_ROUTER_LITELLM_KEY` | `vllm-sr-router` env, resolved via `backend_refs[].api_key_env` — **and** Envoy (Lua filter) | LiteLLM virtual key, scoped to the four chain aliases (`local-general`, `local-code`, `local-reasoning`, `local-private`). The router puts it on every forwarded request. Also a valid bearer on `PORT_SEMANTIC_ROUTER` — see below. See [§ Recursion guard](#recursion-guard). |

Both fail the stack when unset (`${VAR:?…}` in the compose file).

### Why the Envoy key table holds BOTH keys

A *looper* sub-request goes to `global.integrations.looper.endpoint` — this stack's own Envoy listener — signed by the router's HTTP client (`pkg/looper/client.go::CallModel`) with `Authorization: Bearer <accessKey>`, where `accessKey` is the candidate's backend `api_key`, i.e. `SEMANTIC_ROUTER_LITELLM_KEY`. So the Lua `VALID_KEYS` table must contain that key as well as the listener key, or every looper sub-request is 401'd by Envoy.

**No decision uses a looper today**, so the second entry is dormant: a `static` decision's request is forwarded by Envoy straight to the chain's cluster, with the `Authorization` header already rewritten by the router — it never re-enters the listener. The entry stays because `prepare.py` fails the stack if either placeholder is missing from the render, and so that re-enabling a looper does not silently 401.

### Why neither key is in envoy.yaml

Envoy has **no environment-variable substitution in config files**, and vllm-sr's generator inlines each `listeners[].api_keys` value verbatim into a Lua table. So a working config and a committable config are mutually exclusive. The resolution:

- `config.yaml` and `envoy.yaml` both carry the literal placeholders `__SEMANTIC_ROUTER_LISTENER_KEY__` and `__SEMANTIC_ROUTER_LITELLM_KEY__`.
- `vllm-sr-models-init` substitutes both real values and writes the result to the `vllm_sr_envoy` volume.
- `vllm-sr-envoy` mounts that volume read-only at `/etc/envoy` — which is why it is a volume mount and not a bind mount of `./envoy.yaml`.

**If every request 401s from Envoy, that substitution did not run.** Read `docker logs vllm-sr-models-init` before anything else. **If the 401 comes from LiteLLM** (`litellm` in the error body), `SEMANTIC_ROUTER_LITELLM_KEY` is not scoped to the chain alias the router picked — fix the key's model list in the Admin UI.

Note that the router itself does not enforce listener auth — the `api_keys` field in `config.yaml` is consumed only by the Envoy generator.

### Rotating

```bash
# edit SEMANTIC_ROUTER_LISTENER_KEY in .env, then:
make up semantic-router      # recreate -> init re-runs -> new key substituted
make up litellm              # LiteLLM picks up the new env

# rotating SEMANTIC_ROUTER_LITELLM_KEY: mint the new virtual key in the
# LiteLLM Admin UI (scoped to the four chain aliases), edit .env, then the
# same recreate -- the init re-renders the Envoy key table AND the router
# re-reads api_key_env:
make up semantic-router
```

---

## Ports and exposure

| Port | Where | Published? |
|---|---|---|
| `8899` (Envoy listener) | `vllm-sr-envoy` | **yes** — `PORT_SEMANTIC_ROUTER`, default `8025` |
| `9901` (Envoy admin) | `vllm-sr-envoy` | never — serves `/quitquitquit` and a config dump **containing the listener bearer token** |
| `50051` (ext_proc gRPC) | `vllm-sr-router` | never |
| `8080` (management API) | `vllm-sr-router` | never — the healthcheck hits it inside the container |
| `9190` (metrics) | `vllm-sr-router` | never — prometheus scrapes `vllm-sr-router:9190` over `ai_shared` |
| `8700` (dashboard) | `vllm-sr-dashboard` | `127.0.0.1` only, behind the `dashboard` compose profile |

`8025` is **not** behind oauth2-proxy. The listener bearer token is the only thing in front of it. Do not expose it on an untrusted network; if you want it browser-reachable, front it at `chat.zeoenergy.com/vllm-sr/` through `OAUTH2_PROXY_UPSTREAMS` the way `/n8n/` and `/sandboxes/*` are done.

The dashboard can edit the live router config and has no auth of its own. Start it deliberately:

```bash
docker compose -f ai/semantic-router/docker-compose.semantic-router.yml \
  --env-file .env -p ai-semantic-router --profile dashboard up -d
```

### `router_net` — what it is and is not

`router_net` is a `bridge` with `internal: true` carrying the Envoy → router gRPC hop. **It does not hide `50051` from `ai_shared`.** Both containers also sit on `ai_shared` — the router needs it so prometheus can scrape `9190` and so it has a gateway to Hugging Face — and container ports are reachable across any shared bridge whether or not they are published. `router_net` is a dedicated segment for the hop and a statement of intent, not a control. Taking the router off `ai_shared` *would* hide it, at the cost of the metrics scrape and the model-download path.

---

## Seeing what happened to a turn

Two places, and only the second is authoritative about cost:

- **Which decision matched** — the router log (`make logs semantic-router vllm-sr-router`) and the response's `route_diagnostics` object. A code prompt that shows `general` means the keyword signal missed; add the term to `code_kw`. A PII prompt that shows anything but `privacy_local` is the soft spot from [§ The privacy guarantee](#the-privacy-guarantee--and-the-router-down-path) — widen `privacy_kw`.
- **Which backend answered** — LiteLLM's **spend log** (Admin UI → Logs / spend by key). Filter on `SEMANTIC_ROUTER_LITELLM_KEY`: each row's *model group* is the chain alias and its *model* is the deployment that answered (`openai/qwen3.8-solo`, `openai/muse-glimmer`, `anthropic/claude-sonnet-5`, …). Overflow to Claude shows up only here. `docker logs litellm | grep '\[overflow\]'` shows the hook's spill decisions alongside.

> The exact field names inside `route_diagnostics` are **not** verified — see [§ What is still unverified](#what-is-still-unverified).

---

## Verification checklist

Manual — there is no end-to-end suite. The overflow hook's logic has unit tests ([`unit-tests/litellm/test_overflow.py`](../../unit-tests/litellm/test_overflow.py)); everything below needs the box.

| Check | How | Pass when |
|---|---|---|
| Stack comes up clean | `make up semantic-router` on a box with no `vllm_sr_*` volumes | init exits 0; router reports ready; envoy healthy |
| Init guards work | Break a field in `config.yaml`, then `make up semantic-router` | init exits non-zero with the CLI's validation error and the router never starts |
| Envoy drift guard works | Edit `config.yaml` without re-rendering, then `make up semantic-router` | init fails with a unified diff naming the stale file |
| Hook loaded | `docker logs litellm` after `make up litellm` | no import error for `overflow.handler`; first chain request is followed by `[overflow]` lines only when something spills |
| Each decision answers | Postman `Envoy listener (direct)` folder, all four prompts | 200 with content; router log names `privacy_local` / `code` / `reasoning` / `general` |
| Local first | One prompt per decision with both local models idle | spend log on the router's key shows only `openai/qwen3.8-solo` (or `openai/muse-glimmer` for `local-reasoning`) — **zero** Claude rows |
| Busy spill to Claude is visible | Saturate both locals (e.g. 4+ parallel long `auto` chats plus a classifier batch), then send a `general` prompt | `[overflow] local-general: dropping …` lines for both local deployments in `docker logs litellm`, and a spend-log row on the router's key with model `anthropic/claude-sonnet-5` under model group `local-general` |
| Spill to the *other* local first | Saturate only `qwen3.8-solo`, send a `general` prompt | spend log shows `openai/muse-glimmer` under `local-general`, no Claude row |
| `local-private` never shows Claude spend | Repeat the saturation test with a PII prompt (or call `local-private` directly) | the request waits (queues locally) and is answered by a local model; across **every** test, no spend-log row with model group `local-private` ever names a Claude model |
| `local-private` fails closed | Stop both local vLLMs (`qwen3.8` up on the pair takes `muse-glimmer` down; stop `qwen3.8-solo`), send a PII prompt | an error, not an answer — and still no Claude row |
| Router down → answered locally | `make down semantic-router vllm-sr-envoy`, then call `auto` (Postman item *fallback check*) | answered by `qwen3.8-solo` or `muse-glimmer`; spend log shows model group `local-private` under the **caller's** key, no Claude row |
| Long prompt | Send a prompt between 114688 and 245760 tokens to `auto` | answered by `muse-glimmer` (pre-call check skipped `qwen3.8-solo`); a prompt over 245760 is refused, never sent to Claude |
| Streaming in Open WebUI | Chat on `auto` | UI shows a waiting state (not an error) while the turn runs; the reply renders in full |
| Key scoping | Call `qwen3.8-solo` directly with `SEMANTIC_ROUTER_LITELLM_KEY` | 401 from LiteLLM — the key reaches only the four chains |
| Metrics | `curl localhost:9090` → prometheus targets | the `semantic-router` job is UP against `vllm-sr-router:9190` |
| Exposure | `ss -ltnp` on the box | only `8025` (and `8026` on loopback if enabled); `50051` / `8080` / `9190` / `9901` absent |
| Key rotation | Change `SEMANTIC_ROUTER_LISTENER_KEY`, `make up semantic-router && make up litellm` | old key 401s at Envoy; `auto` through LiteLLM still works |

---

## Phase 0 spike checklist

Everything below was derived from the shipped `vllm-sr` 0.3.0 wheel's source, not from a running router. Run these before trusting the deployment, and fold the answers back into this file.

- [ ] `uv tool install vllm-sr==0.3.0`; confirm `vllm-sr --help` matches the command set assumed here (`serve config validate model eval status logs stop dashboard chat`).
- [ ] Confirm the router **starts** with this config — the CLI validates the pydantic surface, the Go router validates the `global:` block, and only the second one can reject `global.router.auto_model_name`, `global.integrations.looper`, `global.services.*.enabled: false` and `global.stores.semantic_cache.enabled: false`.
- [ ] Confirm `global.router.auto_model_name: vllm-sr/auto` really is the trigger, and that a request naming a chain alias passes through unrouted.
- [ ] Hit `/v1/models` and record exactly what it lists (expected: `vllm-sr/auto` plus the four chains).
- [ ] One request per decision (plain / PII / code / math). Confirm the intended decision matches and capture a full `route_diagnostics` payload.
- [ ] Confirm the forwarded request carries `Authorization: Bearer <SEMANTIC_ROUTER_LITELLM_KEY>` (LiteLLM's spend log attributes it to that key, not to the caller's).
- [ ] `docker inspect` the CLI-started router and Envoy containers and diff the mounts / env / entrypoint against `docker-compose.semantic-router.yml`.
- [ ] List `~/.vllm-sr/models` (or wherever the router put them) and record **what** it downloaded and **how big**.
- [ ] Confirm the router does not need Redis / Postgres / Milvus once `global.services` and `global.stores` are disabled as they are here.

**Exit criteria:** all four decisions return a response on the pinned tag, and the spend log shows each landing on the intended chain.

---

## What is still unverified

Written from the v0.3.0 wheel's source and LiteLLM v1.95.0's source. **No part of this has been run against a live router.** In descending order of how much it would hurt:

1. **The whole `global:` block.** `UserConfig.global_` is `Dict[str, Any]` in the CLI — `vllm-sr validate` does not check inside it, so `global.router.*`, `global.integrations.looper.*`, `global.services.*` and `global.stores.semantic_cache` are validated only by the **Go router at startup**. The key names come from `cli/config_migration.py::_place_global_block`; their accepted *shapes* are inferred. If the router refuses to start, this block is the first suspect.
2. **That `auto_model_name` is really the entrypoint mechanism.** v0.3.0 has no `entrypoints:` block, `UserConfig` is `extra="forbid"`, and this is the only key that looks like "the name that triggers routing".
3. **That the router's `Authorization` rewrite reaches LiteLLM on a `static` decision.** Read from `appendCredentialHeaders`; if LiteLLM instead sees the *listener* key, every routed turn 401s at LiteLLM. Diagnostic: the spend log shows nothing under the router's key.
4. **The overflow hook against a live vLLM.** The parsing (`vllm:num_requests_waiting` summed over label sets) matches vLLM's Prometheus names, and the hook's interaction with LiteLLM's order filter is tested against the real v1.95.0 `Router` in `unit-tests/litellm/test_overflow.py` — but the busy thresholds have not been tuned against real traffic.
5. **`route_diagnostics` field names.** Not in the wheel.
6. **Keyword-signal matching semantics.** `KeywordSignal` carries `operator` / `keywords` / `case_sensitive` and nothing else; whether `OR` means substring, token, or something else is owned by the Go side. This matters most for `privacy_kw`.
7. **What the router downloads into `/app/models`, and how big.** The 600 s healthcheck `start_period` is a guess.
8. **Whether `HF_HOME=/app/models` is the right knob** for the router image.
9. **The Envoy healthcheck's usefulness.** A `/dev/tcp` connect proves the listener is bound; it says nothing about the ext_proc cluster behind it.

### Deliberate departures from the original plan

The plan was written against the upstream article, which describes five looper algorithms and a recipe/entrypoint config surface. v0.3.0 ships neither — and the loopers it does ship are now unused (see [§ Why there are no loopers any more](#why-there-are-no-loopers-any-more)).

| Planned | Reality in v0.3.0 | What was built |
|---|---|---|
| Five algorithms incl. Fusion + Workflows | `cli/validator.py` enumerates `{confidence, ratings, remom}`; `AlgorithmConfig` is `extra="forbid"` | first `ratings` / `remom` / `confidence`, now **`static` only** — routing picks a chain, LiteLLM picks the backend |
| `entrypoints:` + `recipes:` blocks | `UserConfig` is `extra="forbid"` over `{version, listeners, providers, routing, global, setup}` | one entrypoint via `global.router.auto_model_name`; decisions live at `routing.decisions` |
| `vllm-sr/fusion`, `/remom`, `/flow`, `/ratings` slugs and `auto-*` LiteLLM aliases | no multi-entrypoint mechanism exists | only `auto`; a caller who wants a specific chain calls it by name |
| Confidence escalation to Claude | possible (`confidence` + `avg_logprob`) | **removed** — the policy forbids buying Claude on a quality judgement |
| `method: bm25` / `bm25_threshold` on keyword signals | `KeywordSignal` has four fields, none of them these | plain keyword lists |
| Context limits on `providers.models` | `Model` has no context field | `routing.modelCards[].context_window_size` (informational at v0.3.0) |
| Tool-bearing requests → static `qwen3.8` | no verified request-shape signal for `tools` | not implemented; **`auto` is a chat model, not an agent model** — keep MCP flows on a concrete alias |
| `models-init` downloads the Vela bundles | no download subcommand exists in the CLI | the init service validates the config, enforces the Envoy re-render, substitutes the listener key, and seeds the volume; the router image downloads |
| `vllm-sr config validate` | does not exist | `vllm-sr validate --config …` |
| Envoy routes to one shared `litellm_cluster` (`STRICT_DNS`) | the generator emits one cluster per model, `LOGICAL_DNS` | left as generated |
| `models-init` needs egress | it has nothing to download | `network_mode: none` |
| `envoy.yaml` bind-mounted from the repo | the listener key would have to be committed | rendered + substituted into the `vllm_sr_envoy` volume by the init service |
