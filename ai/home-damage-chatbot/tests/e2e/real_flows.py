"""End-to-end conversation tests against the RUNNING mock on the real model.

Release test plan 5.3 (conversation flows) and 5.4 (account check). Talks to the
site server exactly like the browser does (http://localhost:8080 by default), then
checks what staff received in the account service's data (mock/.data/account).

Run with the mock started (scripts/mock-start.ps1):
    python tests/e2e/real_flows.py [--base http://localhost:8080] [--only F1,F6] [--skip-accounts]

The client honours 429 Retry-After like the page does, so the run also exercises the
rate limiter; a full run takes several minutes.
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from backend.account_gateway import FOUND_MESSAGE, NOT_FOUND_MESSAGE  # noqa: E402

EMAILS = ROOT / "mock" / ".data" / "account" / "emails.json"
UPLOADS = ROOT / "mock" / ".data" / "chatbot" / "uploads"
SCENARIOS = json.loads((ROOT / "backend" / "fixtures" / "mock_projects.json").read_text(encoding="utf-8"))["scenarios"]
HANDOFF_SOLAR, HANDOFF_OTHER = "727-349-4057", "727-382-0075"


OPEN: list["Chat"] = []


@dataclass
class Chat:
    http: httpx.Client
    sid: str | None = None
    turns: list[dict] = field(default_factory=list)

    def __post_init__(self):
        OPEN.append(self)

    def close(self) -> None:
        """End an unfinished chat like a customer would (ask for a person), so its active
        slot and the client's session quota are freed. One chat at a time, like a browser."""
        if not self.sid or (self.turns and self.turns[-1].get("done")):
            return
        try:
            r = self.say("I'd like to talk to a person", expect=None)
            if not r.get("done"):
                self.say("Roof / electrical / battery / other", expect=None)
        except Exception:  # noqa: BLE001 - best effort cleanup
            pass

    def _post(self, path: str, **kw) -> httpx.Response:
        for _ in range(20):
            r = self.http.post(path, **kw)
            if r.status_code == 429 and r.json().get("detail") not in ("upload_limit", "too_many_sessions"):
                time.sleep(float(r.headers.get("retry-after", "5")) + 0.5)
                continue
            return r
        return r

    def start(self) -> dict:
        r = self._post("/api/chat", json={"message": ""})
        r.raise_for_status()
        j = r.json()
        self.sid = j["session_id"]
        self.turns.append(j)
        return j

    def say(self, text: str = "", attachments: list[str] | None = None, pointer: dict | None = None,
            expect: int = 200) -> dict:
        body = {"session_id": self.sid, "message": text, "attachments": attachments or []}
        if pointer:
            body["pointer"] = pointer
        r = self._post("/api/chat", json=body)
        assert expect is None or r.status_code == expect, f"{text!r}: HTTP {r.status_code} {r.text[:200]}"
        j = r.json() if r.status_code == 200 else {"_status": r.status_code, **r.json()}
        self.turns.append(j)
        return j

    def upload(self, data: bytes, name: str = "photo.png", ctype: str = "image/png") -> httpx.Response:
        return self._post("/api/upload", data={"session_id": self.sid}, files={"file": (name, data, ctype)})

    def texts(self, turn: dict | None = None) -> list[str]:
        ts = [turn] if turn else self.turns
        return [m["text"] for t in ts for m in t.get("messages", [])]

    def steps(self) -> list[str]:
        return [t.get("await_step") for t in self.turns if t.get("await_step")]


def png(color=(120, 140, 90), size=(64, 48), exif_gps: bool = False) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    img = Image.new("RGB", size, color)
    if exif_gps:
        exif = Image.Exif()
        exif[0x8825] = {1: "N", 2: (27.0, 57.0, 0.0), 3: "W", 4: (82.0, 27.0, 0.0)}  # GPSInfo
        img.save(buf, "JPEG", exif=exif.tobytes())
    else:
        img.save(buf, "PNG")
    return buf.getvalue()


def last_email() -> dict:
    return json.loads(EMAILS.read_text(encoding="utf-8"))[-1]


def email_count() -> int:
    return len(json.loads(EMAILS.read_text(encoding="utf-8"))) if EMAILS.exists() else 0


def common(c: Chat, address="100 Solar Way, Tampa, FL 33601", name="Jane Doe",
           issue="Roof", urgency="6") -> dict:
    c.start()
    c.say(name)
    acct = c.say(address)
    c.say("813-555-0199")
    c.say(issue)
    c.say(urgency)
    c.say("skip")  # third parties
    return acct


