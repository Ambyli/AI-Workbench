"""The job runners — the classifier's actual work, decoupled from queueing.

One runner per job type. Each takes the JSON-safe payload ``jobs.payloads``
built at enqueue time and returns the result dict that ends up in
``GET /jobs/{id}``'s ``result`` field. Nothing here knows about the job
registry or the worker pool — that is ``jobs.queue``.

    run_assess()    — decode each document's bytes, re-validate the criteria,
                      load every page of every document, run
                      ``analysis.analyze_document``.
    run_reference() — build one reference (POST /references): load its page,
                      run the pipeline for whatever the caller (or the
                      ``from_job`` it was saved from) did not supply, merge
                      (``references.finalize``), describe the page when no
                      description was given (``analysis.references``), render
                      its files (``references.render``), mark it ``ready``.
    _load()         — one document's base64 → Document, run in a worker thread.

Loading is off the event loop: a PDF renders every page at
CLASSIFIER_PDF_RENDER_DPI and an image is decoded and EXIF-rotated, which is
seconds of CPU for a large upload — and on the loop, every other job's model
calls and OCR passes would sit waiting behind it. The documents of one job
load concurrently (image decodes overlap; PDF renders take turns on
common.documents' PyMuPDF lock, per page).

Each uploaded file may be a JPEG/PNG, a PDF of any page count (the total is
capped at submit), an SVG, a .txt, or a .docx — the bytes are stored verbatim
(an SVG's external images already inlined at submit, when that is on) and the
kind is detected when the worker loads them. A document entry's ``warnings``
(what the submit noticed) are handed to the loader, which puts them ahead of
its own on ``Document.warnings``.

How ``run_reference`` treats the pipeline:

  1. Drop the criteria the submit fully supplied (a breakdown AND regions —
     ``[]`` counts: it means "the whole page").
  2. Strip ``depends_on`` from the rest, so every one is evaluated on the
     example — a dependency gate would otherwise SKIP what the reference
     exists to record. The stored criteria keep theirs.
  3. Force ``boxes: true`` on presence / auto ``llm`` criteria that have no
     supplied regions, so the example is located when the model sees it.
  4. ``analyze_document`` with the creation job's id, so the run writes an
     ordinary artifact directory a reviewer can open while the job lives.
     The reference copies what it needs; when the job expires on
     JOB_TTL_HOURS the reference is untouched.

A failure raises — ``jobs.queue.handle_job`` then marks the reference
``failed`` — after removing any half-written reference files. A cancel (a
shutdown mid-job) leaves the reference ``pending`` for the requeued job to
finish. A reference already ``ready`` (a requeue after the reference was
readied but before the job row was) is returned as-is.

Process flow position: called by ``jobs.queue.ClassifierQueue.handle_job``
after a WorkerPool worker has claimed the job.
"""

import asyncio
import base64
from dataclasses import replace
from typing import Any, Optional

from common.vision import PageGeometry, Region

from analysis import analyze_document, load_document_bytes, load_document_page
from analysis.geometry import page_geometry, working_image_of
from analysis.references import describe_page
from api.schemas import CriterionInput
from config import MAX_ITEMS, REFERENCE_DESCRIBE
from jobs.payloads import PAYLOAD_SCHEMA
# A module, not names: tests script the model at `llm.client._send`.
from llm import client as llm_client
from logger import logger
from metrics import reference_describe_total, references_created_total
from references.finalize import merge
from references.model import RECORD_FILE, RECORD_SCHEMA, REGIONS_FILE, file_url
from references.render import render_files
from references.store import UNSET, reference_files, reference_registry, refresh_gauges
from regions.store import store as artifact_store


class StalePayloadError(RuntimeError):
    """The payload was written by a container with an older request shape."""


async def run_assess(payload: dict[str, Any]) -> dict:
    """Execute one assessment job from its stored payload.

    Raises:
        StalePayloadError: The payload is not PAYLOAD_SCHEMA 3 — resubmit.
    """
    if payload.get("schema") != PAYLOAD_SCHEMA:
        raise StalePayloadError(
            "this job was queued by an older classifier with a request shape "
            "that no longer exists (payload schema "
            f"{payload.get('schema')!r}; this container reads {PAYLOAD_SCHEMA}: "
            "a list of documents, every page an item); resubmit it"
        )
    criteria = [CriterionInput.model_validate(c) for c in payload["criteria"]]
    loaded = await asyncio.gather(
        *(asyncio.to_thread(_load, d) for d in payload["documents"]),
        return_exceptions=True,
    )
    # The first failure in DOCUMENT order, not completion order, so a job with
    # two bad files always reports the same one.
    for outcome in loaded:
        if isinstance(outcome, BaseException):
            raise outcome
    # The plan only when the request listed references: an unguided job calls
    # the pipeline exactly as before.
    extra = {"references_plan": payload["references"]} if payload.get("references") else {}
    return await analyze_document(
        list(loaded), criteria, job_id=payload.get("job_id"), **extra
    )


