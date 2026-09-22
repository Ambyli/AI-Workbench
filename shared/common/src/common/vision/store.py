"""ArtifactStore — one directory of files per job, with a manifest and a TTL.

Rendered layers are files by nature: a transparent PNG for a 3000-px page is
hundreds of kilobytes, and a caller wants a URL for it, not a base64 string
inside a JSON inside a JSON. This store is the file side of that: it owns one
directory per job id, an index (``manifest.json``) describing what is in it,
a byte cap with a defined drop order, a zip view, and a sweeper that makes a
job TTL real for both the directories and the job rows.

    <root>/<job_id>/
    ├── manifest.json     the index — files, bytes, content types, page
    │                     geometry, the request's options, expires_at
    ├── regions.json      every region for the job (the canonical layer)
    ├── p0.svg            per-page rendered layers, only the formats asked for
    ├── p0.layer.png
    ├── p0.preview.jpg
    └── p0.base.jpg       the un-annotated page, kept only when previews were
                          requested, so a FILTERED preview can be re-rendered

Two rules the rest of the code depends on:

  * **Names are validated, never joined blindly.** A file name comes from a
    URL path segment, so it is checked against a strict pattern AND against
    the manifest before any open — the endpoint must not be able to read the
    volume.
  * **The byte cap drops in a fixed order:** PNG layers first, then previews
    (and their bases). ``regions.json``, ``manifest.json`` and the SVGs are
    never dropped — they are the small, canonical, re-renderable ones — and
    whatever went is recorded in the manifest so the result does not quietly
    lie about what exists.

The store is deliberately generic (nothing here knows what a criterion is),
so the interceptor's screenshots or any future service can adopt it.

Process flow position: written during a job's render step, read by the
artifact HTTP endpoints, pruned by the sweeper task.
"""

from __future__ import annotations

import inspect
import json
import re
import shutil
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import SpooledTemporaryFile
from typing import Any, Iterator, Optional, Sequence

# File names are URL path segments. Anything outside this pattern is refused
# before it reaches the filesystem — no separators, no leading dot, no "..".
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

# Job ids are directory names; same reasoning, tighter alphabet.
_SAFE_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

MANIFEST_NAME = "manifest.json"
REGIONS_NAME = "regions.json"

# Suffix → Content-Type for the formats this store holds. Anything unlisted is
# served as an octet-stream rather than guessed.
CONTENT_TYPES: dict[str, str] = {
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".txt": "text/plain; charset=utf-8",
}

# Never dropped by the byte cap: small, canonical, and the source everything
# else can be re-rendered from.
PROTECTED_SUFFIXES = (".json", ".svg")

# Drop order when a job exceeds its byte cap — PNG layers, then previews and
# the base images kept for filtered previews.
DROP_ORDER: tuple[tuple[str, ...], ...] = (
    (".layer.png", ".png"),
    (".preview.jpg", ".base.jpg", ".jpg", ".jpeg"),
)


def content_type_for(name: str) -> str:
    """Content-Type for a stored file name, from its extension."""
    suffix = Path(name).suffix.lower()
    return CONTENT_TYPES.get(suffix, "application/octet-stream")


