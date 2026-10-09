"""Deterministic conversation state machine.

This is the brain AND the security boundary. It owns: which slot is active,
branching by issue type, the optional lookup step, the read-back/confirm, turn
limits, and when the email is rendered. The LLM is only ever asked to extract a
value for the single slot named below -- it cannot change the flow.

Session state is an in-memory dict keyed by session_id (prototype-grade).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import re
from typing import Callable, Optional

import time

from . import account_gateway, llm, validation
from .config import settings
from .schemas import BotMessage, IssueType, Mode
from .validation import validate_answer

logger = logging.getLogger("chatbot.state_machine")

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
        case = _SESSIONS.get(sid)
        if case is not None and case.get("_phase") != "done":
            try:  # last chance to tell the department about an unfinished request
                _send_incomplete_alert(case, "stopped_responding")
            except Exception as exc:  # noqa: BLE001
                logger.warning("Incomplete alert failed (%s).", type(exc).__name__)
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
    "name": "Nonstandard Department (Roofing / Electrical / Battery / Home damage)",
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
        # Account-matching fields: the name on the account (asked once; it is also the
        # name staff address the customer by), then the address. Once both are in,
        # the account check runs through account_gateway (code-only, model-blind, gapped)
        # and the customer is told only
        # "found" or "not found" before the rest of the form.
        Step("account_name", "text", "Hi! I'm here to help with your Zeo Energy service request. "
             "What is the name associated with your Solar Account?", required=True),
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
    if not address or settings.GEOCODER == "none":
        return None
    cleaned = clean_address_for_geocoding(address)
    # Try tiered queries:
    # 1. Cleaned address (e.g., "100 w solar way tampa")
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
                logger.info("Geocoded service address (%d chars) successfully.", len(q))  # never log address/coords (S-M5)
                return lat, lon
        except Exception as exc:
            logger.warning("Geocoding attempt failed (%s).", type(exc).__name__)
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
    "issue. The fastest path for solar production, microinverter, or Enphase issues is "
    "our Service Department: call (727) 349-4057 or email customercare@zeoenergy.com. "
    "I can still log the details for you below."
)

_SKIP_WORDS = {"skip", "none", "no", "n/a", "na", "nope", "pass", "no thanks", "nothing"}

# Human-readable labels for fields we couldn't validate (used in read-back/email).
_FIELD_LABELS = {
    "name": "Your name",
    "account_name": "Solar account name",
    "account_address": "Solar account address",
    "contact": "Best contact",
}



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
        "_acct_raw": {},             # the customer's typed account answers (query inputs)
        "_acct": None,               # {"matched": bool, "ref": opaque} — never the match details
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


# ---------------------------------------------------------------------------
# Escalation: requests for a person, frustration, incomplete-request alerts
# ---------------------------------------------------------------------------
# Only clear requests for a person trigger a handoff. Single words like "agent",
# "person", "support" or "phone number" used to, so ordinary answers ("my insurance
# agent", "my phone number is ...", "the support beam") ended the chat by mistake.
_PERSON = (r"(?:a\s+|an\s+|the\s+|your\s+|some\s+)?(?:real\s+|live\s+|actual\s+)?"
           r"(?:person|human|agent|representative|rep|someone|somebody|manager|supervisor|operator)")
_REPRESENTATIVE_PATTERNS = [
    rf"\b(?:talk|speak|chat)\s+(?:to|with)\s+{_PERSON}\b",
    rf"\b(?:want|need|would\s+like|'d\s+like|demand|request)\s+(?:to\s+(?:talk|speak)\s+(?:to|with)\s+)?{_PERSON}\b",
    rf"\b(?:get|give|find)\s+me\s+{_PERSON}\b",
    r"\b(?:connect|transfer|put)\s+me\s+(?:to|through|with)\b",
    r"\b(?:can|could|may)\s+i\s+(?:talk|speak|chat)\s+(?:to|with)\b",
    r"^\W*(?:human|agent|representative|operator|real\s+person|live\s+person|live\s+agent|manager|supervisor)"
    r"(?:\s+(?:please|pls|now))?\W*$",
    r"\b(?:what(?:'s|\s+is)\s+(?:your|the)\s+(?:phone\s+)?number|number\s+(?:to|i\s+can)\s+call|"
    r"how\s+(?:do|can)\s+i\s+(?:call|reach|contact)\s+(?:you|someone|zeo|a\s+person))\b",
    r"\b(?:stop|enough)\b.{0,20}\b(?:bot|robot|machine|automated)\b",
]
_REP_REGEX = re.compile("|".join(_REPRESENTATIVE_PATTERNS), re.IGNORECASE)

# Frustration / escalation language: acknowledged once with the right phone number,
# and flagged to staff, but the conversation continues (the customer may still be
# answering, e.g. "I contacted my lawyer" at the third-parties question).
_FRUSTRATION_REGEX = re.compile(
    r"\b(?:ridiculous|unacceptable|frustrat\w*|furious|angry|fed\s+up|sick\s+of|tired\s+of|"
    r"waste\s+of\s+(?:my\s+)?time|useless|terrible|worst|scam|not\s+happy|unhappy|"
    r"complain\w*|lawyer|attorney|sue|lawsuit|better\s+business\s+bureau|bbb|"
    r"manager|supervisor|fuck\w*|shit\w*|wtf|damn\w*|crap)\b",
    re.IGNORECASE,
)


def is_representative_request(message: str) -> bool:
    if not message:
        return False
    return _REP_REGEX.search(message) is not None


def is_frustrated(message: str) -> bool:
    return bool(message) and _FRUSTRATION_REGEX.search(message) is not None


def _dept_for(issue_type) -> dict:
    """The department to call. Before the customer has chosen an issue type, the
    Nonstandard line (727-382-0075) is the default (owner decision 2026-10-02)."""
    if issue_type == IssueType.SOLAR.value:
        return SERVICE_DEPT
    return NONSTANDARD_DEPT


def _after_hours_line(now: Optional[datetime] = None) -> str:
    """Tell the customer when the phone line is closed (Mon-Fri 9am-5pm Mountain)."""
    now = now or datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        local = now.astimezone(ZoneInfo("America/Denver"))
    except Exception:  # noqa: BLE001 - no tz database: assume MST (UTC-7)
        local = now.astimezone(timezone(timedelta(hours=-7)))
    if local.weekday() < 5 and 9 <= local.hour < 17:
        return ""
    return ("Our team is closed right now and opens at 9am Mountain Time on the next "
            "business day. You can call then, or finish your request here and they'll follow up.")


def _handoff_text(dept: dict, *, apology: bool, passed_along: bool) -> str:
    """One clear next step (chatbot best practice): empathy, one number, hours,
    after-hours note, and whether we've already passed their details along."""
    lines = ["I'm sorry for the trouble you've had. Let's get you to a person."
             if apology else "Of course. Let's get you to a person.",
             f"Call our {dept['name']} at {dept['phone']}",
             f"Hours: {SUPPORT_HOURS}"]
    closed = _after_hours_line()
    if closed:
        lines.append(closed)
    lines.append(f"Or email {dept['email']}")
    if passed_along:
        lines.append("I've passed along what you've told me so far, so you won't have to start over.")
    else:
        lines.append("Have your Solar Account name and service address handy so the team can find "
                     "your account quickly.")
    return "\n".join(lines)


