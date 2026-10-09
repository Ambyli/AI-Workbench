"""Tests for the hardening + disposition-pipeline additions (mock LLM).

Covers the surfaces not exercised by smoke_test.py / security_test.py:
  * rate limiting (HTTP 429)
  * security response headers
  * the unconfirmed-identity flag flowing into the rendered handoff email
  * CRM confidence scoring + threshold
"""
import os
import sys
from pathlib import Path

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["USE_MOCK_LLM"] = "1"
# This suite exercises the internal staff/demo API (inbox, CRM, lookup), which is
# off by default (S-C1). Opt in explicitly.
os.environ["ENABLE_STAFF_API"] = "true"
os.environ["RATE_LIMIT_REQUESTS"] = "1000"     # roomy for the multi-step flows
os.environ["RATE_LIMIT_WINDOW_SECONDS"] = "60"
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from fastapi.testclient import TestClient
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _session_client import SessionClient

from backend import crm, email_render, seed
from backend.config import settings
from backend.main import app

email_render._STORE.clear()
seed.seed_if_empty()
email_render._persist()

c = SessionClient(app)  # contract v2: server-issued sessions
c.__enter__()


def chat(sid, msg="", mode="standard"):
    return c.post("/api/chat", json={"session_id": sid, "mode": mode, "message": msg}).json()


def test_security_headers():
    print("Testing security headers...")
    r = c.get("/api/health")
    h = r.headers
    assert h.get("x-content-type-options") == "nosniff", dict(h)
    assert h.get("x-frame-options") == "DENY", dict(h)
    assert "content-security-policy" in h, dict(h)
    assert "referrer-policy" in h, dict(h)
    print("  ✓ security headers present")


def test_rate_limit():
    print("Testing rate limiting...")
    import backend.ratelimit as rl
    rl._HITS.clear()                      # fresh window for a deterministic count
    os.environ["RATE_LIMIT_REQUESTS"] = "3"   # settings reads env live
    try:
        codes = [c.post("/api/chat", json={"session_id": "rl", "message": ""}).status_code
                 for _ in range(6)]
    finally:
        os.environ["RATE_LIMIT_REQUESTS"] = "1000"
    assert 429 in codes, f"expected a 429 within {codes}"
    assert codes.count(200) <= 3, f"more 200s than the budget allowed: {codes}"
    print(f"  ✓ rate limiter tripped (codes={codes})")


def test_crm_confidence():
    print("Testing CRM confidence scoring...")
    from backend import account_match
    # Name + address -> every gate passes.
    o = account_match.run("Jane Doe", "100 Solar Way, Tampa, FL 33601")
    assert o.matched and o.match.account_number == "900001", f"outcome={o}"
    # Name alone -> no match (anti-enumeration).
    assert not account_match.run("Jane Doe", "").matched
    # Wrong everything -> no match.
    assert not account_match.run("Nobody Atall", "1 Nowhere Rd").matched
    print(f"  ✓ name+address conf={o.match.confidence}; name-only rejected")


def test_unconfirmed_identity_flag():
    print("Testing unconfirmed-identity flag...")
    sid = "unconf"
    chat(sid)                        # greeting -> solar account name
    chat(sid, "idk")                 # account_name retry 1
    chat(sid, "idk")                 # account_name retry 2
    chat(sid, "idk")                 # exhausted -> flagged, advance to the account address
    r = chat(sid, "123 Real Street, Tampa")  # account address -> account check runs, advance to contact
    assert any(m["text"].startswith("We were unable to locate your account") for m in r["messages"]), \
        "an unverifiable account name should still get the fixed not-found message"
    assert any("best contact" in m["text"].lower() for m in r["messages"]), \
        "after exhausting retries the flow should advance to the best contact"
    # Finish a minimal misc flow.
    chat(sid, "555-1234567")             # contact
    chat(sid, "Misc")
    chat(sid, "5")
    chat(sid, "skip")                # third parties
    chat(sid, "the backyard fence")  # what_damaged (>= 2 words)
    chat(sid, "skip")                # cause
    chat(sid, c.pointer_image(sid)) # damage_pointer
    chat(sid, "skip")                # additional
    chat(sid, "skip")                # photos
    done = chat(sid, "yes")
    assert done["done"] and done.get("email_id"), f"unexpected final turn: {done}"
    rec = c.get("/api/emails/" + done["email_id"]).json()
    assert "Unverified fields" in rec["html"] and "Solar account name" in rec["html"], \
        "unconfirmed account name should be flagged in the handoff email"
    print("  ✓ unconfirmed account name surfaced in the handoff email")


if __name__ == "__main__":
    try:
        test_security_headers()
        test_crm_confidence()
        test_unconfirmed_identity_flag()
        test_rate_limit()   # last: it exhausts the shared rate-limit budget
        print("\nALL PIPELINE/HARDENING CHECKS PASSED")
    except AssertionError as e:
        print(f"\nCHECK FAILED: {e}")
        sys.exit(1)