class ArtifactStore:
    """Per-job artifact directories rooted at ``root``.

    Args:
        root:      Directory holding one subdirectory per job id. Created on
                   first write.
        max_bytes: Per-job byte cap enforced by :meth:`enforce_cap`. ``0``
                   disables the cap.

    Nothing is cached in memory: every method reads the directory, so a second
    process (or a sweeper task) writing the same volume is always visible.
    """

    def __init__(self, root: str | Path, *, max_bytes: int = 0) -> None:
        self.root = Path(root)
        self.max_bytes = max_bytes

    # ── Paths + validation ────────────────────────────────────────────────
    def dir_for(self, job_id: str) -> Path:
        """Directory for ``job_id``. Raises ``ValueError`` on an unsafe id."""
        if not _SAFE_JOB_ID.match(job_id or ""):
            raise ValueError(f"unsafe job id for artifact store: {job_id!r}")
        return self.root / job_id

    def path_of(self, job_id: str, name: str) -> Path:
        """Path of one file. Raises ``ValueError`` on an unsafe name.

        Validation is by pattern, not by resolving and comparing prefixes:
        ``..`` never matches ``_SAFE_NAME`` in the first place, so there is no
        traversal to catch later.
        """
        if not _SAFE_NAME.match(name or "") or ".." in name:
            raise ValueError(f"unsafe artifact name: {name!r}")
        return self.dir_for(job_id) / name

    def exists(self, job_id: str) -> bool:
        """True when this job has an artifact directory on disk."""
        try:
            return self.dir_for(job_id).is_dir()
        except ValueError:
            return False

    # ── Writing ───────────────────────────────────────────────────────────
    def write(self, job_id: str, name: str, data: bytes | str) -> dict[str, Any]:
        """Write one file and return its manifest entry.

        Args:
            job_id: Job the file belongs to.
            name:   File name (validated).
            data:   Bytes, or text which is encoded UTF-8.

        Returns:
            ``{"name", "bytes", "content_type"}`` — the shape the manifest and
            the job result's ``artifacts.files[]`` both use.
        """
        path = self.path_of(job_id, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = data.encode("utf-8") if isinstance(data, str) else data
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(payload)
        tmp.replace(path)
        return {
            "name": name,
            "bytes": len(payload),
            "content_type": content_type_for(name),
        }

    def write_json(self, job_id: str, name: str, payload: Any) -> dict[str, Any]:
        """Write a JSON file with stable key order (so re-runs diff cleanly)."""
        return self.write(
            job_id, name, json.dumps(payload, ensure_ascii=False, sort_keys=False)
        )

    # ── Reading ───────────────────────────────────────────────────────────
    def list(self, job_id: str) -> list[dict[str, Any]]:
        """Manifest entries for every file currently on disk, name-sorted.

        Read from the directory rather than from ``manifest.json`` so a file
        the cap dropped, or one a filtered render cached, is always reflected.
        """
        try:
            directory = self.dir_for(job_id)
        except ValueError:
            return []
        if not directory.is_dir():
            return []
        entries: list[dict[str, Any]] = []
        for path in sorted(directory.iterdir()):
            if not path.is_file() or path.name.endswith(".tmp"):
                continue
            entries.append(
                {
                    "name": path.name,
                    "bytes": path.stat().st_size,
                    "content_type": content_type_for(path.name),
                }
            )
        return entries

    def open(self, job_id: str, name: str) -> Optional[bytes]:
        """Read one file's bytes, or None when it is not there."""
        try:
            path = self.path_of(job_id, name)
        except ValueError:
            return None
        try:
            return path.read_bytes()
        except (FileNotFoundError, NotADirectoryError):
            return None

    def read_json(self, job_id: str, name: str) -> Optional[Any]:
        """Read and parse one JSON file, or None when missing/unparseable."""
        raw = self.open(job_id, name)
        if raw is None:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

    def manifest(self, job_id: str) -> Optional[dict[str, Any]]:
        """The stored manifest with its ``files``/``total_bytes`` refreshed
        from disk, or None when the job has no artifact directory."""
        data = self.read_json(job_id, MANIFEST_NAME)
        if data is None:
            return None
        files = self.list(job_id)
        data["files"] = files
        data["total_bytes"] = sum(f["bytes"] for f in files)
        return data

    def total_bytes(self, job_id: str) -> int:
        """Sum of every file's size in this job's directory."""
        return sum(entry["bytes"] for entry in self.list(job_id))

    # ── Deleting + capping ────────────────────────────────────────────────
    def delete(self, job_id: str) -> bool:
        """Remove the whole directory. False when there was nothing to remove."""
        try:
            directory = self.dir_for(job_id)
        except ValueError:
            return False
        if not directory.is_dir():
            return False
        shutil.rmtree(directory, ignore_errors=True)
        return True

    def delete_file(self, job_id: str, name: str) -> bool:
        """Remove one file. False when it was already gone."""
        try:
            path = self.path_of(job_id, name)
        except ValueError:
            return False
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False

    def enforce_cap(self, job_id: str, max_bytes: Optional[int] = None) -> list[str]:
        """Drop files until the job fits its byte cap; return what went.

        The order is fixed (``DROP_ORDER``): PNG layers first, then previews
        and their base images. Files whose suffix is in ``PROTECTED_SUFFIXES``
        are never dropped, so a capped job still has ``regions.json``,
        ``manifest.json``, and every SVG — the formats a caller can re-render
        the rest from.

        Within a tier the largest file goes first, which reaches the cap in
        the fewest deletions.
        """
        cap = self.max_bytes if max_bytes is None else max_bytes
        if not cap:
            return []
        total = self.total_bytes(job_id)
        if total <= cap:
            return []

        dropped: list[str] = []
        for tier in DROP_ORDER:
            if total <= cap:
                break
            candidates = [
                entry
                for entry in self.list(job_id)
                if not entry["name"].endswith(PROTECTED_SUFFIXES)
                and entry["name"].endswith(tier)
            ]
            for entry in sorted(candidates, key=lambda e: -e["bytes"]):
                if total <= cap:
                    break
                if self.delete_file(job_id, entry["name"]):
                    dropped.append(entry["name"])
                    total -= entry["bytes"]
        return dropped

    # ── Zip view ──────────────────────────────────────────────────────────
    def zip_chunks(self, job_id: str, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        """Yield a zip of every file in the directory, built on the fly.

        Nothing extra is stored: the archive is assembled into a spooled temp
        file (memory up to 8 MB, then disk) and streamed out, so a 40 MB job
        does not have to be held in RAM twice and no zip is left behind.
        """
        entries = self.list(job_id)
        with SpooledTemporaryFile(max_size=8 * 1024 * 1024) as spool:
            with zipfile.ZipFile(spool, "w", zipfile.ZIP_DEFLATED) as archive:
                for entry in entries:
                    path = self.path_of(job_id, entry["name"])
                    archive.write(path, arcname=f"{job_id}/{entry['name']}")
            spool.seek(0)
            while True:
                chunk = spool.read(chunk_size)
                if not chunk:
                    return
                yield chunk

    # ── Housekeeping ──────────────────────────────────────────────────────
    def job_ids(self) -> list[str]:
        """Job ids that currently have a directory."""
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir() if p.is_dir())

    def stats(self) -> dict[str, int]:
        """``{"dirs": n, "bytes": n}`` across the whole root — the two numbers
        the Prometheus gauges report."""
        dirs = self.job_ids()
        return {
            "dirs": len(dirs),
            "bytes": sum(self.total_bytes(job_id) for job_id in dirs),
        }

    async def sweep(
        self,
        registry: Any,
        ttl_hours: float,
        *,
        delete_jobs: bool = True,
        job_scan_limit: int = 500,
    ) -> dict[str, int]:
        """Prune expired artifact directories and, optionally, job rows.

        Three things go:
          1. A directory whose job row no longer exists (the job was deleted
             but the directory write raced, or an old volume was reattached).
          2. A directory whose job is older than ``ttl_hours``.
          3. When ``delete_jobs`` is set, the job rows themselves past the
             TTL — which is what turns an informational ``JOB_TTL_HOURS`` into
             a real retention policy.

        A job still in a non-terminal phase is never pruned, however old: a
        queue that backed up for a day should drain, not evaporate.

        Pass 2 asks the registry for ``expired_job_ids(cutoff, phases)`` when
        it has one — a bounded ``WHERE created_at < ?`` query returning ids
        only. Without it the fallback is ``list_all(limit=job_scan_limit)``,
        which drags every row's ``result`` blob through the store AND stops at
        the limit, so a service taking more than ``job_scan_limit`` jobs
        inside one TTL window would never reach its oldest expired rows.
        ``job_scan_limit`` therefore only applies to the fallback.

        Args:
            registry:       Any ``common.jobs`` registry (sync or async — the
                            result of each call is awaited when awaitable).
            ttl_hours:      Age past which a job and its directory expire.
            delete_jobs:    Also delete the aged job rows.
            job_scan_limit: Rows to examine in the ``list_all`` fallback only.

        Returns:
            ``{"dirs_removed", "jobs_removed", "scanned"}`` — ``scanned`` is
            how many expired candidates pass 2 considered.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max(0.0, ttl_hours))
        removed_dirs = 0
        removed_jobs = 0

        # Pass 1 — directories. Orphans go immediately; aged ones go with
        # their row (below) so the two can never disagree.
        for job_id in self.job_ids():
            job = await _maybe_await(registry.get(job_id))
            if job is None:
                if self.delete(job_id):
                    removed_dirs += 1
                continue
            if _is_expired(job, cutoff):
                if self.delete(job_id):
                    removed_dirs += 1
                if delete_jobs and await _maybe_await(registry.delete(job_id)):
                    removed_jobs += 1

        # Pass 2 — rows past the TTL that never had a directory.
        scanned = 0
        if delete_jobs:
            for job_id in await _expired_ids(registry, cutoff, job_scan_limit):
                scanned += 1
                if self.delete(job_id):
                    removed_dirs += 1
                if await _maybe_await(registry.delete(job_id)):
                    removed_jobs += 1

        return {
            "dirs_removed": removed_dirs,
            "jobs_removed": removed_jobs,
            "scanned": scanned,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TERMINAL_PHASES = frozenset({"completed", "failed", "cancelled"})


def _is_expired(job: Any, cutoff: datetime) -> bool:
    """True when a terminal job's ``created_at`` is older than ``cutoff``.

    Non-terminal jobs are never expired — see :meth:`ArtifactStore.sweep`. An
    unparseable timestamp is treated as "not expired": losing a job to a
    formatting quirk is worse than keeping a stale directory one more cycle.
    """
    if getattr(job, "phase", None) not in _TERMINAL_PHASES:
        return False
    raw = getattr(job, "created_at", None)
    if not raw:
        return False
    try:
        created = datetime.fromisoformat(str(raw))
    except ValueError:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return created < cutoff


async def _expired_ids(registry: Any, cutoff: datetime, scan_limit: int) -> list[str]:
    """Ids of expired job rows, by the cheapest route the registry offers.

    Preferred: ``expired_job_ids(cutoff, phases)`` — ids only, filtered in the
    store, unbounded. Fallback for a registry without it: ``list_all(limit=…)``
    filtered here, which is both capped and expensive (every ``result`` blob
    comes along for the ride). Nothing raises either way; a registry that
    cannot list at all simply contributes no pass-2 deletions, and pass 1 has
    already handled every directory.
    """
    getter = getattr(registry, "expired_job_ids", None)
    if callable(getter):
        return list(await _maybe_await(getter(cutoff, _TERMINAL_PHASES)))

    try:
        listing = await _maybe_await(registry.list_all(limit=scan_limit))
    except TypeError:
        # An InMemoryRegistry-shaped list_all() takes no limit.
        listing = await _maybe_await(registry.list_all())
    jobs = getattr(listing, "jobs", None) or []
    return [job.job_id for job in jobs if _is_expired(job, cutoff)]


async def _maybe_await(value: Any) -> Any:
    """Await ``value`` when it is awaitable, else return it as-is.

    Lets ``sweep`` drive a sync ``InMemoryRegistry`` and an async
    ``SqliteRegistry`` through the same code path, the same way
    ``common.jobs.router`` auto-detects the backend.
    """
    if inspect.isawaitable(value):
        return await value
    return value


def expires_at(ttl_hours: float, *, start: Optional[datetime] = None) -> str:
    """ISO-8601 timestamp ``ttl_hours`` from now — the manifest's
    ``expires_at``, and the same clock the sweeper prunes against."""
    base = start or datetime.now(timezone.utc)
    return (base + timedelta(hours=max(0.0, ttl_hours))).isoformat()


def build_manifest(
    job_id: str,
    *,
    files: Sequence[dict[str, Any]],
    page_geometry: Sequence[dict[str, Any]],
    options: Optional[dict[str, Any]] = None,
    criteria: Optional[dict[str, Any]] = None,
    ttl_hours: float = 24.0,
    notes: Optional[Sequence[str]] = None,
    dropped: Optional[Sequence[str]] = None,
) -> dict[str, Any]:
    """Assemble the manifest object.

    Kept as a free function rather than a method so a caller can build the
    manifest, hand it to :meth:`ArtifactStore.write_json`, and reuse the same
    dict as the job result's ``artifacts`` block without a second shape.
    """
    return {
        "job_id": job_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": expires_at(ttl_hours),
        "options": dict(options or {}),
        "criteria": dict(criteria or {}),
        "page_geometry": list(page_geometry),
        "files": list(files),
        "total_bytes": sum(f.get("bytes", 0) for f in files),
        "dropped": list(dropped or []),
        "notes": list(notes or []),
    }
