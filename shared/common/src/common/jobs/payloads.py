"""
FilePayloadStore — one JSON file per job for inputs too big for ``metadata``.

A registry row holds a job's phase, small metadata, and its result. It is the
wrong place for the job's *input* when that input is an image, a document, or
a multi-megabyte request body. Keeping the input only in memory is worse: the
queue would then lose work on restart even though the row survives.

This store puts each job's input at ``<root>/<job_id><suffix>`` from enqueue
until the job reaches a terminal phase. Writes are atomic (temp file + rename)
so a concurrent worker can never read a half-written file. ``sweep()`` removes
files whose job is gone or already terminal — orphans appear when a pending
job is deleted, or a process dies between ``set_result`` and ``delete``.

Producer pattern (the registry row must not be claimable before the payload
exists)::

    job_id = await registry.register(meta, initial_phase="staging")
    await store.write(job_id, payload)
    await registry.set_phase(job_id, "pending")
    pool.notify()

Consumer pattern (inside a ``WorkerPool`` handler)::

    payload = await store.read(job.job_id)
    try:
        if payload is None:
            raise RuntimeError("payload missing")
        return await run(payload)
    finally:
        await store.delete(job.job_id)
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Iterable, Optional, Protocol

from .model import JobBase

TERMINAL_PHASES = frozenset({"completed", "failed", "cancelled"})


class _Lookup(Protocol):
    async def get(self, job_id: str) -> Optional[JobBase]: ...


class FilePayloadStore:
    """Async, file-backed payload store keyed by job id.

    Args:
        root: Directory to hold the files. Created on first write.
        suffix: File extension. Defaults to ``".json"``.

    Job ids are used as file names, so ids containing path separators or
    ``..`` are rejected with ``ValueError``.
    """

    def __init__(self, root: str | Path, *, suffix: str = ".json") -> None:
        self.root = Path(root)
        self.suffix = suffix

    def path(self, job_id: str) -> Path:
        if not job_id or "/" in job_id or "\\" in job_id or job_id in (".", ".."):
            raise ValueError(f"unsafe job id for payload store: {job_id!r}")
        return self.root / f"{job_id}{self.suffix}"

    def ids(self) -> list[str]:
        """Job ids that currently have a payload file (sync, for sweeps/tests)."""
        if not self.root.is_dir():
            return []
        return sorted(p.name[: -len(self.suffix)] for p in self.root.glob(f"*{self.suffix}"))

    async def write(self, job_id: str, payload: dict[str, Any]) -> None:
        """Persist ``payload`` atomically (temp file + ``os.replace``)."""
        path = self.path(job_id)

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.replace(tmp, path)

        await asyncio.to_thread(_write)

    async def read(self, job_id: str) -> Optional[dict[str, Any]]:
        """Load the payload, or ``None`` if there is no file for ``job_id``."""
        path = self.path(job_id)

        def _read() -> Optional[dict[str, Any]]:
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    return json.load(fh)
            except FileNotFoundError:
                return None

        return await asyncio.to_thread(_read)

    async def delete(self, job_id: str) -> bool:
        """Remove the payload file. Returns ``False`` if it was already gone."""
        path = self.path(job_id)

        def _unlink() -> bool:
            try:
                path.unlink()
                return True
            except FileNotFoundError:
                return False

        return await asyncio.to_thread(_unlink)

    async def sweep(
        self,
        registry: _Lookup,
        terminal_phases: Iterable[str] = TERMINAL_PHASES,
    ) -> int:
        """Delete payloads whose job row is missing or in a terminal phase,
        plus any leftover ``.tmp`` files. Returns the number of payloads removed."""
        terminal = frozenset(terminal_phases)
        removed = 0
        for job_id in self.ids():
            job = await registry.get(job_id)
            if job is None or job.phase in terminal:
                if await self.delete(job_id):
                    removed += 1
        if self.root.is_dir():
            for tmp in self.root.glob(f"*{self.suffix}.tmp"):
                tmp.unlink(missing_ok=True)
        return removed
