"""The scheduler: (criterion, item) units, per-item gating, the per-job cap.

A request's documents are split into ITEMS — every page of every document —
and the unit of work is one criterion on one item: exactly the single-page
evaluation this service always did, dispatched to the evaluator for its
type with that item's context:

    EVALUATORS           {"cv", "text", "llm", "detector"} → ``evaluate(c, ctx)``
    DOCUMENT_EVALUATORS  {"text"} → ``evaluate_document(c, group)`` — a ``text``
                         criterion with ``options.scope: "document"`` is ONE
                         unit per document instead, searching its pages joined.

Three rules, all enforced here and nowhere else:

  * **Gating is per item.** A criterion with ``depends_on`` is not evaluated
    on item k until its dependency has finished ON ITEM k; if that result is
    not PASS, the dependant is SKIPPED on item k without being evaluated — no
    model call, no OCR pass, nothing spent — while it still runs on every
    item where the dependency passed. A skipped or errored dependency skips
    too, so chains propagate item by item. When the two sides have different
    scopes, "the dependency on this unit" is: its unit on the document the
    item belongs to (a document-scope dependency of a page-scope unit), or
    its PAGES aggregate for the document (a page-scope dependency of a
    document-scope unit — ``analysis.aggregate``). One ``asyncio.Event`` per
    unit, so a unit waits on exactly what it needs and nothing else is held
    back. Cycles and unknown names were refused at submit, so every wait
    ends.
  * **The per-job cap counts UNITS.** At most ``MAX_UNITS_PER_JOB`` units of
    one job are EVALUATING at once (a unit waiting on its dependency holds no
    slot). Model calls and OCR passes are further bounded process-wide by
    ``llm.client.LLM_CALLS`` and ``analysis.context.OCR_PASSES``.
  * **A failed unit fails alone.** Any exception out of an evaluator — a
    model HTTP error, an unparseable answer after the retries, a detector
    outage, a bug — becomes ``status: "error"`` for that (criterion, item).
    The job still completes; ``analysis.aggregate`` marks the criterion
    incomplete. Cancellation is the one exception that propagates: a shutdown
    must stop the job, and the queue requeues it.

``score: false`` does not change how a unit runs — only what is kept: its
score / verdict / confidence are cleared after it finishes, so it can never
reach the weighting or satisfy a dependency (the latter was refused at submit
too).

Every region a per-item unit returns is re-stamped ``page = item``: the
evaluators work in one page's frame, and the global item index is the page
index the shared vision code (layers, ``p{n}.*`` files) draws on.

With ``references: "auto"``, a unit that uses references first awaits its
ITEM's selection (``JobReferences.select`` — one call per item, memoised),
after its dependency gate and before it takes a slot.

When the request lists references, a finished per-item unit then goes through
``JobReferences.finish`` — the opt-in position check, which may CAP an llm
score (never raise it) — before ``score: false`` clears the judgement, so a
locate-only criterion still reports the check.

Every unit is its own task under ``gather``, so the scheduler is also where a
model call learns which unit it is for: ``run_item`` wraps the selection call
in ``llm.usage.unit_scope(item, document)``, and ``_evaluate`` /
``_evaluate_document`` wrap the evaluator in one that adds the criterion. The
``llm_calls`` row each request writes carries those, and the per-task context
keeps two concurrent units from ever seeing each other's.

It is also where the job's progress gauge (``common.jobs.progress``) counts
units: the "units" stage is planned at Σ steps over every unit — ONE step per
unit, except an ``llm`` unit with ``options.boxes``, which is TWO (scored,
then located: ``analysis.llm_eval`` checkpoints after the scoring call) — and
each unit body runs inside ``progress.task("units", steps)``, whose exit
credits whatever the unit did not checkpoint. So a unit skipped by its gate,
one that errored, and one whose box loop never ran (a low score) all count in
full, and the stage always reaches its length. With no gauge (a direct
``analyze_document`` call) all of it is a no-op.

Process flow position: called by ``analysis.pipeline.analyze_document``
between building the item contexts and aggregating the results.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from common.jobs import progress

from analysis import aggregate, cv_eval, detector_eval, llm_eval, text_eval
from analysis.context import DocumentContext, DocumentGroup
from analysis.outcome import Outcome, skipped
from analysis.result_specs import clear_judgement
from api.schemas import CriterionInput
from config import MAX_UNITS_PER_JOB
from llm.usage import unit_scope
from logger import logger

EVALUATORS = {
    "cv": cv_eval.evaluate,
    "text": text_eval.evaluate,
    "llm": llm_eval.evaluate,
    "detector": detector_eval.evaluate,
}

DOCUMENT_EVALUATORS = {
    "text": text_eval.evaluate_document,
}


@dataclass
class CriterionUnits:
    """One criterion's unit outcomes.

    ``scope == "page"``: ``outcomes`` is keyed by GLOBAL item index.
    ``scope == "document"``: keyed by document index.
    """

    criterion: CriterionInput
    scope: str
    outcomes: dict[int, Outcome] = field(default_factory=dict)


async def run_units(
    criteria: list[CriterionInput],
    items: list[DocumentContext],
    groups: list[DocumentGroup],
    *,
    max_parallel: int | None = None,
) -> dict[str, CriterionUnits]:
    """Evaluate every (criterion, item) unit; return them per criterion.

    Args:
        criteria:     The validated criteria (unique names, acyclic).
        items:        Every item's context, in global item order.
        groups:       The documents, each with its items.
        max_parallel: Override for MAX_UNITS_PER_JOB (read at call time,
                      so a test can also monkeypatch the module constant).
    """
    limit = max(1, int(max_parallel or MAX_UNITS_PER_JOB))
    slots = asyncio.Semaphore(limit)
    by_name = {c.name: c for c in criteria}
    runs = {c.name: CriterionUnits(criterion=c, scope=c.scope()) for c in criteria}

    def key(name: str, index: int) -> tuple[str, int]:
        return (name, index)

    finished: dict[tuple[str, int], asyncio.Event] = {}
    for c in criteria:
        indices = (
            [g.index for g in groups] if runs[c.name].scope == "document"
            else [ctx.item for ctx in items]
        )
        for index in indices:
            finished[key(c.name, index)] = asyncio.Event()

    async def result_of(name: str, index: int) -> Outcome:
        await finished[key(name, index)].wait()
        return runs[name].outcomes[index]

    async def dependency_on(dep: CriterionInput, *, item: int | None, document: int) -> Outcome:
        """The dependency's answer for one unit — see the module docstring."""
        dep_run = runs[dep.name]
        if dep_run.scope == "document":
            return await result_of(dep.name, document)
        if item is not None:
            return await result_of(dep.name, item)
        members = [ctx.item for ctx in groups[document].items]
        outcomes = [await result_of(dep.name, n) for n in members]
        return aggregate.pages_outcome(dep, members, outcomes)

    async def gate(c: CriterionInput, *, item: int | None, document: int) -> Outcome | None:
        """None to proceed, or the skipped outcome."""
        if c.depends_on is None:
            return None
        dep = await dependency_on(by_name[c.depends_on], item=item, document=document)
        if dep.status == "ok" and dep.verdict == "PASS":
            return None
        shown = dep.verdict if dep.status == "ok" else dep.status
        where = f"item {item}" if item is not None else f"document {document}"
        logger.info(
            "scheduler: skipping '%s' on %s — dependency '%s' is %s",
            c.name, where, c.depends_on, shown,
        )
        return skipped(
            f"Skipped - dependency '{c.depends_on}' did not pass (verdict: {shown})."
        )

    async def run_item(c: CriterionInput, ctx: DocumentContext) -> None:
        try:
            # The unit's progress steps — credited on exit however it ends.
            with progress.task("units", steps[c.name], label=f"{c.name} · item {ctx.item}"):
                outcome = await gate(c, item=ctx.item, document=ctx.document)
                if (outcome is None and ctx.references is not None
                        and ctx.references.needs_selection(c)):
                    # references "auto": this item's one selection call, shared
                    # by every unit on it — awaited BEFORE taking a unit slot,
                    # so a unit waiting on it holds nothing. It never raises.
                    # Its usage row carries the item and NO criterion:
                    # whichever unit gets here first makes the call, on behalf
                    # of all of them.
                    with unit_scope(item=ctx.item, document=ctx.document):
                        await ctx.references.select(ctx)
                if outcome is None:
                    async with slots:
                        outcome = await _evaluate(c, ctx)
                runs[c.name].outcomes[ctx.item] = outcome
        finally:
            finished[key(c.name, ctx.item)].set()

    async def run_document(c: CriterionInput, group: DocumentGroup) -> None:
        try:
            with progress.task("units", 1, label=f"{c.name} · document {group.index}"):
                outcome = await gate(c, item=None, document=group.index)
                if outcome is None:
                    async with slots:
                        outcome = await _evaluate_document(c, group)
                runs[c.name].outcomes[group.index] = outcome
        finally:
            finished[key(c.name, group.index)].set()

    steps = {c.name: unit_steps(c) for c in criteria}
    tasks = []
    total_steps = 0
    for c in criteria:
        if runs[c.name].scope == "document":
            tasks.extend(run_document(c, g) for g in groups)
            total_steps += len(groups)
        else:
            tasks.extend(run_item(c, ctx) for ctx in items)
            total_steps += steps[c.name] * len(items)

    logger.info(
        "scheduler: %d criteria × %d item(s) in %d document(s) = %d unit(s), up to %d at once",
        len(criteria), len(items), len(groups), len(tasks), limit,
    )
    progress.plan("units", total_steps)
    await asyncio.gather(*tasks)
    return runs


