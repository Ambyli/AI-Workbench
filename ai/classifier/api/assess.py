"""POST /assess — the one analysis endpoint. Submit, don't wait.

One route, two encodings, ONE model. The handler branches on Content-Type
only to get the request into ``api.schemas.AssessRequest``; from there on a
JSON caller and a multipart caller take exactly the same path:

    application/json     {"documents": [{"type": "base64"|"url"|"text",
                          "data": "...", "filename": "..."}, ...],
                          "criteria": [...]}
                         — or the one-document shorthand {"document": {...}}.
                         Both keys at once is a 400.
    multipart/form-data  repeated `file` parts (the legacy `image` alias still
                          works) and/or repeated `text` fields, in form order,
                          plus a `criteria` field holding the same JSON array
                          and an optional `references` field (a JSON array of
                          reference ids, the word ``auto``, or the JSON
                          ``{"auto": true, ...}`` object).

Either way, an omitted ``criteria`` is genuinely omitted — the multipart path
no longer fills in the defaults itself — so the model can tell "the caller
sent none" from "the caller sent these": with explicit ``references`` the
listed references' criteria are inherited; without, the four default
quality criteria apply as before.

Then, before anything is queued — every one of these is a 400 on THIS
request rather than a job that fails in a worker a minute later:

  1. validate the request (per-type options, caps, text patterns, unique
     names, dependencies, cycles, ``score: false`` only where there is
     geometry, the static reference rules) — ``AssessRequest``;
  1b. resolve ``references`` against the store (``references.resolve``):
     unknown ids 400, not-ready 409, inherited criteria merged and
     re-validated, the per-criterion example cap, position against a
     whole-page example — the plan the worker follows rides in the payload.
     ``references: "auto"`` fixes its candidate POOL here (and the catalogue
     the worker's per-item selection call reads);
  2. resolve every document's bytes: decode base64, encode inline text, or
     fetch the URL (SSRF-checked by ``common.net``) — at submit, because
     step 3 needs them;
  3. detect each document's kind from its bytes
     (``common.documents.detect_kind``) and count its pages — a PDF's page
     count is read from the cross-reference table without rendering, every
     other kind (an SVG included) is one page. The sum is the job's ITEM
     count, capped at CLASSIFIER_MAX_ITEMS inclusively; over it is a 400
     naming each document's pages. A ``score: false`` criterion is refused
     when NO document has a page image; a ``detector`` criterion is refused
     when DETECTOR_URL is unset;
  3b. with CLASSIFIER_SVG_FETCH_IMAGES on, fetch each SVG's external
     ``<image>`` links and inline them (``analysis.resolve_svg_images``) —
     here, because the worker never touches the network. A link that cannot
     be fetched is blanked and becomes a ``documents[].warnings`` line; it
     never fails the request;
  4. register a job row in phase "staging", write the payload, flip the row
     to "pending", wake a worker — ``_enqueue`` — and return 202.

``/locate`` and ``/assess/compare`` no longer exist and answer 404 like any
unknown route; ``/locate``'s job is now ``score: false`` on a criterion here.

Process flow position: the top of the stack. Mounted by ``main``; hands work
to ``jobs.queue``.
"""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.datastructures import UploadFile

from common.documents import UnsupportedDocumentError, detect_kind, pdf_page_count

from analysis import load_input_bytes, resolve_svg_images, validate_content_type
from api.schemas import AssessRequest, ClassifierMetadata, DocumentInput, validation_message
# A module, not names: SVG_FETCH_IMAGES is read at call time, so a test (or a
# future live-reload) can flip it without re-importing this module.
import config
from config import MAX_ITEMS
from cv import get_detector
# A module, not names: is_configured() reads DETECTOR_URL at call time.
from detector import client as detector_client
from jobs.payloads import SubmittedDocument, build_assess_payload
from jobs.queue import jobs_registry, queue
from logger import logger
from metrics import jobs_total
from middleware import request_id_var
from references.resolve import ReferenceResolutionError, resolve_references
from references.store import reference_registry

router = APIRouter(tags=["assess"])

