"""The /references routes: save a worked example, list, read, edit, delete.

    POST   /references                   202 {reference_id, status: "pending", job_id}
    GET    /references                   summaries, newest first (?status= ?tag= ?limit= ?offset=)
    GET    /references/{id}              the summary, the full record, the file URLs
    GET    /references/{id}/files/{name} page.jpg | working.jpg | c.<slug>.jpg |
                                         regions.json | record.json
    PATCH  /references/{id}              title / description / tags — nothing else
    DELETE /references/{id}              204; 409 while a queued or running job uses it
                                         (``?force=true`` deletes anyway)

POST takes the same two encodings /assess does, parsed into ONE model
(``api.reference_schemas.ReferenceRequest``):

    application/json     {"document": {"type", "data", "filename"}, "page": 0,
                          "criteria": [...], "breakdown": {...}, "regions": {...},
                          "region_units": "px", "title", "description", "tags"}
                         — or "from_job" + "from_job_item" instead of a document.
    multipart/form-data  ONE `file` part (the legacy `image` alias too), and the
                          other keys as fields — `criteria`, `breakdown`,
                          `regions` and `tags` holding JSON (`tags` may instead
                          repeat, one tag per field).

Then, before anything is queued — every one of these is an error on THIS
request, never a creation job that fails a minute later:

  1. the model (``ReferenceRequest``): shape, criteria rules, breakdown
     verdicts, region shapes and grid ranges — 400;
  2. CLASSIFIER_REFERENCE_MAX_COUNT references already exist — 409;
  3. the source:
       ``document``  its bytes resolved like /assess (base64, inline text, an
                     SSRF-checked URL), its kind from the bytes: a JPEG/PNG,
                     an SVG, or a PDF with ``page`` in range; .txt / .docx are
                     a 400 (a reference shows the model a page image). The
                     page's pixel size is read without rendering it. An SVG's
                     external images are fetched and inlined here, as for
                     /assess, when CLASSIFIER_SVG_FETCH_IMAGES is on;
       ``from_job``  404 unknown job; 409 not a completed /assess job; 400 an
                     item it does not have, or one with no page image; 410 its
                     artifacts are gone (the TTL sweeper, a DELETE, or the byte
                     cap dropped ``p{item}.base.jpg``). The page is that base
                     image — the job's ORIGINAL pixels, so its regions apply
                     unchanged. The job's criteria come from its
                     ``result.request.criteria``, or — for a job that predates
                     that block — are rebuilt from each criterion's
                     ``options_used``, with a warning on the reference (no
                     ``depends_on``; ``weight`` only where the breakdown kept
                     it). Each criterion whose type, ``score`` and resolved
                     options match the job's takes the job's answer on that
                     item and its ACCEPTED regions there as supplied;
  4. every breakdown / regions key names a criterion; every pixel region lies
     inside the page (``grid`` regions are converted to pixels here) — 400;
  5. register the creation job ("staging", metadata type "reference"), insert
     the ``pending`` reference, write the payload, wake a worker — 202.

The creation job (``jobs.runners.run_reference``) does the rest and marks the
reference ``ready`` or ``failed``. References are kept until DELETE: nothing
sweeps them, and their creation jobs expire on JOB_TTL_HOURS like any job.

Process flow position: the top of the stack beside ``api.assess``. Mounted
by ``main``; hands work to ``jobs.queue``.
"""

from __future__ import annotations

import base64
import io
import json
from dataclasses import dataclass, field
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from common.documents import (
    UnsupportedDocumentError,
    detect_kind,
    pdf_page_count,
    pdf_page_size,
    svg_page_size,
)
from common.vision import content_type_for

