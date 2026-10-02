"""The `cv` evaluator: one OpenCV detector on the page, and its geometry.

``cv/`` holds the detectors themselves and knows nothing about documents;
this module is the bridge — it runs the detector on the page's working image
(in a worker thread, so a slow detector does not hold the event loop other
criteria are running on) and turns the geometry the detector already computed
into ``Region`` objects in ORIGINAL page pixels.

When no OpenCV detector matches the criterion name, ``options.fallback``
decides what answers instead — resolved at submit to "detector" when
DETECTOR_URL is configured and "llm" otherwise, unless the caller chose:

    fallback "detector"  the open-vocabulary detector scores it from its
                         boxes (``analysis.detector_eval``), at
                         DETECTOR_MIN_SCORE
    fallback "llm"       the vision model scores it with the default llm
                         options (hint auto, no boxes, ocr auto) — guided by
                         the request's references exactly as an llm criterion
                         is (``options.reference`` on a cv criterion is only
                         accepted when this is the path that answers)

The result's ``method`` says which path actually answered.

    evaluate()     — the shared evaluator interface.
    _cv_regions()  — detector output (working-image dicts) → Regions.

Process flow position: one of the four evaluators ``analysis.scheduler``
dispatches to.
"""

from __future__ import annotations

import asyncio

from common.vision import PageGeometry, Region, rescale_region

from analysis import detector_eval, llm_eval
from analysis.context import DocumentContext
from analysis.outcome import Outcome, skipped
from analysis.result_specs import (
    AGGREGATE_FIELD,
    METRIC_FIELD,
    VALUE_FIELD,
    FieldSpec,
    ResultSpec,
    result_spec,
)
from api.criterion_options import LLMOptions
from api.schemas import CriterionInput
from config import DETECTOR_MIN_SCORE
from cv import get_detector
from logger import logger

# The detail a `cv` result carries — the detector's own result
# (cv.result.cv_result) plus the `image` frame added below — declared here and
# registered by type (analysis.result_specs). Each DETECTOR's measurement
# keys and states are declared beside that detector (cv.result.describes,
# GET /cv-detectors). Checked against real output by
# unit-tests/classifier/test_result_specs.py and test_cv_measurements.py.
CV_RESULT = ResultSpec(
    type="cv",
    metric="(per detector — GET /cv-detectors)",
    metric_from="measurements",
    fields={
        "metric": METRIC_FIELD,
        "value": VALUE_FIELD,
        "detector": FieldSpec("The OpenCV function that ran", "string", stable=True),
        "measurements": FieldSpec(
            "Everything the detector counted — its keys: GET /cv-detectors", "object",
        ),
        "thresholds": FieldSpec("The lines that decide the verdict", "object", stable=True),
        "parameters": FieldSpec("Config values it measured with", "object", stable=True),
        "state": FieldSpec(
            "A categorical outcome — its values: GET /cv-detectors", "string",
            when="the detector declares states",
        ),
        "image": FieldSpec(
            "The working-image frame the *_px measurements are in", "object", stable=True,
        ),
        "aggregate": AGGREGATE_FIELD,
    },
    notes=(
        "A cv criterion answered by its `fallback` has the llm or detector shape; "
        "`method` says which.",
    ),
)


@result_spec(CV_RESULT)
async def evaluate(c: CriterionInput, ctx: DocumentContext) -> Outcome:
    """Run the named OpenCV detector on the page, or the resolved fallback."""
    detector = get_detector(c.name)
    if detector is None:
        fallback = c.resolved_options()["fallback"]
        logger.info("cv_eval: no OpenCV detector for '%s' — fallback=%s", c.name, fallback)
        if fallback == "detector":
            outcome = await detector_eval.evaluate_label(c.name, ctx, DETECTOR_MIN_SCORE)
        else:
            guide = ctx.references.guide(c, ctx.item) if ctx.references is not None else None
            outcome = await llm_eval.evaluate_with(
                c.name, LLMOptions().resolve(c.name), ctx, reference=guide
            )
        note = f"No OpenCV detector matches '{c.name}'; answered by the {fallback} fallback."
        outcome.reason = f"{note} {outcome.reason or ''}".strip()
        return outcome

    if not ctx.has_image:
        return skipped(
            f"Skipped - document has no page image ({ctx.doc.kind} documents are "
            "text-only, so OpenCV criteria cannot be evaluated).",
            method="cv",
        )

    result = dict(await asyncio.to_thread(detector, ctx.working_image))
    raw_regions = result.pop("regions", None) or []
    regions = _cv_regions(raw_regions, c.name, ctx.geometry) if ctx.geometry else []
    # The frame every *_px measurement is in: the working image the detector
    # saw, not the original page (regions are rescaled; measurements are not).
    detail = dict(result.get("detail") or {})
    height, width = ctx.working_image.shape[:2]
    detail["image"] = {
        "width": int(width),
        "height": int(height),
        "frame": "working",
        "working_scale": ctx.geometry.working_scale if ctx.geometry else 1.0,
    }
    logger.debug(
        "cv_eval: '%s' score=%s %s=%s regions=%d",
        c.name, result.get("score"), detail.get("metric"), detail.get("value"), len(regions),
    )
    return Outcome(
        method="cv",
        score=result.get("score"),
        verdict=result.get("verdict"),
        confidence=result.get("confidence"),
        reason=result.get("reason"),
        detail=detail,
        regions=regions,
    )


def _cv_regions(raw: list[dict], label: str, geometry: PageGeometry) -> list[Region]:
    """Detector output (working-image dicts) → Regions in original pixels."""
    regions: list[Region] = []
    for item in raw:
        points = item.get("points") or []
        if len(points) < 2:
            continue
        region = Region(
            page=geometry.page,
            kind=item.get("kind", "box"),
            points=points,
            label=label,
            score=item.get("score"),
            source="cv",
            attrs=dict(item.get("attrs") or {}),
        )
        regions.append(rescale_region(region, geometry.working_scale, geometry))
    return regions