# Multipart fields this endpoint reads. `file` / `image` / `text` may repeat —
# each occurrence is one document, in form order. Anything else is refused by
# name, which is how a caller still sending the removed `ocr` / `regions`
# fields finds out they now live on each criterion.
_FORM_FIELDS = {"file", "image", "text", "criteria", "references"}
_REMOVED_FORM_FIELDS = {
    "ocr": "each llm / text criterion's options.ocr",
    "regions": "nothing — regions are always stored now, and layers render on first fetch",
    "llm_boxes": "each llm criterion's options.boxes",
}

# Documented request bodies for the OpenAPI page. The route reads the raw
# request (it has to, to accept two encodings on one path), so FastAPI
# cannot infer them.
_OPENAPI = {
    "requestBody": {
        "required": True,
        "content": {
            "application/json": {"schema": AssessRequest.model_json_schema()},
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "file": {"type": "array",
                                 "items": {"type": "string", "format": "binary"},
                                 "description": "JPEG, PNG, PDF (any page count), SVG, .txt "
                                                "or .docx — repeat the part for more documents"},
                        "text": {"type": "array", "items": {"type": "string"},
                                 "description": "Inline text documents — repeat for more"},
                        "criteria": {"type": "string",
                                     "description": "JSON array of criterion objects"},
                        "references": {"type": "string",
                                       "description": "JSON array of reference ids, "
                                                      "'auto', or a JSON {\"auto\": true, ...}"},
                    },
                }
            },
        },
    }
}


@dataclass
class _Upload:
    """One document as submitted: its bytes and what the caller called it."""

    raw: bytes
    filename: str
    declared: Optional[str]


def _bad(detail: str) -> HTTPException:
    return HTTPException(status_code=400, detail=detail)


def _validate(body: Any) -> AssessRequest:
    try:
        return AssessRequest.model_validate(body)
    except ValidationError as exc:
        raise _bad(f"Invalid request: {validation_message(exc)}")


def _json_filename(doc: DocumentInput) -> str:
    """The filename recorded for a JSON document: given, derived, or a placeholder."""
    if doc.filename:
        return doc.filename
    if doc.type == "url":
        return doc.data.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1][:120] or "download"
    if doc.type == "text":
        return "inline.txt"
    return "inline"


async def _from_json(request: Request) -> tuple[AssessRequest, list[_Upload]]:
    """JSON body → (model, one upload per document, in order)."""
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise _bad(f"Request body is not valid JSON: {exc}")
    if not isinstance(body, dict):
        raise _bad("Request body must be a JSON object with 'documents' and 'criteria'")
    model = _validate(body)
    uploads = []
    for doc in model.documents:
        raw = await load_input_bytes(doc.data, doc.type)
        uploads.append(_Upload(raw=raw, filename=_json_filename(doc), declared=None))
    return model, uploads