def _load(d: dict[str, Any]):
    """One stored document → ``Document``. Blocking; called via to_thread."""
    return load_document_bytes(
        base64.b64decode(d["file_b64"]),
        d.get("filename"),
        d.get("content_type"),
        keep_source=True,  # a native PDF's text hits need the file re-opened
        # The count read at submit; MAX_ITEMS is the ceiling it passed.
        max_pages=int(d.get("pages") or MAX_ITEMS),
        warnings=d.get("warnings"),
    )


# ---------------------------------------------------------------------------
# Reference creation
# ---------------------------------------------------------------------------


async def run_reference(payload: dict[str, Any]) -> dict:
    """Build the reference a POST /references queued; return the job result.

    Raises:
        StalePayloadError: Not a schema-3 reference payload.
        RuntimeError:      The reference was deleted before or during the job.
        Anything the load, the pipeline or the merge raises — the queue marks
        the reference failed with it.
    """
    if payload.get("schema") != PAYLOAD_SCHEMA or payload.get("type") != "reference":
        raise StalePayloadError(
            "this reference job's payload is not the shape this container reads "
            f"(schema {payload.get('schema')!r}, type {payload.get('type')!r}); "
            "POST /references again"
        )
    reference_id = payload["reference_id"]
    job_id: Optional[str] = payload.get("job_id")
    ref = await reference_registry.get(reference_id)
    if ref is None:
        raise RuntimeError(f"reference {reference_id!r} was deleted before its creation job ran")
    if ref.status == "ready":
        logger.info("run_reference: %s is already ready — nothing to do", reference_id)
        return _job_result(ref.id, ref.record or {}, None, ref.description)

    try:
        return await _build(payload, ref, job_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        reference_files.delete(reference_id)
        references_created_total.labels(outcome="failed").inc()
        raise


async def _build(payload: dict[str, Any], ref: Any, job_id: Optional[str]) -> dict:
    doc = await asyncio.to_thread(_load_page, payload["document"], int(payload.get("page") or 0))
    page = doc.pages[0]
    criteria = [CriterionInput.model_validate(c) for c in payload["criteria"]]
    supplied: dict[str, dict[str, Any]] = payload.get("supplied") or {}
    warnings = list(payload.get("warnings") or [])
    # How the page was read (an SVG's references not drawn / not fetched)
    # belongs on the reference too: it explains a blank where a logo was.
    warnings.extend(w for w in doc.warnings if w not in warnings)

    run = _criteria_to_run(criteria, supplied)
    pipeline: Optional[dict] = None
    observed: dict[str, dict[str, Any]] = {}
    observed_regions: dict[str, list[Region]] = {}
    if run:
        pipeline = await analyze_document([doc], run, job_id=job_id)
        observed, observed_regions = _observations(pipeline, job_id)
    logger.info(
        "run_reference: %s — %d criteria, %d run through the pipeline",
        ref.id, len(criteria), len(run),
    )

    working = working_image_of(page)
    geometry = replace(page_geometry(doc, page, working), page=0)
    record_criteria, merge_warnings = merge(
        criteria, supplied, observed, observed_regions, geometry
    )
    warnings.extend(merge_warnings)

    description: Any = UNSET
    description_source: Any = UNSET
    if ref.description is None and REFERENCE_DESCRIBE:
        try:
            description = await describe_page(
                llm_client.encode_image_to_base64(working), document_kind=doc.kind
            )
            description_source = "model"
            reference_describe_total.labels(outcome="ok").inc()
        except llm_client.LLMCallError as exc:
            warnings.append(f"the page could not be described ({exc}); it has no description")
            reference_describe_total.labels(outcome="failed").inc()

    record = {
        "schema": RECORD_SCHEMA,
        "page": {
            "kind": doc.kind,
            "filename": doc.filename,
            "page": page.index,
            "geometry": geometry.as_dict(),
            "working": {"width": int(working.shape[1]), "height": int(working.shape[0])},
        },
        "criteria": record_criteria,
        "source": payload.get("source") or {},
        "job_id": job_id,
        "warnings": warnings,
    }

    files = await asyncio.to_thread(
        render_files, page.image_bgr, working, geometry, record_criteria
    )
    await asyncio.to_thread(_write_files, ref.id, files, record, geometry)
    if not await reference_registry.finish(
        ref.id, record, description=description, description_source=description_source
    ):
        raise RuntimeError(f"reference {ref.id!r} was deleted while it was being created")
    references_created_total.labels(outcome="ready").inc()
    await refresh_gauges()
    final = description if description is not UNSET else ref.description
    logger.info("run_reference: %s ready (%d warning(s))", ref.id, len(warnings))
    return _job_result(ref.id, record, pipeline, final)


def _load_page(d: dict[str, Any], page: int):
    """The reference's one page → a one-page Document. Blocking."""
    return load_document_page(
        base64.b64decode(d["file_b64"]),
        d.get("filename"),
        d.get("content_type"),
        page=page,
        keep_source=True,  # a native PDF's text hits need the file re-opened
        warnings=d.get("warnings"),
    )


def _criteria_to_run(
    criteria: list[CriterionInput], supplied: dict[str, dict[str, Any]]
) -> list[CriterionInput]:
    """The copies the pipeline evaluates — see the module docstring, 1–3."""
    run = []
    for c in criteria:
        sup = supplied.get(c.name) or {}
        has_answer = "breakdown" in sup or not c.score
        has_regions = "regions" in sup
        if has_answer and has_regions:
            continue
        copy = c.model_copy(update={"depends_on": None})
        if c.type == "llm" and not has_regions and c.options.hint in ("presence", "auto"):
            copy = copy.model_copy(
                update={"options": c.options.model_copy(update={"boxes": True})}
            )
        run.append(copy)
    return run


def _observations(
    result: dict, job_id: Optional[str]
) -> tuple[dict[str, dict[str, Any]], dict[str, list[Region]]]:
    """Each criterion's answer, and its ACCEPTED regions, from a pipeline run.

    The regions come from the job's ``regions.json`` — complete, where the
    result's inline copy is capped at CLASSIFIER_INLINE_REGIONS_MAX — and a
    rejected box-loop attempt (``attrs.accepted: false``) is not a location.
    """
    per = result["assessment"]["per_criterion_scores"]
    observed = {
        name: {
            "status": e.get("status"), "method": e.get("method"), "score": e.get("score"),
            "verdict": e.get("verdict"), "confidence": e.get("confidence"),
            "reason": e.get("reason"), "error": e.get("error"),
        }
        for name, e in per.items()
    }
    stored = (artifact_store.read_json(job_id, "regions.json") or {}) if job_id else {}
    entries = stored.get("criteria") or {}
    regions: dict[str, list[Region]] = {}
    for name, e in per.items():
        raw = (entries.get(name) or {}).get("regions")
        if raw is None:
            raw = e.get("regions") or []
        regions[name] = [
            Region.from_dict(r) for r in raw
            if (r.get("attrs") or {}).get("accepted") is not False
        ]
    return observed, regions


def _write_files(
    reference_id: str, files: dict[str, bytes], record: dict, geometry: PageGeometry
) -> None:
    """The reference directory, from scratch. Blocking."""
    reference_files.delete(reference_id)  # a requeued run must not inherit stale files
    for name, data in files.items():
        reference_files.write(reference_id, name, data)
    reference_files.write_json(reference_id, REGIONS_FILE, {
        "reference_id": reference_id,
        "page": geometry.as_dict(),
        "criteria": {
            name: {
                "slug": entry["slug"],
                "region_source": entry["region_source"],
                "regions": entry["regions"],
            }
            for name, entry in record["criteria"].items()
        },
    })
    reference_files.write_json(reference_id, RECORD_FILE, record)


def _job_result(
    reference_id: str, record: dict, pipeline: Optional[dict], description: Optional[str]
) -> dict:
    """The creation job's ``result``: where the reference is, what it holds,
    and the pipeline run (None when the caller supplied everything).
    ``artifacts`` is the run's, lifted to the top so the job's artifact
    routes treat it like any job's (404 vs 410)."""
    return {
        "reference_id": reference_id,
        "status": "ready",
        "reference_url": f"/references/{reference_id}",
        "record_url": file_url(reference_id, RECORD_FILE),
        "description": description,
        "criteria": {
            name: {
                "expected": entry.get("expected"),
                "region_source": entry.get("region_source"),
                "usable": entry.get("usable"),
            }
            for name, entry in (record.get("criteria") or {}).items()
        },
        "warnings": list(record.get("warnings") or []),
        "artifacts": (pipeline or {}).get("artifacts"),
        "pipeline": pipeline,
    }
