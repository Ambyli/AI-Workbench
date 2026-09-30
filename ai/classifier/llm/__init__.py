"""Everything that touches the vision model, in three layers plus the loop.

    prompts.py   the wording: the single-criterion scoring prompt (the
                 rubric from ``config.HINT_RUBRICS``, the document-text block)
                 and the enforcement loop's two small prompts, which live next
                 to it so they cannot drift from it.
    client.py    the transport: ``LLM_CALLS`` (the process-wide limit every
                 request holds a slot of), ``call_vllm`` for the scoring call
                 and ``call_vllm_json`` for the small ones, the image encoder,
                 and the two Prometheus objects that count both.
    validate.py  believing the answer only so far: find the criterion's answer,
                 clamp it, recompute the verdict from the clamped score.
    boxes.py     not believing it at all: ask -> validate -> verify by crop ->
                 retry, the bounding-box enforcement loop.

ONE IMAGE PER PROMPT. The model is served by vLLM WITHOUT
``--limit-mm-per-prompt``, so a request may carry at most one image; a second
one fails the whole call. Every prompt builder here takes a single image.

Nothing is re-exported. A caller imports the submodule it means —
``from llm import client as llm_client`` — so that a test replacing
``llm.client.call_vllm`` / ``call_vllm_json`` / ``_send`` replaces the ONE
object every caller reaches, rather than one of two aliases.
"""
