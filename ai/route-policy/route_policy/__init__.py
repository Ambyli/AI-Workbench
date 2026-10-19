"""route_policy -- the route-policy service (ROUTE_POLICY.md).

A ruleset of evaluators (regex over the latest user message, the tools the
conversation called, an exchange cooldown, backend load, a fixed vote) votes
on which LiteLLM CHAIN ALIAS an `auto` turn should use; a combiner turns the
votes into one route; the service enforces two invariants (a requested
``local-private`` stays ``local-private``; a route is always one of the
ruleset's chain aliases). Every ``/decide`` is logged with its votes, every
``/observe`` with what really answered, in route-policy-db, which Trino reads
as the ``postgres_route_policy`` catalog.

    app.py          FastAPI factory + lifespan (pool, migrations, seeding)
    config.py       infrastructure settings only -- never routing rules
    metrics.py      Prometheus counters and histograms
    service.py      the process-wide objects the routes share
    api/            one module per endpoint group, plus the request/response models
    engine/         request view, evaluator pipeline, invariants
    evaluators/     the plugins (regex_text, tool_calls, cooldown, backend_load, static)
    combiners/      priority, weighted
    rulesets/       document model, loader (files + refs), validation, cache
    db/             pool, migrations, repositories

Phase 3 of ROUTE_POLICY_PLAN: the `default` ruleset is ``mode: enforce`` --
LiteLLM awaits ``/decide`` for every ``auto`` request and serves the chain it
returns when ``apply`` is true (anything else goes to ``local-private``). A
``shadow`` ruleset is still decided and logged, never applied. The
llm_classifier evaluator and the analyses queue are phase 4 and do not exist
here yet.
"""
