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


# --- execute(): what actually reaches the coordinator --------------------
#
# A fake driver records the SQL and how rows were fetched. Regression for
# describe_table failing with "line 1:53: mismatched input 'LIMIT'": the
# LIMIT clamp used to be spliced into every guarded statement, and Trino's
# grammar has no LIMIT on SHOW / DESCRIBE / EXPLAIN.


class _FakeCursor:
    def __init__(self, rows: list[list[object]]) -> None:
        self._rows = rows
        self.sql: str | None = None
        self.fetched_with: tuple[str, int | None] | None = None
        self.closed = False
        self.description = [("col",)]

    def execute(self, sql: str) -> None:
        self.sql = sql

    def fetchall(self) -> list[list[object]]:
        self.fetched_with = ("fetchall", None)
        return list(self._rows)

    def fetchmany(self, size: int) -> list[list[object]]:
        self.fetched_with = ("fetchmany", size)
        return list(self._rows[:size])

    def close(self) -> None:
        self.closed = True


class _FakeConn:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _FakeCursor:
        return self._cursor

    def close(self) -> None:
        pass


@pytest.fixture
def fake_cursor(monkeypatch: pytest.MonkeyPatch) -> _FakeCursor:
    cursor = _FakeCursor([[i] for i in range(250)])
    monkeypatch.setattr(
        "common.trino.client.trino.dbapi.connect",
        lambda **_: _FakeConn(cursor),
    )
    return cursor


@pytest.mark.parametrize(
    "sql",
    [
        'DESCRIBE "postgres_classifier"."public"."llm_calls"',
        'SHOW COLUMNS FROM "postgres_classifier"."public"."llm_calls"',
        "SHOW CATALOGS",
        'SHOW SCHEMAS FROM "postgres_classifier"',
        'SHOW TABLES FROM "postgres_classifier"."public"',
        "-- leading comment\nDESCRIBE iceberg.demo.events",
        "EXPLAIN SELECT * FROM t",
    ],
)
def test_execute_never_appends_limit_to_non_select(
    client: TrinoClient, fake_cursor: _FakeCursor, sql: str
) -> None:
    client.execute(sql)
    assert fake_cursor.sql == sql
    assert "LIMIT" not in fake_cursor.sql.upper()


def test_execute_caps_non_select_rows_at_fetch(
    client: TrinoClient, fake_cursor: _FakeCursor
) -> None:
    _, rows = client.execute("SHOW CATALOGS")
    assert fake_cursor.fetched_with == ("fetchmany", 100)
    assert len(rows) == 100
    assert fake_cursor.closed


def test_execute_appends_limit_to_select_without_one(
    client: TrinoClient, fake_cursor: _FakeCursor
) -> None:
    client.execute("SELECT * FROM t")
    assert fake_cursor.sql == "SELECT * FROM t LIMIT 100"
    assert fake_cursor.fetched_with == ("fetchmany", 100)


def test_execute_keeps_smaller_select_limit(
    client: TrinoClient, fake_cursor: _FakeCursor
) -> None:
    client.execute("SELECT * FROM t LIMIT 5")
    assert fake_cursor.sql == "SELECT * FROM t LIMIT 5"


def test_execute_clamps_larger_select_limit(
    client: TrinoClient, fake_cursor: _FakeCursor
) -> None:
    client.execute("SELECT * FROM t LIMIT 999999")
    assert fake_cursor.sql == "SELECT * FROM t LIMIT 100"


def test_execute_appends_limit_to_with_select(
    client: TrinoClient, fake_cursor: _FakeCursor
) -> None:
    client.execute("WITH x AS (SELECT 1 AS a) SELECT * FROM x")
    assert fake_cursor.sql == "WITH x AS (SELECT 1 AS a) SELECT * FROM x LIMIT 100"


def test_execute_max_rows_lowers_cap_but_never_raises_it(
    client: TrinoClient, fake_cursor: _FakeCursor
) -> None:
    client.execute("SELECT * FROM t", max_rows=10)
    assert fake_cursor.sql == "SELECT * FROM t LIMIT 10"
    assert fake_cursor.fetched_with == ("fetchmany", 10)

    client.execute("DESCRIBE t", max_rows=10_000)
    assert fake_cursor.sql == "DESCRIBE t"
    assert fake_cursor.fetched_with == ("fetchmany", 100)


def test_execute_rejects_writes_before_connecting(
    client: TrinoClient, fake_cursor: _FakeCursor
) -> None:
    with pytest.raises(TrinoQueryError):
        client.execute("DROP TABLE t")
    assert fake_cursor.sql is None


def test_execute_write_mode_sends_sql_untouched(fake_cursor: _FakeCursor) -> None:
    write_client = TrinoClient(host="unused", default_max_rows=100, allow_writes=True)
    _, rows = write_client.execute("CREATE SCHEMA iceberg.demo")
    assert fake_cursor.sql == "CREATE SCHEMA iceberg.demo"
    assert fake_cursor.fetched_with == ("fetchall", None)
    assert len(rows) == 250


def test_execute_failed_cancel_keeps_fetched_rows(
    client: TrinoClient, fake_cursor: _FakeCursor, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom() -> None:
        raise RuntimeError("cancel failed")

    monkeypatch.setattr(fake_cursor, "close", _boom)
    _, rows = client.execute("SHOW CATALOGS")
    assert len(rows) == 100
