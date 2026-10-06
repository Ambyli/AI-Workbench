"""Account check — code-only and model-blind.

After the customer gives the address and the name on the account, this module
searches the CRM twice (address, then name), applies fixed gates, and returns a
MatchOutcome. It is the model-blindness layer:

  * It never imports the LLM client, and the LLM client never imports it, the CRM
    adapter or the SQL (tests/account_match_test.py enforces both directions).
  * Query inputs are the customer's own typed text, parsed by code
    (backend/address_parse.py). No model output is ever used as a query parameter,
    so a prompt injection cannot steer a search.
  * It runs only inside the account service (backend/account_service.py). The
    chatbot learns found / not found plus an opaque ref (backend/account_gateway.py);
    the details are appended to the handoff by the account service, at submit.

Gates for a match (all required, in code):
  1. Address query score >= CRM_ADDRESS_MIN_SCORE.
  2. The house number is exactly the one the customer typed (street1 or street2).
  3. Street-name similarity (house number removed, suffixes normalized)
     >= CRM_STREET_MIN_SIMILARITY, so "100 Oak St" never matches "100 Main St".
  4. The same project is in the name query results with score >= CRM_NAME_MIN_SCORE.
  5. Not ambiguous: if two different homes both pass within CRM_MATCH_MARGIN points,
     nobody is matched and staff resolve it.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from . import address_parse, crm
from .config import settings
from .schemas import AccountCandidate, CRMMatch

logger = logging.getLogger("chatbot.account_match")

_MAX_INPUT = 200



@dataclass
class MatchOutcome:
    matched: bool
    reason: str                                  # matched | no_address | no_candidates | below_threshold | ambiguous | error
    match: Optional[CRMMatch] = None
    candidates: list[dict] = field(default_factory=list)  # staff-only hints (project id + scores)

    def to_private(self) -> dict:
        """Server-side record stored on the case (`case["_acct"]`)."""
        return {
            "matched": self.matched,
            "reason": self.reason,
            "match": self.match.model_dump() if self.match else None,
            "candidates": self.candidates,
        }

    @staticmethod
    def from_private(d: dict) -> "MatchOutcome":
        return MatchOutcome(
            matched=bool(d.get("matched")),
            reason=d.get("reason", ""),
            match=CRMMatch(**d["match"]) if d.get("match") else None,
            candidates=list(d.get("candidates") or []),
        )


def _clean(text: Optional[str]) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()[:_MAX_INPUT]


def _home_key(c: AccountCandidate) -> str:
    return f"{house_or_street(c)}|{c.postal_code[:5]}".lower()


def house_or_street(c: AccountCandidate) -> str:
    return re.sub(r"\s+", " ", (c.street1 or c.street2 or "").lower()).strip()


def _address_line(c: AccountCandidate) -> str:
    street = ", ".join(x for x in (c.street1, c.street2) if x)
    tail = " ".join(x for x in (c.state, c.postal_code) if x)
    return ", ".join(x for x in (street, c.city, tail) if x)


def run(account_name_raw: Optional[str], address_raw: Optional[str],
        client: Optional[crm.CRMClient] = None) -> MatchOutcome:
    """Search by address, then by name, and apply the gates. Never raises: any CRM
    error is logged (without PII) and treated as not found, so staff locate it."""
    name = _clean(account_name_raw)
    parts = address_parse.parse(_clean(address_raw))
    if not name or not parts.number or not parts.street:
        return MatchOutcome(False, "no_address")

    client = client or crm.get_client()
    try:
        by_address = client.search_by_address(parts.street, parts.city, parts.zip)
        by_name = client.search_by_name(name, parts.city)
    except Exception as exc:  # noqa: BLE001 - the chat must continue
        logger.error("Account search failed (%s); treating as not found.", type(exc).__name__)
        return MatchOutcome(False, "error")

    if not by_address:
        return MatchOutcome(False, "no_candidates")

    name_scores = {c.project_id: c.fuzzy_score for c in by_name}
    hints = [{"project_id": c.project_id, "address_score": c.fuzzy_score,
              "name_score": name_scores.get(c.project_id, 0.0)} for c in by_address[:3]]

    eligible: list[tuple[float, AccountCandidate, float]] = []
    for c in by_address:
        if c.fuzzy_score < settings.CRM_ADDRESS_MIN_SCORE:
            continue
        streets = [s for s in (c.street1, c.street2) if s]
        if parts.number not in {address_parse.house_number(s) for s in streets}:
            continue
        if max((address_parse.street_similarity(parts.street, s) for s in streets), default=0.0) \
                < settings.CRM_STREET_MIN_SIMILARITY:
            continue
        name_score = name_scores.get(c.project_id)
        if name_score is None or name_score < settings.CRM_NAME_MIN_SCORE:
            continue
        eligible.append(((c.fuzzy_score + name_score) / 2, c, name_score))

    if not eligible:
        return MatchOutcome(False, "below_threshold", candidates=hints)

    eligible.sort(key=lambda t: t[0], reverse=True)
    top_score, top, _ = eligible[0]
    rivals = [t for t in eligible[1:] if _home_key(t[1]) != _home_key(top)
              and top_score - t[0] < settings.CRM_MATCH_MARGIN]
    if rivals:
        return MatchOutcome(False, "ambiguous", candidates=hints)

    match = CRMMatch(
        account_number=top.project_id,
        service_address=_address_line(top),
        confidence=round(top_score / 100, 3),
    )
    return MatchOutcome(True, "matched", match=match, candidates=hints)
