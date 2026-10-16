"""Everything that touches the models, in four layers plus the loop.

    models.py    the registry: ``models.yaml`` validated into one
                 ``ModelSpec`` per model (endpoint, key, budgets, reasoning,
                 timeout, its own concurrency limit) — what every prompt
                 builder and every request is addressed to.
    prompts.py   the wording: the single-criterion scoring prompt (the
                 rubric from ``config.HINT_RUBRICS``, the document-text block)
                 and the enforcement loop's two small prompts, which live next
                 to it so they cannot drift from it.
    client.py    the transport: each request holds a slot of ITS model's
                 limit, ``call_model`` for the scoring call and
                 ``call_model_json`` for the small ones, the image encoder,
                 and the Prometheus objects that count both.
    usage.py     what each request cost, durably: the ``llm_calls`` table
                 (one row per request, linked to its job, criterion and
                 item through context vars), written by ``client._post``.
    validate.py  believing the answer only so far: find the criterion's answer,
                 clamp it, recompute the verdict from the clamped score.
    boxes.py     not believing it at all: ask -> validate -> verify by crop ->
                 retry, the bounding-box enforcement loop.

IMAGES PER PROMPT. Every prompt builder here takes ONE image — the page, a
crop, or (selection) the candidate — except the scoring prompt when the
request's references guide it, which puts up to the model's
``max_images_per_prompt`` - 1 example images before the candidate.
muse-glimmer is served with ``--limit-mm-per-prompt '{"image": 3}'`` for
that, and its registry entry must match it (see ``llm.prompts``).

Nothing is re-exported. A caller imports the submodule it means —
``from llm import client as llm_client`` — so that a test replacing
``llm.client.call_model`` / ``call_model_json`` / ``_send`` replaces the ONE
object every caller reaches, rather than one of two aliases.
"""
