"""FastAPI + FastMCP shim exposing Trino as an LLM tool.

Registered in ai/litellm/litellm_config.yaml as ``mcp_servers.trino`` at
``http://trino-mcp:8080/mcp``. Any LiteLLM-served model with tool calling
sees the tools below and can run federated SQL over the Iceberg
warehouse + the three Postgres catalogs.

Guardrails (enforced on run_query only — the discovery tools are
schema-only):

* SELECT-only. Any DDL/DML raises TrinoQueryError.
* Row cap: TRINO_MCP_MAX_ROWS (default 10 000). If the SELECT lacks a
  LIMIT clause we splice one in; if it has a larger one we clamp it.
* Runtime cap: TRINO_MCP_MAX_RUNTIME_S (default 30). Forwarded as
  ``query.max-execution-time`` per request via the session properties.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import FastAPI
from fastmcp import FastMCP
from pydantic import Field

from common.trino import TrinoClient, TrinoQueryError

logging.basicConfig(
    level=logging.DEBUG if os.environ.get("DEBUG_LOGGING") == "true" else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("trino-mcp")


_client = TrinoClient(
    host=os.environ.get("TRINO_HOST", "trino-coordinator"),
    port=int(os.environ.get("TRINO_PORT", "8080")),
    user=os.environ.get("TRINO_USER", "trino-mcp"),
    default_max_rows=int(os.environ.get("TRINO_MCP_MAX_ROWS", "10000")),
    default_timeout_s=int(os.environ.get("TRINO_MCP_MAX_RUNTIME_S", "30")),
)


mcp = FastMCP(name="trino")


@mcp.tool()
async def list_catalogs() -> list[str]:
    """Return every catalog Trino can query. Call this first when you
    don't know which data source holds the answer.

    Typical result: ``["iceberg", "postgres_litellm", "postgres_roofix",
    "postgres_sandbox", "system"]``. ``iceberg`` is the lakehouse on
    MinIO; the ``postgres_*`` entries federate the three subsystem
    Postgres instances read-only.
    """
    log.info("MCP tool call: list_catalogs")
    _, rows = _client.execute("SHOW CATALOGS")
    return [row[0] for row in rows]


@mcp.tool()
async def list_schemas(
    catalog: str = Field(description="Catalog name from list_catalogs."),
) -> list[str]:
    """Return every schema in a catalog. Postgres catalogs expose the
    database's schemas (``public``, ``pg_catalog``, …); the Iceberg
    catalog exposes the HMS databases created via ``CREATE SCHEMA``.
    """
    log.info("MCP tool call: list_schemas catalog=%s", catalog)
    _, rows = _client.execute(f"SHOW SCHEMAS FROM {_ident(catalog)}")
    return [row[0] for row in rows]


@mcp.tool()
async def list_tables(
    catalog: str = Field(description="Catalog name from list_catalogs."),
    schema: str = Field(description="Schema name from list_schemas."),
) -> list[str]:
    """Return every table in a schema."""
    log.info("MCP tool call: list_tables catalog=%s schema=%s", catalog, schema)
    _, rows = _client.execute(
        f"SHOW TABLES FROM {_ident(catalog)}.{_ident(schema)}"
    )
    return [row[0] for row in rows]


@mcp.tool()
async def describe_table(
    catalog: str = Field(description="Catalog name from list_catalogs."),
    schema: str = Field(description="Schema name from list_schemas."),
    table: str = Field(description="Table name from list_tables."),
) -> list[dict[str, str]]:
    """Return the columns of a table as ``[{"name": ..., "type": ...}, ...]``.
    Prefer calling this before ``run_query`` so your SELECT list matches
    the real column names.
    """
    log.info(
        "MCP tool call: describe_table catalog=%s schema=%s table=%s",
        catalog, schema, table,
    )
    _, rows = _client.execute(
        f"DESCRIBE {_ident(catalog)}.{_ident(schema)}.{_ident(table)}"
    )
    # DESCRIBE returns columns: Column | Type | Extra | Comment
    return [{"name": r[0], "type": r[1]} for r in rows]


@mcp.tool()
async def run_query(
    sql: str = Field(
        description=(
            "A single SELECT statement (or WITH … SELECT). DDL/DML is "
            "rejected. Reference tables with three-part names — e.g. "
            "`SELECT count(*) FROM postgres_roofix.public.processed_store`."
        )
    ),
    max_rows: int | None = Field(
        default=None,
        description=(
            "Optional cap on returned rows. Server clamps to "
            "TRINO_MCP_MAX_ROWS (default 10 000). Omit to use the "
            "default; the server will splice a LIMIT into your query "
            "if you didn't include one."
        ),
    ),
) -> dict[str, Any]:
    """Run a SELECT against Trino and return ``{columns, rows, row_count,
    truncated}``. Errors come back as ``{error: "...", hint: "..."}`` —
    do not retry the same query; either fix the SQL or call the
    discovery tools first.
    """
    log.info("MCP tool call: run_query max_rows=%s sql=%r", max_rows, sql[:200])
    try:
        columns, rows = _client.execute(sql, max_rows=max_rows)
    except TrinoQueryError as e:
        log.warning("run_query error: %s", e)
        return {"error": str(e), "hint": e.hint or ""}
    truncated = len(rows) >= (max_rows or _client.default_max_rows)
    return {
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
    }


def _ident(name: str) -> str:
    """Quote a Trino identifier. Reject anything that isn't a plain
    catalog/schema/table token so we don't smuggle SQL through the
    discovery tools.
    """
    if not name.replace("_", "").isalnum():
        raise TrinoQueryError(
            f"invalid identifier: {name!r}",
            hint="Identifiers must be alphanumeric plus underscore.",
        )
    return f'"{name}"'


# FastMCP mounts the MCP transport on /mcp; FastAPI serves /health and
# anything else we tack on for operator sanity.
app = FastAPI(title="trino-mcp")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


app.mount("/mcp", mcp.http_app())
