"""The service's one Postgres connection pool (the ``route-policy-db`` container).

Modelled on ai/classifier/db.py:

    Database          owns the pool. ``init()`` / ``close()`` are driven by the
                      app's lifespan; ``acquire()`` is what every repository
                      call uses (``async with database.acquire() as conn``).
    DatabaseNotConfigured
                      ROUTE_POLICY_DB_HOST is unset -- the service seeds its
                      rulesets there, so it refuses to start.

``acquire()`` borrows from the pool on the loop that created it and opens a
one-off connection on any other loop (a test driving a repository with
``asyncio.run`` while the app runs in a TestClient), so a caller never has to
know which loop owns the pool.

Every connection decodes ``json`` / ``jsonb`` to Python objects and encodes
them from Python objects (``_init_connection``), so repositories pass dicts
and lists, never JSON strings.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

import asyncpg

logger = logging.getLogger("route_policy.db")

_CLOSE_TIMEOUT_S = 10.0


class DatabaseNotConfigured(RuntimeError):
    """ROUTE_POLICY_DB_HOST is empty: there is no database to talk to."""


_NOT_CONFIGURED = (
    "route-policy keeps its rulesets, bindings, decisions and outcomes in Postgres "
    "(the route-policy-db container) and has no other store, but ROUTE_POLICY_DB_HOST "
    "is not set. ai/route-policy/docker-compose.route-policy.yml sets it for the "
    "container; the unit tests derive it from TEST_POSTGRES_DSN."
)


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


async def _init_connection(conn: asyncpg.Connection) -> None:
    for typename in ("json", "jsonb"):
        await conn.set_type_codec(typename, encoder=_dumps, decoder=json.loads, schema="pg_catalog")


class Database:
    def __init__(self, dsn: Optional[str], *, min_size: int = 1, max_size: int = 10) -> None:
        self.dsn = dsn
        self.min_size = max(0, int(min_size))
        self.max_size = max(1, int(max_size))
        self._pool: Optional[asyncpg.Pool] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    @property
    def configured(self) -> bool:
        return bool(self.dsn)

    def _require_dsn(self) -> str:
        if not self.dsn:
            raise DatabaseNotConfigured(_NOT_CONFIGURED)
        return self.dsn

    async def init(self) -> None:
        """Create the pool on the running loop. Idempotent on the same loop.
        Raises DatabaseNotConfigured, or asyncpg / OS errors when the database
        is unreachable -- startup fails loudly."""
        dsn = self._require_dsn()
        loop = asyncio.get_running_loop()
        if self._pool is not None and self._loop is loop:
            return
        self._pool = await asyncpg.create_pool(
            dsn, min_size=min(self.min_size, self.max_size), max_size=self.max_size,
            init=_init_connection,
        )
        self._loop = loop
        logger.info("db: pool ready (max_size=%d)", self.max_size)

    async def close(self) -> None:
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
            logger.warning("db: pool did not close within %.0fs -- terminating", _CLOSE_TIMEOUT_S)
            pool.terminate()

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[asyncpg.Connection]:
        pool = self._pool
        if pool is not None and self._loop is asyncio.get_running_loop():
            async with pool.acquire() as conn:
                yield conn
            return
        conn = await asyncpg.connect(self._require_dsn())
        try:
            await _init_connection(conn)
            yield conn
        finally:
            await conn.close()

    async def ping(self) -> bool:
        """True when a ``SELECT 1`` round trip works."""
        try:
            async with self.acquire() as conn:
                return await conn.fetchval("SELECT 1") == 1
        except Exception:  # noqa: BLE001 -- any failure means "not reachable"
            return False
