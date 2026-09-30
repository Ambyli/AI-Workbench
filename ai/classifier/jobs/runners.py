"""The job runner — the classifier's actual work, decoupled from queueing.

One runner for one job type. It takes the JSON-safe payload ``jobs.payloads``
built at enqueue time and returns the result dict that ends up in
``GET /jobs/{id}``'s ``result`` field. Nothing here knows about the registry,
the worker pool, or Prometheus — that is ``jobs.queue``.

    run_assess() — decode each document's bytes, re-validate the criteria,
                   load every page of every document, run
                   ``analysis.analyze_document``.
    _load()      — one document's base64 → Document, run in a worker thread.

Loading is off the event loop: a PDF renders every page at
CLASSIFIER_PDF_RENDER_DPI and an image is decoded and EXIF-rotated, which is
seconds of CPU for a large upload — and on the loop, every other job's model
calls and OCR passes would sit waiting behind it. The documents of one job
load concurrently (image decodes overlap; PDF renders take turns on
common.documents' PyMuPDF lock, per page).

Each uploaded file may be a JPEG/PNG, a PDF of any page count (the total is
capped at submit), a .txt, or a .docx — the bytes are stored verbatim and the
kind is detected when the worker loads them.

Process flow position: called by ``jobs.queue.ClassifierQueue.handle_job``
after a WorkerPool worker has claimed the job.
"""

import asyncio
import base64
from typing import Any

from analysis import analyze_document, load_document_bytes
from api.schemas import CriterionInput
from config import MAX_ITEMS
from jobs.payloads import PAYLOAD_SCHEMA


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
    return await analyze_document(list(loaded), criteria, job_id=payload.get("job_id"))


def _load(d: dict[str, Any]):
    """One stored document → ``Document``. Blocking; called via to_thread."""
    return load_document_bytes(
        base64.b64decode(d["file_b64"]),
        d.get("filename"),
        d.get("content_type"),
        keep_source=True,  # a native PDF's text hits need the file re-opened
        # The count read at submit; MAX_ITEMS is the ceiling it passed.
        max_pages=int(d.get("pages") or MAX_ITEMS),
    )
