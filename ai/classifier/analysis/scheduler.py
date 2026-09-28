"""The scheduler: dependency waves, the per-job cap, and error isolation.

Every criterion is one unit of work — criterion + shared document context in,
one ``Outcome`` out — dispatched to the evaluator for its type:

    EVALUATORS   {"cv", "text", "llm", "detector"} → ``evaluate(c, ctx)``

Three rules, all enforced here and nowhere else:

  * **Waves.** A criterion with ``depends_on`` is not evaluated at all until
    its dependency has finished; if that dependency's verdict is not PASS it
    is SKIPPED without being evaluated — no model call, no OCR pass, nothing
    spent. A skipped or errored dependency skips its dependants too, so
    chains propagate. Implemented with one ``asyncio.Event`` per criterion
    rather than explicit rounds: a dependant waits on exactly the criterion it
    names, so an independent criterion is never held back by an unrelated
    slow one. Cycles and unknown names were refused at submit, so every wait
    ends.
  * **The per-job cap.** At most ``MAX_CRITERIA_PER_JOB`` criteria of one job
    are EVALUATING at once (a dependant waiting on its dependency does not
    hold a slot). Model calls and OCR passes are further bounded process-wide
    by ``llm.client.LLM_CALLS`` and ``analysis.context.OCR_PASSES``.
  * **A failed criterion fails alone.** Any exception out of an evaluator —
    a model HTTP error, an unparseable answer after the retries, a detector
    outage, a bug — becomes ``status: "error"`` with the message for that
    criterion. The job still completes; ``analysis.weighting`` reports it
    incomplete. Cancellation is the one exception that propagates: a
    shutdown must stop the job, and the queue requeues it.

``score: false`` does not change how a criterion runs — only what is kept:
its score / verdict / confidence are cleared after it finishes, so it can
never reach the weighting or satisfy a dependency (the latter was refused at
submit too).

Process flow position: called by ``analysis.pipeline.analyze_document``
between building the context and weighing the results.
"""

from __future__ import annotations

import asyncio

from analysis import cv_eval, detector_eval, llm_eval, text_eval
from analysis.context import DocumentContext
from analysis.outcome import Outcome, skipped
from api.schemas import CriterionInput
from config import MAX_CRITERIA_PER_JOB
from logger import logger

EVALUATORS = {
    "cv": cv_eval.evaluate,
    "text": text_eval.evaluate,
    "llm": llm_eval.evaluate,
    "detector": detector_eval.evaluate,
}


async def run_criteria(
    criteria: list[CriterionInput],
    ctx: DocumentContext,
    *,
    max_parallel: int | None = None,
) -> dict[str, Outcome]:
    """Evaluate every criterion; return ``{name: Outcome}`` in request order.

    Args:
        criteria:     The validated criteria (unique names, acyclic).
        ctx:          The shared document context.
        max_parallel: Override for MAX_CRITERIA_PER_JOB (read at call time,
                      so a test can also monkeypatch the module constant).
    """
    limit = max(1, int(max_parallel or MAX_CRITERIA_PER_JOB))
    slots = asyncio.Semaphore(limit)
    results: dict[str, Outcome] = {}
    finished = {c.name: asyncio.Event() for c in criteria}

    async def run_one(c: CriterionInput) -> None:
        try:
            if c.depends_on is not None:
                await finished[c.depends_on].wait()
                dep = results[c.depends_on]
                if dep.status != "ok" or dep.verdict != "PASS":
                    shown = dep.verdict if dep.status == "ok" else dep.status
                    logger.info(
                        "scheduler: skipping '%s' — dependency '%s' is %s",
                        c.name, c.depends_on, shown,
                    )
                    results[c.name] = skipped(
                        f"Skipped - dependency '{c.depends_on}' did not pass "
                        f"(verdict: {shown})."
                    )
                    return
            async with slots:
                results[c.name] = await _evaluate(c, ctx)
        finally:
            finished[c.name].set()

    logger.info(
        "scheduler: %d criteria, up to %d at once: %s",
        len(criteria), limit, [f"{c.name}({c.type})" for c in criteria],
    )
    await asyncio.gather(*(run_one(c) for c in criteria))
    return {c.name: results[c.name] for c in criteria}


async def _evaluate(c: CriterionInput, ctx: DocumentContext) -> Outcome:
    """One evaluator call with the error isolation and ``score: false`` applied."""
    try:
        outcome = await EVALUATORS[c.type](c, ctx)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — every failure is this criterion's alone
        logger.warning("scheduler: '%s' (%s) failed: %s", c.name, c.type, exc)
        return Outcome(
            status="error",
            method=c.type,
            reason=f"Evaluation failed: {exc}",
            error=str(exc) or type(exc).__name__,
        )
    if not c.score and outcome.status == "ok":
        # Locate without judging: keep the geometry and the explanation,
        # drop the judgement.
        outcome.score = None
        outcome.verdict = None
        outcome.confidence = None
    return outcome
