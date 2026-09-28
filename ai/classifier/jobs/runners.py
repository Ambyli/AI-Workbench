"""The job runner — the classifier's actual work, decoupled from queueing.

One runner for one job type. It takes the JSON-safe payload ``jobs.payloads``
built at enqueue time and returns the result dict that ends up in
``GET /jobs/{id}``'s ``result`` field. Nothing here knows about the registry,
the worker pool, or Prometheus — that is ``jobs.queue``.

    run_assess() — decode the bytes, re-validate the criteria, load the
                   single-page document, run ``analysis.analyze_document``.

The uploaded file may be a JPEG/PNG, a single-page PDF, a .txt, or a .docx —
the bytes are stored verbatim and the kind is detected when the worker loads
them.

Process flow position: called by ``jobs.queue.ClassifierQueue.handle_job``
after a WorkerPool worker has claimed the job.
"""

import base64
from typing import Any

from analysis import analyze_document, load_document_bytes
from api.schemas import CriterionInput
from jobs.payloads import PAYLOAD_SCHEMA


class StalePayloadError(RuntimeError):
    """The payload was written by a container with the old request shape."""


async def run_assess(payload: dict[str, Any]) -> dict:
    """Execute one assessment job from its stored payload.

    Raises:
        StalePayloadError: The payload predates PAYLOAD_SCHEMA 2 — resubmit.
    """
    if payload.get("schema") != PAYLOAD_SCHEMA:
        raise StalePayloadError(
            "this job was queued by an older classifier with a request shape "
            "that no longer exists (per-criterion options, one /assess "
            "endpoint); resubmit it"
        )
    file_bytes = base64.b64decode(payload["file_b64"])
    criteria = [CriterionInput.model_validate(c) for c in payload["criteria"]]
    doc = load_document_bytes(
        file_bytes,
        payload.get("filename"),
        payload.get("content_type"),
        keep_source=True,  # a native PDF's text hits need the file re-opened
    )
    return await analyze_document(doc, criteria, job_id=payload.get("job_id"))
