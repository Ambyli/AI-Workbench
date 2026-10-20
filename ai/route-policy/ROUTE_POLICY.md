# Route policy

The route-policy service decides which LiteLLM **chain alias** an `auto` turn
should use (`local-general`, `local-code`, `local-reasoning` or
`local-private`). It logs every decision, and what really answered it, to its
own Postgres, which Trino reads.

A decision comes from a **ruleset**: a versioned document of **evaluators**
that each vote or abstain, plus a **combiner** that turns the votes into one
route. Two **invariants** have the last word:
- a request for `local-private` stays `local-private`;
- the route is always one of the ruleset's chain aliases.

**Phase 3: the engine routes `auto`.** The `default` ruleset (version 2) is
`mode: enforce`:
- LiteLLM's pre-call hook (`ai/litellm/hooks/routing/policy_client.py`)
  **awaits** `/decide` for every request to `auto`, within
  `ROUTE_POLICY_DECIDE_TIMEOUT_S` (0.25 s);
- when the answer has `apply: true` and the hook's own copy of the invariants
  agrees, it rewrites the request to that chain alias, and LiteLLM serves it
  **in the same pass, streamed**;
- anything else (a timeout, a non-200, bad JSON, `apply: false`, the client
  disabled) sends the turn to **`local-private`**, the chain with no Claude
  deployment, and logs `[route-policy] … -> local-private (<why>)`;
- `/observe` is still fired after the call, never awaited.

Only `auto` is decided. A request that names a chain alias directly is served
as asked. `auto` itself is a LiteLLM `model_group_alias` for `local-private`,
so a request the hook never rewrote is still served privately.

Phase 2 ran the same rules in `shadow` mode beside the semantic router, and
the decisions agreed with what the router plus LiteLLM's code remap chose.
Phase 3 retired both; the pattern files here are now the only copy of the
rules. A `shadow` ruleset still works: it is decided and logged, never
applied.

**Phase 4: a local model reads the conversation, in shadow.** The
`llm_classifier` evaluator votes from a verdict a local model gave the
conversation so far:
- the conversation is classified in the **background**, after `/decide` has
  answered, through the document classifier's existing `POST /assess` (one
  `text` document, one presence criterion per route) on `qwen3.5-4b`, a
  small CPU model; the classifier job is **deleted** as soon as it is read;
- the verdict is stored in the `analyses` table under a hash of the
  conversation prefix, and counts **from the next turn on**;
- it runs in the draft ruleset `default-llm`, bound as a **shadow** beside
  `default`: decided on every `auto` turn after the answer, logged with
  `shadow_of`, never applied. `default@2` still routes every turn, unchanged.

