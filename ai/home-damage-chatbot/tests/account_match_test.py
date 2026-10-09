"""Account check: gates, parsing, and the model-blindness layer.

The account check (backend/account_match.py) must be code-only:
  B1  static: the matching code never imports the LLM client, and the LLM client
      never imports the matching code, the CRM adapter or the SQL
  B2  dynamic: across a full real-model-path conversation (vLLM transport faked),
      no request to the model contains a query, candidate, score, account id,
      CRM-stored value, or the found/not-found outcome
  B3  the customer sees only one of two fixed messages; API responses never carry
      account details, and a found/not-found turn has the same shape
  B4  one search per conversation (corrections at confirm do not re-run it)
  B5  the phoenix client sends customer text only as bound parameters, in a
      read-only transaction
Plus the match gates, the address parser and the fail-closed error path.
"""
from __future__ import annotations

import ast
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("GEOCODER", "none")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="chatbot-test-"))
os.environ.pop("ENABLE_STAFF_API", None)
os.environ["USE_MOCK_LLM"] = "1"
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from _session_client import SessionClient  # noqa: E402

from backend import account_gateway, account_match, address_parse, crm, llm  # noqa: E402
from backend.main import app  # noqa: E402
from backend.schemas import AccountCandidate  # noqa: E402

c = SessionClient(app)

# Values that exist only in the CRM (stub fixture), never typed by the customer: Jane
# Doe's project id and stored street ("Solar Wy" is typed), and another project that
# shows up only as a staff-side candidate hint for this address.
CRM_ONLY = ["900001", "Solar Way", "900015", "Wattage"]
INTERNALS = ["fuzzy", "phoenix", "project_id", "address_score", "name_score", "similarity",
             "SELECT", "_acct"]
OUTCOME_TEXT = [account_gateway.FOUND_MESSAGE, account_gateway.NOT_FOUND_MESSAGE,
                "found your account", "locate your account"]

ELECTRICAL_TAIL = ["Electrical", "7", "skip", "Yes", "No", "No", "No", "kitchen",
                   "the kitchen outlets stopped working", "skip"]


def converse(label: str, answers: list[str]) -> list[dict]:
    out = [c.post("/api/chat", json={"session_id": label, "message": ""}).json()]
    for a in answers:
        r = c.post("/api/chat", json={"session_id": label, "message": a})
        assert r.status_code == 200, (a, r.status_code, r.text)
        out.append(r.json())
    return out


# ---------------------------------------------------------------------------
# B1 — static isolation
# ---------------------------------------------------------------------------
def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
    return names


def test_static_isolation():
    print("B1: matching code and LLM client never import each other...")
    for f in ("account_match.py", "crm.py", "address_parse.py", "trgm.py", "lookup.py"):
        bad = {n for n in _imports(ROOT / "backend" / f) if n.split(".")[-1] == "llm"}
        assert not bad, f"backend/{f} must not import the LLM client: {bad}"
    llm_imports = {n.split(".")[-1] for n in _imports(ROOT / "backend" / "llm.py")}
    forbidden = {"crm", "account_match", "lookup", "address_parse", "trgm", "pipeline", "state_machine"}
    assert not (llm_imports & forbidden), f"llm.py must not import {llm_imports & forbidden}"
    llm_src = (ROOT / "backend" / "llm.py").read_text(encoding="utf-8")
    assert "sql" not in llm_src.lower().replace("sqlite", ""), "llm.py must not reference the SQL"
    print("  ok")


# ---------------------------------------------------------------------------
# B2 — dynamic: nothing from the account check reaches the model
# ---------------------------------------------------------------------------
class _FakeResponse:
    def __init__(self, body: dict):
        self._body = body

    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"content": json.dumps(self._body)}}]}


