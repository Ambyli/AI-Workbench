"""Deterministic conversation state machine.

This is the brain AND the security boundary. It owns: which slot is active,
branching by issue type, the optional lookup step, the read-back/confirm, turn
limits, and when the email is rendered. The LLM is only ever asked to extract a
value for the single slot named below -- it cannot change the flow.

Session state is an in-memory dict keyed by session_id (prototype-grade).
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Callable, Optional

import time

from . import crm, llm, pipeline, validation
from .config import settings
from .schemas import BotMessage, IssueType, Mode
from .validation import validate_answer

# ---------------------------------------------------------------------------
# Session store (prototype: in-memory)
# ---------------------------------------------------------------------------
_SESSIONS: dict[str, dict] = {}

MAX_RETRIES = 2          # re-prompts on a required slot before we accept raw text
MAX_TURNS = 60           # unbounded-consumption guard


# ---------------------------------------------------------------------------
# Session hygiene: TTL expiry + hard cap on concurrent sessions (memory/DoS guard)
# ---------------------------------------------------------------------------
def _purge_expired(now: float) -> None:
    ttl = settings.SESSION_TTL_SECONDS
    stale = [sid for sid, c in _SESSIONS.items()
             if now - c.get("_last_activity", now) > ttl]
    for sid in stale:
        _SESSIONS.pop(sid, None)


def _enforce_cap() -> None:
    cap = settings.MAX_SESSIONS
    if len(_SESSIONS) <= cap:
        return
    # Evict the least-recently-active sessions down to the cap.
    ordered = sorted(_SESSIONS.items(), key=lambda kv: kv[1].get("_last_activity", 0.0))
    for sid, _ in ordered[: len(_SESSIONS) - cap]:
        _SESSIONS.pop(sid, None)


def _register(case: dict) -> None:
    now = time.time()
    case["_last_activity"] = now
    _SESSIONS[case["session_id"]] = case
    _purge_expired(now)
    _enforce_cap()

CONFIRM = "__confirm__"
CORRECT = "__correct__"
DONE = "__done__"
HUMAN = "__human__"      # awaiting the "which team?" answer during a human handoff


# ---------------------------------------------------------------------------
# Live-human handoff: department contacts (kept in one place so every handoff —
# representative request, turn-limit, etc. — is consistent and easy to update).
# Phone format matches the rest of the app (dash form) for easy copy/paste.
# ---------------------------------------------------------------------------
SERVICE_DEPT = {
    "name": "Service Department (Solar)",
    "phone": "727-349-4057",
    "email": "customercare@zeoenergy.com",
}
NONSTANDARD_DEPT = {
    "name": "Nonstandard Department (Electrical / Roof / Home damage)",
    "phone": "727-382-0075",
    "email": "nonstandard@zeoenergy.com",
}
SUPPORT_HOURS = "Monday–Friday, 9am–5pm Mountain Time"


# ---------------------------------------------------------------------------
# Step definitions
# ---------------------------------------------------------------------------
@dataclass
class Step:
    key: str
    field_type: str                       # text | yesno | int_1_10 | email | enum
    question: str
    options: Optional[list[str]] = None   # for enum
    quick_replies: Optional[list[str]] = None
    required: bool = False
    verbatim: bool = False                # also append the raw answer to the case verbatim block
    allow_upload: bool = False
    gate: Callable[[dict], bool] = lambda case: True  # include only if True


_YESNO = ["Yes", "No"]


def _common_steps() -> list[Step]:
    return [
        # Requester identity (who is contacting us — may differ from the account holder).
        Step("name", "text", "Hi! I'm here to help with your Zeo Energy service request. What is your name?", required=True),
        # Account-matching fields (matched against the CRM — phrased to pull the
        # exact name/address on the Solar Account, which boosts match accuracy).
        Step("account_name", "text", "What is the name associated with your Solar Account?", required=True),
        Step("account_address", "text", "What is the address associated with your Solar Account?", required=True),
        Step("contact", "text", "What is the best contact for you? (phone number or email)", required=True),
        Step(
            "issue_type", "enum",
            "What type of issue are you experiencing?",
            options=["roof", "electrical", "solar", "misc"],
            quick_replies=["Roof", "Electrical", "Solar not producing", "Misc / other"],
            required=True,
        ),
        Step("urgency", "int_1_10", "On a scale of 1–10, how urgent is this?", required=True,
             quick_replies=["3", "5", "8", "10"]),
        Step("third_parties", "text", "Have you contacted any third parties about this (insurance, another contractor)? (You can say 'skip'.)"),
    ]


def clean_address_for_geocoding(address: str) -> str:
    if not address:
        return ""
    import re
    # Strip common sub-unit identifiers and their values (e.g. "apt s302", "suite 100", "unit B", "#12")
    cleaned = address
    cleaned = re.sub(
        r"\b(apt|apartment|suite|ste|unit|room|rm|bldg|building|#)\.?\s*[a-zA-Z0-9_-]+\b",
        "",
        cleaned,
        flags=re.IGNORECASE
    )
    cleaned = re.sub(r"\b(apt|apartment|suite|ste|unit|room|rm|bldg|building|#)\b", "", cleaned, flags=re.IGNORECASE)
    # Expand single-letter directionals to improve Nominatim matching accuracy
    cleaned = re.sub(r"\bw\b\.?", "West", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\be\b\.?", "East", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bn\b\.?", "North", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bs\b\.?", "South", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = cleaned.strip(",. ")
    return cleaned


def geocode_address(address: str) -> Optional[tuple[float, float]]:
    if not address:
        return None
    cleaned = clean_address_for_geocoding(address)
    # Try tiered queries:
    # 1. Cleaned address (e.g., "1202 w 110 n pleasant grove")
    # 2. If it has commas, try without the last part (e.g., stripping country or ZIP code)
    queries = [cleaned]
    if "," in cleaned:
        parts = [p.strip() for p in cleaned.split(",")]
        if len(parts) > 2:
            queries.append(", ".join(parts[:-1]))
            
    import httpx
    import logging
    logger = logging.getLogger("chatbot.geocode")
    url = "https://nominatim.openstreetmap.org/search"
    headers = {
        "User-Agent": "HomeDamageChatbot/1.0 (contact: admin@zeoenergy.com)"
    }
    
    for q in queries:
        params = {
            "q": q,
            "format": "json",
            "limit": 1,
            "countrycodes": "us"
        }
        try:
            r = httpx.get(url, params=params, headers=headers, timeout=3.0)
            r.raise_for_status()
            data = r.json()
            if data:
                lat = float(data[0]["lat"])
                lon = float(data[0]["lon"])
                logger.info("Successfully geocoded %r to (%s, %s)", q, lat, lon)
                return lat, lon
        except Exception as exc:
            logger.warning("Geocoding failed for query %r: %s", q, exc)
    return None


def _branch_steps(case: dict) -> list[Step]:
    it = case.get("issue_type")
    if it == IssueType.ROOF.value:
        return [
            Step("roof_type", "enum", "What type of roof does your home have?",
                 options=["shingle", "tile", "metal", "flat"],
                 quick_replies=["Shingle", "Tile", "Metal", "Flat"]),
            Step("leaking", "yesno", "Is your roof actively leaking?", quick_replies=_YESNO, required=True),
            Step("first_noticed", "text", "When did you first notice the leak?",
                 gate=lambda c: c.get("leaking") is True),
            Step("pre_existing", "yesno", "Was there any pre-existing damage to your roof?", quick_replies=_YESNO),
            Step("attic_accessible", "yesno", "Is your attic accessible?", quick_replies=_YESNO),
            Step("damage_pointer", "pointer", "Please click on the house image below to drop a pointer indicating the damage location.", required=True),
            Step("description", "text", "Please describe the problem in your own words.", required=True, verbatim=True),
            Step("_photos", "text", "If you have photos of the roof damage, upload them now — or type 'skip'.",
                 allow_upload=True, quick_replies=["Skip"]),
        ]
    if it == IssueType.ELECTRICAL.value:
        return [
            Step("without_power", "yesno", "Are you currently without power?", quick_replies=_YESNO, required=True),
            Step("breakers_tripped", "yesno", "Have any circuit breakers in your electrical box tripped? (Check your electrical panel box to see if any switches are flipped to OFF or stuck in the middle between ON and OFF, sometimes showing red or orange.)", quick_replies=_YESNO),
            Step("neighbors_without_power", "yesno", "Are you aware of any other houses around you being without power?", quick_replies=_YESNO),
            Step("recent_storm", "yesno", "Was there a recent storm or lightning strike in your area?", quick_replies=_YESNO),
            Step("affected_areas", "text", "Which areas of the home are affected?"),
            Step("description", "text", "Please describe the electrical problem in your own words.", required=True, verbatim=True),
            Step("_photos", "text", "If you have photos related to the electrical issue, upload them now — or type 'skip'.",
                 allow_upload=True, quick_replies=["Skip"]),
        ]
    if it == IssueType.SOLAR.value:
        return [
            Step("solar_status", "enum", "Is the solar system completely offline, producing less than usual, or physically damaged?",
                 options=["completely_off", "low_production", "physical_damage"],
                 quick_replies=["Completely offline", "Low production", "Physical damage"]),
            Step("inverter_error_code", "text", "Do you see any error code or warning light on your solar inverter? (Type 'none' or 'skip' if unsure.)"),
            Step("description", "text", "Please describe what you're seeing with your system's production.", required=True, verbatim=True),
            Step("callback_requested", "yesno", "Would you like a callback from our performance team?", quick_replies=_YESNO),
            Step("callback_info", "text", "What's the best number and time to reach you?",
                 gate=lambda c: c.get("callback_requested") is True),
            Step("_photos", "text", "If you have photos of the solar inverter or panels, upload them now — or type 'skip'.",
                 allow_upload=True, quick_replies=["Skip"]),
        ]
    if it == IssueType.MISC.value:
        return [
            Step("what_damaged", "text", "What is damaged?", required=True, verbatim=True),
            Step("cause_of_damage", "text", "What caused the damage? (e.g. wind, fallen tree, vandalism)"),
            Step("damage_pointer", "pointer", "Please click on the house image below to drop a pointer indicating the damage location.", required=True),
            Step("additional_details", "text", "Any additional details you'd like to add? (You can say 'skip'.)", verbatim=True),
            Step("_photos", "text", "If you have photos of the damage, upload them now — or type 'skip'.",
                 allow_upload=True, quick_replies=["Skip"]),
        ]
    return []


def _active_steps(case: dict) -> list[Step]:
    steps = _common_steps() + _branch_steps(case)
    return [s for s in steps if s.gate(case)]


def _step_by_key(case: dict, key: str) -> Optional[Step]:
    return next((s for s in _active_steps(case) if s.key == key), None)


# Informational (non-question) message shown right before the solar branch.
_SOLAR_DEFLECTION = (
    "It can take time for a solar system's output to match your usage — we "
    "usually need about a year of production history to be sure there's a real "
    "issue. The fastest path is our performance team: call (727) 375-9375 or "
    "email customercare@zeoenergy.com. I can still log the details for you below."
)

_SKIP_WORDS = {"skip", "none", "no", "n/a", "na", "nope", "pass", "no thanks", "nothing"}

# Human-readable labels for fields we couldn't validate (used in read-back/email).
_FIELD_LABELS = {
    "name": "Your name",
    "account_name": "Solar account name",
    "account_address": "Solar account address",
    "contact": "Best contact",
}


def _email_or_none(value: Optional[str]) -> Optional[str]:
    """Return the value only if it looks like an email (so a phone contact isn't
    passed to the CRM as an email second factor)."""
    if value and re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", value):
        return value
    return None


def _unconfirmed_labels(case: dict) -> list[str]:
    return [_FIELD_LABELS.get(k, k) for k in case.get("_unconfirmed", [])]


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------
def _new_case(session_id: str, mode: Mode) -> dict:
    return {
        "session_id": session_id,
        "mode": mode.value,
        "verbatim": [],
        "attachments": [],
        "_done_steps": [],
        "_await": None,
        "_phase": "collect",
        "_retries": 0,
        "_turns": 0,
        "_lookup_done": False,
        "_match": None,
        "_unconfirmed": [],          # identity fields we could not validate
        "_last_activity": time.time(),
    }


def start(session_id: str, mode: Mode) -> dict:
    """Create (or reset) a session and return the greeting turn."""
    case = _new_case(session_id, mode)
    _register(case)
    steps = _active_steps(case)
    first = steps[0]
    case["_await"] = first.key
    disclaimer = (
        "Disclaimer: I am an automated intake assistant. I cannot make contract commitments, "
        "warranty or insurance coverage guarantees, or provide professional safety advice."
    )
    return _turn(case, [
        BotMessage(text=disclaimer, kind="system"),
        BotMessage(text=first.question)
    ], first)


_REPRESENTATIVE_PATTERNS = [
    r"\brepresentative\b",
    r"\bagent\b",
    r"\bhuman\b",
    r"\bperson\b",
    r"\boperator\b",
    r"\bsupport\b",
    r"\bhelp desk\b",
    r"\btalk to someone\b",
    r"\bspeak to someone\b",
    r"\bcall someone\b",
    r"\bphone number\b",
    r"\bcontact number\b",
]
_REP_REGEX = re.compile("|".join(_REPRESENTATIVE_PATTERNS), re.IGNORECASE)


def is_representative_request(message: str) -> bool:
    if not message:
        return False
    return _REP_REGEX.search(message) is not None


def _dept_for(issue_type) -> Optional[dict]:
    """Pick the department that handles a given issue type (None if unknown yet)."""
    if issue_type == IssueType.SOLAR.value:
        return SERVICE_DEPT
    if issue_type in (IssueType.ROOF.value, IssueType.ELECTRICAL.value, IssueType.MISC.value):
        return NONSTANDARD_DEPT
    return None


def _human_handoff_message(dept: dict) -> str:
    """Friendly, complete instructions for reaching a real person in `dept`."""
    return (
        "No problem — let's get you to a real person.\n"
        f"Call our {dept['name']} at {dept['phone']}\n"
        f"Hours: {SUPPORT_HOURS}\n"
        f"Or email {dept['email']}\n"
        "Have your Solar Account name and service address handy so the team can pull up "
        "your account quickly."
    )


def _human_handoff_both() -> str:
    """Handoff when we still can't tell which team is right — give both, clearly."""
    return (
        "No problem — let's get you to a real person.\n"
        f"Solar production: {SERVICE_DEPT['name']} at {SERVICE_DEPT['phone']} ({SERVICE_DEPT['email']})\n"
        f"Electrical, roof, or other home damage: {NONSTANDARD_DEPT['name']} at "
        f"{NONSTANDARD_DEPT['phone']} ({NONSTANDARD_DEPT['email']})\n"
        f"Hours: {SUPPORT_HOURS}\n"
        "Have your Solar Account name and service address handy so the team can help quickly."
    )