from analysis import load_input_bytes, resolve_svg_images, validate_content_type
from analysis.loading import _validate_image_dimensions
from api.assess import _json_filename
from api.reference_schemas import ReferencePatch, ReferenceRequest
from api.schemas import ClassifierMetadata, CriterionInput, check_criteria_rules, validation_message
# A module, not names: SVG_FETCH_IMAGES is read at call time (see api.assess).
import config
from config import (
    LLM_BBOX_GRID,
    LLM_BBOX_MAX_ATTEMPTS,
    PDF_RENDER_DPI,
    REFERENCE_MAX_COUNT,
)
from jobs.payloads import SubmittedDocument, build_reference_payload
from jobs.queue import jobs_registry, queue
from logger import logger
from metrics import jobs_total
from middleware import request_id_var
from references.model import FILE_NAME_RE, STATUSES, file_url, is_reference_id, new_reference_id
from references.store import UNSET, reference_files, reference_registry, refresh_gauges
from regions.artifacts import base_image_name
from regions.store import store as artifact_store

router = APIRouter(tags=["references"])

# Multipart fields this endpoint reads. `file` / `image` is the ONE page.
_FORM_FIELDS = {
    "file", "image", "page", "from_job", "from_job_item", "criteria", "breakdown",
    "regions", "region_units", "title", "description", "tags",
}
_JSON_FORM_FIELDS = ("criteria", "breakdown", "regions")
_INT_FORM_FIELDS = ("page", "from_job_item")

# How far a pixel region may overshoot the page before it is refused: one
# pixel of rounding (a PDF page's render size is an integer rectangle).
_PX_SLACK = 1.0

_OPENAPI = {
    "requestBody": {
        "required": True,
        "content": {
            "application/json": {"schema": ReferenceRequest.model_json_schema()},
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "file": {"type": "string", "format": "binary",
                                 "description": "ONE JPEG, PNG, SVG or PDF"},
                        "page": {"type": "integer"},
                        "from_job": {"type": "string"},
                        "from_job_item": {"type": "integer"},
                        "criteria": {"type": "string", "description": "JSON array"},
                        "breakdown": {"type": "string", "description": "JSON object"},
                        "regions": {"type": "string", "description": "JSON object"},
                        "region_units": {"type": "string", "enum": ["px", "grid"]},
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                        "tags": {"type": "string",
                                 "description": "JSON array, or repeat the field"},
                    },
                }
            },
        },
    }
}


def _bad(detail: str) -> HTTPException:
    return HTTPException(status_code=400, detail=detail)


def _validate(body: Any) -> ReferenceRequest:
    try:
        return ReferenceRequest.model_validate(body)
    except ValidationError as exc:
        raise _bad(f"Invalid reference: {validation_message(exc)}")


@dataclass
class _Upload:
    raw: bytes
    filename: str
    declared: Optional[str]


