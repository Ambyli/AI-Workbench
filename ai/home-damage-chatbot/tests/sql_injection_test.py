"""SQL injection: customer text can only reach the CRM queries as bound data.

The account name and address are the only customer input that reaches SQL (the
phoenix account searches, backend/sql/*.sql via crm.PhoenixSQLClient).

  Q1  static: the SQL files are fixed text whose only placeholders are the four named
      bound parameters; '%' is escaped; no string formatting can splice input in;
      the one f-string in the client (the statement timeout) is an int from config
  Q2  runtime: 13 classic payloads in the account name and the address through the
      real matcher + phoenix client (recording connection). Every executed statement
      is one of the fixed texts, the payload appears only inside the parameters dict,
      and the ZIP parameter is digits only (no LIKE wildcards)
  Q3  end to end: the same payloads typed into the chat never break the conversation;
      the customer just gets "not found"
"""
from __future__ import annotations

import ast
import os
import re
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
os.environ["RATE_LIMIT_REQUESTS"] = "100000"
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from backend import account_match, crm  # noqa: E402

PAYLOADS = [
    "Robert'); DROP TABLE phoenix.project;--",
    "' OR '1'='1",
    "' OR 1=1 --",
    "\\'; SELECT pg_sleep(10);--",
    "%' OR project_name ILIKE '%",
    "1; UPDATE phoenix.project SET archived = true",
    "Jane' UNION SELECT id, street1, city FROM phoenix.project--",
    "$$; DROP TABLE x; $$",
    "Jane Doe\x00' OR 'a'='a",
    "’ OR ‘1’=‘1",
    "%(name)s %(street)s {name} {}",
    "x" * 400,
    "'; COPY phoenix.project TO PROGRAM 'curl evil';--",
]
FIXED = {"SET TRANSACTION READ ONLY"}
ALLOWED_PARAMS = {"street", "city", "zip", "name"}


def test_static():
    print("Q1: SQL text is fixed, parameters are named and bound...")
    for f in (ROOT / "backend" / "sql").glob("*.sql"):
        text = f.read_text(encoding="utf-8")
        body = "\n".join(l for l in text.splitlines() if not l.strip().startswith("--"))
        names = set(re.findall(r"%\((\w+)\)s", body))
        assert names <= ALLOWED_PARAMS, f"{f.name}: unexpected placeholders {names - ALLOWED_PARAMS}"
        stray = re.sub(r"%\(\w+\)s|%%", "", body)
        assert "%" not in stray, f"{f.name}: unescaped % would break binding"
        assert "{" not in body and "}" not in body, f"{f.name}: no format braces allowed"
    tree = ast.parse((ROOT / "backend" / "crm.py").read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "PhoenixSQLClient")
    q = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "_query")
    fstrings = [n for n in ast.walk(q) if isinstance(n, ast.JoinedStr)]
    assert len(fstrings) == 1, "only the statement-timeout f-string is allowed"
    used = {n.id for n in ast.walk(fstrings[0]) if isinstance(n, ast.Name)}
    assert used == {"timeout_ms"}, used
    assigns = [n for n in ast.walk(q) if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == "timeout_ms" for t in n.targets)]
    assert assigns and isinstance(assigns[0].value, ast.Call) and getattr(assigns[0].value.func, "id", "") == "int"
    executes = [n for n in ast.walk(q) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "execute"]
    assert len(executes) == 3 and len(executes[2].args) == 2, "query must be execute(sql, params)"
    print("  ok")


class _Cursor:
    def __init__(self, log):
        self.log = log
        self.description = [("project_id",), ("project_name",), ("street1",), ("city",),
                            ("postal_code",), ("state",), ("fuzzy_score",)]

    def execute(self, sql, params=None):
        self.log.append((sql, params))

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, log):
        self.log = log

    def cursor(self):
        return _Cursor(self.log)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_runtime_binding():
    print(f"Q2: {len(PAYLOADS)} payloads in the name and the address only ever arrive as bound data...")
    fixed = FIXED | {crm.PhoenixSQLClient._ADDRESS_SQL, crm.PhoenixSQLClient._NAME_SQL}
    for p in PAYLOADS:
        for name, address in ((p, "100 Solar Way, Tampa, FL 33601"),
                              ("Jane Doe", f"100 {p}, Tampa, FL 33601"),
                              (p, f"100 {p}, {p}, FL 33601")):
            log: list = []
            client = crm.PhoenixSQLClient(connect=lambda log=log: _Conn(log))
            outcome = account_match.run(name, address, client=client)
            assert not outcome.matched
            for sql, params in log:
                if sql.startswith("SET LOCAL statement_timeout = "):
                    assert re.fullmatch(r"SET LOCAL statement_timeout = \d+", sql), sql
                    continue
                assert sql in fixed, f"unexpected SQL executed for payload {p[:30]!r}"
                if params is not None:
                    assert set(params) <= ALLOWED_PARAMS
                    assert all(isinstance(v, str) for v in params.values())
                    assert re.fullmatch(r"\d{5}|", params.get("zip", "")), params.get("zip")
            searched = [s for s, _ in log if s in (crm.PhoenixSQLClient._ADDRESS_SQL, crm.PhoenixSQLClient._NAME_SQL)]
            assert len(searched) in (0, 2), "address + name searches, or none when the address has no number"
            for s, _ in log:
                assert p[:20] not in s, "payload text must never be part of the SQL string"
    print("  ok")


def test_through_chat():
    print("Q3: payloads typed into the chat never break the conversation...")
    from _session_client import SessionClient
    from backend.account_gateway import NOT_FOUND_MESSAGE
    from backend.main import app
    c = SessionClient(app)
    for i, p in enumerate(PAYLOADS):
        label = f"sqli{i}"
        c.post("/api/chat", json={"session_id": label, "message": ""})
        r1 = c.post("/api/chat", json={"session_id": label, "message": p[:300]})
        assert r1.status_code == 200, (p[:30], r1.status_code)
        r2 = c.post("/api/chat", json={"session_id": label, "message": f"100 Solar Way {p[:200]}, Tampa, FL"})
        assert r2.status_code == 200, (p[:30], r2.status_code)
        texts = [m["text"] for m in r2.json().get("messages", [])]
        # A payload that literally contains a real account name next to that account's real
        # street may fuzzy-match on the genuine name and address (not via the SQL). Any other
        # "found" would mean the injection text influenced the result.
        from backend.account_gateway import FOUND_MESSAGE
        if FOUND_MESSAGE in texts:
            assert "Jane Doe" in p, f"payload {p[:30]!r} produced a match"
        else:
            assert NOT_FOUND_MESSAGE in texts or r2.json().get("await_step") in ("account_name", "account_address"), texts
    print("  ok")


if __name__ == "__main__":
    tests = [test_static, test_runtime_binding, test_through_chat]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL {t.__name__}: {exc}")
    print(f"\nsql_injection_test: {len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