def _enter_human_handoff(case: dict) -> dict:
    """Begin a live-human handoff. If we know the issue type, route straight to the
    right department; otherwise ask which team so we point them correctly."""
    dept = _dept_for(case.get("issue_type"))
    if dept is not None:
        case["_phase"] = "done"
        case["_await"] = DONE
        return _turn(case, [BotMessage(text=_human_handoff_message(dept), kind="system")],
                     None, done=True)
    case["_phase"] = "human"
    case["_await"] = HUMAN
    return _turn(case, [BotMessage(
        text="Of course — I can get you to a real person. Which best describes your issue, "
             "so I can point you to the right team?", kind="system")],
        None, quick_replies=["Solar production", "Electrical / roof / other"])


_SOLAR_ROUTING_HINTS = ("solar", "production", "panel", "inverter")
_NONSTANDARD_ROUTING_HINTS = (
    "electric", "roof", "power", "leak", "damage", "home", "other", "misc", "nonstandard",
)


def _handle_human_routing(case: dict, message: str) -> dict:
    """Interpret the customer's 'which team?' answer and hand them off."""
    low = (message or "").lower()
    if any(k in low for k in _SOLAR_ROUTING_HINTS):
        dept = SERVICE_DEPT
    elif any(k in low for k in _NONSTANDARD_ROUTING_HINTS):
        dept = NONSTANDARD_DEPT
    else:
        dept = None
    case["_phase"] = "done"
    case["_await"] = DONE
    text = _human_handoff_message(dept) if dept else _human_handoff_both()
    return _turn(case, [BotMessage(text=text, kind="system")], None, done=True)


