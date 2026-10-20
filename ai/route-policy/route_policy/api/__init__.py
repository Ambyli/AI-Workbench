"""HTTP endpoints, one module per group (ROUTE_POLICY.md § API):

    decide.py            POST /decide
    observe.py           POST /observe
    evaluate.py          POST /evaluate (dry run, writes nothing)
    rulesets.py          GET /rulesets, /rulesets/{id}, /rulesets/{id}/{version}, /bindings
    chats.py             GET /chats/{chat_id}/decisions (primaries, each with its shadows)
    evaluator_types.py   GET /evaluator-types
    health.py            GET /health, GET /ready
    schemas.py           the request models (the API contract)

GET /metrics is mounted by app.py (prometheus-fastapi-instrumentator).
"""
