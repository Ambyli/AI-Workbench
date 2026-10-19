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

from prometheus_client import Counter, Histogram

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
    # decision_write  -- a decision was computed and returned but could not be stored
    # outcome_write   -- POST /observe answered 503
    # binding_refresh -- the binding snapshot could not be refreshed (the old one is kept)
    # ruleset_compile -- a stored ruleset this copy cannot compile
    # unbound_model   -- POST /decide for a model no binding covers (404)
    ["kind"],
)