def handle(session_id: str, mode: Mode, message: str, attachments: list[str]) -> dict:
    now = time.time()
    _purge_expired(now)
    case = _SESSIONS.get(session_id)
    is_new = case is None
    if is_new:
        # Unknown (or expired) session -> treat as fresh start.
        case = _new_case(session_id, mode)
        _register(case)

    case["_last_activity"] = now
    case["mode"] = mode.value  # slider may have changed since last turn
    case["_turns"] += 1

    # If we're mid-handoff (waiting on "which team?"), interpret that answer first
    # so a stray keyword doesn't restart the handoff in a loop.
    if not is_new and case.get("_phase") == "human":
        return _handle_human_routing(case, message)

    # A request to reach a real person can come at any point — walk them through it.
    if is_representative_request(message):
        return _enter_human_handoff(case)

    if is_new:
        # If it was a new session and not a representative request, return the greeting.
        steps = _active_steps(case)
        first = steps[0]
        case["_await"] = first.key
        return _turn(case, [BotMessage(text=first.question)], first)

    if case["_turns"] > MAX_TURNS:
        it = case.get("issue_type")
        if it == IssueType.SOLAR.value:
            contact_info = "our Service Department at (727) 349-4057"
        elif it in (IssueType.ROOF.value, IssueType.ELECTRICAL.value, IssueType.MISC.value):
            contact_info = "our Nonstandard Department at (727) 382-0075"
        else:
            contact_info = "our Nonstandard Department at (727) 382-0075 or our Service Department at (727) 349-4057"

        return _turn(case, [BotMessage(
            text=f"We've gone back and forth quite a bit — I'll hand this to a human. "
                 f"Please call {contact_info} and reference your details.", kind="system")],
            None, done=True)

    phase = case["_phase"]
    if phase == "done":
        return _turn(case, [BotMessage(
            text="Your request has already been sent to our team. Switch the mode toggle "
                 "or refresh the page to start a new request.", kind="system")],
            None, done=True)
    if phase == "confirm":
        return _handle_confirm(case, message)
    if phase == "correct":
        return _handle_correction(case, message)
    return _handle_collect(case, message, attachments)


