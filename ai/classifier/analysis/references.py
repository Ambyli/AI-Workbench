"""The pipeline side of references: guiding the model with stored examples.

``references/`` stores examples and must never import ``llm``; everything that
USES them in a job lives here, in the analysis layer, and works from the plan
the submit resolved (``references.resolve``) — the worker never looks a
reference up by id.

    JobReferences    built once per job from the payload's ``references``
                     plan (``analysis.pipeline.analyze_document``) and hung
                     on every item's ``DocumentContext.references``:
        guide(c, item) which examples guide criterion ``c`` on an item (a
                       ``CriterionGuide``), or None for a criterion the model
                       does not answer. Explicit ids: the plan's examples.
                       ``auto``: that item's SELECTION, filtered per criterion
                       by the plan's ``matched_by`` / ``matched`` — the
                       reference criterion with that name, or (``key``) with
                       that key. A plan without ``matched_by`` (written
                       before keys) matches by name, as it always did.
        needs_selection(c) / select(ctx)
                       ``references: "auto"``: ONE selection call per item —
                       the candidate page (the one image) and the pool's text
                       catalogue — memoised per item like
                       ``DocumentContext.ask_image_b64``, and awaited by the
                       scheduler after a unit's dependency gate and BEFORE it
                       takes a unit slot, only by units that use references.
                       No call when the pool is empty or the item has no page
                       image. A failed call is not fatal: those units score
                       unguided (``applied: false``) and the error is in the
                       result's ``references.selection``.
        image_b64()    an example's composite as base64, read from the
                       reference store once per job and cached. A reference
                       DELETED after submit raises ``EvaluationError`` — that
                       criterion's units fail (status "error"), nothing else.
        crop_b64()     an example's region crop as base64 — its ORIGINAL
                       page cut around its first region
                       (``llm.boxes.example_crop``), cached; None when the
                       page is gone or unreadable.
        box_examples() what the BOX LOOP shows one unit (a ``BoxGuide``):
                       ``box_selection``'s examples, loaded — composites for
                       the ask (crops under the position check), crops for
                       refine and verify. Never raises: an example that
                       cannot be loaded is dropped from every step, noted.
                       Built by ``analysis.llm_eval`` only when a
                       CLASSIFIER_REFERENCE_BOX_* knob is on.
        finish()       the opt-in POSITION check, run by the scheduler on a
                       finished unit (see below).
        record_calls() what each guided call carried, for
                       ``references.json`` — each with its ``kind``
                       (``scoring``, ``box_ask``, ``box_refine``,
                       ``box_verify``).
        summary()      the result's top-level ``references`` block.
        artifact()     the ``references.json`` body.
    plan_calls()     which examples share a scoring call: with room for three
                     images (the model's max_images_per_prompt ≥ 3) call i pairs
                     the i-th PASS-side example with the i-th FAIL example —
                     one contrastive call, three images; with room for two,
                     each example is its own call. At most
                     CLASSIFIER_REFERENCE_MAX_PER_CRITERION calls.
    combine()        several guided answers → one: ``any`` the best (with its
                     reason), ``all`` the lowest, ``mean`` the rounded mean.
    box_selection()  which examples the box loop is shown: regions only,
                     PASS then MARGINAL then FAIL (a FAIL only when its
                     reference criterion's hint is presence), one PASS-side
                     slot plus, at three images, one FAIL slot.
    describe_page()  ONE single-image call: a short, neutral description of a
                     reference's page, for the catalogue ``references:
                     "auto"`` reads. Made by the creation job
                     (``jobs.runners.run_reference``) only when the caller
                     sent no description and CLASSIFIER_REFERENCE_DESCRIBE is
                     on.

The position check (``options.reference.position: "check"``, llm criteria
with boxes only): the unit's ACCEPTED box-loop regions and the PASS-side
examples' regions are each normalised to their own page
(``common.vision.normalize_region``) and compared pair by pair — the best
IoU, and the smallest centre distance / √2. A HIT is IoU ≥ ``min_iou`` or
offset ≤ CLASSIFIER_REFERENCE_POSITION_MAX_OFFSET; a MISS caps the score at
CLASSIFIER_REFERENCE_POSITION_CAP (never raises it) and recomputes the
verdict; nothing located is ``unlocated`` and is not capped. On a ``score:
false`` criterion the check is reported and changes nothing. No model call.

Every model call goes through ``llm.client``, so its model's
``max_concurrent`` limit bounds it like any scoring call. The selection call
runs on the JOB's model (``DocumentContext.default_model``), the describe
call on the creation job's.

Process flow position: ``JobReferences`` is built by ``analysis.pipeline``,
read by ``analysis.llm_eval`` / ``cv_eval`` (guide, images, box examples)
and ``analysis.scheduler`` (finish); ``describe_page`` is called by
``jobs.runners.run_reference``.
"""

