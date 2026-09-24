"""Render the validated case into a handoff email and store it for the Inbox.

extract -> validate -> render. The LLM does the extraction (upstream, into the
case dict); THIS module owns layout, wording, and encoding. Jinja2 autoescape is
on, so every dynamic field is HTML-escaped -- the LLM cannot inject markup.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import settings
from .schemas import AccountMatch, EmailRecord, IssueType, Mode

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_EMAILS_FILE = Path(__file__).parent / "data" / "emails.json"

_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=select_autoescape(["html", "j2", "html.j2"]),
)
_template = _env.get_template("handoff_email.html.j2")

_ISSUE_LABELS = {
    IssueType.ROOF: "Roof",
    IssueType.ELECTRICAL: "Electrical",
    IssueType.SOLAR: "Solar Production",
    IssueType.MISC: "Miscellaneous Damage",
}

# Friendly labels for fields that could not be validated during intake.
_FIELD_LABELS = {
    "name": "Your name",
    "account_name": "Solar account name",
    "account_address": "Solar account address",
    "contact": "Best contact",
}


# ---------------------------------------------------------------------------
# Email store (prototype: in-memory list mirrored to a JSON file)
# ---------------------------------------------------------------------------
def _load_store() -> list[dict]:
    if _EMAILS_FILE.exists():
        try:
            return json.loads(_EMAILS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
    return []


_STORE: list[dict] = _load_store()


def _persist() -> None:
    try:
        _EMAILS_FILE.write_text(json.dumps(_STORE, indent=2), encoding="utf-8")
    except OSError:
        pass  # prototype: persistence is best-effort


def list_emails() -> list[dict]:
    """Newest-first summaries for the inbox list view."""
    keys = (
        "id", "created_at", "subject", "issue_type", "issue_label", "urgency",
        "mode_label", "customer_name", "matched", "tier_label", "tier_color", "summary",
    )
    return [{k: e.get(k) for k in keys} for e in reversed(_STORE)]


def is_empty() -> bool:
    return len(_STORE) == 0


def get_email(email_id: str) -> Optional[dict]:
    return next((e for e in _STORE if e["id"] == email_id), None)


# ---------------------------------------------------------------------------
# Urgency tiering
# ---------------------------------------------------------------------------
def _tier(urgency: int, issue_type: IssueType) -> tuple[str, str]:
    """Map 1-10 urgency to a (label, color). Electrical floors at Elevated."""
    if issue_type == IssueType.ELECTRICAL and urgency < 6:
        urgency = max(urgency, 6)
    if urgency >= 8:
        return "HIGH PRIORITY", "#c0262b"
    if urgency >= 5:
        return "Elevated", "#e08a1e"
    return "Standard", "#3a8d5b"


# ---------------------------------------------------------------------------
# Per-issue fact extraction (deterministic mapping case -> labeled rows)
# ---------------------------------------------------------------------------
def _yn(v: Any) -> str:
    if v is True:
        return "Yes"
    if v is False:
        return "No"
    return "[Not provided]"


def _facts(case: dict) -> list[tuple[str, str]]:
    it = IssueType(case["issue_type"])
    g = lambda k: case.get(k) or "[Not provided]"
    if it == IssueType.ROOF:
        return [
            ("Roof type", g("roof_type")),
            ("Actively leaking?", _yn(case.get("leaking"))),
            ("First noticed", g("first_noticed")),
            ("Pre-existing damage?", _yn(case.get("pre_existing"))),
            ("Attic accessible?", _yn(case.get("attic_accessible"))),
            ("Location of problem", case.get("damage_location_text") or g("location")),
        ]
    if it == IssueType.ELECTRICAL:
        return [
            ("Currently without power?", _yn(case.get("without_power"))),
            ("Breakers tripped?", _yn(case.get("breakers_tripped"))),
            ("Neighbors without power?", _yn(case.get("neighbors_without_power"))),
            ("Recent storm/lightning?", _yn(case.get("recent_storm"))),
            ("Affected areas", g("affected_areas")),
        ]
    if it == IssueType.SOLAR:
        return [
            ("System status", g("solar_status")),
            ("Inverter error code", g("inverter_error_code")),
            ("Callback requested?", _yn(case.get("callback_requested"))),
            ("Callback info", g("callback_info")),
            ("Note", "Deflected: production issues typically need ~1 year of data."),
        ]
    # MISC
    return [
        ("What is damaged", g("what_damaged")),
        ("Cause of damage", g("cause_of_damage")),
        ("Location of problem", case.get("damage_location_text") or g("location")),
        ("Additional details", g("additional_details")),
    ]


def _safety_flag(case: dict) -> str:
    it = IssueType(case["issue_type"])
    if it == IssueType.ELECTRICAL and case.get("without_power"):
        return "SAFETY: electrical, no power"
    if it == IssueType.ROOF and case.get("leaking"):
        return "Active leak"
    return ""


def _summary(case: dict, match: Optional[AccountMatch]) -> str:
    """A grounded one/two-liner built from validated fields (not free LLM prose)."""
    it = IssueType(case["issue_type"])
    name = case.get("name") or "An unidentified customer"
    label = _ISSUE_LABELS[it]
    bits = [f"{name} reported a {label.lower()} issue (urgency {case.get('urgency', 1)}/10)."]
    if it == IssueType.ROOF and case.get("leaking"):
        bits.append("Roof is actively leaking.")
    if it == IssueType.ELECTRICAL and case.get("without_power"):
        bits.append("Property is currently without power.")
    if it == IssueType.SOLAR:
        bits.append("Production concern — deflected to the performance team per policy.")
    if match:
        bits.append(f"Verified against account {match.account_number}.")
    return " ".join(bits)


# ---------------------------------------------------------------------------
# Public: render + store
# ---------------------------------------------------------------------------
def render_and_store(
    case: dict,
    match: Optional[AccountMatch] = None,
    created_at: Optional[str] = None,
    *,
    to: Optional[str] = None,
    case_id: Optional[str] = None,
    match_confidence: float = 0.0,
) -> EmailRecord:
    """Render the validated case to a handoff email and store it for the Inbox.

    Routing metadata (`to`, `case_id`, `match_confidence`) is supplied by the
    disposition pipeline (backend/pipeline.py). When omitted (e.g. seed data) the
    address falls back to the configured disposition mailbox, preserving prior
    behavior.
    """
    it = IssueType(case["issue_type"])
    urgency = int(case.get("urgency") or 1)
    mode = Mode(case.get("mode", Mode.STANDARD))
    tier_label, tier_color = _tier(urgency, it)
    issue_label = _ISSUE_LABELS[it]
    mode_label = "Account Lookup" if mode == Mode.LOOKUP else "Standard"
    summary = _summary(case, match)
    to_address = to or settings.MAILBOX_DISPOSITION

    # Requester (who contacted us) vs. Solar Account fields (what we matched on).
    # Fall back to the legacy name/address/email keys for seeded/older cases.
    requester_name = case.get("name")
    contact = case.get("contact") or case.get("email")
    account_name = case.get("account_name") or case.get("name")
    account_address = case.get("account_address") or case.get("address")
    unconfirmed = [_FIELD_LABELS.get(k, k) for k in case.get("_unconfirmed", [])]

    html = _template.render(
        mode_label=mode_label,
        issue_label=issue_label,
        tier_label=tier_label,
        tier_color=tier_color,
        urgency=urgency,
        safety_flag=_safety_flag(case),
        match=match,
        requester_name=requester_name,
        contact=contact,
        account_name=account_name,
        account_address=account_address,
        third_parties=case.get("third_parties"),
        facts=_facts(case),
        attachments=case.get("attachments", []),
        summary=summary,
        verbatim=[v for v in case.get("verbatim", []) if v and v.strip()],
        unconfirmed=unconfirmed,
        damage_pointer=case.get("damage_pointer"),
    ).strip()

    record = EmailRecord(
        id=uuid.uuid4().hex[:12],
        created_at=created_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        subject=f"[{tier_label}] {issue_label} — {case.get('name') or 'Unknown requester'}",
        issue_type=it,
        issue_label=issue_label,
        urgency=urgency,
        mode=mode,
        mode_label=mode_label,
        to=to_address,
        html=html,
        customer_name=case.get("name") or "Unknown",
        matched=match is not None,
        tier_label=tier_label,
        tier_color=tier_color,
        summary=summary,
        routed_to=to_address,
        match_confidence=round(match_confidence, 3),
        case_id=case_id,
    )
    _STORE.append(record.model_dump())
    _persist()
    return record
