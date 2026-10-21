"""The fast signals of phase 5: what evaluators ask the ``signals`` sidecar
for, merged into ONE call per decision, and what they read back.

    model.py   SignalText / SignalRequest (what an evaluator asks for),
               merge() -> SignalBatch (one request's worth, request caps
               applied), SignalTask (one task's result as evaluators read
               it on ``ctx.signals``), the model-pin rule -- pure
    wire.py    the adapter to ``common.signals``: a SignalBatch -> the wire
               request, the client's outcome -> ``ctx.signals`` /
               ``ctx.signals_error`` / ``ctx.signals_ms``

The flow (ROUTE_POLICY.md § Signals): before the pipeline runs,
``engine.pipeline.signal_requests`` gathers the ruleset's
``Evaluator.signal_requests`` into one batch, and
``Service.load_signals`` sends it -- one ``POST /signals``, concurrently
with the verdict lookup and the turn pin, within
``ROUTE_POLICY_SIGNALS_BUDGET_S`` -- and puts the answer on the request
view. Evaluators read only the context, so the pipeline stays a pure
function. No text from a request is ever logged, stored or put in a
metric label: only task names, statuses, counts and scores come back.

Deliberately imports nothing here (like ``analysis``): ``engine.context``
and ``evaluators.base`` import ``signals.model``, and ``wire`` imports
``common.signals``.
"""
