"""A throwaway Postgres database for the classifier's tests and ``--local`` runs.

The classifier keeps every row in Postgres (``classifier-db`` in the
container) and nowhere else, so anything that runs the app or its
stores outside the container needs a database. This module makes one per run
on an admin connection the developer supplies, points the classifier's
``CLASSIFIER_DB_*`` variables at it, and drops it afterwards:

    admin_dsn()         TEST_POSTGRES_DSN, else a DSN built from an
                        explicitly set CLASSIFIER_DB_HOST & co., else None.
    ThrowawayDatabase   ``create()`` → ``CREATE DATABASE classifier_test_<pid>_<hex>``;
                        ``env()`` → the CLASSIFIER_DB_* values for it;
                        ``drop()`` → ``DROP DATABASE … WITH (FORCE)``.

A unique name per run means two suites (or a suite and a ``--local`` report)
against the same server never share rows, and ``WITH (FORCE)`` (Postgres 13+)
means a connection a test leaked cannot keep the database alive.

Used by ``conftest.py`` (one database for the pytest session) and by
``regions_report.LocalTransport`` (one per ``--local`` run). Synchronous on
purpose: both callers run before any event loop exists — the conftest at
import, the transport before it starts the app — so each operation is its
own ``asyncio.run``.

    TEST_POSTGRES_DSN=postgresql://postgres@localhost:5432/postgres
"""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Optional
from urllib.parse import quote, unquote, urlsplit

DB_ENV_KEYS = (
    "CLASSIFIER_DB_HOST",
    "CLASSIFIER_DB_PORT",
    "CLASSIFIER_DB_USER",
    "CLASSIFIER_DB_PASSWORD",
    "CLASSIFIER_DB_NAME",
)


def admin_dsn() -> Optional[str]:
    """Where to create the throwaway database.

    ``TEST_POSTGRES_DSN`` first (the same variable
    ``shared/common/tests/test_jobs_postgres.py`` uses). Failing that, an
    explicitly set ``CLASSIFIER_DB_HOST`` with its siblings — connecting to
    their database only to CREATE a new one beside it, never to use it.
    """
    dsn = os.environ.get("TEST_POSTGRES_DSN", "").strip()
    if dsn:
        return dsn
    host = os.environ.get("CLASSIFIER_DB_HOST", "").strip()
    if not host:
        return None
    user = quote(os.environ.get("CLASSIFIER_DB_USER", "classifier"), safe="")
    password = os.environ.get("CLASSIFIER_DB_PASSWORD", "")
    auth = user + (":" + quote(password, safe="") if password else "")
    port = os.environ.get("CLASSIFIER_DB_PORT", "5432") or "5432"
    name = quote(os.environ.get("CLASSIFIER_DB_NAME", "classifier"), safe="")
    return f"postgresql://{auth}@{host}:{port}/{name}"


class ThrowawayDatabase:
    """One uniquely named database on the server behind ``admin``.

    Args:
        admin:  A DSN with the right to ``CREATE DATABASE``.
        prefix: Name prefix; the full name is ``<prefix>_<pid>_<8 hex>``.
    """

    def __init__(self, admin: str, prefix: str = "classifier_test") -> None:
        self.admin = admin
        self.name = f"{prefix}_{os.getpid()}_{uuid.uuid4().hex[:8]}"
        self.created = False

    def env(self) -> dict[str, str]:
        """The ``CLASSIFIER_DB_*`` values that point the classifier here."""
        parts = urlsplit(self.admin)
        return {
            "CLASSIFIER_DB_HOST": parts.hostname or "localhost",
            "CLASSIFIER_DB_PORT": str(parts.port or 5432),
            "CLASSIFIER_DB_USER": unquote(parts.username or "postgres"),
            "CLASSIFIER_DB_PASSWORD": unquote(parts.password or ""),
            "CLASSIFIER_DB_NAME": self.name,
        }

    def create(self) -> "ThrowawayDatabase":
        asyncio.run(self._execute(f'CREATE DATABASE "{self.name}"'))
        self.created = True
        return self

    def drop(self) -> None:
        """Drop it, terminating any connection still open. Never raises — a
        leftover database is a nuisance, not a failed run."""
        if not self.created:
            return
        try:
            asyncio.run(self._execute(f'DROP DATABASE IF EXISTS "{self.name}" WITH (FORCE)'))
            self.created = False
        except Exception as exc:  # noqa: BLE001
            print(f"pg_testdb: could not drop {self.name}: {exc}")

    async def _execute(self, sql: str) -> None:
        import asyncpg  # noqa: PLC0415 — only a run that asked for a database needs it

        conn = await asyncpg.connect(self.admin)
        try:
            await conn.execute(sql)
        finally:
            await conn.close()