# ---------------------------------------------------------------------------
# Collect phase
# ---------------------------------------------------------------------------
def _handle_collect(case: dict, message: str, attachments: list[str]) -> dict:
    step = _step_by_key(case, case["_await"])
    if step is None:
        # Active step set changed (e.g. issue_type just chosen) — re-anchor.
        return _advance(case, [])

    pre: list[BotMessage] = []

    if step.allow_upload:
        if attachments:
            case["attachments"].extend(attachments)
            pre.append(BotMessage(text=f"Got {len(attachments)} photo(s) — thanks.", kind="system"))
        _resolve(case, step)
        return _advance(case, pre)

    low = message.strip().lower()
    # A field is skippable only if optional AND not yes/no (for yes/no, "no" is a
    # real answer, not a skip — so skip words must not swallow it).
    skippable = (not step.required) and step.field_type != "yesno"
    if skippable and low in _SKIP_WORDS:
        case["_retries"] = 0
        _store(case, step, None, message)
        _resolve(case, step)
        return _advance(case, pre)

    # Extract a candidate value, then gate it through the validation layer before
    # we accept it. This is the core fix: we no longer advance just because the
    # extractor echoed text back — the answer must actually answer the question.
    value = llm.extract(step.field_type, message, field_label=step.key, options=step.options)
    verdict = validate_answer(step, message, value, step.question)

    if not verdict.ok:
        if case["_retries"] < MAX_RETRIES:
            case["_retries"] += 1
            return _turn(case, [BotMessage(text=verdict.reason, kind="system")], step)
        # Retries exhausted — handle by criticality.
        if step.key in validation.IDENTITY_FIELDS:
            # Never pass unverified identity data off as confirmed. Keep what they
            # typed but flag it loudly so staff don't trust it as-is.
            value = message.strip() or None
            if step.key not in case["_unconfirmed"]:
                case["_unconfirmed"].append(step.key)
        elif step.required:
            # Non-identity required: accept raw so the flow never dead-ends.
            value = value if value is not None else (message.strip() or "[unclear]")
        # optional fields keep whatever value we have (possibly None)

    case["_retries"] = 0
    _store(case, step, value, message)
    _resolve(case, step)
    return _advance(case, pre)


