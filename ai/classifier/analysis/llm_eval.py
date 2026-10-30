"""The `llm` evaluator: ONE scoring call for one criterion, then maybe boxes.

Each (``llm`` criterion, item) is its own unit of work and its own model
call, sent to ITS model — ``options.model``, else the job's ``model``
(``DocumentContext.default_model``), else the registry default
(``models.yaml``): the item's page image (one page, so there is no page to
choose) plus the text layer ITS ``options.ocr`` produces, truncated to that
model's ``text_char_budget``, scored on the rubric ITS ``options.hint``
selects. A model that cannot answer fails this criterion alone. The model's
registry id is ``detail.model``.

A TEXT-ONLY model (``vision: false``) is sent the text layer and no image;
an item with no text at all fails the unit ("no text available for a
text-only model"), and the box loop never runs for it (the submit refuses
boxes on such a model; the gate below is the second line).

When the request lists ``references`` and they hold examples for this
criterion (``analysis.references.JobReferences.guide``), the one scoring
call becomes one call PER example group: ``plan_calls`` pairs a PASS-side
example with a FAIL example in one contrastive call (three images: positive,
counter-example, candidate) when the model admits three images, else one
example per call (two images). The calls run concurrently, each taking its
own slot of the model's ``max_concurrent``, and their answers combine by
``options.reference.combine`` (``any`` by default: the best, with its
reason). A call that fails is left out and noted; when EVERY call fails the
unit fails, exactly as a plain scoring call's failure does. What was shown
and answered is ``detail.reference`` — on every llm-answered criterion of a
request with references, ``applied: false`` (with a note) when no example
matched.

With ``options.boxes`` the bounding-box enforcement loop (``llm.boxes``)
runs right after, gated on the (combined) presence score exactly as before —
hint presence/auto, score >= LLM_BBOX_PRESENCE_MIN, a page image to crop —
for at most ``options.max_attempts`` attempts. It never changes the score.
When the guide applied and a CLASSIFIER_REFERENCE_BOX_* knob is on, the
examples the loop may show are built AFTER the gate passed
(``JobReferences.box_examples``: regions only, FAIL only under a presence
hint, composites for the ask — region crops under the position check — and
region crops for refine and verify) and handed to the loop, which shows each
step its examples when that step's knob is on. Every unit of a criterion
with a guide records what the loop was shown as ONE
``localization.references`` entry tagged with its ``item`` (``applied:
false`` with a note when nothing was shown — the knobs off, no example with
a region, a load failure), each call that carried examples in
``references.json`` (``kind: box_ask | box_refine | box_verify``) and in
``classifier_reference_calls_total``. Without references in the request the
key is absent and the loop sends one image, exactly as before.

    evaluate()       — the shared evaluator interface.
    evaluate_with()  — the same for a name and RESOLVED llm options, which is
                       how a `cv` criterion's "llm" fallback reaches it (its
                       ``reference`` guide passed through).
    _guided()        — the reference-guided scoring calls, combined.
    _box_reference_entry() — what the loop was shown: the unit's
                       ``localization.references`` entry, references.json
                       rows and the metric.
    _budgeted()      — the text layer truncated to the prompt budget.

Every call goes through ``llm.client``, so the model's ``max_concurrent``
limit bounds them all.

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
from analysis.references import BoxGuide, CriterionGuide, combine, plan_calls
from analysis.result_specs import (
    AGGREGATE_FIELD,
    METRIC_FIELD,
    REFERENCE_FIELD,
    FieldSpec,
    ResultSpec,
    result_spec,
    with_metric,
)
from analysis.outcome import EvaluationError
from api.schemas import CriterionInput
# Modules, not names: a test scripts the model by replacing
# `llm.client.call_model` / `call_model_json`, and must reach the one object
# every caller uses; the registry is looked up at call time (tests swap it).
from llm import boxes as llm_boxes
from llm import client as llm_client
from llm import confidence as llm_confidence
from llm import models as llm_models
from llm.models import ModelSpec
from llm.prompts import ReferenceExample, build_llm_prompt
from llm.validate import answer_validator
from logger import logger
from metrics import reference_calls_total


def _budgeted(text: str, budget: int) -> tuple[str, bool]:
    """The text truncated to the model's ``text_char_budget``, and whether it was."""
    if budget and len(text) > budget:
        logger.info("llm_eval: truncating %d chars to the %d-char budget", len(text), budget)
        return text[:budget], True
    return text, False


