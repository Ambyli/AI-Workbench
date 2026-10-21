"""The signals sidecar: fast CPU signals for route-policy's evaluators.

One ``POST /signals`` per route-policy decision carries the conversation's
texts (by id) and up to four tasks -- ``pii`` (NER entity types), ``nearest``
(topic by nearest labelled examples), ``injection`` (a jailbreak score) and
``language`` -- and gets back, per task, a status and per-text features
within the caller's deadline. Never text, spans or offsets.

    app.py          FastAPI app: lifespan (lanes, background model load), routes
    api.py          POST /signals, GET /models, GET /health; error shapes that never echo text
    schemas.py      the request model (on common.signals' contract) and the answer
    scheduler.py    model lanes (single-flight, deadline-aware) and Engine.run
    cache.py        feature LRU by text hash; embedded example sets
    runtime.py      models.yaml specs, onnxruntime sessions, load state, the build check
    tasks/          one module per model lane
    metrics.py      Prometheus metrics
    config.py       infrastructure settings (threads, cache size, paths)
    fetch_models.py build time: download + verify the pinned files
    bench.py        `python -m signals.bench`: latency on this CPU

Operator guide: ai/signals/SIGNALS.md.
"""