from __future__ import annotations

import asyncio
import base64
import math
from dataclasses import dataclass, field
from typing import Any, Optional

from common.vision import PageGeometry, Region, center_distance, iou, normalize_region

from analysis.outcome import EvaluationError, Outcome
from api.schemas import CriterionInput
from config import (
    REFERENCE_AUTO_MIN_CONFIDENCE,
    REFERENCE_DESCRIPTION_MAX_CHARS,
    REFERENCE_MAX_PER_CRITERION,
    REFERENCE_POSITION_CAP,
    REFERENCE_POSITION_MAX_OFFSET,
    REFERENCE_POSITION_MIN_IOU,
)
# A module, not names: tests script the model at `llm.client._send`, under
# the limit, and every call must reach the one object they replaced.
from llm import boxes as llm_boxes
from llm import client as llm_client
from llm.models import ModelSpec, get_registry
from llm.prompts import BoxExample, build_describe_prompt, build_reference_selection_prompt
from llm.validate import description_validator, selection_validator
from logger import logger
from metrics import reference_calls_total
from references.model import PAGE_FILE
from references.render import catalogue_text
from references.store import reference_files
from utils import verdict_from_score

_SQRT2 = math.sqrt(2.0)


# ---------------------------------------------------------------------------
# Planning and combining the guided calls (pure)
# ---------------------------------------------------------------------------


def plan_calls(
    examples: list[dict[str, Any]],
    max_images: int,
    max_calls: int = REFERENCE_MAX_PER_CRITERION,
) -> list[list[int]]:
    """Example indices per scoring call.

    ``max_images`` is what one request may carry INCLUDING the candidate.
    With three or more, call i pairs the i-th PASS-side example (PASS, then
    MARGINAL, in list order) with the i-th FAIL example; with two, every
    example is a call of its own, in list order. Capped at ``max_calls``.
    """
    if not examples or max_images < 2:
        return []
    if max_images >= 3:
        positive = [i for i, e in enumerate(examples) if e["polarity"] != "fail"]
        negative = [i for i, e in enumerate(examples) if e["polarity"] == "fail"]
        calls = []
        for k in range(max(len(positive), len(negative))):
            call = []
            if k < len(positive):
                call.append(positive[k])
            if k < len(negative):
                call.append(negative[k])
            calls.append(call)
    else:
        calls = [[i] for i in range(len(examples))]
    return calls[: max(1, max_calls)]


