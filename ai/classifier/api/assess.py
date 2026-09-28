"""POST /assess — the one analysis endpoint. Submit, don't wait.

One route, two encodings, ONE model. The handler branches on Content-Type
only to get the request into ``api.schemas.AssessRequest``; from there on a
JSON caller and a multipart caller take exactly the same path:

    application/json     {"document": {"type": "base64"|"url"|"text",
                          "data": "...", "filename": "..."},
                          "criteria": [...]}
    multipart/form-data  a `file` part (the legacy `image` alias still
                          works) OR a `text` field, plus a `criteria` field
                          holding the same JSON array.

Then, before anything is queued — every one of these is a 400 on THIS
request rather than a job that fails in a worker a minute later:

  1. validate the request (per-type options, caps, text patterns, unique
     names, dependencies, cycles, ``score: false`` only where there is
     geometry) — ``AssessRequest``;
  2. resolve the bytes: decode base64, encode inline text, or fetch the URL
     (SSRF-checked by ``common.net``) — at submit, because step 3 needs them;
  3. detect the kind from the bytes (``common.documents.detect_kind``); a
     PDF with more than one page is refused ("only single-page PDFs are
     supported", with the page count, read without rendering); a
     ``score: false`` criterion on a document with no page image is refused;
     a ``detector`` criterion is refused when DETECTOR_URL is unset;
  4. register a job row in phase "staging", write the payload, flip the row
     to "pending", wake a worker — ``_enqueue`` — and return 202.

``/locate`` and ``/assess/compare`` no longer exist and answer 404 like any
unknown route; ``/locate``'s job is now ``score: false`` on a criterion here.

Process flow position: the top of the stack. Mounted by ``main``; hands work
to ``jobs.queue``.
"""

from __future__ import annotations

import base64
import json
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.datastructures import UploadFile

from common.documents import UnsupportedDocumentError, detect_kind, pdf_page_count

from analysis import load_input_bytes, validate_content_type
from api.schemas import AssessRequest, ClassifierMetadata, validation_message
from config import DEFAULT_CRITERIA
from cv import get_detector
# A module, not names: is_configured() reads DETECTOR_URL at call time.
from detector import client as detector_client
from jobs.payloads import build_assess_payload
from jobs.queue import jobs_registry, queue
from logger import logger
from metrics import jobs_total
from middleware import request_id_var

router = APIRouter(tags=["assess"])

# Multipart fields this endpoint reads. Anything else is refused by name,
# which is how a caller still sending the removed `ocr` / `regions` fields
# finds out they now live on each criterion.
_FORM_FIELDS = {"file", "image", "text", "criteria"}
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
                        "file": {"type": "string", "format": "binary",
                                 "description": "JPEG, PNG, single-page PDF, .txt or .docx"},
                        "text": {"type": "string",
                                 "description": "Inline text, instead of a file"},
                        "criteria": {"type": "string",
                                     "description": "JSON array of criterion objects"},
                    },
                }
            },
        },
    }
}


def _bad(detail: str) -> HTTPException:
    return HTTPException(status_code=400, detail=detail)


def _validate(body: Any) -> AssessRequest:
    try:
        return AssessRequest.model_validate(body)
    except ValidationError as exc:
        raise _bad(f"Invalid request: {validation_message(exc)}")


async def _from_json(request: Request) -> tuple[AssessRequest, bytes, str, Optional[str]]:
    """JSON body → (model, bytes, filename, declared content type)."""
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise _bad(f"Request body is not valid JSON: {exc}")
    if not isinstance(body, dict):
        raise _bad("Request body must be a JSON object with 'document' and 'criteria'")
    model = _validate(body)
    doc = model.document
    raw = await load_input_bytes(doc.data, doc.type)
    if doc.filename:
        filename = doc.filename
    elif doc.type == "url":
        filename = doc.data.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1][:120] or "download"
    elif doc.type == "text":
        filename = "inline.txt"
    else:
        filename = "inline"
    return model, raw, filename, None


