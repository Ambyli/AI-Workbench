"""The analysis pipeline: items, units, aggregate, weigh, store, assemble.

A request is a LIST of documents, each normalised into a
``common.documents.Document`` before any criterion runs, and every page of
every document is one ITEM — numbered globally in document then page order —
so nothing below this line branches on the uploaded file type or on how many
files there were. This module is the ORCHESTRATION; the work lives in its
siblings:

  1. Check each page image is not a thumbnail          analysis.loading
  2. Build one context per item (working image,        analysis.context
     geometry; text layers memoised per item and OCR
     setting, stored as text.p{n}.<key>.json) and one
     group per document
  3. Evaluate every (criterion, item) unit — per-item  analysis.scheduler
     dependency gating, the per-job unit cap, one
     failure never spreading
       cv / text / llm / detector                      analysis.*_eval
  4. Aggregate each criterion: pages → per document,   analysis.aggregate
     documents → per request
  5. Weigh: once per item, and once over the           analysis.weighting
     aggregates (the overall score and verdict)
  6. Store references.json (when the request listed    regions.artifacts
     references), regions.json, the base images, the
     manifest
  7. Assemble the ``schema_version: 3`` result (with ``request.criteria``,
     the validated criteria as run)

    analyze_document() — the pipeline itself; ``jobs.runners.run_assess`` is
                         its only caller in the service (tests call it with a
                         single Document, which is a one-item list).

Step 6 only reads outcomes and ADDS files and keys, so storing regions can
never change a score or a verdict. It always runs when there is a job id —
regions are no longer opt-in; the rendered layers are produced lazily by the
artifact endpoint.

Process flow position: the top of the analysis package. Called by
``jobs.runners.run_assess`` after the job is dequeued.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional, Union

from common.documents import Document, TextLayer

from analysis.aggregate import Aggregated, aggregate_criterion
from analysis.context import (
    DocumentContext,
    DocumentGroup,
    document_text_file,
    item_text_file,
)
from analysis.loading import _validate_image_dimensions
from analysis.outcome import Outcome, empty_localization
from analysis.references import JobReferences
from analysis.scheduler import CriterionUnits, run_units
from analysis.weighting import compute_weighted_score
from api.schemas import CriterionInput
from config import OCR_ENGINE, OCR_MIN_NATIVE_CHARS, TEXT_CHAR_BUDGET
# A module, not names: detector_client.status() reads DETECTOR_URL at call time.
from detector import client as detector_client
from logger import logger
from regions.artifacts import (
    ItemInfo,
    prepare_job_dir,
    text_layer_payload,
    text_link,
    write_job_artifacts,
    write_references_json,
    write_text_layer,
)
from regions.collect import inline_regions

SCHEMA_VERSION = 3


async def analyze_document(
    docs: Union[Document, list[Document]],
    criteria: list[CriterionInput],
    *,
    job_id: Optional[str] = None,
    references_plan: Optional[dict] = None,
) -> dict:
    """Run every criterion on every item of ``docs`` and return the job result.

    Args:
        docs:     The loaded documents (``load_document_bytes``), in request
                  order; a single Document is a one-item list.
        criteria: Validated criteria (``AssessRequest`` rules already hold).
        job_id:   The job the artifact directory is named after. Without one
                  (a direct library call) nothing is written to disk and
                  every ``artifacts`` field is None; the result is otherwise
                  identical.
        references_plan: The plan ``references.resolve`` built at submit (the
                  payload's ``references`` key), or None. One
                  ``JobReferences`` is built from it and shared by every item.

    Returns:
        The ``schema_version: 3`` result — see API.md § Result shape.
    """
    documents = [docs] if isinstance(docs, Document) else list(docs)
    logger.info(
        "analyze_document: %d document(s) %s criteria=%s job=%s",
        len(documents),
        [f"{d.kind}:{len(d.pages)}p" for d in documents],
        [f"{c.name}({c.type}{'' if c.score else ', score:false'})" for c in criteria],
        job_id,
    )

    # Step 1 — a thumbnail is not a document.
    for doc in documents:
        for page in doc.pages:
            if page.image_bgr is not None:
                _validate_image_dimensions(page.width, page.height)

    if job_id:
        await asyncio.to_thread(prepare_job_dir, job_id)

    # Step 2 — one context per item, one group per document, and the sinks
    # that store each text layer the moment a memo produces it.
    def item_sink(n: int):
        async def store(key: str, settings: dict, layer: TextLayer, engine: Optional[str]) -> None:
            await asyncio.to_thread(
                write_text_layer, job_id, item_text_file(n, key),
                text_layer_payload(key, settings, layer, engine, item=n),
            )
        return store

    def joined_sink(i: int):
        async def store(key: str, payload: dict) -> None:
            await asyncio.to_thread(write_text_layer, job_id, document_text_file(i, key), payload)
        return store

    detector_stats = detector_client.DetectorStats()
    job_references = JobReferences(references_plan) if references_plan else None
    items: list[DocumentContext] = []
    groups: list[DocumentGroup] = []
    for i, doc in enumerate(documents):
        members = []
        for page in doc.pages:
            n = len(items)
            ctx = DocumentContext.build(
                doc, page=page, item=n, document=i,
                layer_sink=item_sink(n) if job_id else None,
                detector_stats=detector_stats,
            )
            ctx.references = job_references
            items.append(ctx)
            members.append(ctx)
        groups.append(DocumentGroup(
            index=i, doc=doc, items=members,
            joined_sink=joined_sink(i) if job_id else None,
        ))

    # Step 3 — every (criterion, item) unit, independently.
    runs = await run_units(criteria, items, groups)

    # Step 4 — two-level aggregation per criterion.
    aggregated = {c.name: aggregate_criterion(c, runs[c.name], groups) for c in criteria}

    # Step 5 — the weighted score per item, then over the aggregates.
    doc_scope = {c.name: "scope: document" for c in criteria if runs[c.name].scope == "document"}
    item_rows = []
    for ctx in items:
        outcomes = {
            c.name: runs[c.name].outcomes[ctx.item]
            for c in criteria if c.name not in doc_scope
        }
        scored = compute_weighted_score(criteria, outcomes, not_applicable=doc_scope)
        item_rows.append({
            "item": ctx.item,
            "document": ctx.document,
            "page": ctx.page.index,
            "filename": ctx.doc.filename,
            "overall_score": scored["overall_score"],
            "overall_verdict": scored["overall_verdict"],
            "complete": scored["complete"],
        })
    scoring = compute_weighted_score(criteria, {n: a.final for n, a in aggregated.items()})

    # Step 6 — the artifact directory.
    region_map = {c.name: list(aggregated[c.name].final.regions) for c in criteria}
    localizations = {
        name: a.final.localization
        for name, a in aggregated.items()
        if a.final.localization and a.final.localization.get("calls")
    }
    text_refs = {c.name: _unit_text_refs(runs[c.name]) for c in criteria}
    detector_used = bool(detector_stats.calls or detector_stats.errors)
    artifacts: Optional[dict] = None
    per_criterion_artifacts: dict[str, Optional[dict]] = {}
    if job_id and job_references is not None:
        # Before the manifest, so it lists the file; JSON, so the byte cap
        # never drops it.
        await asyncio.to_thread(write_references_json, job_id, job_references.artifact())
    if job_id:
        infos = [
            ItemInfo(
                item=ctx.item, document=ctx.document, page=ctx.page.index,
                filename=ctx.doc.filename, geometry=ctx.geometry,
                image_bgr=ctx.page.image_bgr,
            )
            for ctx in items
        ]
        artifacts, per_criterion_artifacts = await asyncio.to_thread(
            write_job_artifacts,
            job_id,
            infos,
            region_map,
            criteria,
            localizations=localizations,
            detector=detector_stats.as_dict() if detector_used else None,
            text_refs={name: refs for name, refs in text_refs.items() if refs},
        )

    # Step 7 — the result.
    per_criterion = {
        c.name: _entry(
            c, aggregated[c.name], runs[c.name], items, groups,
            per_criterion_artifacts.get(c.name), job_id,
        )
        for c in criteria
    }
    result = {
        "schema_version": SCHEMA_VERSION,
        "documents": [_document_entry(g) for g in groups],
        "items": item_rows,
        "assessment": {**scoring, "per_criterion_scores": per_criterion},
        "verdict": scoring["overall_verdict"],
        "page_geometry": [_geometry_entry(ctx) for ctx in items],
        "detector": {
            **detector_client.status(),
            **detector_stats.as_dict(),
            "used": detector_used,
        },
        "artifacts": artifacts,
        # What the request's references did — null when it listed none.
        "references": job_references.summary() if job_references is not None else None,
        # The criteria exactly as validated (depends_on, weight, every
        # option as sent) — what POST /references' `from_job` rebuilds a
        # reviewed job's criteria from. Additive: nothing else reads it.
        "request": {"criteria": [c.model_dump(mode="json") for c in criteria]},
    }
    logger.info(
        "analyze_document: verdict=%s overall_score=%s complete=%s items=%d regions=%d",
        scoring["overall_verdict"],
        scoring["overall_score"],
        scoring["complete"],
        len(items),
        sum(len(v) for v in region_map.values()),
    )
    return result


def _unit_text_refs(run: CriterionUnits) -> list[dict]:
    """Every text layer this criterion's units read, tagged with its unit."""
    tag = "document" if run.scope == "document" else "item"
    return [
        {tag: index, **o.text_layer}
        for index, o in sorted(run.outcomes.items())
        if o.text_layer is not None
    ]


def _is_llm(c: CriterionInput, o: Outcome) -> bool:
    return c.type == "llm" or o.method == "llm"


def _item_entry(
    c: CriterionInput,
    o: Outcome,
    *,
    item: Optional[int],
    document: int,
    page: Optional[int],
    job_id: Optional[str],
    covers: Optional[list[int]] = None,
) -> dict[str, Any]:
    """One unit's result inside a criterion's ``items`` list."""
    entry: dict[str, Any] = {"item": item, "document": document, "page": page}
    if covers is not None:
        entry["items"] = covers
    entry.update(
        status=o.status,
        score=o.score,
        verdict=o.verdict,
        confidence=o.confidence,
        reason=o.reason,
        detail=o.detail,
        error=o.error,
        # A COUNT, not the list: the geometry is in the criterion's own
        # (capped) `regions` and, complete, in regions.json — per item via
        # `artifacts.items`.
        regions=len(o.regions),
        text_layer=(
            text_link(job_id, o.text_layer) if (job_id and o.text_layer)
            else (dict(o.text_layer) if o.text_layer else None)
        ),
    )
    if _is_llm(c, o):
        entry["localization"] = o.localization or empty_localization()
    return entry


def _entry(
    c: CriterionInput,
    agg: Aggregated,
    run: CriterionUnits,
    items: list[DocumentContext],
    groups: list[DocumentGroup],
    artifacts: Optional[dict],
    job_id: Optional[str],
) -> dict[str, Any]:
    """One criterion's result — the same keys for every type and status."""
    o = agg.final
    regions, truncated = inline_regions(o.regions)
    localization = None
    if _is_llm(c, o):
        localization = o.localization or empty_localization()
    if run.scope == "document":
        units = [
            _item_entry(
                c, run.outcomes[g.index], item=None, document=g.index, page=None,
                job_id=job_id, covers=[ctx.item for ctx in g.items],
            )
            for g in groups
        ]
    else:
        units = [
            _item_entry(
                c, run.outcomes[ctx.item], item=ctx.item, document=ctx.document,
                page=ctx.page.index, job_id=job_id,
            )
            for ctx in items
        ]
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
        "complete": o.complete,
        "aggregate_used": agg.used,
        "items": units,
    }