Trino's `shadow_comparisons` and the benchmark ([§ Benchmark](#benchmark))
say whether it is better; promoting it is a separate, later change
([§ Promoting the classifier](#promoting-the-classifier)).

Plan: `ROUTE_POLICY_PLAN.md` (outside the repo), phases 2–6.

| | |
|---|---|
| Compose file | [`docker-compose.route-policy.yml`](docker-compose.route-policy.yml) |
| Services | `route-policy` (FastAPI, swap-class), `route-policy-db` (Postgres 17, recreate-class) |
| Inside ai_shared | `http://route-policy:8000` (what LiteLLM uses), `route-policy-db:5432` |
| Calls | `http://classifier:8000` (`ROUTE_POLICY_CLASSIFIER_URL`): background `/assess` jobs only, never on the `/decide` path |
| LAN | `PORT_ROUTE_POLICY` 8027, published by `ai/edge` (`EDGE_ALLOWED_CIDRS`); `PORT_ROUTE_POLICY_DB` 5441 |
| Through LiteLLM | `{{litellm}}/v1/route-policy/*` (pass-through, `auth: true`) |
| Trino catalog | `postgres_route_policy` (read-only `trino_reader`) |
| Postman | [`route-policy.postman_collection.json`](route-policy.postman_collection.json) |
| Tests | `unit-tests/route-policy/` ([§ Tests](#tests)) |

## Contents

- [How it fits](#how-it-fits)
- [Deploying](#deploying)
- [API](#api)
- [Rulesets](#rulesets)
- [Evaluators](#evaluators)
- [Combiners and roles](#combiners-and-roles)
- [The `default` ruleset](#the-default-ruleset)
- [Shadow rulesets](#shadow-rulesets)
- [The llm_classifier](#the-llm_classifier)
- [The `default-llm` draft](#the-default-llm-draft)
- [Decision log and Trino](#decision-log-and-trino)
- [Benchmark](#benchmark)
- [Promoting the classifier](#promoting-the-classifier)
- [Metrics](#metrics)
- [Configuration](#configuration)
- [Operations](#operations)
- [Code layout](#code-layout)
- [Tests](#tests)

## How it fits

```
Open WebUI ─► LiteLLM (model `auto`, one pass)
                ├─ pre-call hook ── POST /decide {decision_id, messages, tools, ids, backends} ──► route-policy
                │   (awaited, ≤ 0.25 s)  ◄── {route: local-code, apply: true, ...} ────────────── ├─ ruleset (bound to `auto`)
                │   data["model"] = "local-code"   (fallback on any failure: local-private)       ├─ evaluators → votes
                ├─ deployment pick: overflow filter (context fit, busy / down) inside the chain   ├─ combiner → route
                ├─ vLLM / Claude ── STREAMED straight back to the client                          └─ invariants
                └─ success / failure hook ── POST /observe {decision_id, served chain, ...} ──► (fired)  │
                                                                         route-policy-db ◄───────────────┤
                                                                              │ trino_reader             │
                                                                         Trino: postgres_route_policy    │
                                                                                                         │
   after the answer (one background task per /decide) ───────────────────────────────────────────────────┘
     ├─ shadow rulesets (default-llm) decided on the same request ──► decisions row, shadow_of = primary
     └─ llm_classifier analysis requests ──► analysis queue (in memory, deduplicated)
            └─ worker: GET /models (locality) ─ POST /assess ─ poll GET /jobs/{id} ─ read ─ DELETE /jobs/{id}
                   classifier:8000 ──► qwen3.5-4b (llama.cpp, CPU)
                   └─ one `analyses` row per conversation prefix ──► read back by the NEXT /decide
```

The responsibilities are split:
- **The engine decides intent:** which chain, from the conversation.
- **LiteLLM's overflow layer decides capacity:** which deployment inside that
  chain.

The hook sends the overflow layer's backend snapshot in every `/decide`
(`backends`), so a ruleset can be load-aware (`backend_load`). The engine
still never picks a deployment.

**Nothing slow is on the request path.** `/decide` runs the bound
(primary) ruleset and answers. A model's reading of the conversation is
always the verdict of an EARLIER background classification, read in one
bounded query; shadow rulesets and new classifications start only after the
answer, and cannot change, delay or fail it.

**Two copies of the invariants.** The service enforces them
(`route_policy/engine/invariants.py`), and the hook re-checks the answer with
its own copy (`ai/litellm/hooks/routing/invariants.py`, whose allowed routes
are exactly the four chain aliases) before it rewrites anything. So a wrong
answer from this service can never move a turn off `local-private` or onto a
model that is not a chain. `unit-tests/litellm` runs both files over the same
cases, which is why `enforce`, `fail_safe_route` and `PRIVATE_ROUTE` keep
their names.

**The hook makes the `decision_id`** (uuid4) and sends both calls with it.
`/observe` normally follows `/decide`, but a `/decide` the hook gave up on
can still land after the fallback answer's `/observe`, so `outcomes` has no
foreign key to `decisions`. The `decision_outcomes` view joins them.

## Deploying

On a box that already runs phase 3, follow
[§ Upgrading to phase 4](#upgrading-to-phase-4) instead; on one that runs
phase 2 (shadow), [§ Upgrading from phase 2](#upgrading-from-phase-2-shadow)
first.

Routing needs only route-policy and its database. The `llm_classifier` in
the shadow ruleset also needs the document classifier (`ai/classifier`) with
the `qwen3.5-4b` entry in its `models.yaml`, and the `qwen3.5-4b` llama.cpp
service (`ai/llama`); without them every background classification is
stored as failed (`unavailable`, `refused`, `not_local`) and `topic-llm`
abstains -- routing is unaffected.

A fresh box, in order (from the repo root):

1. **`.env`:**
   - Copy the `## Route policy` block (it includes the five
     `ROUTE_POLICY_CLASSIF*` / `ROUTE_POLICY_ANALYSIS_*` lines; their defaults
     are right for this box) and the two `PORT_ROUTE_POLICY*` rows from
     `.env.example`.
   - Set `ROUTE_POLICY_TRINO_READER_PASSWORD` (`openssl rand -hex 32`)
     **before the first start**: the role is created only when the volume is
     empty ([§ Operations](#operations) covers retrofitting).
   - Override `ROUTE_POLICY_DB_PASSWORD` if the database port will be reachable
     past the host.
2. **Check the ports are free:**
   ```bash
   sudo ss -ltnp 'sport = :8027'
   sudo ss -ltnp 'sport = :5441'
   ```
3. **`make up edge` first.**
   - It publishes `8027`. On a box whose edge predates route-policy this is a
     recreate: a second or two of LAN downtime.
   - It must come first. A swap of a fronted service refuses to go ahead when
     the running edge has no backend for it.
4. **`make build route-policy && make up route-policy`.**
   - `route-policy-db` is recreated (it is new); `route-policy` swaps.
   - The image build itself validates the baked-in rulesets.
   - It must be up before LiteLLM routes through it. Until it answers, every
     `auto` turn goes to `local-private` (safe, never to Claude).
5. **`make up trino trino-coordinator`.** A recreate, because its environment
   gained `ROUTE_POLICY_DB_NAME` / `ROUTE_POLICY_TRINO_READER_PASSWORD` and
   the new catalog file.
6. **`make up litellm`.** Picks up the `/v1/route-policy` pass-through in
   `litellm_config.yaml` and the hook that awaits `/decide`.
7. **`docker restart prometheus`.** Its config is bind-mounted, and it gained
   the `route-policy` scrape job.

**Checks after the deploy:**

```bash
make logs route-policy route-policy FOLLOW=0 TAIL=50   # "applied migration 0001_initial.sql" / "0002_llm_classifier.sql",
                                                      # "seeded default@2", "seeded default-llm@1",
                                                      # "binding auto -> default@2", "binding auto shadowed by default-llm@1",
                                                      # "analysis: queue ready (2 worker(s), ...)"
curl -s localhost:8027/health                         # {"status":"ok","rulesets":[{"id":"default","version":2},
                                                      #   {"id":"default-llm","version":1}],"bindings":1}
curl -s localhost:8027/ready                          # {"status":"ready","db":"up"}
curl -s localhost:8027/bindings
curl -s -X POST localhost:8027/evaluate -H 'content-type: application/json' \
  -d '{"ruleset":{"id":"default"},"messages":[{"role":"user","content":"write a python script"}]}' | jq '.route, .apply'
                                                      # "local-code", true
docker exec route-policy-db psql -U route_policy -c '\dt'
docker exec route-policy-db psql -U route_policy -c '\du trino_reader'
```

- **Once LiteLLM is up:** chat once through `auto` in Open WebUI. The answer
  streams, and its footer names the chain and deployment that served it.
  Then `docker exec route-policy-db psql -U route_policy -c 'select
  chosen_route, applied, served_model_group, agree from decision_outcomes
  order by decided_at desc limit 5'`: every turn has one row with both sides,
  `applied = true` and `agree = true`.
- **No fallbacks:** `make logs litellm` has no `[route-policy] … ->
  local-private (…)` lines.
- **In Trino:** `SHOW TABLES FROM postgres_route_policy.public`.
- **In Prometheus:** the `route-policy` target is UP.
- **On the edge status page** (`chat.zeoenergy.com/edge/`): `route-policy` is
  listed.

### Upgrading from phase 2 (shadow)

Staged, so `auto` never breaks. On the box, from the repo root:

1. **`git pull`** on the branch.
2. **`make build route-policy && make up route-policy`.** Seeds `default@2`
   (enforce) and moves the `auto` binding to it. The running phase-2 LiteLLM
   ignores `/decide`'s answer, so nothing changes for users yet. Check
   `curl -s localhost:8027/health` shows version 2. Version 1 stays in the
   database with its decisions (`GET /rulesets/default/1`), although it is no
   longer baked in.
3. **`make up litellm`.** Swaps in the single-pass hook. Check:
   - an `auto` chat streams;
   - its footer names the chain;
   - `decision_outcomes` shows `applied = true` for the new turns;
   - no `[route-policy] … -> local-private (…)` lines in `make logs litellm`.
4. **Tear down the semantic router.**
   ```bash
   docker compose -p ai-semantic-router down --volumes
   ```
   If compose wants the deleted compose file, recover it from git and pass it
   with `-f`. `57c3812` is the last commit before phase 3, so this works
   whatever has landed since:
   ```bash
   git show 57c3812:ai/semantic-router/docker-compose.semantic-router.yml > /tmp/sr.yml
   docker compose -p ai-semantic-router -f /tmp/sr.yml --env-file .env down --volumes
   ```
   Run it from the repo root and before step 6. The old file fails fast on its
   `${SEMANTIC_ROUTER_*:?…}` variables, so it needs the `.env` that still
   holds them.
   Then `docker network rm router_net` if it is left over.
5. **`make up edge`.** The edge compose file loses the router's port, so this
   is a recreate (a second or two of LAN downtime). Then `docker restart
   prometheus`: its scrape config lost the router job.
6. **Manual cleanup:** remove the `SEMANTIC_ROUTER_*` and
   `PORT_SEMANTIC_ROUTER*` lines from the box `.env`, and delete the
   `SEMANTIC_ROUTER_LITELLM_KEY` virtual key in the LiteLLM Admin UI.

**Rollback:** `git revert` the phase-3 commit, then `make up semantic-router`,
`make up litellm`, `make up edge`. The reverted image bakes `default@1` again
and re-points the binding at it; version 2 stays stored.

### Upgrading to phase 4

Routing does not change: `default@2` keeps deciding every `auto` turn. On
the box, from the repo root:

1. **`git pull`**, and add the new `.env` lines from `.env.example`: the
   five `ROUTE_POLICY_CLASSIF*` / `ROUTE_POLICY_ANALYSIS_*` lines (the
   compose file falls back to the same defaults if they are missing), the
   `LLAMA_QWEN35_4B_*` block and the `PORT_LLAMA_QWEN35_4B=8028` row.
2. **`make up llama qwen3.5-4b`** (recreate-class: model load, host port).
   Wait for `/health`, then check that a chat completion with
   `chat_template_kwargs: {enable_thinking: false}` answers with no think
   block, and watch its CPU use with `docker stats`. (`ai/llama/LLAMA.md §
   qwen3.5-4b`.)
3. **`docker restart classifier`** (it picks up `models.yaml`; the edge
   answers 503 for a few seconds). Then:
   - `curl -s localhost:8005/models | jq '.models[] | select(.id=="qwen3.5-4b") | {available, authenticated, endpoint}'`
     shows it available, unauthenticated, on host `qwen3.5-4b`;
   - a hand-made `/assess` with a `text` document and one `llm` criterion on
     `"model": "qwen3.5-4b"` completes with a score -- then
     `curl -X DELETE localhost:8005/jobs/<id>`.
4. **`make build route-policy && make up route-policy`.** Migration 0002
   runs, `default-llm@1` is seeded and bound as `auto`'s shadow, the analysis
   queue starts. Then:
   - `analyses` rows appear tens of seconds after `auto` turns:
     `docker exec route-policy-db psql -U route_policy -c 'select created_at, status, route, model_seconds, queue_ms from analyses order by created_at desc limit 5'`;
   - `shadow_comparisons` fills (one row per `auto` turn);
   - the classifier keeps no finished route jobs: each is deleted as soon as
     it has been read, so `curl -s 'localhost:8005/jobs?limit=50'` shows at
     most the ones still running (≤ `ROUTE_POLICY_ANALYSIS_CONCURRENCY` per
     route-policy copy);
   - `curl -s localhost:8027/metrics | grep -E 'analysis_|shadow_|errors_total'`
     shows no `analysis_write` / `analysis_lookup` / `shadow_*` errors.
5. **Benchmark, off-hours** ([§ Benchmark](#benchmark)). Promotion is a
   later, separate decision.

**Rollback:** `git revert` the phase-4 commit and `make up route-policy`.
Migration 0002 only added tables, a nullable column, indexes and views, so
the phase-3 code runs on it unchanged (its `decision_outcomes` reads now
cover primaries only, which is every row it writes). The phase-3 image
re-seeds its bindings without a shadow slot. Then remove the two
`models.yaml` entries and `docker restart classifier`, and stop
`qwen3.5-4b`.

## API

No auth on the service itself, the same as the classifier:
- **On `ai_shared`:** anything can call it.
- **On the LAN:** the edge's `EDGE_ALLOWED_CIDRS` is the gate.
- **Through LiteLLM:** a key needs `/v1/route-policy` in
  `metadata.allowed_passthrough_routes`, or the master key.

| Method | Path | What |
|---|---|---|
| POST | `/decide` | Decide (and log) the chain for one request |
| POST | `/observe` | Record what answered it |
| POST | `/evaluate` | Dry run of a stored or draft ruleset; writes nothing |
| GET | `/rulesets` | Every stored version, without documents |
| GET | `/rulesets/{id}` | The highest version with its document, plus every version's summary |
| GET | `/rulesets/{id}/{version}` | One version with its document |
| GET | `/bindings` | Requested model (+ key alias) → ruleset version, with its shadow slots |
| GET | `/chats/{chat_id}/decisions` | One conversation's primary decisions with their outcomes and shadows |
| GET | `/evaluator-types` | Each evaluator type's config schema and declared output; the combiners |
| GET | `/health`, `/ready` | Liveness (no database); readiness (a live `SELECT 1`) |
| GET | `/metrics` | Prometheus |

The ruleset **write** API (create a version, activate it, edit bindings) is
phase 6.

### POST /decide

This is the contract LiteLLM's client (`ai/litellm/hooks/routing/`) is built
against (ROUTE_POLICY_PLAN § 7, "Phase 2 build spec"; unchanged in phase 3,
which only started awaiting the answer).

```json
{"decision_id": "uuid4, made by the hook", "requested_model": "auto",
 "litellm_call_id": "…", "chat_id": "… | null", "message_id": "… | null", "user_id": "… | null", "key_alias": "… | null",
 "messages": [/* the OpenAI messages as LiteLLM received them */], "tools": [/* or null */],
 "backends": {"<api_base>": {"verdict": "ok|busy|down|stale|unknown", "waiting": 0.0, "running": 0.0}}}
```

→ `200`, and the same body is what gets logged:

```json
{"decision_id": "…", "route": "local-code", "mode": "enforce", "apply": true,
 "ruleset": {"id": "default", "version": 2},
 "votes": [{"evaluator": "code-tools", "type": "tool_calls", "route": "local-code", "weight": 1.0,
            "confidence": 1.0, "role": "vote", "abstained": false,
            "reason": "called sandbox-mcp_exec in this exchange", "latency_ms": 0.1}],
 "scores": {"local-code": 1.0}, "latency_ms": 2.3}
```

- **`votes`:** one per evaluator, in ruleset order, abstains included. An
  abstain has `route` and `confidence` null and a reason.
- **`scores`:** Σ weight × confidence per route, over the non-abstaining
  `vote` and `force` votes.
- **`apply`:** true only for an `enforce` ruleset whose invariants held, so
  true for `default@2` unless a combined route fell outside the ruleset's
  `routes`. LiteLLM applies `route` only when it is true; `false` (a `shadow`
  ruleset, or an invariant override) sends the turn to `local-private`.
- **Idempotent on `decision_id`.** A repeat returns the **first** stored
  response, whatever the new body says. Nothing is written twice.
- **Errors:**
  - `404`: no ruleset is bound to `requested_model` (+ `key_alias`).
  - `422`: the body breaks the contract (e.g. `decision_id` is not a UUID).
  - `503`: the bound ruleset cannot be compiled by this copy (only possible
    mid-swap after a ruleset gained an evaluator type the old code lacks).
- **Lenient where strictness would only lose data.** A `422` on `/decide`
  only sends the turn to LiteLLM's fallback (`local-private`), and one on the
  fire-and-forget `/observe` is never seen, so:
  - unknown fields are ignored;
  - a null `messages` is an empty list;
  - an unknown `verdict` is kept as sent.
- **A decision is returned even when route-policy-db is down.** It is then
  not logged; that is counted in `route_policy_errors_total{kind="decision_write"}`.
  Routing never depends on the log.
- **And it is returned in time, whatever the database does.** LiteLLM waits
  0.25 s for `/decide` and sends the turn to `local-private` past it, so no
  database wait may hold the answer for long (`route_policy/service.py`,
  `rulesets/store.py`):
  - **The decision write** runs as its own task. `/decide` waits for it up
    to `ROUTE_POLICY_DECISION_WRITE_BUDGET_S` (0.05 s). In time, it behaves
    as if the write were synchronous: a duplicate `decision_id` replays the
    first stored response. Late (a hung database, an exhausted pool), the
    computed response goes out at once and the write finishes in the
    background (`route_policy_writes_deferred_total{write="decision"}`); if
    it then fails, it is counted and logged like any `decision_write` error.
    A duplicate whose write is late gets the newly computed answer; the
    stored row still holds the first.
  - **At most 256 writes** are in flight (`WRITES_MAX_IN_FLIGHT`,
    `route_policy_writes_in_flight`). Past that a write is dropped, never
    queued (`route_policy_errors_total{kind="decision_write_dropped"}`): the
    decision is still returned, just not logged.
  - **The reads on the path** — the binding refresh every
    `ROUTE_POLICY_BINDING_REFRESH_S`, and compiling a stored version this
    copy has not cached — get `DB_READ_BUDGET_S` (0.1 s, a constant in
    `config.py`). A slow refresh finishes in the background while requests
    decide from the last snapshot; a slow version fetch answers `503` (so
    LiteLLM falls back to `local-private`) and lands in the cache for the
    next request. Both count in `route_policy_db_reads_cut_total`.
  - **Shutdown** waits up to 5 s (`WRITES_DRAIN_S`) for background writes,
    then cancels the rest.
- **Latency.** The engine takes well under a millisecond (p50 0.28 ms in
  shadow); the HTTP hop and, normally, a few milliseconds of decision write
  are the rest. Every `auto` turn waits for it.
- **Verdict lookups.** When the primary ruleset has an `llm_classifier`
  (`default@2` has none), `/decide` reads the verdicts it names in ONE query
  before the pipeline runs, within `DB_READ_BUDGET_S` (0.1 s); past it the
  evaluator abstains (`lookup timed out`) and the decision goes on.
  `route_policy_db_reads_cut_total{read="analysis_lookup"}` counts it.
- **After the answer, in one background task** (counted with the writes
  against `WRITES_MAX_IN_FLIGHT`; dropped past it, `shadow_dropped`):
  1. the primary's `llm_classifier` analysis requests go to the analysis
     queue;
  2. each **shadow** ruleset of the binding runs on the same request (its
     own verdict lookup, with the same budget, so it measures what
     enforcing it would do) and is stored as a `decisions` row with
     `decision_id` = `uuid5(<this decision_id>, "<ruleset>@<version>")` and
     `shadow_of` = this decision_id, with its own `evaluations`; then its
     analysis requests go to the queue.

  The response is the primary's alone and is never changed by any of it. A
  repeated `/decide` derives the same shadow ids (nothing is written twice);
  the queue deduplicates by key. See [§ Shadow rulesets](#shadow-rulesets).

### POST /observe

```json
{"decision_id": "…", "litellm_call_id": "…", "response_id": "chatcmpl-… | null",
 "served_model_group": "local-code | null", "served_deployment": "openai/muse-glimmer | null",
 "overflow_reason": "busy|down|null", "finish_reason": "stop|tool_calls|length|null",
 "tool_calls": ["sandbox-mcp_exec"], "status": "success|failure", "error": "… | null"}
```

- **`202`:** `{"decision_id": "…", "stored": true}`, or `stored: false` when
  this decision already had an outcome (the first write wins), or
  `{"decision_id": "…", "stored": null, "deferred": true}` when the write
  outlasted `ROUTE_POLICY_DECISION_WRITE_BUDGET_S` and goes on in the
  background (the hook ignores the body).
- **`503`:** the outcome could not be stored: no database, the write failed
  within the budget, or 256 writes were already in flight
  (`outcome_write_dropped`).
- **`error`** is cut to 2000 characters.

### GET /bindings and GET /chats/{chat_id}/decisions

- **`/bindings`:** each binding carries `shadows: [{ruleset_id, version}, ...]`,
  in order (`[]` when it has none).
- **`/chats/{chat_id}/decisions`:** the latest `limit` (default 100, at most
  1000) **primary** decisions, oldest first, each with its votes, its
  outcome (null until `/observe`; `agree` = chosen route == served chain)
  and `shadows`: the decisions the binding's shadow rulesets made beside it
  -- the same keys except `shadows`, with `shadow_of` = the primary's id and
  `outcome` always null (the turn's outcome is the primary's). A primary's
  `shadow_of` is null.

### POST /evaluate

Give exactly one of:
- `{"ruleset": {"id": "default", "version": 2}}` (version omitted = the
  highest). A stored version that is no longer baked in (`default@1`) works
  too: it is compiled from the database;
- `{"document": {…}}`.

Plus `messages` / `tools` / `backends` and an optional `requested_model`
(default `auto`).

It returns the `/decide` shape with `decision_id: null`. Each vote also carries
its evaluator's `detail` and `error`.

- **An `llm_classifier`** reads the verdicts STORED for the conversation
  (one read-only query, waited for as long as it takes); `/evaluate` never
  enqueues a classification, so a conversation `/decide` never saw has none
  and the evaluator abstains (`no verdict in the last N message(s)`).
- **A draft document** may use `patterns_ref` refs. They resolve inside the
  baked `rulesets/` directory; a path that leaves it is a `422`.
- **A stored document** from `GET /rulesets/{id}/{version}` posts back as it
  is.
- **An invalid document** is a `422` whose `detail.errors` lists **every**
  problem.

## Rulesets

```yaml
id: default                      # [a-z0-9][a-z0-9_-]*
version: 2                       # immutable: new content = a new version
description: …
mode: enforce                    # shadow (decide + log) | enforce (LiteLLM applies it)
routes: [local-general, local-code, local-reasoning, local-private]
default_route: local-general     # one of routes
evaluators:                      # in order; each votes or abstains
  - id: privacy
    type: regex_text             # GET /evaluator-types
    role: force                  # vote (default) | force | veto
    config: {patterns_ref: patterns/privacy.yaml}
    vote: {route: local-private, weight: 1.0}
combiner: {type: priority}       # priority | {type: weighted, threshold: 0.7}
```

Validation (`route_policy/rulesets/validate.py`):
- **The whole document** is checked, and all problems are reported at once.
- **Unknown keys are errors, never ignored.** That holds in every evaluator's
  `config` too.
- **Every `vote.route` must be one of `routes`**, so a ruleset can only ever
  vote for its own chain aliases.
- **Cross-references must resolve** (`cooldown.relevant_if`).

**Files** (`ai/route-policy/rulesets/`, baked into the image):
- `*.yaml` at the top level: one ruleset each (`default.yaml`,
  `default-llm.yaml`).
- `bindings.yaml`: the seed bindings.
- `patterns/*.yaml`: keyword rules, referenced by `patterns_ref` -- by
  `default` AND `default-llm`, so a pattern edit is new content for both:
  bump both versions.

**Bindings** (`bindings.yaml`):

```yaml
- model: auto                # the requested model
  key_alias: null            # optional: only requests from this LiteLLM key alias
  ruleset: default           # must be mode: enforce
  version: null              # omitted = the highest version baked in
  shadow: [default-llm]      # optional: each an id (highest version) or {ruleset: <id>, version: <n>};
                             # each must be mode: shadow
```

Two seed rules, checked when the files are loaded (so at image build time
as well as startup): a binding's **primary** ruleset must be `mode: enforce`
(a shadow primary would answer `apply: false` and send every turn to
`local-private`), and every **shadow** slot must be `mode: shadow`.

**Seeding at startup** (`route_policy/rulesets/store.py`). It runs in one
transaction under an advisory lock, so two copies starting together during a
swap cannot race.

1. **Each version** is inserted unless it already exists.
   - An existing version must have the **same content hash**. If not, the
     service **refuses to start**: `SeedConflict … give the edited ruleset a
     new version`.
   - The hash is SHA-256 over the resolved document: refs inlined, YAML
     comments ignored.
   - So editing a description, or a pattern in a referenced file, means
     bumping `version`.
2. **Each seed binding** is pointed at its version, and its shadow slots
   (`binding_shadows`) are replaced by the file's. `version` omitted means
   the highest version baked in, so bumping `default.yaml`'s version
   re-points `auto` on the next deploy.
3. **`active_from` / `active_to`** are refreshed:
   - `active_from` is the first time a binding or a shadow slot pointed at a
     version;
   - `active_to` is when none does any more.

Startup owns the seeded bindings and resets them on every start.

**A stored version needs no file.** Only the current `default` (version 2)
and `default-llm` (version 1) are baked in; version 1 (phase 2, shadow) stays in the database with the
decisions made under it, and the store compiles it from there on first use
(`GET /rulesets/default/1`, `POST /evaluate`). Keeping old files would also
freeze the pattern files: a version that references `patterns/code.yaml`
hashes its content, so an edited pattern would stop startup on the old
version.

**Changing a ruleset:**
1. Edit the YAML in place (`default.yaml` holds only the current version).
2. Bump `version`.
3. Update the expected values in `unit-tests/route-policy/test_default_ruleset.py`
   (and the RE2 check passes for a new pattern).
4. Try it with `POST /evaluate {"document": …}` on real conversation shapes,
   or ship it first as a `shadow` ruleset bound to a test key alias.
5. `make build route-policy && make up route-policy`. The new copy seeds the
   version and moves the binding; the old copy keeps deciding with the old
   version until its next binding refresh (`ROUTE_POLICY_BINDING_REFRESH_S`,
   10 s), then with the new one, until it retires.

The old version stays in the database, so its decisions remain readable.

**At runtime:**
- Compiled versions are cached for the life of the process; they are
  immutable.
- The binding snapshot is re-read when older than
  `ROUTE_POLICY_BINDING_REFRESH_S`.
- A copy that cannot reach the database keeps deciding from its last
  snapshot.

## Evaluators

Each type is a plugin (`route_policy/evaluators/`) with:
- a pydantic `config` model (`extra="forbid"`);
- a **declared output**: when it votes, what one exchange's "relevance" means,
  and every `detail` key with its kind. It is declared next to the code
  (`@declares`) and served by `GET /evaluator-types`.

`unit-tests/route-policy/test_declarations.py` runs every real evaluator and
fails on an undeclared or missing key: the classifier's `@result_spec`
standard. **No `detail` ever holds message text.** Decisions are federated
into Trino; `regex_text` reports pattern *indices*, `tool_calls` tool *names*.

| Type | Config | Votes when |
|---|---|---|
| `regex_text` | `rule` (or `patterns_ref`): a keyword rule `{name, operator: OR\|AND\|NOR, case_sensitive, method: regex?, keywords}`. `scope: current_message` | The rule matches the **latest user message**. Uses `common.keywordmatch` (Go-RE2 semantics: ASCII `\b` / `\w`, `(?i)` unless case-sensitive; no `method` = whole-word literals). |
| `tool_calls` | `pattern` (Python regex, **full-name** match), `scope: current_exchange \| conversation` | The conversation **called** a matching tool: OpenAI `tool_calls`, legacy `function_call`, Anthropic `tool_use`, named tool results (`common.conversation.called_tool_names`). Offered `tools` never count. |
| `cooldown` | `exchanges` (≥ 0), `relevant_if: [evaluator ids]` | The current exchange is relevant, or an earlier one is and fewer than `exchanges` irrelevant exchanges followed it. The judges are asked in order; the first reason wins. Recomputed from the history on every request, no per-chat state. |
| `backend_load` | `backends` (regexes over api_base), `verdicts` (default busy, down), `min_waiting`, `require: any \| all` | Watched backends in the snapshot qualify. Abstains when none matches. |
| `static` | `abstain`, `confidence`, `reason` | Always (or never). For tests, overrides, and as a `veto`. |
| `llm_classifier` | `model`, `criteria` (route → question), `otherwise`, `min_score`, `history`, `max_age_exchanges` | A stored verdict (a local model's classification of an earlier prefix of this conversation, at most `max_age_exchanges` user messages old) names a route. **Chooses its own route**: `vote.route` is refused. [§ The llm_classifier](#the-llm_classifier). |

- **Exchanges** come from `common.conversation.split_exchanges`: one per user
  message, holding the message and every round after it.
- **`regex_text` and `tool_calls` can judge a single exchange**, which is what
  `cooldown.relevant_if` uses.
- **`llm_classifier`** reads only what the service put on the request
  (`ctx.analyses`); the slow part happens in the background
  ([§ The llm_classifier](#the-llm_classifier)). `context_size` is in the
  plan but not scheduled.

## Combiners and roles

Every combiner runs the same frame (`route_policy/combiners/base.py`):

1. **force:** the first non-abstaining `role: force` vote wins outright and is
   never vetoed. This is how `privacy` beats every other signal.
2. **veto:** each non-abstaining `role: veto` vote takes its route out of
   play.
3. **choose:**
   - **`priority`:** the first non-abstaining `vote` in ruleset order.
   - **`weighted`:** the route with the best Σ weight × confidence, if it is
     > 0 and ≥ `threshold` (default 0.5). A tie goes to the earlier route in
     `routes`.
4. **default:** `default_route`; if that is vetoed, the first route in play.

**Every evaluator always runs**, even after a force vote, and every vote is
logged: a rule's hit rate is readable in Trino whatever decided the turn.

An evaluator that **raises** is recorded as an abstain with its error, and the
decision goes on. A vote for a route the ruleset does not list is turned into
an abstain with an error.

**The invariants** (`route_policy/engine/invariants.py`) run last:
- `requested_model == local-private` → `local-private`.
- A combined route outside `routes` → `local-private` (or `default_route` if
  the ruleset lacks it), with `apply: false`.

## The `default` ruleset

`rulesets/default.yaml`, version 2, `mode: enforce`, `priority` combiner:

| # | Evaluator | Type | Votes when | Route |
|---|---|---|---|---|
| 1 | `privacy` (`force`) | `regex_text` → `patterns/privacy.yaml` (`privacy_kw`) | PII words in the latest user message | `local-private` |
| 2 | `code-text` | `regex_text` → `patterns/code.yaml` (`code_kw`, regexes) | code words, a file name or a code fence | `local-code` |
| 3 | `reasoning-text` | `regex_text` → `patterns/reasoning.yaml` (`reasoning_kw`) | math / science / multi-step words | `local-reasoning` |
| 4 | `code-tools` | `tool_calls`, current exchange | a sandbox MCP tool (`sandbox-mcp_.+`) or one of Open Terminal's distinctive operationIds was CALLED | `local-code` |
| 5 | `sticky-code` | `cooldown`, 3 exchanges, judges `code-tools`, `code-text` | a coding exchange within the last 3 | `local-code` |
| — | default | | | `local-general` |

- **Privacy forces.** A PII word wins over every other vote, and
  `local-private` has no Claude deployment. The match is lexical: a PII
  prompt that names none of the words lands on another chain and can
  overflow to Claude. Widen `patterns/privacy.yaml` rather than trusting it.
- **Rows 4–5 come after `reasoning-text`**, so a turn that asks for a proof
  stays on `local-reasoning` in a chat that has been coding; only an
  otherwise `local-general` turn is held on `local-code`.
- **Offered tools never count**, only called ones: a chat with the sandbox
  attached but unused is not a coding chat.
- **The cooldown is stateless.** Build a calculator, then ask three weather
  questions: they stay on `local-code`, and the fourth drops to
  `local-general`. Any coding exchange resets it.

**The pattern files are the only copy of the rules.** Phase 2 kept them equal
to the semantic router's keyword signals; phase 3 retired the router and its
config, so nothing else holds them. `test_default_ruleset.py` checks that:
- every `patterns/*.yaml` is a valid rule, and every `method: regex` keyword
  stays in the RE2 subset (no lookaround, backreferences, atomic or
  possessive groups, `\Z`). `common.keywordmatch` gives Go RE2's semantics
  (ASCII `\b` / `\w`), but Python's `re` accepts more, so this check is what
  keeps the patterns portable;
- the real "Scientific Calculator" chat prompts match `code_kw` (or don't);
- the wiring and the routes on ~20 conversation shapes are as listed above.

Changing any of it is a ruleset change: update the expected values and bump
`version` ([§ Rulesets](#rulesets)).

## Shadow rulesets

A **shadow** ruleset is bound beside a primary (`bindings.yaml`, `shadow:`):
decided on every request the binding covers, logged, never applied. It is
how a draft is measured on real traffic before anyone promotes it.

- **When:** after `/decide` has answered, in the same background task as the
  primary's analysis requests ([§ API](#post-decide)). The primary's
  response is never touched; a shadow that cannot be loaded (not stored, not
  compilable by this copy, not handed over within `DB_READ_BUDGET_S`) is
  skipped and counted (`shadow_load`), one that raises is counted
  (`shadow_error`), and neither affects the primary or the other shadows.
- **Stored as:** a `decisions` row with `decision_id = uuid5(<primary id>,
  "<ruleset>@<version>")`, `shadow_of` = the primary's id, `mode = shadow`,
  `applied = false`, the same chat / message / user ids, and its own
  `evaluations`. A repeated `/decide` derives the same ids, and the first
  write wins. No foreign key to the primary: its own write may still be in
  flight, like `outcomes`.
- **Read as:** `GET /chats/{id}/decisions` nests them under their primary;
  Trino's `shadow_comparisons` view puts each beside its primary and the
  turn's outcome ([§ Decision log and Trino](#decision-log-and-trino));
  `route_policy_shadow_decisions_total{ruleset, agree}` counts agreement
  with the primary live.
- **Counted like decisions:** `route_policy_decisions_total` (with
  `mode="shadow"`), `route_policy_votes_total` and
  `route_policy_evaluator_seconds` include them, so a shadow evaluator's hit
  rate is on the dashboard too.
- **Cost:** CPU for one more pipeline run per request, one more decision
  write, and whatever its evaluators enqueue -- off the request path.

## The llm_classifier

`route_policy/evaluators/llm_classifier.py`, with its background half in
`route_policy/analysis/`.

```yaml
- id: topic-llm
  type: llm_classifier
  config:
    model: qwen3.5-4b           # a document-classifier registry id; must be LOCAL (below)
    criteria:                   # route -> the question /assess scores 1-10 (hint: presence), <= 200 chars each
      local-code: "The latest user request in this conversation is about writing, running, debugging or changing code or software."
      local-reasoning: "The latest user request needs careful multi-step reasoning such as maths, logic, planning or analysis."
      local-private: "The conversation contains personal data about a real person, such as a name with an address, phone number, email or account number."
    otherwise: local-general    # no criterion reaches min_score -> this route; null = abstain
    min_score: 7                # the classifier's PASS line
    history: {exchanges: 4, max_chars: 6000}   # max_chars <= the model's text_char_budget (12000 for qwen3.5-4b)
    max_age_exchanges: 2        # a verdict may be this many user messages old
  vote: {weight: 1.0}           # no route: the verdict picks it
```

- **Validation:** criteria keys and `otherwise` must be routes of the
  ruleset; texts are non-empty, unique and at most 200 characters (the
  classifier's criterion-name cap); `vote.route` is refused; unknown keys
  are errors.
- **The verdict** (`analysis/verdict.py::choose`): the highest score at or
  above `min_score` wins, confidence `score / 10`, a tie going to the route
  listed first in the ruleset's `routes`; none reaches it → `otherwise`
  with confidence `(10 − best) / 9`, or no route (abstain) when `otherwise`
  is null; every criterion errored → no route.
- **The tie order is the ruleset's `routes`**, never the order of the
  `criteria` mapping: a stored ruleset is jsonb, which keeps object keys in
  its own order. For the same reason the fingerprint sorts the criteria.

### The transcript and the verdict key

What the model reads is `common.conversation.render_transcript(messages,
exchanges=4, max_chars=6000, upto_user=k)`: the conversation up to and
including the k-th user message -- its last 4 exchanges as `User:` /
`Assistant:` lines, a `[tool: <name>]` line per tool call (results are not
rendered), non-text parts dropped, cut to 6000 characters (oldest exchange
first; the latest user message is always kept). The benchmark renders its
decision points with the same function.

A verdict is stored under `input_key = sha256(fingerprint, transcript)`,
where `fingerprint = sha256(model, criteria, otherwise, min_score,
history)`. So:
- **tool rounds of one user message** render the same prefix: one
  classification, however many `/decide` calls the message makes;
- **an edited or regenerated earlier message** changes every later prefix:
  an old verdict can never be read for new text;
- **no chat id is needed**: Claude Code on `/v1/messages` works the same
  way;
- **a changed configuration** (a new ruleset version with other criteria or
  another model) starts from scratch; `max_age_exchanges` is not in the
  fingerprint (it decides which verdicts are read, not what one says).

### When it votes

At `/decide` for user message n, the evaluator names the keys of the
prefixes ending at user messages n, n−1, … n − `max_age_exchanges`; the
service reads them in one query (within `DB_READ_BUDGET_S` on `/decide`) and
the evaluator votes the **newest usable** one (`ok` / `incomplete`, with a
route). The prefix ending at n is classified only after this answer, so the
vote normally comes from the previous message's verdict: **it applies from
the next turn on**. The reasons, which Trino can group on:

| Reason | Meaning |
|---|---|
| `local-code scored 9/10 (qwen3.5-4b, 1 message(s) ago)` | voted a criterion's route |
| `no criterion reached 7/10, otherwise local-general (…)` | voted `otherwise` |
| `verdict 1 message(s) ago: no criterion reached 7/10` | the newest usable verdict has no route (no `otherwise`): abstains |
| `verdict failed: timeout` | the verdicts found all failed (any status but ok / incomplete) |
| `no verdict in the last 3 message(s)` | nothing classified yet (a new chat's first turns), or older than `max_age_exchanges` |
| `lookup timed out` / `lookup failed` / `lookup unavailable (no database)` | the read did not happen |

`detail`: `model`, `user_messages` (n), `looked_up`, `lookup_error`, and for
the verdict reported -- `input_key`, `exchange_index` (its k), `age_exchanges`
(n − k), `status`, `scores` (route → 1–10).

### The /assess cycle

`route_policy/analysis/classifier_client.py`, one cycle per classification;
a remote failure is a status, never an exception:

1. **Locality guard.** `GET /models` on the classifier (cached 60 s) must
   list the model with `authenticated: false` and `available: true`, and
   every address its `endpoint.host` resolves to -- resolved from inside
   route-policy, which sits on `ai_shared` like the classifier -- must be
   private, loopback or link-local (`common.net.DEFAULT_BLOCKED_NETWORKS`).
   The classifier reads conversations, so only a model on this box's own
   networks may answer. A key-authenticated entry, a host resolving to any
   public address, or one that does not resolve at all is **`not_local`**
   and nothing is submitted. Docker DNS answers only for RUNNING
   containers, so a stopped `qwen3.5-4b` shows up as `not_local` too. The
   host's verdict is cached per host for 60 s. An unknown or unavailable
   model is `refused`.
2. **`POST /assess`** (JSON): `documents: [{type: text, data: <transcript>}]`,
   one `{name: <question>, type: llm, options: {hint: presence, ocr: never}}`
   per criterion in tie order, `model`. N criteria cost N model calls, run
   concurrently up to the model's `max_concurrent` in `models.yaml`.
3. **Poll** `GET /jobs/{id}` every `ROUTE_POLICY_CLASSIFY_POLL_S` until
   `completed` / `failed`, or `ROUTE_POLICY_CLASSIFY_TIMEOUT_S` (180 s,
   counted from the start of the cycle) has passed.
4. **Read** `assessment.per_criterion_scores` (each `score`, and `reason`
   cut to 200 characters) and `result.usage`.
5. **Always `DELETE /jobs/{id}`** -- after a result, a failure, a timeout
   (preceded by `POST /jobs/{id}/cancel`, which the classifier does not
   mount today; the DELETE is what counts) and a cancellation at shutdown.

**Why the job is deleted.** `/assess` keeps the text as a payload until the
job finishes and as a text-layer artifact until the classifier's
`JOB_TTL_HOURS`. The DELETE removes the row, the artifacts (text layers
included), the progress history and the job's `llm_calls` rows -- so the
usage (calls, tokens, seconds) is copied into route-policy's `analyses` row
first. A job deleted while it still runs may write its artifact directory
afterwards; the classifier's sweeper removes a directory whose job row is
gone (every `CLASSIFIER_ARTIFACT_SWEEP_INTERVAL_S`). A DELETE that fails is
logged and counted (`classifier_delete`); that job then expires with
`JOB_TTL_HOURS`.

Statuses: `ok` (every criterion scored), `incomplete` (some did; the partial
scores are used), `failed` (the job failed, its result is unreadable, or no
criterion was scored), `refused` (a 4xx from `/assess`, an unknown or
unavailable model), `unavailable` (no connection, a 5xx), `timeout`,
`not_local`. An `error` (≤ 300 characters) that would quote the submitted
text is withheld.

### The analysis queue

`route_policy/analysis/queue.py`, started and stopped by the app's
lifespan:
- **In memory, bounded, deduplicated.** At most
  `ROUTE_POLICY_ANALYSIS_QUEUE_MAX` (64) requests wait; past it a request is
  dropped and counted (`route_policy_analysis_dropped_total{reason="queue_full"}`).
  A key already queued or running is not queued again. There is no durable
  queue on purpose: a request carries the transcript, which route-policy
  never writes anywhere (`common.jobs`' Postgres queue would put it in a
  payload). A restart loses what was waiting; a lost analysis costs one
  turn's verdict.
- **`ROUTE_POLICY_ANALYSIS_CONCURRENCY` (2) workers**, each running one
  cycle at a time -- so at most that many route jobs are open on the
  classifier; its per-model `max_concurrent` bounds the model calls.
- **Each worker** skips a key that already has a verdict (another copy
  during a swap, a repeated `/decide`), runs the cycle, turns the scores
  into a route and writes ONE `analyses` row (`ON CONFLICT DO NOTHING`).
  Failures are stored too, with their status, so their rate can be read;
  that prefix is not retried (the next user message is a new prefix).
- **Shutdown:** stop accepting, drop what waits (`reason="shutdown"`), give
  running cycles `WRITES_DRAIN_S` (5 s), then cancel them -- each still
  sends its DELETE (5 s cap per request) before it ends.
- **No conversation text is stored or logged.** The transcript lives in
  route-policy's memory (and in the classifier's job for its length);
  `[analysis]` log lines carry the key prefix, model, scores, route and
  timings.

### Known limitations

- **Route jobs share the classifier's FIFO with document jobs.** A document
  backlog delays verdicts: it shows up as lower coverage (more `no verdict`
  abstains in `evaluations`) and higher `queue_ms` in `analyses`
  ([§ Decision log and Trino](#decision-log-and-trino) has the queries).
  Past `ROUTE_POLICY_CLASSIFY_TIMEOUT_S` a job is cancelled and deleted
  (`timeout`).
- **Claude Code traffic ages verdicts per tool round.** On `/v1/messages`, a
  user-role message carrying only `tool_result` blocks counts as a user
  message (as in `split_exchanges`), so every tool round is a new "user
  message n": each round enqueues a new prefix, and a verdict is
  `max_age_exchanges` tool rounds old soon. The queue's in-flight dedupe and
  `max_age_exchanges` bound the cost. Open WebUI traffic (`tool` role
  results) is unaffected.
- **The criteria are lexical questions to a 4B model.** Its PII criterion
  is a second opinion only: `privacy` (`force`, regex) still runs first and
  the invariants still guard `local-private`.

## The `default-llm` draft

`rulesets/default-llm.yaml`, version 1, `mode: shadow`, bound as `auto`'s
shadow, `priority` combiner:

| # | Evaluator | Type | Route |
|---|---|---|---|
| 1 | `privacy` (`force`) | `regex_text` → `patterns/privacy.yaml` | `local-private` |
| 2 | `code-text` | `regex_text` → `patterns/code.yaml` | `local-code` |
| 3 | `reasoning-text` | `regex_text` → `patterns/reasoning.yaml` | `local-reasoning` |
| 4 | `code-tools` | `tool_calls`, current exchange | `local-code` |
| 5 | `topic-llm` | `llm_classifier` on `qwen3.5-4b` (config above) | the verdict's |
| — | default | | `local-general` |

Rows 1–4 are `default`'s, config for config and in the same order, so the
two rulesets differ only in the last rule: `default`'s 3-exchange
`sticky-code` cooldown is replaced by `topic-llm`. What it tests: does the
classifier replace the cooldown, and catch what the keyword rules miss?
`topic-llm` decides only a turn no keyword or tool rule claimed.
`unit-tests/route-policy/test_llm_classifier.py` pins this wiring.

## Decision log and Trino

`route-policy-db`, schema from `route_policy/db/migrations/0001_initial.sql`
and `0002_llm_classifier.sql`:

| Table | One row per | Key columns |
|---|---|---|
| `rulesets` | ruleset version | `id`, `version` (PK), `document` (jsonb, resolved), `content_sha256`, `source`, `created_at`, `created_by`, `active_from`, `active_to` |
| `bindings` | scope → version | `id`, `scope_model`, `scope_key_alias` (null = every key; unique with NULLS NOT DISTINCT), `ruleset_id`, `version` (FK), `updated_at`, `updated_by` |
| `binding_shadows` | shadow slot | `binding_id` (FK, cascade), `position`, `ruleset_id`, `version` (FK) |
| `decisions` | `/decide`, and each shadow ruleset beside it | `decision_id` (uuid PK), `created_at`, `chat_id`, `message_id`, `litellm_call_id`, `user_id`, `key_alias`, `requested_model`, `chosen_route`, `applied`, `mode`, `ruleset_id`, `ruleset_version` (FK), `reason`, `scores`, `backend_snapshot`, `message_count`, `tool_count`, `latency_ms`, `error`, `response`, `shadow_of` (the primary's id; null for a primary; **no FK**) |
| `evaluations` | evaluator × decision | `decision_id` (FK, cascade), `position`, `evaluator_id`, `type`, `role`, `vote_route`, `weight`, `confidence`, `reason`, `abstained`, `latency_ms`, `error`, `detail` |
| `outcomes` | `/observe` | `decision_id` (uuid PK, **no FK**), `created_at`, `litellm_call_id`, `response_id`, `served_model_group`, `served_deployment`, `overflow_reason`, `finish_reason`, `tool_calls` (jsonb), `status`, `error` |
| `analyses` | classified conversation prefix | `input_key` (PK: sha256 of fingerprint + transcript), `created_at`, `fingerprint`, `model_id`; who asked first: `ruleset_id`, `ruleset_version`, `evaluator_id`, `chat_id`, `message_id`, `decision_id`; `exchange_index` (k), `input_chars`; the verdict: `route`, `confidence`, `scores` (jsonb route → 1–10), `reason` (the best-scoring criterion's, ≤ 200, CHECKed), `status`, `error`; the cost: `classifier_job_id`, `calls`, `prompt_tokens`, `completion_tokens`, `model_seconds`, `queue_ms`, `latency_ms`. **No transcript** |
| `decision_outcomes` | view | every PRIMARY decision FULL JOIN its outcome, plus `agree` = `chosen_route = served_model_group` (0001's columns; 0002 recreated it over `shadow_of IS NULL`) |
| `shadow_comparisons` | view | every shadow decision beside its primary and the turn's outcome: `shadow_decision_id`, `primary_decision_id`, both `*_decided_at`, `observed_at`, `chat_id`, `message_id`, `user_id`, `requested_model`, `shadow_ruleset_id` / `_version`, `primary_ruleset_id` / `_version`, `shadow_route`, `primary_route`, `primary_applied`, `served_model_group`, `served_deployment`, `shadow_reason`, `primary_reason`, `shadow_error`, `agree_with_primary`, `agree_with_served` |
| `schema_migrations` | applied migration | `version`, `name`, `sha256`, `applied_at` |

- **Indexes:**
  - `decisions(chat_id, created_at)`, `decisions(created_at)`,
    `decisions(litellm_call_id)`;
  - `outcomes(created_at)`, `outcomes(response_id)`,
    `outcomes(litellm_call_id)`;
  - `evaluations(evaluator_id)`;
  - `decisions(shadow_of)`; `analyses(created_at)`, `analyses(chat_id, created_at)`,
    `analyses(decision_id)`.
- **JSON is jsonb** (Trino: `json`) rather than arrays: the PostgreSQL
  connector hides array columns by default.

**Migrations** run at every startup, in one transaction under an advisory
lock:
- each file is applied once and recorded with its SHA-256;
- an applied file whose content changed **stops startup**. Change the schema
  with a new `NNNN_*.sql`, never by editing one that has run.

**Trino.** The catalog is `postgres_route_policy`
(`ai/trino/catalogs/postgres_route_policy.properties`): `route-policy-db:5432`
by service DNS, through the read-only `trino_reader` role.

**Agreement:** did the chain the engine chose actually serve the turn? In
phase 2 this compared the shadow decision with the semantic router; under
`enforce` it measures LiteLLM's fallbacks:

```sql
SELECT d.applied, d.chosen_route = o.served_model_group AS agree, count(*)
FROM postgres_route_policy.public.decisions d
JOIN postgres_route_policy.public.outcomes o USING (decision_id)
WHERE d.mode = 'enforce'
GROUP BY 1, 2;
```

`applied = true, agree = false` means the hook did not use the answer: the
`/decide` came back too late (past `ROUTE_POLICY_DECIDE_TIMEOUT_S`) and the
turn went to `local-private`. Those rows should be rare; LiteLLM logs each
one as `[route-policy] … -> local-private (<why>)`. Overflow to Claude does
not count: `served_model_group` is the chain, the deployment is
`served_deployment`. An outcome with no decision row is a `/decide` that
never arrived or was not logged.

The comments inside `0001_initial.sql` still describe phase 2 (fire-and-
forget calls, the shadow comparison). The file is immutable once applied,
so they stay; this section is the current description.

**Every message's votes, route, the deployment that answered, and its cost.**
`LiteLLM_SpendLogs.request_id` is the provider's response id; `litellm_call_id`
is the cross-check:

```sql
SELECT d.created_at, d.chat_id, d.chosen_route, o.served_model_group, o.served_deployment,
       s.spend, s.total_tokens
FROM postgres_route_policy.public.decisions d
JOIN postgres_route_policy.public.outcomes o USING (decision_id)
JOIN postgres_litellm.public.litellm_spendlogs s ON s.request_id = o.response_id
WHERE json_extract_scalar(s.metadata, '$.litellm_call_id') = d.litellm_call_id
ORDER BY d.created_at DESC
LIMIT 100;
```

**Which evaluator decided:**

```sql
SELECT evaluator_id, vote_route, count(*)
FROM postgres_route_policy.public.evaluations
WHERE NOT abstained
GROUP BY 1, 2
ORDER BY 3 DESC;
```

**Shadow agreement** -- the promotion measure, by shadow ruleset: how often
the draft chose what the live ruleset chose, and the chain that served:

```sql
SELECT shadow_ruleset_id, shadow_ruleset_version, primary_route, shadow_route,
       count(*) AS turns,
       count_if(agree_with_served) AS agree_served
FROM postgres_route_policy.public.shadow_comparisons
WHERE shadow_decided_at > current_timestamp - INTERVAL '7' DAY
GROUP BY 1, 2, 3, 4
ORDER BY turns DESC;
```

**The classifier's vote against the rules', per turn** -- where `topic-llm`
decided the shadow, what `default`'s `sticky-code` said for the same turn:

```sql
SELECT e.vote_route AS classifier, p.vote_route AS sticky_code, s.primary_route, count(*)
FROM postgres_route_policy.public.shadow_comparisons s
JOIN postgres_route_policy.public.evaluations e
  ON e.decision_id = s.shadow_decision_id AND e.evaluator_id = 'topic-llm'
JOIN postgres_route_policy.public.evaluations p
  ON p.decision_id = s.primary_decision_id AND p.evaluator_id = 'sticky-code'
GROUP BY 1, 2, 3
ORDER BY 4 DESC;
```

**Why `topic-llm` abstained** -- coverage (`no verdict …` grows when
classifications lag behind turns):

```sql
SELECT regexp_replace(reason, '[0-9]+', 'N') AS why, abstained, count(*)
FROM postgres_route_policy.public.evaluations
WHERE evaluator_id = 'topic-llm'
GROUP BY 1, 2
ORDER BY 3 DESC;
```

**Classifications: status, latency, queue wait, cost**:

```sql
SELECT model_id, status, count(*) AS n,
       approx_percentile(latency_ms, 0.5) AS p50_ms, approx_percentile(latency_ms, 0.95) AS p95_ms,
       approx_percentile(queue_ms, 0.95) AS queue_p95_ms,
       avg(model_seconds) AS model_s, avg(prompt_tokens) AS tokens_in, avg(completion_tokens) AS tokens_out
FROM postgres_route_policy.public.analyses
WHERE created_at > current_timestamp - INTERVAL '1' DAY
GROUP BY 1, 2
ORDER BY 1, 3 DESC;
```

A rising `queue_p95_ms` is the classifier's FIFO filling with document jobs
([§ Known limitations](#known-limitations)); `not_local` means the model's
host did not resolve to a private address (a stopped container counts).

**Privacy.** No message text is stored anywhere. Decisions keep:
- ids (chat, message, user, key alias, call, response);
- routes and votes;
- reasons, which name patterns and tools, never content.

`analyses` keeps a hash of the transcript that was classified, never the
transcript, plus the model's own reason for its best-scoring criterion, cut
to 200 characters -- the only model-written text stored. It can paraphrase
the conversation ("the user asks to fix a crash in billing.py").

Treat `ROUTE_POLICY_TRINO_READER_PASSWORD` as a real secret anyway.

## Benchmark

`ai/route-policy/bench/` measures a classifier model and criteria set on
real conversations before anyone promotes it (ROUTE_POLICY_PLAN § 10). The
operator guide is [`bench/BENCH.md`](bench/BENCH.md). In short:
- export the recent Open WebUI chats (read-only, inside the `openwebui`
  container) to a box directory OUTSIDE the repo -- it is customer data,
  deleted afterwards, and no label ever comes from Claude;
- turn every user message into a decision point, rendered with the same
  `render_transcript` as the evaluator, with weak labels from what happened
  next;
- run every point × candidate model × criteria set through the SAME
  `/assess` cycle (`analysis.classifier_client`, each job deleted after
  reading), from a one-off container of this image (the image ships
  `bench/`), sequentially and off-hours;
- label ~150–200 points by hand in the offline `label.html`;
- `report.py` prints accuracy, macro-F1, `local-private` recall, the
  confusion matrix, agreement with the rules, failures, latency and the
  promotion rule as pass / fail.

Its decisions export reads `decisions.shadow_of` to keep shadow rows apart,
so it needs migration 0002 applied (any phase-4 route-policy has run it).

## Promoting the classifier

Only after the benchmark report passes the promotion rule (ROUTE_POLICY_PLAN
§ 10: at least as accurate as the rules on the gold set, `local-private`
recall at least as good as `privacy_kw` alone, under 1 % invalid, async p95
below a typical between-message gap) and `shadow_comparisons` agrees:

1. Add `topic-llm` to `default.yaml` -- a **new version** of `default`
   (versions are immutable), in the place `default-llm` tested (after the
   keyword and tool rules, replacing `sticky-code`).
2. Update `test_default_ruleset.py`'s expected routes and wiring.
3. Drop `default-llm` from `bindings.yaml`'s `shadow:` (keep its file only
   while it is still bound; a stored version needs no file).
4. `make build route-policy && make up route-policy`.

The verdict keys do not change with the ruleset version (they hash the
evaluator's configuration, not the ruleset), so the new `default` reads the
verdicts `default-llm` already stored for the same `topic-llm` config.

## Metrics

`GET /metrics`, scraped by the litellm stack's prometheus (job `route-policy`):

| Metric | Labels | What |
|---|---|---|
| `route_policy_decisions_total` | `route`, `mode`, `applied` | Decisions computed (duplicates not counted) |
| `route_policy_decide_seconds` | — | Engine time per decision, without the DB write |
| `route_policy_evaluator_seconds` | `evaluator`, `type` | Time per evaluator |
| `route_policy_votes_total` | `evaluator`, `type`, `outcome` (vote / abstain / error) | Evaluator results |
| `route_policy_invariant_overrides_total` | `invariant` (private / not_a_chain) | Decisions an invariant changed |
| `route_policy_observations_total` | `status` (success / failure / other) | Outcomes stored |
| `route_policy_duplicates_total` | `endpoint` | Repeated `decision_id`s |
| `route_policy_writes_deferred_total` | `write` (decision / outcome) | Writes that outlasted `ROUTE_POLICY_DECISION_WRITE_BUDGET_S`: answered at once, finished in the background |
| `route_policy_writes_in_flight` | — | Decision / outcome writes started and not finished (cap 256) |
| `route_policy_db_reads_cut_total` | `read` (binding_refresh / ruleset_fetch / analysis_lookup) | Reads on the `/decide` path that outlasted `DB_READ_BUDGET_S` |
| `route_policy_shadow_decisions_total` | `ruleset` (`<id>@<version>`), `agree` (true / false: chose the primary's route) | Shadow decisions stored |
| `route_policy_analyses_total` | `model`, `status` (ok / incomplete / unavailable / refused / failed / timeout / not_local) | Classifications finished |
| `route_policy_analysis_seconds` | `model` | One classification's cycle, locality check to result |
| `route_policy_analysis_queue_depth` | — | Requests waiting for a worker |
| `route_policy_analysis_dropped_total` | `reason` (queue_full / shutdown) | Requests not classified |
| `route_policy_errors_total` | `kind` (decision_write, decision_write_dropped, outcome_write, outcome_write_dropped, binding_refresh, ruleset_compile, unbound_model, shadow_load, shadow_dropped, shadow_error, analysis_lookup, analysis_write, classifier_delete) | Errors |

Shadow decisions also count in `route_policy_decisions_total` (with
`mode="shadow"`), `route_policy_votes_total` and
`route_policy_evaluator_seconds`. Plus the HTTP request metrics of
prometheus-fastapi-instrumentator. Each `/decide` logs one INFO line:
`[decide] <id> auto -> local-code (code-tools: called … ; default@2 enforce) chat=<id>`,
each shadow one `[shadow] <id> default-llm@1 -> … beside <primary id> -> …`,
each classification one `[analysis] <key prefix> qwen3.5-4b k=3 -> local-code (ok; scores {…}; 6.2s, queue 0.4s) chat=<id>`
-- ids, routes, scores and timings, never text.

## Configuration

**Infrastructure only.** Routing behaviour lives in rulesets, never in
environment variables.

| Variable | Default | What |
|---|---|---|
| `PORT_ROUTE_POLICY` | `8027` | LAN port, published by `ai/edge` |
| `PORT_ROUTE_POLICY_DB` | `5441` | Host port of `route-policy-db` (psql / DBeaver) |
| `ROUTE_POLICY_DB_USER` / `_PASSWORD` / `_NAME` | `route_policy` × 3 | Database credentials (dev defaults; override before exposing 5441) |
| `ROUTE_POLICY_TRINO_READER_PASSWORD` | _(empty)_ | `trino_reader` password; empty skips the role (only the Trino catalog breaks) |
| `ROUTE_POLICY_DB_POOL_MAX` | `10` | Pool ceiling |
| `ROUTE_POLICY_BINDING_REFRESH_S` | `10` | Age at which `/decide` re-reads `bindings` |
| `ROUTE_POLICY_DECISION_WRITE_BUDGET_S` | `0.05` | How long `/decide` / `/observe` wait for their row before answering anyway (floor 0; the write then finishes in the background). Keep it well below LiteLLM's 0.25 s `ROUTE_POLICY_DECIDE_TIMEOUT_S` |
| `ROUTE_POLICY_CLASSIFIER_URL` | `http://classifier:8000` | The document classifier, by Docker DNS on `ai_shared`, no auth. Empty (only possible outside the compose file) runs no analysis queue: every `llm_classifier` abstains |
| `ROUTE_POLICY_CLASSIFY_TIMEOUT_S` | `180` | One classification, from the locality check to the result (floor 1); past it the job is cancelled + deleted and stored as `timeout` |
| `ROUTE_POLICY_CLASSIFY_POLL_S` | `1` | `GET /jobs/{id}` interval (floor 0.1); also the resolution of `analyses.queue_ms` |
| `ROUTE_POLICY_ANALYSIS_QUEUE_MAX` | `64` | Requests that may wait for a worker (floor 1); past it dropped and counted |
| `ROUTE_POLICY_ANALYSIS_CONCURRENCY` | `2` | Workers = route jobs open on the classifier at once (floor 1) |

Constants in `route_policy/config.py`, not environment variables:
`WRITES_MAX_IN_FLIGHT` (256 background writes and after-the-answer tasks),
`WRITES_DRAIN_S` (5 s at shutdown, for the writes and again for running
classifications), `DB_READ_BUDGET_S` (0.1 s for the binding refresh, a
version fetch and a verdict lookup on the `/decide` path),
`CLASSIFIER_MODELS_TTL_S` (60 s cache of `GET /models` and of each model
host's locality verdict).

`ROUTE_POLICY_DB_HOST` / `_PORT` are fixed in the compose file
(`route-policy-db:5432`). The hook's own settings belong to LiteLLM (see
`ai/litellm/LITELLM.md`): `ROUTE_POLICY_URL`, `ROUTE_POLICY_ENABLED` (`false`
or an empty URL sends every `auto` turn to `local-private`),
`ROUTE_POLICY_DECIDE_TIMEOUT_S` (0.25, the awaited `/decide`) and
`ROUTE_POLICY_TIMEOUT_S` (the background `/observe`).

## Operations

- **Logs:** `make logs route-policy route-policy`. It is swap-class, so the
  container is `route-policy-<hash>`. Use `make exec route-policy route-policy
  CMD='…'`, not `docker exec route-policy`.
- **psql:** `docker exec -it route-policy-db psql -U route_policy`.
- **Startup failed with `SeedConflict`:** a baked-in ruleset changed without a
  version bump. Bump `version` and rebuild. The old copy keeps serving
  meanwhile; a swap never retires a copy that did not get healthy.
- **Startup failed with `MigrationChanged`:** an applied migration file was
  edited. Restore it and add a new migration.
- **`/ready` is 503 but `/health` is 200:** the database is unreachable. The
  service keeps deciding from its cache, in time; decisions in the gap are
  not logged (`route_policy_errors_total{kind="decision_write"}`, or
  `decision_write_dropped` once 256 writes are stuck behind a hung
  database).
- **`route_policy_writes_deferred_total` climbing:** route-policy-db is slow
  or the pool (`ROUTE_POLICY_DB_POOL_MAX`) is exhausted. Routing is
  unaffected; the log falls behind, and past 256 pending writes it drops.
- **Retrofit or rotate the Trino role:**
  1. Edit `ROUTE_POLICY_TRINO_READER_PASSWORD`.
  2. `make up route-policy route-policy-db`.
  3. Re-run the script:
     ```bash
     docker exec route-policy-db sh /docker-entrypoint-initdb.d/10-trino-reader.sh
     ```
  4. `make up trino trino-coordinator`.
- **Deploy classes:**
  - `route-policy` swaps side by side. Two copies are safe: every decision
    comes from the request alone, startup takes advisory locks, and writes
    are idempotent.
  - `route-policy-db` is recreated (one data directory, one Postgres). The
    service keeps deciding during the gap.
- **Exposure:** no auth on the service. Anything on `ai_shared`, and any LAN
  client in `EDGE_ALLOWED_CIDRS` on 8027, can:
  - read decisions by chat id;
  - write decisions and outcomes.

  That is the classifier's posture, and `/decide` being authoritative does
  not change what a caller can do with it: the hook makes a fresh uuid4 per
  request, so nobody can pre-store a decision LiteLLM will read back, and the
  hook's own invariants cap any answer at the four chain aliases, never off
  `local-private`. **What is now routing control is the database:** a row in
  `bindings` (re-read every `ROUTE_POLICY_BINDING_REFRESH_S`) or a new
  `rulesets` version decides every `auto` turn, including whether PII
  reaches a Claude-capable chain. Override the dev-default
  `ROUTE_POLICY_DB_PASSWORD` and keep `PORT_ROUTE_POLICY_DB` off untrusted
  networks.
- **Route-policy down:** every `auto` turn goes to `local-private` (counted
  and logged by LiteLLM's hook) until a copy answers again. A swap never
  causes that: the old copy serves until the new one is healthy.
- **No `analyses` rows after `auto` turns:** check
  `route_policy_analyses_total` by status and the `[analysis]` lines in
  `make logs route-policy route-policy`:
  - `not_local`: the model's host did not resolve to a private address from
    route-policy -- is `qwen3.5-4b` running (`make ps llama`)? Is its
    `models.yaml` entry still `authenticated: false`?
  - `refused`: the classifier does not know the model (restart it after a
    `models.yaml` edit) or refused the request (the error says why);
  - `unavailable`: the classifier is down or answering 5xx;
  - `timeout`: the classifier's queue is backed up with document jobs, or
    the model is slow (raise `ROUTE_POLICY_CLASSIFY_TIMEOUT_S` only after
    looking at `queue_ms`).
- **Leftover route jobs on the classifier:** every cycle deletes its job; a
  failed DELETE is counted (`classifier_delete`) and that job expires with
  the classifier's `JOB_TTL_HOURS`. A hard kill of route-policy mid-cycle
  can orphan one the same way.
- **`route_policy_analysis_dropped_total{reason="queue_full"}` climbing:**
  classifications are slower than turns arrive. Those turns get no verdict;
  routing is unaffected (the shadow's `topic-llm` abstains more).

## Code layout

```
ai/route-policy/
  docker-compose.route-policy.yml  Dockerfile  pyproject.toml  ROUTE_POLICY.md  route-policy.postman_collection.json
  db-init/10-trino-reader.sh       the read-only Trino role (first start of an empty volume)
  rulesets/default.yaml  rulesets/default-llm.yaml  rulesets/bindings.yaml
  rulesets/patterns/{privacy,code,reasoning}.yaml
  bench/                the classifier benchmark (bench/BENCH.md), shipped in the image
  route_policy/
    app.py              FastAPI factory + lifespan (pool, migrations, seeding, analysis queue)
    config.py           infrastructure settings only
    metrics.py          Prometheus metrics
    service.py          the shared objects; storing decisions and outcomes; verdict lookups;
                        the after-the-answer task (shadows, analysis requests)
    api/                decide, observe, evaluate, rulesets, chats, evaluator_types, health, schemas
    engine/             context (the request view), pipeline (evaluators → combiner → invariants), invariants
    evaluators/         base (contract, @declares, registry), regex_text, tool_calls, cooldown, backend_load,
                        static, llm_classifier
    combiners/          base (force / veto / default frame), priority, weighted
    rulesets/           model (document, compiled form, hash), loader (files, refs, bindings), validate, store
    analysis/           model (AnalysisRequest, Verdict), verdict (choose), classifier_client (the /assess
                        cycle), queue (AnalysisQueue)
    db/                 pool, migrations/ (0001_initial.sql, 0002_llm_classifier.sql + runner), repositories/
```

Reused from `shared/common`:
- `common.keywordmatch`: Go-RE2 keyword rules.
- `common.conversation`: exchanges, called tools, `user_message_count`,
  `render_transcript`.
- `common.net`: `DEFAULT_BLOCKED_NETWORKS`, the locality guard's networks.

## Tests

`unit-tests/route-policy/`, in its own pytest process:

```bash
UV_LINK_MODE=copy uv run --no-sync python -m pytest unit-tests/route-policy -q -p no:cacheprovider
TEST_POSTGRES_DSN=postgresql://postgres@localhost:55441/postgres \
  UV_LINK_MODE=copy uv run --no-sync python -m pytest unit-tests/route-policy -q -p no:cacheprovider
```

| Module | Covers |
|---|---|
| `test_evaluators.py` | Each evaluator. The cooldown's verdicts and reasons are pinned on a table of histories. |
| `test_declarations.py` | Every real evaluator's `detail` against its declaration |
| `test_combiners.py`, `test_invariants.py`, `test_pipeline.py` | The frame, both combiners, the invariants, the contract shapes, error handling |
| `test_rulesets.py` | Files, refs (no path escape), validation, content hash, seed bindings, the cache |
| `test_default_ruleset.py` | The pattern files (valid rules, every regex in the RE2 subset, the real-chat prompts `code_kw` must and must not match), the ruleset's wiring, and its routes on ~20 conversation shapes |
| `test_api.py` | Every endpoint with no database: contract, validation, decisions still answered with the DB down |
| `test_writes.py` | No database wait holds `/decide`: deferred and dropped writes, shutdown, bounded reads |
| `test_llm_classifier.py` | The evaluator: config validation, route checks, fingerprint, verdict keys (stable across tool rounds, changed by an edit), ageing, every vote and abstain reason, analysis requests, the tie order surviving a jsonb round trip, the `default-llm` wiring |
| `test_verdict.py` | `choose`: winners, ties, `otherwise` confidence, errored criteria |
| `test_analysis_client.py` | The `/assess` cycle on a fake classifier: every status, the locality guard (authenticated, public, mixed and unresolvable hosts refused before submitting; private / loopback / ULA local), caches, DELETE always (after a cancel on timeout, and when cancelled), no submitted text in any error |
| `test_analysis_queue.py` | Dedupe, overflow, skip-if-stored, the written row (scores by route, winner's reason, usage, provenance; failures with no scores), write failures, shutdown (waiting dropped, running cancelled and still deleted) |
| `test_shadows.py` | Resolving shadows, skipped unloadable ones, derived ids, the primary response unchanged by a shadow, the shadow row and its evaluations, idempotency, provenance of analysis requests, a primary with an `llm_classifier`, isolation (a raising shadow, a hung database, the task cap), `/evaluate` reading without enqueueing |
| `test_db.py` (`postgres`) | Startup, seeding (with shadow slots), immutability, migrations, the phase-2 → phase-3 upgrade, idempotent `/decide` / `/observe` in either order, shadow rows and both views, the `analyses` table (first write wins, caps, the CHECK), the whole classifier loop on a fake classifier (job deleted, verdict voted next turn, no text stored), an edit making a verdict unreachable, a stored draft compiling like its file, the reads |
| `test_wiring.py` | Postman coverage, compose classes and env, Trino env, pass-through, Makefile, Dockerfile (bench shipped and imported), line endings |
| `bench/` | The benchmark's pure parts ([`bench/BENCH.md`](bench/BENCH.md)) |

- **The `postgres` tests** need `TEST_POSTGRES_DSN`: an admin connection on
  which each session creates and drops its own `route_policy_test_*`
  database. Without it they skip.
- **For a scratch server on Windows,** use the EDB binaries zip:
  `initdb -U postgres --auth=trust`, then `pg_ctl -o "-p 55441" start`.