def unit_steps(c: CriterionInput) -> int:
    """Progress steps one per-item unit of ``c`` is worth: 2 for an ``llm``
    criterion with ``options.boxes`` (scored, then located), else 1. A
    document-scope unit is always 1."""
    if c.type == "llm" and c.resolved_options().get("boxes"):
        return 2
    return 1


def _failed(c: CriterionInput, exc: Exception) -> Outcome:
    logger.warning("scheduler: '%s' (%s) failed: %s", c.name, c.type, exc)
    return Outcome(
        status="error",
        method=c.type,
        reason=f"Evaluation failed: {exc}",
        error=str(exc) or type(exc).__name__,
    )


def _unjudged(c: CriterionInput, outcome: Outcome) -> Outcome:
    if not c.score and outcome.status == "ok":
        # Locate without judging: keep the geometry and the explanation,
        # drop the judgement.
        outcome.score = None
        outcome.verdict = None
        outcome.confidence = None
        # An llm criterion's detail.value IS the score; it goes with the rest
        # of the judgement. A cv measurement or a text count is not a
        # judgement and stays.
        outcome.detail = clear_judgement(outcome.detail, outcome.method)
    return outcome


async def _evaluate(c: CriterionInput, ctx: DocumentContext) -> Outcome:
    """One evaluator call with the error isolation and ``score: false`` applied."""
    try:
        # Every model call this unit makes is attributed to it (llm.usage).
        with unit_scope(criterion=c.name, item=ctx.item, document=ctx.document):
            outcome = await EVALUATORS[c.type](c, ctx)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — every failure is this unit's alone
        return _failed(c, exc)
    for region in outcome.regions:
        region.page = ctx.item
    if ctx.references is not None:
        try:
            outcome = ctx.references.finish(c, ctx, outcome)
        except Exception as exc:  # noqa: BLE001 — the check fails this unit alone
            return _failed(c, exc)
    return _unjudged(c, outcome)


async def _evaluate_document(c: CriterionInput, group: DocumentGroup) -> Outcome:
    """The same, for a document-scope unit (its evaluator stamps each region)."""
    try:
        with unit_scope(criterion=c.name, document=group.index):
            outcome = await DOCUMENT_EVALUATORS[c.type](c, group)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        return _failed(c, exc)
    return _unjudged(c, outcome)