def _frustration_notice(case: dict) -> BotMessage:
    dept = _dept_for(case.get("issue_type"))
    text = (f"I'm sorry this has been frustrating. If you'd rather talk to someone now, call our "
            f"{dept['name']} at {dept['phone']} ({SUPPORT_HOURS}). Otherwise, I'll keep going with "
            "your request.")
    closed = _after_hours_line()
    if closed:
        text += " " + closed
    return BotMessage(text=text, kind="notice")


# Labels for the "Not answered" list in incomplete-request alerts.
_STEP_LABELS = {
    "account_name": "Solar account name", "account_address": "Solar account address",
    "contact": "Best contact", "issue_type": "Issue type", "urgency": "Urgency",
    "description": "Problem description", "what_damaged": "What is damaged",
    "damage_pointer": "Damage location on the map", "leaking": "Actively leaking",
    "without_power": "Without power",
}
_INCOMPLETE_REASONS = {
    "asked_for_person": "The customer asked to talk to a person before finishing the form.",
    "turn_limit": "The conversation hit the turn limit before the form was finished.",
    "stopped_responding": "The customer stopped responding before finishing the form.",
}


def _missing_required(case: dict) -> list[str]:
    done = set(case.get("_done_steps", []))
    return [_STEP_LABELS.get(s.key, s.key.replace("_", " ").capitalize())
            for s in _active_steps(case) if s.required and s.key not in done]


