"""The JSON-safe payload each endpoint stores at enqueue time.

A payload round-trips through a file on disk (``common.jobs.payloads``), so
file bytes are base64 and Pydantic models are dumped to dicts and re-validated
by the runner. That is what lets a queued job survive a container restart —
and what makes the ``job_id`` ride INSIDE the payload rather than being handed
to the runner separately: the row is registered before the payload is written,
so the id is already known here, and the runner needs it to name the artifact
directory.

    build_assess_payload()  — POST /assess
    build_compare_payload() — POST /assess/compare
    build_locate_payload()  — POST /locate
    _regions_from_payload() — the regions options a payload was queued with.

A payload written by an older container simply has no ``job_id`` key and gets
no artifacts, which is the right outcome — it asked for none.

Process flow position: called by ``api.assess`` and ``api.locate`` at submit
time; read by ``jobs.runners`` after a worker claims the row.
"""

import base64
from typing import Any, Optional

from api.schemas import CompareRequest, CriterionInput, RegionsOptions


def build_assess_payload(
    file_bytes: bytes,
    content_type: Optional[str],
    filename: str,
    criteria: list[CriterionInput],
    ocr: str = "auto",
    regions: Optional[RegionsOptions] = None,
    job_id: Optional[str] = None,
) -> dict[str, Any]:
    """Serialise a POST /assess submission for the payload store.

    The bytes are stored under ``file_b64``; ``run_assess`` still accepts the
    older ``image_b64`` key so a job queued by a previous container version
    survives the upgrade.

    ``job_id`` rides in the payload rather than being handed to the runner
    separately: the row is registered before the payload is written, so the id
    is already known here, and the runner needs it to name the artifact
    directory. A payload written by an older container simply has no
    ``job_id`` key and gets no artifacts, which is the right outcome — it
    asked for none.
    """
    return {
        "file_b64": base64.b64encode(file_bytes).decode("ascii"),
        "content_type": content_type or "application/octet-stream",
        "filename": filename,
        "criteria": [c.model_dump() for c in criteria],
        "ocr": ocr,
        "regions": regions.model_dump() if regions else None,
        "job_id": job_id,
    }


def build_compare_payload(
    request: CompareRequest, job_id: Optional[str] = None
) -> dict[str, Any]:
    """Serialise a POST /assess/compare body for the payload store."""
    return {"request": request.model_dump(), "job_id": job_id}


def build_locate_payload(
    file_bytes: bytes,
    content_type: Optional[str],
    filename: str,
    features: list[CriterionInput],
    ocr: str = "auto",
    regions: Optional[RegionsOptions] = None,
    job_id: Optional[str] = None,
) -> dict[str, Any]:
    """Serialise a POST /locate submission for the payload store.

    Same shape as ``runners.build_assess_payload`` — ``features`` under the
    ``criteria`` key, because the runner feeds them to the same pipeline —
    with ``regions`` always present and enabled, which is the point of the
    endpoint.
    """
    options = regions or RegionsOptions()
    return {
        "file_b64": base64.b64encode(file_bytes).decode("ascii"),
        "content_type": content_type or "application/octet-stream",
        "filename": filename,
        "criteria": [f.model_dump() for f in features],
        "ocr": ocr,
        "regions": options.model_copy(update={"enabled": True}).model_dump(),
        "job_id": job_id,
    }


def _regions_from_payload(payload: dict[str, Any]) -> Optional[RegionsOptions]:
    """Rebuild the regions options a payload was queued with, or None."""
    raw = payload.get("regions")
    return RegionsOptions.model_validate(raw) if raw else None