def _store(case: dict, step: Step, value, raw_message: str) -> None:
    if not step.key.startswith("_"):
        case[step.key] = value
    if step.key == "account_address" and value:
        coords = geocode_address(value)
        if coords:
            case["latitude"] = coords[0]
            case["longitude"] = coords[1]
    if step.key == "damage_pointer" and value:
        if value in ("fallback", "incorrect_address_fallback"):
            case["geocoding_incorrect"] = True
            case["latitude"] = None
            case["longitude"] = None
            case.pop("damage_pointer", None)
        else:
            if value not in case.setdefault("attachments", []):
                case["attachments"].append(value)
    if step.verbatim and raw_message.strip() and raw_message.strip().lower() not in _SKIP_WORDS:
        case["verbatim"].append(raw_message.strip())


def _resolve(case: dict, step: Step) -> None:
    if step.key == "damage_pointer" and case.get("geocoding_incorrect"):
        if "damage_pointer" in case.setdefault("_done_steps", []):
            case["_done_steps"].remove("damage_pointer")
        return
    if step.key not in case.setdefault("_done_steps", []):
        case["_done_steps"].append(step.key)


def _next_step(case: dict) -> Optional[Step]:
    for s in _active_steps(case):
        if s.key not in case["_done_steps"]:
            return s
    return None


