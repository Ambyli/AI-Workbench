"""
PHOENIX MCP CLIENT — talks to Phoenix over the MCP HTTP transport (JSON-RPC 2.0).

Replaces the original psycopg2-based `phoenix_client.py`. Same public methods and
`Result` dataclass so the orchestrator is unchanged; only the wire format differs.

READS (today, via the existing Phoenix MCP `run_query` tool — SELECT-only):
    ping()
    find_project_by_roofix_id(roofix_id)
    find_project_by_identity(name, street1=None)
    resolve_process_block_id(block_name)

WRITES (assumes write tools land per the Phoenix MCP roadmap):
    update_chatter(project_id, note_text)
    update_milestone(project_id, block_name, status_id)
    create_project(fields)                       # Phase 1, still stubbed

The assumed write-tool names are configurable via env so we can adjust when the
real Phoenix MCP write tools land without a code change:
    PHOENIX_MCP_TOOL_QUERY           default "run_query"        (SELECT only)
    PHOENIX_MCP_TOOL_INSERT_NOTE     default "insert_note"      (planned)
    PHOENIX_MCP_TOOL_UPSERT_BLOCK    default "upsert_project_process_block"

DRY_RUN=true short-circuits writes and returns Result(ok=True, dry_run=True) with
the intended tool + arguments in `data` so we can inspect what would happen.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx


AGENT_USER_ID = os.getenv("PHOENIX_AGENT_USER_ID")
_ROOFIX_ID_COLUMN = os.getenv("PHOENIX_ROOFIX_ID_COLUMN", "migration_external_id")

_TOOL_QUERY = os.getenv("PHOENIX_MCP_TOOL_QUERY", "run_query")
_TOOL_INSERT_NOTE = os.getenv("PHOENIX_MCP_TOOL_INSERT_NOTE", "insert_note")
_TOOL_UPSERT_BLOCK = os.getenv(
    "PHOENIX_MCP_TOOL_UPSERT_BLOCK", "upsert_project_process_block")

DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"


@dataclass
class Result:
    ok: bool
    detail: str = ""
    data: dict = field(default_factory=dict)
    dry_run: bool = False

    def __bool__(self) -> bool:
        return self.ok


class PhoenixMcpClient:
    def __init__(self, url: Optional[str] = None, auth_value: Optional[str] = None,
                 dry_run: Optional[bool] = None, timeout: float = 30.0):
        self.url = (url or os.environ["PHOENIX_MCP_URL"]).rstrip("/")
        self.auth = auth_value or os.environ.get("PHOENIX_MCP_AUTH_VALUE", "")
        self.dry_run = DRY_RUN if dry_run is None else dry_run
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # --- transport ---------------------------------------------------------------

    def _call_tool(self, name: str, arguments: dict) -> dict:
        """Invoke an MCP tool. Returns the parsed tool result payload.

        Speaks JSON-RPC 2.0 tools/call. Text-content results are JSON-decoded
        when possible; otherwise returned as {"text": ...}.
        """
        headers = {"Content-Type": "application/json"}
        if self.auth:
            headers["Authorization"] = f"Bearer {self.auth}"

        body = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        resp = self._client.post(self.url, headers=headers, json=body)
        resp.raise_for_status()
        env = resp.json()
        if "error" in env:
            raise RuntimeError(f"MCP error: {env['error']}")
        result = env.get("result", {})
        if result.get("isError"):
            raise RuntimeError(f"MCP tool '{name}' returned isError=true: {result}")

        for block in result.get("content", []) or []:
            if block.get("type") == "text":
                txt = block.get("text", "")
                try:
                    return {"value": json.loads(txt)}
                except json.JSONDecodeError:
                    return {"text": txt}
        return {"raw": result}

    def _query(self, sql: str) -> list[dict]:
        """Convenience: run a SELECT via the query tool and return a list of rows."""
        r = self._call_tool(_TOOL_QUERY, {"sql": sql})
        rows = r.get("value")
        if isinstance(rows, list):
            return rows
        if isinstance(rows, dict) and "rows" in rows:
            return rows["rows"]
        return []

    @staticmethod
    def _q(value: str) -> str:
        """Single-quote a SQL string literal (best-effort escape; MCP host does the
        real safety enforcement via prepared queries / validation)."""
        return "'" + str(value).replace("'", "''") + "'"

    # --- reads -------------------------------------------------------------------

    def ping(self) -> Result:
        try:
            rows = self._query("SELECT 1 AS ok;")
            return Result(ok=True, detail="Phoenix MCP reachable", data={"rows": rows})
        except Exception as e:
            return Result(ok=False, detail=f"ping failed: {e}")

    def find_project_by_roofix_id(self, roofix_id: str) -> Result:
        col = _ROOFIX_ID_COLUMN
        sql = (
            f"SELECT id, project_name, street1, city, postal_code, object_status_id "
            f"FROM project WHERE {col} = {self._q(roofix_id)} AND archived = false;"
        )
        try:
            rows = self._query(sql)
            return Result(ok=True, detail=f"{len(rows)} match(es) on {col}",
                          data={"matches": rows})
        except Exception as e:
            return Result(ok=False, detail=f"find_by_roofix_id failed: {e}")

    def find_project_by_identity(self, name: str, street1: Optional[str] = None) -> Result:
        clauses = [f"LOWER(project_name) = LOWER({self._q(name.strip())})",
                   "archived = false"]
        if street1:
            clauses.append(f"LOWER(street1) = LOWER({self._q(street1.strip())})")
        sql = ("SELECT id, project_name, street1, city, postal_code, object_status_id "
               "FROM project WHERE " + " AND ".join(clauses) + ";")
        try:
            rows = self._query(sql)
            return Result(ok=True, detail=f"{len(rows)} candidate(s)",
                          data={"matches": rows, "unambiguous": len(rows) == 1})
        except Exception as e:
            return Result(ok=False, detail=f"find_by_identity failed: {e}")

    def resolve_process_block_id(self, block_name: str) -> Result:
        sql = ("SELECT id, process_block_name FROM process_block "
               f"WHERE LOWER(process_block_name) = LOWER({self._q(block_name.strip())}) "
               f"AND archived = false;")
        try:
            rows = self._query(sql)
            return Result(ok=True, detail=f"{len(rows)} match(es)",
                          data={"matches": rows})
        except Exception as e:
            return Result(ok=False, detail=f"resolve_process_block_id failed: {e}")

    # --- writes ------------------------------------------------------------------

    def _require_agent_user(self) -> Optional[Result]:
        if not AGENT_USER_ID:
            return Result(ok=False, detail=(
                "PHOENIX_AGENT_USER_ID is not set. Create a dedicated agent user in "
                "Phoenix and set it before writing, so notes are attributable."))
        return None

    def update_chatter(self, project_id: int, note_text: str) -> Result:
        guard = self._require_agent_user()
        if guard:
            return guard
        args = {"project_id": int(project_id),
                "note_text": note_text,
                "agent_user_id": int(AGENT_USER_ID)}
        if self.dry_run:
            return Result(ok=True, dry_run=True,
                          detail=f"DRY_RUN: would call {_TOOL_INSERT_NOTE}",
                          data={"tool": _TOOL_INSERT_NOTE, "arguments": args})
        try:
            r = self._call_tool(_TOOL_INSERT_NOTE, args)
            new_id = (r.get("value") or {}).get("note_id") if isinstance(r.get("value"), dict) else None
            return Result(ok=True, detail="note inserted",
                          data={"note_id": new_id, "raw": r})
        except Exception as e:
            return Result(ok=False, detail=f"update_chatter failed: {e}")

    def update_milestone(self, project_id: int, block_name: str,
                         status_id: int) -> Result:
        blk = self.resolve_process_block_id(block_name)
        if not blk.ok:
            return blk
        matches = blk.data.get("matches", [])
        if len(matches) != 1:
            return Result(ok=False, detail=(
                f"process_block '{block_name}' resolved to {len(matches)} rows; "
                f"need exactly one (mapping with Michael)."))
        process_block_id = matches[0]["id"]

        guard = self._require_agent_user()
        if guard:
            return guard

        args = {"project_id": int(project_id),
                "process_block_id": int(process_block_id),
                "status_id": int(status_id),
                "agent_user_id": int(AGENT_USER_ID)}
        if self.dry_run:
            return Result(ok=True, dry_run=True,
                          detail=f"DRY_RUN: would call {_TOOL_UPSERT_BLOCK}",
                          data={"tool": _TOOL_UPSERT_BLOCK, "arguments": args})
        try:
            r = self._call_tool(_TOOL_UPSERT_BLOCK, args)
            ppb_id = (r.get("value") or {}).get("project_process_block_id") \
                if isinstance(r.get("value"), dict) else None
            return Result(ok=True, detail="milestone set",
                          data={"project_process_block_id": ppb_id, "raw": r})
        except Exception as e:
            return Result(ok=False, detail=f"update_milestone failed: {e}")

    def create_project(self, fields: dict) -> Result:
        return Result(ok=False, detail="create_project not implemented (Phase 1)")
