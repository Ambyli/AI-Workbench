"""The `llm` evaluator: ONE scoring call for one criterion, then maybe boxes.

Each (``llm`` criterion, item) is its own unit of work and its own model
call, sent to ITS model — ``options.model``, else the job's ``model``
(``DocumentContext.default_model``), else the registry default
(``models.yaml``): the item's page image (one page, so there is no page to
choose) plus the text layer ITS ``options.ocr`` produces, truncated to that
model's ``text_char_budget``, scored on the rubric ITS ``options.hint``
selects. A model that cannot answer fails this criterion alone. The model's
registry id is ``detail.model``.

**``options.mode: "fast"``** replaces that scoring call with ONE readout
(``_fast``): the same prompt prefix asking for a digit 0-9 with reasoning off
(``build_llm_prompt(style="fast")``, ``ModelSpec.fast_body``), read from the
token's probabilities (``llm.client.call_readout`` — posted once, never
retried) and rated by ``llm.confidence.rate_readout``: the band with the most
mass is the verdict, the most probable score inside it the score,
``round(100 × that mass)`` the confidence, a service-written line of the
band probabilities the reason (for a ``score: false`` criterion, whose
judgement is not reported, the line without them); ``detail.confidence``
keeps its shape with source ``readout`` (plus ``unseen_bound``). An
incomplete or capped readout
fails the unit, naming mode ``reasoned``. A fast criterion is never guided by
references (its ``detail.reference``, in a request with references, is
``applied: false`` with the reason). ``detail.mode`` echoes ``options.mode``
only when it was sent. The box loop after a fast score runs reasoned,
exactly as it does after a reasoned one.

**``options.mode: "adaptive"``** (``_adaptive``) runs that readout, then —
when its confidence is under the threshold (``options.escalate_below``, else
the model's ``fast.escalate_below.rating``) or it was incomplete / capped —
today's REASONED scoring call on the registry's ESCALATION model
(``CLASSIFIER_ESCALATION_MODEL``; ``_escalate``, label ``escalate/<name>``,
call kind ``escalate``): its text budget, vision, reasoning and confidence
method, never reference-guided. The escalated answer is the unit's
(``detail.model`` the escalation id, ``detail.mode`` ``reasoned``), else the
readout's (``detail.mode`` ``fast``); ``detail.escalation`` records whether,
why, the threshold and signal, a failure and — when it escalated — the
readout it replaced. A failed escalation keeps a complete readout's answer;
only an unreadable readout whose escalation failed errors the unit. The box
loop stays on the criterion's own model, reasoned. ``detail.escalations`` is
the page's tally (units 1: escalated, failed, why — ``_unit_escalations``);
its declaration's ``rebuild`` (``combine_escalations``) sums the members'
at every aggregate level, so a criterion over five pages reads "escalated on
2 of 5 pages".

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

**The loop runs with the CRITERION's settings**
(``api.criterion_options.box_loop_settings`` over its resolved options):
``options.refine``, ``options.verify_partial``, ``options.verify_min_span``,
``options.reference.boxes`` and ``options.reference.text``, each already
resolved to the criterion's value or, unset, the service default (the
CLASSIFIER_LLM_BBOX_* / CLASSIFIER_REFERENCE_BOX_* env vars), and passed to
the loop and the example builder EXPLICITLY — so two criteria on one page
can run the loop two ways, and ``options_used`` says how each ran. The loop's
own env defaults are for direct callers only. With nothing set and the env
defaults off, every keyword is today's value and every prompt is today's.

When the guide applied and the criterion's ``reference.boxes`` turns a step
on, the examples the loop may show are built AFTER the gate passed
(``JobReferences.box_examples``: regions only, FAIL only under a presence
hint, composites for the ask — region crops under the position check — and
region crops for refine and verify; with ``reference.text`` each one's region
text) and handed to the loop, which shows each step its examples when that
step is on. Every unit of a criterion with a guide records what the loop was
shown as ONE ``localization.references`` entry tagged with its ``item``
(``applied: false`` with a note when nothing was shown — every step off, no
example with a region, a load failure), each call that carried examples in
``references.json`` (``kind: box_ask | box_refine | box_verify``) and in
``classifier_reference_calls_total``. Without references in the request the
key is absent and the loop sends one image, exactly as before.

The loop is also handed the marks refine's (``options.refine: "marks"``)
three tiers of text lines, ORIGINAL page pixels each, and numbers the first
that has one in its window: the page's own PDF / SVG lines
(``TextLayer.native_lines`` — exact, so a digital PDF needs no OCR at all),
the line boxes of the text layer the scoring call read (``TextLayer.lines``
— no second pass of the page), then an OCR pass of the window alone
(``DocumentContext.window_lines``, injected as a callable so ``llm`` never
imports ``analysis``) — left out under the criterion's ``options.ocr:
"never"``, which promised no recognition. In grid mode none is read.

    evaluate()       — the shared evaluator interface.
    evaluate_with()  — the same for a name and RESOLVED llm options, which is
                       how a `cv` criterion's "llm" fallback reaches it (its
                       ``reference`` guide passed through).
    _guided()        — the reference-guided scoring calls, combined.
    _readout()       — the one-token readout, rated (fast and adaptive).
    _fast()          — ``options.mode: "fast"``: the readout or a unit error.
    _adaptive()      — ``options.mode: "adaptive"``: the readout, escalated
                       when unsure (``_escalate``, the reasoned call on the
                       escalation model).
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
from dataclasses import dataclass
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
from api.criterion_options import box_loop_settings
from api.schemas import CriterionInput
from config import (
    ADAPTIVE_BOTH_FAILED,
    DEFAULT_ANSWER_TYPE,
    DETAIL_FLOAT_DECIMALS,
    ESCALATION_NO_TEXT,
    ESCALATION_UNAVAILABLE_AT_RUN,
    ESCALATION_UNPARSED_UNJUDGED,
    ESCALATION_WHY_CONFIDENCE,
    ESCALATION_WHY_CONFIDENCE_UNJUDGED,
    ESCALATIONS_FAILED_NOUNS,
    ESCALATIONS_SUMMARY,
    ESCALATIONS_SUMMARY_FAILED,
    ESCALATIONS_UNIT_NOUNS,
    FAST_DIGITS,
    FAST_REASON_UNJUDGED,
    FAST_UNIT_ERROR_FIX,
    MAX_LLM_RETRIES,
    READOUT_MODES,
)
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
from metrics import escalations_total, reference_calls_total


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


# The keys of detail.escalations — the adaptive tally LLM_RESULT's
# `escalations` field describes — and of its `why`: the reasons a unit
# escalated or tried to, the `why` label values of
# classifier_escalations_total (metrics.py). For the tests and for callers.
ESCALATIONS_KEYS: tuple[str, ...] = (
    "units", "escalated", "escalation_failed", "readout_failed", "why", "models", "summary",
)
ESCALATIONS_WHY_KEYS: tuple[str, ...] = ("confidence", "incomplete", "cap")


def _plural(nouns: tuple[str, str], count: int) -> str:
    return nouns[0] if count == 1 else nouns[1]


def _escalations(
    *, units: int, escalated: int, failed: int, readout_failed: int,
    why: dict[str, int], models: list[str],
) -> dict:
    """``detail.escalations`` from its counts, the summary written from them
    (``config.ESCALATIONS_SUMMARY``, its failed tail only when one failed)."""
    summary = ESCALATIONS_SUMMARY.format(
        escalated=escalated, units=units, unit_noun=_plural(ESCALATIONS_UNIT_NOUNS, units),
    )
    if failed:
        summary += ESCALATIONS_SUMMARY_FAILED.format(
            failed=failed, failed_noun=_plural(ESCALATIONS_FAILED_NOUNS, failed),
        )
    return {
        "units": units,
        "escalated": escalated,
        "escalation_failed": failed,
        "readout_failed": readout_failed,
        "why": {key: int(why.get(key, 0)) for key in ESCALATIONS_WHY_KEYS},
        "models": models,
        "summary": summary,
    }


def _unit_escalations(
    *, why: Optional[str] = None, answered_by: Optional[str] = None,
    readout_failed: bool = False,
) -> dict:
    """One adaptive unit's ``detail.escalations`` — itself, counted once
    (units 1). ``why`` is the reason it tried to escalate (an
    ``ESCALATIONS_WHY_KEYS`` key), None when the readout stood;
    ``answered_by`` the escalation model's id when its answer is the unit's,
    None when the escalation failed; ``readout_failed`` an incomplete /
    capped readout (which only reaches here escalated: with a failed
    escalation the unit errors)."""
    tried = why is not None
    escalated = answered_by is not None
    return _escalations(
        units=1,
        escalated=int(escalated),
        failed=int(tried and not escalated),
        readout_failed=int(readout_failed),
        why={why: 1} if tried else {},
        models=[answered_by] if escalated else [],
    )


def combine_escalations(tallies: list[dict]) -> dict:
    """``detail.escalations`` of an aggregate — the ``rebuild`` of its
    declaration below (``result_specs.aggregated_detail`` calls it under
    every rule): the counted members' tallies summed. A member is a unit
    (units 1) or a document's aggregate (its pages already summed), so
    ``units`` counts the LEAF units — the pages — never the documents.
    ``models`` keeps each escalation id once, in member order; the summary
    is rewritten from the sums."""
    models: list[str] = []
    for tally in tallies:
        for model_id in tally["models"]:
            if model_id not in models:
                models.append(model_id)
    return _escalations(
        units=sum(t["units"] for t in tallies),
        escalated=sum(t["escalated"] for t in tallies),
        failed=sum(t["escalation_failed"] for t in tallies),
        readout_failed=sum(t["readout_failed"] for t in tallies),
        why={key: sum(t["why"][key] for t in tallies) for key in ESCALATIONS_WHY_KEYS},
        models=models,
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
            "judgement is not reported", "number", nullable=True,
        ),
        "hint": FieldSpec("The rubric the model was asked to use", "string", stable=True),
        "model": FieldSpec(
            "The registry id of the model that answered (GET /models): options.model, "
            "else the request's model, else the registry default — under options.mode "
            "'adaptive', the escalation model's id when it escalated "
            "(detail.escalation); under a `mean` aggregate, the first member's (adaptive "
            "members may differ — each unit's is in items[])", "string", stable=True,
        ),
        "image_sent": FieldSpec(
            "Whether the page image was attached to the call that answered (never, for "
            "a text-only model); under a `mean` aggregate, the first member's (each "
            "unit's is in items[])", "boolean",
            stable=True,
        ),
        "text_sent": FieldSpec(
            "The text attached to the call that answered: `{chars, truncated, budget, "
            "source}` (budget: that model's text_char_budget)", "object",
        ),
        "confidence": FieldSpec(
            "How sure the model was, measured per models.yaml `confidence.method` "
            "(llm.confidence): `{method, source: logprobs | sampling | stated | readout, "
            "headline: stated | calibrated | readout, stated, stated_defaulted, calibrated, "
            "verdict_probability, band: {PASS, MARGINAL, FAIL}, distribution: "
            "{\"1\"..\"10\"}, expected_score, entropy_bits, certainty, coverage, "
            "unresolved: [{values, mass}], samples: null | {asked, answered, agreement, "
            "stated_mean}, note}` — the distribution keys are null when source is "
            "`stated`; under options.mode 'fast' method and source are `readout` (the "
            "one-token readout's digit probabilities, score = digit + 1), `stated` is "
            "null, headline is `readout` (or `calibrated` after the entry's "
            "fast.calibration.rating), and the block adds `unseen_bound` (an estimate of "
            "what the digits missing from the top-k hold); under options.mode 'adaptive' "
            "it is the readout's block when the readout stood and the escalation model's "
            "measured block when it escalated; the whole block is null for a "
            "`score: false` criterion",
            "object", nullable=True,
        ),
        "mode": FieldSpec(
            "How the model answered: `reasoned` | `fast` — the criterion's options.mode "
            "as sent; under 'adaptive', `fast` when the readout stood and `reasoned` when "
            "it escalated; under a `mean` aggregate, the first member's (adaptive members "
            "may differ — each unit's is in items[], and detail.escalations counts them)",
            "string", stable=True,
            when="options.mode was sent",
        ),
        "escalation": FieldSpec(
            "What the adaptive mode did: `{escalated, model, threshold, signal, why, "
            "error, fast}` — `model` the escalation model's id (CLASSIFIER_ESCALATION_MODEL), "
            "`threshold` the 0-100 confidence it escalates under (options.escalate_below, "
            "else the model's fast.escalate_below.rating), `signal` the readout's "
            "confidence (null when the readout was incomplete / capped, and under "
            "`score: false`), `why` null | 'confidence 62 < 80' | the readout's "
            "'readout incomplete: …' / 'probability cap reached: …', `error` null or why "
            "the escalation failed (the readout's answer then stands, escalated false; "
            "under `score: false` an unparseable answer is not quoted), "
            "`fast` null or — only when it escalated — the readout it replaced: "
            "`{model, score, verdict, confidence, coverage}` (score / verdict / confidence "
            "null when the readout was incomplete, and under `score: false`)",
            "object", when="options.mode was 'adaptive'",
        ),
        "escalations": FieldSpec(
            "What the adaptive mode did across the pages this result covers: `{units, "
            "escalated, escalation_failed, readout_failed, why: {confidence, incomplete, cap}, "
            "models, summary}` — `units` the pages counted (a unit counts itself, units 1; an "
            "aggregate sums its members', so a criterion's are its documents' pages added; an "
            "errored or skipped unit is excluded, as from the aggregate's `values`), "
            "`escalated` those the escalation model answered, `escalation_failed` those whose "
            "escalation failed (the readout's answer stood), `readout_failed` those whose "
            "readout could not be read (incomplete / capped — each escalated), `why` each "
            "page that escalated or tried to, by reason (they add up to escalated + "
            "escalation_failed), `models` the escalation model ids that answered, `summary` "
            "e.g. 'escalated on 2 of 5 pages' (+ ' and 1 escalation failed'). Rebuilt at "
            "every aggregate level from all its members, under every rule (never one "
            "member's), and kept under `mean` and under `score: false` (counts, not a "
            "judgement)",
            "object", when="options.mode was 'adaptive'", rebuild=combine_escalations,
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
        "Under options.mode 'fast' the answer is a one-token readout: the verdict is the "
        "band with the most probability, the score the most probable score inside it, "
        "the confidence round(100 × that band's probability) and the reason a "
        "service-written line of the three band probabilities (without them for a "
        "`score: false` criterion).",
        "Under options.mode 'adaptive' the readout answers unless its confidence is under "
        "the threshold or it could not be read; then the escalation model's reasoned "
        "scoring call answers (detail.model, detail.confidence and every top-level field "
        "are its), never guided by references. A failed escalation keeps a complete "
        "readout's answer (detail.escalation.error); the unit errors only when the "
        "readout was incomplete too. detail.escalations counts what happened over every "
        "page the result covers ('escalated on 2 of 5 pages'); each page's "
        "detail.escalation is in items[].",
        "Box-loop results are in `localization`, not `detail`.",
        "A cv criterion answered by the llm fallback has this shape, `reference` included.",
    ),
)

# The keys of detail.escalation and of its `fast` (the readout an escalation
# replaced) — the shape LLM_RESULT's `escalation` field describes, built by
# _adaptive below; for the tests and for callers.
ESCALATION_KEYS: tuple[str, ...] = (
    "escalated", "model", "threshold", "signal", "why", "error", "fast",
)
ESCALATION_FAST_KEYS: tuple[str, ...] = ("model", "score", "verdict", "confidence", "coverage")


@result_spec(LLM_RESULT)
async def evaluate(c: CriterionInput, ctx: DocumentContext) -> Outcome:
    """Score ``c`` with one model call (or its guided calls); locate it when
    ``options.boxes``."""
    guide = ctx.references.guide(c, ctx.item) if ctx.references is not None else None
    return await evaluate_with(c.name, c.resolved_options(), ctx, reference=guide, judged=c.score)


@result_spec(LLM_RESULT)
async def evaluate_with(
    name: str,
    opts: dict,
    ctx: DocumentContext,
    *,
    reference: Optional[CriterionGuide] = None,
    judged: bool = True,
) -> Outcome:
    """Score under RESOLVED llm options — one call, or the reference-guided
    calls combined — then the loop if asked.

    ``judged`` is the criterion's ``score``. The scheduler clears a ``score:
    false`` unit's judgement afterwards (``analysis.scheduler._unjudged``) but
    keeps its reason — a model-written explanation. A fast readout's reason is
    nothing but its band probabilities, i.e. the judgement, so an unjudged
    fast unit is given ``config.FAST_REASON_UNJUDGED`` instead. The adaptive
    escalation decision still reads the readout's confidence (the scheduler
    then clears ``detail.escalation``'s judgement too); an adaptive readout
    that stood is unjudged ``FAST_REASON_UNJUDGED`` as well, an escalated one
    keeps the escalation model's reason.

    Raises:
        llm.client.LLMCallError: The scoring call failed (HTTP error, or no
            parseable answer after MAX_LLM_RETRIES) — for guided calls, every
            one of them did; a readout's own request failed. The scheduler
            turns it into ``status: "error"``.
        EvaluationError: A guiding reference was deleted after submit, the
            model is text-only and the item has no text at all, a fast
            readout was incomplete / capped, or an adaptive one was AND its
            escalation failed.
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

    mode = opts.get("mode")
    reference_detail: Optional[dict] = None
    # What the reported answer's call was: its model and what it was sent —
    # the criterion's own unless an adaptive readout escalated.
    answered = _Sent(spec, image_b64 is not None, _text_sent(text, truncated, spec, layer))
    detail_mode = mode
    escalation_detail: Optional[dict] = None
    escalations_detail: Optional[dict] = None
    if mode == "fast":
        # The one-token readout: never guided (a fast criterion's guide is
        # an empty one whose note says so), posted once, no retry.
        answer, confidence_block, headline = await _fast(
            name, opts, spec, page_b64, text, document_kind=ctx.doc.kind,
            text_truncated=truncated,
        )
        if not judged:
            # The band line IS the judgement `score: false` withholds.
            answer["reason"] = FAST_REASON_UNJUDGED
        if reference is not None:
            reference_detail = reference.unapplied_detail()
    elif mode == "adaptive":
        # The readout, then — when it is unsure or unreadable — the reasoned
        # call on the escalation model. Never guided (an empty guide whose
        # note says so), as fast.
        result = await _adaptive(
            name, opts, spec, ctx, page_b64, text, layer, text_truncated=truncated,
            judged=judged,
        )
        answer, confidence_block, headline = result.answer, result.block, result.headline
        escalation_detail = result.escalation
        escalations_detail = result.escalations
        detail_mode = result.mode
        if result.sent is not None:
            answered = result.sent
        elif not judged:
            # The readout stood: its band line is the withheld judgement.
            answer["reason"] = FAST_REASON_UNJUDGED
        if reference is not None:
            reference_detail = reference.unapplied_detail()
    elif reference is not None and reference.applied:
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

    if mode not in READOUT_MODES:
        # What the model's own sampling says about this answer (models.yaml
        # `confidence`), and which number is the headline: the stated one,
        # until the entry is flipped to `headline: calibrated` with a fitted
        # curve.
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
                "model": answered.spec.id,
                "image_sent": answered.image_sent,
                "text_sent": answered.text_sent,
                "confidence": confidence_block,
                # Only when the criterion sent options.mode, so a result
                # without it is exactly what it always was.
                **({"mode": detail_mode} if detail_mode is not None else {}),
                **({"escalation": escalation_detail} if escalation_detail is not None else {}),
                # The page's own tally (units 1); every aggregate level
                # rebuilds it from its members (combine_escalations).
                **({"escalations": escalations_detail} if escalations_detail is not None
                   else {}),
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

    # The loop's settings for THIS criterion: its own values, else the
    # service defaults (resolve() filled them in), passed explicitly.
    loop = box_loop_settings(opts)
    # The loop's reference examples: built only now (the gate passed) and
    # only when a step would show them, so a page that is not located — or a
    # criterion whose reference.boxes leaves every step off — never decodes
    # a reference page.
    box_guide: Optional[BoxGuide] = None
    knobs_on = loop["guide_ask"] or loop["guide_refine"] or loop["guide_verify"]
    if reference is not None and reference.applied and knobs_on:
        box_guide = await ctx.references.box_examples(
            reference, spec,
            position_check=(opts.get("reference") or {}).get("position") == "check",
            with_text=loop["reference_text"],
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
        guide_ask=loop["guide_ask"],
        guide_refine=loop["guide_refine"],
        guide_verify=loop["guide_verify"],
        # The marks refine's tiers, all in ORIGINAL page pixels (the frame
        # `original_image` is in), read only in marks mode: the page's own
        # PDF / SVG lines, the OCR layer this criterion's options.ocr
        # already produced, then OCR of the window alone — never under
        # ocr: "never", which asked for no recognition.
        native_lines=layer.native_lines,
        text_lines=layer.lines,
        window_lines=None if opts["ocr"] == "never" else ctx.window_lines,
        refine_mode=loop["refine_mode"],
        verify_partial=loop["verify_partial"],
        verify_min_span=loop["verify_min_span"],
    )
    outcome.regions = regions
    outcome.localization = loc.as_dict()
    if reference is not None:
        outcome.localization["references"] = [
            _box_reference_entry(name, ctx, reference, box_guide, loc, knobs_on)
        ]
    return outcome


