"""The analysis pipeline: build the context, schedule, weigh, assemble, store.

Everything a request asks for is normalised into a single-page
``common.documents.Document`` before any criterion runs, so nothing below
this line branches on the uploaded file type. This module is the
ORCHESTRATION; the work lives in its siblings:

  1. Check the page image is not a thumbnail           analysis.loading
  2. Build the shared, read-only document context      analysis.context
     (working image, geometry; text layers memoised
     per OCR setting and stored as text.<key>.json)
  3. Evaluate every criterion — dependency waves, the  analysis.scheduler
     per-job cap, one failure never spreading
       cv / text / llm / detector                      analysis.*_eval
  4. Weigh what counts, say whether it is complete     analysis.weighting
  5. Store regions.json, the base image, the manifest  regions.artifacts
  6. Assemble the result: one shape per criterion

    analyze_document() — the pipeline itself; ``jobs.runners.run_assess`` is
                         its only caller.

Step 5 only reads outcomes and ADDS files and keys, so storing regions can
never change a score or a verdict. It always runs when there is a job id —
regions are no longer opt-in; the rendered layers are produced lazily by the
artifact endpoint.

Process flow position: the top of the analysis package. Called by
``jobs.runners.run_assess`` after the job is dequeued.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from common.documents import Document, TextLayer

from analysis.context import DocumentContext
from analysis.loading import _validate_image_dimensions
from analysis.outcome import Outcome, empty_localization
from analysis.scheduler import run_criteria
from analysis.weighting import compute_weighted_score
from api.schemas import CriterionInput
from config import OCR_ENGINE, OCR_MIN_NATIVE_CHARS, TEXT_CHAR_BUDGET
# A module, not names: detector_client.status() reads DETECTOR_URL at call time.
from detector import client as detector_client
from logger import logger
from regions.artifacts import (
    prepare_job_dir,
    text_layer_payload,
    write_job_artifacts,
    write_text_layer,
)
from regions.collect import inline_regions

SCHEMA_VERSION = 2


async def analyze_document(
    doc: Document,
    criteria: list[CriterionInput],
    *,
    job_id: Optional[str] = None,
) -> dict:
    """Run every criterion on ``doc`` and return the job result.

    Args:
        doc:      A loaded single-page document (``load_document_bytes``).
        criteria: Validated criteria (``AssessRequest`` rules already hold).
        job_id:   The job the artifact directory is named after. Without one
                  (a direct library call) nothing is written to disk and
                  every ``artifacts`` field is None; the result is otherwise
                  identical.

    Returns:
        The ``schema_version: 2`` result — see API.md § Result shape.
    """
    page = doc.pages[0]
    logger.info(
        "analyze_document: kind=%s image=%s criteria=%s job=%s",
        doc.kind,
        page.image_bgr is not None,
        [f"{c.name}({c.type}{'' if c.score else ', score:false'})" for c in criteria],
        job_id,
    )

    # Step 1 — a thumbnail is not a document.
    if page.image_bgr is not None:
        _validate_image_dimensions(page.width, page.height)

    # Step 2 — the context, and the sink that stores each text layer the
    # moment the memo produces it.
    async def store_layer(key: str, settings: dict, layer: TextLayer, engine: Optional[str]) -> None:
        await asyncio.to_thread(
            write_text_layer, job_id, key,
            text_layer_payload(key, settings, layer, engine),
        )

    if job_id:
        await asyncio.to_thread(prepare_job_dir, job_id)
    ctx = DocumentContext.build(doc, layer_sink=store_layer if job_id else None)

    # Step 3 — every criterion, independently.
    outcomes = await run_criteria(criteria, ctx)

    # Step 4 — the weighted score over what counts.
    scoring = compute_weighted_score(criteria, outcomes)

    # Step 5 — the artifact directory.
    region_map = {c.name: list(outcomes[c.name].regions) for c in criteria}
    localizations = {
        c.name: outcomes[c.name].localization
        for c in criteria
        if outcomes[c.name].localization and outcomes[c.name].localization.get("calls")
    }
    text_refs = {
        c.name: outcomes[c.name].text_layer
        for c in criteria
        if outcomes[c.name].text_layer is not None
    }
    detector_used = bool(ctx.detector_stats.calls or ctx.detector_stats.errors)
    artifacts: Optional[dict] = None
    per_criterion_artifacts: dict[str, Optional[dict]] = {}
    if job_id:
        artifacts, per_criterion_artifacts = await asyncio.to_thread(
            write_job_artifacts,
            job_id,
            doc,
            ctx.geometry,
            region_map,
            criteria,
            localizations=localizations,
            detector=ctx.detector_stats.as_dict() if detector_used else None,
            text_refs=text_refs,
        )

    # Step 6 — the result.
    per_criterion = {
        c.name: _entry(c, outcomes[c.name], per_criterion_artifacts.get(c.name))
        for c in criteria
    }
    result = {
        "schema_version": SCHEMA_VERSION,
        "document_info": _document_info(doc, ctx, detector_used),
        "assessment": {**scoring, "per_criterion_scores": per_criterion},
        "verdict": scoring["overall_verdict"],
        "page_geometry": ctx.geometry.as_dict() if ctx.geometry is not None else None,
        "artifacts": artifacts,
    }
    logger.info(
        "analyze_document: verdict=%s overall_score=%s complete=%s regions=%d",
        scoring["overall_verdict"],
        scoring["overall_score"],
        scoring["complete"],
        sum(len(v) for v in region_map.values()),
    )
    return result


def _entry(c: CriterionInput, o: Outcome, artifacts: Optional[dict]) -> dict[str, Any]:
    """One criterion's result — the same keys for every type and status."""
    regions, truncated = inline_regions(o.regions)
    localization = None
    if c.type == "llm" or o.method == "llm":
        localization = o.localization or empty_localization()
    return {
        "status": o.status,
        "type": c.type,
        "method": o.method,
        "scored": c.score,
        "score": o.score,
        "verdict": o.verdict,
        "confidence": o.confidence,
        "reason": o.reason,
        "detail": o.detail,
        "regions": regions,
        "regions_truncated": truncated,
        "artifacts": artifacts,
        "localization": localization,
        "options_used": c.resolved_options(),
        "error": o.error,
    }


def _document_info(doc: Document, ctx: DocumentContext, detector_used: bool) -> dict:
    """What was loaded, and what was done to read it."""
    page = ctx.page
    return {
        "kind": doc.kind,
        "filename": doc.filename,
        "content_type": doc.content_type,
        "size_bytes": doc.size_bytes,
        "width": page.width,
        "height": page.height,
        "has_image": page.image_bgr is not None,
        "native_text_chars": len(page.text) if page.text_source == "native" else 0,
        "ocr": {
            # Not ocr_engine_status(): its `available` loads the engine, and
            # a job that needed no OCR must not pay for that here.
            "engine": OCR_ENGINE or "none",
            "min_native_chars": OCR_MIN_NATIVE_CHARS,
            # One entry per distinct text layer the criteria asked for, in
            # the order they were produced — each is text.<key>.json.
            "layers": list(ctx.ocr_passes),
        },
        "llm_text_char_budget": TEXT_CHAR_BUDGET,
        # What the open-vocabulary detector did for this job. Present even
        # when unused, because "configured: false" and "configured: true,
        # calls: 0" are different facts.
        "detector": {
            **detector_client.status(),
            **ctx.detector_stats.as_dict(),
            "used": detector_used,
        },
    }
