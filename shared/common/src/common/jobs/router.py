"""
FastAPI router factory for the shared jobs endpoints.

Both ``InMemoryRegistry`` and ``SqliteRegistry`` expose the same conceptual
interface (``get``, ``list_all``, ``cancel``, ``delete``) — the difference
is that SqliteRegistry's methods are coroutines. FastAPI handles both
transparently: an ``async def`` route can ``await`` a coroutine registry,
and a sync route just calls the sync registry directly.

We build two internal router variants (sync and async) and pick based on
whether the registry's ``get`` method is a coroutine function.

Optional dep: ``fastapi``. Consumers who don't want the pre-built router
just skip importing this module.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException

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