@dataclass
class _Source:
    """The example page as submit resolved it, plus what it already knows."""

    raw: bytes
    filename: str
    declared: Optional[str]
    kind: str
    pages: int
    page: int
    width: int
    height: int
    criteria: list[CriterionInput]
    source: dict[str, Any]
    supplied: dict[str, dict[str, Any]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    # What the submit noticed about the page's FILE (an SVG image not
    # fetched) — rides on the payload's document entry, not the reference's
    # own warnings; the creation job merges it into those.
    document_warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Parsing — two encodings, one model
# ---------------------------------------------------------------------------


async def _from_json(request: Request) -> tuple[ReferenceRequest, Optional[_Upload]]:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise _bad(f"Request body is not valid JSON: {exc}")
    if not isinstance(body, dict):
        raise _bad("Request body must be a JSON object")
    model = _validate(body)
    upload = None
    if model.document is not None:
        raw = await load_input_bytes(model.document.data, model.document.type)
        upload = _Upload(raw=raw, filename=_json_filename(model.document), declared=None)
    return model, upload


async def _from_form(request: Request) -> tuple[ReferenceRequest, Optional[_Upload]]:
    form = await request.form()
    keys = list(form.keys())
    if "text" in keys:
        raise _bad("A reference is one page image; inline 'text' has none. Send a 'file'.")
    unknown = sorted({k for k in keys if k not in _FORM_FIELDS})
    if unknown:
        raise _bad(f"Unknown form field(s) {unknown}; expected {sorted(_FORM_FIELDS)}")
    parts = form.getlist("file") + form.getlist("image")
    if len(parts) > 1:
        raise _bad("A reference is ONE page: send one 'file' part")
    for key in _FORM_FIELDS - {"file", "image", "tags"}:
        if len(form.getlist(key)) > 1:
            raise _bad(f"Send '{key}' once")

    body: dict[str, Any] = {}
    upload = None
    if parts:
        part = parts[0]
        if isinstance(part, str):
            raise _bad("'file' must be a file part, not a plain form value")
        validate_content_type(part.content_type)
        raw = await part.read()
        if not raw:
            raise _bad(f"Empty document file {part.filename or '(unnamed)'!r}")
        filename = part.filename or "upload"
        upload = _Upload(raw=raw, filename=filename, declared=part.content_type)
        body["document"] = {
            "type": "base64", "data": base64.b64encode(raw).decode("ascii"),
            "filename": filename,
        }
    for key in _JSON_FORM_FIELDS:
        value = form.get(key)
        if value is None:
            continue
        try:
            body[key] = json.loads(str(value))
        except json.JSONDecodeError as exc:
            raise _bad(f"'{key}' is not valid JSON: {exc}")
    for key in _INT_FORM_FIELDS:
        value = form.get(key)
        if value is None:
            continue
        try:
            body[key] = int(str(value))
        except ValueError:
            raise _bad(f"'{key}' must be an integer, got {str(value)[:40]!r}")
    for key in ("from_job", "region_units", "title", "description"):
        value = form.get(key)
        if value is not None:
            body[key] = str(value)
    tags = [str(v) for v in form.getlist("tags")]
    if len(tags) == 1 and tags[0].lstrip().startswith("["):
        try:
            body["tags"] = json.loads(tags[0])
        except json.JSONDecodeError as exc:
            raise _bad(f"'tags' is not valid JSON: {exc}")
    elif tags:
        body["tags"] = tags
    return _validate(body), upload


# ---------------------------------------------------------------------------
# The source page
# ---------------------------------------------------------------------------


def _image_size(raw: bytes) -> tuple[int, int]:
    """An image's (width, height) AFTER EXIF rotation, from its header only —
    the same frame ``analysis.loading`` decodes it into."""
    from PIL import Image

    try:
        with Image.open(io.BytesIO(raw)) as img:
            width, height = img.size
            orientation = img.getexif().get(0x0112)
    except Exception as exc:  # PIL raises a zoo of exception types
        raise _bad(f"Could not read the image: {exc}")
    if orientation in (5, 6, 7, 8):  # the transposing orientations
        width, height = height, width
    return int(width), int(height)


def _resolve_document(model: ReferenceRequest, upload: _Upload) -> _Source:
    try:
        kind = detect_kind(upload.raw, filename=upload.filename, content_type=upload.declared)
    except UnsupportedDocumentError as exc:
        raise _bad(f"document {upload.filename!r}: {exc}")
    if kind not in ("image", "pdf", "svg"):
        raise _bad(
            f"document {upload.filename!r} is {kind}, which has no page image; a reference "
            "is one JPEG/PNG, one SVG, or one PDF page — the example is shown to the "
            "vision model"
        )
    page = model.page or 0
    pages = 1
    if kind == "image":
        if page != 0:
            raise _bad(f"an image has one page (page 0); got page {page}")
        width, height = _image_size(upload.raw)
    elif kind == "svg":
        if page != 0:
            raise _bad(f"an SVG has one page (page 0); got page {page}")
        try:
            width, height = svg_page_size(upload.raw, render_dpi=PDF_RENDER_DPI)
        except UnsupportedDocumentError as exc:
            raise _bad(f"document {upload.filename!r}: {exc}")
    else:
        try:
            pages = pdf_page_count(upload.raw)
            if page >= pages:
                raise _bad(f"page {page} is not in {upload.filename!r} ({pages} page(s))")
            width, height = pdf_page_size(upload.raw, page, render_dpi=PDF_RENDER_DPI)
        except UnsupportedDocumentError as exc:
            raise _bad(f"document {upload.filename!r}: {exc}")
    _validate_image_dimensions(width, height)
    return _Source(
        raw=upload.raw, filename=upload.filename, declared=upload.declared, kind=kind,
        pages=pages, page=page, width=width, height=height,
        criteria=list(model.criteria or []),
        source={"kind": "document", "job_id": None, "item": None, "page": page},
    )


def _same_question(a: CriterionInput, b: CriterionInput) -> bool:
    """Whether a job's answer to ``b`` answers ``a`` — same type, same
    ``score``, same resolved options. Weight and depends_on change how an
    answer counts, not what it is."""
    return a.type == b.type and a.score == b.score and a.resolved_options() == b.resolved_options()


def _legacy_criteria(job_id: str, result: dict) -> tuple[list[CriterionInput], str]:
    """Rebuild a pre-``request``-block job's criteria from its result."""
    assessment = result.get("assessment") or {}
    per = assessment.get("per_criterion_scores") or {}
    weights = ((assessment.get("weighted_score_breakdown") or {}).get("per_criterion")) or {}
    out: list[CriterionInput] = []
    defaulted: list[str] = []
    for name, entry in per.items():
        options = dict(entry.get("options_used") or {})
        if entry.get("type") == "llm" and isinstance(options.get("max_attempts"), int):
            options["max_attempts"] = min(options["max_attempts"], LLM_BBOX_MAX_ATTEMPTS)
        data: dict[str, Any] = {
            "name": name,
            "type": entry.get("type", "llm"),
            "score": bool(entry.get("scored", True)),
            "options": options,
        }
        weight = (weights.get(name) or {}).get("weight")
        if weight:
            data["weight"] = weight
        else:
            defaulted.append(name)
        try:
            out.append(CriterionInput.model_validate(data))
        except ValidationError as exc:
            raise HTTPException(
                status_code=409,
                detail=f"job {job_id!r}'s criterion {name!r} cannot be rebuilt on this "
                       f"container ({validation_message(exc)}); send 'criteria' explicitly",
            )
    warning = (
        f"job {job_id!r} predates result.request, so its criteria were rebuilt from "
        "options_used: depends_on is not recorded there and was dropped"
        + (f", and weight defaulted to 1 for {defaulted}" if defaulted else "")
        + "."
    )
    return out, warning


async def _resolve_from_job(model: ReferenceRequest) -> _Source:
    job_id = model.from_job or ""
    job = await jobs_registry.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"no job {job_id!r}")
    job_type = job.metadata.get("type", "assess")
    if job_type != "assess":
        raise HTTPException(
            status_code=409,
            detail=f"job {job_id!r} is a {job_type!r} job; from_job takes a completed /assess job",
        )
    if job.phase != "completed" or not isinstance(job.result, dict):
        raise HTTPException(
            status_code=409,
            detail=f"job {job_id!r} is {job.phase}; from_job takes a COMPLETED /assess job",
        )
    result = job.result
    item = model.from_job_item or 0
    geometries = result.get("page_geometry") or []
    if not 0 <= item < len(result.get("items") or []):
        raise _bad(
            f"job {job_id!r} has {len(result.get('items') or [])} item(s); "
            f"from_job_item {item} is not one of them"
        )
    geo = next((g for g in geometries if g.get("item") == item), None) or {}
    if not geo.get("width") or not geo.get("height"):
        raise _bad(f"job {job_id!r} item {item} has no page image (a .txt / .docx page)")
    gone = (
        f"job {job_id!r}'s artifacts are gone — the TTL sweeper or a DELETE removed "
        "them, or the byte cap dropped the page image; re-run the job, or send the "
        "page as 'document'"
    )
    raw = artifact_store.open(job_id, base_image_name(item))
    regions_doc = artifact_store.read_json(job_id, "regions.json")
    if raw is None or regions_doc is None:
        raise HTTPException(status_code=410, detail=gone)

    warnings: list[str] = []
    stored = (result.get("request") or {}).get("criteria")
    if stored is not None:
        try:
            job_criteria = [CriterionInput.model_validate(c) for c in stored]
        except ValidationError as exc:
            raise HTTPException(
                status_code=409,
                detail=f"job {job_id!r}'s criteria do not validate on this container "
                       f"({validation_message(exc)}); send 'criteria' explicitly",
            )
    else:
        job_criteria, warning = _legacy_criteria(job_id, result)
        warnings.append(warning)
    # A guided job's criteria carry options.reference; a reference's own
    # criteria are never guided, so it is dropped (the answer still stands).
    job_criteria = [
        c.model_copy(update={"options": c.options.model_copy(update={"reference": None})})
        if getattr(c.options, "reference", None) is not None else c
        for c in job_criteria
    ]
    criteria = list(model.criteria) if model.criteria is not None else job_criteria
    if model.criteria is None:
        try:
            check_criteria_rules(criteria)
        except ValueError as exc:
            raise _bad(f"Invalid reference: the job's criteria: {exc}")

    per = (result.get("assessment") or {}).get("per_criterion_scores") or {}
    region_entries = regions_doc.get("criteria") or {}
    job_by_name = {c.name: c for c in job_criteria}
    supplied: dict[str, dict[str, Any]] = {}
    for c in criteria:
        theirs = job_by_name.get(c.name)
        if theirs is None or not _same_question(c, theirs):
            continue
        entry: dict[str, Any] = {}
        unit = next(
            (u for u in (per.get(c.name) or {}).get("items") or [] if u.get("item") == item),
            None,
        )
        if unit is not None:
            entry["observed"] = {
                "status": unit.get("status"), "method": unit.get("method"),
                "score": unit.get("score"), "verdict": unit.get("verdict"),
                "confidence": unit.get("confidence"), "reason": unit.get("reason"),
                "error": unit.get("error"), "job_id": job_id,
            }
            if c.score and unit.get("status") == "ok" and unit.get("score") is not None:
                entry["breakdown"] = {
                    "score": int(unit["score"]), "verdict": unit["verdict"],
                    "reason": unit.get("reason") or "", "source": "pipeline",
                }
        located = [
            {"kind": r.get("kind", "box"), "points": r.get("points") or []}
            for r in (region_entries.get(c.name) or {}).get("regions") or []
            if int(r.get("page", -1)) == item
            and (r.get("attrs") or {}).get("accepted") is not False
        ]
        if located:
            entry["regions"] = located
            entry["regions_source"] = "pipeline"
        if entry:
            supplied[c.name] = entry
    return _Source(
        raw=raw, filename=f"{job_id}-p{item}.jpg", declared="image/jpeg", kind="image",
        pages=1, page=0, width=int(geo["width"]), height=int(geo["height"]),
        criteria=criteria,
        source={"kind": "from_job", "job_id": job_id, "item": item, "page": None},
        supplied=supplied, warnings=warnings,
    )


