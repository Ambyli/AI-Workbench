"""The classifier's one Postgres connection pool (the ``classifier-db`` container).

Every row the classifier keeps lives in one database: the job queue
(``jobs``, ``common.jobs.postgres.PostgresRegistry``), saved references
(``reference_examples``, ``references.store.ReferenceRegistry``) and one row
per vision-model request (``llm_calls``, ``llm.usage.UsageStore``). They share
ONE pool rather than one each, so the connection count is sized once
(``DB_POOL_MAX``) and the three stores can never starve each other's
connection budget separately.

    Database          the pool owner. ``init()`` / ``close()`` are driven by
                      ``main``'s lifespan; ``acquire()`` is what every store
                      calls (``async with database.acquire() as conn``).
    database          the process-wide instance on ``config.DB_DSN``. Built at
                      import time with no pool yet — the stores hold a
                      reference to it from their own import, and the pool
                      appears when the lifespan runs.
    create_schema()   run a store's ``CREATE … IF NOT EXISTS`` under an
                      advisory lock, so two first writers can never race on
                      the catalog (concurrent ``CREATE TABLE IF NOT EXISTS``
                      can fail with a unique violation on ``pg_type``).
    DatabaseNotConfigured
                      raised when CLASSIFIER_DB_HOST is unset — there is no
                      other store, so the classifier refuses to start.

**Why ``acquire()`` is not just ``pool.acquire()``.** An asyncpg pool belongs
to the event loop it was created on; using it from another loop fails. In the
container there is exactly one loop (uvicorn's) and every call takes the pool.
But a caller on ANOTHER loop — a test that does ``asyncio.run(store.get(…))``
while the app runs inside a ``TestClient``, an operator script, the store's
lazy table creation before the lifespan ran — gets a one-off connection that
is opened and closed around its block instead, so a store can be driven from
any loop without the caller knowing which one owns the pool. A one-off
connection is the exception, never the hot path.

Process flow position: imported by the three stores; ``main`` calls
``database.init()`` first thing in its lifespan and ``database.close()`` last.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional

import asyncpg

from config import DB_DSN, DB_POOL_MAX
from logger import logger

# Seconds ``close()`` waits for borrowed connections to come back before it
# terminates the pool. Shutdown cancels the workers first, so this only bites
# on a request that is still mid-query when the container stops.
_CLOSE_TIMEOUT_S = 10.0


class DatabaseNotConfigured(RuntimeError):
    """CLASSIFIER_DB_HOST is empty: there is no database to talk to."""


_NOT_CONFIGURED = (
    "the classifier keeps its job queue, references and model-usage rows in "
    "Postgres (the classifier-db container) and has no other store, but "
    "CLASSIFIER_DB_HOST is not set. Set CLASSIFIER_DB_HOST / _PORT / _USER / "
    "_PASSWORD / _NAME — ai/classifier/docker-compose.classifier.yml sets them "
    "for the container; the unit tests derive them from TEST_POSTGRES_DSN."
)


class Database:
    """One asyncpg pool, usable from the loop that created it, plus one-off
    connections for any other loop.

    Args:
        dsn:      ``postgresql://…`` — ``None`` when CLASSIFIER_DB_HOST is
                  unset, in which case every use raises
                  :class:`DatabaseNotConfigured`.
        min_size: Connections the pool keeps open while idle.
        max_size: The pool's ceiling (``config.DB_POOL_MAX``).
    """

    def __init__(self, dsn: Optional[str], *, min_size: int = 1, max_size: int = 10) -> None:
        self.dsn = dsn
        self.min_size = max(0, int(min_size))
        self.max_size = max(1, int(max_size))
        self._pool: Optional[asyncpg.Pool] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    @property
    def configured(self) -> bool:
        """True when there is a DSN to connect to."""
        return bool(self.dsn)

    @property
    def pooled(self) -> bool:
        """True when the pool exists AND belongs to the running loop — i.e.
        ``acquire()`` would borrow rather than open a one-off connection."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        return self._pool is not None and self._loop is loop

    def _require_dsn(self) -> str:
        if not self.dsn:
            raise DatabaseNotConfigured(_NOT_CONFIGURED)
        return self.dsn

    async def init(self) -> None:
        """Create the pool on the running loop. Idempotent on the same loop.

        Raises:
            DatabaseNotConfigured: CLASSIFIER_DB_HOST is unset.
            OSError / asyncpg errors: the database is unreachable or refuses
                the credentials — startup fails loudly rather than leaving
                workers that cannot claim anything.
        """
        dsn = self._require_dsn()
        loop = asyncio.get_running_loop()
        if self._pool is not None and self._loop is loop:
            return
        # A pool left over from a loop that has since ended (a TestClient
        # that exited without the lifespan closing it) cannot be closed from
        # here — its transports belong to that loop. Drop the reference.
        self._pool = await asyncpg.create_pool(
            dsn, min_size=self.min_size, max_size=self.max_size,
        )
        self._loop = loop
        logger.info("db: pool ready (max_size=%d)", self.max_size)

    async def close(self) -> None:
        """Close the pool. Idempotent. Waits up to ``_CLOSE_TIMEOUT_S`` for
        borrowed connections, then terminates the rest."""
        pool, loop = self._pool, self._loop
        self._pool, self._loop = None, None
        if pool is None:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not loop:
            pool.terminate()
            return
        try:
            await asyncio.wait_for(pool.close(), timeout=_CLOSE_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.warning("db: pool did not close within %.0fs — terminating", _CLOSE_TIMEOUT_S)
            pool.terminate()

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[asyncpg.Connection]:
        """``async with database.acquire() as conn`` — a pooled connection on
        the pool's own loop, a one-off connection anywhere else (see the
        module docstring).

        Raises:
            DatabaseNotConfigured: CLASSIFIER_DB_HOST is unset.
        """
        pool = self._pool
        if pool is not None and self._loop is asyncio.get_running_loop():
            async with pool.acquire() as conn:
                yield conn
            return
        conn = await asyncpg.connect(self._require_dsn())
        try:
            yield conn
        finally:
            await conn.close()


async def create_schema(conn: Any, lock_name: str, ddl: str) -> None:
    """Run ``ddl`` (one or more idempotent statements) in a transaction that
    first takes ``pg_advisory_xact_lock(hashtext(lock_name))``.

    Concurrent ``CREATE TABLE IF NOT EXISTS`` is not race-free in Postgres —
    two sessions can both pass the existence check and one fails with a
    unique violation on ``pg_type``. Startup creates the tables in order, so
    this only matters for a store's lazy first-use creation, but the lock is
    one cheap round trip and makes the order irrelevant.
    """
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", lock_name)
        await conn.execute(ddl)


database = Database(DB_DSN, min_size=1, max_size=DB_POOL_MAX)