@dataclass
class _Sent:
    """The call whose answer a unit reports: its model, and what it was sent
    (``detail.model`` / ``image_sent`` / ``text_sent``)."""

    spec: ModelSpec
    image_sent: bool
    text_sent: dict


def _text_sent(text: str, truncated: bool, spec: ModelSpec, layer: Any) -> dict:
    """``detail.text_sent`` for a call that was sent ``text`` under ``spec``'s
    budget."""
    return {
        "chars": len(text),
        "truncated": truncated,
        "budget": spec.text_char_budget,
        "source": layer.source,
    }


def _no_fast_block(spec: ModelSpec) -> EvaluationError:
    """The unit error for a fast / adaptive criterion whose model lost its
    ``fast:`` block between submit and run (the submit check refuses it
    now)."""
    return EvaluationError(
        f"model {spec.id!r} has no fast block in models.yaml any more (the registry "
        f"changed after this job was submitted){FAST_UNIT_ERROR_FIX}"
    )


async def _readout(
    name: str,
    opts: dict,
    spec: ModelSpec,
    page_b64: Optional[str],
    text: str,
    *,
    document_kind: str,
    text_truncated: bool,
) -> tuple[dict, Any]:
    """The one-token readout for one unit, rated: ``(rated, readout)``.

    ONE ``llm.client.call_readout`` (label ``fast/<name>``, kind ``fast``) of
    ``build_llm_prompt(style="fast")`` — the digits 0-9, score = digit + 1 —
    and ``llm.confidence.rate_readout`` over it, after the entry's
    ``fast.calibration.rating`` temperature when one is fitted. ``rated``
    has the scoring answer's keys (score, verdict, confidence, reason) and
    the ``block`` (``detail.confidence``); ``readout`` is the
    ``common.logprobs.OptionReadout``.

    Raises:
        EvaluationError: The model has no ``fast:`` block any more (the
            registry changed after submit).
        llm.client.ReadoutIncomplete / ReadoutCapped: The readout could not
            be believed — the caller decides (fast fails the unit, adaptive
            escalates).
        llm.client.LLMCallError: The request failed (a server refusing
            ``top_logprobs`` is named).
    """
    if spec.fast is None:
        raise _no_fast_block(spec)
    prompt = build_llm_prompt(
        page_b64, name, opts["hint"], text,
        document_kind=document_kind, text_truncated=text_truncated, model=spec, style="fast",
    )
    readout = await llm_client.call_readout(
        prompt, labels=FAST_DIGITS, label=f"fast/{name}", model=spec,
    )
    rated = llm_confidence.rate_readout(readout, spec.fast.calibration.get(DEFAULT_ANSWER_TYPE))
    logger.info(
        "llm_eval: '%s' fast readout on %s → %s (%s, confidence %d, coverage %.3f)",
        name, spec.id, rated["score"], rated["verdict"], rated["confidence"], readout.coverage,
    )
    return rated, readout


