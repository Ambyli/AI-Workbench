"""Everything that touches the vision model, in three layers plus the loop.

    prompts.py   the wording: the scoring prompt (rubrics from
                 ``config.HINT_RUBRICS``, the pre-filled scaffold, the
                 document-text block) and the enforcement loop's two small
                 prompts, which live next to it so they cannot drift from it.
    client.py    the transport: ``call_vllm`` for the scoring call and
                 ``call_vllm_json`` for the small ones, the image encoder, and
                 the two Prometheus objects that count both.
    validate.py  believing the answer only so far: key normalisation and the
                 clamp that recomputes every verdict from its clamped score.
    boxes.py     not believing it at all: ask -> validate -> verify by crop ->
                 retry, the bounding-box enforcement loop.

ONE IMAGE PER PROMPT. The model is served by vLLM WITHOUT
``--limit-mm-per-prompt``, so a request may carry at most one image; a second
one fails the whole call. Every prompt builder here takes a single image.

Nothing is re-exported. A caller imports the submodule it means —
``from llm.client import call_vllm``, ``from llm import client as llm_client``
— so that a test replacing ``llm.client.call_vllm_json`` replaces the ONE
object every caller reaches, rather than one of two aliases.
"""
