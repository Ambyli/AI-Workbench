"""Account separation layer: the chatbot has no path to the CRM.

  G1  static: nothing the chatbot imports at startup reaches the CRM, matching, SQL,
      delivery or staff modules; the account service never imports the LLM client
  G2  live, two real processes: a full conversation in a gapped chatbot process
      matches an account through the separate account service. The chatbot process
      never loads CRM/matching/delivery code and never receives match details; the
      account details are appended to the staff handoff by the account service.
  G3  the chatbot refuses to start in gapped mode if the gap does not hold
  G4  the account service rejects calls without the shared key, and won't start without one
  G5  the check reply is exactly {matched, ref}; refs are random and single-use
  G6  account service down: customer gets "not found", the submit is held in the
      outbox (never lost) and delivered once the service is back
"""
from __future__ import annotations

import ast
import json
import os
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

FORBIDDEN_IN_CHATBOT = {"crm", "account_match", "account_service", "pipeline", "mailer", "crm_store",
                        "chat_notifier", "email_render", "lookup", "staff_api", "seed"}
BACKEND = ROOT / "backend"


# ---------------------------------------------------------------------------
# G1 — static import graph (module-level imports only; lazy imports are opt-in paths)
# ---------------------------------------------------------------------------
def _module_level_imports(mod: str) -> set[str]:
    tree = ast.parse((BACKEND / f"{mod}.py").read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in tree.body:  # top level only
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            if node.module:
                out.add(node.module.split(".")[0])
            else:
                out.update(a.name for a in node.names)
    return {m for m in out if (BACKEND / f"{m}.py").exists()}


def _reachable(start: str) -> set[str]:
    seen, todo = set(), [start]
    while todo:
        m = todo.pop()
        if m in seen:
            continue
        seen.add(m)
        todo.extend(_module_level_imports(m) - seen)
    return seen


def test_static_import_graph():
    print("G1: chatbot import graph never reaches CRM / matching / delivery / staff code...")
    reach = _reachable("main")
    bad = reach & FORBIDDEN_IN_CHATBOT
    assert not bad, f"backend.main reaches {sorted(bad)}"
    svc = _reachable("account_service")
    assert "llm" not in svc, "the account service must not import the LLM client"
    assert {"account_match", "crm", "pipeline"} <= svc
    print(f"  ok: chatbot loads {len(reach)} modules, none forbidden; account service never loads llm")


# ---------------------------------------------------------------------------
# helpers for live processes
# ---------------------------------------------------------------------------
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _empty_env_file() -> str:
    f = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False)
    f.close()
    return f.name  # never load the developer's real .env (model key) in these tests


def _base_env(**extra) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CRM_", "ACCOUNT_", "VLLM_", "LLM_", "GOOGLE_", "CHAT_", "EMAIL_"))}
    env.update(ZEO_ENV_FILE=_empty_env_file(), GEOCODER="none", PYTHONPATH=str(ROOT),
               RATE_LIMIT_REQUESTS="1000", MAX_SESSIONS_PER_CLIENT="1000")
    env.update(extra)
    return env


def _start_account_service(port: int, key: str, data_dir: str, upload_dir: str):
    env = _base_env(ACCOUNT_SERVICE_KEY=key, CRM_BACKEND="stub", DATA_DIR=data_dir,
                    UPLOAD_DIR=upload_dir, EMAIL_SEND_ENABLED="false", CHAT_SEND_ENABLED="false")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "backend.account_service:app",
                             "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
                            cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    import httpx
    for _ in range(60):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1).status_code == 200:
                return proc
        except Exception:
            time.sleep(0.25)
    proc.kill()
    raise AssertionError("account service did not start: " + proc.stdout.read().decode(errors="replace")[-600:])


CHATBOT_SCRIPT = textwrap.dedent('''
    import json, sys
    sys.path.insert(0, ".")
    sys.path.insert(0, "tests")
    from _session_client import SessionClient
    from backend.main import app
    FORBIDDEN = set(json.loads(sys.argv[1]))
    answers = json.loads(sys.argv[2])
    out = {"turns": []}
    with SessionClient(app) as c:  # runs startup: the gap guard + outbox
        c.post("/api/chat", json={"session_id": "g", "message": ""})
        for a in answers:
            out["turns"].append(c.post("/api/chat", json={"session_id": "g", "message": a}).json())
    out["loaded"] = sorted(m.split(".")[1] for m in sys.modules
                           if m.startswith("backend.") and m.split(".")[1] in FORBIDDEN)
    from backend import account_gateway
    out["outbox"] = account_gateway.pending_outbox()
    print("@@RESULT@@" + json.dumps(out))
''')