def _answer(rated: dict) -> dict:
    """A rated readout as a scoring answer (score, verdict, confidence,
    reason) — what the box loop's gate reads, as it reads a reasoned one."""
    return {k: rated[k] for k in ("score", "verdict", "confidence", "reason")}


async def _fast(
    name: str,
    opts: dict,
    spec: ModelSpec,
    page_b64: Optional[str],
    text: str,
    *,
    document_kind: str,
    text_truncated: bool,
) -> tuple[dict, dict, int]:
    """The fast readout for one unit: ``(answer, detail.confidence, headline)``
    — :func:`_readout`, whose refusal fails the unit.

    Raises:
        EvaluationError: The model has no ``fast:`` block any more (the
            registry changed after submit), or the readout was incomplete or
            capped — the message ends "— use mode 'reasoned' for this
            criterion" (``config.FAST_UNIT_ERROR_FIX``).
        llm.client.LLMCallError: The request failed (a server refusing
            ``top_logprobs`` is named).
    """
    try:
        rated, _ = await _readout(
            name, opts, spec, page_b64, text,
            document_kind=document_kind, text_truncated=text_truncated,
        )
    except llm_client.ReadoutIncomplete as exc:
        raise EvaluationError(f"{exc.summary}{FAST_UNIT_ERROR_FIX}") from exc
    return _answer(rated), rated["block"], rated["confidence"]