def combine(answers: list[tuple[int, dict]], rule: str) -> tuple[dict, int]:
    """``[(call, answer), ...]`` (the survivors) → ``(answer, chosen call)``.

    ``any`` keeps the highest score's answer whole (the first, on a tie);
    ``all`` the lowest's; ``mean`` rounds the mean score, derives the verdict
    from it, averages the confidence, and keeps the reason of the call
    closest to the mean.
    """
    if rule == "all":
        call, answer = min(answers, key=lambda pair: pair[1]["score"])
        return dict(answer), call
    if rule == "mean":
        scores = [a["score"] for _, a in answers]
        mean = sum(scores) / len(scores)
        score = max(1, min(10, int(round(mean))))
        call, nearest = min(answers, key=lambda pair: abs(pair[1]["score"] - mean))
        confidence = int(round(sum(a["confidence"] for _, a in answers) / len(answers)))
        return {
            "score": score,
            "verdict": verdict_from_score(score),
            "confidence": confidence,
            "reason": nearest["reason"],
        }, call
    best = max(a["score"] for _, a in answers)
    call, answer = next(pair for pair in answers if pair[1]["score"] == best)
    return dict(answer), call


def box_selection(
    examples: list[dict[str, Any]], max_images: int
) -> tuple[list[dict[str, Any]], Optional[str]]:
    """Which of a criterion's examples the BOX LOOP is shown, in order, and
    — when none — why.

    Only examples WITH regions (``region_source`` not ``whole_page`` and a
    non-empty ``regions``): a whole page has no outline to teach where. A
    FAIL example only when its REFERENCE criterion's ``hint`` is
    ``presence`` — there FAIL means "not there", so whatever is outlined is
    NOT the feature; under ``quality`` / ``auto`` FAIL can mean "there, but
    bad" (an illegible signature) and showing it as "do not box this" would
    steer the model off the right object. A plan queued before ``hint``
    existed has none: not presence.

    The order is PASS, then MARGINAL, then FAIL, each in list order —
    deliberately unlike ``plan_calls``, which keeps PASS and MARGINAL
    together in list order: a MARGINAL outline is a borderline instance, the
    weaker teacher of WHAT to box, so a PASS outline takes the one PASS-side
    slot when there is one.

    The budget is ``max_images − 1`` (the candidate takes one), and the loop
    stays one call per step: one PASS-side slot, plus at three or more
    images one FAIL slot. With no PASS-side example an eligible FAIL rides
    alone at three or more (the only save so far may be "it boxed the
    heading"); at two only a PASS-side example is ever shown; at one, none
    (the submit refuses references below two images, so ``/assess`` cannot
    reach it).
    """
    framed = [
        e for e in examples
        if e.get("region_source") != "whole_page" and e.get("regions")
    ]
    if not framed:
        return [], (
            "no example has a region to show the box loop (a whole-page example "
            "teaches no location)"
        )
    if max_images < 2:
        return [], (
            f"the model takes {max_images} image(s) per prompt — no room for an "
            "example beside the candidate"
        )
    positive = (
        [e for e in framed if e["polarity"] == "pass"]
        + [e for e in framed if e["polarity"] == "marginal"]
    )
    negative = [e for e in framed if e["polarity"] == "fail" and e.get("hint") == "presence"]
    chosen = positive[:1] + (negative[:1] if max_images >= 3 else [])
    if chosen:
        return chosen, None
    if negative:
        return [], (
            f"only a FAIL example has a region, and at {max_images} images per prompt "
            "the box loop is shown a PASS-side example only"
        )
    return [], (
        "only FAIL examples have regions, and the box loop is shown a FAIL only when "
        "its reference criterion's hint is presence"
    )


# ---------------------------------------------------------------------------
# One criterion's guidance, and the job's
# ---------------------------------------------------------------------------


@dataclass
class CriterionGuide:
    """The examples guiding one criterion in one job (empty = not applied)."""

    name: str
    mode: str
    options: dict[str, Any]
    examples: list[dict[str, Any]]
    note: Optional[str] = None

    @property
    def applied(self) -> bool:
        return bool(self.examples)

    def unapplied_detail(self) -> dict[str, Any]:
        """``detail.reference`` for a criterion that scored without examples."""
        return {
            "applied": False,
            "mode": self.mode,
            "combine": self.options.get("combine", "any"),
            "examples": [],
            "calls": [],
            "position": None,
            "note": self.note,
        }


