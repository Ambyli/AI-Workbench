"""
InMemoryRegistry — sync, threading-based, ephemeral job tracking.

Jobs live only inside a `with registry.job(...) as job:` block. On context
exit (successful or via exception) the job is unregistered — snapshots of a
completed job are not retained. This is the right shape for services where
the caller synchronously waits on the result, like ``interceptor-api`` where
``POST /capture`` blocks until the capture window closes.

Cancellation is via ``threading.Event`` — a caller of `POST /jobs/{id}/cancel`
sets the event; the code inside the job block observes it via
``job.wait_or_cancel(timeout)`` returning ``True``.

Log tagging: ``job.log(msg)`` writes ``[<job_id>] <msg>`` to stderr so
interleaved concurrent-capture logs remain readable.
"""

from __future__ import annotations

import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator, Optional
from uuid import uuid4

from pydantic import BaseModel

from .model import JobBase, JobsListResponse


@dataclass
class _MutableJob:
    """Internal record — mutated under ``InMemoryRegistry._lock``. Never
    exposed to callers; ``_snapshot`` copies into an immutable ``JobBase``.
    """

    job_id: str
    phase: str
    created_at: str
    updated_at: str
    started_monotonic: float
    metadata: dict[str, Any]
    cancel_event: threading.Event
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None


class JobHandle:
    """Caller-facing handle for one in-flight job.

    Yielded by ``InMemoryRegistry.job(...)``. Encapsulates the operations a
    running job needs: phase transitions, cancel-aware waiting, cancel status
    checks, and per-job log tagging.
    """

    def __init__(
        self,
        job_id: str,
        registry: "InMemoryRegistry",
        cancel_event: threading.Event,
    ) -> None:
        self.job_id = job_id
        self._registry = registry
        self._cancel_event = cancel_event

    def set_phase(self, phase: str) -> None:
        self._registry._set_phase(self.job_id, phase)

    def set_result(self, result: dict[str, Any]) -> None:
        self._registry._set_result(self.job_id, result)

    def set_error(self, error: str) -> None:
        self._registry._set_error(self.job_id, error)

    def update_metadata(self, **fields: Any) -> None:
        """Patch metadata fields in place — useful when a value isn't known
        at ``job(...)`` time (e.g. the temp dir a slow-path capture will use)."""
        self._registry._update_metadata(self.job_id, fields)

    def wait_or_cancel(self, timeout: float) -> bool:
        """Block for up to ``timeout`` seconds or until the job is cancelled.

        Returns ``True`` if the wait ended via cancel, ``False`` on timeout.
        A drop-in replacement for ``time.sleep(timeout)`` when the caller
        wants to be cancellable.
        """
        return self._cancel_event.wait(timeout)

    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def log(self, msg: str) -> None:
        """Emit ``[<job_id>] <msg>`` to stderr. Interleave-safe for
        concurrent captures because the job_id tag disambiguates."""
        print(f"[{self.job_id}] {msg}", file=sys.stderr, flush=True)


