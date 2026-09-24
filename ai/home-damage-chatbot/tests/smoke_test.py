"""Quick end-to-end smoke test (mock LLM). Not part of the app."""
import os
import sys
from pathlib import Path

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["USE_MOCK_LLM"] = "1"
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from fastapi.testclient import TestClient
from backend.main import app
from backend import email_render, seed

# Clear existing persisted/in-memory data for a clean test run
email_render._STORE.clear()
seed.seed_if_empty()
email_render._persist()

c = TestClient(app)
c.__enter__()


def chat(sid, mode, msg="", atts=None):
    r = c.post("/api/chat", json={"session_id": sid, "mode": mode, "message": msg,
                                   "attachments": atts or []})
    r.raise_for_status()
    d = r.json()
    last = d["messages"][-1]["text"][:70] if d["messages"] else ""
    print(f"  [{d['state']:8}] said={msg!r:35} bot={last!r} qr={d['quick_replies']}")
    return d


print("=== Standard mode: Roof flow ===")
chat("t1", "standard")  # greeting
chat("t1", "standard", "Maria Tester")            # your name
chat("t1", "standard", "Maria Tester")            # solar account name
chat("t1", "standard", "100 Test St, Tampa, FL")  # solar account address
chat("t1", "standard", "555-100-2000")            # best contact
chat("t1", "standard", "Roof")
chat("t1", "standard", "8")
chat("t1", "standard", "skip")
chat("t1", "standard", "shingle")            # roof_type
chat("t1", "standard", "Yes")            # leaking
chat("t1", "standard", "last night")     # first noticed
chat("t1", "standard", "No")             # pre-existing
chat("t1", "standard", "Yes")            # attic
chat("t1", "standard", "damage_pointer_test.png")
chat("t1", "standard", "water is dripping from the ceiling onto the floor")
chat("t1", "standard", "skip")           # photos
final = chat("t1", "standard", "Yes, send it")
assert final["done"] and final["email_id"], "standard flow should finish with an email"
print("  -> email_id:", final["email_id"])

print("\n=== Account Lookup mode: known customer ===")
chat("t2", "lookup")
chat("t2", "lookup", "Jane Doe")                     # your name
chat("t2", "lookup", "Jane Doe")                     # solar account name
chat("t2", "lookup", "100 Solar Way, Tampa, FL 33601")  # solar account address
chat("t2", "lookup", "jane.doe@example.com")         # best contact
chat("t2", "lookup", "Electrical")
chat("t2", "lookup", "9")
chat("t2", "lookup", "skip")
chat("t2", "lookup", "Yes")              # without power
chat("t2", "lookup", "Yes")              # breakers
chat("t2", "lookup", "No")               # neighbors_without_power
chat("t2", "lookup", "No")               # recent_storm
chat("t2", "lookup", "whole home")
chat("t2", "lookup", "the panel popped and went dark")
lk = chat("t2", "lookup", "skip")        # -> lookup runs, then confirm
assert any("found your account" in m["text"] for m in lk["messages"]), "should match Jane Doe"
done2 = chat("t2", "lookup", "yes")
assert done2["done"] and done2["email_id"]
# Matched requester -> CRM case opened, routed to the disposition mailbox.
m2 = c.get("/api/emails/" + done2["email_id"]).json()
assert m2["matched"] is True, "Jane Doe should match an account"
assert m2["case_id"], "a matched account should open a CRM case for disposition"
assert m2["routed_to"] == "service-team@zeoenergy.com", "matched -> disposition mailbox"
print("  -> matched, case:", m2["case_id"], "routed_to:", m2["routed_to"])

# Standard 'Maria Tester' from the first flow did NOT match -> unverified routing.
u1 = c.get("/api/emails/" + final["email_id"]).json()
assert u1["matched"] is False, "Maria Tester is not a real account"
assert u1["case_id"] is None, "no CRM case when there's no confident match"
assert u1["routed_to"] == "intake-review@zeoenergy.com", "no match -> unverified mailbox"
print("  -> unmatched routed_to:", u1["routed_to"])

print("\n=== Answer-validation gate ===")
# Non-answer to a required field must re-prompt, not advance.
chat("t4", "standard")
nv = chat("t4", "standard", "idk")
assert nv["state"] == "collect" and any("name" in m["text"].lower() for m in nv["messages"]), \
    "non-answer to name should re-prompt, not advance"
chat("t4", "standard", "Jordan Rivers")   # valid requester name -> account name
chat("t4", "standard", "Jordan Rivers")   # valid account name -> account address
# Account address with no street number must re-prompt.
bad_addr = chat("t4", "standard", "somewhere in town")
assert bad_addr["state"] == "collect" and any("address" in m["text"].lower() for m in bad_addr["messages"]), \
    "account address without a street number should re-prompt"
ok_addr = chat("t4", "standard", "742 Evergreen Ter, Springfield")
assert not any("street number" in m["text"].lower() for m in ok_addr["messages"]), \
    "valid account address should be accepted"
print("  validation gate re-prompts on non-answers and bad addresses")

print("\n=== Confirm-phase ambiguity ===")
# Drive a quick misc flow to the confirm step, then reply ambiguously.
chat("t5", "standard")
chat("t5", "standard", "Avery Stone")            # your name
chat("t5", "standard", "Avery Stone")            # solar account name
chat("t5", "standard", "55 Oak Street, Tampa")   # solar account address
chat("t5", "standard", "avery@example.com")      # best contact
chat("t5", "standard", "Misc")
chat("t5", "standard", "4")
chat("t5", "standard", "skip")
chat("t5", "standard", "the garage door panel")   # what_damaged (required)
chat("t5", "standard", "a falling branch")        # cause
chat("t5", "standard", "damage_pointer_test.png") # damage_pointer
chat("t5", "standard", "skip")                    # additional details
amb = chat("t5", "standard", "maybe later")       # ambiguous at confirm
assert amb["state"] == "confirm", "ambiguous confirm reply should stay at confirm, not enter correction"
print("  ambiguous confirm reply stays at confirm")

print("\n=== Safety check mid-flow ===")
chat("t3", "standard")
chat("t3", "standard", "John Doe")
s = chat("t3", "standard", "I smell gas in the house")
assert s["state"] == "safety" and "911" in s["messages"][0]["text"]

print("\n=== Database endpoints ===")
cust = c.get("/api/customers").json()
print("  customers:", len(cust), "| first:", cust[0]["account_number"], cust[0]["name"])
assert "phone" not in cust[0], "phone must not be exposed in the table"

for body, expect in [
    ({"name": "Jane Doe", "address": "100 Solar Way, Tampa"}, "match"),
    ({"name": "Jane Doe"}, "need"),
    ({"name": "Nobody Here", "email": "x@y.com"}, "nomatch"),
]:
    res = c.post("/api/lookup", json=body).json()
    print(f"  lookup {body} -> {res['kind']} ({res['title']})")
    assert res["kind"] == expect, f"expected {expect}, got {res['kind']}"

print("\n=== Emails ===")
emails = c.get("/api/emails").json()
print("  total emails:", len(emails), "(4 seeded + 2 generated)")
assert len(emails) == 6, "expected 4 seeded + 2 generated"
detail = c.get("/api/emails/" + emails[0]["id"]).json()
assert detail["html"].lstrip().startswith("<div"), "email html should render"
assert "<script" not in detail["html"].lower(), "no scripts in rendered email"
print("  newest subject:", emails[0]["subject"])
print("\nALL CHECKS PASSED")
