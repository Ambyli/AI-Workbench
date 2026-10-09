"""
FastAPI router factory for the shared jobs endpoints.

Both ``InMemoryRegistry`` and ``SqliteRegistry`` expose the same conceptual
interface (``get``, ``list_all``, ``cancel``, ``delete``) — the difference
is that SqliteRegistry's methods are coroutines. FastAPI handles both
transparently: an ``async def`` route can ``await`` a coroutine registry,
and a sync route just calls the sync registry directly.

We build two internal router variants (sync and async) and pick based on
whether the registry's ``get`` method is a coroutine function.

``build_progress_router`` is the separate, opt-in ``GET /jobs/{job_id}/progress``
for a service whose jobs carry a ``common.jobs.progress.ProgressGauge``: the
snapshot from the job row plus a page of the history a
``PostgresProgressStore`` keeps.

Optional dep: ``fastapi``. Consumers who don't want the pre-built router
just skip importing this module.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException, Query

from .model import JobBase, JobsListResponse

_log = logging.getLogger(__name__)


def build_router(
    registry: Any,
    *,
    prefix: str = "/jobs",
    tags: list[str] | None = None,
    include_cancel: bool = False,
    include_delete: bool = False,
    on_delete: Optional[Callable[[str], Any]] = None,
) -> APIRouter:
    """Return a FastAPI ``APIRouter`` exposing standard jobs endpoints.

    Endpoints:
        ``GET  {prefix}``              — list active jobs (JobsListResponse).
        ``GET  {prefix}/{job_id}``     — one job's snapshot (JobBase), 404 if unknown.
        ``POST {prefix}/{job_id}/cancel`` — only if ``include_cancel=True``.
        ``DELETE {prefix}/{job_id}``   — only if ``include_delete=True``.

    ``include_cancel`` and ``include_delete`` default to ``False`` so
    consumers opt in. Interceptor-api mounts with ``include_cancel=True``
    (operators can abort a stuck capture); classifier mounts with
    ``include_delete=True`` (its jobs persist and need explicit cleanup).

    ``on_delete`` is called with the job id **before** the row is deleted, so
    a consumer can clean up whatever it hangs off a job — the classifier uses
    it to remove the job's artifact directory. It may be sync or async (the
    async router awaits an awaitable return; the sync router logs and skips
    one, since it has no loop to run it in). Failures are logged and
    swallowed: side-cleanup that throws must not turn a successful delete
    into a 500, and the row is the thing the caller asked to be gone.

    The factory auto-detects sync vs async by inspecting ``registry.get``.
    """
    router = APIRouter(prefix=prefix, tags=tags or ["jobs"])
    is_async = inspect.iscoroutinefunction(registry.get)

    if is_async:
        _register_async(router, registry, include_cancel, include_delete, on_delete)
    else:
        _register_sync(router, registry, include_cancel, include_delete, on_delete)

    return router


def _run_delete_hook_sync(on_delete: Optional[Callable[[str], Any]], job_id: str) -> None:
    """Invoke a sync ``on_delete`` hook, never raising."""
    if on_delete is None:
        return
    try:
        result = on_delete(job_id)
    except Exception as exc:
        _log.warning("on_delete hook failed for job %s: %s", job_id, exc)
        return
    if inspect.isawaitable(result):
        # A sync route has no event loop to await in. Close the coroutine so
        # Python does not warn about it never being awaited, and say plainly
        # what the consumer has to fix.
        getattr(result, "close", lambda: None)()
        _log.warning(
            "on_delete hook for job %s returned an awaitable, but this router "
            "was built for a SYNC registry — pass a sync callable",
            job_id,
        )


async def _run_delete_hook_async(
    on_delete: Optional[Callable[[str], Any]], job_id: str
) -> None:
    """Invoke a sync-or-async ``on_delete`` hook, never raising."""
    if on_delete is None:
        return
    try:
        result = on_delete(job_id)
        if inspect.isawaitable(result):
            await result
    except Exception as exc:
        _log.warning("on_delete hook failed for job %s: %s", job_id, exc)


def _register_sync(
    router: APIRouter,
    registry: Any,
    include_cancel: bool,
    include_delete: bool,
    on_delete: Optional[Callable[[str], Any]] = None,
) -> None:
    @router.get("", response_model=JobsListResponse)
    def list_jobs() -> JobsListResponse:
        return registry.list_all()

    @router.get("/{job_id}", response_model=JobBase)
    def get_job(job_id: str) -> JobBase:
        job = registry.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"no job {job_id!r}")
        return job

    if include_cancel:
        @router.post("/{job_id}/cancel")
        def cancel_job(job_id: str) -> dict:
            ok, phase_or_reason = registry.cancel(job_id)
            if not ok:
                if phase_or_reason == "not_found":
                    raise HTTPException(
                        status_code=404, detail=f"no active job {job_id!r}"
                    )
                raise HTTPException(
                    status_code=409,
                    detail=f"cannot cancel {job_id!r}: {phase_or_reason}",
                )
            return {
                "job_id": job_id,
                "cancelled": True,
                "was_phase": phase_or_reason,
            }

    if include_delete:
        @router.delete("/{job_id}", status_code=204)
        def delete_job(job_id: str) -> None:
            _run_delete_hook_sync(on_delete, job_id)
            if not registry.delete(job_id):
                raise HTTPException(
                    status_code=404, detail=f"no job {job_id!r}"
                )


def _register_async(
    router: APIRouter,
    registry: Any,
    include_cancel: bool,
    include_delete: bool,
    on_delete: Optional[Callable[[str], Any]] = None,
) -> None:
    @router.get("", response_model=JobsListResponse)
    async def list_jobs() -> JobsListResponse:
        return await registry.list_all()

    @router.get("/{job_id}", response_model=JobBase)
    async def get_job(job_id: str) -> JobBase:
        job = await registry.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"no job {job_id!r}")
        return job

    if include_cancel:
        @router.post("/{job_id}/cancel")
        async def cancel_job(job_id: str) -> dict:
            ok, phase_or_reason = await registry.cancel(job_id)
            if not ok:
                if phase_or_reason == "not_found":
                    raise HTTPException(
                        status_code=404, detail=f"no active job {job_id!r}"
                    )
                raise HTTPException(
                    status_code=409,
                    detail=f"cannot cancel {job_id!r}: {phase_or_reason}",
                )
            return {
                "job_id": job_id,
                "cancelled": True,
                "was_phase": phase_or_reason,
            }

    if include_delete:
        @router.delete("/{job_id}", status_code=204)
        async def delete_job(job_id: str) -> None:
            await _run_delete_hook_async(on_delete, job_id)
            if not await registry.delete(job_id):
                raise HTTPException(
                    status_code=404, detail=f"no job {job_id!r}"
                )


# ── Progress ──────────────────────────────────────────────────────────────
# The largest page of history one request may ask for. A job's history is a
# few rows per unit of work, so a long job can hold thousands; past this the
# caller pages with ``after``.
PROGRESS_MAX_LIMIT = 1000


def build_progress_router(
    registry: Any,
    store: Any,
    *,
    prefix: str = "/jobs",
    tags: list[str] | None = None,
) -> APIRouter:
    """Return an ``APIRouter`` with ``GET {prefix}/{job_id}/progress``.

    ``store`` is a ``common.jobs.progress_postgres.PostgresProgressStore``
    (anything with an async ``history(job_id, *, attempt, after, limit)``);
    ``registry`` answers whether the job exists and supplies its phase and
    ``metadata.progress`` snapshot — sync or async, like ``build_router``.

    Query parameters:
        ``after``   — only events with ``seq`` greater than this (paging; 0 = from the start).
        ``limit``   — events per page, default 200, clamped to 1..1000.
        ``attempt`` — which run's history (default: the latest; 0 is the queued event).

    Returns ``{job_id, phase, progress, attempt, events, next_after}`` —
    ``progress`` is the snapshot (``null`` for a job that never had a gauge),
    ``events`` the history page oldest first, ``next_after`` the ``after`` to
    pass for the next page. **404** when the job is unknown — its history
    went with it (``ON DELETE CASCADE``).

    Three path segments deep, so it can never collide with
    ``build_router``'s ``{prefix}/{job_id}``, whatever the include order.
    """
    router = APIRouter(prefix=prefix, tags=tags or ["jobs"])

    @router.get("/{job_id}/progress")
    async def job_progress(
        job_id: str,
        after: int = Query(0, description="Only events with seq greater than this"),
        limit: int = Query(200, description=f"Events per page, clamped to 1..{PROGRESS_MAX_LIMIT}"),
        attempt: Optional[int] = Query(None, description="Which run (default: the latest)"),
    ) -> dict:
        job = registry.get(job_id)
        if inspect.isawaitable(job):
            job = await job
        if job is None:
            raise HTTPException(status_code=404, detail=f"no job {job_id!r}")
        history = await store.history(
            job_id,
            attempt=attempt,
            after=max(0, after),
            limit=max(1, min(limit, PROGRESS_MAX_LIMIT)),
        )
        return {
            "job_id": job_id,
            "phase": job.phase,
            "progress": (job.metadata or {}).get("progress"),
            "attempt": history["attempt"],
            "events": history["events"],
            "next_after": history["next_after"],
        }

    return router
