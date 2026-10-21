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

**Phase 3: the engine routes `auto`.** The `default` ruleset (version 3) is
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
  `shadow_of`, never applied. `default` still routes every turn by its
  keyword and tool rules.

Trino's `shadow_comparisons` and the benchmark ([§ Benchmark](#benchmark))
say whether it is better; promoting it is a separate, later change
([§ Promoting the classifier](#promoting-the-classifier)).

**One route per turn, and no classification for background tasks**
(`default@3`, `default-llm@2`, migration 0003):
- **A turn keeps one route.** A ruleset with `pin: turn` holds every call
  of one answer (the same Open WebUI chat id and message id) to the route
  its first call got. That covers the answer's tool rounds and Open WebUI's
  hidden auto-continue nudge, which arrives as an extra user message under
  the same message id. A fresh `local-private` always wins over the pin.
  [§ Turn pinning](#turn-pinning).
- **Background tasks are flagged.** The hook marks Open WebUI's title / tags
  / follow-up requests with `background_task`. They are decided and logged
  as before, but an `llm_classifier` never classifies one, and a background
  task never pins a turn. [§ Background tasks](#background-tasks).
- **The code cooldown is 2 exchanges** (was 3), and `default-llm`'s code
  criterion no longer counts a tool call (a web search) as "running
  software".

**Phase 5: fast signals, in shadow.** A second draft, `default-signals`,
is bound beside `default-llm` as another shadow. Its evaluators read fast
CPU signals from the **`signals` sidecar** (`ai/signals`, ONNX models on
CPU) -- PII NER, a nearest-example topic, a prompt-injection score, the
language -- fetched in ONE `POST /signals` per decision within
`ROUTE_POLICY_SIGNALS_BUDGET_S` (0.1 s), concurrently with the database
reads ([§ Signals](#signals)):
- **`pii` forces `local-private`** on sensitive data (email, phone, street
  address, government id, card / bank / account number) in ANY user
  message -- once it appears, the chat stays private -- or in a Phoenix /
  Trino tool result; a name forces only with a date of birth, a bare name
  never. Its patterns (`common.pii`) run in-process, so it still forces
  with the sidecar down;
- **`topic`** (`nearest_examples`) votes the route of the curated example
  prompts nearest to the latest user message, for the turns no keyword,
  tool or cooldown rule claimed;
- the **injection score**, the code shape, the language and the size are
  logged with the new role **`observe`** and route nothing.

`default@3` keeps routing every turn and makes no signals call; promotion is
a separate, later change ([§ Promoting the signals](#promoting-the-signals)).
[§ The `default-signals` draft](#the-default-signals-draft).

Plan: `ROUTE_POLICY_PLAN.md` and `ROUTE_POLICY_PHASE5_PLAN.md` (outside the
repo), phases 2–6.

| | |
|---|---|
| Compose file | [`docker-compose.route-policy.yml`](docker-compose.route-policy.yml) |
| Services | `route-policy` (FastAPI, swap-class), `route-policy-db` (Postgres 17, recreate-class) |
| Inside ai_shared | `http://route-policy:8000` (what LiteLLM uses), `route-policy-db:5432` |
| Calls | `http://classifier:8000` (`ROUTE_POLICY_CLASSIFIER_URL`): background `/assess` jobs only, never on the `/decide` path; `http://signals:8000` (`ROUTE_POLICY_SIGNALS_URL`): one `POST /signals` per decision whose ruleset asks (today only the `default-signals` shadow, after the answer), within `ROUTE_POLICY_SIGNALS_BUDGET_S` |
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
- [Turn pinning](#turn-pinning)
- [Background tasks](#background-tasks)
- [Shadow rulesets](#shadow-rulesets)
- [The llm_classifier](#the-llm_classifier)
- [The `default-llm` draft](#the-default-llm-draft)
- [Signals](#signals)
- [The `default-signals` draft](#the-default-signals-draft)
- [Decision log and Trino](#decision-log-and-trino)
- [Benchmark](#benchmark)
- [Promoting the classifier](#promoting-the-classifier)
- [Promoting the signals](#promoting-the-signals)
- [Metrics](#metrics)
- [Configuration](#configuration)
- [Operations](#operations)
- [Code layout](#code-layout)
- [Tests](#tests)

## How it fits

```
Open WebUI ─► LiteLLM (model `auto`, one pass)
                ├─ pre-call hook ── POST /decide {decision_id, messages, tools, ids, backends, background_task} ──► route-policy
                │   (awaited, ≤ 0.25 s)  ◄── {route: local-code, apply: true, ...} ────────────── ├─ ruleset (bound to `auto`)
                │   data["model"] = "local-code"   (fallback on any failure: local-private)       ├─ evaluators → votes
                ├─ deployment pick: overflow filter (context fit, busy / down) inside the chain   ├─ combiner → route
                ├─ vLLM / Claude ── STREAMED straight back to the client                          ├─ invariants
                │                                                                                 └─ the turn's pin (`pin: turn`)
                └─ success / failure hook ── POST /observe {decision_id, served chain, ...} ──► (fired)  │
                                                                         route-policy-db ◄───────────────┤
                                                                              │ trino_reader             │
                                                                         Trino: postgres_route_policy    │
                                                                                                         │
   after the answer (one background task per /decide) ───────────────────────────────────────────────────┘
     ├─ shadow rulesets (default-llm, default-signals) decided on the same request ──► decisions row, shadow_of = primary
     │    └─ default-signals: ONE POST /signals (≤ ROUTE_POLICY_SIGNALS_BUDGET_S) ──► signals:8000 (ai/signals, CPU)
     │                          pii NER · nearest-example topic · injection · language ◄── statuses + features, never text
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
bounded query, and so is the turn's first decision for a `pin: turn`
ruleset (the two reads run concurrently); shadow rulesets and new
classifications start only after the answer, and cannot change, delay or
fail it. A ruleset whose evaluators read the signals sidecar gets ONE call,
concurrently with those reads and within `ROUTE_POLICY_SIGNALS_BUDGET_S`
(0.1 s, no more than the read budget), so the worst case does not grow;
today only the `default-signals` shadow makes it, after the answer
([§ Signals](#signals)).

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

On a box that already runs `default@3` and `default-llm@3`, follow
[§ Upgrading to phase 5](#upgrading-to-phase-5-default-signals1); on one
that runs phase 4,
[§ Upgrading to the turn pins](#upgrading-to-the-turn-pins-default3)
instead; on one that runs phase 3,
[§ Upgrading to phase 4](#upgrading-to-phase-4) first; on one that runs
phase 2 (shadow), [§ Upgrading from phase 2](#upgrading-from-phase-2-shadow)
before that.

Routing needs only route-policy and its database. The `llm_classifier` in
the shadow ruleset also needs the document classifier (`ai/classifier`) with
the `qwen3.5-4b` entry in its `models.yaml`, and the `qwen3.5-4b` llama.cpp
service (`ai/llama`); without them every background classification is
stored as failed (`unavailable`, `refused`, `not_local`) and `topic-llm`
abstains -- routing is unaffected. Likewise the `default-signals` shadow
reads the `signals` sidecar (`ai/signals`, `ai/signals/SIGNALS.md`): without
it every signal evaluator abstains and `pii` votes from its patterns alone
-- routing is unaffected. Deploy it before route-policy so the startup
warm-up of the example set reaches it.

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
make logs route-policy route-policy FOLLOW=0 TAIL=50   # "applied migration 0001_initial.sql" / "0002_llm_classifier.sql" /
                                                      # "0003_turn_pins.sql", "seeded default@3", "seeded default-llm@3",
                                                      # "binding auto -> default@3", "binding auto shadowed by default-llm@3",
                                                      # "analysis: queue ready (2 worker(s), ...)"
curl -s localhost:8027/health                         # {"status":"ok","rulesets":[{"id":"default","version":3},
                                                      #   {"id":"default-llm","version":3},
                                                      #   {"id":"default-signals","version":1}],"bindings":1}
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

Staged, so `auto` never breaks. (Written for the phase-3 image, which
baked `default@2`; a later image seeds its own current version in step 2,
and the rest is the same.) On the box, from the repo root:

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

Routing does not change: `default@2` keeps deciding every `auto` turn (an
image that also has the turn pins moves it to `default@3` in step 4; the
checks of [§ Upgrading to the turn pins](#upgrading-to-the-turn-pins-default3)
apply then too). On the box, from the repo root:

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

### Upgrading to the turn pins (default@3)

`default@3` (the 2-exchange code cooldown, `pin: turn`), `default-llm@2`
(the reworded code criterion, `pin: turn`) and migration 0003. Staged, like
phase 3: route-policy first, then the hook. On the box, from the repo root:

1. **`git pull`.** No new `.env` lines.
2. **`make build route-policy && make up route-policy`.** Migration 0003
   adds `decisions.pinned_from`, `decisions.background_task` (old rows read
   `false`), the turn index and the new view columns; `default@3` and
   `default-llm@2` are seeded and the binding and its shadow slot move to
   them. The running LiteLLM does not send `background_task` yet; it
   defaults to `false`, so everything else works at once. Check:
   - the deploy log lines and `/health` above show the new versions;
   - two calls of one answer (an `auto` chat that uses a tool) give one
     route:
     `docker exec route-policy-db psql -U route_policy -c "select message_id, chosen_route, pinned_from is not null as pinned, left(reason, 60) from decisions where shadow_of is null order by created_at desc limit 6"`.
3. **`make up litellm`.** The hook now sends `background_task`. Check that
   an Open WebUI chat title lands as a decision with `background_task =
   true` and no `analyses` row of its own.
4. **Open WebUI (recommended): Admin Settings → Interface → Task Model =
   `local-private`.** Background tasks then never reach `auto` (or Claude)
   at all; see [§ Background tasks](#background-tasks).

**Rollback:** `git revert` the change and `make up route-policy` (then
`make up litellm`). The reverted image bakes `default@2` / `default-llm@1`
again, already stored with the same content, and moves both slots back;
the old code ignores the two columns (new rows get their defaults) and the
appended view columns. Versions 3 and 2 stay stored with their decisions.

### Upgrading to default-llm@3

Only the shadow draft changes: `topic-llm` reads just the latest user
message, a verdict counts for the next message only, and the code
criterion covers building an app again ([§ The `default-llm`
draft](#the-default-llm-draft)). Routing does not change -- `default@3` is
untouched.

1. **`git pull`.** No new `.env` lines, no migration.
2. **`make build route-policy && make up route-policy`.** `default-llm@3` is
   seeded and the shadow slot moves to it (`/health` lists
   `{"id":"default-llm","version":3}`). Its fingerprint is new, so it
   classifies from scratch; compare versions in Trino by
   `shadow_ruleset_version` / `analyses.ruleset_version`.

**Rollback:** `git revert` and `make up route-policy`; `default-llm@2` is
already stored with the same content and the slot moves back.

### Upgrading to phase 5 (default-signals@1)

Only a second shadow is added: `default-signals@1` beside `default-llm@3`.
Routing does not change -- `default@3` is untouched and makes no signals
call. No migration. On the box, from the repo root (the sidecar's own guide
is `ai/signals/SIGNALS.md`):

1. **`git pull`**, and add the new `.env` lines from `.env.example`: the
   `## Signals` block, the `PORT_SIGNALS` row, and
   `ROUTE_POLICY_SIGNALS_URL` / `ROUTE_POLICY_SIGNALS_BUDGET_S` in the
   `## Route policy` block (the compose file falls back to the same
   defaults if they are missing).
2. **`make up edge`** (it gains the sidecar's LAN port; a recreate, a second
   or two of LAN downtime).
3. **`make build signals && make up signals`.** Wait for `curl -s
   localhost:8025/health` to answer 200 (models loaded and warm).
4. **`make build route-policy && make up route-policy`.** `default-signals@1`
   is seeded and added as the binding's second shadow slot, and startup
   sends its example set to the sidecar (`startup: signals warm-up nearest:
   ok (nearest warming)` in the log). Check:
   - `/health` lists `{"id":"default-signals","version":1}` and `/bindings`
     shows two shadow slots;
   - a dry run: `curl -s -X POST localhost:8027/evaluate -H
     'content-type: application/json' -d
     '{"ruleset":{"id":"default-signals"},"messages":[{"role":"user","content":"my SSN is 523-45-6789"}]}'
     | jq '.route, (.votes[] | select(.evaluator=="pii") | .reason)'` →
     `"local-private"`, `"sensitive data: gov_id"`;
   - after a few `auto` turns, `shadow_comparisons` has a
     `default-signals` row per turn, and `curl -s localhost:8027/metrics |
     grep route_policy_signals_total` shows `outcome="ok"` growing (not
     `timeout` / `unavailable` / `not_local`).
5. **`docker restart prometheus`** (the sidecar's scrape job).
6. **Optional: `qwen3.5-4b` on cores of its own** (the box's CPU split,
   `ai/llama/LLAMA.md § qwen3.5-4b`). Copy `LLAMA_QWEN35_4B_THREADS=16` and
   `LLAMA_QWEN35_4B_EXTRA_ARGS="--cpu-range 0-15"` from `.env.example`,
   check the image knows the flag (`docker run --rm --entrypoint
   llama-server llama-unsloth:<LLAMA_UNSLOTH_CPU_TAG>-cpu --help | grep
   cpu-range`), then `make up llama qwen3.5-4b` -- a recreate: while it
   reloads, background classifications fail (`not_local` /
   `unavailable`) and `topic-llm` abstains; routing is unaffected.

**Rollback:** `git revert` and `make up route-policy`: the binding loses the
slot; `default-signals@1` stays stored with its decisions. Then
`docker compose -p ai-signals down` and `make up edge`.

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
which only started awaiting the answer; the request gained
`background_task` with the turn pins, the response is unchanged).

```json
{"decision_id": "uuid4, made by the hook", "requested_model": "auto",
 "litellm_call_id": "…", "chat_id": "… | null", "message_id": "… | null", "user_id": "… | null", "key_alias": "… | null",
 "messages": [/* the OpenAI messages as LiteLLM received them */], "tools": [/* or null */],
 "backends": {"<api_base>": {"verdict": "ok|busy|down|stale|unknown", "waiting": 0.0, "running": 0.0}},
 "background_task": false}
```

- **`background_task`:** the hook's verdict that this is an Open WebUI
  background task (a title, tags, follow-ups; [§ Background
  tasks](#background-tasks)). Optional, default `false`, `null` reads as
  `false`: a hook that predates it still works, which is why route-policy
  deploys first.

→ `200`, and the same body is what gets logged:

```json
{"decision_id": "…", "route": "local-code", "mode": "enforce", "apply": true,
 "ruleset": {"id": "default", "version": 3},
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
  true for `default@3` unless a combined (or pinned) route fell outside the
  ruleset's `routes`. LiteLLM applies `route` only when it is true; `false`
  (a `shadow` ruleset, or an invariant override) sends the turn to
  `local-private`.
- **`route` of a later call of a turn** is the turn's first route for a
  `pin: turn` ruleset (`default@3` is one), unless the fresh decision is
  `local-private`; `votes` and `scores` are still the fresh ones.
  [§ Turn pinning](#turn-pinning).
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
  shadow); the HTTP hop, the turn-pin read below (one indexed query) and,
  normally, a few milliseconds of decision write are the rest. Every `auto`
  turn waits for it.
- **Verdict lookups.** When the primary ruleset has an `llm_classifier`
  (`default@3` has none), `/decide` reads the verdicts it names in ONE query
  before the pipeline runs, within `DB_READ_BUDGET_S` (0.1 s); past it the
  evaluator abstains (`lookup timed out`) and the decision goes on.
  `route_policy_db_reads_cut_total{read="analysis_lookup"}` counts it.
- **The turn's first decision.** For a `pin: turn` ruleset and a request
  with both `chat_id` and `message_id` (and not a background task),
  `/decide` reads the turn's first decision in ONE query, concurrently with
  the verdicts and within the same budget. Past it, or on a failure, the
  call is decided afresh as if it were the turn's first
  (`route_policy_db_reads_cut_total{read="turn_pin"}`,
  `route_policy_errors_total{kind="turn_pin"}`).
- **Signals.** When the primary ruleset has signal evaluators (`default@3`
  has none, so no call), `/decide` sends ONE `POST /signals` for all of
  them, concurrently with the two reads, within
  `ROUTE_POLICY_SIGNALS_BUDGET_S` (0.1 s; the sidecar is told to answer 20
  ms before it). Past it, or on any failure -- a bug around the call
  included: `load_signals` never raises, it leaves `signals failed` and
  counts `route_policy_errors_total{kind="signals"}` -- the signal
  evaluators abstain and `pii` votes from its patterns ([§ Signals](#signals)).
- **After the answer, in one background task** (counted with the writes
  against `WRITES_MAX_IN_FLIGHT`; dropped past it, `shadow_dropped`):
  1. the primary's `llm_classifier` analysis requests go to the analysis
     queue (none from a call that is not its turn's first);
  2. each **shadow** ruleset of the binding runs on the same request (its
     own verdict lookup, turn pin and signals call, with the same budgets,
     so it measures what enforcing it would do) and is stored as a `decisions` row with
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
  `shadow_of` is null. Every decision also carries `pinned_from` (the turn's
  first decision, on a later call of a `pin: turn` turn; null otherwise)
  and `background_task`.

### POST /evaluate

Give exactly one of:
- `{"ruleset": {"id": "default", "version": 3}}` (version omitted = the
  highest). A stored version that is no longer baked in (`default@1`,
  `default@2`) works too: it is compiled from the database;
- `{"document": {…}}`.

Plus `messages` / `tools` / `backends` and an optional `requested_model`
(default `auto`) and `background_task` (default `false`). There is no chat
or message id, so a `pin: turn` ruleset is never pinned here: every call is
its turn's first.

It returns the `/decide` shape with `decision_id: null`. Each vote also carries
its evaluator's `detail` and `error`.

- **An `llm_classifier`** reads the verdicts STORED for the conversation
  (one read-only query, waited for as long as it takes); `/evaluate` never
  enqueues a classification, so a conversation `/decide` never saw has none
  and the evaluator abstains (`no verdict in the last N message(s)`).
- **The signal evaluators** get ONE signals call, as on `/decide`, but
  waited for up to `SIGNALS_EVALUATE_TIMEOUT_S` (10 s) with no deadline cut,
  so a draft is judged on complete signals; each signal vote's `detail`
  carries its task's `status` and `signals_ms`. It runs concurrently with
  the verdict read.
- **A draft document** may use `patterns_ref` / `examples_ref` refs. They
  resolve inside the baked `rulesets/` directory; a path that leaves it is a
  `422`.
- **A stored document** from `GET /rulesets/{id}/{version}` posts back as it
  is.
- **An invalid document** is a `422` whose `detail.errors` lists **every**
  problem.

## Rulesets

```yaml
id: default                      # [a-z0-9][a-z0-9_-]*
version: 3                       # immutable: new content = a new version
description: …
mode: enforce                    # shadow (decide + log) | enforce (LiteLLM applies it)
pin: turn                        # optional: one route per turn (§ Turn pinning); omitted = every call afresh
routes: [local-general, local-code, local-reasoning, local-private]
default_route: local-general     # one of routes
evaluators:                      # in order; each votes or abstains
  - id: privacy
    type: regex_text             # GET /evaluator-types
    role: force                  # vote (default) | force | veto | observe (logged, never combined)
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
- **One parameter set per signals task:** two evaluators that would ask the
  signals sidecar for the same task with different parameters are refused
  ([§ Signals](#signals)).
- **Example sets** (`nearest_examples`): ids unique, routes among `routes`,
  texts 1–300 characters, at most 1,000 examples.
- **`pin`** is `turn` or absent (`null`); anything else is an error. It is
  content like the rest: adding or dropping it is a new version.

**Files** (`ai/route-policy/rulesets/`, baked into the image):
- `*.yaml` at the top level: one ruleset each (`default.yaml`,
  `default-llm.yaml`, `default-signals.yaml`).
- `bindings.yaml`: the seed bindings.
- `patterns/*.yaml`: keyword rules, referenced by `patterns_ref` -- by
  `default`, `default-llm` AND `default-signals`, so a pattern edit is new
  content for all three: bump all three versions.
- `examples/*.yaml`: labelled example sets for `nearest_examples`,
  referenced by `examples_ref` and inlined as `config.examples` -- the same
  mechanism, so an edited example is new content too
  ([§ The `default-signals` draft](#the-default-signals-draft)).

**Bindings** (`bindings.yaml`):

```yaml
- model: auto                # the requested model
  key_alias: null            # optional: only requests from this LiteLLM key alias
  ruleset: default           # must be mode: enforce
  version: null              # omitted = the highest version baked in
  shadow: [default-llm, default-signals]   # optional: each an id (highest version) or
                                           # {ruleset: <id>, version: <n>}; each must be mode: shadow
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

**A stored version needs no file.** Only the current `default` (version 3),
`default-llm` (version 3) and `default-signals` (version 1) are baked in; `default@1` (phase 2, shadow),
`default@2` (phase 3), `default-llm@1` and `default-llm@2` stay in the database with the
decisions made under them, and the store compiles each from there on first
use (`GET /rulesets/default/1`, `POST /evaluate`). Keeping old files would
also freeze the pattern files: a version that references `patterns/code.yaml`
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
| `llm_classifier` | `model`, `criteria` (route → question), `otherwise`, `min_score`, `history`, `max_age_exchanges` | A stored verdict (a local model's classification of an earlier prefix of this conversation, at most `max_age_exchanges` user messages old) names a route. **Chooses its own route**: `vote.route` is refused. Abstains for a background task (`background task`), which it never classifies. [§ The llm_classifier](#the-llm_classifier). |
| `pii` | `scope: {user_messages: all \| latest, tool_results: null \| <full-match regex over tool names>}`, `force: [types]` (required), `with_person: [types]`, `ner` (true), `min_score` (0.5), `model` (optional NER pin) | A `force` type is found in a text in scope -- by `common.pii`'s patterns (in-process, always) or by the sidecar's NER at `max_score >= min_score` -- or a `with_person` type in the SAME text as a person. `person` / `location` are refused in both lists: alone they never force. With the sidecar down, cut or on another model it votes from the patterns (reason `… (patterns only: signals timed out)`). `detail`: `types`, `by_source`, `forced_by`, `forced_in` (`latest_user` / `earlier_user` / `tool:<name>`), `texts_in_scope`, `texts_scanned`, `chars_scanned`, `ner_status`, `ner_unscanned_texts`, `ner_model`, `signals_ms`. |
| `nearest_examples` | `model` (pin), `examples` (usually `examples_ref`), `k` (5), `min_share` (0.8), `min_similarity` (0), `max_tokens` (256), `scope: current_message` | The top-k route's share of the similarity mass is ≥ `min_share`. **Chooses its own route** (confidence = the share). Abstains while the sidecar embeds the set (`nearest warming`), on any missing answer, and on another embedding model (`nearest model mismatch`). `detail`: `model`, `status`, `fingerprint`, `neighbours` (example ids), `route_shares`, `best_similarity`, `signals_ms`. |
| `prompt_injection` | `model` (pin), `threshold` (0.9), `scope: current_message \| current_exchange` (+ this exchange's tool results: indirect injection), `max_tokens` (256) | The highest score (head + tail windows per text) is ≥ `threshold`. `detail`: `model`, `status`, `score`, `scored_texts`, `windows`, `signals_ms`. Observed only in `default-signals`. |
| `language` | `languages` or `not_languages` (exactly one), `min_confidence` (0.6) | The latest user message's language (fastText) matches. `detail`: `language`, `confidence`, `status`, `signals_ms`. |
| `text_shape` | `min_lines`, `min_code_ratio` (at least one) | Pure. The latest user message looks like pasted code. `detail`: `chars`, `lines`, `fenced_blocks`, `code_lines`, `code_ratio`, `urls`, `attachments` `{image, file, audio}` (content parts by type -- the hook's slimmed placeholders keep it). |
| `context_size` | `max_tokens`, `chars_per_token` (2.5) | Pure. The overflow hook's estimate -- JSON characters of `messages` + `tools` / `chars_per_token` -- is above `max_tokens`. `detail`: `estimated_tokens`, `chars`, `max_tokens`. |

- **Exchanges** come from `common.conversation.split_exchanges`: one per user
  message, holding the message and every round after it.
- **A user message is what the user typed.** On a request that carries
  retrieved sources -- a turn's attached files or knowledge on its first
  call, the pages a web search or fetch tool returned on every later tool
  round -- Open WebUI rewrites the latest user message for that one request:
  its RAG template (`### Task: Respond to the user query using the
  provided context …`, ~1,450 characters) with the sources filled into
  `<context>…</context>`, then the user's words. `split_exchanges` reads
  user text through `common.conversation.user_text`, which removes that
  block (everything up to the last `</context>`, so a fetched page cannot
  pass text off as the user's), so `regex_text`, the cooldown's judges and
  the classifier's transcript never read Open WebUI's instructions or a
  fetched page as the user's request. Before it, the template's own example
  sentence ("the proposed **method** increases efficiency") matched
  `code_kw` on every fetch round (2026-10-20); a privacy word in a fetched
  page would have forced `local-private` mid-answer. `regex_text`'s
  `searched_chars` is the typed length. A customised `RAG_TEMPLATE` that
  does not start `### Task:` and end `<context>…</context>` is left as
  is.
- **`regex_text` and `tool_calls` can judge a single exchange**, which is what
  `cooldown.relevant_if` uses.
- **`llm_classifier`** reads only what the service put on the request
  (`ctx.analyses`); the slow part happens in the background
  ([§ The llm_classifier](#the-llm_classifier)).
- **`pii`, `nearest_examples`, `prompt_injection`, `language`** read only
  what the service put on the request (`ctx.signals`: the ONE signals call
  their `signal_requests` asked for, [§ Signals](#signals)); `pii` also runs
  `common.pii` in-process. `text_shape` and `context_size` read the request
  alone. None of them stores a span or a character of the text.

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

**`observe`** is in none of the steps: an observe evaluator runs, and is
logged with its would-be route (`evaluations.vote_route`, `abstained =
false` when it "voted"), and no combiner reads it -- no score, no force, no
veto, in either combiner. It is how a signal is measured without routing
anything: `default-signals` observes the injection score, the code shape,
the language and the size. A later version changes what a hit does by
changing the `role` (`force`, `veto`) -- in a shadow first, or inside an
enforced ruleset as an observer. The invariants are unchanged (in both
copies).

**Every evaluator always runs**, even after a force vote, and every vote is
logged: a rule's hit rate is readable in Trino whatever decided the turn.

An evaluator that **raises** is recorded as an abstain with its error, and the
decision goes on. The error is the exception's **type name** only
(`KeyError`; reason `error: KeyError`), in the vote, `evaluations.error`,
`decisions.error` and the log: an evaluator reads the conversation, and a
message can quote it (`common.pii`'s grid lookup once raised `KeyError('ſ')`,
a character of the user's text). The same holds for every exception
route-policy logs or stores around a decision, a shadow, a write or an
analysis. A vote for a route the ruleset does not list is turned into an
abstain with an error.

**The invariants** (`route_policy/engine/invariants.py`) run last:
- `requested_model == local-private` → `local-private`.
- A combined route outside `routes` → `local-private` (or `default_route` if
  the ruleset lacks it), with `apply: false`.

## The `default` ruleset

`rulesets/default.yaml`, version 3, `mode: enforce`, `pin: turn`,
`priority` combiner:

| # | Evaluator | Type | Votes when | Route |
|---|---|---|---|---|
| 1 | `privacy` (`force`) | `regex_text` → `patterns/privacy.yaml` (`privacy_kw`) | PII words in the latest user message | `local-private` |
| 2 | `code-text` | `regex_text` → `patterns/code.yaml` (`code_kw`, regexes) | code words, a file name or a code fence | `local-code` |
| 3 | `reasoning-text` | `regex_text` → `patterns/reasoning.yaml` (`reasoning_kw`) | math / science / multi-step words | `local-reasoning` |
| 4 | `code-tools` | `tool_calls`, current exchange | a sandbox MCP tool (`sandbox-mcp_.+`) or one of Open Terminal's distinctive operationIds was CALLED | `local-code` |
| 5 | `sticky-code` | `cooldown`, 2 exchanges, judges `code-tools`, `code-text` | a coding exchange within the last 2 | `local-code` |
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
- **The cooldown is stateless.** Build a calculator, then ask two weather
  questions: they stay on `local-code`, and the third drops to
  `local-general`. Any coding exchange resets it. Version 2 held three; the
  2026-10 calculator-then-weather chat was the reason to shorten it.
- **One route per turn** (`pin: turn`): the tool rounds of one answer, and
  Open WebUI's hidden auto-continue nudge, keep the route the answer's
  first call got -- so a nudge, which is a new "exchange", cannot expire the
  cooldown halfway through an answer. [§ Turn pinning](#turn-pinning).

**Version history:** 1 (phase 2) ran these rules in `shadow` mode; 2
(phase 3) enforced them with a 3-exchange cooldown; 3 has the 2-exchange
cooldown and `pin: turn`. Routing is otherwise identical.

**The pattern files are the only copy of the rules.** Phase 2 kept them equal
to the semantic router's keyword signals; phase 3 retired the router and its
config, so nothing else holds them. `test_default_ruleset.py` checks that:
- every `patterns/*.yaml` is a valid rule, and every `method: regex` keyword
  stays in the RE2 subset (no lookaround, backreferences, atomic or
  possessive groups, `\Z`). `common.keywordmatch` gives Go RE2's semantics
  (ASCII `\b` / `\w`), but Python's `re` accepts more, so this check is what
  keeps the patterns portable;
- the real "Scientific Calculator" chat prompts match `code_kw` (or don't);
- the wiring and the routes on ~25 conversation shapes are as listed above,
  and so are the routes of a turn's later calls (a tool round, the nudge,
  PII mid-turn) with the turn's first decision on the context.

Changing any of it is a ruleset change: update the expected values and bump
`version` ([§ Rulesets](#rulesets)).

## Turn pinning

A **turn** is one assistant answer: every `/decide` with the same Open
WebUI `chat_id` and `message_id` (the hook reads them from
`X-OpenWebUI-Chat-Id` / `-Message-Id`, patch 0005). One answer makes
several calls: one per tool round, and Open WebUI's patch 0004 adds a
hidden auto-continue **nudge** -- an extra USER message under the same
message id -- when the model announces a tool call it did not make. A
ruleset re-deciding each call fresh could switch chains mid-answer: the
nudge is a new user message, so it is a new "exchange" for `sticky-code`
(that is how a weather answer fell off `local-code` halfway), and its text
is the nudge's, not the user's.

**`pin: turn`** (a ruleset field; `default@3` and `default-llm@2` onward set it):
1. **The first call of a turn is decided exactly as without a pin.**
2. **A later call** (a stored decision of the same ruleset id exists for the
   same chat + message) runs every evaluator as usual -- votes and scores
   are logged fresh -- and then keeps the first decision's route:
   - fresh route = the turn's route: unchanged (reason: the combiner's);
   - fresh route differs: the turn's route, `decided_by` `pin`, reason
     `pinned to this turn's first decision (<route>, <first id>); fresh:
     <fresh route> — <fresh reason>`;
   - **fresh route `local-private`: the fresh route wins.** A privacy force
     (or a request for `local-private`) is never pinned away; reason
     `<fresh reason>; local-private beats this turn's first decision (…)`.
     A turn whose first call was private stays private through a nudge
     that names no PII.
3. **The invariants run again on the pinned route**, so they still have the
   last word (a first decision of another version, whose routes this one
   lacks, becomes the fail-safe with `apply: false`).
4. **Every later call stores `pinned_from`** = the turn's first decision id,
   whichever of the three it was. The first call's `pinned_from` is null.
5. **A later call asks for no classification**: the first call already
   asked for its conversation prefix, and the nudge's prefix (one user
   message more) would be a wasted one.

**The lookup** (`db/repositories/decisions.py::first_of_turn`): the EARLIEST
stored decision of the same **ruleset id** (any version: a turn that spans a
deploy keeps its route) for the same `(chat_id, message_id)`, leaving out
the decision being made and background tasks -- among the primaries for the
primary, among that ruleset's own shadow rows for a shadow. One query on
`decisions_turn_idx`, within `DB_READ_BUDGET_S`, concurrently with the
verdict lookup. No pin (decided afresh, as a first call) when:
- the ruleset has no `pin`;
- `chat_id` or `message_id` is missing -- Claude Code on `/v1/messages`,
  any non-Open-WebUI client;
- it is a background task ([§ Background tasks](#background-tasks));
- the read timed out, failed, or there is no database (counted:
  `route_policy_db_reads_cut_total{read="turn_pin"}`,
  `route_policy_errors_total{kind="turn_pin"}`), or the first decision's
  row was not written yet.

**Still per call, not per turn:** a code tool called in the middle of a
general answer does not move that answer to `local-code` -- the next user
message does (`sticky-code` sees the call). A regenerated answer is a new
message id, so a new turn.

`route_policy_pinned_total{ruleset, outcome}` counts the later calls:
`kept` (the fresh route already was the turn's), `changed` (the pin held a
different fresh route), `private` (a fresh `local-private` beat the pin).

## Background tasks

Open WebUI's **background tasks** -- the chat title, tags, follow-up
suggestions, query generation, emoji -- are ordinary chat completions. With
the Task Model left on `auto` they reach route-policy like a chat turn
(their decisions have no `message_id`), and before this an `llm_classifier`
classified each one: ~100 s of CPU on `qwen3.5-4b` for a prompt that is not
a turn of the conversation.

- **The hook flags them.** `/decide` carries `background_task`, computed by
  the LiteLLM hook with the `auto` footer's own detection
  (`ai/litellm/hooks/footer/render.py::is_openwebui_task`: a `### Task:`
  prompt sent without streaming, a `<chat_history>` block, or legacy
  function calling's `Available Tools:` system message). The hook computes
  it because it needs the request's `stream` flag, which `/decide` does not
  carry.
- **Decided and logged as before**, with `decisions.background_task = true`
  -- so Trino can leave them out of anything about chat turns.
- **Never classified:** an `llm_classifier` names no verdicts to look up,
  enqueues nothing and abstains with `background task`.
- **Never pinned, never pinning:** a background task is not a turn's first
  decision, and is never held to one.

**Recommended:** Open WebUI Admin Settings → Interface → **Task Model =
`local-private`**. Background tasks then never reach `auto` at all, and a
chat's title and follow-ups -- generated from the whole conversation --
never reach a Claude-capable chain either (`auto` sends a task to whatever
chain its words match, and those can overflow to Claude).

## Shadow rulesets

A **shadow** ruleset is bound beside a primary (`bindings.yaml`, `shadow:`):
decided on every request the binding covers, logged, never applied. It is
how a draft is measured on real traffic before anyone promotes it.

- **When:** after `/decide` has answered, in the same background task as the
  primary's analysis requests ([§ API](#post-decide)). The primary's
  response is never touched; a shadow that cannot be loaded (not stored, not
  compilable by this copy, not handed over within `DB_READ_BUDGET_S`) is
  skipped and counted (`shadow_load`), one that raises is counted
  (`shadow_error`, logged by exception type), and neither affects the
  primary or the other shadows: each shadow runs on its own, so a bug in
  `default-llm` never skips `default-signals`.
- **Stored as:** a `decisions` row with `decision_id = uuid5(<primary id>,
  "<ruleset>@<version>")`, `shadow_of` = the primary's id, `mode = shadow`,
  `applied = false`, the same chat / message / user ids and
  `background_task`, and its own `evaluations`. A repeated `/decide` derives
  the same ids, and the first write wins. No foreign key to the primary: its
  own write may still be in flight, like `outcomes`.
- **Pinned on its own:** a `pin: turn` shadow is held to ITS turn's first
  shadow decision (read from its own shadow rows, `pinned_from` = that
  shadow row), never to the primary's route -- so it still measures what
  enforcing it would do.
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
  write, and whatever its evaluators enqueue or fetch -- off the request
  path. `auto` has two: `default-llm` (an analysis request per turn) and
  `default-signals` (one signals call per call, within the same budget an
  enforced ruleset would get; texts the sidecar already scanned are cache
  hits).

## The llm_classifier

`route_policy/evaluators/llm_classifier.py`, with its background half in
`route_policy/analysis/`.

```yaml
- id: topic-llm
  type: llm_classifier
  config:
    model: qwen3.5-4b           # a document-classifier registry id; must be LOCAL (below)
    criteria:                   # route -> the question /assess scores 1-10 (hint: presence), <= 200 chars each
      local-code: "The latest user request asks to build, write, fix, change or explain software: an app, a website, a script or source code. Using a tool such as web search does not count."
      local-reasoning: "The latest user request needs careful multi-step reasoning such as maths, logic, planning or analysis."
      local-private: "The conversation contains personal data about a real person, such as a name with an address, phone number, email or account number."
    otherwise: local-general    # no criterion reaches min_score -> this route; null = abstain
    min_score: 7                # the classifier's PASS line
    history: {exchanges: 1, max_chars: 6000}   # ONLY the latest user message; max_chars <= the model's text_char_budget (12000 for qwen3.5-4b)
    max_age_exchanges: 1        # a verdict counts for the next user message only
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
| `background task` | an Open WebUI background task: nothing is looked up or classified ([§ Background tasks](#background-tasks)) |

`detail`: `model`, `user_messages` (n), `looked_up`, `lookup_error`, and for
the verdict reported -- `input_key`, `exchange_index` (its k), `age_exchanges`
(n − k), `status`, `scores` (route → 1–10).

**Which calls ask for a classification:** after the answer, a call asks for
the prefix ending at its latest user message, unless that verdict is
already stored -- and never a background task, nor (in a `pin: turn`
ruleset) a call that is not its turn's first. So one Open WebUI answer, its
tool rounds and its nudge included, costs at most one classification.

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
   host's verdict is cached per host for 60 s -- an unresolved one for 2 s
   only, so a restarted model is classified again within seconds. An
   unknown or unavailable model is `refused`.
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

`rulesets/default-llm.yaml`, version 3, `mode: shadow`, `pin: turn`, bound
as `auto`'s shadow, `priority` combiner:

| # | Evaluator | Type | Route |
|---|---|---|---|
| 1 | `privacy` (`force`) | `regex_text` → `patterns/privacy.yaml` | `local-private` |
| 2 | `code-text` | `regex_text` → `patterns/code.yaml` | `local-code` |
| 3 | `reasoning-text` | `regex_text` → `patterns/reasoning.yaml` | `local-reasoning` |
| 4 | `code-tools` | `tool_calls`, current exchange | `local-code` |
| 5 | `topic-llm` | `llm_classifier` on `qwen3.5-4b` (config above) | the verdict's |
| — | default | | `local-general` |

Rows 1–4 are `default`'s, config for config and in the same order, so the
two rulesets differ only in the last rule: `default`'s 2-exchange
`sticky-code` cooldown is replaced by `topic-llm`. What it tests: does the
classifier replace the cooldown, and catch what the keyword rules miss?
`topic-llm` decides only a turn no keyword or tool rule claimed.
`unit-tests/route-policy/test_llm_classifier.py` pins this wiring.

**Version history:** 1's code criterion asked whether the request was
"about writing, running, debugging or changing code or software", and
`qwen3.5-4b` scored "check the weather in Utah" 10/10 for `local-code` --
it read the web-search tool call as running software. 2 asks about source
code, a script or a software project and says that using a tool does not
count, and pins the route per turn (from its own shadow rows). The
criteria are in the verdict keys' fingerprint, so version 2 classifies from
scratch instead of reading version 1's verdicts; in Trino, compare the two
by `shadow_ruleset_version`. 3 reads **only the latest user message**
(`history.exchanges: 1`, was 4) -- the latest request is what says where
the conversation is heading, and with 4 exchanges in the window two
weather follow-ups scored 10/10 for code because the calculator work was
still in it; a verdict counts for the next message only
(`max_age_exchanges: 1`, was 2); and the code criterion covers building
an app again (2 scored "Can you make me a calculator app?" 1/10 as "an
application rather than source code").

## Signals

Four evaluator types read the **`signals` sidecar** (`ai/signals`, operator
guide `ai/signals/SIGNALS.md`): `pii` (its NER), `nearest_examples`,
`prompt_injection` and `language`. Small ONNX models on CPU, fast enough to
vote on the CURRENT turn, unlike the `llm_classifier`.

**One call per decision** (`route_policy/signals/`, `engine/pipeline.py`,
`service.py`), the phase-4 prefetch pattern:
1. **Before the pipeline,** each evaluator's `signal_requests(ctx)` says which
   task it will read, over which texts, with which parameters (cheap, pure).
   `pipeline.signal_requests` merges them into ONE request
   (`signals.model.merge`): each text once, by id; equal requests for a task
   become one task over the union of their texts. A request carries each
   task once (its results are keyed by task), so **validation refuses** a
   ruleset in which two evaluators would ask for one task with different
   parameters -- two `nearest_examples` with different example sets or `k`,
   two `prompt_injection` with different `max_tokens`
   (`Evaluator.signal_tasks`); `pii`'s scope is which texts, not a
   parameter, so two `pii` share the task. Should a conflict reach the
   pipeline anyway, the first request is sent and the other evaluator
   abstains (`conflict`, logged once per ruleset version). No request, no
   call: `default@3` makes none.
2. **`Service.load_signals`** sends it through `common.signals.SignalsClient`
   -- inside `load_context`'s `asyncio.gather`, beside the verdict lookup
   and the turn pin -- within `ROUTE_POLICY_SIGNALS_BUDGET_S` (0.1 s), with
   `deadline_ms` = the budget minus 20 ms, so the sidecar answers with what
   finished before route-policy's own wait gives up. The decision id is the
   `request_id` (the sidecar's logs only).
3. **The answer lands on the request view:** `ctx.signals` (task → status,
   model id, per-text features, the text ids sent), or `ctx.signals_error`
   (`timed out` | `failed` | `unavailable` | `not local`), and
   `ctx.signals_ms`. Evaluators read only the context, so the pipeline stays
   a pure function.

**Texts and their ids**, newest first in every task (the order the sidecar
scans in):
- `u<k>`: the user text of the k-th user message
  (`common.conversation.user_texts`; Open WebUI's RAG block stripped, the
  same text `regex_text` reads). An empty one is never sent;
- `t<k>`: the k-th tool result of the conversation
  (`common.conversation.tool_results`, named through its `tool_call_id`),
  sent only when an evaluator's scope selects it: `pii`'s
  `scope.tool_results` regex over the tool name, `prompt_injection`'s
  `current_exchange` (the results after the latest user message).

**The request caps** the sidecar enforces with a 413 (`common.signals`: 20,000
characters per text, 200,000 per request, 64 texts) are applied before
sending: a longer text is cut to its first 20,000 characters (`pii` counts
it as not completely scanned by NER), and, newest first, a text that no
longer fits is left out -- an older, shorter one may still fit. The `pii`
patterns scan everything in-process regardless (up to 200,000 characters
per decision, user texts first).

**Task statuses** (each signal vote's `detail.status`; `pii`'s
`ner_status`): the sidecar's `ok` | `partial` (some texts: the deadline
cut the rest, or a text failed -- the others still count and the failed
one is "not scanned") | `deadline` | `skipped` (queued past the deadline) |
`warming` (the example set is being embedded) | `error` (no text has
features) | `unavailable` (its model is not loaded); or
route-policy's `not_sent` (no text fit the caps), `timed out` / `failed` /
`unavailable` / `not local` (the call itself), `not fetched`, `conflict`,
`model mismatch` (the sidecar answered with another model than the
evaluator's `model` pin; a pin without `@<revision>` accepts any revision).
Only `ok` / `partial` carry features. Every other status is an abstain --
**the sidecar down, slow or wrong never moves a turn**, and never off
`local-private` (the invariants are unchanged).

**Shadows** prefetch their own signals after the answer, with the SAME budget
(so a shadow measures what enforcing it would cost); the texts the sidecar
saw for an earlier call are cache hits. **`POST /evaluate`** waits up to
`SIGNALS_EVALUATE_TIMEOUT_S` (10 s) and sends no deadline, so a draft is
judged on complete signals.

**The startup warm-up.** After seeding, the service sends each bound
ruleset's warm-up (`Evaluator.signal_warmups`: a `nearest` task with the
example set and no text), so the sidecar embeds the set before the first
turn needs it; the answer is `warming` (started) or `ok` (cached). A sidecar
that is down at startup is logged and never stops it; the set is then
embedded on first use, and `topic` abstains (`nearest warming`) until it is.

**Locality and privacy.** The client resolves the sidecar's host first and
sends nothing unless it resolves only to private / loopback / link-local
addresses (`common.net.check_locality`, cached 60 s -- 2 s when the host
did not resolve, so a sidecar that restarts, or starts after route-policy,
is reached again within seconds; the classifier client's rule): a
misconfigured URL can never carry conversation text off
the box. Answers carry statuses, model ids, counts, scores, example ids and
language codes -- never text, never spans -- and that is all `detail`
keeps. An empty `ROUTE_POLICY_SIGNALS_URL` runs no client: the signal
evaluators abstain (`signals unavailable`) and `pii` votes from its
patterns.

**No migration:** a signal evaluator's `detail` carries its `status` and
`signals_ms`, which Trino reads from `evaluations.detail`.

## The `default-signals` draft

`rulesets/default-signals.yaml`, version 1, `mode: shadow`, `pin: turn`,
bound as `auto`'s SECOND shadow (`bindings.yaml`: `shadow: [default-llm,
default-signals]`), `priority` combiner:

| # | Evaluator | Type | Role | Route |
|---|---|---|---|---|
| 1 | `pii` | `pii`: `scope: {user_messages: all, tool_results: '(?i)(?:.*[-_])?(?:phoenix\|trino)[-_].+'}` (Open WebUI's `phoenix_run_query` and Claude Code's prefixed `mcp__claude_ai_Phoenix-MCP__run_query` / `mcp__litellm__trino-run_query`), `force: [email, phone, street_address, gov_id, card_number, bank_account, account_number]`, `with_person: [date_of_birth]`, `min_score: 0.5`, `model: gravitee-io/bert-small-pii-detection` | **force** | `local-private` |
| 2 | `privacy` | `regex_text` → `patterns/privacy.yaml` (as `default`) | force | `local-private` |
| 3 | `injection` | `prompt_injection`: `model: NeuralTrust/prompt-guard-oss-small`, `threshold: 0.9`, `scope: current_message` | **observe** | `local-private` (would-be) |
| 4–7 | `code-text`, `reasoning-text`, `code-tools`, `sticky-code` | as `default@3`, config for config, same order | vote | as `default` |
| 8 | `topic` | `nearest_examples`: `examples_ref: examples/routes.yaml`, `k: 5`, `min_share: 0.8`, `model: intfloat/multilingual-e5-small` | vote | chosen |
| 9 | `code-shape` | `text_shape`: `min_code_ratio: 0.5`, `min_lines: 5` | observe | `local-code` (would-be) |
| 10 | `language` | `language`: `not_languages: [en]` | observe | `local-general` (would-be) |
| 11 | `context-size` | `context_size`: `max_tokens: 100000` | observe | `local-reasoning` (would-be) |
| — | default | | | `local-general` |

- **PII forces first.** The user's rule: sensitive data in ANY user message
  keeps the chat private (`user_messages: all` -- "my SSN is …", then "now
  write a poem" stays on `local-private`), and so does a result of a DATA
  tool: the tools seen in `outcomes.tool_calls` so far are
  `phoenix_run_query` and `phoenix_describe_table`; `trino[-_]…` covers the
  Trino MCP. Web search, fetch, the sandbox, Open Terminal and knowledge
  search results are NOT scanned -- a business's public phone or address is
  not sensitive data. A name forces only with a date of birth in the same
  text; a bare name ("my name is Sam"), a place or a date of birth alone
  never does.
- **`privacy` (the keyword rule) stays a force** for now: it forces on words
  alone ("how do I renew my passport"), which the "sensitive data only" rule
  would not; whether to demote it is decided at promotion, from Trino.
- **`topic` decides only a turn no keyword, tool or cooldown rule claimed**
  (as `topic-llm` does in `default-llm`).
- **The observe rows route nothing**; they put their numbers and would-be
  route on every decision.
- **What it answers** (`shadow_comparisons`): how often `pii` would have
  forced a turn `default` sent to a Claude-capable chain (the gap a name +
  address with no privacy keyword leaves open), and how often `topic`
  disagrees with `local-general`. Queries in
  [§ Decision log and Trino](#decision-log-and-trino).

`unit-tests/route-policy/test_default_signals.py` pins the wiring (rows 2 and
4–7 identical to `default@3`'s), the PII rule on fabricated shapes (every
pattern type forcing with the sidecar timed out; what has no shape forcing
through the NER; the never-force shapes), observe rows never routing, and
that with nothing from the sidecar the draft routes every `default`
conversation shape like `default@3`.

### The example set

`rulesets/examples/routes.yaml` (`routes_v1`): about 30 curated example
prompts each for `local-code`, `local-reasoning` and `local-general` (none
for `local-private`: privacy is `pii`'s and `privacy`'s job, never a
topic's), written for this business -- solar installs, permits, Phoenix /
Trino data questions:

```yaml
name: routes_v1
examples:
  - {id: code-001, route: local-code, text: "my script throws an error when I run it"}
```

- **Versioned with the ruleset:** `examples_ref` is inlined as
  `config.examples` and hashed with the document, so an edited example is a
  new ruleset version.
- **Validated:** ids unique and stable (Trino reads them back from
  `detail.neighbours`; never reuse a removed id), routes among the ruleset's,
  each text 1–300 characters, at most 1,000 examples.
- **PII-free, never real prompts.** The file is stored in route-policy-db and
  federated into Trino. A test runs `common.pii` over every example and fails
  on any finding. The phase-4 gold set (real customer chats) is used only to
  EVALUATE, on the box; a person writes the examples, guided by what it shows
  is misrouted -- never its prompts, never their embeddings.
- **The fingerprint** (SHA-256 over the sorted (id, route, text) and the
  pinned model id, `common.signals.fingerprint_examples`) is the sidecar's
  cache key; a new example set, or another embedding model, embeds afresh.
- **The thresholds** come from the phase-5 measurements (e5-small, kNN-5):
  `min_share` 0.6 voted on 40/43 test prompts at 0.975 accuracy, 0.8 on 29/43
  at 1.00; noise ("ok", "?") has a share around 0.4 and abstains.

### The jailbreak action is config

`injection` is `role: observe` with a would-be `vote.route: local-private`:
its score is in `evaluations.detail.score` on every decision, and it routes
nothing. Changing what a hit does is a new ruleset version with another
`role` (`force` → `local-private`, or `veto` on a chain) or `threshold`,
chosen from the score histogram ([§ Decision log and Trino](#decision-log-and-trino)).
Refusing a request is not an engine action: route-policy can only pick a
chain. On a long message the injection model is the slowest
(~340 ms at 2,000 characters on the box), so within the 0.1 s budget it is
often `deadline` / `skipped` -- harmless while it only observes.

## Decision log and Trino

`route-policy-db`, schema from `route_policy/db/migrations/0001_initial.sql`,
`0002_llm_classifier.sql` and `0003_turn_pins.sql`:

| Table | One row per | Key columns |
|---|---|---|
| `rulesets` | ruleset version | `id`, `version` (PK), `document` (jsonb, resolved), `content_sha256`, `source`, `created_at`, `created_by`, `active_from`, `active_to` |
| `bindings` | scope → version | `id`, `scope_model`, `scope_key_alias` (null = every key; unique with NULLS NOT DISTINCT), `ruleset_id`, `version` (FK), `updated_at`, `updated_by` |
| `binding_shadows` | shadow slot | `binding_id` (FK, cascade), `position`, `ruleset_id`, `version` (FK) |
| `decisions` | `/decide`, and each shadow ruleset beside it | `decision_id` (uuid PK), `created_at`, `chat_id`, `message_id`, `litellm_call_id`, `user_id`, `key_alias`, `requested_model`, `chosen_route`, `applied`, `mode`, `ruleset_id`, `ruleset_version` (FK), `reason`, `scores`, `backend_snapshot`, `message_count`, `tool_count`, `latency_ms`, `error`, `response`, `shadow_of` (the primary's id; null for a primary; **no FK**), `pinned_from` (0003: the turn's first decision, on a later call of a `pin: turn` turn; null otherwise; **no FK**), `background_task` (0003: the hook's flag; `false` for older rows) |
| `evaluations` | evaluator × decision | `decision_id` (FK, cascade), `position`, `evaluator_id`, `type`, `role`, `vote_route`, `weight`, `confidence`, `reason`, `abstained`, `latency_ms`, `error`, `detail` |
| `outcomes` | `/observe` | `decision_id` (uuid PK, **no FK**), `created_at`, `litellm_call_id`, `response_id`, `served_model_group`, `served_deployment`, `overflow_reason`, `finish_reason`, `tool_calls` (jsonb), `status`, `error` |
| `analyses` | classified conversation prefix | `input_key` (PK: sha256 of fingerprint + transcript), `created_at`, `fingerprint`, `model_id`; who asked first: `ruleset_id`, `ruleset_version`, `evaluator_id`, `chat_id`, `message_id`, `decision_id`; `exchange_index` (k), `input_chars`; the verdict: `route`, `confidence`, `scores` (jsonb route → 1–10), `reason` (the best-scoring criterion's, ≤ 200, CHECKed), `status`, `error`; the cost: `classifier_job_id`, `calls`, `prompt_tokens`, `completion_tokens`, `model_seconds`, `queue_ms`, `latency_ms`. **No transcript** |
| `decision_outcomes` | view | every PRIMARY decision FULL JOIN its outcome, plus `agree` = `chosen_route = served_model_group` (0001's columns; 0002 recreated it over `shadow_of IS NULL`; 0003 appended `pinned_from`, `background_task`) |
| `shadow_comparisons` | view | every shadow decision beside its primary and the turn's outcome: `shadow_decision_id`, `primary_decision_id`, both `*_decided_at`, `observed_at`, `chat_id`, `message_id`, `user_id`, `requested_model`, `shadow_ruleset_id` / `_version`, `primary_ruleset_id` / `_version`, `shadow_route`, `primary_route`, `primary_applied`, `served_model_group`, `served_deployment`, `shadow_reason`, `primary_reason`, `shadow_error`, `agree_with_primary`, `agree_with_served`; 0003 appended `shadow_pinned_from`, `primary_pinned_from`, `background_task` |
| `schema_migrations` | applied migration | `version`, `name`, `sha256`, `applied_at` |

- **Indexes:**
  - `decisions(chat_id, created_at)`, `decisions(created_at)`,
    `decisions(litellm_call_id)`;
  - `outcomes(created_at)`, `outcomes(response_id)`,
    `outcomes(litellm_call_id)`;
  - `evaluations(evaluator_id)`;
  - `decisions(shadow_of)`; `analyses(created_at)`, `analyses(chat_id, created_at)`,
    `analyses(decision_id)`;
  - `decisions(chat_id, message_id, ruleset_id, created_at)`
    (`decisions_turn_idx`, 0003: the turn-pin lookup).
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

**One row per chat turn** -- each answer's FIRST decision, background tasks
left out (rows from before 0003 have no pins, so there every call of an
answer shows):

```sql
SELECT created_at, chat_id, message_id, ruleset_version, chosen_route, reason
FROM postgres_route_policy.public.decisions
WHERE shadow_of IS NULL AND pinned_from IS NULL AND NOT background_task
  AND message_id IS NOT NULL
ORDER BY created_at DESC
LIMIT 100;
```

**What the turn pin did** -- the later calls of each turn, by what the pin
did to their fresh route (the same split as `route_policy_pinned_total`):

```sql
SELECT ruleset_id, ruleset_version,
       CASE WHEN reason LIKE 'pinned to this turn''s first decision%' THEN 'changed'
            WHEN reason LIKE '%local-private beats this turn''s first decision%' THEN 'private'
            ELSE 'kept' END AS pin,
       count(*) AS calls
FROM postgres_route_policy.public.decisions
WHERE pinned_from IS NOT NULL
GROUP BY 1, 2, 3
ORDER BY calls DESC;
```

Each `changed` row's reason names the fresh route the pin overrode (`fresh:
local-general — …`); join `pinned_from` back to `decisions` for the turn's
first call.

**Background tasks reaching `auto`** -- they should drop to ~0 once Open
WebUI's Task Model is `local-private` ([§ Background tasks](#background-tasks)):

```sql
SELECT date_trunc('day', created_at) AS day, background_task, count(*)
FROM postgres_route_policy.public.decisions
WHERE shadow_of IS NULL AND requested_model = 'auto'
GROUP BY 1, 2
ORDER BY 1 DESC, 2;
```

**Shadow agreement** -- the promotion measure, by shadow ruleset: how often
the draft chose what the live ruleset chose, and the chain that served,
over chat turns only:

```sql
SELECT shadow_ruleset_id, shadow_ruleset_version, primary_route, shadow_route,
       count(*) AS turns,
       count_if(agree_with_served) AS agree_served
FROM postgres_route_policy.public.shadow_comparisons
WHERE shadow_decided_at > current_timestamp - INTERVAL '7' DAY
  AND NOT background_task
GROUP BY 1, 2, 3, 4
ORDER BY turns DESC;
```

**The classifier's vote against the rules', per turn** -- where `topic-llm`
decided the shadow, what `default`'s `sticky-code` said for the same turn
(each answer's first call):

```sql
SELECT e.vote_route AS classifier, p.vote_route AS sticky_code, s.primary_route, count(*)
FROM postgres_route_policy.public.shadow_comparisons s
JOIN postgres_route_policy.public.evaluations e
  ON e.decision_id = s.shadow_decision_id AND e.evaluator_id = 'topic-llm'
JOIN postgres_route_policy.public.evaluations p
  ON p.decision_id = s.primary_decision_id AND p.evaluator_id = 'sticky-code'
WHERE NOT s.background_task AND s.primary_pinned_from IS NULL
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

**The gap `pii` closes** -- turns `default` sent to a Claude-capable chain
that `default-signals`' `pii` would have forced to `local-private`, by what
it found and where:

```sql
SELECT s.primary_route, json_format(json_extract(e.detail, '$.forced_by')) AS forced_by,
       json_format(json_extract(e.detail, '$.forced_in')) AS forced_in, count(*) AS turns
FROM postgres_route_policy.public.shadow_comparisons s
JOIN postgres_route_policy.public.evaluations e
  ON e.decision_id = s.shadow_decision_id AND e.evaluator_id = 'pii'
WHERE s.shadow_ruleset_id = 'default-signals' AND NOT e.abstained
  AND s.primary_route <> 'local-private' AND NOT s.background_task
GROUP BY 1, 2, 3
ORDER BY turns DESC;
```

`forced_in` says whether it was the latest user message, an earlier one (the
"stays private" rule) or a Phoenix / Trino result.

**The injection score histogram** -- what a `threshold` would catch, before a
later version changes the role:

```sql
SELECT round(CAST(json_extract_scalar(detail, '$.score') AS double), 1) AS score, count(*)
FROM postgres_route_policy.public.evaluations
WHERE evaluator_id = 'injection' AND json_extract_scalar(detail, '$.status') = 'ok'
GROUP BY 1
ORDER BY 1;
```

**The topic against `local-general`** -- what `topic` chose on the turns no
rule claimed (`decided_by` is the topic when the shadow's reason starts
`topic:`):

```sql
SELECT s.primary_route, s.shadow_route, count(*) AS turns
FROM postgres_route_policy.public.shadow_comparisons s
WHERE s.shadow_ruleset_id = 'default-signals' AND s.shadow_reason LIKE 'topic:%'
  AND NOT s.background_task AND s.shadow_pinned_from IS NULL
GROUP BY 1, 2
ORDER BY turns DESC;
```

**Signal cost and coverage** -- each signal evaluator's task status and
`signals_ms` (the promotion bar: `ok` on ≥ 95 % of a turn's first calls):

```sql
SELECT e.evaluator_id,
       coalesce(json_extract_scalar(e.detail, '$.status'), json_extract_scalar(e.detail, '$.ner_status')) AS status,
       count(*) AS n,
       approx_percentile(CAST(json_extract_scalar(e.detail, '$.signals_ms') AS double), 0.95) AS signals_p95_ms
FROM postgres_route_policy.public.evaluations e
JOIN postgres_route_policy.public.decisions d USING (decision_id)
WHERE d.ruleset_id = 'default-signals' AND d.pinned_from IS NULL
  AND e.evaluator_id IN ('pii', 'topic', 'injection', 'language')
GROUP BY 1, 2
ORDER BY 1, 3 DESC;
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
- reasons, which name patterns, tools, entity types and scores, never
  content; the signal evaluators' `detail` holds types, counts, scores,
  example ids, statuses -- never a span.

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

## Promoting the signals

A later, separate change (ROUTE_POLICY_PHASE5_PLAN § 4.4). Promote when, over
at least two weeks of shadow and on the gold set:
- **PII:** the `pii` force fires on at least the gold `local-private` points
  `privacy_kw` catches, and on at most 2 % of gold non-private points;
- **Topic:** `topic` does not lower gold accuracy against `default@3`;
- **Cost:** the `pii` and `topic` tasks are `ok` on at least 95 % of a turn's
  first calls, and `/decide` p95 stays below 150 ms.

Then:
1. A **new `default` version** that adds `pii` (force), `topic` (vote) and the
   observe rows -- so the injection score lands on every primary decision --
   in the places `default-signals` tested.
2. Update `test_default_ruleset.py`'s wiring and routes.
3. Decide `privacy_kw`'s role from the data (demote it to `observe` if `pii`
   covers its real hits).
4. Drop `default-signals` from `bindings.yaml`'s `shadow:`.
5. `make build route-policy && make up route-policy`. The primary then makes
   the signals call on the request path, within the same 0.1 s budget.

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
| `route_policy_db_reads_cut_total` | `read` (binding_refresh / ruleset_fetch / analysis_lookup / turn_pin) | Reads on the `/decide` path that outlasted `DB_READ_BUDGET_S` |
| `route_policy_pinned_total` | `ruleset` (`<id>@<version>`), `outcome` (kept / changed / private) | Later calls of a `pin: turn` turn: the fresh route already was the turn's, the pin held a different one, or a fresh `local-private` beat the pin ([§ Turn pinning](#turn-pinning)); shadows included |
| `route_policy_shadow_decisions_total` | `ruleset` (`<id>@<version>`), `agree` (true / false: chose the primary's route) | Shadow decisions stored |
| `route_policy_analyses_total` | `model`, `status` (ok / incomplete / unavailable / refused / failed / timeout / not_local) | Classifications finished |
| `route_policy_analysis_seconds` | `model` | One classification's cycle, locality check to result |
| `route_policy_analysis_queue_depth` | — | Requests waiting for a worker |
| `route_policy_analysis_dropped_total` | `reason` (queue_full / shutdown) | Requests not classified |
| `route_policy_signals_seconds` | `kind` (primary / shadow / evaluate) | One signals call, as route-policy waited for it |
| `route_policy_signals_total` | `outcome` (ok / timeout / error / unavailable / not_local) | Signals calls; `unavailable` also counts a call that would have been made with no client (an empty `ROUTE_POLICY_SIGNALS_URL`) |
| `route_policy_signal_tasks_total` | `task` (pii / nearest / injection / language), `status` (the sidecar's, or missing / not_sent) | Each task of an answered call |
| `route_policy_errors_total` | `kind` (decision_write, decision_write_dropped, outcome_write, outcome_write_dropped, binding_refresh, ruleset_compile, unbound_model, shadow_load, shadow_dropped, shadow_error, analysis_lookup, turn_pin, analysis_write, classifier_delete, signals) | Errors |

Shadow decisions also count in `route_policy_decisions_total` (with
`mode="shadow"`), `route_policy_votes_total` and
`route_policy_evaluator_seconds`. Plus the HTTP request metrics of
prometheus-fastapi-instrumentator. Each `/decide` logs one INFO line:
`[decide] <id> auto -> local-code (code-tools: called … ; default@3 enforce) chat=<id>`
(` task` appended for a background task; a pinned call's reason starts
`pinned to this turn's first decision …`),
each shadow one `[shadow] <id> default-llm@3 -> … beside <primary id> -> …`,
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
| `ROUTE_POLICY_SIGNALS_URL` | `http://signals:8000` | The signals sidecar, by Docker DNS on `ai_shared`, no auth. Only a host that resolves to this box's networks is ever sent text. Empty (only possible outside the compose file) runs no client: every signal evaluator abstains (`signals unavailable`), `pii` votes from its patterns |
| `ROUTE_POLICY_SIGNALS_BUDGET_S` | `0.1` | How long a decision (primary or shadow) waits for its signals call (floor 0.04: the margin + 20 ms, so the sidecar always gets at least 20 ms -- at a floor of 0.02 it was told `deadline_ms: 1` and skipped every task); the sidecar is told 20 ms less. It runs concurrently with the database reads, so at or below `DB_READ_BUDGET_S` it adds nothing to the worst case |

Constants in `route_policy/config.py`, not environment variables:
`WRITES_MAX_IN_FLIGHT` (256 background writes and after-the-answer tasks),
`WRITES_DRAIN_S` (5 s at shutdown, for the writes and again for running
classifications), `DB_READ_BUDGET_S` (0.1 s for the binding refresh, a
version fetch, a verdict lookup and the turn-pin read on the `/decide`
path),
`CLASSIFIER_MODELS_TTL_S` (60 s cache of `GET /models` and of each model
host's locality verdict), `SIGNALS_EVALUATE_TIMEOUT_S` (10 s: `/evaluate`'s
signals call, no deadline cut), `SIGNALS_DEADLINE_MARGIN_S` (20 ms: under load the sidecar answers a few ms past its deadline, and a 10 ms margin lost a 2,000-character turn's partial PII result),
`SIGNALS_WARM_TIMEOUT_S` (5 s per startup warm-up), `SIGNALS_MODELS_TTL_S`
(60 s cache of the sidecar host's locality verdict).

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
    comes from the request and from rows any copy reads alike (stored
    verdicts, a turn's first decision), startup takes advisory locks, and
    writes are idempotent.
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
- **`route_policy_signals_total` not `ok`:** routing is unaffected (only the
  `default-signals` shadow calls the sidecar); its signal votes abstain.
  - `not_local`: `ROUTE_POLICY_SIGNALS_URL`'s host did not resolve to a
    private address from route-policy -- is the sidecar running (`make ps
    signals`)? Docker DNS answers only for running containers (an
    unresolved host is retried after 2 s, a public one after 60 s);
  - `unavailable`: the sidecar is down, still loading (its `/health` is 503
    until its models are warm) or answering 5xx; or the URL is empty;
  - `timeout`: the sidecar did not answer within the budget -- check its
    CPU (`SIGNALS_CPUSET`, `ai/signals/SIGNALS.md`) before raising
    `ROUTE_POLICY_SIGNALS_BUDGET_S`;
  - `error`: a 4xx (413 / 422) or a malformed answer -- the two images'
    `common.signals` disagree; rebuild both.
- **`topic` abstaining with `nearest warming`** for more than a few seconds
  after a deploy: the startup warm-up did not reach the sidecar (look for
  `startup: signals warm-up` in the log); the first real request starts the
  embedding instead.
- **`route_policy_analysis_dropped_total{reason="queue_full"}` climbing:**
  classifications are slower than turns arrive. Those turns get no verdict;
  routing is unaffected (the shadow's `topic-llm` abstains more).

## Code layout

```
ai/route-policy/
  docker-compose.route-policy.yml  Dockerfile  pyproject.toml  ROUTE_POLICY.md  route-policy.postman_collection.json
  db-init/10-trino-reader.sh       the read-only Trino role (first start of an empty volume)
  rulesets/default.yaml  rulesets/default-llm.yaml  rulesets/default-signals.yaml  rulesets/bindings.yaml
  rulesets/patterns/{privacy,code,reasoning}.yaml
  rulesets/examples/routes.yaml    the curated example prompts (nearest_examples)
  bench/                the classifier benchmark (bench/BENCH.md), shipped in the image
  route_policy/
    app.py              FastAPI factory + lifespan (pool, migrations, seeding, analysis queue, signals
                        client + warm-up)
    config.py           infrastructure settings only
    metrics.py          Prometheus metrics
    service.py          the shared objects; storing decisions and outcomes; verdict lookups; the
                        signals call; the after-the-answer task (shadows, analysis requests)
    api/                decide, observe, evaluate, rulesets, chats, evaluator_types, health, schemas
    engine/             context (the request view), pipeline (evaluators → combiner → invariants), invariants
    evaluators/         base (contract, @declares, registry), regex_text, tool_calls, cooldown, backend_load,
                        static, llm_classifier, pii, nearest_examples, prompt_injection, language,
                        text_shape, context_size; _signals (reading a task off ctx.signals)
    combiners/          base (force / veto / default frame), priority, weighted
    rulesets/           model (document, compiled form, hash), loader (files, refs, bindings), validate, store
    analysis/           model (AnalysisRequest, Verdict), verdict (choose), classifier_client (the /assess
                        cycle), queue (AnalysisQueue)
    signals/            model (SignalText, SignalRequest, merge -> SignalBatch, SignalTask; the caps),
                        wire (the adapter to common.signals: build_request, apply)
    db/                 pool, migrations/ (0001_initial.sql, 0002_llm_classifier.sql, 0003_turn_pins.sql
                        + runner), repositories/
```

Reused from `shared/common`:
- `common.keywordmatch`: Go-RE2 keyword rules.
- `common.conversation`: exchanges, called tools, `user_message_count`,
  `render_transcript`; `user_texts` / `tool_results` (the signal texts).
- `common.net`: `DEFAULT_BLOCKED_NETWORKS`, the locality guard's networks.
- `common.pii`: the in-process PII patterns (`counts`, `ENTITY_TYPES`).
- `common.signals`: the sidecar's wire format, caps, example fingerprint and
  `SignalsClient` (locality guard, budget, never raises).

## Tests

`unit-tests/route-policy/`, in its own pytest process:

```bash
UV_LINK_MODE=copy uv run --no-sync python -m pytest unit-tests/route-policy -q -p no:cacheprovider
TEST_POSTGRES_DSN=postgresql://postgres@localhost:55441/postgres \
  UV_LINK_MODE=copy uv run --no-sync python -m pytest unit-tests/route-policy -q -p no:cacheprovider
```

| Module | Covers |
|---|---|
| `test_evaluators.py` | Each evaluator. The cooldown's verdicts and reasons are pinned on a table of histories (0 to 3 exchanges; the ruleset's 2 spelled out). |
| `test_declarations.py` | Every real evaluator's `detail` against its declaration (the `llm_classifier`'s background-task abstain included; the six phase-5 types on a fake sidecar's answers) |
| `test_signal_evaluators.py` | The phase-5 types: `pii` (patterns and NER, sticky over every user message, data-tool results yes and web search no, name + DOB only together, a bare name never, still forcing from patterns with the sidecar timed out / failed / unavailable / not local / cut / on another model, NER coverage, the pattern cap, config validation), `nearest_examples` (shares, `min_share`, `min_similarity`, warming / skipped / error / model mismatch, the fingerprint, example-set validation), `prompt_injection` (threshold, the current exchange's tool results), `language`, `text_shape`, `context_size` |
| `test_signals.py` | The prefetch: `merge` (dedupe, newest first, conflicts, the caps), the text ids, ONE call per `/decide` through the real `SignalsClient` on a fake sidecar (none for `default`), the budget cut leaving `/decide` in time with `pii` forcing from patterns, the primary unchanged by a shadow's signals, shadows prefetching on their own with the same budget, `/evaluate` waiting, every failure an abstain (no client, not local -- nothing sent --, down, 5xx, 413), the warm-up, the metrics, no request text in any log line or stored row |
| `test_default_signals.py` | The `default-signals` draft: wiring (rows shared with `default@3` verbatim), the binding's second shadow slot, the example set valid and PII-free (`common.pii` over every example), the user's PII rule on fabricated shapes, observe rows never routing, the topic deciding only unclaimed turns, one batched request, and routing like `default@3` on every shape when the sidecar adds nothing |
| `test_combiners.py`, `test_invariants.py`, `test_pipeline.py` | The frame, both combiners (`observe` ignored by both, still logged with its route), the invariants, the contract shapes, error handling; the turn pin as a pure function (the contract unchanged, every vote kept, privacy and a requested `local-private` winning, the invariants on the pinned route, no pin without `pin: turn`, no analysis requests from a later call) |
| `test_rulesets.py` | Files, refs (`patterns_ref` and `examples_ref`; no path escape), validation (`pin` and example sets included), content hash (an example edit is content of the ruleset that references it), seed bindings (two shadow slots), the cache |
| `test_default_ruleset.py` | The pattern files (valid rules, every regex in the RE2 subset, the real-chat prompts `code_kw` must and must not match), the ruleset's wiring, its routes on ~25 conversation shapes, and the routes of a turn's later calls (tool rounds, the nudge, PII mid-turn) |
| `test_turns.py` | Turn pinning and background tasks through `/decide` with the tables in memory: one route for a turn's calls, `pinned_from`, a shadow pinned from its own rows, one classification per turn, privacy beating the pin, no lookup without a message id, a cut / failed lookup decided afresh in time, a background task logged / never classified / never pinning, the metrics |
| `test_api.py` | Every endpoint with no database: contract, validation, decisions still answered with the DB down |
| `test_writes.py` | No database wait holds `/decide`: deferred and dropped writes, shutdown, bounded reads |
| `test_llm_classifier.py` | The evaluator: config validation, route checks, fingerprint, verdict keys (stable across tool rounds, changed by an edit), ageing, every vote and abstain reason, analysis requests, background tasks, the tie order surviving a jsonb round trip, the `default-llm` wiring (version, pin, the reworded code criterion) |
| `test_verdict.py` | `choose`: winners, ties, `otherwise` confidence, errored criteria |
| `test_analysis_client.py` | The `/assess` cycle on a fake classifier: every status, the locality guard (authenticated, public, mixed and unresolvable hosts refused before submitting; private / loopback / ULA local), caches, DELETE always (after a cancel on timeout, and when cancelled), no submitted text in any error |
| `test_analysis_queue.py` | Dedupe, overflow, skip-if-stored, the written row (scores by route, winner's reason, usage, provenance; failures with no scores), write failures, shutdown (waiting dropped, running cancelled and still deleted) |
| `test_shadows.py` | Resolving shadows, skipped unloadable ones, derived ids, the primary response unchanged by a shadow, the shadow row and its evaluations, idempotency, provenance of analysis requests, a primary with an `llm_classifier`, isolation (a raising shadow, a hung database, the task cap), `/evaluate` reading without enqueueing |
| `test_db.py` (`postgres`) | Startup, seeding (with shadow slots), immutability, migrations (0003 on a phase-4 database with rows), the phase-2 → phase-3 and phase-4 → `default@3` upgrades (a turn pinned across the deploy), idempotent `/decide` / `/observe` in either order, shadow rows and both views (the 0003 columns), the `analyses` table (first write wins, caps, the CHECK), the whole classifier loop on a fake classifier (job deleted, verdict voted next turn, no text stored), one classification per turn, the turn lookup on real rows (each shadow pinned from its own rows), the startup warm-up and a shadow's signals call on a fake sidecar, an edit making a verdict unreachable, a stored draft compiling like its file, the reads |
| `test_wiring.py` | Postman coverage (a `default-signals` dry run included), compose classes and env (the signals settings), Trino env, pass-through, Makefile, Dockerfile (bench shipped and imported), line endings |
| `bench/` | The benchmark's pure parts ([`bench/BENCH.md`](bench/BENCH.md)) |

- **The `postgres` tests** need `TEST_POSTGRES_DSN`: an admin connection on
  which each session creates and drops its own `route_policy_test_*`
  database. Without it they skip.
- **For a scratch server on Windows,** use the EDB binaries zip:
  `initdb -U postgres --auth=trust`, then `pg_ctl -o "-p 55441" start`.
