"""Module-level singletons for the runner. Populated in ``app.lifespan()``.

Kept in its own module so ``operations.py`` can read them without a
circular import on ``app.py``. Only ``lifespan()`` writes; everyone else
reads.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from common.jobs.postgres import PostgresRegistry

from reaper import Reaper
from spawner import Spawner


log = logging.getLogger("sandbox-runner.state")

# Populated in app.lifespan().
registry: Optional[PostgresRegistry] = None
spawner: Optional[Spawner] = None
reaper: Optional[Reaper] = None
# Simple semaphore for concurrency capping; the runner also relies on
# Docker to reject a container-create if resources are exhausted, but this
# semaphore gives us a clean 429 without paying a docker round-trip first.
slot_sem: Optional[asyncio.Semaphore] = None
