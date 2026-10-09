"""The `llm` evaluator: ONE scoring call for one criterion, then maybe boxes.

Each (``llm`` criterion, item) is its own unit of work and its own model
call: the item's page image (one page, so there is no page to choose) plus
the text layer ITS ``options.ocr`` produces, truncated to
CLASSIFIER_TEXT_CHAR_BUDGET, scored on the rubric ITS ``options.hint``
selects. A model that cannot answer fails this criterion alone.

When the request lists ``references`` and they hold examples for this
criterion (``analysis.references.JobReferences.guide``), the one scoring
call becomes one call PER example group: ``plan_calls`` pairs a PASS-side
example with a FAIL example in one contrastive call (three images: positive,
counter-example, candidate) when the model admits three images, else one
example per call (two images). The calls run concurrently, each taking its
own CLASSIFIER_MAX_LLM_CALLS slot, and their answers combine by
``options.reference.combine`` (``any`` by default: the best, with its
reason). A call that fails is left out and noted; when EVERY call fails the
unit fails, exactly as a plain scoring call's failure does. What was shown
and answered is ``detail.reference`` — on every llm-answered criterion of a
request with references, ``applied: false`` (with a note) when no example
matched.

With ``options.boxes`` the bounding-box enforcement loop (``llm.boxes``)
runs right after, gated on the (combined) presence score exactly as before —
hint presence/auto, score >= LLM_BBOX_PRESENCE_MIN, a page image to crop —
for at most ``options.max_attempts`` attempts. It never changes the score,
and it always sends one image: references do not reach the loop.

    evaluate()       — the shared evaluator interface.
    evaluate_with()  — the same for a name and RESOLVED llm options, which is
                       how a `cv` criterion's "llm" fallback reaches it (its
                       ``reference`` guide passed through).
    _guided()        — the reference-guided scoring calls, combined.
    _budgeted()      — the text layer truncated to the prompt budget.

Every call goes through ``llm.client``, so the process-wide
CLASSIFIER_MAX_LLM_CALLS limit bounds them all.

Progress: ``evaluate_with`` checkpoints ``"<name>: scored"`` on the job's
gauge (``common.jobs.progress``) right after the scoring answer — the first
of the two steps ``analysis.scheduler`` gives an llm unit with boxes; the
unit's exit credits the second, located or not.

Process flow position: one of the four evaluators ``analysis.scheduler``
dispatches to; also called by ``analysis.cv_eval``.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from common.jobs import progress

from analysis.context import DocumentContext
from analysis.outcome import Outcome, empty_localization
from analysis.references import CriterionGuide, combine, plan_calls
from analysis.result_specs import (
    AGGREGATE_FIELD,
    METRIC_FIELD,
    REFERENCE_FIELD,
    FieldSpec,
    ResultSpec,
    result_spec,
    with_metric,
)
from api.schemas import CriterionInput
from config import TEXT_CHAR_BUDGET, VISION_LLM_MAX_IMAGES_PER_PROMPT
# Modules, not names: a test scripts the model by replacing
# `llm.client.call_vllm` / `call_vllm_json`, and must reach the one object
# every caller uses.
from llm import boxes as llm_boxes
from llm import client as llm_client
from llm.prompts import ReferenceExample, build_llm_prompt
from llm.validate import answer_validator
from logger import logger
from metrics import reference_calls_total


def _budgeted(text: str) -> tuple[str, bool]:
    """The text truncated to CLASSIFIER_TEXT_CHAR_BUDGET, and whether it was."""
    if TEXT_CHAR_BUDGET and len(text) > TEXT_CHAR_BUDGET:
        logger.info(
            "llm_eval: truncating %d chars to the %d-char budget", len(text), TEXT_CHAR_BUDGET
        )
        return text[:TEXT_CHAR_BUDGET], True
    return text, False


# The detail an `llm` result carries — declared here, beside the code that
# builds it, and registered by type (analysis.result_specs). Checked against
# real output by unit-tests/classifier/test_result_specs.py.
LLM_RESULT = ResultSpec(
    type="llm",
    metric="score",
    metric_from="score",
    fields={
        "metric": METRIC_FIELD,
        "value": FieldSpec(
            "The model's score, 1-10 — null for a `score: false` criterion, whose "
            "judgement is not reported", "number",
        ),
        "hint": FieldSpec("The rubric the model was asked to use", "string", stable=True),
        "image_sent": FieldSpec("Whether the page image was attached", "boolean", stable=True),
        "text_sent": FieldSpec(
            "The text attached: `{chars, truncated, budget, source}`", "object",
        ),
        "reference": REFERENCE_FIELD,
        "aggregate": AGGREGATE_FIELD,
    },
    notes=(
        "The model's answer is the top-level score / verdict / confidence / reason; "
        "detail records what it was shown.",
        "Box-loop results are in `localization`, not `detail`.",
        "A cv criterion answered by the llm fallback has this shape, `reference` included.",
    ),
)


@result_spec(LLM_RESULT)
async def evaluate(c: CriterionInput, ctx: DocumentContext) -> Outcome:
    """Score ``c`` with one model call (or its guided calls); locate it when
    ``options.boxes``."""
    guide = ctx.references.guide(c, ctx.item) if ctx.references is not None else None
    return await evaluate_with(c.name, c.resolved_options(), ctx, reference=guide)


@result_spec(LLM_RESULT)
async def evaluate_with(
    name: str,
    opts: dict,
    ctx: DocumentContext,
    *,
    reference: Optional[CriterionGuide] = None,
) -> Outcome:
    """Score under RESOLVED llm options — one call, or the reference-guided
    calls combined — then the loop if asked.

    Raises:
        llm.client.LLMCallError: The scoring call failed (HTTP error, or no
            parseable answer after MAX_LLM_RETRIES) — for guided calls, every
            one of them did. The scheduler turns it into ``status: "error"``.
        EvaluationError: A guiding reference was deleted after submit.
    """
    _, layer = await ctx.text_document(opts["ocr"])
    text, truncated = _budgeted(layer.text)
    image_b64 = ctx.image_b64()

    def prompt(examples: Optional[list[ReferenceExample]] = None) -> dict:
        return build_llm_prompt(
            image_b64,
            name,
            opts["hint"],
            text,
            document_kind=ctx.doc.kind,
            text_truncated=truncated,
            references=examples,
        )

    reference_detail: Optional[dict] = None
    if reference is not None and reference.applied:
        answer, reference_detail = await _guided(name, reference, ctx, prompt, image_b64)
    else:
        answer = await llm_client.call_vllm(
            prompt(), label=f"score/{name}", validator=answer_validator(name),
        )
        if reference is not None:
            reference_detail = reference.unapplied_detail()
    # The unit's first progress step (of two when it has boxes). Whether the
    # loop then runs or is skipped, the scheduler's task exit credits the
    # second; for a one-step unit (no boxes, or a `cv` criterion's llm
    # fallback) the cap makes this a label update only.
    progress.checkpoint(f"{name}: scored", logger=logger)

    outcome = Outcome(
        method="llm",
        score=answer["score"],
        verdict=answer["verdict"],
        confidence=answer["confidence"],
        reason=answer["reason"],
        # The model's headline number is its score; the scheduler clears
        # `value` for a `score: false` criterion (result_specs.clear_judgement).
        detail=with_metric(
            {
                "hint": opts["hint"],
                "image_sent": image_b64 is not None,
                "text_sent": {
                    "chars": len(text),
                    "truncated": truncated,
                    "budget": TEXT_CHAR_BUDGET,
                    "source": layer.source,
                },
                **({"reference": reference_detail} if reference_detail is not None else {}),
            },
            "llm", answer["score"],
        ),
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
        ask_b64=await ctx.ask_image_b64(),
    )
    outcome.regions = regions
    outcome.localization = loc.as_dict()
    return outcome


async def _guided(
    name: str,
    guide: CriterionGuide,
    ctx: DocumentContext,
    prompt: Any,
    image_b64: Optional[str],
) -> tuple[dict, dict]:
    """The reference-guided scoring calls for one unit, combined.

    Returns ``(answer, detail.reference)``. Raises the first call's error when
    every call failed, and ``EvaluationError`` when a reference is gone.
    """
    examples = guide.examples
    calls = plan_calls(examples, VISION_LLM_MAX_IMAGES_PER_PROMPT)
    images = {i: await ctx.references.image_b64(examples[i]) for call in calls for i in call}

    def shown(i: int) -> ReferenceExample:
        e = examples[i]
        return ReferenceExample(
            criterion=e["criterion"],
            verdict=e["expected"]["verdict"],
            score=int(e["expected"]["score"]),
            reason=e["expected"].get("reason") or "",
            image_b64=images[i],
            whole_page=e.get("region_source") == "whole_page",
        )

    results = await asyncio.gather(
        *(
            llm_client.call_vllm(
                prompt([shown(i) for i in call]),
                label=f"score/{name}/ref{k}",
                validator=answer_validator(name),
            )
            for k, call in enumerate(calls)
        ),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, BaseException) and not isinstance(result, Exception):
            raise result  # a cancel: the job is being torn down
    survivors = [(k, r) for k, r in enumerate(results) if not isinstance(r, BaseException)]
    reference_calls_total.labels(kind="scoring", outcome="ok").inc(len(survivors))
    if len(results) > len(survivors):
        reference_calls_total.labels(kind="scoring", outcome="failed").inc(
            len(results) - len(survivors)
        )
    if not survivors:
        raise next(r for r in results if isinstance(r, BaseException))

    rule = guide.options.get("combine", "any")
    answer, chosen = combine(survivors, rule)
    candidate = 1 if image_b64 else 0
    call_of = {i: k for k, call in enumerate(calls) for i in call}
    call_rows = []
    for k, (call, result) in enumerate(zip(calls, results)):
        failed = isinstance(result, BaseException)
        call_rows.append({
            "call": k,
            "images": len(call) + candidate,
            "score": None if failed else result["score"],
            "verdict": None if failed else result["verdict"],
            "confidence": None if failed else result["confidence"],
            "reason": None if failed else result["reason"],
            "chosen": (k == chosen) if rule != "mean" else not failed,
            "error": str(result) if failed else None,
        })
    failed_calls = sum(1 for row in call_rows if row["error"])
    note = (
        f"{failed_calls} of {len(calls)} reference-guided calls failed; the answer "
        "combines the rest"
        if failed_calls else None
    )
    ctx.references.record_calls(name, ctx.item, [
        {
            "call": k,
            "images": call_rows[k]["images"],
            "examples": [
                {"reference_id": examples[i]["reference_id"],
                 "criterion": examples[i]["criterion"],
                 "composite": examples[i]["composite"],
                 "polarity": examples[i]["polarity"]}
                for i in call
            ],
            "score": call_rows[k]["score"],
            "error": call_rows[k]["error"],
        }
        for k, call in enumerate(calls)
    ])
    logger.info(
        "llm_eval: '%s' guided by %d example(s) in %d call(s), combine=%s → %s",
        name, len(examples), len(calls), rule, answer["score"],
    )
    detail = {
        "applied": True,
        "mode": guide.mode,
        "combine": rule,
        "examples": [
            {
                "reference_id": e["reference_id"],
                "criterion": e["criterion"],
                "expected": {"score": e["expected"]["score"],
                             "verdict": e["expected"]["verdict"]},
                "polarity": e["polarity"],
                "region": e.get("region_source"),
                "call": call_of.get(i),
            }
            for i, e in enumerate(examples)
        ],
        "calls": call_rows,
        "position": None,
        "note": note,
    }
    return answer, detail