def _geometry_entry(ctx: DocumentContext) -> dict[str, Any]:
    """One item's frame; nulls (with the item) when it has no page image."""
    if ctx.geometry is None:
        return {"item": ctx.item, "page": ctx.item, "width": None, "height": None,
                "working_scale": None, "pdf_points": None}
    return {"item": ctx.item, **ctx.geometry.as_dict()}


def _document_entry(group: DocumentGroup) -> dict[str, Any]:
    """One document: what it is, its items, and what was done to read it."""
    doc = group.doc
    passes = [p for ctx in group.items for p in ctx.ocr_passes]
    joined = []
    for key, future in group._joined.items():
        if future.done() and not future.cancelled() and future.exception() is None:
            j = future.result()
            joined.append({"key": key, "source": j.source(), "chars": len(j.text),
                           "file": document_text_file(group.index, key)})
    return {
        "index": group.index,
        "filename": doc.filename,
        "kind": doc.kind,
        "pages": len(doc.pages),
        "items": [ctx.item for ctx in group.items],
        # How the document was read that is worth knowing but is not an
        # error — today an SVG's external references: not rendered (MuPDF
        # never fetches), or not fetched and why (CLASSIFIER_SVG_FETCH_IMAGES).
        # [] for every other kind.
        "warnings": list(doc.warnings),
        "document_info": {
            "content_type": doc.content_type,
            "size_bytes": doc.size_bytes,
            "has_image": doc.has_images(),
            "native_text_chars": sum(
                len(p.text) for p in doc.pages if p.text_source == "native"
            ),
            "ocr": {
                # Not ocr_engine_status(): its `available` loads the engine,
                # and a job that needed no OCR must not pay for that here.
                "engine": OCR_ENGINE or "none",
                "min_native_chars": OCR_MIN_NATIVE_CHARS,
                # One entry per distinct text layer per item the criteria
                # asked for, in the order they were produced — each is
                # text.p{item}.<key>.json.
                "layers": passes,
                # The joined text a scope-"document" search read — each is
                # text.d{index}.<key>.json.
                "document_layers": joined,
            },
            "llm_text_char_budget": TEXT_CHAR_BUDGET,
        },
    }
