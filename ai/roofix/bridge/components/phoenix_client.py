"""
PHOENIX CLIENT — direct psycopg2 access to the Phoenix CRM database.

Reverted from the Phoenix MCP variant because the MCP write tools aren't
ready in time for end-to-end testing. Once Phoenix MCP write tools land, we
can swap back — the public method surface is unchanged so the orchestrator
doesn't care which implementation it's talking to.

Phoenix is a PostgreSQL CRM. Confirmed structure (from schema inspection):

  project                  thin core row: project_name (customer), street1/city/
                           postal_code/state_id, object_type_id (6 = roofing),
                           object_status_id, external_id (UUID), migration_external_id.
  note                     chatter/comments. project_id + content_text +
                           content_delta (Quill JSONB) + created_by_id.
  project_process_block    a project's milestone stages. project_id + process_block_id
                           + object_status_id.
  process_block            the named stages (process_block_name -> id).

Contract D (Orchestrator -> Phoenix):
    update_chatter(project_id, note_text)             -> Result
    update_milestone(project_id, block_name, status)  -> Result
    create_project(fields)                            -> Result   (Phase 1)

Memory (lives in Phoenix, same DB as real data):
    find_project_by_identity(name, address)  -> match info
    find_project_by_roofix_id(roofix_id)     -> match info

DRY_RUN: when true, reads still run, but writes are NOT executed — the intended
SQL and params are logged and returned so we can inspect what *would* happen.
"""

from __future__ import annotations

# Load .env before the module-level env reads below, so this file works both
# when imported by app.py (which also loads .env) and when run directly as
# __main__ for the smoke test. load_env is idempotent — safe to call twice.
from common.env import load_env

load_env()

import json
import os
from dataclasses import dataclass, field
from typing import Any, Optional

import psycopg2
import psycopg2.extras


# --- configuration knobs --------------------------------------------------------

ROOFING_OBJECT_TYPE_ID = int(os.getenv("PHOENIX_ROOFING_OBJECT_TYPE_ID", "6"))
AGENT_USER_ID = os.getenv("PHOENIX_AGENT_USER_ID")
_ROOFIX_ID_COLUMN = os.getenv("PHOENIX_ROOFIX_ID_COLUMN", "migration_external_id")
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"


# --- result type ----------------------------------------------------------------

@dataclass
class Result:
    """Every write returns one of these — never a bare bool. Failures surfaced."""
    ok: bool
    detail: str = ""
    data: dict = field(default_factory=dict)
    dry_run: bool = False

    def __bool__(self) -> bool:
        return self.ok


# --- client ---------------------------------------------------------------------

