"""Guarded Trino client.

Two things this wrapper adds over the raw ``trino.dbapi`` driver:

1. A SELECT-only mode (``allow_writes=False``, the default) that
   rejects any statement whose first significant token isn't ``SELECT``
   or ``WITH``. Non-alphanumeric prefixes (comments, whitespace) are
   stripped before the check.

2. Row + runtime clamps forwarded per-request via
   ``session_properties={"query_max_execution_time": ...}`` and a
   spliced ``LIMIT`` clause on SELECTs that don't already have a tighter
   one. The clamps keep a rogue tool call from starving the coordinator.

The write path exists for one caller only: the warehouse-seed script.
Everything model-facing goes through the default (guarded) path.
"""
from __future__ import annotations

import re
from typing import Any, Optional

import trino
from trino.exceptions import TrinoQueryError as _DriverQueryError


class TrinoQueryError(Exception):
    """Raised by ``TrinoClient.execute`` when a query is rejected by the
    guardrails OR the coordinator returns an error. Carries an optional
    ``hint`` field the MCP shim surfaces back to the model.
    """

    def __init__(self, message: str, hint: Optional[str] = None) -> None:
        super().__init__(message)
        self.hint = hint


# Match a leading token, ignoring whitespace and line/block comments.
# We collapse then inspect the first alphabetic word.
_LEADING_COMMENT = re.compile(
    r"^(?:\s+|--[^\n]*\n|/\*.*?\*/)*", flags=re.DOTALL
)
_FIRST_WORD = re.compile(r"[A-Za-z]+")


class TrinoClient:
    """Blocking Trino client. One connection per ``execute`` call —
    Trino's Python driver is not thread-safe and holding cursors open
    across MCP requests would deadlock the FastMCP worker.

    Args:
        host / port / user: coordinator endpoint.
        default_max_rows: LIMIT clamp applied to guarded SELECTs when
            the caller doesn't pass ``max_rows``.
        default_timeout_s: ``query.max_execution_time`` forwarded per
            request in ``session_properties``.
        allow_writes: when True, skip the SELECT-only check. Only the
            warehouse-seed script sets this.
    """

    def __init__(
        self,
        host: str,
        port: int = 8080,
        user: str = "trino",
        default_max_rows: int = 10_000,
        default_timeout_s: int = 30,
        allow_writes: bool = False,
    ) -> None:
        self.host = host
        self.port = port
        self.user = user
        self.default_max_rows = default_max_rows
        self.default_timeout_s = default_timeout_s
        self.allow_writes = allow_writes

    def execute(
        self,
        sql: str,
        *,
        max_rows: Optional[int] = None,
        timeout_s: Optional[int] = None,
    ) -> tuple[list[str], list[list[Any]]]:
        """Run ``sql`` and return ``(column_names, rows)``.

        Guardrail behavior (only when ``allow_writes=False``):

        * Any statement whose first alphabetic token isn't ``SELECT`` or
          ``WITH`` raises ``TrinoQueryError`` before dispatch. This
          catches ``DROP``, ``INSERT``, ``DELETE``, ``ALTER``, ``CALL``,
          ``EXECUTE``, ``USE``, ``GRANT``, ``REVOKE`` — everything with
          side effects.
        * A LIMIT clamp is applied. If ``max_rows`` is passed it wins;
          otherwise ``default_max_rows`` is used. If the SQL already has
          a smaller LIMIT we leave it alone.
        """
        if not self.allow_writes:
            self._reject_non_select(sql)
            effective_max = max_rows if max_rows is not None else self.default_max_rows
            effective_max = max(1, min(effective_max, self.default_max_rows))
            sql = _clamp_limit(sql, effective_max)

        timeout_s = timeout_s or self.default_timeout_s
        session_properties = {"query_max_execution_time": f"{timeout_s}s"}

        conn = trino.dbapi.connect(
            host=self.host,
            port=self.port,
            user=self.user,
            session_properties=session_properties,
        )
        try:
            cur = conn.cursor()
            try:
                cur.execute(sql)
                rows = cur.fetchall()
                columns = [c[0] for c in (cur.description or [])]
                return columns, rows
            finally:
                cur.close()
        except _DriverQueryError as e:
            raise TrinoQueryError(
                str(e),
                hint=(
                    "The coordinator rejected this. Common causes: "
                    "unknown table (call list_tables first), missing "
                    "three-part name (catalog.schema.table), or a "
                    "column that doesn't exist (describe_table)."
                ),
            ) from e
        finally:
            conn.close()

    @staticmethod
    def _reject_non_select(sql: str) -> None:
        stripped = _LEADING_COMMENT.sub("", sql).lstrip()
        match = _FIRST_WORD.match(stripped)
        first = match.group(0).upper() if match else ""
        if first not in {"SELECT", "WITH", "SHOW", "DESCRIBE", "EXPLAIN"}:
            raise TrinoQueryError(
                f"only SELECT/WITH/SHOW/DESCRIBE/EXPLAIN permitted; got {first or '?'}",
                hint=(
                    "run_query is read-only. Use the Trino UI or Superset "
                    "if you genuinely need DDL/DML."
                ),
            )


_LIMIT_TAIL = re.compile(
    r"\bLIMIT\s+(\d+)\s*;?\s*$", flags=re.IGNORECASE
)


def _clamp_limit(sql: str, max_rows: int) -> str:
    """Ensure the SQL ends with LIMIT ≤ max_rows.

    Two cases:

    * Already has a trailing ``LIMIT n`` — rewrite in place if
      ``n > max_rows``, leave it alone otherwise.
    * No trailing LIMIT — append `` LIMIT max_rows``. Anything more
      structural (LIMIT inside a subquery, LIMIT ALL, etc.) is left
      alone; the coordinator's own ``query.max_execution_time`` and the
      row-fetch cap in ``execute`` still apply, so no unbounded fetch
      escapes.
    """
    stripped = sql.rstrip().rstrip(";").rstrip()
    match = _LIMIT_TAIL.search(stripped)
    if match:
        existing = int(match.group(1))
        if existing <= max_rows:
            return sql
        start, end = match.span()
        return stripped[:start] + f"LIMIT {max_rows}"
    return f"{stripped} LIMIT {max_rows}"
