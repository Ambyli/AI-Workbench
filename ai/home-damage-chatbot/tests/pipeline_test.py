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
os.environ["RATE_LIMIT_REQUESTS"] = "1000"     # roomy for the multi-step flows
os.environ["RATE_LIMIT_WINDOW_SECONDS"] = "60"
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from fastapi.testclient import TestClient

from backend import crm, email_render, seed
from backend.config import settings
from backend.main import app

email_render._STORE.clear()
seed.seed_if_empty()
email_render._persist()

c = TestClient(app)
c.__enter__()


def chat(sid, msg="", mode="standard"):
    return c.post("/api/chat", json={"session_id": sid, "mode": mode, "message": msg}).json()


def test_security_headers():
    print("Testing security headers...")
    r = c.get("/api/health")
    h = r.headers
    assert h.get("x-content-type-options") == "nosniff"
    assert h.get("x-frame-options") == "DENY"
    assert "content-security-policy" in h
    assert "referrer-policy" in h
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
    # Name + address -> confident match above threshold.
    m = crm.find_account("Jane Doe", "100 Solar Way, Tampa, FL 33601")
    assert m is not None and m.confidence >= settings.CRM_MATCH_THRESHOLD
    # Name alone -> below threshold -> no match (anti-enumeration).
    assert crm.find_account("Jane Doe", "") is None
    # Wrong everything -> no match.
    assert crm.find_account("Nobody Atall", "1 Nowhere Rd") is None
    print(f"  ✓ name+address conf={m.confidence}; name-only rejected")


def test_unconfirmed_identity_flag():
    print("Testing unconfirmed-identity flag...")
    sid = "unconf"
    chat(sid)                        # greeting -> your name
    chat(sid, "Jordan Rivers")       # name ok -> solar account name
    chat(sid, "idk")                 # account_name retry 1
    chat(sid, "idk")                 # account_name retry 2
    r = chat(sid, "idk")             # exhausted -> account_name flagged, advance to account address
    assert any("address" in m["text"].lower() for m in r["messages"]), \
        "after exhausting retries the flow should advance to the account address"
    # Finish a minimal misc flow.
    chat(sid, "123 Real Street, Tampa")  # account address
    chat(sid, "555-1234567")             # contact
    chat(sid, "Misc")
    chat(sid, "5")
    chat(sid, "skip")                # third parties
    chat(sid, "the backyard fence")  # what_damaged (>= 2 words)
    chat(sid, "skip")                # cause
    chat(sid, "damage_pointer_test.png") # damage_pointer
    chat(sid, "skip")                # additional
    chat(sid, "skip")                # photos
    done = chat(sid, "yes")
    assert done["done"] and done["email_id"]
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
