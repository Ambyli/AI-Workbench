"""CRM adapter — fuzzy account search + case disposition.

The account check (backend/account_match.py) and the disposition pipeline
(backend/pipeline.py) ask three things of the CRM:

    1. search_by_address(street, city, zip) -> up to 5 AccountCandidates (0-100 score)
    2. search_by_name(name, city)           -> up to 5 AccountCandidates (0-100 score)
    3. open_case(match, payload)            -> a CaseResult (case opened for disposition)

Clients only SEARCH. Whether a candidate is "the" account is decided by the gates in
backend/account_match.py, never by a client and never by the LLM.

The CRM is **in-house**, so this module is a provider-agnostic seam. A `CRMClient`
Protocol defines the contract; concrete backends are selected by `settings.CRM_BACKEND`:

    stub     -> StubCRMClient:    local placeholder over data/customers.json (default)
    phoenix  -> PhoenixSQLClient: the fuzzy queries on a read-only phoenix connection
    mcp      -> MCPCRMClient:     calls the in-house CRM MCP server (NOT wired yet)
    inhouse  -> HTTPCRMClient:    calls the in-house CRM REST API   (NOT wired yet)

Security note: this module never imports the LLM client, and nothing it returns is
ever given to the model or shown to the customer (tests/account_match_test.py).
"""
from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Protocol

from .config import settings
from .schemas import AccountCandidate, CaseResult, CRMMatch
from .trgm import similarity

logger = logging.getLogger("chatbot.crm")


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------
class CRMClient(Protocol):
    """The surface every CRM backend must implement."""

    def search_by_address(self, street: str, city: str, zip5: str) -> list[AccountCandidate]:
        """Top candidates for the service address (0-100 fuzzy_score, best first)."""
        ...

    def search_by_name(self, name: str, city: str) -> list[AccountCandidate]:
        """Top candidates for the name on the account (0-100 fuzzy_score, best first)."""
        ...

    def open_case(self, match: CRMMatch, payload: dict) -> CaseResult:
        """Open a case for disposition against the matched account."""
        ...


# ---------------------------------------------------------------------------
# Stub backend (default) — local placeholder over the mock customer table.
# ---------------------------------------------------------------------------
MOCK_PROJECTS_FILE = Path(__file__).parent / "fixtures" / "mock_projects.json"


def load_mock_projects() -> dict:
    return json.loads(MOCK_PROJECTS_FILE.read_text(encoding="utf-8"))


class StubCRMClient:
    """Placeholder CRM over the SYNTHETIC phoenix-shaped rows in
    backend/fixtures/mock_projects.json, scored exactly like the phoenix queries
    (backend/sql/) using a Python pg_trgm equivalent. `open_case` fabricates a case
    id; nothing is persisted to a real CRM.
    """

    _ROWS = [AccountCandidate(**p) for p in load_mock_projects()["projects"]]

    @classmethod
    def _rows(cls) -> list[AccountCandidate]:
        return cls._ROWS

    def search_by_address(self, street: str, city: str, zip5: str) -> list[AccountCandidate]:
        """Same scoring and filter as backend/sql/account_address_search.sql."""
        out = []
        for r in self._rows():
            street_sim = max(similarity(r.street1, street), similarity(r.street2, street))
            city_sim = similarity(r.city, city)
            zip_hit = bool(zip5) and r.postal_code.lower().startswith(zip5.lower())
            if street_sim > 0.2 or city_sim > 0.3 or zip_hit:
                score = round(100 * (0.6 * street_sim + 0.3 * city_sim + 0.1 * zip_hit), 1)
                out.append(r.model_copy(update={"fuzzy_score": score}))
        return sorted(out, key=lambda r: r.fuzzy_score, reverse=True)[:5]

    def search_by_name(self, name: str, city: str) -> list[AccountCandidate]:
        """Same scoring and filter as backend/sql/account_name_search.sql."""
        out = []
        for r in self._rows():
            name_sim = similarity(r.project_name, name)
            if name_sim > 0.2:
                score = round(100 * (0.7 * name_sim + 0.3 * similarity(r.city, city)), 1)
                out.append(r.model_copy(update={"fuzzy_score": score}))
        return sorted(out, key=lambda r: r.fuzzy_score, reverse=True)[:5]

    def open_case(self, match: CRMMatch, payload: dict) -> CaseResult:
        case_id = f"CASE-{uuid.uuid4().hex[:8].upper()}"
        logger.info(
            "CRM(stub) opened %s for account %s (issue=%s, urgency=%s)",
            case_id, match.account_number, payload.get("issue_type"), payload.get("urgency"),
        )
        from . import crm_store
        crm_store.record_case({
            "case_id": case_id,
            "account_number": match.account_number,
            "customer_name": payload.get("requester_name") or payload.get("account_name", "Customer"),
            "service_address": match.service_address,
            "contact": payload.get("contact", "Not provided"),
            "issue_type": payload.get("issue_type", "misc"),
            "urgency": payload.get("urgency", 5),
            "summary": payload.get("summary", ""),
            "status": "pending_disposition",
            "routed_to": payload.get("routed_to", "service-team@zeoenergy.com"),
            "attachments": payload.get("attachments", []),
            "damage_pointer": payload.get("damage_pointer"),
            "verbatim": payload.get("verbatim", []),
        })
        return CaseResult(case_id=case_id, status="pending_disposition", backend="stub")