def _has_follow_up_info(case: dict) -> bool:
    """Worth alerting staff only if they can reach or identify the customer."""
    return bool(case.get("contact") or (case.get("account_name") and case.get("account_address")))


def _send_incomplete_alert(case: dict, reason: str) -> bool:
    """Send the department what we have so far (once per conversation). Returns True
    if an alert was sent. Goes through the account gateway like a normal submit, so
    the account service appends the account check and routes it."""
    if case.get("_alerted") or case.get("_submitted") or not _has_follow_up_info(case):
        return False
    case["_alerted"] = True
    # The "incomplete" marker goes on the alert's copy only: if the customer comes back
    # and finishes, the finished request must not be labelled incomplete.
    alert = dict(case)
    alert["_incomplete"] = {"reason": reason, "reason_text": _INCOMPLETE_REASONS.get(reason, reason),
                            "missing": _missing_required(case)}
    acct = case.get("_acct") or {}
    # The account ref is single-use, so the alert re-checks from the typed answers and
    # the real submit (if it comes) keeps the ref.
    account_gateway.dispatch(None, alert, matched_hint=bool(acct.get("matched")))
    return True


def _enter_human_handoff(case: dict, reason: str = "asked_for_person", apology: bool = False) -> dict:
    """Hand the customer to a person: the right number straight away (no gatekeeping,
    no 'which team?' question), and alert the department with what we have."""
    dept = _dept_for(case.get("issue_type"))
    passed = _send_incomplete_alert(case, reason)
    case["_phase"] = "done"
    case["_await"] = DONE
    text = _handoff_text(dept, apology=apology or bool(case.get("_escalation")), passed_along=passed)
    return _turn(case, [BotMessage(text=text, kind="system")], None, done=True, outcome="handoff")


def note_safety(session_id: Optional[str]) -> None:
    """Record that the 911 safety message was shown, so staff see it on any alert."""
    case = _SESSIONS.get(session_id or "")
    if case is not None:
        case["_safety_shown"] = True


def sweep_abandoned(now: Optional[float] = None) -> int:
    """Alert the department about requests the customer stopped answering. Runs on a
    timer (backend/main.py) and before expired sessions are purged."""
    now = now or time.time()
    sent = 0
    for case in list(_SESSIONS.values()):
        idle = now - case.get("_last_activity", now)
        if case.get("_phase") != "done" and idle >= settings.ABANDON_AFTER_SECONDS:
            try:
                if _send_incomplete_alert(case, "stopped_responding"):
                    sent += 1
            except Exception as exc:  # noqa: BLE001 - never break the sweep
                logger.warning("Incomplete alert failed (%s).", type(exc).__name__)
    return sent


def current_step(session_id: str) -> Optional[str]:
    """The step this session is waiting on (None if unknown). Read-only accessor."""
    case = _SESSIONS.get(session_id)
    return case.get("_await") if case else None


def forget(session_id: str) -> None:
    """Drop a finished conversation's case data from memory (PII minimisation)."""
    _SESSIONS.pop(session_id, None)


def handle(session_id: str, mode: Mode, message: str, attachments: list[str],
           pointer: Optional[tuple[float, float]] = None) -> dict:
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

    frustrated = is_frustrated(message)
    if frustrated:
        case["_escalation"] = True  # staff see "customer may be upset" on the email

    # A request to reach a real person can come at any point: give the right number
    # straight away (never gatekeep), and alert the department with what we have.
    if case.get("_phase") != "done" and is_representative_request(message):
        return _enter_human_handoff(case, apology=frustrated)

    if is_new:
        # If it was a new session and not a representative request, return the greeting.
        steps = _active_steps(case)
        first = steps[0]
        case["_await"] = first.key
        return _turn(case, [BotMessage(text=first.question)], first)

    if case["_turns"] > MAX_TURNS:
        # Don't loop forever: hand off to a person (right number) and alert the team.
        return _enter_human_handoff(case, reason="turn_limit")

    phase = case["_phase"]
    if phase == "done":
        return _turn(case, [BotMessage(
            text="This conversation has ended. Use Start over to begin a new request.", kind="system")],
            None, done=True)
    notice = None
    if frustrated and not case.get("_frustration_noticed"):
        case["_frustration_noticed"] = True  # acknowledge once, never nag
        notice = _frustration_notice(case)
    res = _route_turn(case, phase, message, attachments, pointer)
    if notice is not None:
        res["messages"] = [notice.model_dump()] + res["messages"]
    return res


