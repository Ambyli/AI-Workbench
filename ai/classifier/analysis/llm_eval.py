"""The `llm` evaluator: ONE scoring call for one criterion, then maybe boxes.

Each (``llm`` criterion, item) is its own unit of work and its own model
call: the item's page image (one page, so there is no page to choose) plus
the text layer ITS ``options.ocr`` produces, truncated to
CLASSIFIER_TEXT_CHAR_BUDGET, scored on the rubric ITS ``options.hint``
selects. A model that cannot answer fails this criterion alone.

With ``options.boxes`` the bounding-box enforcement loop (``llm.boxes``)
runs right after, gated on the presence score exactly as before — hint
presence/auto, score >= LLM_BBOX_PRESENCE_MIN, a page image to crop — for at
most ``options.max_attempts`` attempts. It never changes the score.

    evaluate()       — the shared evaluator interface.
    evaluate_with()  — the same for a name and RESOLVED llm options, which is
                       how a `cv` criterion's "llm" fallback reaches it.
    _budgeted()      — the text layer truncated to the prompt budget.

Every call goes through ``llm.client``, so the process-wide
CLASSIFIER_MAX_LLM_CALLS limit bounds them all.

Process flow position: one of the four evaluators ``analysis.scheduler``
dispatches to; also called by ``analysis.cv_eval``.
"""

from __future__ import annotations

from analysis.context import DocumentContext
from analysis.outcome import Outcome, empty_localization
from api.schemas import CriterionInput
from config import TEXT_CHAR_BUDGET
# Modules, not names: a test scripts the model by replacing
# `llm.client.call_vllm` / `call_vllm_json`, and must reach the one object
# every caller uses.
from llm import boxes as llm_boxes
from llm import client as llm_client
from llm.prompts import build_llm_prompt
from llm.validate import answer_validator
from logger import logger


def _budgeted(text: str) -> tuple[str, bool]:
    """The text truncated to CLASSIFIER_TEXT_CHAR_BUDGET, and whether it was."""
    if TEXT_CHAR_BUDGET and len(text) > TEXT_CHAR_BUDGET:
        logger.info(
            "llm_eval: truncating %d chars to the %d-char budget", len(text), TEXT_CHAR_BUDGET
        )
        return text[:TEXT_CHAR_BUDGET], True
    return text, False


async def evaluate(c: CriterionInput, ctx: DocumentContext) -> Outcome:
    """Score ``c`` with one model call; locate it when ``options.boxes``."""
    return await evaluate_with(c.name, c.resolved_options(), ctx)


async def evaluate_with(name: str, opts: dict, ctx: DocumentContext) -> Outcome:
    """One scoring call under RESOLVED llm options, then the loop if asked.

    Raises:
        llm.client.LLMCallError: The scoring call failed (HTTP error, or no
            parseable answer after MAX_LLM_RETRIES). The scheduler turns it
            into ``status: "error"``.
    """
    _, layer = await ctx.text_document(opts["ocr"])
    text, truncated = _budgeted(layer.text)
    image_b64 = ctx.image_b64()

    answer = await llm_client.call_vllm(
        build_llm_prompt(
            image_b64,
            name,
            opts["hint"],
            text,
            document_kind=ctx.doc.kind,
            text_truncated=truncated,
        ),
        label=f"score/{name}",
        validator=answer_validator(name),
    )

    outcome = Outcome(
        method="llm",
        score=answer["score"],
        verdict=answer["verdict"],
        confidence=answer["confidence"],
        reason=answer["reason"],
        detail={
            "hint": opts["hint"],
            "image_sent": image_b64 is not None,
            "text_sent": {
                "chars": len(text),
                "truncated": truncated,
                "budget": TEXT_CHAR_BUDGET,
                "source": layer.source,
            },
        },
        localization=empty_localization(),
        text_layer=ctx.layer_ref(opts["ocr"], layer),
    )

    if not opts["boxes"]:
        return outcome
    if image_b64 is None or ctx.geometry is None:
        logger.info("llm_eval: '%s' asked for boxes but there is no page image", name)
        return outcome
    if not llm_boxes.wants_boxes(opts["hint"], answer):
        logger.info(
            "llm_eval: '%s' not located — hint=%s score=%s (the loop needs "
            "presence/auto and a score >= %d)",
            name, opts["hint"], answer["score"], llm_boxes.LLM_BBOX_PRESENCE_MIN,
        )
        return outcome

    regions, loc = await llm_boxes.locate_criterion(
        name,
        image_b64=image_b64,
        original_image=ctx.page.image_bgr,
        geometry=ctx.geometry,
        max_attempts=opts["max_attempts"],
        working_image=ctx.working_image,
    )
    outcome.regions = regions
    outcome.localization = loc.as_dict()
    return outcome