def _advance(case: dict, pre: list[BotMessage]) -> dict:
    nxt = _next_step(case)
    if nxt is not None:
        case["_await"] = nxt.key
        msgs = list(pre)
        # Show the solar deflection note right as we enter the solar branch.
        if nxt.key == "description" and case.get("issue_type") == IssueType.SOLAR.value \
                and "_solar_note" not in case["_done_steps"]:
            msgs.append(BotMessage(text=_SOLAR_DEFLECTION, kind="system"))
            case["_done_steps"].append("_solar_note")
        msgs.append(BotMessage(text=nxt.question))
        return _turn(case, msgs, nxt)

    # All slots collected -> optional lookup, then confirm.
    msgs = list(pre)
    if case["mode"] == Mode.LOOKUP.value and not case["_lookup_done"]:
        msgs.extend(_do_lookup(case))
    return _enter_confirm(case, msgs)


# ---------------------------------------------------------------------------
# Lookup phase (Lookup mode only)
# ---------------------------------------------------------------------------
def _do_lookup(case: dict) -> list[BotMessage]:
    case["_lookup_done"] = True
    # Match on the Solar Account name + address (not the requester's own name).
    match = crm.find_account(
        case.get("account_name") or case.get("name"),
        case.get("account_address") or case.get("address"),
        _email_or_none(case.get("contact")),
    )
    if match is not None:
        # Display-only; the authoritative disposition decision is re-run in the
        # pipeline at finalize time (single source of truth: crm.find_account).
        case["_match"] = match.to_account_match().model_dump()
        return [BotMessage(
            text=(f"I found your account on file — {match.system_size}, installed "
                  f"{match.install_date} at {match.service_address}. I've attached it to your case."),
            kind="system")]
    # Generic, non-confirming response (no account enumeration).
    return [BotMessage(
        text="I couldn't match those details to an account on file — that's okay. "
             "I'll log this as an unverified request and the team will follow up.",
        kind="system")]


