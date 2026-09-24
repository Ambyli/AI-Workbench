"""CRM adapter — fuzzy-but-high-confidence account matching + case disposition.

The disposition pipeline (backend/pipeline.py) asks two things of the CRM:

    1. find_account(name, address)  -> a confident CRMMatch, or None
    2. open_case(match, payload)    -> a CaseResult (case opened for disposition)

The CRM is **in-house and not yet documented**, so this module is a clean
provider-agnostic seam. A `CRMClient` Protocol defines the contract; concrete
backends are selected by `settings.CRM_BACKEND`:

    stub     -> StubCRMClient: local placeholder over data/customers.json (default)
    mcp      -> MCPCRMClient:  calls the in-house CRM MCP server (NOT wired yet)
    inhouse  -> HTTPCRMClient: calls the in-house CRM REST API   (NOT wired yet)

When the in-house docs / MCP tool schemas arrive, filling in the two unwired
clients is mechanical — the rest of the app already depends only on the Protocol.

Security note: matching/authorization happens HERE in backend code, never in the
LLM. Only a match at/above the high-confidence threshold is ever returned, and
only the minimal routing subset of the record crosses the boundary.
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional, Protocol

from . import lookup
from .config import settings
from .schemas import CaseResult, CRMMatch

logger = logging.getLogger("chatbot.crm")


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------
class CRMClient(Protocol):
    """The surface every CRM backend must implement."""

    def find_account(
        self, name: Optional[str], address: Optional[str], email: Optional[str] = None
    ) -> Optional[CRMMatch]:
        """Return a confident match (>= threshold) for the requester, else None."""
        ...

    def open_case(self, match: CRMMatch, payload: dict) -> CaseResult:
        """Open a case for disposition against the matched account."""
        ...


# ---------------------------------------------------------------------------
# Stub backend (default) — local placeholder over the mock customer table.
# ---------------------------------------------------------------------------
class StubCRMClient:
    """Placeholder CRM backed by lookup.score_match + the mock customers.json.

    Mirrors the real contract so the pipeline can be developed and tested end to
    end before the in-house CRM exists. `open_case` fabricates a case id; nothing
    is actually persisted to a CRM.
    """

    def find_account(
        self, name: Optional[str], address: Optional[str], email: Optional[str] = None
    ) -> Optional[CRMMatch]:
        customer, confidence = lookup.score_match(name, address, email)
        threshold = settings.CRM_MATCH_THRESHOLD
        if customer is None or confidence < threshold:
            # Non-enumerating: we never reveal *which* field was close.
            logger.info("CRM(stub) no confident match (best=%.2f, need>=%.2f)", confidence, threshold)
            return None
        logger.info("CRM(stub) matched %s at confidence %.2f", customer["account_number"], confidence)
        return CRMMatch(
            account_number=customer["account_number"],
            service_address=customer["service_address"],
            system_size=customer["system_size"],
            install_date=customer["install_date"],
            confidence=round(confidence, 3),
        )

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
# MCP backend (NOT wired yet) — drop-in once the in-house MCP is documented.
# ---------------------------------------------------------------------------
class MCPCRMClient:
    """Calls the in-house CRM via its MCP server.

    Expected MCP tool surface (placeholder names — confirm against the in-house
    docs when they arrive):

        crm_lookup_account(name, address, email) -> { account, confidence }
        crm_open_case(account_number, issue_type, urgency, summary) -> { case_id, status }

    Wiring steps once docs land:
        1. Connect to the MCP server at settings.CRM_MCP_URL.
        2. Map find_account -> crm_lookup_account; enforce the high-confidence
           threshold HERE (never trust the model/tool to gate it).
        3. Map open_case -> crm_open_case.
    """

    def find_account(self, name, address, email=None) -> Optional[CRMMatch]:  # pragma: no cover - seam
        raise NotImplementedError(
            "CRM_BACKEND=mcp is not wired yet. Provide the in-house CRM MCP docs / "
            "tool schemas and set CRM_MCP_URL, then implement crm_lookup_account here."
        )

    def open_case(self, match: CRMMatch, payload: dict) -> CaseResult:  # pragma: no cover - seam
        raise NotImplementedError(
            "CRM_BACKEND=mcp is not wired yet. Implement crm_open_case against the in-house MCP."
        )


# ---------------------------------------------------------------------------
# Direct REST backend (NOT wired yet) — alternative to MCP.
# ---------------------------------------------------------------------------
class HTTPCRMClient:
    """Calls the in-house CRM REST API directly (settings.CRM_API_BASE_URL/_KEY).

    Same rule as MCP: enforce the confidence threshold in this code, return only
    the minimal CRMMatch subset. Implement once the API is documented.
    """

    def find_account(self, name, address, email=None) -> Optional[CRMMatch]:  # pragma: no cover - seam
        raise NotImplementedError(
            "CRM_BACKEND=inhouse is not wired yet. Set CRM_API_BASE_URL / CRM_API_KEY "
            "and implement the account-search call here."
        )

    def open_case(self, match: CRMMatch, payload: dict) -> CaseResult:  # pragma: no cover - seam
        raise NotImplementedError(
            "CRM_BACKEND=inhouse is not wired yet. Implement the case-creation call here."
        )


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------
_BACKENDS = {
    "stub": StubCRMClient,
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


# Convenience wrappers used by the pipeline -------------------------------------
def find_account(
    name: Optional[str], address: Optional[str], email: Optional[str] = None
) -> Optional[CRMMatch]:
    return get_client().find_account(name, address, email)


def open_case(match: CRMMatch, payload: dict) -> CaseResult:
    return get_client().open_case(match, payload)
