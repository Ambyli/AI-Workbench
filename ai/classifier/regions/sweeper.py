"""The TTL: the background sweep, the delete hook, and the disk gauges.

CLASSIFIER_JOB_TTL_HOURS used to be a policy nothing enforced. This module is
what makes it real — for artifact directories AND for the expired job rows
themselves.

    delete_artifacts_for_job() — the ``on_delete`` hook handed to
                                 ``common.jobs.router.build_router``, so
                                 DELETE /jobs/{id} takes the directory with the
                                 row rather than leaving files nothing points at.
    _refresh_gauges()          — point ``classifier_artifact_bytes`` /
                                 ``_dirs`` at what is actually on disk. Called
                                 on every sweep, write, cache and delete.
    ArtifactSweeper            — the background task: one pass at startup (a
                                 container that was down through a whole TTL
                                 window catches up immediately), then every
                                 CLASSIFIER_ARTIFACT_SWEEP_INTERVAL_S.

A sweep failure is logged and the loop continues: a transient filesystem error
must not take the sweeper down for the life of the container.

Process flow position: the sweeper instance lives in ``jobs.queue`` with the
other process-wide singletons and is started from ``main``'s lifespan;
``delete_artifacts_for_job`` is wired into the jobs router there too.
"""

import asyncio
from typing import Any, Optional

from config import ARTIFACT_DIR, ARTIFACT_SWEEP_INTERVAL_S, JOB_TTL_HOURS
from logger import logger
from metrics import artifact_bytes, artifact_dirs
from regions.store import store


def delete_artifacts_for_job(job_id: str) -> None:
    """``on_delete`` hook for ``common.jobs.router.build_router``.

    Called before the row is deleted, so ``DELETE /jobs/{id}`` takes the
    directory with it rather than leaving files nothing points at. Exceptions
    here are logged and swallowed by the router — the caller asked for the
    row to be gone, and it will be.
    """
    if store.delete(job_id):
        logger.info("delete_artifacts_for_job: removed artifacts for job %s", job_id)
        _refresh_gauges()


def _refresh_gauges() -> None:
    """Point ``classifier_artifact_bytes`` / ``_dirs`` at what is on disk."""
    stats = store.stats()
    artifact_bytes.set(stats["bytes"])
    artifact_dirs.set(stats["dirs"])


class ArtifactSweeper:
    """Background task that makes JOB_TTL_HOURS real.

    Runs once at startup (so a container that was down through a whole TTL
    window catches up immediately) and then every
    CLASSIFIER_ARTIFACT_SWEEP_INTERVAL_S. Each pass deletes artifact
    directories whose job is gone or expired AND the expired job rows
    themselves — the first time the classifier's TTL has actually retained
    anything rather than just describing a policy.

    A sweep failure is logged and the loop continues: a transient filesystem
    error must not take the sweeper down for the life of the container.
    """

    def __init__(self, registry: Any) -> None:
        self.registry = registry
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Run one sweep now, then start the periodic loop."""
        await self.sweep_once()
        self._task = asyncio.create_task(self._loop(), name="classifier-artifact-sweeper")
        logger.info(
            "ArtifactSweeper: started — every %.0fs, ttl=%sh, dir=%s",
            ARTIFACT_SWEEP_INTERVAL_S, JOB_TTL_HOURS, ARTIFACT_DIR,
        )

    async def stop(self) -> None:
        """Cancel the loop. A sweep in flight is interrupted; nothing is lost
        because a sweep is idempotent — the next one redoes it."""
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def sweep_once(self) -> dict:
        """One pass: prune, then refresh the gauges."""
        try:
            out = await store.sweep(self.registry, JOB_TTL_HOURS)
        except Exception as exc:
            logger.error("ArtifactSweeper: sweep failed: %s", exc)
            return {"dirs_removed": 0, "jobs_removed": 0, "scanned": 0}
        if out["dirs_removed"] or out["jobs_removed"]:
            logger.info(
                "ArtifactSweeper: removed %d director(ies) and %d job row(s) past "
                "the %sh TTL", out["dirs_removed"], out["jobs_removed"], JOB_TTL_HOURS,
            )
        _refresh_gauges()
        return out

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(ARTIFACT_SWEEP_INTERVAL_S)
            await self.sweep_once()