def pointer_answer(c: Chat) -> dict:
    r = c.upload(png((200, 60, 60)), "pin.png")
    assert r.status_code == 200, r.text
    return c.say(r.json()["filename"], pointer={"lat": 27.9506, "lng": -82.4572})


# ---------------------------------------------------------------------------
# Flows (release test plan 5.3)
# ---------------------------------------------------------------------------
def F1(c: Chat):
    """Roof, actively leaking, map pin, 2 photos, confirm -> submitted; staff email complete."""
    n = email_count()
    acct = common(c, issue="Roof")
    assert FOUND_MESSAGE in c.texts(acct)
    c.say("Shingle"); c.say("Yes"); c.say("two days ago after the storm"); c.say("No"); c.say("Yes")
    assert c.turns[-1]["await_step"] == "damage_pointer"
    pointer_answer(c)
    c.say("Water drips through the bedroom ceiling near the chimney when it rains.")
    photos = [c.upload(png((i * 40, 90, 120))).json()["filename"] for i in range(2)]
    conf = c.say("", attachments=photos)
    assert conf["state"] == "confirm" and conf["summary"], conf
    rows = {r["label"]: r["value"] for r in conf["summary"]["rows"]}
    assert "Your name" not in rows and rows["Solar account name"] == "Jane Doe", rows
    assert rows.get("Photos") == "2 attached" and "Damage location" in rows, rows
    done = c.say("Yes, send it")
    assert done["done"] and done["outcome"] == "submitted", done
    assert email_count() == n + 1
    e = last_email()
    assert e["matched"] and "900001 (Verified Match)" in e["html"]
    for f in photos:
        assert f in e["html"], "photo missing from staff email"
    assert "Name on Account" in e["html"] and "Requester Name" not in e["html"]


def F2(c: Chat):
    """Roof, not leaking -> 'first noticed' is skipped."""
    common(c, issue="Roof")
    c.say("Tile"); c.say("No")
    assert c.turns[-1]["await_step"] == "pre_existing", c.turns[-1]["await_step"]
    assert "first_noticed" not in c.steps()


def F3(c: Chat):
    """Electrical end to end; matched -> disposition mailbox."""
    common(c, issue="Electrical", urgency="8")
    for a in ("Yes", "No", "No", "No", "Kitchen and garage",
              "The kitchen outlets and garage lights stopped working after the storm.", "skip"):
        c.say(a)
    done = c.say("yes")
    assert done["outcome"] == "submitted"
    assert last_email()["routed_to"] == "service-team@zeoenergy.com"


def F4(c: Chat):
    """Solar: deflection note uses 349-4057; callback detail only when requested."""
    common(c, issue="Solar not producing")
    c.say("Low production")
    note = c.say("none")
    assert any("349-4057" in t and "year" in t for t in c.texts(note)), c.texts(note)
    c.say("Production dropped by half since last month.")
    ask = c.say("Yes")
    assert ask["await_step"] == "callback_info"
    c.say("813-555-0199 after 3pm"); c.say("skip")
    done = c.say("yes")
    assert done["outcome"] == "submitted" and last_email()["routed_to"].startswith("performance")


def F4b(c: Chat):
    """Solar, no callback -> the callback question is skipped."""
    common(c, issue="Solar not producing")
    c.say("Completely offline"); c.say("E013"); c.say("System shows zero output today.")
    nxt = c.say("No")
    assert nxt["await_step"] == "_photos", nxt["await_step"]


def F5(c: Chat):
    """Other damage with the map pin."""
    common(c, address="302 Voltage St, Tampa, FL 33603", name="Laura Chen", issue="Misc / other")
    c.say("The backyard fence and gate"); c.say("A tree branch fell in the wind")
    pointer_answer(c)
    c.say("skip"); c.say("skip")
    done = c.say("yes")
    e = last_email()
    assert done["outcome"] == "submitted" and not e["matched"]
    assert "Account not located automatically" in e["html"] and "#900009" in e["html"], "staff hints missing"