async def _from_form(request: Request) -> tuple[AssessRequest, bytes, str, Optional[str]]:
    """Multipart form → the SAME model, with the upload as a base64 document."""
    form = await request.form()
    removed = [k for k in form.keys() if k in _REMOVED_FORM_FIELDS]
    if removed:
        raise _bad(
            "Removed form field(s): "
            + "; ".join(f"'{k}' — use {_REMOVED_FORM_FIELDS[k]}" for k in removed)
            + ". See GET /criterion-types."
        )
    unknown = sorted(k for k in form.keys() if k not in _FORM_FIELDS)
    if unknown:
        raise _bad(f"Unknown form field(s) {unknown}; expected 'file' or 'text', and 'criteria'")

    upload = form.get("file") or form.get("image")
    text = form.get("text")
    if isinstance(upload, str):
        raise _bad("'file' must be a file part, not a plain form value (use 'text' for inline text)")
    if upload is not None and text:
        raise _bad("Send either a 'file' part or a 'text' field, not both")
    if upload is None and not text:
        raise _bad(
            "No document. Send the file as the 'file' multipart part (the legacy "
            "name 'image' is also accepted), or inline text as the 'text' field."
        )

    raw_criteria = form.get("criteria")
    if raw_criteria is None:
        criteria: Any = DEFAULT_CRITERIA
    else:
        try:
            criteria = json.loads(str(raw_criteria))
        except json.JSONDecodeError as exc:
            raise _bad(
                f"'criteria' is not valid JSON: {exc}. Expected a JSON array, e.g. "
                '[{"name": "has solar panels", "type": "llm", "options": {"hint": "presence"}}]'
            )

    if isinstance(upload, UploadFile):
        validate_content_type(upload.content_type)
        raw = await upload.read()
        if not raw:
            raise _bad("Empty document file")
        filename = upload.filename or "upload"
        document = {
            "type": "base64",
            "data": base64.b64encode(raw).decode("ascii"),
            "filename": filename,
        }
        declared = upload.content_type
    else:
        raw = str(text).encode("utf-8")
        filename = "inline.txt"
        document = {"type": "text", "data": str(text), "filename": filename}
        declared = None
    model = _validate({"document": document, "criteria": criteria})
    return model, raw, filename, declared


def _check_document(model: AssessRequest, raw: bytes, filename: str, declared: Optional[str]) -> str:
    """Kind, page count, and the rules that depend on them. Returns the kind."""
    try:
        kind = detect_kind(raw, filename=filename, content_type=declared)
    except UnsupportedDocumentError as exc:
        logger.warning("assess: rejected %s: %s", filename, exc)
        raise _bad(str(exc))

    if kind == "pdf":
        try:
            pages = pdf_page_count(raw)
        except UnsupportedDocumentError as exc:
            raise _bad(str(exc))
        if pages != 1:
            raise _bad(
                f"only single-page PDFs are supported (this one has {pages} pages). "
                "Split it and submit each page as its own job."
            )

    if kind in ("txt", "docx"):
        blind = [c.name for c in model.criteria if not c.score]
        if blind:
            raise _bad(
                f"score: false criteria {blind} need a page image to locate on, and a "
                f"{kind} document has none"
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
    return kind


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
        model, raw, filename, declared = await _from_form(request)
    elif content_type.startswith("application/json") or not content_type:
        model, raw, filename, declared = await _from_json(request)
    else:
        raise HTTPException(
            status_code=415,
            detail=(
                f"Unsupported Content-Type {content_type!r}: send application/json "
                "or multipart/form-data"
            ),
        )

    kind = _check_document(model, raw, filename, declared)
    logger.info(
        "assess: kind=%s filename=%s bytes=%d criteria=%s",
        kind, filename, len(raw), [f"{c.name}({c.type})" for c in model.criteria],
    )

    job_id = await jobs_registry.register(
        ClassifierMetadata(type="assess", request_id=request_id_var.get("-")),
        initial_phase="staging",
    )
    depth = await _enqueue(
        job_id,
        build_assess_payload(
            model, raw, filename=filename, content_type=declared, kind=kind, job_id=job_id
        ),
    )
    jobs_total.labels(type="assess", status="pending").inc()
    logger.info("assess: queued job_id=%s queue_depth=%d", job_id, depth)
    return JSONResponse(status_code=202, content={"job_id": job_id, "phase": "pending"})