# ---------------------------------------------------------------------------
# Confirm phase
# ---------------------------------------------------------------------------
def _readback(case: dict) -> str:
    it = IssueType(case["issue_type"])
    lines = [
        "Here's what I've got — please confirm:",
        f"• Your name: {case.get('name') or '—'}",
        f"• Best contact: {case.get('contact') or case.get('email') or '—'}",
        f"• Solar account name: {case.get('account_name') or '—'}",
        f"• Solar account address: {case.get('account_address') or '—'}",
        f"• Issue: {it.value}",
        f"• Urgency: {case.get('urgency') or '—'}/10",
    ]
    if case.get("third_parties"):
        lines.append(f"• Third parties: {case['third_parties']}")
    if it == IssueType.ROOF:
        lines += [
            f"• Roof type: {case.get('roof_type') or '—'}",
            f"• Actively leaking: {_yn(case.get('leaking'))}",
            f"• First noticed: {case.get('first_noticed') or '—'}",
            f"• Pre-existing damage: {_yn(case.get('pre_existing'))}",
            f"• Attic accessible: {_yn(case.get('attic_accessible'))}",
            f"• Location: {case.get('location') or '—'}",
        ]
        if case.get("damage_pointer"):
            lines.append("• Damage location: [Marked on map/house photo]")
    elif it == IssueType.ELECTRICAL:
        lines += [
            f"• Without power: {_yn(case.get('without_power'))}",
            f"• Breakers tripped: {_yn(case.get('breakers_tripped'))}",
            f"• Neighbors without power: {_yn(case.get('neighbors_without_power'))}",
            f"• Recent storm: {_yn(case.get('recent_storm'))}",
            f"• Affected areas: {case.get('affected_areas') or '—'}",
        ]
    elif it == IssueType.SOLAR:
        lines += [
            f"• System status: {case.get('solar_status') or '—'}",
            f"• Inverter error code: {case.get('inverter_error_code') or '—'}",
            f"• Callback requested: {_yn(case.get('callback_requested'))}",
        ]
    elif it == IssueType.MISC:
        lines += [
            f"• What's damaged: {case.get('what_damaged') or '—'}",
            f"• Cause of damage: {case.get('cause_of_damage') or '—'}",
            f"• Location: {case.get('location') or '—'}",
        ]
        if case.get("damage_pointer"):
            lines.append("• Damage location: [Marked on map/house photo]")
    if case.get("attachments"):
        lines.append(f"• Photos: {len(case['attachments'])} attached")
    if case.get("verbatim"):
        lines.append(f"• Your description: \"{case['verbatim'][0]}\"")
    if case.get("_unconfirmed"):
        fields = ", ".join(_unconfirmed_labels(case))
        lines.append(f"\nNote: I couldn't fully verify: {fields}. I'll flag this for the team to confirm.")
    lines.append("\nShould I send this to our service team? (Note: Submission does not guarantee service coverage, warranty approval, or immediate dispatch.)")
    return "\n".join(lines)