class InMemoryRegistry:
    """Ephemeral job registry.

    Args:
        max_concurrent: Optional soft cap. Purely informational — the
            registry itself doesn't enforce it; the consumer's port pool /
            semaphore does. Populates ``max_concurrent`` and ``available``
            fields on the ``GET /jobs`` response.
    """

    def __init__(self, *, max_concurrent: Optional[int] = None) -> None:
        self._store: dict[str, _MutableJob] = {}
        self._lock = threading.Lock()
        self._max_concurrent = max_concurrent

    def register(
        self,
        metadata: BaseModel | dict[str, Any],
        initial_phase: str = "running",
    ) -> JobHandle:
        """Register a new job and return a ``JobHandle`` for it.

        The job stays in the registry until ``unregister(job_id)`` is called
        (or the ``job()`` context manager exits, if using that). Callers that
        want auto-cleanup should prefer ``registry.job(...)`` as a ``with``
        block. Callers that need to keep a job "leaked" past the normal
        request lifetime (e.g. interceptor-api's ``keep_open`` mode) can call
        ``register`` directly and skip the ``unregister``.
        """
        if isinstance(metadata, BaseModel):
            metadata_dict = metadata.model_dump()
        else:
            metadata_dict = dict(metadata)

        job_id = uuid4().hex[:12]
        cancel_event = threading.Event()
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._store[job_id] = _MutableJob(
                job_id=job_id,
                phase=initial_phase,
                created_at=now,
                updated_at=now,
                started_monotonic=time.monotonic(),
                metadata=metadata_dict,
                cancel_event=cancel_event,
            )
        return JobHandle(job_id, self, cancel_event)

    def unregister(self, job_id: str) -> None:
        """Remove a job from the registry. No-op if the id isn't present."""
        with self._lock:
            self._store.pop(job_id, None)

    @contextmanager
    def job(
        self,
        metadata: BaseModel | dict[str, Any],
        initial_phase: str = "running",
    ) -> Iterator[JobHandle]:
        """Register a job and yield a ``JobHandle`` scoped to a ``with`` block.

        Convenience wrapper around ``register`` / ``unregister``. The job is
        auto-unregistered on context exit — success or exception. Use this
        when the job's lifetime matches a natural code block; for lifetimes
        that need to escape (e.g. deliberately-leaked jobs), call
        ``register`` and ``unregister`` directly.
        """
        handle = self.register(metadata, initial_phase)
        try:
            yield handle
        finally:
            self.unregister(handle.job_id)

    # ── Internal mutation helpers, called from JobHandle ──────────────────
    def _set_phase(self, job_id: str, phase: str) -> None:
        with self._lock:
            j = self._store.get(job_id)
            if j is not None:
                j.phase = phase
                j.updated_at = datetime.now(timezone.utc).isoformat()

    def _set_result(self, job_id: str, result: dict[str, Any]) -> None:
        with self._lock:
            j = self._store.get(job_id)
            if j is not None:
                j.result = result
                j.updated_at = datetime.now(timezone.utc).isoformat()

    def _set_error(self, job_id: str, error: str) -> None:
        with self._lock:
            j = self._store.get(job_id)
            if j is not None:
                j.error = error
                j.updated_at = datetime.now(timezone.utc).isoformat()

    def _update_metadata(self, job_id: str, fields: dict[str, Any]) -> None:
        with self._lock:
            j = self._store.get(job_id)
            if j is not None:
                j.metadata.update(fields)
                j.updated_at = datetime.now(timezone.utc).isoformat()

    # ── Read-side API ─────────────────────────────────────────────────────
    def _snapshot(self, j: _MutableJob) -> JobBase:
        now_mono = time.monotonic()
        return JobBase(
            job_id=j.job_id,
            phase=j.phase,
            created_at=j.created_at,
            updated_at=j.updated_at,
            elapsed_seconds=round(now_mono - j.started_monotonic, 3),
            metadata=j.metadata,
            result=j.result,
            error=j.error,
        )

    def get(self, job_id: str) -> Optional[JobBase]:
        with self._lock:
            j = self._store.get(job_id)
            if j is None:
                return None
            return self._snapshot(j)

    def list_all(self) -> JobsListResponse:
        with self._lock:
            snaps = [self._snapshot(j) for j in self._store.values()]
        available = (
            (self._max_concurrent - len(snaps))
            if self._max_concurrent is not None
            else None
        )
        return JobsListResponse(
            active_count=len(snaps),
            jobs=snaps,
            max_concurrent=self._max_concurrent,
            available=available,
        )

    def cancel(self, job_id: str) -> tuple[bool, str]:
        """Signal a running job to stop.

        Returns ``(ok, phase_or_reason)``:
          * ``(True, "<was_phase>")`` — cancel event was set; the job block
            will observe it on its next ``wait_or_cancel`` / ``is_cancelled``.
          * ``(False, "not_found")`` — no active job with that id.
          * ``(False, "already_cleaning_up")`` — job is past the point where
            cancel is meaningful; let it finish.
        """
        with self._lock:
            j = self._store.get(job_id)
            if j is None:
                return False, "not_found"
            if j.phase == "cleaning_up":
                return False, "already_cleaning_up"
            was_phase = j.phase
            j.cancel_event.set()
        return True, was_phase

    def delete(self, job_id: str) -> bool:
        """No-op for the in-memory backend — jobs auto-unregister when their
        context exits. Provided for router symmetry with the SQLite backend.
        Returns ``False`` always."""
        return False