async def _from_form(request: Request) -> tuple[AssessRequest, list[_Upload]]:
    """Multipart form → the SAME model, each upload a base64 document."""
    form = await request.form()
    keys = list(form.keys())
    removed = [k for k in keys if k in _REMOVED_FORM_FIELDS]
    if removed:
        raise _bad(
            "Removed form field(s): "
            + "; ".join(f"'{k}' — use {_REMOVED_FORM_FIELDS[k]}" for k in removed)
            + ". See GET /criterion-types."
        )
    unknown = sorted({k for k in keys if k not in _FORM_FIELDS})
    if unknown:
        raise _bad(
            f"Unknown form field(s) {unknown}; expected 'file' parts and/or 'text' "
            "fields, and 'criteria'"
        )
    if len(form.getlist("criteria")) > 1:
        raise _bad("Send 'criteria' once — one JSON array covers every document")
    if len(form.getlist("references")) > 1:
        raise _bad("Send 'references' once — one JSON array of reference ids")

    uploads: list[_Upload] = []
    documents: list[dict] = []
    for key, value in form.multi_items():
        if key in ("file", "image"):
            if isinstance(value, str):
                raise _bad(
                    f"'{key}' must be a file part, not a plain form value "
                    "(use 'text' for inline text)"
                )
            validate_content_type(value.content_type)
            raw = await value.read()
            if not raw:
                raise _bad(f"Empty document file {value.filename or '(unnamed)'!r}")
            filename = value.filename or "upload"
            uploads.append(_Upload(raw=raw, filename=filename, declared=value.content_type))
            documents.append({
                "type": "base64",
                "data": base64.b64encode(raw).decode("ascii"),
                "filename": filename,
            })
        elif key == "text":
            text = value if isinstance(value, str) else (await value.read()).decode("utf-8")
            if not text:
                raise _bad("A 'text' field is empty; drop it or give it the document text")
            uploads.append(_Upload(raw=text.encode("utf-8"), filename="inline.txt", declared=None))
            documents.append({"type": "text", "data": text, "filename": "inline.txt"})
    if not uploads:
        raise _bad(
            "No document. Send each file as a 'file' multipart part (the legacy "
            "name 'image' is also accepted), or inline text as a 'text' field; "
            "either may repeat."
        )

    body: dict[str, Any] = {"documents": documents}
    raw_criteria = form.get("criteria")
    # Omitted stays omitted: the model's default (or, with references, the
    # inherited criteria) applies, and `criteria_given()` can tell.
    if raw_criteria is not None:
        try:
            body["criteria"] = json.loads(str(raw_criteria))
        except json.JSONDecodeError as exc:
            raise _bad(
                f"'criteria' is not valid JSON: {exc}. Expected a JSON array, e.g. "
                '[{"name": "has solar panels", "type": "llm", "options": {"hint": "presence"}}]'
            )
    raw_references = form.get("references")
    if raw_references is not None:
        text = str(raw_references).strip()
        if text == "auto":
            body["references"] = "auto"
        else:
            try:
                body["references"] = json.loads(text)
            except json.JSONDecodeError as exc:
                raise _bad(
                    f"'references' is not valid JSON: {exc}. Expected a JSON array of "
                    'reference ids, e.g. ["r0123456789ab"], or the word auto'
                )
    model = _validate(body)
    return model, uploads


async def _resolve(model: AssessRequest):
    """The store-checked reference rules; the (maybe inherited) request and
    the worker's plan."""
    try:
        resolved = await resolve_references(model, reference_registry)
    except ReferenceResolutionError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)
    return resolved


def _label(index: int, filename: str) -> str:
    return f"#{index} {filename}"


def _check_documents(model: AssessRequest, uploads: list[_Upload]) -> list[SubmittedDocument]:
    """Kind and page count per document, the item cap, and the rules that
    depend on what was uploaded. Returns one ``SubmittedDocument`` each."""
    submitted: list[SubmittedDocument] = []
    for index, up in enumerate(uploads):
        try:
            kind = detect_kind(up.raw, filename=up.filename, content_type=up.declared)
        except UnsupportedDocumentError as exc:
            logger.warning("assess: rejected document %s: %s", _label(index, up.filename), exc)
            raise _bad(f"document {_label(index, up.filename)}: {exc}")
        pages = 1
        if kind == "pdf":
            try:
                pages = pdf_page_count(up.raw)
            except UnsupportedDocumentError as exc:
                raise _bad(f"document {_label(index, up.filename)}: {exc}")
            if pages < 1:
                raise _bad(f"document {_label(index, up.filename)}: the PDF has no pages")
        submitted.append(SubmittedDocument(
            raw=up.raw, filename=up.filename, content_type=up.declared, kind=kind, pages=pages,
        ))

    total = sum(d.pages for d in submitted)
    if total > MAX_ITEMS:
        breakdown = ", ".join(
            f"{_label(i, d.filename)}: {d.pages} page{'s' if d.pages != 1 else ''}"
            for i, d in enumerate(submitted)
        )
        raise _bad(
            f"too many items: {total} pages across {len(submitted)} document(s) exceeds "
            f"CLASSIFIER_MAX_ITEMS={MAX_ITEMS} ({breakdown}). Every page of every "
            "document is one item; split the request."
        )

    if all(d.kind in ("txt", "docx") for d in submitted):
        blind = [c.name for c in model.criteria if not c.score]
        if blind:
            kinds = sorted({d.kind for d in submitted})
            raise _bad(
                f"score: false criteria {blind} need a page image to locate on, and "
                f"no document in this request has one ({', '.join(kinds)})"
            )

    if not detector_client.is_configured():
        needs = [
            c.name
            for c in model.criteria
            if c.type == "detector"
            or (c.type == "cv" and c.options.fallback == "detector" and get_detector(c.name) is None)
        ]
        if needs:
            raise _bad(
                f"criteria {needs} need the open-vocabulary detector, and this container "
                "has none configured (DETECTOR_URL is empty). Use type 'llm', or omit "
                "options.fallback so a cv criterion falls back to the llm."
            )
    return submitted


