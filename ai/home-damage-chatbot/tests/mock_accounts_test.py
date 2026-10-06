"""Mock accounts: every cheat-sheet scenario behaves as documented.

The scenarios in backend/fixtures/mock_projects.json are what testers type on the
mock page (docs/MOCK_ACCOUNTS.md). Each one is checked twice:
  * unit: account_match picks exactly the expected project (or none)
  * end to end: through /api/chat, the customer sees the matching fixed message
Also guards the fixture itself: synthetic ids only, no duplicate ids.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("GEOCODER", "none")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="chatbot-test-"))
os.environ.pop("ENABLE_STAFF_API", None)
os.environ["USE_MOCK_LLM"] = "1"
os.environ["CRM_BACKEND"] = "stub"
os.environ["RATE_LIMIT_REQUESTS"] = "1000"  # 18 scenarios x 3 turns from one test client
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from _session_client import SessionClient  # noqa: E402

from backend import account_gateway, account_match, crm  # noqa: E402
from backend.main import app  # noqa: E402

DATA = crm.load_mock_projects()
c = SessionClient(app)


def test_fixture_is_synthetic():
    print("Fixture: synthetic 9000xx ids, unique, every scenario target exists...")
    ids = [p["project_id"] for p in DATA["projects"]]
    assert len(ids) == len(set(ids)), "duplicate project ids"
    assert all(i.startswith("9000") for i in ids), "mock ids must stay in the 9000xx range"
    for s in DATA["scenarios"]:
        assert s["expect"] is None or s["expect"] in ids, s
    print(f"  ok: {len(ids)} projects, {len(DATA['scenarios'])} scenarios")


def test_scenarios_unit():
    print("Scenarios (unit): expected project or none...")
    bad = []
    for s in DATA["scenarios"]:
        o = account_match.run(s["name"], s["address"])
        got = o.match.account_number if o.match else None
        if got != s["expect"]:
            bad.append(f"{s['id']} {s['what']}: expected {s['expect']}, got {got} ({o.reason})")
    assert not bad, "\n    " + "\n    ".join(bad)
    print("  ok")


def test_scenarios_through_chat():
    print("Scenarios (chat): the customer sees the matching fixed message...")
    bad = []
    for s in DATA["scenarios"]:
        label = f"mock_{s['id']}"
        c.post("/api/chat", json={"session_id": label, "message": ""})
        c.post("/api/chat", json={"session_id": label, "message": s["name"]})
        r = c.post("/api/chat", json={"session_id": label, "message": s["address"]}).json()
        want = account_gateway.FOUND_MESSAGE if s["expect"] else account_gateway.NOT_FOUND_MESSAGE
        texts = [m["text"] for m in r.get("messages", [])]
        if want not in texts:
            bad.append(f"{s['id']} {s['what']}: got {texts[:1]}")
    assert not bad, "\n    " + "\n    ".join(bad)
    print("  ok")


if __name__ == "__main__":
    tests = [test_fixture_is_synthetic, test_scenarios_unit, test_scenarios_through_chat]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL {t.__name__}: {exc}")
    print(f"\nmock_accounts_test: {len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
