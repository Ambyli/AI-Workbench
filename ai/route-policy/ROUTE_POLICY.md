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

**Phase 2 runs in shadow mode.** Every ruleset is `mode: shadow`:
- LiteLLM's hook (`ai/litellm/hooks/routing/policy_client.py`) calls
  `/decide` and `/observe` without waiting for them;
- nothing is applied;
- the semantic router plus the hook's code remap still route every turn.

The point of phase 2 is the comparison in Trino: does the engine choose what
today's routing chose ([§ Decision log](#decision-log-and-trino))? Phase 3
flips the ruleset to `enforce`, and the engine replaces the router.

Plan: `ROUTE_POLICY_PLAN.md` (outside the repo), phases 2–5.

| | |
|---|---|
| Compose file | [`docker-compose.route-policy.yml`](docker-compose.route-policy.yml) |
| Services | `route-policy` (FastAPI, swap-class), `route-policy-db` (Postgres 17, recreate-class) |
| Inside ai_shared | `http://route-policy:8000` (what LiteLLM uses), `route-policy-db:5432` |
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
- [Decision log and Trino](#decision-log-and-trino)
- [Metrics](#metrics)
- [Configuration](#configuration)
- [Operations](#operations)
- [Code layout](#code-layout)
- [Tests](#tests)

## How it fits

```
Open WebUI ─► LiteLLM (model `auto`)
                ├─ pre-call hook ── POST /decide {decision_id, messages, tools, ids, backends} ──► route-policy
                │                   (fired, not awaited)                                          ├─ ruleset (bound to `auto`)
                ├─ (still) Envoy ─► semantic router ─► chain alias ─► LiteLLM ─► vLLM / Claude     ├─ evaluators → votes
                └─ success / failure hook ── POST /observe {decision_id, served chain, ...} ──────► ├─ combiner → route
                                                                                                   └─ invariants
                                                                         route-policy-db ◄────────────┘
                                                                              │ trino_reader
                                                                         Trino: postgres_route_policy
```

The responsibilities are split:
- **The engine decides intent:** which chain, from the conversation.
- **LiteLLM's overflow layer decides capacity:** which deployment inside that
  chain.

The hook sends the overflow layer's backend snapshot in every `/decide`
(`backends`), so a ruleset can be load-aware (`backend_load`). The engine
still never picks a deployment.

**The hook makes the `decision_id`** (uuid4) and sends both calls with it. The
two calls may arrive in either order, so `outcomes` has no foreign key to
`decisions`. The `decision_outcomes` view joins them.

## Deploying

Steps, in order (on the box, from the repo root):

1. **`.env`:**
   - Copy the `## Route policy` block and the two `PORT_ROUTE_POLICY*` rows
     from `.env.example`.
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
   - The edge compose file gained the `8027` port, so this is a recreate: a
     second or two of LAN downtime, and `auto` turns fall back to
     `local-private` meanwhile.
   - It must come first. A swap of a fronted service refuses to go ahead when
     the running edge has no backend for it.
4. **`make build route-policy && make up route-policy`.**
   - `route-policy-db` is recreated (it is new); `route-policy` swaps.
   - The image build itself validates the baked-in rulesets.
5. **`make up trino trino-coordinator`.** A recreate, because its environment
   gained `ROUTE_POLICY_DB_NAME` / `ROUTE_POLICY_TRINO_READER_PASSWORD` and
   the new catalog file.
6. **`make up litellm`.** Picks up the `/v1/route-policy` pass-through in
   `litellm_config.yaml` (and workstream C's hook).
7. **`docker restart prometheus`.** Its config is bind-mounted, and it gained
   the `route-policy` scrape job.

**Checks after the deploy:**

```bash
make logs route-policy route-policy FOLLOW=0 TAIL=50   # "applied migration 0001_initial.sql",
                                                      # "seeded default@1", "binding auto -> default@1"
curl -s localhost:8027/health                         # {"status":"ok","rulesets":[{"id":"default","version":1}],"bindings":1}
curl -s localhost:8027/ready                          # {"status":"ready","db":"up"}
curl -s localhost:8027/bindings
curl -s -X POST localhost:8027/evaluate -H 'content-type: application/json' \
  -d '{"ruleset":{"id":"default"},"messages":[{"role":"user","content":"write a python script"}]}' | jq .route
                                                      # "local-code"
docker exec route-policy-db psql -U route_policy -c '\dt'
docker exec route-policy-db psql -U route_policy -c '\du trino_reader'
```

- **Once LiteLLM's hook is live:** chat once through `auto` in Open WebUI, then
  `docker exec route-policy-db psql -U route_policy -c 'select * from
  decision_outcomes order by decided_at desc limit 5'`. Every turn should
  have one row with both sides.
- **In Trino:** `SHOW TABLES FROM postgres_route_policy.public`.
- **In Prometheus:** the `route-policy` target is UP.
- **On the edge status page** (`chat.zeoenergy.com/edge/`): `route-policy` is
  listed.

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
| GET | `/bindings` | Requested model (+ key alias) → ruleset version |
| GET | `/chats/{chat_id}/decisions` | One conversation's decisions with their outcomes |
| GET | `/evaluator-types` | Each evaluator type's config schema and declared output; the combiners |
| GET | `/health`, `/ready` | Liveness (no database); readiness (a live `SELECT 1`) |
| GET | `/metrics` | Prometheus |

The ruleset **write** API (create a version, activate it, edit bindings) is
phase 5.

### POST /decide

This is the contract workstream C's client is built against
(ROUTE_POLICY_PLAN § 7, "Phase 2 build spec").

```json
{"decision_id": "uuid4, made by the hook", "requested_model": "auto",
 "litellm_call_id": "…", "chat_id": "… | null", "message_id": "… | null", "user_id": "… | null", "key_alias": "… | null",
 "messages": [/* the OpenAI messages as LiteLLM received them */], "tools": [/* or null */],
 "backends": {"<api_base>": {"verdict": "ok|busy|down|stale|unknown", "waiting": 0.0, "running": 0.0}}}
```

→ `200`, and the same body is what gets logged:

```json
{"decision_id": "…", "route": "local-code", "mode": "shadow", "apply": false,
 "ruleset": {"id": "default", "version": 1},
 "votes": [{"evaluator": "code-tools", "type": "tool_calls", "route": "local-code", "weight": 1.0,
            "confidence": 1.0, "role": "vote", "abstained": false,
            "reason": "called sandbox-mcp_exec in this exchange", "latency_ms": 0.1}],
 "scores": {"local-code": 1.0}, "latency_ms": 2.3}
```

- **`votes`:** one per evaluator, in ruleset order, abstains included. An
  abstain has `route` and `confidence` null and a reason.
- **`scores`:** Σ weight × confidence per route, over the non-abstaining
  `vote` and `force` votes.
- **`apply`:** true only for an `enforce` ruleset whose invariants held. It is
  never true in phase 2.
- **Idempotent on `decision_id`.** A repeat returns the **first** stored
  response, whatever the new body says. Nothing is written twice.
- **Errors:**
  - `404`: no ruleset is bound to `requested_model` (+ `key_alias`).
  - `422`: the body breaks the contract (e.g. `decision_id` is not a UUID).
  - `503`: the bound ruleset cannot be compiled by this copy (only possible
    mid-swap after a ruleset gained an evaluator type the old code lacks).
- **Lenient where strictness would only lose data.** Calls are fire-and-forget,
  so a `422` is invisible to everyone:
  - unknown fields are ignored;
  - a null `messages` is an empty list;
  - an unknown `verdict` is kept as sent.
- **A decision is returned even when route-policy-db is down.** It is then
  not logged; that is counted in `route_policy_errors_total{kind="decision_write"}`.
  Routing never depends on the log.

### POST /observe

```json
{"decision_id": "…", "litellm_call_id": "…", "response_id": "chatcmpl-… | null",
 "served_model_group": "local-code | null", "served_deployment": "openai/muse-glimmer | null",
 "overflow_reason": "busy|down|null", "finish_reason": "stop|tool_calls|length|null",
 "tool_calls": ["sandbox-mcp_exec"], "status": "success|failure", "error": "… | null"}
```

- **`202`:** `{"decision_id": "…", "stored": true}`, or `stored: false` when
  this decision already had an outcome (the first write wins).
- **`503`:** the database is unavailable.
- **`error`** is cut to 2000 characters.

### POST /evaluate

Give exactly one of:
- `{"ruleset": {"id": "default", "version": 1}}` (version omitted = the
  highest);
- `{"document": {…}}`.

Plus `messages` / `tools` / `backends` and an optional `requested_model`
(default `auto`).

It returns the `/decide` shape with `decision_id: null`. Each vote also carries
its evaluator's `detail` and `error`.

- **A draft document** may use `patterns_ref` refs. They resolve inside the
  baked `rulesets/` directory; a path that leaves it is a `422`.
- **A stored document** from `GET /rulesets/{id}/{version}` posts back as it
  is.
- **An invalid document** is a `422` whose `detail.errors` lists **every**
  problem.

## Rulesets

```yaml
id: default                      # [a-z0-9][a-z0-9_-]*
version: 1                       # immutable: new content = a new version
description: …
mode: shadow                     # shadow | enforce
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
- `*.yaml` at the top level: one ruleset each.
- `bindings.yaml`: the seed bindings.
- `patterns/*.yaml`: keyword rules, referenced by `patterns_ref`.

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
2. **Each seed binding** is pointed at its version. `version` omitted means
   the highest version baked in, so bumping `default.yaml`'s version
   re-points `auto` on the next deploy.
3. **`active_from` / `active_to`** are refreshed:
   - `active_from` is the first time a binding pointed at a version;
   - `active_to` is when none does any more.

In phase 2, startup owns the seeded bindings and resets them on every start.

**Changing a ruleset:**
1. Edit the YAML.
2. Bump `version`.
3. Try it with `POST /evaluate {"document": …}` on real conversation shapes.
4. `make build route-policy && make up route-policy`.

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
| `regex_text` | `rule` (or `patterns_ref`): a semantic-router keyword rule `{name, operator: OR\|AND\|NOR, case_sensitive, method: regex?, keywords}`. `scope: current_message` | The rule matches the **latest user message**. Uses `common.keywordmatch` (Go-RE2 semantics: ASCII `\b` / `\w`, `(?i)` unless case-sensitive; no `method` = whole-word literals). |
| `tool_calls` | `pattern` (Python regex, **full-name** match), `scope: current_exchange \| conversation` | The conversation **called** a matching tool: OpenAI `tool_calls`, legacy `function_call`, Anthropic `tool_use`, named tool results (`common.conversation.called_tool_names`). Offered `tools` never count. |
| `cooldown` | `exchanges` (≥ 0), `relevant_if: [evaluator ids]` | The current exchange is relevant, or an earlier one is and fewer than `exchanges` irrelevant exchanges followed it. A faithful port of `ai/litellm/hooks/routing/code_route.py::code_route_decision`, reasons word for word. The judges are asked in order. |
| `backend_load` | `backends` (regexes over api_base), `verdicts` (default busy, down), `min_waiting`, `require: any \| all` | Watched backends in the snapshot qualify. Abstains when none matches. |
| `static` | `abstain`, `confidence`, `reason` | Always (or never). For tests, overrides, and as a `veto`. |

- **Exchanges** come from `common.conversation.split_exchanges`: one per user
  message, holding the message and every round after it.
- **`regex_text` and `tool_calls` can judge a single exchange**, which is what
  `cooldown.relevant_if` uses.
- **Phase 4 adds `llm_classifier`** (`async_next_turn`, with the `analyses`
  table). `context_size` and `semantic_router` are in the plan but not
  scheduled.

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

**Every evaluator always runs**, even after a force vote. The log is the point
of shadow mode.

An evaluator that **raises** is recorded as an abstain with its error, and the
decision goes on. A vote for a route the ruleset does not list is turned into
an abstain with an error.

**The invariants** (`route_policy/engine/invariants.py`) run last:
- `requested_model == local-private` → `local-private`.
- A combined route outside `routes` → `local-private` (or `default_route` if
  the ruleset lacks it), with `apply: false`.

## The `default` ruleset

`rulesets/default.yaml`, version 1, `mode: shadow`, `priority` combiner. It
reproduces today's `auto` routing:

| # | Evaluator | Type | Today's source | Route |
|---|---|---|---|---|
| 1 | `privacy` (`force`) | `regex_text` → `patterns/privacy.yaml` | router `privacy_kw` (priority 300) | `local-private` |
| 2 | `code-text` | `regex_text` → `patterns/code.yaml` | router `code_kw` (200) | `local-code` |
| 3 | `reasoning-text` | `regex_text` → `patterns/reasoning.yaml` | router `reasoning_kw` (150) | `local-reasoning` |
| 4 | `code-tools` | `tool_calls`, current exchange | hook remap, `CODE_ROUTE_TOOL_REGEX` | `local-code` |
| 5 | `sticky-code` | `cooldown`, 3 exchanges, judges `code-tools`, `code-text` | hook cooldown, `CODE_ROUTE_COOLDOWN` | `local-code` |
| — | default | | router `general` (10) | `local-general` |

Rows 4–5 sit after `reasoning-text` because the hook only ever remaps
`local-general`: a reasoning turn stays reasoning.

Until phase 3 deletes the sources, two tests keep this ruleset a copy of them:
- **`test_drift.py`:**
  - the pattern files equal the router's rules (keywords, operator,
    case_sensitive, method);
  - the tool regex and cooldown equal `ai/litellm/hooks/settings.py`'s
    `CODE_ROUTE_*` defaults, read in a subprocess with those variables unset;
  - the wiring mirrors the router's priorities.
- **`test_parity.py`:** runs the ruleset and a reference built from the real
  sources on ~20 conversation shapes. The reference is the router's
  `decisions` compiled with `common.keywordmatch`, plus the hook's
  `code_route_decision`.

**When one fails, change both sides and bump `version`.**

## Decision log and Trino

`route-policy-db`, schema from `route_policy/db/migrations/0001_initial.sql`:

| Table | One row per | Key columns |
|---|---|---|
| `rulesets` | ruleset version | `id`, `version` (PK), `document` (jsonb, resolved), `content_sha256`, `source`, `created_at`, `created_by`, `active_from`, `active_to` |
| `bindings` | scope → version | `scope_model`, `scope_key_alias` (null = every key; unique with NULLS NOT DISTINCT), `ruleset_id`, `version` (FK), `updated_at`, `updated_by` |
| `decisions` | `/decide` | `decision_id` (uuid PK), `created_at`, `chat_id`, `message_id`, `litellm_call_id`, `user_id`, `key_alias`, `requested_model`, `chosen_route`, `applied`, `mode`, `ruleset_id`, `ruleset_version` (FK), `reason`, `scores`, `backend_snapshot`, `message_count`, `tool_count`, `latency_ms`, `error`, `response` |
| `evaluations` | evaluator × decision | `decision_id` (FK, cascade), `position`, `evaluator_id`, `type`, `role`, `vote_route`, `weight`, `confidence`, `reason`, `abstained`, `latency_ms`, `error`, `detail` |
| `outcomes` | `/observe` | `decision_id` (uuid PK, **no FK**), `created_at`, `litellm_call_id`, `response_id`, `served_model_group`, `served_deployment`, `overflow_reason`, `finish_reason`, `tool_calls` (jsonb), `status`, `error` |
| `decision_outcomes` | view | every decision FULL JOIN its outcome, plus `agree` = `chosen_route = served_model_group` |
| `schema_migrations` | applied migration | `version`, `name`, `sha256`, `applied_at` |

- **Indexes:**
  - `decisions(chat_id, created_at)`, `decisions(created_at)`,
    `decisions(litellm_call_id)`;
  - `outcomes(created_at)`, `outcomes(response_id)`,
    `outcomes(litellm_call_id)`;
  - `evaluations(evaluator_id)`.
- **Phase 4 adds `analyses`** as its own migration.
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

The phase-2 success query:

```sql
SELECT d.chosen_route = o.served_model_group AS agree, count(*)
FROM postgres_route_policy.public.decisions d
JOIN postgres_route_policy.public.outcomes o USING (decision_id)
GROUP BY 1;
```

Disagreements are expected only where the engine and today's path differ by
design. Overflow to Claude does not count: `served_model_group` is the chain.

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

**Privacy.** No message text is stored anywhere. Decisions keep:
- ids (chat, message, user, key alias, call, response);
- routes and votes;
- reasons, which name patterns and tools, never content.

Treat `ROUTE_POLICY_TRINO_READER_PASSWORD` as a real secret anyway.

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
| `route_policy_errors_total` | `kind` (decision_write, outcome_write, binding_refresh, ruleset_compile, unbound_model) | Errors |

Plus the HTTP request metrics of prometheus-fastapi-instrumentator. Each
`/decide` also logs one INFO line:
`[decide] <id> auto -> local-code (code-tools: called … ; default@1 shadow) chat=<id>`.

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

`ROUTE_POLICY_DB_HOST` / `_PORT` are fixed in the compose file
(`route-policy-db:5432`). The hook's own settings (`ROUTE_POLICY_URL`,
`ROUTE_POLICY_ENABLED`, `ROUTE_POLICY_TIMEOUT_S`) belong to LiteLLM; see
`ai/litellm/LITELLM.md`.

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
  service keeps deciding from its cache; decisions in the gap are not logged
  (`route_policy_errors_total{kind="decision_write"}`).
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

  That is the classifier's posture, accepted for a shadow log. Revisit it
  before phase 3 makes `/decide` authoritative.

## Code layout

```
ai/route-policy/
  docker-compose.route-policy.yml  Dockerfile  pyproject.toml  ROUTE_POLICY.md  route-policy.postman_collection.json
  db-init/10-trino-reader.sh       the read-only Trino role (first start of an empty volume)
  rulesets/default.yaml  rulesets/bindings.yaml  rulesets/patterns/{privacy,code,reasoning}.yaml
  route_policy/
    app.py              FastAPI factory + lifespan (pool, migrations, seeding)
    config.py           infrastructure settings only
    metrics.py          Prometheus metrics
    service.py          the shared objects; storing decisions and outcomes
    api/                decide, observe, evaluate, rulesets, chats, evaluator_types, health, schemas
    engine/             context (the request view), pipeline (evaluators → combiner → invariants), invariants
    evaluators/         base (contract, @declares, registry), regex_text, tool_calls, cooldown, backend_load, static
    combiners/          base (force / veto / default frame), priority, weighted
    rulesets/           model (document, compiled form, hash), loader (files, refs, bindings), validate, store
    db/                 pool, migrations/ (0001_initial.sql + runner), repositories/
```

Phase 4 adds `analyses/` (the `async_next_turn` queue) and
`evaluators/llm_classifier.py`.

Reused from `shared/common`:
- `common.keywordmatch`: Go-RE2 keyword rules.
- `common.conversation`: exchanges and called tools.

## Tests

`unit-tests/route-policy/`, in its own pytest process:

```bash
UV_LINK_MODE=copy uv run --no-sync python -m pytest unit-tests/route-policy -q -p no:cacheprovider
TEST_POSTGRES_DSN=postgresql://postgres@localhost:55441/postgres \
  UV_LINK_MODE=copy uv run --no-sync python -m pytest unit-tests/route-policy -q -p no:cacheprovider
```

| Module | Covers |
|---|---|
| `test_evaluators.py` | Each evaluator. The cooldown runs against the hook's `code_route_decision` on the same histories. |
| `test_declarations.py` | Every real evaluator's `detail` against its declaration |
| `test_combiners.py`, `test_invariants.py`, `test_pipeline.py` | The frame, both combiners, the invariants, the contract shapes, error handling |
| `test_rulesets.py` | Files, refs (no path escape), validation, content hash, seed bindings, the cache |
| `test_drift.py`, `test_parity.py` | The `default` ruleset against the router config and the hook |
| `test_api.py` | Every endpoint with no database: contract, validation, decisions still answered with the DB down |
| `test_db.py` (`postgres`) | Startup, seeding, immutability, migrations, idempotent `/decide` / `/observe` in either order, the view, the reads |
| `test_wiring.py` | Postman coverage, compose classes, Trino env, pass-through, Makefile, Dockerfile, line endings |

- **The `postgres` tests** need `TEST_POSTGRES_DSN`: an admin connection on
  which each session creates and drops its own `route_policy_test_*`
  database. Without it they skip.
- **For a scratch server on Windows,** use the EDB binaries zip:
  `initdb -U postgres --auth=trust`, then `pg_ctl -o "-p 55441" start`.
