"""The two model-usage routes: one job's calls, and totals across jobs.

    GET /jobs/{job_id}/usage   every vision-model request the job made, with
                               its totals and the same totals per call kind,
                               outcome and criterion
    GET /usage                 totals across jobs, by call kind and by UTC day,
                               optionally limited to a time window and a job
                               type

Both read the ``llm_calls`` rows ``llm.client._post`` writes through
``llm.usage`` (see that module for what a row holds and how it is attributed
to its job, criterion and item).

**The rows outlive the job.** The TTL sweeper removes the job row and its
artifacts and leaves the usage rows alone, so ``GET /jobs/{job_id}/usage``
keeps answering for an expired job — ``job_present: false`` says the row is
gone. It is a 404 only when there is neither a job row nor a single usage
row: an unknown id, or a job deleted with ``DELETE /jobs/{id}`` (the one
thing that removes its usage). A job that is queued, or ran without calling
the model (text and cv criteria only), answers 200 with zero calls.

The job route is three path segments deep (``/jobs/{id}/usage``), so it
cannot collide with the jobs router's ``/jobs/{job_id}``, whatever the
include order — same reasoning as ``api.artifacts``. Auth posture is the same
as every other route: the LiteLLM pass-through does the bearer check.

Process flow position: mounted by ``main``; reads only.
"""

from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query

from llm import usage as llm_usage


def _bound(value: Optional[str], name: str) -> Optional[str]:
    """An ISO date or datetime query value → the UTC string the rows store.

    A date (``2026-10-08``) means its midnight UTC; a datetime without an
    offset is taken as UTC. Normalised to :func:`llm.usage.now_iso`'s exact
    format (UTC, microseconds) so it compares correctly as a string.

    Raises:
        HTTPException(400): Not an ISO date or datetime.
    """
    if value is None or value == "":
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"{name}={value!r} is not an ISO date or datetime "
                   "(e.g. 2026-10-08 or 2026-10-08T14:00:00Z)",
        )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def build_usage_router(registry: Any) -> APIRouter:
    """The two usage routes; ``registry`` answers "does the job row exist"."""
    router = APIRouter(tags=["usage"])

    @router.get("/jobs/{job_id}/usage")
    async def job_usage(job_id: str) -> dict:
        """One job's model calls, its totals, and the totals per kind,
        outcome and criterion."""
        store = llm_usage.usage_store
        job = await registry.get(job_id)
        calls = await store.calls(job_id)
        if job is None and not calls:
            raise HTTPException(
                status_code=404,
                detail=f"no job {job_id!r} and no model usage recorded for it",
            )
        breakdown = await store.totals(job_id)
        job_type = (
            job.metadata.get("type", "assess") if job is not None
            else next((c["job_type"] for c in calls if c.get("job_type")), None)
        )
        return {
            "job_id": job_id,
            "job_present": job is not None,
            "job_type": job_type,
            **breakdown,
            "calls": calls,
        }

    @router.get("/usage")
    async def usage_summary(
        since: Optional[str] = Query(
            None, description="Inclusive lower bound on a call's started_at (ISO, UTC if no offset)"),
        until: Optional[str] = Query(
            None, description="Exclusive upper bound on a call's started_at (ISO, UTC if no offset)"),
        job_type: Optional[str] = Query(
            None, description="Only calls made by jobs of this type: assess | reference"),
    ) -> dict:
        """Totals across jobs, grouped by call kind and by UTC day."""
        lower, upper = _bound(since, "since"), _bound(until, "until")
        if lower is not None and upper is not None and lower >= upper:
            raise HTTPException(status_code=400, detail="since must be before until")
        summary = await llm_usage.usage_store.summary(
            since=lower, until=upper, job_type=job_type or None,
        )
        return {
            "filter": {"since": lower, "until": upper, "job_type": job_type or None},
            **summary,
        }

    return router
