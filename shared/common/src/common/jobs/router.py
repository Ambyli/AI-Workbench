"""
FastAPI router factory for the shared jobs endpoints.

Both ``InMemoryRegistry`` and ``SqliteRegistry`` expose the same conceptual
interface (``get``, ``list_all``, ``cancel``, ``delete``) — the difference
is that SqliteRegistry's methods are coroutines. FastAPI handles both
transparently: an ``async def`` route can ``await`` a coroutine registry,
and a sync route just calls the sync registry directly.

We build two internal router variants (sync and async) and pick based on
whether the registry's ``get`` method is a coroutine function.

Long-poll (opt-in, async registries only): built with ``terminal_phases``,
``GET {prefix}/{job_id}`` takes ``?wait=<seconds>`` and holds the request
until the job reaches one of those phases or the wait runs out, re-reading the
row every ``wait_check_interval_s`` — so a caller waiting on a job makes one
or two requests instead of one every second or two. The re-reads are cheap
primary-key reads inside the service; through a metered gateway (LiteLLM's
``/v1/classifier`` pass-through) only the one outer request counts. Built
without ``terminal_phases`` the route is exactly what it was, and FastAPI
ignores a ``?wait=`` it does not declare — a client that sends it to such a
router gets an immediate answer.

``build_progress_router`` is the separate, opt-in ``GET /jobs/{job_id}/progress``
for a service whose jobs carry a ``common.jobs.progress.ProgressGauge``: the
snapshot from the job row plus a page of the history a
``PostgresProgressStore`` keeps.

Optional dep: ``fastapi``. Consumers who don't want the pre-built router
just skip importing this module.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
import time
from typing import Any, Callable, Iterable, Optional

from fastapi import APIRouter, HTTPException, Query, Request

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
    on_cancel: Optional[Callable[[str, str], Any]] = None,
    terminal_phases: Optional[Iterable[str]] = None,
    max_wait_s: float = 60.0,
    wait_check_interval_s: float = 0.5,
) -> APIRouter:
    """Return a FastAPI ``APIRouter`` exposing standard jobs endpoints.

    Endpoints:
        ``GET  {prefix}``              — list active jobs (JobsListResponse).
        ``GET  {prefix}/{job_id}``     — one job's snapshot (JobBase), 404 if unknown;
                                         ``?wait=`` with ``terminal_phases`` (below).
        ``POST {prefix}/{job_id}/cancel`` — only if ``include_cancel=True``:
                                         200 ``{job_id, cancelled: true, was_phase}``,
                                         404 for an unknown job, 409
                                         ``already_<phase>`` for a finished one.
        ``DELETE {prefix}/{job_id}``   — only if ``include_delete=True``.

    ``include_cancel`` and ``include_delete`` default to ``False`` so
    consumers opt in. Interceptor-api mounts with ``include_cancel=True``
    (operators can abort a stuck capture; its in-memory registry signals the
    capture thread). The classifier mounts both: ``include_delete=True`` (its
    jobs persist and need explicit cleanup) and ``include_cancel=True`` — a
    persistent registry's cancel only flips the phase, and what stops the
    running job is its ``WorkerPool``, built with ``cancel_check_interval``.
    Mount the cancel route over a persistent registry only with such a pool:
    with a plain one the job runs on and its result overwrites the cancel.

    ``on_delete`` is called with the job id **before** the row is deleted, so
    a consumer can clean up whatever it hangs off a job — the classifier uses
    it to remove the job's artifact directory. It may be sync or async (the
    async router awaits an awaitable return; the sync router logs and skips
    one, since it has no loop to run it in). Failures are logged and
    swallowed: side-cleanup that throws must not turn a successful delete
    into a 500, and the row is the thing the caller asked to be gone.

    ``on_cancel`` is called with ``(job_id, was_phase)`` **after** a
    successful cancel (never for a 404 or a 409), so a consumer can clean up
    a job no worker owns — the classifier finishes a ``staging`` /
    ``pending`` job there and leaves a ``processing`` one to its pool. Sync
    or async and swallowed on failure exactly like ``on_delete``: the cancel
    already happened, and the answer says so.

    ``terminal_phases`` turns on the long-poll: ``GET {prefix}/{job_id}``
    then takes ``wait`` (query, seconds, default 0, ``>= 0``; above
    ``max_wait_s`` it is clamped, not refused). With ``wait > 0`` and the job
    not yet in one of ``terminal_phases``, the route re-reads the job every
    ``wait_check_interval_s`` — never sleeping past the deadline — until its
    phase is terminal or the wait is used up, then answers with the latest
    snapshot: the same ``JobBase`` and the same 200 either way, so the caller
    reads ``phase`` exactly as before. A job deleted mid-wait is a 404; a
    client that disconnects ends the wait early. No connection is held while
    it sleeps (each read borrows one and gives it back). Only for an async
    registry — a sync route would hold a threadpool thread for the whole
    wait, so ``terminal_phases`` with a sync registry is a ``ValueError``.
    ``max_wait_s`` is the service's cap: below every proxy timeout in front of
    it. Without ``terminal_phases`` neither knob is read and the route is
    unchanged.

    The factory auto-detects sync vs async by inspecting ``registry.get``.
    """
    router = APIRouter(prefix=prefix, tags=tags or ["jobs"])
    is_async = inspect.iscoroutinefunction(registry.get)
    long_poll = _long_poll_settings(
        terminal_phases, max_wait_s, wait_check_interval_s, is_async=is_async
    )

    if is_async:
        _register_async(
            router, registry, include_cancel, include_delete, on_delete, long_poll,
            on_cancel,
        )
    else:
        _register_sync(
            router, registry, include_cancel, include_delete, on_delete, on_cancel
        )

    return router


def _long_poll_settings(
    terminal_phases: Optional[Iterable[str]],
    max_wait_s: float,
    wait_check_interval_s: float,
    *,
    is_async: bool,
) -> Optional[tuple[frozenset[str], float, float]]:
    """``(terminal phases, max wait, check interval)``, or None for no
    long-poll. Refuses what would make a wait never end or block a thread."""
    if terminal_phases is None:
        return None
    if isinstance(terminal_phases, str):
        # A bare string would iterate into one-letter "phases".
        raise TypeError("terminal_phases takes a collection of phase names, not one string")
    phases = frozenset(terminal_phases)
    if not phases:
        raise ValueError("terminal_phases names no phase: every wait would run to its cap")
    if not is_async:
        raise ValueError(
            "terminal_phases needs an async registry: a sync route would hold a "
            "threadpool thread for the whole wait"
        )
    if not (math.isfinite(max_wait_s) and max_wait_s >= 0):
        raise ValueError(f"max_wait_s must be a finite number >= 0, not {max_wait_s!r}")
    if not (math.isfinite(wait_check_interval_s) and wait_check_interval_s > 0):
        raise ValueError(
            f"wait_check_interval_s must be a finite number > 0, not {wait_check_interval_s!r}"
        )
    return phases, float(max_wait_s), float(wait_check_interval_s)


def _run_hook_sync(
    hook: Optional[Callable[..., Any]], name: str, job_id: str, *args: Any
) -> None:
    """Invoke a sync ``on_delete`` / ``on_cancel`` hook with ``(job_id,
    *args)``, never raising."""
    if hook is None:
        return
    try:
        result = hook(job_id, *args)
    except Exception as exc:
        _log.warning("%s hook failed for job %s: %s", name, job_id, exc)
        return
    if inspect.isawaitable(result):
        # A sync route has no event loop to await in. Close the coroutine so
        # Python does not warn about it never being awaited, and say plainly
        # what the consumer has to fix.
        getattr(result, "close", lambda: None)()
        _log.warning(
            "%s hook for job %s returned an awaitable, but this router "
            "was built for a SYNC registry — pass a sync callable",
            name,
            job_id,
        )


async def _run_hook_async(
    hook: Optional[Callable[..., Any]], name: str, job_id: str, *args: Any
) -> None:
    """Invoke a sync-or-async ``on_delete`` / ``on_cancel`` hook with
    ``(job_id, *args)``, never raising."""
    if hook is None:
        return
    try:
        result = hook(job_id, *args)
        if inspect.isawaitable(result):
            await result
    except Exception as exc:
        _log.warning("%s hook failed for job %s: %s", name, job_id, exc)


def _register_sync(
    router: APIRouter,
    registry: Any,
    include_cancel: bool,
    include_delete: bool,
    on_delete: Optional[Callable[[str], Any]] = None,
    on_cancel: Optional[Callable[[str, str], Any]] = None,
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
            _run_hook_sync(on_cancel, "on_cancel", job_id, phase_or_reason)
            return {
                "job_id": job_id,
                "cancelled": True,
                "was_phase": phase_or_reason,
            }

    if include_delete:
        @router.delete("/{job_id}", status_code=204)
        def delete_job(job_id: str) -> None:
            _run_hook_sync(on_delete, "on_delete", job_id)
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
    long_poll: Optional[tuple[frozenset[str], float, float]] = None,
    on_cancel: Optional[Callable[[str, str], Any]] = None,
) -> None:
    @router.get("", response_model=JobsListResponse)
    async def list_jobs() -> JobsListResponse:
        return await registry.list_all()

    if long_poll is None:
        @router.get("/{job_id}", response_model=JobBase)
        async def get_job(job_id: str) -> JobBase:
            job = await registry.get(job_id)
            if job is None:
                raise HTTPException(status_code=404, detail=f"no job {job_id!r}")
            return job
    else:
        _add_long_poll_get(router, registry, *long_poll)

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
            await _run_hook_async(on_cancel, "on_cancel", job_id, phase_or_reason)
            return {
                "job_id": job_id,
                "cancelled": True,
                "was_phase": phase_or_reason,
            }

    if include_delete:
        @router.delete("/{job_id}", status_code=204)
        async def delete_job(job_id: str) -> None:
            await _run_hook_async(on_delete, "on_delete", job_id)
            if not await registry.delete(job_id):
                raise HTTPException(
                    status_code=404, detail=f"no job {job_id!r}"
                )


def _add_long_poll_get(
    router: APIRouter,
    registry: Any,
    terminal: frozenset[str],
    max_wait_s: float,
    check_interval_s: float,
) -> None:
    """``GET {prefix}/{job_id}`` with ``?wait=`` — see ``build_router``."""
    phases = ", ".join(sorted(terminal))

    @router.get("/{job_id}", response_model=JobBase)
    async def get_job(
        job_id: str,
        request: Request,
        wait: float = Query(
            0.0,
            ge=0,
            # NaN would compare false against every deadline and wait forever.
            allow_inf_nan=False,
            description=(
                f"Seconds to hold the request until the job's phase is terminal "
                f"({phases}); clamped to {max_wait_s:g}. 0 answers at once."
            ),
        ),
    ) -> JobBase:
        job = await registry.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"no job {job_id!r}")
        deadline = time.monotonic() + min(wait, max_wait_s)
        while job.phase not in terminal:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(check_interval_s, remaining))
            if await request.is_disconnected():
                # Nobody is left to read the answer; stop re-reading the row.
                break
            job = await registry.get(job_id)
            if job is None:
                raise HTTPException(status_code=404, detail=f"no job {job_id!r}")
        return job


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