def _route_turn(case: dict, phase: str, message: str, attachments: list[str],
                pointer: Optional[tuple[float, float]]) -> dict:
    if phase == "confirm":
        return _handle_confirm(case, message)
    if phase == "correct":
        return _handle_correction(case, message)
    if pointer is not None and case.get("_await") == "damage_pointer":
        # Exact pinned coordinates (contract v2) so staff get a precise Maps link.
        case["damage_pointer_coords"] = {"lat": round(pointer[0], 6), "lng": round(pointer[1], 6)}
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
        elif step.required and step.field_type == "text":
            # Non-identity required text: accept raw so the flow never dead-ends.
            value = value if value is not None else (message.strip() or "[unclear]")
        elif step.required and step.key == "issue_type":
            # A typed field must never hold raw text (it crashed the read-back as an
            # invalid IssueType). Route to the general branch; staff see the verbatim.
            value = value if value is not None else "misc"
        # other typed fields keep None (shown as "—") rather than raw text
        # optional fields keep whatever value we have (possibly None)
        if step.required and (value is None or value == "[unclear]" or step.key == "issue_type"):
            label = _STEP_LABELS.get(step.key, step.key.replace("_", " ").capitalize())
            if label not in case.setdefault("_unclear", []):
                case["_unclear"].append(label)  # staff see "Unclear answers: ..."

    case["_retries"] = 0
    _store(case, step, value, message)
    _resolve(case, step)
    return _advance(case, pre)


def _store(case: dict, step: Step, value, raw_message: str) -> None:
    if not step.key.startswith("_"):
        case[step.key] = value
    if step.key == "account_name":
        # Asked once: the account name is also the name on the case (email subject,
        # staff notes, CRM record).
        case["name"] = value
    if step.key in ("account_name", "account_address"):
        # The account search uses what the customer typed, never the model's extraction.
        case.setdefault("_acct_raw", {})[step.key] = raw_message
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
    if not case["_lookup_done"] and {"account_name", "account_address"} <= set(case["_done_steps"]):
        pre = list(pre) + [_account_check(case)]
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

    # All slots collected -> confirm.
    return _enter_confirm(case, list(pre))


# ---------------------------------------------------------------------------
# Account check (code-only, model-blind, gapped — see backend/account_gateway.py)
# ---------------------------------------------------------------------------
def _account_check(case: dict) -> BotMessage:
    """Run once per conversation, right after the account name and address.

    Separation layer: the chatbot asks the account gateway and learns ONLY whether
    there is a match, plus an opaque reference. The project, candidates and scores
    stay in the account service, which appends them to the end of the handoff at
    submit. The customer sees one of two fixed messages. Corrections at the confirm
    step do not re-run it (one attempt per chat limits probing)."""
    case["_lookup_done"] = True
    raw = case.get("_acct_raw", {})
    result = account_gateway.check(raw.get("account_name"), raw.get("account_address"))
    case["_acct"] = {"matched": result.matched, "ref": result.ref}
    return BotMessage(text=result.customer_message(), kind="notice")


