"""
PHOENIX CLIENT — direct psycopg2 access to the Phoenix CRM database.

Reverted from the Phoenix MCP variant because the MCP write tools weren't
ready in time for end-to-end testing. Once Phoenix MCP write tools land we
can swap back — the public method surface is unchanged so the orchestrator
doesn't care which implementation it's talking to.

Phoenix data model (confirmed via schema inspection):

  entity                           the homeowner (or other party) as an
                                   independent record — first_name/last_name,
                                   street1/city/postal_code, state_id (FK),
                                   email, phone, mobile_phone (both DOMAINs),
                                   object_type_id (8 = "Lead"),
                                   migration_external_id (Roofix homeowner id).

  project                          the roofing job/order — project_name,
                                   street1/city/postal_code/state_id,
                                   object_type_id (7 = "R&R / Roof",
                                   NOT 6 which is "Solar"),
                                   object_status_id (starter = 4
                                   "Qualification"), migration_external_id
                                   (Roofix project id), migration_entity_id
                                   (Roofix homeowner id, TEXT — different
                                   from the entity FK).

  entity_project_relationship      JUNCTION table linking entity ↔ project.
                                   Phoenix has NO direct entity_id FK on
                                   project. The link goes through this table
                                   with relationship_type_id (7 = "Homeowner")
                                   and a `main` boolean.

  note                             chatter/comments (Phase 0).

  project_process_block            milestone stages (Phase 0).

Contract D (Orchestrator -> Phoenix):
    update_chatter(project_id, note_text)             -> Result
    update_milestone(project_id, block_name, status)  -> Result
    find_project_by_roofix_id(roofix_id)              -> Result
    find_project_by_identity(name, address)           -> Result
    find_entity_by_identity(name, email, street1)     -> Result
    create_entity(**fields)                           -> Result
    create_project(**fields)                          -> Result
    link_entity_project(entity_id, project_id)        -> Result
    ensure_entity_and_project(extracted)              -> Result (idempotent
                                                          find-or-create for
                                                          both, then link)

DRY_RUN: when true, reads still run, but writes are NOT executed — the
intended SQL and params are logged and returned in Result.data so we can
inspect what *would* happen before flipping the flag.
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
# Values derived from Phoenix data (grep phoenix.object_type / relationship_type
# / phoenix.project GROUP BY company_id, etc.). Overridable via env for a
# non-default Phoenix deployment. All live in components/constants.py and are
# re-imported here so `from components.phoenix_client import COMPANY_ID` and
# `pc.AGENT_USER_ID = None` (test mutation pattern) keep working.

from components.constants import (
    PHOENIX_COMPANY_ID as COMPANY_ID,
    PHOENIX_PROJECT_OBJECT_TYPE_ID as PROJECT_OBJECT_TYPE_ID,
    PHOENIX_ENTITY_OBJECT_TYPE_ID as ENTITY_OBJECT_TYPE_ID,
    PHOENIX_HOMEOWNER_REL_TYPE_ID as HOMEOWNER_REL_TYPE_ID,
    PHOENIX_PROJECT_START_STATUS_ID as PROJECT_START_STATUS_ID,
    PHOENIX_AGENT_USER_ID as AGENT_USER_ID,
    PHOENIX_ROOFIX_ID_COLUMN as _ROOFIX_ID_COLUMN,
    DRY_RUN,
)


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
        # State lookup cache: keyed by lowercased text OR uppercased 2-letter
        # abbreviation. Populated lazily on first resolve_state_id call.
        self._state_cache_by_abbr: Optional[dict[str, int]] = None
        self._state_cache_by_name: Optional[dict[str, int]] = None

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
        """Primary project identity path: exact match on migration_external_id."""
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
        """Fallback project identity path: case-insensitive project_name (+ street1).
        Returns ALL candidates so the caller can decide; NEVER auto-picks."""
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

    def find_entity_by_identity(
        self,
        name: Optional[str] = None,
        email: Optional[str] = None,
        street1: Optional[str] = None,
    ) -> Result:
        """Find homeowner-type entities matching the given identity.

        Match rule (in priority order):
          1. If ``email`` is provided, match on lowercased email exact. Email is
             the strongest unique signal; a hit here wins even if name/street
             disagree slightly.
          2. Else if ``name`` (last_name required, first_name optional) and
             optional ``street1`` are provided, match on those case-insensitively.

        Returns Result.data = ``{matches: [...], unambiguous: bool}``. Callers
        should treat len > 1 as ambiguous and escalate rather than auto-pick.
        """
        clauses: list[str] = ["archived = false", "object_type_id = %s"]
        params: list[Any] = [ENTITY_OBJECT_TYPE_ID]

        if email:
            clauses.append("LOWER(email) = LOWER(%s)")
            params.append(email.strip())
        elif name:
            # Best-effort split: "First Last" → last_name=Last, first_name=First.
            # Falls back to whole-string as last_name.
            parts = name.strip().split(None, 1)
            first = parts[0] if len(parts) == 2 else None
            last = parts[1] if len(parts) == 2 else parts[0]
            clauses.append("LOWER(last_name) = LOWER(%s)")
            params.append(last)
            if first:
                clauses.append("LOWER(first_name) = LOWER(%s)")
                params.append(first)
            if street1:
                clauses.append("LOWER(street1) = LOWER(%s)")
                params.append(street1.strip())
        else:
            return Result(ok=False, detail=(
                "find_entity_by_identity needs at least one of email or name"))

        sql = (
            "SELECT id, first_name, last_name, email, street1, city, postal_code, "
            "state_id, migration_external_id "
            "FROM entity WHERE " + " AND ".join(clauses) + ";"
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
            return Result(ok=False, detail=f"find_entity_by_identity failed: {e}")

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

    def resolve_state_id(
        self,
        state_text: Optional[str] = None,
        state_abbr: Optional[str] = None,
    ) -> Optional[int]:
        """Return phoenix.state.id for a state name or 2-letter abbreviation.

        Prefers the abbreviation when provided (2 chars, exact-match). Falls
        back to case-insensitive name match. Returns None if neither resolves.
        Results are cached on the client instance — 50 states, one round trip.
        """
        if not state_text and not state_abbr:
            return None
        self._ensure_state_cache()
        assert self._state_cache_by_abbr is not None
        assert self._state_cache_by_name is not None
        if state_abbr:
            hit = self._state_cache_by_abbr.get(state_abbr.strip().upper())
            if hit is not None:
                return hit
        if state_text:
            return self._state_cache_by_name.get(state_text.strip().lower())
        return None

    def _ensure_state_cache(self) -> None:
        if self._state_cache_by_abbr is not None:
            return
        try:
            with self._cursor() as cur:
                cur.execute("SELECT id, state_name, abbreviation FROM state;")
                rows = cur.fetchall()
        except Exception:
            # Cache empty dicts so we don't repeatedly retry a broken DB.
            self._state_cache_by_abbr = {}
            self._state_cache_by_name = {}
            return
        self._state_cache_by_abbr = {r["abbreviation"].upper(): r["id"] for r in rows}
        self._state_cache_by_name = {r["state_name"].lower(): r["id"] for r in rows}

    # writes ---------------------------------------------------------------------

    def _require_agent_user(self) -> Optional[Result]:
        if not AGENT_USER_ID:
            return Result(ok=False, detail=(
                "PHOENIX_AGENT_USER_ID is not set. Create a dedicated agent user in "
                "Phoenix and set it before writing, so rows are attributable."))
        return None

    def _build_note_delta(self, text: str) -> dict:
        """Minimal Quill delta wrapper around plain text."""
        return {"ops": [{"insert": text if text.endswith("\n") else text + "\n"}]}

    def update_chatter(self, project_id: int, note_text: str) -> Result:
        """Phase 0: append-only insert into `note`."""
        guard = self._require_agent_user()
        if guard is not None:
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
        """Advance a milestone: upsert project_process_block."""
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
        if guard is not None:
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

    def create_entity(
        self,
        *,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        email: Optional[str] = None,
        phone: Optional[str] = None,
        mobile_phone: Optional[str] = None,
        street1: Optional[str] = None,
        street2: Optional[str] = None,
        city: Optional[str] = None,
        postal_code: Optional[str] = None,
        state_id: Optional[int] = None,
        migration_external_id: Optional[str] = None,
    ) -> Result:
        """Insert a new row into `entity` for a homeowner. All human data fields
        are optional per schema; company_id / object_type_id / created_by_id /
        archived / date_created are the only NOT NULLs (set from constants).
        """
        guard = self._require_agent_user()
        if guard is not None:
            return guard

        sql = (
            "INSERT INTO entity ("
            "  company_id, object_type_id, first_name, last_name, "
            "  email, phone, mobile_phone, street1, street2, city, "
            "  postal_code, state_id, migration_external_id, "
            "  created_by_id, date_created, archived"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), false) "
            "RETURNING id;"
        )
        params = (
            COMPANY_ID, ENTITY_OBJECT_TYPE_ID,
            first_name, last_name,
            email, phone, mobile_phone, street1, street2, city,
            postal_code, state_id, migration_external_id,
            int(AGENT_USER_ID),
        )

        if self.dry_run:
            return Result(ok=True, dry_run=True,
                          detail="DRY_RUN: would insert entity",
                          data={"sql": sql, "params": params})
        try:
            conn = self.connect()
            with conn.cursor() as cur:
                cur.execute(sql, params)
                new_id = cur.fetchone()[0]
            conn.commit()
            return Result(ok=True, detail="entity inserted", data={"entity_id": new_id})
        except Exception as e:
            self.connect().rollback()
            return Result(ok=False, detail=f"create_entity failed: {e}")

    def create_project(
        self,
        *,
        project_name: str,
        street1: Optional[str] = None,
        street2: Optional[str] = None,
        city: Optional[str] = None,
        postal_code: Optional[str] = None,
        state_id: Optional[int] = None,
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
        timezone: Optional[str] = None,
        migration_external_id: Optional[str] = None,
        migration_entity_id: Optional[str] = None,
        object_status_id: Optional[int] = None,
    ) -> Result:
        """Insert a new row into `project`. ``project_name`` is required
        (schema NN). Everything else is optional per schema. object_status_id
        defaults to PROJECT_START_STATUS_ID (Qualification).
        """
        guard = self._require_agent_user()
        if guard is not None:
            return guard
        if not project_name:
            return Result(ok=False, detail="project_name is required (NOT NULL)")

        status_id = object_status_id if object_status_id is not None else PROJECT_START_STATUS_ID

        sql = (
            "INSERT INTO project ("
            "  project_name, company_id, object_type_id, object_status_id, "
            "  street1, street2, city, postal_code, latitude, longitude, "
            "  state_id, timezone, migration_external_id, migration_entity_id, "
            "  created_by_id, date_created, archived"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), false) "
            "RETURNING id;"
        )
        params = (
            project_name, COMPANY_ID, PROJECT_OBJECT_TYPE_ID, status_id,
            street1, street2, city, postal_code, latitude, longitude,
            state_id, timezone, migration_external_id, migration_entity_id,
            int(AGENT_USER_ID),
        )

        if self.dry_run:
            return Result(ok=True, dry_run=True,
                          detail="DRY_RUN: would insert project",
                          data={"sql": sql, "params": params})
        try:
            conn = self.connect()
            with conn.cursor() as cur:
                cur.execute(sql, params)
                new_id = cur.fetchone()[0]
            conn.commit()
            return Result(ok=True, detail="project inserted", data={"project_id": new_id})
        except Exception as e:
            self.connect().rollback()
            return Result(ok=False, detail=f"create_project failed: {e}")

    def link_entity_project(
        self,
        entity_id: int,
        project_id: int,
        *,
        relationship_type_id: int = HOMEOWNER_REL_TYPE_ID,
        main: bool = True,
    ) -> Result:
        """Insert into entity_project_relationship — the junction table that
        links an entity (homeowner) to a project. Idempotency is the caller's
        responsibility; this method does NOT check for a pre-existing link.
        """
        guard = self._require_agent_user()
        if guard is not None:
            return guard

        sql = (
            "INSERT INTO entity_project_relationship ("
            "  entity_id, project_id, relationship_type_id, main, "
            "  created_by_id, date_created, archived"
            ") VALUES (%s, %s, %s, %s, %s, NOW(), false) RETURNING id;"
        )
        params = (entity_id, project_id, relationship_type_id, main, int(AGENT_USER_ID))

        if self.dry_run:
            return Result(ok=True, dry_run=True,
                          detail="DRY_RUN: would link entity → project",
                          data={"sql": sql, "params": params})
        try:
            conn = self.connect()
            with conn.cursor() as cur:
                cur.execute(sql, params)
                new_id = cur.fetchone()[0]
            conn.commit()
            return Result(ok=True, detail="entity linked to project",
                          data={"entity_project_relationship_id": new_id})
        except Exception as e:
            self.connect().rollback()
            return Result(ok=False, detail=f"link_entity_project failed: {e}")

    def find_link(self, entity_id: int, project_id: int) -> Result:
        """Does an entity_project_relationship already exist for this pair?"""
        sql = (
            "SELECT id, relationship_type_id, main FROM entity_project_relationship "
            "WHERE entity_id = %s AND project_id = %s AND archived = false;"
        )
        try:
            with self._cursor() as cur:
                cur.execute(sql, (entity_id, project_id))
                rows = cur.fetchall()
            return Result(ok=True, detail=f"{len(rows)} link(s)",
                          data={"matches": [dict(r) for r in rows]})
        except Exception as e:
            return Result(ok=False, detail=f"find_link failed: {e}")

    def ensure_entity_and_project(self, extracted: dict) -> Result:
        """Idempotent find-or-create for a Roofix proposal.

        Expects a dict shaped like ExtractedProposal.__dict__ (or the dataclass
        itself's dict form). Reads these fields:
            roofix_project_id, display_text, customer_name (or full_name),
            first_name, last_name, email, phone,
            street_address, city, state_text, state_abbr, zip_code

        Flow:
            1. find_entity_by_identity(name, email, street_address).
               0 → create_entity. 1 → use. >1 → escalate.
            2. find_project_by_roofix_id(roofix_project_id).
               0 → create_project + link_entity_project.
               1 → find_link; link only if not already linked. >1 → escalate.

        Returns Result.data:
            entity_id, phoenix_project_id, created_entity (bool),
            created_project (bool), created_link (bool)
        """
        roofix_id = extracted.get("roofix_project_id")
        if not roofix_id:
            return Result(ok=False, detail="extracted lacks roofix_project_id")

        # ── Entity ──────────────────────────────────────────────────────────
        first = extracted.get("first_name")
        last = extracted.get("last_name")
        name = extracted.get("full_name") or extracted.get("customer_name")
        if not name and (first or last):
            name = " ".join(p for p in (first, last) if p)

        ent_r = self.find_entity_by_identity(
            name=name,
            email=extracted.get("email"),
            street1=extracted.get("street_address"),
        )
        if not ent_r.ok:
            return ent_r
        ent_matches = ent_r.data.get("matches", [])
        if len(ent_matches) > 1:
            return Result(ok=False, detail=(
                f"ambiguous entity match ({len(ent_matches)} candidates) — "
                f"human review required for name={name!r} email={extracted.get('email')!r}"),
                data={"entity_matches": ent_matches})

        created_entity = False
        if len(ent_matches) == 1:
            entity_id = ent_matches[0]["id"]
        else:
            state_id = self.resolve_state_id(
                state_text=extracted.get("state_text"),
                state_abbr=extracted.get("state_abbr"),
            )
            cr = self.create_entity(
                first_name=first,
                last_name=last,
                email=extracted.get("email"),
                phone=extracted.get("phone"),
                street1=extracted.get("street_address"),
                city=extracted.get("city"),
                postal_code=extracted.get("zip_code"),
                state_id=state_id,
                migration_external_id=extracted.get("homeowner_ref"),
            )
            if not cr.ok:
                return cr
            entity_id = cr.data.get("entity_id")
            created_entity = True

        # ── Project ─────────────────────────────────────────────────────────
        proj_r = self.find_project_by_roofix_id(roofix_id)
        if not proj_r.ok:
            return proj_r
        proj_matches = proj_r.data.get("matches", [])
        if len(proj_matches) > 1:
            return Result(ok=False, detail=(
                f"ambiguous project match on roofix_id={roofix_id!r} "
                f"({len(proj_matches)} candidates) — human review required"),
                data={"project_matches": proj_matches})

        created_project = False
        if len(proj_matches) == 1:
            phoenix_project_id = proj_matches[0]["id"]
        else:
            state_id = self.resolve_state_id(
                state_text=extracted.get("state_text"),
                state_abbr=extracted.get("state_abbr"),
            )
            project_name = (extracted.get("display_text")
                            or name
                            or f"Roofix project {roofix_id}")
            cr = self.create_project(
                project_name=project_name,
                street1=extracted.get("street_address"),
                city=extracted.get("city"),
                postal_code=extracted.get("zip_code"),
                state_id=state_id,
                migration_external_id=roofix_id,
                migration_entity_id=extracted.get("homeowner_ref"),
            )
            if not cr.ok:
                return cr
            phoenix_project_id = cr.data.get("project_id")
            created_project = True

        # ── Link ────────────────────────────────────────────────────────────
        # In DRY_RUN, the ids above are None (writes never executed), so we
        # can't sensibly call find_link. Skip the pre-check and just record
        # what the link write WOULD look like.
        created_link = False
        if self.dry_run and (created_entity or created_project):
            lk = self.link_entity_project(entity_id or 0, phoenix_project_id or 0)
            if not lk.ok:
                return lk
            created_link = True
        elif not self.dry_run:
            existing = self.find_link(entity_id, phoenix_project_id)
            if not existing.ok:
                return existing
            if not existing.data.get("matches"):
                lk = self.link_entity_project(entity_id, phoenix_project_id)
                if not lk.ok:
                    return lk
                created_link = True

        return Result(
            ok=True,
            dry_run=self.dry_run,
            detail=(f"{'DRY_RUN: ' if self.dry_run else ''}"
                    f"entity={'CREATED' if created_entity else 'FOUND'} "
                    f"project={'CREATED' if created_project else 'FOUND'} "
                    f"link={'CREATED' if created_link else 'EXISTED'}"),
            data={
                "entity_id": entity_id,
                "phoenix_project_id": phoenix_project_id,
                "created_entity": created_entity,
                "created_project": created_project,
                "created_link": created_link,
            },
        )


# --- standalone connection test -------------------------------------------------
# Run on the server after filling .env:  python components/phoenix_client.py
if __name__ == "__main__":
    client = PhoenixClient()
    print("DRY_RUN:", client.dry_run)
    r = client.ping()
    print("ping:", r.ok, "-", r.detail)
    client.close()