def F6(c: Chat):
    """911 hazard at the start, mid-flow and at confirm -> emergency reply every time."""
    c.start()
    r = c.say("I smell gas in the house")
    assert r["state"] == "safety" and r["messages"][0]["kind"] == "safety"
    c2 = Chat(c.http); common(c2, issue="Electrical")
    r = c2.say("there are sparks and smoke coming from the panel")
    assert r["state"] == "safety"
    c3 = Chat(c.http); common(c3, issue="Electrical")
    for a in ("Yes", "No", "No", "No", "Kitchen", "Outlets dead after storm.", "skip"):
        c3.say(a)
    r = c3.say("wait, now there's a fire")
    assert r["state"] == "safety"


def F7(c: Chat):
    """'Talk to a person': no issue yet -> 0075 at once; solar -> 349-4057; others -> 0075."""
    n = email_count()
    c.start()
    d = c.say("can I talk to a real person")
    assert d["done"] and d["outcome"] == "handoff", d
    assert any(HANDOFF_OTHER in t for t in c.texts(d)) and not any(HANDOFF_SOLAR in t for t in c.texts(d))
    assert email_count() == n, "nothing collected yet -> no alert"
    c2 = Chat(c.http); common(c2, issue="Solar not producing")
    d = c2.say("I want to speak to a representative")
    assert d["outcome"] == "handoff" and any(HANDOFF_SOLAR in t for t in c2.texts(d))
    assert any("passed along" in t for t in c2.texts(d)), "customer told their details were passed on"
    e = last_email()
    assert e["subject"].startswith("[Incomplete]") and "asked to talk to a person" in e["html"]
    c3 = Chat(c.http); c3.start(); c3.say("Jane Doe"); c3.say("100 Solar Way, Tampa, FL 33601")
    d = c3.say("my battery backup is beeping, I need a human")
    assert d["outcome"] == "handoff" and any(HANDOFF_OTHER in t for t in c3.texts(d)), c3.texts(d)


def F8(c: Chat):
    """Non-answers x3: account name flagged unverified; issue type falls back to 'other' (A-05)."""
    c.start()
    for _ in range(3):
        c.say("idk")
    assert c.turns[-1]["await_step"] == "account_address"
    acct = c.say("123 Real Street, Tampa, FL")
    assert NOT_FOUND_MESSAGE in c.texts(acct)
    c.say("813-555-0199")
    for _ in range(3):
        r = c.say("hmm not sure")
    assert r["await_step"] == "urgency", r["await_step"]  # fell back to 'misc' and moved on
    for _ in range(3):
        r = c.say("dunno")
    assert r["await_step"] == "third_parties", r["await_step"]


def F9(c: Chat):
    """Confirm -> change -> correction -> summary -> yes; still matched (not re-run)."""
    common(c, issue="Electrical")
    for a in ("Yes", "No", "No", "No", "Kitchen", "Outlets dead after the storm.", "skip"):
        c.say(a)
    c.say("No, change something")
    again = c.say("The urgency should be 9, not 6")
    assert again["state"] == "confirm"
    done = c.say("Yes, send it")
    assert done["outcome"] == "submitted" and last_email()["matched"]
    assert "[Correction]" in last_email()["html"]


def F10(c: Chat):
    """Ambiguous reply at confirm -> asked again, nothing sent."""
    n = email_count()
    common(c, issue="Electrical")
    for a in ("Yes", "No", "No", "No", "Kitchen", "Outlets dead after the storm.", "skip"):
        c.say(a)
    r = c.say("maybe later")
    assert r["state"] == "confirm" and not r["done"] and email_count() == n


def F15(c: Chat):
    """Upload abuse: wrong type, oversize, GPS stripped, per-session cap."""
    c.start()
    assert c.upload(b"%PDF-1.7 not an image", "doc.jpg", "image/jpeg").status_code == 400
    big = png(size=(2600, 2600)) + b"\0" * (6 * 1024 * 1024)
    assert c.upload(big, "big.png").status_code in (400, 413)
    r = c.upload(png(exif_gps=True), "gps.jpg", "image/jpeg")
    assert r.status_code == 200, r.text
    from PIL import Image
    saved = Image.open(UPLOADS / r.json()["filename"])
    assert 0x8825 not in saved.getexif(), "GPS EXIF must be stripped"
    codes = [c.upload(png((i, i, i))).status_code for i in range(10)]
    assert codes.count(200) == 9 and codes[-1] == 429, codes  # 10 per session in total


def F16(c: Chat):
    """60-turn limit ends the chat with a handoff to a person."""
    c.start()
    last = {}
    for i in range(70):
        last = c.say("idk" if i % 2 else "hmm")
        if last.get("done"):
            break
    assert last.get("done") and any(HANDOFF_OTHER in t or HANDOFF_SOLAR in t for t in c.texts(last)), last


