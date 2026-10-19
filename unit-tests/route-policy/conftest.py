"""Make ``route_policy`` importable, on a throwaway Postgres database when one is offered.

``ai/route-policy`` goes first on sys.path (the container's PYTHONPATH), so
``import route_policy`` works from the repo root.

**The database.** route-policy keeps everything in route-policy-db. With
``TEST_POSTGRES_DSN`` set (an admin connection, the same variable the
classifier and ``shared/common`` tests use), this file creates one uniquely
named database for the session -- ``route_policy_test_<pid>_<hex>`` -- points
``ROUTE_POLICY_DB_*`` at it BEFORE ``route_policy.config`` is imported, and
drops it at the end. Without it, ``ROUTE_POLICY_DB_HOST`` is forced empty (no
test can reach a database a developer's shell points at) and every test
marked ``postgres`` is skipped. The engine, evaluator, combiner, ruleset
and default-ruleset tests never need it.

    UV_LINK_MODE=copy uv run --no-sync python -m pytest unit-tests/route-policy -q -p no:cacheprovider
    TEST_POSTGRES_DSN=postgresql://postgres@localhost:55441/postgres  (same command)

Run it in its own pytest process, like unit-tests/classifier and
unit-tests/detector.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import sys
import uuid
from typing import Dict, Optional
from urllib.parse import unquote, urlsplit

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVICE_DIR = ROOT / "ai" / "route-policy"
RULESETS_DIR = SERVICE_DIR / "rulesets"

if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))
# rp_helpers.py (conversation builders, the app with no database).
_HERE = pathlib.Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

DB_ENV_KEYS = ("ROUTE_POLICY_DB_HOST", "ROUTE_POLICY_DB_PORT", "ROUTE_POLICY_DB_USER",
               "ROUTE_POLICY_DB_PASSWORD", "ROUTE_POLICY_DB_NAME")


class ThrowawayDatabase:
    """One uniquely named database on the server behind ``admin``."""

    def __init__(self, admin: str) -> None:
        self.admin = admin
        self.name = f"route_policy_test_{os.getpid()}_{uuid.uuid4().hex[:8]}"
        self.created = False

    def env(self) -> Dict[str, str]:
        parts = urlsplit(self.admin)
        return {
            "ROUTE_POLICY_DB_HOST": parts.hostname or "localhost",
            "ROUTE_POLICY_DB_PORT": str(parts.port or 5432),
            "ROUTE_POLICY_DB_USER": unquote(parts.username or "postgres"),
            "ROUTE_POLICY_DB_PASSWORD": unquote(parts.password or ""),
            "ROUTE_POLICY_DB_NAME": self.name,
        }

    async def _execute(self, sql: str) -> None:
        import asyncpg

        conn = await asyncpg.connect(self.admin)
        try:
            await conn.execute(sql)
        finally:
            await conn.close()

    def create(self) -> "ThrowawayDatabase":
        asyncio.run(self._execute(f'CREATE DATABASE "{self.name}"'))
        self.created = True
        return self

    def drop(self) -> None:
        if not self.created:
            return
        try:
            asyncio.run(self._execute(f'DROP DATABASE IF EXISTS "{self.name}" WITH (FORCE)'))
            self.created = False
        except Exception as exc:  # noqa: BLE001
            print(f"route-policy tests: could not drop {self.name}: {exc}")


_TEST_DSN = os.environ.get("TEST_POSTGRES_DSN", "").strip()
_SESSION_DB: Optional[ThrowawayDatabase] = ThrowawayDatabase(_TEST_DSN).create() if _TEST_DSN else None
if _SESSION_DB is not None:
    os.environ.update(_SESSION_DB.env())
else:
    for _key in DB_ENV_KEYS:
        os.environ.pop(_key, None)
    os.environ["ROUTE_POLICY_DB_HOST"] = ""
# Re-read on every refresh in tests that change bindings under a running app.
os.environ["ROUTE_POLICY_BINDING_REFRESH_S"] = "0"
# The shared tests read what they just wrote, so their database waits must
# not be cut short: a scratch Postgres on a laptop can take more than 50 ms.
# The budgets themselves are tested with fake databases and explicit values
# (test_writes.py).
os.environ["ROUTE_POLICY_DECISION_WRITE_BUDGET_S"] = "30"
import route_policy.config as _config  # noqa: E402 -- after the environment above

_config.DB_READ_BUDGET_S = 30.0

HAVE_POSTGRES = _SESSION_DB is not None


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "postgres: needs route-policy's Postgres (TEST_POSTGRES_DSN); skipped without it")


def pytest_collection_modifyitems(config: pytest.Config, items) -> None:
    if HAVE_POSTGRES:
        return
    skip = pytest.mark.skip(reason="TEST_POSTGRES_DSN not set")
    for item in items:
        if "postgres" in item.keywords:
            item.add_marker(skip)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if _SESSION_DB is not None:
        _SESSION_DB.drop()


@pytest.fixture(scope="session")
def admin_dsn() -> str:
    if not HAVE_POSTGRES:
        pytest.skip("TEST_POSTGRES_DSN not set")
    return _TEST_DSN


@pytest.fixture
def fresh_database(admin_dsn):
    """A database of its own for one test (startup / seeding / migration
    tests that must begin empty). Yields its DSN; dropped afterwards."""
    db = ThrowawayDatabase(admin_dsn).create()
    parts = urlsplit(admin_dsn)
    netloc = parts.netloc
    try:
        yield f"{parts.scheme}://{netloc}/{db.name}"
    finally:
        db.drop()