def test_model_never_sees_account_check():
    print("B2: full conversation on the real-model code path; capture every model request...")
    sent: list[str] = []

    def fake_post(url, json=None, headers=None, timeout=None):  # noqa: A002 - httpx signature
        sent.append(__import__("json").dumps(json))
        system = json["messages"][0]["content"]
        user = json["messages"][1]["content"]
        if "extraction" in system:
            ftype = re.search(r"\(type: ([a-z_0-9]+)\)", user).group(1)
            msg = re.search(r"<customer_message>\n(.*)\n</customer_message>", user, re.S).group(1)
            opts = re.search(r"Choose exactly one of: (\[.*?\])\.", user)
            options = ast.literal_eval(opts.group(1)) if opts else None
            return _FakeResponse({"value": llm._mock_extract(ftype, msg, options)})
        return _FakeResponse({"responsive": True, "confidence": 0.95})

    saved = os.environ.pop("USE_MOCK_LLM")
    os.environ["LLM_BACKEND"] = "vllm"
    try:
        with patch.object(llm, "is_backend_ready", return_value=True), \
             patch.object(llm.httpx, "post", side_effect=fake_post):
            assert not llm.using_mock(), "test must exercise the real-model code path"
            turns = converse("blind1", ["Jane Doe", "100 Solar Wy, Tampa FL 33601",
                                        "813-555-0199", *ELECTRICAL_TAIL, "yes"])
    finally:
        os.environ["USE_MOCK_LLM"] = saved
        os.environ.pop("LLM_BACKEND", None)

    assert turns[-1]["done"], "conversation should complete"
    assert any(account_gateway.FOUND_MESSAGE == m["text"] for t in turns for m in t["messages"]), \
        "precondition: this conversation's account check should match"
    assert len(sent) >= 10, f"expected many model calls, saw {len(sent)}"
    blob = "\n".join(sent)
    for s in CRM_ONLY + INTERNALS + OUTCOME_TEXT:
        assert s.lower() not in blob.lower(), f"model request leaked {s!r}"
    print(f"  ok: {len(sent)} model requests, none contain account-check data")


# ---------------------------------------------------------------------------
# B3 — customer-facing: fixed messages only, no details, same shape
# ---------------------------------------------------------------------------
def test_customer_sees_only_fixed_messages():
    print("B3: responses carry no account details; found/not-found turns look alike...")
    found = converse("blind_found", ["Jane Doe", "100 Solar Way, Tampa, FL 33601"])
    missed = converse("blind_missed", ["Pat Tester", "500 Test Blvd, Tampa, FL"])
    for turns in (found, missed):
        blob = json.dumps(turns)
        for s in [x for x in CRM_ONLY if x != "Solar Way"] + INTERNALS:  # "Solar Way" is typed here
            assert s.lower() not in blob.lower(), f"response leaked {s!r}"
    f_turn, m_turn = found[-1], missed[-1]
    assert f_turn["messages"][0]["text"] == account_gateway.FOUND_MESSAGE
    assert m_turn["messages"][0]["text"] == account_gateway.NOT_FOUND_MESSAGE
    assert set(f_turn) == set(m_turn), "found and not-found turns must have the same keys"
    strip = lambda t: {k: v for k, v in t.items() if k not in ("session_id", "messages")}  # noqa: E731
    assert strip(f_turn) == strip(m_turn), "only the message text may differ"
    assert [m["kind"] for m in f_turn["messages"]] == [m["kind"] for m in m_turn["messages"]]
    print("  ok")


# ---------------------------------------------------------------------------
# B4 — one search per conversation
# ---------------------------------------------------------------------------
class _CountingClient(crm.StubCRMClient):
    calls = {"address": 0, "name": 0}

    def search_by_address(self, street, city, zip5):
        self.calls["address"] += 1
        return super().search_by_address(street, city, zip5)

    def search_by_name(self, name, city):
        self.calls["name"] += 1
        return super().search_by_name(name, city)


def test_one_search_per_conversation():
    print("B4: corrections at confirm do not re-run the account search...")
    _CountingClient.calls = {"address": 0, "name": 0}
    with patch.object(crm, "get_client", return_value=_CountingClient()):
        turns = converse("once", ["Jane Doe", "100 Solar Way, Tampa, FL 33601",
                                  "813-555-0199", *ELECTRICAL_TAIL,
                                  "No, change something", "my account name is Jane Q Doe", "yes"])
    assert turns[-1]["done"]
    assert _CountingClient.calls == {"address": 1, "name": 1}, _CountingClient.calls
    print("  ok:", _CountingClient.calls)


# ---------------------------------------------------------------------------
# B5 — phoenix client: bound parameters, read-only transaction
# ---------------------------------------------------------------------------
class _FakeCursor:
    def __init__(self, log):
        self.log = log
        self.description = [("project_id",), ("project_name",), ("street1",), ("city",),
                            ("postal_code",), ("state",), ("fuzzy_score",)]

    def execute(self, sql, params=None):
        self.log.append((sql, params))

    def fetchall(self):
        return [(42, "Jane Doe", "100 Solar Way", "Tampa", "33601", "FL", 97.5)]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, log):
        self.log = log

    def cursor(self):
        return _FakeCursor(self.log)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_phoenix_client_binds_parameters():
    print("B5: phoenix client uses bound parameters in a read-only transaction...")
    log: list = []
    client = crm.PhoenixSQLClient(connect=lambda: _FakeConn(log))
    evil = "100 Solar Way'; DROP TABLE phoenix.project; --"
    rows = client.search_by_address(evil, "Tampa", "33601")
    assert log[0][0] == "SET TRANSACTION READ ONLY"
    assert log[1][0].startswith("SET LOCAL statement_timeout")
    sql, params = log[2]
    assert sql == crm.PhoenixSQLClient._ADDRESS_SQL, "SQL text must be the fixed file"
    assert evil not in sql and params == {"street": evil, "city": "Tampa", "zip": "33601"}
    assert rows[0].project_id == "42" and rows[0].fuzzy_score == 97.5
    log.clear()
    client.search_by_name("Jane Doe", "Tampa")
    assert log[2] == (crm.PhoenixSQLClient._NAME_SQL, {"name": "Jane Doe", "city": "Tampa"})
    print("  ok")


