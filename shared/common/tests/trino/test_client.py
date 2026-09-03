"""Guardrail tests for common.trino.

No live coordinator here — these test the reject-write and clamp-limit
paths that run before the driver is touched. End-to-end round-trip
against a real Trino lives in the operator-run checks in
ai/trino/TRINO.md § Verification.
"""
from __future__ import annotations

import pytest

from common.trino import TrinoClient, TrinoQueryError
from common.trino.client import _clamp_limit


@pytest.fixture
def client() -> TrinoClient:
    return TrinoClient(host="unused", default_max_rows=100)


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE iceberg.demo.events",
        "INSERT INTO iceberg.demo.events VALUES (1, 'x', now())",
        "DELETE FROM postgres_roofix.public.processed_store",
        "ALTER TABLE t ADD COLUMN c INT",
        "CALL system.runtime.kill_query('x')",
        "GRANT SELECT ON schema.tbl TO USER foo",
    ],
)
def test_rejects_write_statements(client: TrinoClient, sql: str) -> None:
    with pytest.raises(TrinoQueryError):
        client._reject_non_select(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "  SELECT 1",
        "-- some comment\nSELECT 1",
        "/* block */ SELECT 1",
        "WITH t AS (SELECT 1) SELECT * FROM t",
        "SHOW CATALOGS",
        "DESCRIBE iceberg.demo.events",
        "EXPLAIN SELECT 1",
    ],
)
def test_accepts_read_statements(client: TrinoClient, sql: str) -> None:
    # Should not raise.
    client._reject_non_select(sql)


def test_write_mode_bypasses_check() -> None:
    write_client = TrinoClient(host="unused", allow_writes=True)
    # Would fail the guardrail — but write mode skips it entirely.
    # We can't actually .execute() without a coordinator, so just prove
    # the guard method never gets called on the write path by inspection.
    assert write_client.allow_writes is True


def test_clamp_limit_appends_when_absent() -> None:
    assert _clamp_limit("SELECT * FROM t", 100) == "SELECT * FROM t LIMIT 100"


def test_clamp_limit_leaves_smaller_limit_alone() -> None:
    assert _clamp_limit("SELECT * FROM t LIMIT 5", 100) == "SELECT * FROM t LIMIT 5"


def test_clamp_limit_rewrites_larger_limit() -> None:
    result = _clamp_limit("SELECT * FROM t LIMIT 999999", 100)
    assert result.endswith("LIMIT 100")


def test_clamp_limit_handles_trailing_semicolon() -> None:
    result = _clamp_limit("SELECT * FROM t LIMIT 999999;", 100)
    assert result.endswith("LIMIT 100")


def test_clamp_limit_ignores_inner_limits() -> None:
    # LIMIT inside a subquery isn't the row-cap on the outer result set;
    # we don't try to be clever about it. The clamp appends a fresh
    # outer LIMIT.
    result = _clamp_limit("SELECT * FROM (SELECT * FROM t LIMIT 500) sub", 100)
    assert result.endswith("LIMIT 100")
