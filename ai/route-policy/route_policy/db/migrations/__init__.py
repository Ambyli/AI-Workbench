"""Schema migrations: numbered SQL files, applied once each, in order.

    NNNN_<name>.sql     one migration; NNNN is its version (0001, 0002, ...)

``migrate(conn)`` runs at every startup, inside ONE transaction holding an
advisory lock, so two copies starting together (a swap) cannot race:

* ``schema_migrations`` records each applied version with the file's SHA-256;
* an unapplied file is executed and recorded;
* an applied file whose content CHANGED stops startup -- a migration that
  has run on the box is history; change the schema with a new file.

The SQL itself is idempotent too (``IF NOT EXISTS``, ``CREATE OR REPLACE
VIEW``), so a database created by hand, or a half-recorded history, still
converges. Postgres DDL is transactional: a failing migration leaves nothing
behind.

    0001_initial.sql         rulesets, bindings, decisions, evaluations, outcomes,
                             the decision_outcomes view (phase 2)
    0002_llm_classifier.sql  binding_shadows, decisions.shadow_of, analyses (the
                             llm_classifier's verdicts), decision_outcomes over
                             primaries only, the shadow_comparisons view (phase 4)
"""

from __future__ import annotations

import hashlib
import logging
import pathlib
import re
from dataclasses import dataclass
from typing import Any, List

logger = logging.getLogger("route_policy.db")

MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parent
_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
LOCK = "route_policy.migrations"


class MigrationChanged(RuntimeError):
    """An applied migration's file no longer has the content that ran."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str
    sha256: str


def migrations(directory: pathlib.Path = MIGRATIONS_DIR) -> List[Migration]:
    out = []
    for path in sorted(directory.glob("*.sql")):
        match = _NAME.match(path.name)
        if not match:
            raise ValueError(f"migration file {path.name!r} must be named NNNN_<name>.sql")
        # LF-normalised, so a Windows checkout hashes like the image does.
        sql = path.read_bytes().replace(b"\r\n", b"\n").decode("utf-8")
        out.append(Migration(int(match.group(1)), path.name, sql, hashlib.sha256(sql.encode()).hexdigest()))
    versions = [m.version for m in out]
    if len(set(versions)) != len(versions):
        raise ValueError("two migration files share a version number")
    return out


async def migrate(conn: Any, directory: pathlib.Path = MIGRATIONS_DIR) -> List[str]:
    """Apply every pending migration; return the names applied."""
    pending = migrations(directory)
    applied: List[str] = []
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", LOCK)
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version integer PRIMARY KEY,"
            " name text NOT NULL,"
            " sha256 text NOT NULL,"
            " applied_at timestamptz NOT NULL DEFAULT now())"
        )
        done = {r["version"]: r for r in await conn.fetch("SELECT version, name, sha256 FROM schema_migrations")}
        for m in pending:
            row = done.get(m.version)
            if row is not None:
                if row["sha256"] != m.sha256:
                    raise MigrationChanged(
                        f"migration {m.name} was applied with different content (recorded "
                        f"{row['sha256'][:12]}, file {m.sha256[:12]}); add a new migration instead"
                    )
                continue
            await conn.execute(m.sql)
            await conn.execute(
                "INSERT INTO schema_migrations (version, name, sha256) VALUES ($1, $2, $3)",
                m.version, m.name, m.sha256,
            )
            applied.append(m.name)
    for name in applied:
        logger.info("db: applied migration %s", name)
    return applied