# ---------------------------------------------------------------------------
# Confirm phase
# ---------------------------------------------------------------------------
def _readback(case: dict) -> str:
    it = IssueType(case["issue_type"])
    lines = [
        "Here's what I've got — please confirm:",
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


_ISSUE_LABELS = {"roof": "Roof", "electrical": "Electrical",
                 "solar": "Solar production", "misc": "Other damage"}
_SOLAR_STATUS_LABELS = {"completely_off": "Completely offline", "low_production": "Low production",
                        "physical_damage": "Physical damage"}
_CONFIRM_NOTE = ("Submission does not guarantee service coverage, warranty approval, "
                 "or immediate dispatch.")


def _summary(case: dict) -> dict:
    """Structured read-back for the confirmation card (contract v2 `summary`).

    Mirrors _readback's data but only includes rows that have a value, uses
    customer-friendly labels, and never includes internal fields. All values are
    plain text; the client renders them as text nodes.
    """
    it = IssueType(case["issue_type"])
    rows: list[dict] = []

    def add(label: str, value, always: bool = False) -> None:
        if isinstance(value, bool):
            value = "Yes" if value else "No"
        text = "" if value is None else str(value).strip()
        if text or always:
            rows.append({"label": label, "value": text or "—"})

    add("Best contact", case.get("contact") or case.get("email"), always=True)
    add("Solar account name", case.get("account_name"), always=True)
    add("Solar account address", case.get("account_address"), always=True)
    add("Issue", _ISSUE_LABELS.get(it.value, it.value), always=True)
    add("Urgency", f"{case['urgency']} of 10" if case.get("urgency") else None, always=True)
    add("Third parties contacted", case.get("third_parties"))
    if it == IssueType.ROOF:
        add("Roof type", (case.get("roof_type") or "").title() or None)
        add("Actively leaking", case.get("leaking"))
        add("First noticed", case.get("first_noticed"))
        add("Pre-existing damage", case.get("pre_existing"))
        add("Attic accessible", case.get("attic_accessible"))
    elif it == IssueType.ELECTRICAL:
        add("Without power", case.get("without_power"))
        add("Breakers tripped", case.get("breakers_tripped"))
        add("Neighbors without power", case.get("neighbors_without_power"))
        add("Recent storm", case.get("recent_storm"))
        add("Affected areas", case.get("affected_areas"))
    elif it == IssueType.SOLAR:
        add("System status", _SOLAR_STATUS_LABELS.get(case.get("solar_status") or "", case.get("solar_status")))
        add("Inverter error code", case.get("inverter_error_code"))
        add("Callback requested", case.get("callback_requested"))
    elif it == IssueType.MISC:
        add("What's damaged", case.get("what_damaged"))
        add("Cause of damage", case.get("cause_of_damage"))
    if case.get("damage_pointer"):
        coords = case.get("damage_pointer_coords")
        add("Damage location", f"Marked on map ({coords['lat']:.5f}, {coords['lng']:.5f})"
            if coords else "Marked on map")
    photos = [a for a in case.get("attachments", []) if a != case.get("damage_pointer")]
    if photos:
        add("Photos", f"{len(photos)} attached")
    if case.get("verbatim"):
        add("Your description", case["verbatim"][0])
    return {"rows": rows, "unverified": _unconfirmed_labels(case), "note": _CONFIRM_NOTE}


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
    # The account service appends the account result to the end of the handoff and
    # delivers it (email, team chat, CRM case). The chatbot only passes the opaque ref.
    acct = case.get("_acct") or {}
    case["_submitted"] = True
    result = account_gateway.dispatch(acct.get("ref"), case, matched_hint=bool(acct.get("matched")))
    case["_phase"] = "done"
    case["_await"] = DONE
    if result.matched:
        sent_line = (
            "Account matched! Your request has been submitted and opened with our service team. "
            "You should expect a response in 1–2 business days. If you need to reach us sooner, "
            "you can call 727-349-4057 for solar production, microinverter, or Enphase issues, "
            "727-382-0075 for roofing, electrical, battery, or other home damage, "
            "or Customer Care at 727-375-9375 for any other questions (just mention that you've submitted this form!)."
        )
    else:
        sent_line = (
            "We've submitted your request! Our team will review it and follow up within 1–2 business days. "
            "If you need to reach us sooner, "
            "you can call 727-349-4057 for solar production, microinverter, or Enphase issues, "
            "727-382-0075 for roofing, electrical, battery, or other home damage, "
            "or Customer Care at 727-375-9375 for any other questions (just mention that you've submitted this form!)."
        )
    msg = BotMessage(text=sent_line, kind="system")
    return _turn(case, [msg], None, done=True, email_id=result.email_id, outcome="submitted")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _turn(case: dict, messages: list[BotMessage], step: Optional[Step],
          *, quick_replies: Optional[list[str]] = None,
          done: bool = False, email_id: Optional[str] = None,
          outcome: Optional[str] = None) -> dict:
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
        # Contract v2.1: how a finished conversation ended (submitted | handoff | ended).
        "outcome": (outcome or "ended") if done else None,
        "email_id": email_id,
        "await_step": case.get("_await"),
        **_location_fields(case),
        "summary": _summary(case) if case["_phase"] == "confirm" else None,
    }


def _location_fields(case: dict) -> dict:
    """Address + coordinates only while the map step needs them (contract v2).

    Previously every turn echoed the address and geocoded coordinates (S-API3).
    The flat account_address/latitude/longitude keys are kept, at this step only,
    for the internal demo portal.
    """
    if case.get("_await") != "damage_pointer":
        return {"location": None}
    lat, lng = case.get("latitude"), case.get("longitude")
    return {
        "location": {"address": case.get("account_address") or "", "lat": lat, "lng": lng},
        "account_address": case.get("account_address"),
        "latitude": lat,
        "longitude": lng,
    }