def _yn(v) -> str:
    return "Yes" if v is True else "No" if v is False else "—"


def _enter_confirm(case: dict, pre: list[BotMessage]) -> dict:
    case["_phase"] = "confirm"
    case["_await"] = CONFIRM
    msgs = list(pre) + [BotMessage(text=_readback(case))]
    return _turn(case, msgs, None, quick_replies=["Yes, send it", "No, change something"])


def _handle_confirm(case: dict, message: str) -> dict:
    yes = llm.extract("yesno", message)
    if yes is True:
        return _finalize(case)
    if yes is False:
        case["_phase"] = "correct"
        case["_await"] = CORRECT
        return _turn(case, [BotMessage(
            text="No problem — what would you like to change? Just tell me the correct "
                 "detail and I'll note it.", kind="system")], None)
    # Ambiguous reply (not a clear yes/no): re-ask rather than guessing. Previously
    # any non-"yes" was treated as "change", which mis-routed unclear replies.
    return _turn(case, [BotMessage(
        text="Just to confirm — should I send this to our team? Reply 'yes' to send, "
             "or 'no' to change something.", kind="system")],
        None, quick_replies=["Yes, send it", "No, change something"])


def _handle_correction(case: dict, message: str) -> dict:
    if message.strip():
        case["verbatim"].append(f"[Correction] {message.strip()}")
    return _enter_confirm(case, [BotMessage(
        text="Thanks, I've noted that correction. Here's the updated summary:", kind="system")])


# ---------------------------------------------------------------------------
# Finalize -> render email
# ---------------------------------------------------------------------------
def _finalize(case: dict) -> dict:
    # The disposition pipeline owns the CRM match, case creation, routing, and
    # email send. It re-runs the match authoritatively (not trusting any earlier
    # display-only lookup) and persists the handoff email for the Inbox tab.
    result = pipeline.dispatch(case)
    case["_phase"] = "done"
    case["_await"] = DONE
    if result.matched:
        sent_line = (
            "Account matched! Your request has been submitted and opened with our service team. "
            "You should expect a response in 1–2 business days. If you need to reach us sooner, "
            "you can call our Service Team at 727-349-4057 for solar issues, "
            "our Nonstandard Department at 727-382-0075 for electrical or roof damage, "
            "or Customer Care at 727-375-9375 for any other questions (just mention that you've submitted this form!)."
        )
    else:
        sent_line = (
            "We've submitted your request! Our team will review it and follow up within 1–2 business days. "
            "If you need to reach us sooner, you can call our Service Team at 727-349-4057 for solar issues, "
            "our Nonstandard Department at 727-382-0075 for electrical or roof damage, "
            "or Customer Care at 727-375-9375 for any other questions (just mention that you've submitted this form!)."
        )
    msg = BotMessage(text=sent_line, kind="system")
    return _turn(case, [msg], None, done=True, email_id=result.email_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _turn(case: dict, messages: list[BotMessage], step: Optional[Step],
          *, quick_replies: Optional[list[str]] = None,
          done: bool = False, email_id: Optional[str] = None) -> dict:
    qr = quick_replies
    allow_upload = False
    if step is not None and quick_replies is None:
        qr = step.quick_replies
        allow_upload = step.allow_upload
    return {
        "session_id": case["session_id"],
        "messages": [m.model_dump() for m in messages],
        "quick_replies": qr or [],
        "allow_upload": allow_upload,
        "state": case["_phase"],
        "done": done,
        "email_id": email_id,
        "await_step": case.get("_await"),
        "account_address": case.get("account_address"),
        "latitude": case.get("latitude"),
        "longitude": case.get("longitude"),
    }