class PhoenixClient:
    def __init__(self, dry_run: Optional[bool] = None):
        self.dry_run = DRY_RUN if dry_run is None else dry_run
        self._conn = None

    # connection -----------------------------------------------------------------

    def connect(self):
        """Open a connection from env vars. Raises if it can't — fail loud, not silent."""
        if self._conn and not self._conn.closed:
            return self._conn
        self._conn = psycopg2.connect(
            host=os.environ["PHOENIX_DB_HOST"],
            port=os.getenv("PHOENIX_DB_PORT", "5432"),
            dbname=os.environ["PHOENIX_DB_NAME"],
            user=os.environ["PHOENIX_DB_USER"],
            password=os.environ["PHOENIX_DB_PASSWORD"],
            options="-c search_path=phoenix",
            sslmode=os.getenv("PHOENIX_DB_SSLMODE", "require"),
        )
        self._conn.autocommit = False
        return self._conn

    def close(self):
        if self._conn and not self._conn.closed:
            self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _cursor(self):
        return self.connect().cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # reads ----------------------------------------------------------------------

    def ping(self) -> Result:
        """Trivial read — proves auth + reachability."""
        try:
            with self._cursor() as cur:
                cur.execute("SELECT 1 AS ok;")
                cur.fetchone()
            return Result(ok=True, detail="Phoenix reachable")
        except Exception as e:
            return Result(ok=False, detail=f"ping failed: {e}")

    def find_project_by_roofix_id(self, roofix_id: str) -> Result:
        """Primary identity path: exact match on the column we stamp the Roofix
        id into. Clean and unambiguous."""
        col = _ROOFIX_ID_COLUMN
        sql = (
            f"SELECT id, project_name, street1, city, postal_code, object_status_id "
            f"FROM project WHERE {col} = %s AND archived = false;"
        )
        try:
            with self._cursor() as cur:
                cur.execute(sql, (roofix_id,))
                rows = cur.fetchall()
            return Result(
                ok=True,
                detail=f"{len(rows)} match(es) on {col}",
                data={"matches": [dict(r) for r in rows]},
            )
        except Exception as e:
            return Result(ok=False, detail=f"find_by_roofix_id failed: {e}")

    def find_project_by_identity(self, name: str, street1: Optional[str] = None) -> Result:
        """Fallback identity path: case-insensitive match on customer name
        (+ street if given). Returns ALL candidates with a count so the caller
        can decide; deliberately does NOT auto-pick when ambiguous."""
        clauses = ["LOWER(project_name) = LOWER(%s)", "archived = false"]
        params: list[Any] = [name.strip()]
        if street1:
            clauses.append("LOWER(street1) = LOWER(%s)")
            params.append(street1.strip())
        sql = (
            "SELECT id, project_name, street1, city, postal_code, object_status_id "
            "FROM project WHERE " + " AND ".join(clauses) + ";"
        )
        try:
            with self._cursor() as cur:
                cur.execute(sql, tuple(params))
                rows = cur.fetchall()
            matches = [dict(r) for r in rows]
            return Result(
                ok=True,
                detail=f"{len(matches)} candidate(s)",
                data={"matches": matches, "unambiguous": len(matches) == 1},
            )
        except Exception as e:
            return Result(ok=False, detail=f"find_by_identity failed: {e}")

    def resolve_process_block_id(self, block_name: str) -> Result:
        """Look up a milestone stage id by its human name. The Roofix-event
        → block_name mapping is Michael's; this just resolves the name to an id."""
        sql = (
            "SELECT id, process_block_name FROM process_block "
            "WHERE LOWER(process_block_name) = LOWER(%s) AND archived = false;"
        )
        try:
            with self._cursor() as cur:
                cur.execute(sql, (block_name.strip(),))
                rows = cur.fetchall()
            return Result(ok=True, detail=f"{len(rows)} match(es)",
                          data={"matches": [dict(r) for r in rows]})
        except Exception as e:
            return Result(ok=False, detail=f"resolve_process_block_id failed: {e}")

    # writes ---------------------------------------------------------------------

    def _require_agent_user(self) -> Optional[Result]:
        if not AGENT_USER_ID:
            return Result(ok=False, detail=(
                "PHOENIX_AGENT_USER_ID is not set. Create a dedicated agent user in "
                "Phoenix and set it before writing, so notes are attributable."))
        return None

    def _build_note_delta(self, text: str) -> dict:
        """Minimal Quill delta wrapper around plain text. Phoenix stores both
        the rich content_delta and the plain content_text; for an agent comment
        a single insert op is enough."""
        return {"ops": [{"insert": text if text.endswith("\n") else text + "\n"}]}

    def update_chatter(self, project_id: int, note_text: str) -> Result:
        """Phase 0 core action: post a comment to a project's chatter by
        inserting a row into `note`. Append-only."""
        guard = self._require_agent_user()
        if guard:
            return guard

        delta = self._build_note_delta(note_text)
        sql = (
            "INSERT INTO note (project_id, content_delta, content_text, "
            "created_by_id, date_created, archived) "
            "VALUES (%s, %s, %s, %s, NOW(), false) RETURNING id;"
        )
        params = (project_id, json.dumps(delta), note_text, int(AGENT_USER_ID))

        if self.dry_run:
            return Result(ok=True, dry_run=True,
                          detail="DRY_RUN: would insert note",
                          data={"sql": sql, "params": params})
        try:
            conn = self.connect()
            with conn.cursor() as cur:
                cur.execute(sql, params)
                new_id = cur.fetchone()[0]
            conn.commit()
            return Result(ok=True, detail="note inserted", data={"note_id": new_id})
        except Exception as e:
            self.connect().rollback()
            return Result(ok=False, detail=f"update_chatter failed: {e}")

    def update_milestone(self, project_id: int, block_name: str,
                         status_id: int) -> Result:
        """Advance a milestone: set object_status_id on the project's
        project_process_block row for the given stage (insert if absent).

        Needs Michael's mapping for (block_name, status_id)."""
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

        update_sql = (
            "UPDATE project_process_block SET object_status_id = %s, "
            "modified_by_id = %s, date_modified = NOW() "
            "WHERE project_id = %s AND process_block_id = %s "
            "RETURNING id;"
        )
        insert_sql = (
            "INSERT INTO project_process_block (project_id, process_block_id, main, "
            "object_status_id, created_by_id, date_created, archived) "
            "VALUES (%s, %s, false, %s, %s, NOW(), false) RETURNING id;"
        )
        up_params = (status_id, int(AGENT_USER_ID), project_id, process_block_id)
        ins_params = (project_id, process_block_id, status_id, int(AGENT_USER_ID))

        if self.dry_run:
            return Result(ok=True, dry_run=True,
                          detail="DRY_RUN: would update-or-insert process block",
                          data={"update_sql": update_sql, "update_params": up_params,
                                "insert_sql": insert_sql, "insert_params": ins_params})
        try:
            conn = self.connect()
            with conn.cursor() as cur:
                cur.execute(update_sql, up_params)
                row = cur.fetchone()
                if row is None:
                    cur.execute(insert_sql, ins_params)
                    row = cur.fetchone()
                ppb_id = row[0]
            conn.commit()
            return Result(ok=True, detail="milestone set",
                          data={"project_process_block_id": ppb_id})
        except Exception as e:
            self.connect().rollback()
            return Result(ok=False, detail=f"update_milestone failed: {e}")

    def create_project(self, fields: dict) -> Result:
        """Phase 1. Deliberately not implemented yet — creation needs the
        scraped field set + Michael's full field mapping."""
        return Result(ok=False, detail="create_project not implemented (Phase 1)")


# --- standalone connection test -------------------------------------------------
# Run on the server after filling .env:  python components/phoenix_client.py
if __name__ == "__main__":
    client = PhoenixClient()
    print("DRY_RUN:", client.dry_run)
    r = client.ping()
    print("ping:", r.ok, "-", r.detail)
    client.close()
