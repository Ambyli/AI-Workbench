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
    build_assess_payload() — an /assess job's payload.
    build_reference_payload() — a reference creation job's (POST
                             /references): ONE document and the page of it,
                             the reference's criteria, and what the submit
                             already resolved — the caller's breakdown and
                             regions (in page pixels) over a ``from_job``'s
                             answers. Same schema number, ``type:
                             "reference"`` added; the assess payload is
                             unchanged.

The bytes are the ones resolved AT SUBMIT: a URL was fetched, inline text
encoded, a multipart upload read, an SVG's external images fetched and
inlined (when CLASSIFIER_SVG_FETCH_IMAGES is on) — so the worker never
touches the network for its input, and the item cap already ran on exactly
these bytes.

Each document entry may carry ``warnings``: what the submit noticed about it
(an SVG image it could not fetch, and why). Optional with a default of none,
so it needed no schema bump — an older payload without the key loads exactly
as before — and the runner prepends them to the loader's own in
``Document.warnings``.

Process flow position: called by ``api.assess`` / ``api.references`` at
submit time; read by ``jobs.runners.run_assess`` / ``run_reference`` after a
worker claims the row.
"""

import base64
from dataclasses import dataclass, field
from typing import Any, Optional

from api.schemas import AssessRequest, CriterionInput

PAYLOAD_SCHEMA = 3


@dataclass
class SubmittedDocument:
    """One document as submitted: bytes, name, declared type, kind, pages,
    and the submit's warnings about it (see the module docstring)."""

    raw: bytes
    filename: str
    content_type: Optional[str]
    kind: str
    pages: int
    warnings: list[str] = field(default_factory=list)


def build_assess_payload(
    request: AssessRequest,
    documents: list[SubmittedDocument],
    *,
    job_id: Optional[str] = None,
    references: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Serialise one validated submission for the payload store.

    ``references`` is the plan ``references.resolve`` built at submit; the
    key is present only when the request listed references, so a plain
    assess payload keeps exactly its old keys (same schema number — the
    runner reads the key with ``get``).
    """
    payload = {
        "schema": PAYLOAD_SCHEMA,
        "documents": [_document_entry(d) for d in documents],
        "criteria": [c.model_dump() for c in request.criteria],
        "job_id": job_id,
    }
    if references is not None:
        payload["references"] = references
    return payload


def _document_entry(d: SubmittedDocument) -> dict[str, Any]:
    """One document's JSON-safe entry — shared by both payload types.
    ``warnings`` only when there are some, so a plain document's entry keeps
    exactly its old keys."""
    entry: dict[str, Any] = {
        "file_b64": base64.b64encode(d.raw).decode("ascii"),
        "filename": d.filename,
        "content_type": d.content_type or "application/octet-stream",
        "kind": d.kind,
        "pages": d.pages,
    }
    if d.warnings:
        entry["warnings"] = list(d.warnings)
    return entry


def build_reference_payload(
    *,
    reference_id: str,
    job_id: str,
    document: SubmittedDocument,
    page: int,
    criteria: list[CriterionInput],
    supplied: dict[str, dict[str, Any]],
    source: dict[str, Any],
    warnings: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Serialise one validated POST /references for the payload store.

    ``supplied`` is ``{name: {"breakdown"?, "regions"?, "regions_source"?,
    "observed"?}}`` exactly as ``references.finalize.merge`` reads it; the
    regions are already in ORIGINAL page pixels (grid converted at submit).
    """
    return {
        "schema": PAYLOAD_SCHEMA,
        "type": "reference",
        "reference_id": reference_id,
        "job_id": job_id,
        "document": _document_entry(document),
        "page": int(page),
        "criteria": [c.model_dump() for c in criteria],
        "supplied": supplied,
        "source": source,
        "warnings": list(warnings or []),
    }
