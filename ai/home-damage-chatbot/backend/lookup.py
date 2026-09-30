"""Account-matching primitives + the Database-tab demo.

Cardinal rule from the design: the LLM never owns the lookup decision. The state
machine extracts candidate identifiers (name / address / email); THIS backend
code authorizes the match and decides what minimal data to return.

This module owns the low-level fuzzy matching and a single **confidence score**
(`score_match`) over the mock "database" (a JSON file). Policy on top of that
score — the high-confidence threshold, opening a CRM case, choosing a backend —
lives in `backend/crm.py`. Matching is deliberately strict (multi-factor) and the
returned payload is minimal (never the full record), mirroring how a real
least-privilege lookup service behaves.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from .schemas import AccountMatch, LookupResult

_DATA_FILE = Path(__file__).parent / "data" / "customers.json"


def _load() -> list[dict]:
    with _DATA_FILE.open(encoding="utf-8") as fh:
        return json.load(fh)


_CUSTOMERS = _load()


def _norm(s: Optional[str]) -> str:
    """Lowercase, collapse whitespace, drop punctuation for forgiving compares."""
    if not s:
        return ""
    s = s.lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _street_number(addr: str) -> str:
    m = re.match(r"\s*(\d+)", addr or "")
    return m.group(1) if m else ""


def _name_matches(query: str, record: str) -> bool:
    q, r = _norm(query), _norm(record)
    if not q:
        return False
    # Require last name + at least the first initial to match, order-independent.
    q_parts = q.split()
    if len(q_parts) < 2:
        return False
    r_parts = set(r.split())
    return all(any(rp == qp or rp.startswith(qp[:3]) for rp in r_parts) for qp in q_parts)


def _address_matches(query: str, record: str) -> bool:
    if not query:
        return False
    # Street number must match AND a meaningful street-name token must overlap.
    if _street_number(query) and _street_number(query) != _street_number(record):
        return False
    q_tokens = {t for t in _norm(query).split() if len(t) > 2 and not t.isdigit()}
    r_tokens = {t for t in _norm(record).split() if len(t) > 2 and not t.isdigit()}
    return bool(q_tokens & r_tokens) and _street_number(query) == _street_number(record)


def _email_matches(query: str, record: str) -> bool:
    return bool(query) and _norm(query) == _norm(record)


# ---------------------------------------------------------------------------
# Confidence scoring — the single source of truth for "how sure are we?"
# ---------------------------------------------------------------------------
# Weights are tuned so name alone can never clear a high-confidence threshold
# (anti-enumeration), but name + a second factor lands comfortably above 0.9.
_W_NAME = 0.5
_W_ADDRESS = 0.45
_W_EMAIL = 0.45


def score_match(
    name: Optional[str],
    address: Optional[str] = None,
    email: Optional[str] = None,
) -> tuple[Optional[dict], float]:
    """Return (best_customer, confidence in 0..1) over the mock table.

    Confidence is 0 unless the name matches (name is a hard precondition). With a
    matching name, each corroborating factor (address, email) adds weight, so a
    name+address match scores ~0.95 while a name-only match caps at ~0.5.
    Returns (None, 0.0) when nothing matches the name at all.
    """
    if not name:
        return None, 0.0

    best: Optional[dict] = None
    best_score = 0.0
    for c in _CUSTOMERS:
        if not _name_matches(name, c["name"]):
            continue
        score = _W_NAME
        if _address_matches(address or "", c["service_address"]):
            score += _W_ADDRESS
        if _email_matches(email or "", c["email"]):
            score += _W_EMAIL
        score = min(score, 1.0)
        if score > best_score:
            best, best_score = c, score
    return best, best_score


def lookup(
    name: Optional[str],
    address: Optional[str] = None,
    email: Optional[str] = None,
    *,
    threshold: float = 0.9,
) -> Optional[AccountMatch]:
    """Return a minimal AccountMatch only on a confident multi-factor match.

    Requires name + a second factor (address or email) to clear `threshold`.
    Single-field matches score too low and are rejected to prevent enumeration.
    Returns None when nothing matches confidently.
    """
    customer, score = score_match(name, address, email)
    if customer is None or score < threshold:
        return None
    return AccountMatch(
        account_number=customer["account_number"],
        service_address=customer["service_address"],
        system_size=customer["system_size"],
        install_date=customer["install_date"],
    )


def list_customers() -> list[dict]:
    """Admin/demo view of the mock table — phone deliberately withheld."""
    return [
        {
            "account_number": c["account_number"],
            "name": c["name"],
            "service_address": c["service_address"],
            "email": c["email"],
            "system_size": c["system_size"],
        }
        for c in _CUSTOMERS
    ]


def lookup_demo(name: str, address: str, email: str) -> LookupResult:
    """Power the Database-tab 'Try a lookup' panel: enforce the multi-factor
    rule, then run the real lookup() and describe the (non-enumerating) result.
    """
    if not name.strip():
        return LookupResult(
            ok=False, kind="need", title="Name required",
            body="Provide a name plus an address or email — name alone is never enough to match.",
        )
    if not address.strip() and not email.strip():
        return LookupResult(
            ok=False, kind="need", title="Second factor required",
            body="A name alone won't match. Add an address or email so the service can confirm identity.",
        )
    match = lookup(name, address, email)
    if match is not None:
        return LookupResult(
            ok=True, kind="match", title=f"Match — {match.account_number}",
            body=(f"Returned to the case: {match.service_address} · {match.system_size} · "
                  f"installed {match.install_date}. (Phone and full record withheld.)"),
        )
    return LookupResult(
        ok=False, kind="nomatch", title="No match",
        body=("No account matched confidently. Logged as an unverified request — the chatbot "
              "never reveals which fields were close, to prevent enumeration."),
    )