def F17(_c: Chat):
    """No-send proof: every handoff went to the inbox store, never to Gmail or Chat."""
    log = (ROOT / "mock" / "logs" / "account.log").read_text(encoding="utf-8", errors="replace")
    assert "Inbox(no-send)" in log, "expected inbox-only deliveries"
    for bad in ("Gmail send", "webhook POST", "gmail.googleapis"):
        assert bad not in log, f"found {bad!r} in the account service log"


def F18(c: Chat):
    """Escalated customer: frustration acknowledged once with the number; then a
    manager demand hands off with an apology; staff alert flags the upset customer."""
    c.start(); c.say("Jane Doe"); c.say("100 Solar Way, Tampa, FL 33601"); c.say("813-555-0199")
    r = c.say("This is ridiculous, I've called three times about my roof leaking")
    notice = [m for m in r["messages"] if m["kind"] == "notice"]
    assert notice and HANDOFF_OTHER in notice[0]["text"] and not r.get("done"), c.texts(r)
    assert r["await_step"] == "urgency", "the roof answer in the same message still counts"
    r = c.say("I want to speak with a manager right now")
    assert r["outcome"] == "handoff" and any("sorry" in t.lower() and HANDOFF_OTHER in t for t in c.texts(r))
    e = last_email()
    assert "Customer may be upset" in e["html"] and e["subject"].startswith("[Incomplete]")


def F19(c: Chat):
    """Messy answers: ordinary words that used to trigger a handoff now don't."""
    c.start(); c.say("Jane Doe"); c.say("100 Solar Way, Tampa, FL 33601")
    r = c.say("my phone number is 813-555-0199")
    assert not r.get("done") and r["await_step"] == "issue_type", r
    c.say("Roof"); c.say("7")
    r = c.say("yes, my insurance agent came out yesterday")
    assert not r.get("done") and r["await_step"] == "roof_type", r


FLOWS = [F1, F2, F3, F4, F4b, F5, F6, F7, F8, F9, F10, F15, F16, F17, F18, F19]


# ---------------------------------------------------------------------------
# Account scenarios (release test plan 5.4)
# ---------------------------------------------------------------------------
def account_scenarios(http: httpx.Client) -> list[str]:
    bad = []
    for s in SCENARIOS:
        c = Chat(http)
        c.start()
        c.say(s["name"])
        r = c.say(s["address"])
        want = FOUND_MESSAGE if s["expect"] else NOT_FOUND_MESSAGE
        notice = [m for m in r.get("messages", []) if m.get("kind") == "notice"]
        if not notice or notice[0]["text"] != want:
            bad.append(f"{s['id']} {s['what']}: got {[m['text'][:40] for m in r.get('messages', [])]}")
        c.close()
        OPEN.clear()
    return bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8080")
    ap.add_argument("--only", default="")
    ap.add_argument("--skip-accounts", action="store_true")
    args = ap.parse_args()
    http = httpx.Client(base_url=args.base, timeout=40)
    assert http.get("/api/health").status_code == 200, "mock is not running/healthy"
    only = {x.strip() for x in args.only.split(",") if x.strip()}
    results = []
    for f in FLOWS:
        if only and f.__name__ not in only:
            continue
        t0 = time.monotonic()
        try:
            try:
                f(Chat(http))
            finally:
                while OPEN:
                    OPEN.pop().close()
            results.append((f.__name__, True, f"{time.monotonic() - t0:5.1f}s", f.__doc__.strip().splitlines()[0]))
        except AssertionError as exc:
            results.append((f.__name__, False, f"{time.monotonic() - t0:5.1f}s", str(exc)[:220]))
        print(("PASS " if results[-1][1] else "FAIL ") + f"{results[-1][0]:<4} {results[-1][2]}  {results[-1][3]}",
              flush=True)
    if not args.skip_accounts and not only:
        t0 = time.monotonic()
        bad = account_scenarios(http)
        results.append(("ACCT", not bad, f"{time.monotonic() - t0:5.1f}s", "; ".join(bad) or f"{len(SCENARIOS)} scenarios"))
        print(("PASS " if not bad else "FAIL ") + f"ACCT {results[-1][2]}  {results[-1][3]}", flush=True)
    failed = [r for r in results if not r[1]]
    print(f"\nreal_flows: {len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
