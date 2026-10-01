"""The JSON-safe payload POST /assess stores at enqueue time.

A payload round-trips through a file on disk (``common.jobs.payloads``), so
each document's bytes are base64 and the validated request is dumped to
plain JSON and re-validated by the runner. That is what lets a queued job
survive a container restart — and what makes the ``job_id`` ride INSIDE the
payload: the row is registered before the payload is written, so the id is
already known here, and the runner needs it to name the artifact directory.

    PAYLOAD_SCHEMA         — 3: a LIST of documents, each with its kind and
                             page count as counted at submit. A payload with
                             any other schema was written by an older
                             container (schema 2: one single-page document;
                             none: the pre-redesign shapes) and is refused by
                             the runner by name rather than half-run. Drain
                             the queue before deploying — see API.md
                             § Deploying.
    SubmittedDocument      — one document as the endpoint resolved it.
    build_assess_payload() — the one payload shape.

The bytes are the ones resolved AT SUBMIT: a URL was fetched, inline text
encoded, a multipart upload read — so the worker never touches the network
for its input, and the item cap already ran on exactly these bytes.

Process flow position: called by ``api.assess`` at submit time; read by
``jobs.runners.run_assess`` after a worker claims the row.
"""

import base64
from dataclasses import dataclass
from typing import Any, Optional

from api.schemas import AssessRequest

PAYLOAD_SCHEMA = 3


@dataclass
class SubmittedDocument:
    """One document as submitted: bytes, name, declared type, kind, pages."""

    raw: bytes
    filename: str
    content_type: Optional[str]
    kind: str
    pages: int


def build_assess_payload(
    request: AssessRequest,
    documents: list[SubmittedDocument],
    *,
    job_id: Optional[str] = None,
) -> dict[str, Any]:
    """Serialise one validated submission for the payload store."""
    return {
        "schema": PAYLOAD_SCHEMA,
        "documents": [
            {
                "file_b64": base64.b64encode(d.raw).decode("ascii"),
                "filename": d.filename,
                "content_type": d.content_type or "application/octet-stream",
                "kind": d.kind,
                "pages": d.pages,
            }
            for d in documents
        ],
        "criteria": [c.model_dump() for c in request.criteria],
        "job_id": job_id,
    }