FLOW = ["Jane Doe", "100 Solar Way, Tampa, FL 33601", "813-555-0199", "Electrical", "7", "skip",
        "Yes", "No", "No", "No", "Kitchen", "The kitchen outlets stopped working after the storm.",
        "skip", "yes"]


def _run_chatbot(port: int, key: str, data_dir: str, answers: list[str], **extra) -> dict:
    settings = dict(ACCOUNT_GATEWAY="service", ACCOUNT_SERVICE_URL=f"http://127.0.0.1:{port}",
                    ACCOUNT_SERVICE_KEY=key, REQUIRE_ACCOUNT_GAP="true", ENABLE_STAFF_API="false",
                    USE_MOCK_LLM="1", DATA_DIR=data_dir)
    settings.update(extra)
    env = _base_env(**settings)
    r = subprocess.run([sys.executable, "-c", CHATBOT_SCRIPT, json.dumps(sorted(FORBIDDEN_IN_CHATBOT)),
                        json.dumps(answers)], cwd=str(ROOT), env=env, capture_output=True, text=True,
                       timeout=180)
    marker = [ln for ln in r.stdout.splitlines() if ln.startswith("@@RESULT@@")]
    assert marker, f"chatbot process failed:\n{r.stdout[-1500:]}\n{r.stderr[-2500:]}"
    return json.loads(marker[0][len("@@RESULT@@"):])


# ---------------------------------------------------------------------------
# G2 — live two-process conversation
# ---------------------------------------------------------------------------
def test_live_gapped_conversation():
    print("G2: full conversation across two real processes (gapped chatbot + account service)...")
    port, key = _free_port(), "test-" + os.urandom(8).hex()
    bot_dir, svc_dir = tempfile.mkdtemp(prefix="gap-bot-"), tempfile.mkdtemp(prefix="gap-svc-")
    svc = _start_account_service(port, key, svc_dir, str(Path(bot_dir) / "uploads"))
    try:
        res = _run_chatbot(port, key, bot_dir, FLOW)
    finally:
        svc.kill()
    turns = res["turns"]
    texts = [m["text"] for t in turns for m in t.get("messages", [])]
    from backend.account_gateway import FOUND_MESSAGE
    assert FOUND_MESSAGE in texts, "the gapped chatbot should still tell the customer 'found'"
    assert turns[-1]["done"] and turns[-1].get("outcome") == "submitted", turns[-1]
    assert res["loaded"] == [], f"chatbot process loaded forbidden modules: {res['loaded']}"
    blob = json.dumps(turns)
    for s in ("900001", "Solar Way, Tampa, FL 33601 (", "address_score", "project_id", "ref"):
        assert s not in blob, f"chatbot response carried {s!r}"
    # The account details were appended by the account service, in ITS data dir only.
    emails = json.loads((Path(svc_dir) / "emails.json").read_text(encoding="utf-8"))
    assert emails and "900001 (Verified Match)" in emails[-1]["html"], "account block not appended"
    assert not (Path(bot_dir) / "emails.json").exists(), "the chatbot must not hold staff records"
    print("  ok: found + submitted; chatbot loaded no CRM code; details appended by the account service")


# ---------------------------------------------------------------------------
# G3 — startup guard
# ---------------------------------------------------------------------------
def test_startup_guard_refuses_broken_gap():
    print("G3: gapped chatbot refuses to start if the gap does not hold...")
    port, key = _free_port(), "k-" + os.urandom(6).hex()
    for extra, why in ((dict(CRM_DB_URL="postgresql://ro@db/phoenix"), "CRM_DB_URL"),
                       (dict(ACCOUNT_GATEWAY="inprocess"), "ACCOUNT_GATEWAY"),
                       (dict(ACCOUNT_SERVICE_KEY=""), "ACCOUNT_SERVICE_KEY"),
                       (dict(ENABLE_STAFF_API="true"), "ENABLE_STAFF_API")):
        try:
            _run_chatbot(port, key, tempfile.mkdtemp(), [], **extra)
        except AssertionError as exc:
            assert why in str(exc), f"refused, but not for {why}: {str(exc)[-300:]}"
            continue
        raise AssertionError(f"chatbot started although {why} breaks the gap")
    print("  ok: refused for CRM secret, in-process gateway, missing key, staff API on")