@dataclass
class _Adaptive:
    """What :func:`_adaptive` reports for one unit."""

    answer: dict                # score, verdict, confidence, reason
    block: dict                 # detail.confidence
    headline: int               # the top-level confidence
    mode: str                   # detail.mode: "fast" (the readout stood) | "reasoned"
    escalation: dict            # detail.escalation (ESCALATION_KEYS)
    escalations: dict           # detail.escalations: this unit's tally (ESCALATIONS_KEYS)
    sent: Optional[_Sent]       # the escalation call, when it answered; None = the readout's


class _EscalationFailed(Exception):
    """The escalation could not run at all (the entry unavailable at run
    time, a text-only escalation model on a page with no text). Its message
    is ``detail.escalation.error``."""


async def _adaptive(
    name: str,
    opts: dict,
    spec: ModelSpec,
    ctx: DocumentContext,
    page_b64: Optional[str],
    text: str,
    layer: Any,
    *,
    text_truncated: bool,
    judged: bool,
) -> _Adaptive:
    """``options.mode: "adaptive"`` for one unit: the readout on the
    criterion's model (``spec``), then — only when it is unsure — today's
    REASONED scoring call on the escalation model.

    **When it escalates.** The readout raised ``ReadoutIncomplete`` (``why``
    its summary, "readout incomplete: …") or ``ReadoutCapped`` ("probability
    cap reached: …"), or its SIGNAL — the readout's headline confidence,
    after the entry's ``fast.calibration.rating`` when fitted, computed even
    for a ``score: false`` criterion — is under the THRESHOLD:
    ``options.escalate_below``, else the criterion's model's
    ``fast.escalate_below.rating`` (``why`` "confidence 62 < 80",
    ``config.ESCALATION_WHY_CONFIDENCE``; without the number under ``score:
    false``, whose judgement it is). At or over the threshold the readout is
    the answer (``detail.mode`` ``fast``).

    **The escalation** (:func:`_escalate`): ``build_llm_prompt`` reasoned —
    the same hint, the same text layer and OCR rule, truncated to the
    ESCALATION model's ``text_char_budget``, the page image only when it has
    ``vision``, its reasoning and budgets — never reference-guided; label
    ``escalate/<name>`` (call kind ``escalate``), measured per ITS
    ``confidence.method``. Its answer is the unit's (``detail.mode``
    ``reasoned``, ``detail.model`` the escalation id, its own
    ``detail.confidence``), with ``escalation.fast`` the readout it
    replaced. Counted in ``classifier_escalations_total{model, why,
    outcome}``, and in the unit's ``detail.escalations`` (``_unit_escalations``:
    the same ``why``; escalated or failed).

    **A failed escalation** (an HTTP error, a timeout, an unparseable
    answer, the entry unavailable at run time, a text-only escalation model
    with no text): a complete readout's answer stands (``escalated: false``,
    ``error`` the reason). Only when the readout was incomplete / capped AND
    the escalation failed does the unit error
    (``config.ADAPTIVE_BOTH_FAILED``, naming both). Under ``score: false``
    an unparseable answer (``llm.client.AnswerUnparsed``, whose message
    quotes the answer's start — its verdict and confidence) is reported as
    ``config.ESCALATION_UNPARSED_UNJUDGED`` instead; the log line keeps it.

    Raises:
        EvaluationError: The model has no ``fast:`` block any more, or the
            readout and the escalation both failed.
        llm.client.LLMCallError: The readout's own request failed (a
            transport error is not "unsure": the unit errors as in fast).
    """
    if spec.fast is None:
        raise _no_fast_block(spec)
    threshold = opts.get("escalate_below")
    if threshold is None:
        threshold = spec.fast.escalate_below_for(DEFAULT_ANSWER_TYPE)
    escalation = llm_models.get_registry().escalation
    refused: Optional[llm_client.ReadoutIncomplete] = None
    rated: Optional[dict] = None
    readout = None
    try:
        rated, readout = await _readout(
            name, opts, spec, page_b64, text,
            document_kind=ctx.doc.kind, text_truncated=text_truncated,
        )
    except llm_client.ReadoutIncomplete as exc:
        refused = exc
        readout = exc.readout
    detail = {
        "escalated": False,
        "model": escalation.id,
        "threshold": int(threshold),
        "signal": rated["confidence"] if rated is not None else None,
        "why": None,
        "error": None,
        "fast": None,
    }
    if rated is not None and rated["confidence"] >= threshold:
        logger.info(
            "llm_eval: '%s' adaptive: the readout stands (confidence %d >= %d)",
            name, rated["confidence"], threshold,
        )
        return _Adaptive(_answer(rated), rated["block"], rated["confidence"], "fast", detail,
                         _unit_escalations(), None)

    if refused is not None:
        detail["why"] = refused.summary
        metric_why = "cap" if refused.reason == "cap" else "incomplete"
    else:
        wording = ESCALATION_WHY_CONFIDENCE if judged else ESCALATION_WHY_CONFIDENCE_UNJUDGED
        detail["why"] = wording.format(signal=rated["confidence"], threshold=threshold)
        metric_why = "confidence"
    try:
        answer, sent = await _escalate(name, opts, escalation, ctx, page_b64, layer)
    except (llm_client.LLMCallError, _EscalationFailed) as exc:
        escalations_total.labels(model=escalation.id, why=metric_why, outcome="failed").inc()
        logger.warning(
            "llm_eval: '%s' adaptive: the escalation to %s failed (%s): %s",
            name, escalation.id, detail["why"], exc,
        )
        # An unparseable answer's message quotes the model's answer: under
        # `score: false` that is a judgement the unit must not report.
        error = (
            ESCALATION_UNPARSED_UNJUDGED.format(model=escalation.id, attempts=MAX_LLM_RETRIES)
            if not judged and isinstance(exc, llm_client.AnswerUnparsed)
            else str(exc) or type(exc).__name__
        )
        if refused is not None:
            raise EvaluationError(ADAPTIVE_BOTH_FAILED.format(
                readout=refused.summary, model=escalation.id, error=error,
            )) from exc
        detail["error"] = error
        return _Adaptive(_answer(rated), rated["block"], rated["confidence"], "fast", detail,
                         _unit_escalations(why=metric_why), None)

    escalations_total.labels(model=escalation.id, why=metric_why, outcome="ok").inc()
    detail["escalated"] = True
    detail["fast"] = {
        "model": spec.id,
        "score": rated["score"] if rated is not None else None,
        "verdict": rated["verdict"] if rated is not None else None,
        "confidence": rated["confidence"] if rated is not None else None,
        "coverage": (
            round(float(readout.coverage), DETAIL_FLOAT_DECIMALS) if readout is not None else None
        ),
    }
    # The escalation model's own measurement and headline (models.yaml
    # `confidence`), as for any reasoned answer on it.
    block, headline = llm_confidence.assess(answer, escalation.confidence)
    logger.info(
        "llm_eval: '%s' adaptive: escalated to %s (%s) → %s (%s)",
        name, escalation.id, detail["why"], answer["score"], answer["verdict"],
    )
    tally = _unit_escalations(
        why=metric_why, answered_by=escalation.id, readout_failed=refused is not None,
    )
    return _Adaptive(answer, block, headline, "reasoned", detail, tally, sent)