# ---------------------------------------------------------------------------
# Phoenix backend — the fuzzy queries on a read-only database connection.
# ---------------------------------------------------------------------------
_SQL_DIR = Path(__file__).parent / "sql"


class PhoenixSQLClient:
    """Runs backend/sql/account_*_search.sql against phoenix (PostgreSQL + pg_trgm).

    Requirements before enabling (CRM_BACKEND=phoenix):
      * CRM_DB_URL for a READ-ONLY role (SELECT on phoenix.project / phoenix.state only).
      * `pip install "psycopg[binary]"` (imported lazily so other backends don't need it).
    Every value is a bound parameter; the SQL text is fixed at import. Each call runs in
    a read-only transaction with a statement timeout. Not yet verified against the real
    database (access pending).
    """

    _ADDRESS_SQL = (_SQL_DIR / "account_address_search.sql").read_text(encoding="utf-8")
    _NAME_SQL = (_SQL_DIR / "account_name_search.sql").read_text(encoding="utf-8")

    def __init__(self, connect=None):
        self._connect = connect  # injectable for tests

    def _connection(self):
        if self._connect is not None:
            return self._connect()
        if not settings.CRM_DB_URL:
            raise RuntimeError("CRM_BACKEND=phoenix but CRM_DB_URL is not set")
        import psycopg  # noqa: PLC0415 - optional dependency
        return psycopg.connect(settings.CRM_DB_URL,
                               connect_timeout=max(1, int(settings.CRM_QUERY_TIMEOUT_SECONDS)))

    def _query(self, sql: str, params: dict) -> list[AccountCandidate]:
        timeout_ms = int(settings.CRM_QUERY_TIMEOUT_SECONDS * 1000)
        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SET TRANSACTION READ ONLY")
                cur.execute(f"SET LOCAL statement_timeout = {timeout_ms}")  # int from config, not input
                cur.execute(sql, params)
                cols = [d[0] for d in cur.description]
                rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        return [
            AccountCandidate(
                **{k: ("" if v is None else str(v)) for k, v in row.items() if k != "fuzzy_score"},
                fuzzy_score=float(row.get("fuzzy_score") or 0),
            )
            for row in rows
        ]

    def search_by_address(self, street: str, city: str, zip5: str) -> list[AccountCandidate]:
        return self._query(self._ADDRESS_SQL, {"street": street, "city": city, "zip": zip5})

    def search_by_name(self, name: str, city: str) -> list[AccountCandidate]:
        return self._query(self._NAME_SQL, {"name": name, "city": city})

    def open_case(self, match: CRMMatch, payload: dict) -> CaseResult:  # pragma: no cover - seam
        raise NotImplementedError("Case creation in phoenix is not defined yet (read-only access).")


# ---------------------------------------------------------------------------
# MCP backend (NOT wired yet) — drop-in once the in-house MCP is documented.
# ---------------------------------------------------------------------------
class MCPCRMClient:
    """Calls the in-house CRM via its MCP server.

    Expected MCP tool surface (placeholder names — confirm against the in-house
    docs when they arrive):

        crm_search_address(street, city, zip) / crm_search_name(name, city) -> candidates
        crm_open_case(account_number, issue_type, urgency, summary) -> { case_id, status }

    The MCP client is backend code only; the match gates stay in account_match.py and
    the LLM is never given these tools.
    """

    def search_by_address(self, street, city, zip5) -> list[AccountCandidate]:  # pragma: no cover - seam
        raise NotImplementedError("CRM_BACKEND=mcp is not wired yet.")

    def search_by_name(self, name, city) -> list[AccountCandidate]:  # pragma: no cover - seam
        raise NotImplementedError("CRM_BACKEND=mcp is not wired yet.")

    def open_case(self, match: CRMMatch, payload: dict) -> CaseResult:  # pragma: no cover - seam
        raise NotImplementedError(
            "CRM_BACKEND=mcp is not wired yet. Implement crm_open_case against the in-house MCP."
        )


# ---------------------------------------------------------------------------
# Direct REST backend (NOT wired yet) — alternative to MCP.
# ---------------------------------------------------------------------------
class HTTPCRMClient:
    """Calls the in-house CRM REST API directly (settings.CRM_API_BASE_URL/_KEY)."""

    def search_by_address(self, street, city, zip5) -> list[AccountCandidate]:  # pragma: no cover - seam
        raise NotImplementedError("CRM_BACKEND=inhouse is not wired yet.")

    def search_by_name(self, name, city) -> list[AccountCandidate]:  # pragma: no cover - seam
        raise NotImplementedError("CRM_BACKEND=inhouse is not wired yet.")

    def open_case(self, match: CRMMatch, payload: dict) -> CaseResult:  # pragma: no cover - seam
        raise NotImplementedError(
            "CRM_BACKEND=inhouse is not wired yet. Implement the case-creation call here."
        )


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------
_BACKENDS = {
    "stub": StubCRMClient,
    "phoenix": PhoenixSQLClient,
    "mcp": MCPCRMClient,
    "inhouse": HTTPCRMClient,
}


def get_client() -> CRMClient:
    """Return the configured CRM client (defaults to the stub)."""
    backend = settings.CRM_BACKEND
    cls = _BACKENDS.get(backend)
    if cls is None:
        logger.warning("Unknown CRM_BACKEND=%r — falling back to stub.", backend)
        cls = StubCRMClient
    return cls()


def open_case(match: CRMMatch, payload: dict) -> CaseResult:
    return get_client().open_case(match, payload)