def model_for(opts: dict, ctx: DocumentContext) -> ModelSpec:
    """The registry entry a unit runs on: ``options.model``, else the job's
    model, else the registry default."""
    return llm_models.get_registry().resolve(
        opts.get("model") or getattr(ctx, "default_model", None)
    )


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
        "model": FieldSpec(
            "The registry id of the model that answered (GET /models): options.model, "
            "else the request's model, else the registry default", "string", stable=True,
        ),
        "image_sent": FieldSpec(
            "Whether the page image was attached (never, for a text-only model)", "boolean",
            stable=True,
        ),
        "text_sent": FieldSpec(
            "The text attached: `{chars, truncated, budget, source}`", "object",
        ),
        "confidence": FieldSpec(
            "How sure the model was, measured per models.yaml `confidence.method` "
            "(llm.confidence): `{method, source: logprobs | sampling | stated, headline: "
            "stated | calibrated, stated, stated_defaulted, calibrated, "
            "verdict_probability, band: {PASS, MARGINAL, FAIL}, distribution: "
            "{\"1\"..\"10\"}, expected_score, entropy_bits, certainty, coverage, "
            "unresolved: [{values, mass}], samples: null | {asked, answered, agreement, "
            "stated_mean}, note}` — the distribution keys are null when source is "
            "`stated`; the whole block is null for a `score: false` criterion",
            "object",
        ),
        "reference": REFERENCE_FIELD,
        "aggregate": AGGREGATE_FIELD,
    },
    notes=(
        "The model's answer is the top-level score / verdict / confidence / reason; "
        "detail records what it was shown.",
        "The top-level confidence is detail.confidence.stated unless the model's "
        "models.yaml entry says `headline: calibrated` and this answer has the "
        "calibration's signal (then detail.confidence.calibrated).",
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
        EvaluationError: A guiding reference was deleted after submit, or the
            model is text-only and the item has no text at all.
    """
    spec = model_for(opts, ctx)
    _, layer = await ctx.text_document(opts["ocr"])
    text, truncated = _budgeted(layer.text, spec.text_char_budget)
    # A text-only model is never shown the page: build_llm_prompt drops the
    # image for it (and words the prompt for text); `image_b64` here is what
    # was SENT.
    page_b64 = ctx.image_b64()
    image_b64 = page_b64 if spec.vision else None
    if not spec.vision and not text.strip():
        raise EvaluationError(
            f"no text available for a text-only model ({spec.id!r}): this page has no "
            f"text layer (source {layer.source!r}) — use a vision model, or OCR"
        )

    def prompt(examples: Optional[list[ReferenceExample]] = None) -> dict:
        return build_llm_prompt(
            page_b64,
            name,
            opts["hint"],
            text,
            document_kind=ctx.doc.kind,
            text_truncated=truncated,
            references=examples,
            model=spec,
        )

    reference_detail: Optional[dict] = None
    if reference is not None and reference.applied:
        answer, reference_detail = await _guided(name, reference, ctx, prompt, image_b64, spec)
    else:
        answer = await llm_client.call_model(
            prompt(), label=f"score/{name}", validator=answer_validator(name), model=spec,
        )
        if reference is not None:
            reference_detail = reference.unapplied_detail()
    # The unit's first progress step (of two when it has boxes). Whether the
    # loop then runs or is skipped, the scheduler's task exit credits the
    # second; for a one-step unit (no boxes, or a `cv` criterion's llm
    # fallback) the cap makes this a label update only.
    progress.checkpoint(f"{name}: scored", logger=logger)

    # What the model's own sampling says about this answer (models.yaml
    # `confidence`), and which number is the headline: the stated one, until
    # the entry is flipped to `headline: calibrated` with a fitted curve.
    confidence_block, headline = llm_confidence.assess(answer, spec.confidence)

    outcome = Outcome(
        method="llm",
        score=answer["score"],
        verdict=answer["verdict"],
        confidence=headline,
        reason=answer["reason"],
        # The model's headline number is its score; the scheduler clears
        # `value` for a `score: false` criterion (result_specs.clear_judgement).
        detail=with_metric(
            {
                "hint": opts["hint"],
                "model": spec.id,
                "image_sent": image_b64 is not None,
                "text_sent": {
                    "chars": len(text),
                    "truncated": truncated,
                    "budget": spec.text_char_budget,
                    "source": layer.source,
                },
                "confidence": confidence_block,
                **({"reference": reference_detail} if reference_detail is not None else {}),
            },
            "llm", answer["score"],
        ),
        localization=empty_localization(),
        text_layer=ctx.layer_ref(opts["ocr"], layer),
    )

    if not opts["boxes"]:
        return outcome
    if not spec.supports_boxes:
        # Refused at submit; the second line, should the registry have
        # changed between the submit and this run.
        logger.info("llm_eval: '%s' asked for boxes but model %s is text-only", name, spec.id)
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

    # The loop's reference examples: built only now (the gate passed) and
    # only when a knob would show them, so a page that is not located — or a
    # deployment with the knobs off — never decodes a reference page.
    box_guide: Optional[BoxGuide] = None
    knobs_on = (
        llm_boxes.REFERENCE_BOX_ASK or llm_boxes.REFERENCE_BOX_REFINE
        or llm_boxes.REFERENCE_BOX_VERIFY
    )
    if reference is not None and reference.applied and knobs_on:
        box_guide = await ctx.references.box_examples(
            reference, spec,
            position_check=(opts.get("reference") or {}).get("position") == "check",
        )

    regions, loc = await llm_boxes.locate_criterion(
        name,
        image_b64=image_b64,
        original_image=ctx.page.image_bgr,
        geometry=ctx.geometry,
        max_attempts=opts["max_attempts"],
        working_image=ctx.working_image,
        ask_b64=await ctx.ask_image_b64(),
        model=spec,
        ask_examples=box_guide.ask if box_guide else None,
        crop_examples=box_guide.crops if box_guide else None,
    )
    outcome.regions = regions
    outcome.localization = loc.as_dict()
    if reference is not None:
        outcome.localization["references"] = [
            _box_reference_entry(name, ctx, reference, box_guide, loc, knobs_on)
        ]
    return outcome


def _box_reference_entry(
    name: str,
    ctx: DocumentContext,
    guide: CriterionGuide,
    box_guide: Optional[BoxGuide],
    loc: llm_boxes.Localization,
    knobs_on: bool,
) -> dict:
    """One unit's ``localization.references`` entry — what the box loop was
    shown — after recording each call that carried examples in
    ``references.json`` and ``classifier_reference_calls_total``.

    Tagged with its ``item`` here, so the aggregate merge is plain
    concatenation. ``applied`` is whether any step carried examples; when
    none did, the ``note`` says why (the guide's own note, the knobs, or
    ``box_examples``')."""
    examples = box_guide.records if box_guide else []
    for call in loc.guided_calls:
        reference_calls_total.labels(
            kind=call["kind"], outcome="ok" if call["ok"] else "failed",
        ).inc()
    if loc.guided_calls:
        ctx.references.record_calls(name, ctx.item, [
            {
                "kind": call["kind"],
                "attempt": call["attempt"],
                "images": call["images"],
                "example_images": (
                    box_guide.ask_images if call["kind"] == "box_ask" else "crop"
                ),
                "examples": examples,
                "ok": call["ok"],
            }
            for call in loc.guided_calls
        ])
    applied = any(loc.guided.values())
    if not guide.applied:
        note = guide.note
    elif not knobs_on:
        note = "the CLASSIFIER_REFERENCE_BOX_* knobs are off"
    else:
        note = box_guide.note if box_guide else None
    return {
        "item": ctx.item,
        "applied": applied,
        "ask_images": box_guide.ask_images if loc.guided["ask"] else None,
        "examples": examples,
        "ask": loc.guided["ask"],
        "refine": loc.guided["refine"],
        "verify": loc.guided["verify"],
        "note": note,
    }


async def _guided(
    name: str,
    guide: CriterionGuide,
    ctx: DocumentContext,
    prompt: Any,
    image_b64: Optional[str],
    spec: ModelSpec,
) -> tuple[dict, dict]:
    """The reference-guided scoring calls for one unit, combined, planned to
    fit ``spec.max_images_per_prompt``.

    Returns ``(answer, detail.reference)``. Raises the first call's error when
    every call failed, and ``EvaluationError`` when a reference is gone.
    """
    examples = guide.examples
    calls = plan_calls(examples, spec.max_images_per_prompt)
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
            llm_client.call_model(
                prompt([shown(i) for i in call]),
                label=f"score/{name}/ref{k}",
                validator=answer_validator(name),
                model=spec,
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
    if rule == "mean":
        # `combine` averages the scores (analysis.references); the measured
        # confidence of that averaged answer is the MIXTURE of the calls'
        # distributions (any / all keep the chosen call's answer whole, its
        # measurement with it).
        answer = llm_confidence.combined_answer(answer, [r for _, r in survivors])
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
            "verdict_probability": (
                None if failed
                else llm_confidence.assess(result, spec.confidence)[0]["verdict_probability"]
            ),
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
            "kind": "scoring",
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
