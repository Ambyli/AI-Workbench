"""Escalated customers, requests for a person, incomplete and messy forms.

  E1  ordinary answers never trigger a handoff (the old single-word rules did)
  E2  real requests for a person always do, from any step
  E3  the right number: solar -> 349-4057; everything else, or no issue yet -> 382-0075
  E4  after-hours wording follows Mountain Time business hours
  E5  frustration: acknowledged once (with the right number), conversation continues,
      staff email flags "customer may be upset"
  E6  handoff mid-form alerts the department with what was collected; nothing to send
      when there's nothing to follow up on
  E7  customer stops responding: one alert after the idle threshold, never twice; a later
      finished request says an alert went out earlier
  E8  turn limit hands off with an alert; a 911 message shown earlier is flagged
  E9  answers that stayed unclear are listed for staff
  E10 incomplete alerts never open a CRM case
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("GEOCODER", "none")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="chatbot-test-"))
os.environ.pop("ENABLE_STAFF_API", None)
os.environ["USE_MOCK_LLM"] = "1"
os.environ["RATE_LIMIT_REQUESTS"] = "100000"
os.environ["ACCOUNT_GATEWAY"] = "inprocess"
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from _session_client import SessionClient  # noqa: E402

from backend import email_render, state_machine  # noqa: E402
from backend.main import app  # noqa: E402

c = SessionClient(app)
SOLAR, OTHER = "727-349-4057", "727-382-0075"
_n = [0]


def chat(*answers: str) -> tuple[str, list[dict]]:
    _n[0] += 1
    label = f"esc{_n[0]}"
    turns = [c.post("/api/chat", json={"session_id": label, "message": ""}).json()]
    for a in answers:
        turns.append(c.post("/api/chat", json={"session_id": label, "message": a}).json())
    return label, turns


def texts(turn: dict) -> str:
    return "\n".join(m["text"] for m in turn.get("messages", []))


def emails() -> list[dict]:
    return list(email_render._STORE)


def test_no_false_handoffs():
    print("E1: ordinary answers never hand off...")
    cases = [
        ("contact", ["Jane Doe", "100 Solar Way, Tampa, FL 33601", "my phone number is 813-555-0199"]),
        ("third parties", ["Jane Doe", "100 Solar Way, Tampa, FL 33601", "813-555-0199", "Roof", "6",
                           "yes, my insurance agent came out"]),
        ("account name", ["the person on the account is my husband Robert Smith"]),
        ("description", ["Jane Doe", "100 Solar Way, Tampa, FL 33601", "813-555-0199", "Misc / other", "4", "skip",
                         "the support beam under the deck is cracked"]),
        ("contact time", ["Jane Doe", "100 Solar Way, Tampa, FL 33601", "call me after 5pm at 813-555-0199"]),
        ("manager word", ["Jane Doe", "100 Solar Way, Tampa, FL 33601", "813-555-0199", "Roof", "6",
                          "the property manager called the roofer"]),
    ]
    for name, answers in cases:
        _, turns = chat(*answers)
        assert not turns[-1].get("done"), f"{name}: handed off by mistake: {texts(turns[-1])[:120]}"
    print(f"  ok ({len(cases)} cases)")


def test_real_requests_hand_off():
    print("E2: real requests for a person always hand off...")
    for msg in ["can I talk to a real person", "I want to speak with a manager", "get me a human",
                "agent please", "representative", "What's your phone number?", "how do I contact someone",
                "stop with the bot", "connect me to someone", "I need to talk to somebody now"]:
        _, turns = chat("Jane Doe", msg)
        t = turns[-1]
        assert t.get("done") and t.get("outcome") == "handoff", f"{msg!r} did not hand off: {texts(t)[:80]}"
    print("  ok")


def test_right_number():
    print("E3: right number for the situation...")
    _, t = chat("I want to talk to a person")
    assert OTHER in texts(t[-1]) and SOLAR not in texts(t[-1]), "no issue yet -> 0075 only"
    _, t = chat("Jane Doe", "100 Solar Way, Tampa, FL 33601", "813-555-0199", "Solar not producing",
                "can I speak to someone")
    assert SOLAR in texts(t[-1]) and OTHER not in texts(t[-1]), "solar -> 349-4057 only"
    for issue in ("Roof", "Electrical", "Misc / other"):
        _, t = chat("Jane Doe", "100 Solar Way, Tampa, FL 33601", "813-555-0199", issue, "get me a human")
        assert OTHER in texts(t[-1]) and SOLAR not in texts(t[-1]), f"{issue} -> 0075 only"
    print("  ok")


def test_after_hours():
    print("E4: after-hours wording follows Mountain Time...")
    open_ = datetime(2026, 10, 7, 17, 0, tzinfo=timezone.utc)    # Wed 11:00 MDT
    closed = datetime(2026, 10, 7, 3, 0, tzinfo=timezone.utc)    # Wed 21:00 MDT (Tue night)
    weekend = datetime(2026, 10, 10, 18, 0, tzinfo=timezone.utc)  # Sat 12:00 MDT
    assert state_machine._after_hours_line(open_) == ""
    assert "closed right now" in state_machine._after_hours_line(closed)
    assert "closed right now" in state_machine._after_hours_line(weekend)
    print("  ok")


def test_frustration():
    print("E5: frustration acknowledged once, flow continues, staff flagged...")
    label, t = chat("Jane Doe", "100 Solar Way, Tampa, FL 33601", "813-555-0199",
                    "this is ridiculous, my roof is leaking")
    notice = [m for m in t[-1]["messages"] if m["kind"] == "notice"]
    assert notice and OTHER in notice[0]["text"] and not t[-1].get("done"), texts(t[-1])
    assert t[-1]["await_step"] == "urgency", "the answer in the same message still counts (roof)"
    t2 = c.post("/api/chat", json={"session_id": label, "message": "ugh this is terrible, 9"}).json()
    assert not any(m["kind"] == "notice" and "frustrating" in m["text"] for m in t2["messages"]), "only once"
    for a in ("skip", "Shingle", "Yes", "yesterday", "No", "Yes"):
        c.post("/api/chat", json={"session_id": label, "message": a})
    rec_before = len(emails())
    for a in (c.pointer_image(label), "Water drips into the hallway.", "skip", "Yes, send it"):
        c.post("/api/chat", json={"session_id": label, "message": a})
    assert len(emails()) == rec_before + 1, "request should be submitted"
    assert "Customer may be upset" in emails()[-1]["html"]
    print("  ok")


def test_handoff_alert():
    print("E6: handoff mid-form alerts the department; nothing to alert without details...")
    before = len(emails())
    _, t = chat("Jane Doe", "100 Solar Way, Tampa, FL 33601", "813-555-0199", "Electrical",
                "I want to talk to a person")
    assert "passed along what you've told me" in texts(t[-1])
    assert len(emails()) == before + 1
    e = emails()[-1]
    assert e["subject"].startswith("[Incomplete]") and "INCOMPLETE REQUEST" in e["html"]
    assert "asked to talk to a person" in e["html"] and "Urgency" in e["html"], "reason and missing fields"
    assert e["case_id"] is None, "E10: incomplete alerts never open a CRM case"
    before = len(emails())
    _, t = chat("I want to talk to a person")
    assert len(emails()) == before and "Have your Solar Account name" in texts(t[-1])
    print("  ok")


def test_abandoned():
    print("E7: stopped responding -> one alert, never twice; later submit says so...")
    label, _ = chat("Jane Doe", "100 Solar Way, Tampa, FL 33601", "813-555-0199", "Roof")
    sid = c.real_sid(label)
    case = state_machine._SESSIONS[sid]
    case["_last_activity"] -= 3600 * 0 + 1300  # idle past ABANDON_AFTER_SECONDS (1200)
    before = len(emails())
    assert state_machine.sweep_abandoned() >= 1
    assert len(emails()) == before + 1 and "stopped responding" in emails()[-1]["html"]
    assert state_machine.sweep_abandoned() == 0, "never alert twice"
    case["_last_activity"] = time.time()
    for a in ("6", "skip", "Shingle", "No", "No", "Yes", "<pin>", "Leak near the vent.", "skip", "Yes, send it"):
        msg = c.pointer_image(label) if a == "<pin>" else a
        c.post("/api/chat", json={"session_id": label, "message": msg})
    last = emails()[-1]
    assert not last["subject"].startswith("[Incomplete]") and "alert was sent earlier" in last["html"]
    # Nothing worth alerting (no contact, no account details): no email.
    label2, _ = chat("Jane Doe")
    state_machine._SESSIONS[c.real_sid(label2)]["_last_activity"] -= 1300
    n = len(emails())
    state_machine.sweep_abandoned()
    assert len(emails()) == n
    print("  ok")


def test_turn_limit_and_safety():
    print("E8: turn limit hands off with an alert; earlier 911 message is flagged...")
    label, _ = chat("Jane Doe", "100 Solar Way, Tampa, FL 33601", "813-555-0199")
    c.post("/api/chat", json={"session_id": label, "message": "I smell gas"})
    before = len(emails())
    last = {}
    for i in range(70):
        last = c.post("/api/chat", json={"session_id": label, "message": "hmm" if i % 2 else "idk"}).json()
        if last.get("done"):
            break
    assert last.get("outcome") == "handoff" and OTHER in texts(last)
    e = emails()[-1]
    assert len(emails()) == before + 1 and "turn limit" in e["html"] and "911 safety message" in e["html"]
    print("  ok")


def test_unclear_answers():
    print("E9: answers that stayed unclear are listed for staff...")
    label, _ = chat("Jane Doe", "100 Solar Way, Tampa, FL 33601", "813-555-0199", "Electrical",
                    "dunno", "no idea", "not sure")  # urgency x3 -> unclear
    for a in ("skip", "Yes", "No", "No", "No", "Kitchen", "Outlets stopped working.", "skip", "Yes, send it"):
        c.post("/api/chat", json={"session_id": label, "message": a})
    e = emails()[-1]
    assert "Unclear answers" in e["html"] and "Urgency" in e["html"] and "not given" in e["html"], e["subject"]
    print("  ok")


if __name__ == "__main__":
    tests = [test_no_false_handoffs, test_real_requests_hand_off, test_right_number, test_after_hours,
             test_frustration, test_handoff_alert, test_abandoned, test_turn_limit_and_safety,
             test_unclear_answers]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL {t.__name__}: {exc}")
    print(f"\nescalation_test: {len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
