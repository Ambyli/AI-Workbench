"""Prometheus metrics for the route-policy service, served at GET /metrics.

In one module so the API layer, the engine and the database code record into
the same objects without importing each other. HTTP request counts and
latencies per handler come from prometheus-fastapi-instrumentator (app.py);
these are the domain metrics on top. Scraped by the litellm stack's
prometheus (ai/litellm/prometheus.yml, job "route-policy").

Label values are bounded: routes and modes come from validated rulesets,
evaluator ids and types from the rulesets this copy has compiled, `kind` and
`outcome` from fixed lists below -- never anything a caller sends.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

# Engine latency is sub-millisecond to a few ms; the buckets resolve that and
# still show a pathological history (a huge conversation) as a tail.
_FAST_BUCKETS = (0.0001, 0.00025, 0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 1.0)

decisions_total = Counter(
    "route_policy_decisions_total",
    "Decisions computed by POST /decide, by chosen route, ruleset mode and whether it may be applied",
    ["route", "mode", "applied"],
)
decide_seconds = Histogram(
    "route_policy_decide_seconds",
    "Engine time for one decision (every evaluator plus the combiner), without the database write",
    buckets=_FAST_BUCKETS,
)
evaluator_seconds = Histogram(
    "route_policy_evaluator_seconds",
    "Time one evaluator took on one request",
    ["evaluator", "type"],
    buckets=_FAST_BUCKETS,
)
votes_total = Counter(
    "route_policy_votes_total",
    "Evaluator results by outcome",
    # vote    -- voted for a route
    # abstain -- had nothing to say
    # error   -- raised; counted as an abstain, the error is logged and stored
    ["evaluator", "type", "outcome"],
)
invariant_overrides_total = Counter(
    "route_policy_invariant_overrides_total",
    "Decisions the service invariants changed",
    # private     -- the request asked for local-private; the route stays local-private
    # not_a_chain -- the combined route is not one of the ruleset's chain aliases
    ["invariant"],
)
observations_total = Counter(
    "route_policy_observations_total",
    "Outcomes recorded by POST /observe, by status",
    ["status"],  # success | failure | other (any other string; it is stored as sent)
)
duplicates_total = Counter(
    "route_policy_duplicates_total",
    "Requests whose decision_id was already stored (the first write wins)",
    ["endpoint"],  # decide | observe
)
errors_total = Counter(
    "route_policy_errors_total",
    "Errors by kind",
    # decision_write          -- a decision was computed and returned but could not be stored
    # decision_write_dropped  -- ... and was not even tried: WRITES_MAX_IN_FLIGHT background writes pending
    # outcome_write           -- an outcome could not be stored (503 when it failed within the budget)
    # outcome_write_dropped   -- POST /observe answered 503 without trying: too many writes pending
    # binding_refresh -- the binding snapshot could not be refreshed (the old one is kept)
    # ruleset_compile -- a stored ruleset this copy cannot compile
    # unbound_model   -- POST /decide for a model no binding covers (404)
    # shadow_load     -- a bound shadow ruleset could not be loaded by this copy (skipped)
    # shadow_dropped  -- /decide's after-the-answer task (shadows, analysis requests) was not
    #                    started: WRITES_MAX_IN_FLIGHT background tasks pending
    # shadow_error    -- that task, or one shadow ruleset in it, raised (a bug; logged by exception
    #                    type; the other shadows still run)
    # signals         -- load_signals raised past the client (a bug): the signal evaluators abstain
    #                    ("signals failed") and pii votes from its patterns
    # analysis_lookup -- the verdicts an llm_classifier needs could not be read (it abstains)
    # turn_pin        -- the turn's first decision could not be read (the call is decided afresh)
    # analysis_write  -- a classification ended but its analyses row could not be stored
    # classifier_delete -- DELETE /jobs/{id} on the classifier failed (the job expires with its TTL)
    ["kind"],
)
writes_deferred_total = Counter(
    "route_policy_writes_deferred_total",
    "Writes that outlasted ROUTE_POLICY_DECISION_WRITE_BUDGET_S: the request was answered and the "
    "write went on in the background",
    ["write"],  # decision | outcome
)
writes_in_flight = Gauge(
    "route_policy_writes_in_flight",
    "Decision / outcome writes started and not finished yet (capped at WRITES_MAX_IN_FLIGHT)",
)
db_reads_cut_total = Counter(
    "route_policy_db_reads_cut_total",
    "Database reads on the /decide path that outlasted DB_READ_BUDGET_S (the request went on without them)",
    ["read"],  # binding_refresh | ruleset_fetch | analysis_lookup | turn_pin
)
pinned_total = Counter(
    "route_policy_pinned_total",
    "Decisions of a `pin: turn` ruleset that were not their turn's first (same chat id + message id), by "
    "ruleset and what the turn's pin did",
    # ruleset: <id>@<version>
    # outcome: changed -- the fresh route differed; the turn's first route was kept instead
    #          kept    -- the fresh route already was the turn's
    #          private -- the fresh route was local-private and beat the pin (privacy always wins)
    ["ruleset", "outcome"],
)

# -- Shadow rulesets and the analysis queue (phase 4) ------------------------------

shadow_decisions_total = Counter(
    "route_policy_shadow_decisions_total",
    "Shadow decisions made after /decide answered, by shadow ruleset and whether they chose the "
    "primary's route",
    ["ruleset", "agree"],  # ruleset: <id>@<version> of a bound shadow; agree: true | false
)
analyses_total = Counter(
    "route_policy_analyses_total",
    "Background classifications finished, by classifier model and status",
    # status: ok | incomplete | unavailable | refused | failed | timeout | not_local
    ["model", "status"],
)
analysis_seconds = Histogram(
    "route_policy_analysis_seconds",
    "One classification's cycle (locality check, submit, the classifier's queue, the model calls, "
    "the result), by classifier model",
    ["model"],
    buckets=(0.5, 1, 2, 5, 10, 20, 30, 60, 90, 120, 180, 300),
)
analysis_queue_depth = Gauge(
    "route_policy_analysis_queue_depth",
    "Analysis requests waiting for a worker (running ones not included)",
)
analysis_dropped_total = Counter(
    "route_policy_analysis_dropped_total",
    "Analysis requests not classified",
    # queue_full -- ROUTE_POLICY_ANALYSIS_QUEUE_MAX requests were already waiting
    # shutdown   -- still waiting (or arriving) when the service stopped
    ["reason"],
)

# -- The signals sidecar (phase 5) ------------------------------------------------------

signals_seconds = Histogram(
    "route_policy_signals_seconds",
    "One POST /signals to the signals sidecar, as route-policy waited for it (within "
    "ROUTE_POLICY_SIGNALS_BUDGET_S on /decide and for shadows), by kind",
    ["kind"],  # primary | shadow | evaluate (POST /evaluate, 10 s, no deadline cut)
    buckets=(0.005, 0.01, 0.02, 0.03, 0.05, 0.075, 0.1, 0.15, 0.25, 0.5, 1.0, 2.5, 10.0),
)
signals_total = Counter(
    "route_policy_signals_total",
    "Signals calls by outcome",
    # ok          -- the sidecar answered (each task's own status: route_policy_signal_tasks_total)
    # timeout     -- no answer within the budget: the signal evaluators abstain, pii votes from patterns
    # error       -- a non-200, a malformed answer, a connection error
    # unavailable -- ROUTE_POLICY_SIGNALS_URL is empty (no client)
    # not_local   -- the sidecar's host resolves outside the box's networks: nothing was sent
    ["outcome"],
)
signal_tasks_total = Counter(
    "route_policy_signal_tasks_total",
    "Tasks of the signals calls that were answered, by task and status",
    # task:   pii | nearest | injection | language | other
    # status: ok | partial | deadline | skipped | warming | error | unavailable (the sidecar's),
    #         missing (no result for a task that was sent), not_sent (no text fit the caps), other
    ["task", "status"],
)