@dataclass
class BoxGuide:
    """What the box loop is shown for one unit (``JobReferences.box_examples``).

    Attributes:
        ask:        The ask's examples — composites, or region crops under
                    the position check.
        crops:      The same examples as region crops, for refine and verify.
        records:    ``{reference_id, criterion, polarity, composite}`` per
                    example in them, for ``localization.references`` and
                    ``references.json``.
        ask_images: ``"composite"`` | ``"crop"`` — what the ask is shown.
        note:       Why there are none, or which were dropped; None when
                    there is nothing to say.
    """

    ask: list[BoxExample] = field(default_factory=list)
    crops: list[BoxExample] = field(default_factory=list)
    records: list[dict[str, Any]] = field(default_factory=list)
    ask_images: str = "composite"
    note: Optional[str] = None


@dataclass
class JobReferences:
    """One job's reference plan, and what the job did with it."""

    plan: dict[str, Any]
    _images: dict[tuple[str, str], str] = field(default_factory=dict)
    _crops: dict[tuple[str, str], str] = field(default_factory=dict)
    _calls: list[dict[str, Any]] = field(default_factory=list)
    # auto: one selection per item — the future (memo) and what it decided.
    _selecting: dict[int, "asyncio.Future[Optional[list[dict]]]"] = field(default_factory=dict)
    _selections: dict[int, dict[str, Any]] = field(default_factory=dict)

    @property
    def mode(self) -> str:
        return self.plan.get("mode", "explicit")

    # ── Guidance ──────────────────────────────────────────────────────────
    def _options(self, c: CriterionInput) -> dict[str, Any]:
        entry = (self.plan.get("criteria") or {}).get(c.name)
        return (entry or {}).get("options") or c.reference_options() or {
            "use": True, "criterion": c.name, "position": "off",
            "min_iou": REFERENCE_POSITION_MIN_IOU, "combine": "any",
        }

    def guide(self, c: CriterionInput, item: Optional[int] = None) -> Optional[CriterionGuide]:
        """The guidance for ``c`` (on ``item``, for ``auto``); None when the
        model does not answer it."""
        if not c.guided_by_llm():
            return None
        entry = (self.plan.get("criteria") or {}).get(c.name)
        options = self._options(c)
        if self.mode == "auto":
            return self._auto_guide(c, options, item)
        if entry is None:
            return CriterionGuide(c.name, self.mode, options, [],
                                  note="this criterion was not in the submitted plan")
        if not options.get("use", True):
            return CriterionGuide(c.name, self.mode, options, [],
                                  note="options.reference.use is false")
        examples = list(entry.get("examples") or [])
        matched = entry.get("matched")
        rule = (
            f"with key {matched!r}" if entry.get("matched_by", "name") == "key"
            else f"named {matched!r}"
        )
        note = None if examples else f"no listed reference has a usable criterion {rule}"
        return CriterionGuide(c.name, self.mode, options, examples, note=note)

    # ── auto: the per-item selection ──────────────────────────────────────
    def needs_selection(self, c: CriterionInput) -> bool:
        """Whether a unit of ``c`` waits for its item's selection call."""
        return (
            self.mode == "auto" and c.guided_by_llm()
            and bool(self._options(c).get("use", True))
        )

    async def select(self, ctx: Any) -> Optional[list[dict]]:
        """This item's ranked matches, from ONE call made at most once per
        item (concurrent units await the same future). None when there was
        nothing to ask (empty pool, no page image) or the call failed."""
        future = self._selecting.get(ctx.item)
        if future is None:
            future = asyncio.get_running_loop().create_future()
            self._selecting[ctx.item] = future
            try:
                future.set_result(await self._select(ctx))
            except asyncio.CancelledError:
                self._selecting.pop(ctx.item, None)
                future.cancel()
                raise
            except Exception as exc:  # pragma: no cover — _select never raises
                future.set_exception(exc)
                future.exception()
                raise
        return await asyncio.shield(future)

    async def _select(self, ctx: Any) -> Optional[list[dict]]:
        pool = list(self.plan.get("pool") or [])
        record: dict[str, Any] = {
            "item": ctx.item, "status": "skipped", "matches": [], "dropped": [],
            "error": None, "reason": None, "raw": None,
        }
        self._selections[ctx.item] = record
        if not pool:
            record["reason"] = "the pool is empty — no ready reference matched the criteria and tags"
            return None
        image = ctx.image_b64()
        if image is None:
            record["reason"] = "the item has no page image to select examples for"
            return None
        names = [
            name for name, entry in (self.plan.get("criteria") or {}).items()
            if (entry.get("options") or {}).get("use", True)
        ]
        # The JOB's model makes the selection: it serves every criterion on
        # the item, so no one criterion's options.model can speak for it.
        spec = get_registry().resolve(getattr(ctx, "default_model", None))
        prompt = build_reference_selection_prompt(
            image, catalogue_text(self.plan.get("catalogue") or []), names,
            document_kind=getattr(ctx.doc, "kind", "image"), model=spec,
        )
        try:
            answer = await llm_client.call_model(
                prompt, label=f"reference/select#{ctx.item}",
                validator=selection_validator(pool), model=spec,
            )
        except Exception as exc:  # noqa: BLE001 — a failed selection only degrades
            reference_calls_total.labels(kind="selection", outcome="failed").inc()
            record.update(status="failed", error=str(exc) or type(exc).__name__)
            logger.warning("references: selection for item %d failed: %s", ctx.item, exc)
            return None
        reference_calls_total.labels(kind="selection", outcome="ok").inc()
        record.update(status="ok", matches=answer["matches"], dropped=answer["dropped"],
                      raw=answer["raw"])
        logger.info(
            "references: item %d selected %s from a pool of %d",
            ctx.item, [(m["id"], m["confidence"]) for m in answer["matches"]], len(pool),
        )
        return answer["matches"]

    def _auto_guide(
        self, c: CriterionInput, options: dict[str, Any], item: Optional[int]
    ) -> CriterionGuide:
        """The examples ``item``'s selection gives ``c``: matches that HAVE the
        criterion, at or above CLASSIFIER_REFERENCE_AUTO_MIN_CONFIDENCE, in
        rank order, at most CLASSIFIER_REFERENCE_MAX_PER_CRITERION.

        "Have the criterion" follows the plan entry's ``matched_by``: ``key``
        looks the folded ``matched`` up in ``examples_by_key``, anything else
        in ``examples`` (by folded name). A plan written before keys has
        neither field: ``name``, and ``options.criterion`` or the name — what
        it always did."""
        if not options.get("use", True):
            return CriterionGuide(c.name, "auto", options, [],
                                  note="options.reference.use is false")
        record = self._selections.get(item) if item is not None else None
        if record is None:
            return CriterionGuide(c.name, "auto", options, [],
                                  note="no selection was made for this item")
        if record["status"] == "skipped":
            return CriterionGuide(c.name, "auto", options, [], note=record["reason"])
        if record["status"] == "failed":
            return CriterionGuide(c.name, "auto", options, [],
                                  note=f"the selection call failed: {record['error']}")
        entry = (self.plan.get("criteria") or {}).get(c.name) or {}
        by = entry.get("matched_by", "name")
        value = entry.get("matched") or options.get("criterion") or c.name
        target = value.casefold().strip()
        by_ref = self.plan.get("examples_by_key" if by == "key" else "examples") or {}
        examples = []
        for match in record["matches"]:
            if match["confidence"] < REFERENCE_AUTO_MIN_CONFIDENCE:
                continue
            example = (by_ref.get(match["id"]) or {}).get(target)
            if example is None:
                continue
            examples.append({**example, "selection": {
                "confidence": match["confidence"], "reason": match["reason"]}})
            if len(examples) >= REFERENCE_MAX_PER_CRITERION:
                break
        rule = f"with key {value!r}" if by == "key" else repr(value)
        note = None if examples else (
            f"the selection matched no reference with a usable criterion {rule} at "
            f"confidence >= {REFERENCE_AUTO_MIN_CONFIDENCE}"
        )
        return CriterionGuide(c.name, "auto", options, examples, note=note)

    def selected_ids(self) -> list[str]:
        """Every reference some item's selection kept (any criterion, any
        confidence at or above the line), in first-seen order."""
        seen: list[str] = []
        for record in self._selections.values():
            for m in record["matches"]:
                if m["confidence"] >= REFERENCE_AUTO_MIN_CONFIDENCE and m["id"] not in seen:
                    seen.append(m["id"])
        return seen

    async def image_b64(self, example: dict[str, Any]) -> str:
        """An example's composite, base64 — read once per job, then cached."""
        key = (example["reference_id"], example["composite"])
        cached = self._images.get(key)
        if cached is not None:
            return cached
        raw = await asyncio.to_thread(reference_files.open, *key)
        if raw is None:
            raise EvaluationError(
                f"reference {example['reference_id']} was deleted after this job was "
                "submitted, so its example cannot be shown; resubmit without it"
            )
        encoded = base64.b64encode(raw).decode("ascii")
        self._images[key] = encoded
        return encoded

    async def crop_b64(self, example: dict[str, Any]) -> Optional[str]:
        """An example's region crop, base64 — cut from the reference's
        ORIGINAL page (``page.jpg``, the frame its regions are in) around its
        FIRST region by ``llm.boxes.example_crop``, the way the verify crop is
        cut from the candidate's. The first region is the right one: a
        pipeline region is the one accepted box, and a caller's first region
        is the one it named first. A polygon is cut by its bounds. Read and
        decoded once per job (cached by ``(reference_id, criterion)``).

        None when ``page.jpg`` is gone (a reference deleted after submit) or
        does not decode."""
        key = (example["reference_id"], example["criterion"])
        cached = self._crops.get(key)
        if cached is not None:
            return cached
        raw = await asyncio.to_thread(reference_files.open, example["reference_id"], PAGE_FILE)
        if raw is None:
            return None
        bounds = list(Region.from_dict(example["regions"][0]).bounds())

        def cut() -> Optional[str]:
            import cv2
            import numpy as np

            page = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
            if page is None:
                return None
            crop = llm_boxes.example_crop(page, bounds)
            return None if crop is None else llm_client.encode_image_to_base64(crop)

        encoded = await asyncio.to_thread(cut)
        if encoded is not None:
            self._crops[key] = encoded
        return encoded

    async def box_examples(
        self, guide: CriterionGuide, spec: ModelSpec, *, position_check: bool
    ) -> BoxGuide:
        """The examples the box loop shows for one unit of ``guide``'s
        criterion on ``spec``.

        Chosen by ``box_selection`` (regions only, FAIL only under a
        presence hint, one PASS-side + one FAIL at most), then loaded: the
        ask's composites through the ``image_b64`` cache — or, under the
        position check (``options.reference.position: "check"``), the region
        crops, so the ask is shown the feature without the page around it
        and cannot be nudged toward the example's spot, which is exactly what
        the check then compares against — and every example's region crop
        (``crop_b64``) for refine and verify.

        **Never raises.** The score is final by now and the loop never moves
        it, so an example that cannot be loaded — a reference deleted after
        submit (``EvaluationError``), a missing or undecodable ``page.jpg``,
        any other failure — is dropped from EVERY step, with a note, so
        every step sees the same examples. Called by ``analysis.llm_eval``
        only after the loop's gate passed and a knob is on, so a page that is
        not located never decodes a reference page.
        """
        chosen, note = box_selection(guide.examples, spec.max_images_per_prompt)
        out = BoxGuide(ask_images="crop" if position_check else "composite", note=note)
        dropped: list[str] = []
        for example in chosen:
            rid = example["reference_id"]
            try:
                composite = None if position_check else await self.image_b64(example)
                crop = await self.crop_b64(example)
            except Exception as exc:  # noqa: BLE001 — a lost example never fails the unit
                dropped.append(f"{rid}: {exc}")
                continue
            if crop is None:
                dropped.append(f"{rid}: its page image ({PAGE_FILE}) is missing or unreadable")
                continue
            verdict = example["expected"]["verdict"]
            out.ask.append(BoxExample(
                criterion=example["criterion"], verdict=verdict,
                image_b64=crop if position_check else composite, crop=position_check,
            ))
            out.crops.append(BoxExample(
                criterion=example["criterion"], verdict=verdict, image_b64=crop, crop=True,
            ))
            out.records.append({
                "reference_id": rid, "criterion": example["criterion"],
                "polarity": example["polarity"], "composite": example["composite"],
            })
        if dropped:
            out.note = (
                "no example could be loaded for the box loop — " if not out.ask
                else "dropped from the box loop, could not be loaded — "
            ) + "; ".join(dropped)
            logger.warning("references: box examples for '%s': %s", guide.name, out.note)
        return out

    def record_calls(self, name: str, item: int, calls: list[dict[str, Any]]) -> None:
        """What one unit's guided calls carried, for ``references.json`` —
        each entry with its ``kind``: ``scoring``, or the box loop's
        ``box_ask`` / ``box_refine`` / ``box_verify``."""
        for call in calls:
            self._calls.append({"criterion": name, "item": item, **call})

    # ── The position check ────────────────────────────────────────────────
    def finish(self, c: CriterionInput, ctx: Any, outcome: Outcome) -> Outcome:
        """Run the position check on a finished unit when ``c`` asked for it."""
        opts = c.reference_options()
        if not opts or opts.get("position") != "check" or outcome.status != "ok":
            return outcome
        detail = outcome.detail if isinstance(outcome.detail, dict) else None
        ref = (detail or {}).get("reference")
        if not ref or not ref.get("applied") or ctx.geometry is None:
            return outcome
        guide = self.guide(c, getattr(ctx, "item", None))
        examples = [
            e for e in (guide.examples if guide else [])
            if e["polarity"] != "fail" and e.get("regions") and e.get("geometry")
        ]
        min_iou = float(opts.get("min_iou", REFERENCE_POSITION_MIN_IOU))
        position: dict[str, Any] = {
            "status": "unlocated", "iou": None, "center_offset": None,
            "min_iou": min_iou, "max_offset": REFERENCE_POSITION_MAX_OFFSET,
            "reference_id": None, "capped_from": None,
        }
        located = [
            r for r in outcome.regions
            if r.source == "llm" and r.attrs.get("accepted") is not False
        ]
        if not examples:
            position["status"] = "no_reference_region"
        elif located:
            best_iou, best_offset, best_ref = 0.0, None, None
            for region in located:
                mine = normalize_region(region, ctx.geometry)
                for example in examples:
                    frame = PageGeometry.from_dict(example["geometry"])
                    for theirs in example["regions"]:
                        other = normalize_region(Region.from_dict(theirs), frame)
                        overlap = iou(mine, other)
                        offset = center_distance(mine, other) / _SQRT2
                        better = (
                            best_ref is None or overlap > best_iou
                            or (overlap == best_iou and offset < (best_offset or 1.0))
                        )
                        if better:
                            best_iou, best_offset, best_ref = overlap, offset, example["reference_id"]
            hit = best_iou >= min_iou or (best_offset is not None
                                          and best_offset <= REFERENCE_POSITION_MAX_OFFSET)
            position.update(
                status="hit" if hit else "miss",
                iou=round(best_iou, 4),
                center_offset=round(best_offset, 4) if best_offset is not None else None,
                reference_id=best_ref,
            )
            if not hit and c.score and outcome.score is not None:
                where = (
                    f"Position check: the located box is not where reference {best_ref} "
                    f"has it (IoU {best_iou:.2f}, offset {best_offset:.2f})"
                )
                if outcome.score > REFERENCE_POSITION_CAP:
                    position["capped_from"] = outcome.score
                    outcome.score = REFERENCE_POSITION_CAP
                    outcome.verdict = verdict_from_score(REFERENCE_POSITION_CAP)
                    if "value" in detail:
                        detail["value"] = REFERENCE_POSITION_CAP
                    where += f"; score capped at {REFERENCE_POSITION_CAP}"
                outcome.reason = f"{outcome.reason or ''} {where}.".strip()
        ref["position"] = position
        logger.info(
            "references: position check '%s' item %s → %s (iou=%s offset=%s)",
            c.name, getattr(ctx, "item", "?"), position["status"], position["iou"],
            position["center_offset"],
        )
        return outcome

    # ── What the job reports ──────────────────────────────────────────────
    def summary(self) -> dict[str, Any]:
        """The result's top-level ``references`` block."""
        criteria = {
            name: {
                "matched": entry.get("matched"),
                # name | key | criterion — which rule found the examples.
                "matched_by": entry.get("matched_by", "name"),
                "use": (entry.get("options") or {}).get("use", True),
                "combine": (entry.get("options") or {}).get("combine", "any"),
                "position": (entry.get("options") or {}).get("position", "off"),
                "examples": [
                    {"reference_id": e["reference_id"], "criterion": e["criterion"],
                     "polarity": e["polarity"], "expected": e["expected"]}
                    for e in entry.get("examples") or []
                ],
            }
            for name, entry in (self.plan.get("criteria") or {}).items()
        }
        auto = self.mode == "auto"
        return {
            "mode": self.mode,
            "requested": self.plan.get("requested"),
            "pool": self.plan.get("pool"),
            "pool_truncated": bool(self.plan.get("pool_truncated")),
            "resolved": self.selected_ids() if auto else list(self.plan.get("resolved") or []),
            "inherited": bool(self.plan.get("inherited")),
            "criteria": criteria,
            "selection": [
                {k: v for k, v in record.items() if k != "raw"}
                for _, record in sorted(self._selections.items())
            ],
            # The guided SCORING calls — references.json lists the box loop's
            # guided calls too, each with its `kind`.
            "calls": sum(1 for c in self._calls if c.get("kind", "scoring") == "scoring"),
            "selection_calls": sum(
                1 for r in self._selections.values() if r["status"] in ("ok", "failed")
            ),
        }

    def artifact(self) -> dict[str, Any]:
        """The ``references.json`` body: the plan, and every guided call's
        images — which example went into which call, scoring and box loop
        alike (each call's ``kind`` says which)."""
        return {
            "summary": self.summary(),
            "catalogue": self.plan.get("catalogue"),
            # Every selection with the model's own answer beside the validated one.
            "selection": [record for _, record in sorted(self._selections.items())],
            "calls": list(self._calls),
        }


# ---------------------------------------------------------------------------
# The reference page's description (creation job)
# ---------------------------------------------------------------------------


async def describe_page(
    image_b64: str, *, document_kind: str = "image", model: Optional[ModelSpec] = None,
) -> str:
    """The model's description of one page image, cut to the length cap.

    ``model`` is the creation job's (POST /references ``model``; None = the
    registry default) — always a vision model, the submit saw to that.

    Raises:
        llm.client.LLMCallError: The call failed or never produced a
            description (after MAX_LLM_RETRIES).
    """
    return await llm_client.call_model(
        build_describe_prompt(image_b64, document_kind=document_kind, model=model),
        label="reference/describe",
        validator=description_validator(REFERENCE_DESCRIPTION_MAX_CHARS),
        model=model,
    )