def _caller_answers(model: ReferenceRequest, src: _Source) -> None:
    """The caller's breakdown and regions over whatever ``from_job`` supplied,
    regions converted to page pixels and checked against the page."""
    try:
        model.check_keys([c.name for c in src.criteria], {c.name: c for c in src.criteria})
    except ValueError as exc:
        raise _bad(f"Invalid reference: {exc}")
    for name, bd in model.breakdown.items():
        src.supplied.setdefault(name, {})["breakdown"] = {
            "score": bd.score, "verdict": bd.verdict, "reason": bd.reason or "",
            "source": "caller",
        }
    sx = src.width / LLM_BBOX_GRID if model.region_units == "grid" else 1.0
    sy = src.height / LLM_BBOX_GRID if model.region_units == "grid" else 1.0
    for name, regions in model.regions.items():
        converted = []
        for i, region in enumerate(regions):
            points = [(x * sx, y * sy) for x, y in region.points()]
            over = [
                (round(x, 1), round(y, 1)) for x, y in points
                if x > src.width + _PX_SLACK or y > src.height + _PX_SLACK
            ]
            if over:
                raise _bad(
                    f"regions[{name!r}][{i}] falls outside the {src.width}×{src.height} page "
                    f"(at {over[0]}); pixel regions are in ORIGINAL page pixels — use "
                    "region_units 'grid' for 0-1000"
                )
            converted.append({"kind": region.kind, "points": [[x, y] for x, y in points]})
        entry = src.supplied.setdefault(name, {})
        entry["regions"] = converted
        entry["regions_source"] = "caller"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/references", status_code=202, openapi_extra=_OPENAPI)