# ---------------------------------------------------------------------------
# G4/G5 — account service auth and the shape of what it returns
# ---------------------------------------------------------------------------
def test_service_auth_and_reply_shape():
    print("G4/G5: service needs the key; check reply is only {matched, ref}; refs single-use...")
    import httpx
    port, key = _free_port(), "k-" + os.urandom(8).hex()
    svc_dir = tempfile.mkdtemp(prefix="gap-svc-")
    svc = _start_account_service(port, key, svc_dir, str(Path(svc_dir) / "uploads"))
    try:
        base = f"http://127.0.0.1:{port}"
        body = {"account_name": "Jane Doe", "address": "100 Solar Way, Tampa, FL 33601"}
        assert httpx.post(base + "/v1/check", json=body).status_code == 401
        assert httpx.post(base + "/v1/check", json=body, headers={"X-Internal-Key": "wrong"}).status_code == 401
        h = {"X-Internal-Key": key}
        a = httpx.post(base + "/v1/check", json=body, headers=h).json()
        b = httpx.post(base + "/v1/check", json=body, headers=h).json()
        assert set(a) == {"matched", "ref"} and a["matched"] is True, a
        assert a["ref"] != b["ref"] and len(a["ref"]) >= 20, "refs must be random and long"
        for path in ("/docs", "/openapi.json", "/api/emails"):
            assert httpx.get(base + path).status_code == 404, path
        case = {"issue_type": "misc", "account_name": "Jane Doe", "contact": "813-555-0199",
                "account_address": "100 Solar Way, Tampa, FL 33601", "urgency": 3,
                "what_damaged": "fence gate", "verbatim": ["fence gate"], "attachments": []}
        d = httpx.post(base + "/v1/dispatch", json={"ref": a["ref"], "case": case}, headers=h).json()
        assert set(d) == {"matched", "email_id"} and d["matched"] is True, d
        # Single use: the ref is gone; a replay re-runs the match from the typed answers instead.
        from backend import account_service  # noqa: F401  (documented behavior check only)
        d2 = httpx.post(base + "/v1/dispatch", json={"ref": a["ref"], "case": case}, headers=h).json()
        assert d2["matched"] is True and d2["email_id"] != d["email_id"]
    finally:
        svc.kill()
    env = _base_env(ACCOUNT_SERVICE_KEY="", DATA_DIR=tempfile.mkdtemp())
    p = subprocess.run([sys.executable, "-c", "from fastapi.testclient import TestClient;"
                        "from backend.account_service import app\nwith TestClient(app): pass"],
                       cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=60)
    assert p.returncode != 0 and "ACCOUNT_SERVICE_KEY" in p.stderr, "service must refuse to start without a key"
    print("  ok")


# ---------------------------------------------------------------------------
# G6 — service down: safe customer message, request held and later delivered
# ---------------------------------------------------------------------------
def test_service_down_outbox():
    print("G6: account service down -> 'not found', submit held in outbox, delivered later...")
    port, key = _free_port(), "k-" + os.urandom(8).hex()
    bot_dir, svc_dir = tempfile.mkdtemp(prefix="gap-bot-"), tempfile.mkdtemp(prefix="gap-svc-")
    res = _run_chatbot(port, key, bot_dir, FLOW)  # nothing listening on `port`
    texts = [m["text"] for t in res["turns"] for m in t.get("messages", [])]
    from backend.account_gateway import NOT_FOUND_MESSAGE
    assert NOT_FOUND_MESSAGE in texts and res["turns"][-1]["done"], "customer path must still complete"
    assert res["outbox"] == 1, f"submit should be held in the outbox, got {res['outbox']}"
    svc = _start_account_service(port, key, svc_dir, str(Path(bot_dir) / "uploads"))
    try:
        res2 = _run_chatbot(port, key, bot_dir, [])  # startup flushes the outbox
    finally:
        svc.kill()
    assert res2["outbox"] == 0, "outbox should be delivered once the service is back"
    emails = json.loads((Path(svc_dir) / "emails.json").read_text(encoding="utf-8"))
    assert len(emails) == 1 and "900001 (Verified Match)" in emails[0]["html"], \
        "held request delivered, and re-matched by the service"
    print("  ok")


if __name__ == "__main__":
    tests = [test_static_import_graph, test_service_auth_and_reply_shape, test_startup_guard_refuses_broken_gap,
             test_live_gapped_conversation, test_service_down_outbox]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL {t.__name__}: {exc}")
    print(f"\naccount_gap_test: {len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