async def _inline_svg_images(submitted: list[SubmittedDocument]) -> None:
    """Step 3b: each SVG's external images fetched and inlined, in place.

    A no-op unless CLASSIFIER_SVG_FETCH_IMAGES is on. Every SVG — and every
    image inside each — is fetched concurrently, so the added submit latency
    is about one CLASSIFIER_SVG_FETCH_TIMEOUT_S however many SVGs the request
    carries (one after another, 20 SVGs could hold the submit for 20 of
    them). In flight at once: at most CLASSIFIER_MAX_ITEMS ×
    CLASSIFIER_SVG_FETCH_MAX_IMAGES, both caps the submit already enforced.
    """
    if not config.SVG_FETCH_IMAGES:
        return
    svgs = [d for d in submitted if d.kind == "svg"]
    outcomes = await asyncio.gather(*(resolve_svg_images(d.raw) for d in svgs))
    for d, (raw, warnings) in zip(svgs, outcomes):
        d.raw = raw
        d.warnings.extend(warnings)


async def _enqueue(job_id: str, payload: dict) -> int:
    """Persist ``payload``, publish the job to the workers, return queue depth.

    The row was registered in phase "staging" so no worker can claim it
    before the payload exists. If the payload write fails the job is marked
    failed and the caller gets a 500 instead of a job that can never run.
    """
    try:
        return await queue.enqueue(job_id, payload)
    except Exception:
        # queue.enqueue already logged and marked the job failed
        raise HTTPException(status_code=500, detail="Could not persist job payload")


@router.post("/assess", status_code=202, openapi_extra=_OPENAPI)
async def assess(request: Request):
    """Submit an assessment job. Returns 202 with a job_id to poll.

    Accepts JSON or multipart (see the module docstring); both are parsed
    into the same ``AssessRequest``. Poll ``GET /jobs/{job_id}`` until
    ``phase`` is "completed" or "failed".
    """
    content_type = (request.headers.get("content-type") or "").lower()
    if content_type.startswith(("multipart/form-data", "application/x-www-form-urlencoded")):
        model, uploads = await _from_form(request)
    elif content_type.startswith("application/json") or not content_type:
        model, uploads = await _from_json(request)
    else:
        raise HTTPException(
            status_code=415,
            detail=(
                f"Unsupported Content-Type {content_type!r}: send application/json "
                "or multipart/form-data"
            ),
        )

    resolved = await _resolve(model)
    model = resolved.request
    submitted = _check_documents(model, uploads)
    await _inline_svg_images(submitted)
    logger.info(
        "assess: %d document(s) %s, %d item(s), criteria=%s",
        len(submitted),
        [f"{d.filename}({d.kind}, {d.pages}p)" for d in submitted],
        sum(d.pages for d in submitted),
        [f"{c.name}({c.type})" for c in model.criteria],
    )

    job_id = await jobs_registry.register(
        ClassifierMetadata(
            type="assess",
            request_id=request_id_var.get("-"),
            # The ids this job reads, so DELETE /references/{id} can refuse
            # while it is queued or running. None (left out) without references.
            references=resolved.reference_ids or None,
        ),
        initial_phase="staging",
    )
    depth = await _enqueue(
        job_id,
        build_assess_payload(model, submitted, job_id=job_id, references=resolved.plan),
    )
    jobs_total.labels(type="assess", status="pending").inc()
    logger.info("assess: queued job_id=%s queue_depth=%d", job_id, depth)
    return JSONResponse(status_code=202, content={"job_id": job_id, "phase": "pending"})
