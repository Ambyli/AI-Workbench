# Route-classifier benchmark

How well does an `llm_classifier` evaluator route real conversations, how fast, and is it good enough to promote? This directory answers that before anyone adds the classifier to the live `default` ruleset (ROUTE_POLICY_PLAN § 10, Jira ZT-471 phase 4). It measures each candidate **model × criteria set** on decision points rebuilt from real Open WebUI chats, against a person's gold labels and against the rule-based ruleset that routes today.

## Contents

- [The rules for the data](#the-rules-for-the-data)
- [What it measures](#what-it-measures)
- [Before you start](#before-you-start)
- [Step by step on the box](#step-by-step-on-the-box)
- [File formats](#file-formats)
- [Reading the report](#reading-the-report)
- [How a decision point is rebuilt](#how-a-decision-point-is-rebuilt)
- [Re-running after a criteria change](#re-running-after-a-criteria-change)
- [Code and tests](#code-and-tests)

## The rules for the data

The export holds **real customer conversations**. Every file this benchmark reads or writes is customer data or sits next to it.

- **Never in the repo, never in an image.** Everything goes into one directory on the box, outside the repo (`~/route-bench/` below), created `chmod 700`. No tool writes anywhere it is not told to; the chat export goes straight from `docker exec` to that directory and is never written inside a container.
- **Labels come from a person, never from Claude or any hosted model.** The candidates are local models reached through the document classifier, and route-policy's client refuses a model the classifier does not report as local (`not_local`).
- **Labelling happens offline.** `label.html` opens from disk and loads the sample into the tab's memory only: no network (a Content-Security-Policy forbids it), no browser storage. Copy the sample to a labeller's machine **only if that person is allowed to see customer data**, and delete the copy when the labels are back on the box.
- **Delete everything afterwards** ([step 7](#7-clean-up)). Keep only the report text, which holds counts and rates, no conversation text.

## What it measures

For each candidate (a classifier model and a criteria set: the `llm_classifier` entry of a ruleset file, e.g. `rulesets/default-llm.yaml` → `topic-llm`):

| Metric | Why |
|---|---|
| Accuracy and macro-F1, per-route precision and recall | the headline |
| `local-private` recall, on its own, next to the privacy keywords' alone | a missed PII conversation is the costly error |
| Confusion matrix | e.g. general ↔ code drift |
| Agreement with the live rules (`rules_route`), overall / where a rule fired / where the rules fell through to their default | where the classifier adds signal, where it only repeats the rules |
| Failure rate (status not `ok` / `incomplete`) and no-verdict rate | a failed classification is an abstain in production |
| p50 / p95 latency, queue time, model seconds, tokens per call | cost to the shared models |
| Calibration: accuracy per confidence bucket | to set weights and thresholds later |

Against two label tiers, always named in the output: **gold** (a person's labels on a sample) and **weak** (derived from what happened, on every point — see [points](#pointsjsonl)).

**The promotion rule** (§ 10), PASS / FAIL per candidate. A candidate passes only if all four hold:

1. its accuracy on the gold labels is at least the rules' (`rules_route`) on the same points;
2. its `local-private` recall on gold is at least the privacy keywords' alone;
3. its failure rate is under 1 %;
4. its p95 latency is below the median gap between user messages (printed; the time a verdict requested at one message has before the next needs it).

A condition that cannot be computed (no gold labels yet, no gold `local-private` point among the candidate's points) is not passed. A PASS is the go-ahead to *propose* a new `default` version with the classifier in it; promoting is a separate decision (ROUTE_POLICY.md).

## Before you start

- Phase 4 is deployed: `qwen3.5-4b` is up, the classifier lists the candidate models in `GET /models` as available (`docker exec classifier python3 -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8000/models').read().decode())"`), and route-policy runs an image built from this branch (it carries `bench/` and `rulesets/default-llm.yaml`).
- **Run the classification step off-hours.** The candidates are shared: `muse-glimmer` also serves chat and the document classifier, `qwen3.8-solo` serves chat. Keep **one, at most two, classifications in flight** (`run.py` refuses more): a chat model whose vLLM waiting queue stays non-empty for `OVERFLOW_BUSY_AFTER_S` counts as busy, and LiteLLM's overflow hook then spills live chat turns to the next order — to Claude, on the Claude-capable chains. The benchmark's jobs also share the classifier's FIFO with document jobs; a document backlog shows up as `queue_ms`.
- The steps below run from the repo checkout on the box (`ai/route-policy/bench/export_chats.py` is read from it), with the data directory as `~/route-bench`.

## Step by step on the box

### 0. The data directory

```bash
umask 077
mkdir -p ~/route-bench && chmod 700 ~/route-bench
# The image of the running route-policy (it has bench/ baked in):
IMAGE=$(docker inspect --format '{{.Image}}' "$(docker ps -q \
  --filter label=com.docker.compose.project=ai-route-policy \
  --filter label=com.docker.compose.service=route-policy \
  --filter label=com.docker.compose.oneoff=False | head -n1)")
echo "$IMAGE"
```

Every tool but the export runs as a one-off container of that image (`python -m bench.<tool>`, working directory `/app/ai/route-policy`, so `rulesets/...` paths resolve), with the data directory bind-mounted at `/data` and your own uid so the files stay yours. Plain `docker run`, not `docker compose run`: the tools need none of the service's environment (its database credentials) or dependencies, and get only the network each step names (`none` except for the classification step).

### 1. Export the chats

Read-only, from inside the Open WebUI container, straight to the box:

```bash
docker exec -i openwebui python3 - --days 30 \
  < ai/route-policy/bench/export_chats.py > ~/route-bench/chats.jsonl
```

`--days N` takes chats updated in the last N days (`--chat-id ID`, repeatable, takes named chats instead). Progress goes to stderr. It opens `webui.db` with `mode=ro` and `PRAGMA query_only` and only ever SELECTs.

### 2. Export the decisions

The primary decision route-policy logged for each answer — one of the weak-label sources:

```bash
docker exec route-policy-db psql -U route_policy -At -c "
  SELECT row_to_json(d) FROM (
    SELECT decision_id, created_at, chat_id, message_id, chosen_route, shadow_of,
           ruleset_id, ruleset_version, mode, requested_model
    FROM decisions
    WHERE chat_id IS NOT NULL AND message_id IS NOT NULL
      AND created_at > now() - interval '31 days'
  ) d" > ~/route-bench/decisions.jsonl
```

(`-U` is `ROUTE_POLICY_DB_USER` if `.env` overrides it.) `-At` prints one JSON object per line, unescaped. Optional: without it, the third weak-label rule simply never fires.

### 3. Build the decision points and the sample

```bash
docker run --rm --network none --user "$(id -u):$(id -g)" \
  -v ~/route-bench:/data "$IMAGE" /app/.venv/bin/python -m bench.points \
  --chats /data/chats.jsonl --decisions /data/decisions.jsonl \
  --out /data/points.jsonl --sample 200 --sample-out /data/sample.jsonl
```

No network: it needs none. It rebuilds what `/decide` received for every user message ([how](#how-a-decision-point-is-rebuilt)), runs the live `rulesets/default.yaml` on it offline (`rules_route`), and derives weak labels. The sample is stratified by (weak label, rules route) — round-robin across the groups, so rare ones such as `local-private` are fully represented before common ones fill the rest — with at most `--sample-per-chat` (5) points per chat, written shuffled (`--seed`). Aim for 150–200 gold points.

`--keep-exchanges N` keeps only each point's last N exchanges (smaller files). It must be at least the largest `history.exchanges` of the criteria sets you will run; `run.py` warns when it is not.

### 4. Label the sample

1. Copy `~/route-bench/sample.jsonl` to the labeller's machine — only if they may see customer data — and open `ai/route-policy/bench/label.html` from disk in a browser (no server, no network).
2. **Points** → `sample.jsonl`. To resume a session, also load the earlier `labels.jsonl` under **Labels to resume**.
3. For each point, read the conversation and label the **latest user message** (highlighted) with the chain it should go to: keys `1`–`4` (`local-general`, `local-code`, `local-reasoning`, `local-private`), `←` / `→` back / next, `N` next unlabelled, `X` clear, `R` reveal. Add a note for anything ambiguous. "What the rules said" stays hidden unless revealed — reveal only after choosing, if at all.
4. **Export labels.jsonl** (the page warns before closing with unexported labels), copy it to `~/route-bench/labels.jsonl`, then delete the sample and the labels from the labeller's machine.

### 5. Classify (off-hours)

Gold-labelled points first — they are what the promotion rule reads:

```bash
docker run --rm --network ai_shared --user "$(id -u):$(id -g)" \
  -v ~/route-bench:/data "$IMAGE" /app/.venv/bin/python -m bench.run \
  --points /data/points.jsonl --only-points /data/labels.jsonl --results /data/results.jsonl \
  --models qwen3.5-4b,muse-glimmer,qwen3.8-solo \
  --ruleset rulesets/default-llm.yaml --evaluator topic-llm
```

Then, if there is time, everything (`--only-points` dropped; the same `--results` file — what is already there is skipped). Other options: `--concurrency 2` (the cap), `--classifier-url` (default `http://classifier:8000`), `--timeout-s` (180) / `--poll-s` (1) per `/assess` cycle, `--limit N`, `--retry-failed` (classify again what produced no scores), `--progress-every N`. Repeat `--ruleset` / `--evaluator` to compare criteria sets (one `--ruleset` with several `--evaluator`s reuses that file); a criteria set must be an `llm_classifier` entry that route-policy itself would accept.

Each classification is the production cycle — route-policy's own `ClassifierClient`: the locality guard, `POST /assess` with the rendered transcript as one `text` document and one `llm` presence criterion per route, polling, reading the scores, and **always** `DELETE /jobs/{id}`. Interrupting (Ctrl-C) keeps every finished line; rerun the same command to resume. A job cut off mid-cycle is deleted by the client when it can; anything left expires with the classifier's `JOB_TTL_HOURS`.

### 6. Report

```bash
docker run --rm --network none --user "$(id -u):$(id -g)" \
  -v ~/route-bench:/data "$IMAGE" /app/.venv/bin/python -m bench.report \
  --points /data/points.jsonl --results /data/results.jsonl --labels /data/labels.jsonl \
  > ~/route-bench-report.txt
```

`--json` prints the same numbers as one JSON document. The report holds no conversation text; it is the one thing to keep.

### 7. Clean up

```bash
rm -rf ~/route-bench
```

Every classification deleted its own classifier job (`DELETE /jobs/{id}`), with its text payload, artifacts and usage rows. One the client could not delete (the classifier was down at that moment, or the run was killed mid-cycle) expires with the classifier's `JOB_TTL_HOURS`; to remove it sooner, find it in `GET /jobs` on the classifier and `DELETE /jobs/{id}`.

## File formats

All JSONL: one JSON object per line, UTF-8 (written ASCII-escaped).

### chats.jsonl

`export_chats.py`, one line per chat:

| Field | |
|---|---|
| `chat_id`, `user_id` | Open WebUI ids |
| `created_at`, `updated_at` | epoch seconds |
| `current_message_id` | the branch tip |
| `source` | `chat_message` (normalised rows), `chat_message+history` (rows with gaps filled from the legacy JSON) or `history` (legacy JSON only) — as Open WebUI 0.11.4 builds its message map |
| `context_compaction` | Open WebUI's context compaction was on (config row, else env) |
| `function_calling` | the chat's `params.function_calling` (`legacy` turns off the attached-files block), or null |
| `messages` | the current branch, root first: `id`, `parentId`, `role`, `content`, `output` (the assistant's items: text, tool calls, tool results; reasoning reduced to its type), `model`, `timestamp`, `done`, `error` (bool), `files` (user messages: `type` / `id` / `url` / `content_type` / `name`), `context_summary` (bool), `sources` (count). Inline `data:` payloads are replaced by `data:<type>;base64,[omitted: N chars]`. |

### decisions.jsonl

Step 2's query: `decision_id`, `created_at`, `chat_id`, `message_id` (the assistant message being generated), `chosen_route`, `shadow_of` (null for a primary decision), `ruleset_id`, `ruleset_version`, `mode`, `requested_model`.

### points.jsonl

`points.py`, one line per user message of every chat's branch (`sample.jsonl` has the same lines):

| Field | |
|---|---|
| `point_id` | `<chat_id>:<user_message_id>` |
| `chat_id`, `user_id`, `user_message_id` | |
| `answer_message_id` | the assistant message answering it (null when none) |
| `k` | the user message's 1-based index among the user messages of `messages` (= their count): `render_transcript(messages, upto_user=k)` |
| `user_index` | its index among the STORED user messages (differs from `k` after a tool-image message or compaction) |
| `timestamp`, `gap_s` | epoch seconds; seconds since the previous user message (null for the first) |
| `messages` | the OpenAI messages `/decide` received, up to and including this user message |
| `trimmed_exchanges` | exchanges cut from the front by `--keep-exchanges` (0 = complete) |
| `answer_tools` | tools the answer called, most recent first |
| `weak_label`, `weak_source` | by precedence: `privacy_rule` → `local-private`; `code_tool` (the answer called a tool the `code-tools` regex matches) → `local-code`; `decision` (the earliest primary decision for `answer_message_id`) → its route; else null |
| `decision_route` | that decision's route, whatever the precedence chose |
| `rules_ruleset`, `rules_route`, `rules_decided_by` | the live ruleset (`default@N`), its route offline, and the evaluator that decided (null = its default route) |
| `privacy_kw_route` | `local-private` when the privacy keywords matched, else null |
| `compacted`, `answer_error`, `answer_sources` | a summary checkpoint dropped earlier messages; the answer failed; how many sources the answer cited (the prompt `/decide` saw may then have carried Open WebUI's retrieval template, which cannot be rebuilt) |

### labels.jsonl

`label.html`: `point_id`, `label` (one of the four routes), `note`, `labelled_at` (ISO time). The last line per point wins.

### results.jsonl

`run.py`, one line per classification, appended:

| Field | |
|---|---|
| `point_id`, `model` | |
| `criteria_set`, `criteria_fingerprint` | `<ruleset>@<version>/<evaluator>`, and a SHA-256 over the criteria (in order), `otherwise`, `min_score` and `history` — resume and the report key on the fingerprint |
| `route`, `confidence` | `verdict.choose`'s answer (null route = abstain or failure) |
| `scores` | route → 1–10 (null: that criterion errored) |
| `reason` | the winning criterion's reason (≤ 200 characters) |
| `status`, `error` | `ok`, `incomplete`, `unavailable`, `refused`, `failed`, `timeout`, `not_local`, or `skipped` (no transcript) |
| `latency_ms`, `queue_ms`, `model_seconds` | the cycle up to the result; submit → running; the classifier's model time |
| `calls`, `prompt_tokens`, `completion_tokens` | the classifier's usage for the job |
| `input_chars`, `at` | transcript length; ISO time |

## Reading the report

The summary table lists the rules and every candidate (`model · criteria set (fingerprint)`) with results, failure rate, gold and weak accuracy, p95 latency and the verdict; then one block per candidate. In each tier the rules' numbers are computed **on the same points** as the candidate's, so a candidate run on the gold points only is compared fairly.

- **Weak accuracy is not a quality score.** Most weak labels are the live rules' own decisions, so the rules score high there by construction. Read the weak tier as "how often does this candidate depart from today's routing, and where" — the confusion matrix and the agreement split say where.
- **Agreement where the rules fell through** (the default route, no rule fired) is where a classifier can add signal; agreement where a rule fired is where it only repeats one.
- **Calibration** shows whether high-confidence verdicts are more often right — the input for a later `weight` / threshold choice.
- **An abstain or a failure counts as wrong** in accuracy and recall.

## How a decision point is rebuilt

A point is only worth measuring if the classifier sees what route-policy saw. Open WebUI 0.11.4 does not send the browser's copy of a saved chat: on every completion request it rebuilds the history from its database. `convert.py` repeats that rebuild, step for step (the module docstring names each 0.11.4 function it ports):

1. the chain from the root to the user message; an errored, empty assistant message is dropped;
2. image files become `image_url` parts (placeholder URLs);
3. with compaction on, the latest summary checkpoint drops what precedes it;
4. stored output items become the real assistant rounds — text, `tool_calls` (completed calls with a result only), `tool` results, and the extra user message Open WebUI adds for images a tool returned; reasoning is not replayed;
5. unpaired tool calls and results are dropped;
6. with native function calling, each user message gets the `<attached_files>` block naming its files (which the code keywords can match: `report.json`);
7. LiteLLM's pre-call hook strips the `auto` footer (`*model · chain*`) from earlier answers before `/decide` — the regex is a copy of `ai/litellm/hooks/footer/render.py`'s, pinned by a test.

Not rebuilt (not in the database): the system prompt (no evaluator or transcript reads it), retrieval context Open WebUI writes into the user message for attached files or web search (flagged as `answer_sources`), guided-regeneration prompts.

## Re-running after a criteria change

The same harness is the regression test for a new criteria set or model: write the new version of the draft ruleset (a version bump — the content hash enforces it), then run `run.py` on the gold points with both `--ruleset`/`--evaluator` pairs and compare the two blocks of the report. Old results stay valid: they are keyed by fingerprint.

## Code and tests

| File | |
|---|---|
| `export_chats.py` | the read-only export (stdlib only; runs in the Open WebUI container) |
| `convert.py` | Open WebUI's stored chat → the messages `/decide` received |
| `baseline.py` | the live ruleset run offline; the weak labellers |
| `points.py` | decision points, weak labels, the stratified sample |
| `criteria.py` | a criteria set from a ruleset file, and its fingerprint |
| `run.py` | the classification loop (resumable, ≤ 2 in flight) |
| `metrics.py` | the pure metric maths and the promotion rule |
| `report.py` | the report |
| `results.py` | the `results.jsonl` line's fields and identity (shared by `run.py` and `report.py`) |
| `jsonl.py` | reading and appending the data files |
| `label.html` | the offline labelling page |

Tests use fabricated conversations only:

```bash
UV_LINK_MODE=copy uv run --no-sync python -m pytest unit-tests/route-policy/bench -q -p no:cacheprovider
```