# ---------------------------------------------------------------------------
# Gates, parser, errors
# ---------------------------------------------------------------------------
def _cand(pid, street, score, name="Jane Doe", zip5="33601"):
    return AccountCandidate(project_id=pid, project_name=name, street1=street, city="Tampa",
                            postal_code=zip5, state="FL", fuzzy_score=score)


class _FixedClient:
    def __init__(self, by_address, by_name):
        self.a, self.n = by_address, by_name

    def search_by_address(self, *a):
        return self.a

    def search_by_name(self, *a):
        return self.n


def test_gates():
    print("Gates: exact number, street name, name floor, ambiguity...")
    run = account_match.run
    addr = "100 Solar Way, Tampa, FL 33601"
    assert run("Jane Doe", addr).matched
    assert run("Jane Doe", "100 Solar Wy Tampa FL").matched, "suffix typo should still match"
    o = run("Jane Doe", "101 Solar Way, Tampa, FL 33601")
    assert not o.matched and o.reason == "below_threshold", "wrong house number must not match"
    assert not run("Jane Doe", "100 Main St, Tampa, FL 33601").matched, "same number, other street"
    assert not run("Nobody Atall", addr).matched, "address alone must not match"
    assert not run("Jane Doe", "").matched and run("Jane Doe", "somewhere").reason == "no_address"
    # Two different homes, both passing and within the margin -> nobody is matched.
    two_homes = _FixedClient([_cand("1", "100 Solar Way", 90), _cand("2", "100 Solar Way", 88, zip5="33602")],
                             [_cand("1", "", 90), _cand("2", "", 89)])
    assert run("Jane Doe", addr, client=two_homes).reason == "ambiguous"
    # Two projects on the same home (e.g. a re-install) -> still a match, best first.
    same_home = _FixedClient([_cand("1", "100 Solar Way", 90), _cand("2", "100 Solar Way", 89)],
                             [_cand("1", "", 90), _cand("2", "", 90)])
    o = run("Jane Doe", addr, client=same_home)
    assert o.matched and o.match.account_number == "1"
    print("  ok")


def test_search_error_fails_closed():
    print("Errors: a CRM failure is 'not found' and the chat continues...")

    class Boom:
        def search_by_address(self, *a):
            raise ConnectionError("db down")

        def search_by_name(self, *a):
            raise ConnectionError("db down")

    o = account_match.run("Jane Doe", "100 Solar Way, Tampa, FL 33601", client=Boom())
    assert not o.matched and o.reason == "error"
    with patch.object(crm, "get_client", return_value=Boom()):
        turns = converse("dbdown", ["Jane Doe", "100 Solar Way, Tampa, FL 33601"])
    assert turns[-1]["messages"][0]["text"] == account_gateway.NOT_FOUND_MESSAGE
    assert turns[-1]["state"] == "collect", "the chat must continue to the next question"
    print("  ok")


def test_address_parser():
    print("Parser: street / city / ZIP from typical inputs...")
    p = address_parse.parse
    assert p("100 Solar Way, Tampa, FL 33601") == address_parse.AddressParts(
        "100 Solar Way", "100", "Tampa", "FL", "33601")
    q = p("742 Evergreen Terrace Springfield OR 97477-1234")
    assert (q.street, q.city, q.zip) == ("742 Evergreen Terrace", "Springfield", "97477")
    q = p("12 Oak St Apt 4, Tampa")
    assert (q.street, q.number, q.city) == ("12 Oak St", "12", "Tampa")
    assert p("somewhere in Florida").number == ""
    assert address_parse.street_similarity("100 Main St", "100 Oak St") < 0.3
    assert address_parse.street_similarity("100 Main Street", "100 Main St") == 1.0
    print("  ok")


if __name__ == "__main__":
    tests = [test_static_isolation, test_address_parser, test_gates, test_search_error_fails_closed,
             test_phoenix_client_binds_parameters, test_customer_sees_only_fixed_messages,
             test_one_search_per_conversation, test_model_never_sees_account_check]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL {t.__name__}: {exc}")
    print(f"\naccount_match_test: {len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