async def create_reference(request: Request):
    """Save a worked example. Returns 202 with the reference id and the job
    that builds it; poll ``GET /references/{id}`` until ``status`` is
    "ready" or "failed"."""
    content_type = (request.headers.get("content-type") or "").lower()
    if content_type.startswith(("multipart/form-data", "application/x-www-form-urlencoded")):
        model, upload = await _from_form(request)
    elif content_type.startswith("application/json") or not content_type:
        model, upload = await _from_json(request)
    else:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported Content-Type {content_type!r}: send application/json "
                   "or multipart/form-data",
        )

    existing = await reference_registry.count()
    if existing >= REFERENCE_MAX_COUNT:
        raise HTTPException(
            status_code=409,
            detail=f"{existing} references exist, the CLASSIFIER_REFERENCE_MAX_COUNT cap "
                   f"({REFERENCE_MAX_COUNT}); DELETE some before saving more",
        )

    if model.document is not None:
        assert upload is not None
        src = _resolve_document(model, upload)
        if src.kind == "svg" and config.SVG_FETCH_IMAGES:
            # At submit, like /assess: the creation job never fetches.
            src.raw, src.document_warnings = await resolve_svg_images(src.raw)
    else:
        src = await _resolve_from_job(model)
    _caller_answers(model, src)

    reference_id = new_reference_id()
    job_id = await jobs_registry.register(
        ClassifierMetadata(
            type="reference", request_id=request_id_var.get("-"), reference_id=reference_id,
        ),
        initial_phase="staging",
    )
    await reference_registry.create(
        reference_id,
        source_kind=src.source["kind"],
        job_id=job_id,
        title=model.title,
        description=model.description,
        tags=model.tags or [],
        source_job_id=src.source.get("job_id"),
        source_item=src.source.get("item"),
    )
    payload = build_reference_payload(
        reference_id=reference_id,
        job_id=job_id,
        document=SubmittedDocument(
            raw=src.raw, filename=src.filename, content_type=src.declared,
            kind=src.kind, pages=src.pages, warnings=list(src.document_warnings),
        ),
        page=src.page,
        criteria=src.criteria,
        supplied=src.supplied,
        source=src.source,
        warnings=src.warnings,
    )
    try:
        depth = await queue.enqueue(job_id, payload)
    except Exception:
        await reference_registry.fail(reference_id, "could not persist the creation job's payload")
        await refresh_gauges()
        raise HTTPException(status_code=500, detail="Could not persist job payload")
    jobs_total.labels(type="reference", status="pending").inc()
    await refresh_gauges()
    logger.info(
        "references: queued %s (job_id=%s, %s, %d criteria, %d supplied) queue_depth=%d",
        reference_id, job_id, src.source["kind"], len(src.criteria), len(src.supplied), depth,
    )
    return JSONResponse(
        status_code=202,
        content={"reference_id": reference_id, "status": "pending", "job_id": job_id},
    )