async def _escalate(
    name: str,
    opts: dict,
    escalation: ModelSpec,
    ctx: DocumentContext,
    page_b64: Optional[str],
    layer: Any,
) -> tuple[dict, _Sent]:
    """The escalation call: today's REASONED scoring call for the criterion,
    on the ESCALATION model — ``build_llm_prompt`` with the same hint and
    text layer, the text truncated to ITS ``text_char_budget``, the page
    image only when it has ``vision`` (``build_llm_prompt`` drops it for a
    text-only entry), its reasoning, budgets and ``confidence`` request.
    Never reference-guided. ``(answer, what was sent)``.

    Raises:
        _EscalationFailed: The entry is unavailable now (its key unset since
            submit), or it is text-only and the page has no text.
        llm.client.LLMCallError: The call failed (HTTP, timeout, no parseable
            answer after the retries).
    """
    if not escalation.available:
        raise _EscalationFailed(ESCALATION_UNAVAILABLE_AT_RUN.format(
            model=escalation.id, reason=escalation.unavailable_reason,
        ))
    text, truncated = _budgeted(layer.text, escalation.text_char_budget)
    if not escalation.vision and not text.strip():
        raise _EscalationFailed(ESCALATION_NO_TEXT.format(
            model=escalation.id, source=layer.source,
        ))
    prompt = build_llm_prompt(
        page_b64, name, opts["hint"], text,
        document_kind=ctx.doc.kind, text_truncated=truncated, model=escalation,
    )
    answer = await llm_client.call_model(
        prompt, label=f"escalate/{name}", validator=answer_validator(name), model=escalation,
    )
    image_sent = bool(page_b64) and escalation.vision
    return answer, _Sent(escalation, image_sent, _text_sent(text, truncated, escalation, layer))


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
    none did, the ``note`` says why (the guide's own note, every step off —
    ``knobs_on`` is the criterion's resolved ``reference.boxes`` — or
    ``box_examples``'). Every step off reads as it always did while the
    service defaults are off too; with a default on, the criterion's own
    ``reference.boxes`` is what turned them off, and the note says so."""
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
        defaults = box_loop_settings({})
        note = (
            "options.reference.boxes turns every step off for this criterion"
            if defaults["guide_ask"] or defaults["guide_refine"] or defaults["guide_verify"]
            else "the CLASSIFIER_REFERENCE_BOX_* knobs are off"
        )
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
