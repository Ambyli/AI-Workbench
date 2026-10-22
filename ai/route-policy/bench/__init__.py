"""The route-classifier benchmark (ROUTE_POLICY_PLAN § 10, phase 4).

Measures how well an ``llm_classifier`` evaluator -- a criteria set scored
by a classifier model through the document classifier's ``POST /assess`` --
routes real conversations, against the rule-based ``default`` ruleset, before
anyone promotes it. Operator guide: ``bench/BENCH.md``.

The pipeline, one file each, every step writing JSONL into a DATA directory
the operator names (on the box, OUTSIDE the repo, deleted afterwards):

    export_chats.py   inside the ``openwebui`` container (stdlib only, read-only
                      sqlite): the current branch of every recent chat
                      -> chats.jsonl
    points.py         one DECISION POINT per user message: the OpenAI
                      messages /decide received for it (``convert``), weak
                      labels from what happened next, and the live rules'
                      own route (``baseline``) -> points.jsonl (+ a
                      stratified sample for gold labelling)
    label.html        a person labels the sample in a browser, offline
                      -> labels.jsonl
    run.py            every point x candidate model x criteria set through
                      route-policy's own ``ClassifierClient`` -- the
                      production /assess cycle -> results.jsonl (resumable)
    report.py         accuracy, macro-F1, per-route P/R, local-private
                      recall, confusion, agreement with the rules, failure
                      rate, latency, calibration and the § 10 promotion rule
                      (``metrics`` holds the pure maths)

Supporting modules: ``convert`` (Open WebUI 0.11.4's stored chat -> the
messages /decide received), ``baseline`` (the live ruleset run offline),
``criteria`` (a criteria set: an llm_classifier entry of a ruleset file),
``results`` (the results.jsonl line: fields and identity), ``jsonl``
(reading and appending the data files).

**Customer data.** Every DATA file holds real conversations (results and
labels hold ids and verdicts, but sit next to them). None of it is ever
written into the repo or the image; the tools only write where they are
told. Labels come from a person, never from Claude or any hosted model --
and the candidates are refused by route-policy's client unless the
classifier reports them as local models.

The package is importable when ``ai/route-policy`` is on ``sys.path`` (the
image's PYTHONPATH; unit-tests/route-policy/conftest.py in the tests), and
each tool runs as ``python -m bench.<tool>`` -- except ``export_chats.py``,
which is piped into the Open WebUI container's own interpreter.
"""