@router.get("/references")
async def list_references(
    status: Optional[str] = Query(default=None, description="pending | ready | failed"),
    tag: Optional[str] = Query(default=None, description="Only references with this tag."),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """Reference summaries, newest first."""
    if status is not None and status not in STATUSES:
        raise _bad(f"status must be one of {', '.join(STATUSES)}; got {status!r}")
    refs, total = await reference_registry.list(status=status, tag=tag, limit=limit, offset=offset)
    return {
        "references": [r.summary() for r in refs],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


async def _get_or_404(reference_id: str):
    ref = await reference_registry.get(reference_id) if is_reference_id(reference_id) else None
    if ref is None:
        raise HTTPException(status_code=404, detail=f"no reference {reference_id!r}")
    return ref


def _full(ref) -> dict[str, Any]:
    files = [
        {**f, "url": file_url(ref.id, f["name"])} for f in reference_files.list(ref.id)
    ]
    return {**ref.summary(), "record": ref.record, "files": files}


@router.get("/references/{reference_id}")
async def get_reference(reference_id: str):
    """One reference: its summary, the frozen record, and its file URLs."""
    return _full(await _get_or_404(reference_id))


@router.get("/references/{reference_id}/files/{name}")
async def get_reference_file(reference_id: str, name: str):
    """One file of a reference. The name must match the reference file grammar
    (``page.jpg``, ``working.jpg``, ``c.<slug>.jpg``, ``regions.json``,
    ``record.json``) before the disk is touched — 400 otherwise."""
    if not FILE_NAME_RE.match(name or ""):
        raise _bad(
            f"{name!r} is not a reference file name (page.jpg, working.jpg, "
            "c.<slug>.jpg, regions.json, record.json)"
        )
    ref = await _get_or_404(reference_id)
    raw = reference_files.open(ref.id, name)
    if raw is None:
        detail = (
            f"reference {ref.id!r} is {ref.status}; its files are written when it is ready"
            if ref.status != "ready" else f"reference {ref.id!r} has no file {name!r}"
        )
        raise HTTPException(status_code=404, detail=detail)
    return Response(content=raw, media_type=content_type_for(name))


@router.patch("/references/{reference_id}")
async def patch_reference(reference_id: str, request: Request):
    """Edit the title, description or tags. The content is immutable."""
    await _get_or_404(reference_id)
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise _bad(f"Request body is not valid JSON: {exc}")
    if not isinstance(body, dict):
        raise _bad("Request body must be a JSON object")
    try:
        patch = ReferencePatch.model_validate(body)
    except ValidationError as exc:
        message = validation_message(exc)
        if "Extra inputs" in message:
            message += (
                " — only title, description and tags are editable; a new answer key "
                "is a new reference"
            )
        raise _bad(f"Invalid patch: {message}")
    fields = patch.model_fields_set
    ref = await reference_registry.update_meta(
        reference_id,
        title=patch.title if "title" in fields else UNSET,
        description=patch.description if "description" in fields else UNSET,
        tags=patch.tags if "tags" in fields else UNSET,
    )
    if ref is None:  # deleted between the two calls
        raise HTTPException(status_code=404, detail=f"no reference {reference_id!r}")
    return _full(ref)


@router.delete("/references/{reference_id}", status_code=204)
async def delete_reference(
    reference_id: str,
    force: bool = Query(
        default=False,
        description="Delete even while a queued or running job uses it; that job's "
                    "criteria then fail on the missing reference.",
    ),
):
    """Delete a reference and its files. 409 while a job that has not finished
    uses it (its creation job, or an /assess listing it), unless ``force``."""
    ref = await _get_or_404(reference_id)
    if not force:
        using = await reference_registry.jobs_using(
            ref.id, include_creation=ref.status == "pending"
        )
        if using:
            raise HTTPException(
                status_code=409,
                detail=f"reference {ref.id!r} is in use by job(s) {using} that have not "
                       "finished; wait for them, or DELETE with ?force=true",
            )
    await reference_registry.delete(ref.id)
    reference_files.delete(ref.id)
    await refresh_gauges()
    logger.info("references: deleted %s%s", ref.id, " (forced)" if force else "")
    return Response(status_code=204)
