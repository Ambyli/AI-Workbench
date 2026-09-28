"""The JSON-safe payload POST /assess stores at enqueue time.

A payload round-trips through a file on disk (``common.jobs.payloads``), so
the document bytes are base64 and the validated request is dumped to plain
JSON and re-validated by the runner. That is what lets a queued job survive a
container restart — and what makes the ``job_id`` ride INSIDE the payload:
the row is registered before the payload is written, so the id is already
known here, and the runner needs it to name the artifact directory.

    PAYLOAD_SCHEMA         — 2. A payload without it was written by a
                             container from before the one-endpoint redesign
                             (an assess, compare or locate job with the old
                             shape) and is refused by the runner by name
                             rather than half-run. Drain the queue before
                             deploying — see API.md § Deploying.
    build_assess_payload() — the one payload shape.

The bytes are the ones resolved AT SUBMIT: a URL was fetched, inline text
encoded, a multipart upload read — so the worker never touches the network
for its input, and the single-page check already ran on exactly these bytes.

Process flow position: called by ``api.assess`` at submit time; read by
``jobs.runners.run_assess`` after a worker claims the row.
"""

import base64
from typing import Any, Optional

from api.schemas import AssessRequest

PAYLOAD_SCHEMA = 2


def build_assess_payload(
    request: AssessRequest,
    file_bytes: bytes,
    *,
    filename: str,
    content_type: Optional[str],
    kind: str,
    job_id: Optional[str] = None,
) -> dict[str, Any]:
    """Serialise one validated submission for the payload store."""
    return {
        "schema": PAYLOAD_SCHEMA,
        "file_b64": base64.b64encode(file_bytes).decode("ascii"),
        "filename": filename,
        "content_type": content_type or "application/octet-stream",
        "kind": kind,
        "criteria": [c.model_dump() for c in request.criteria],
        "job_id": job_id,
    }
