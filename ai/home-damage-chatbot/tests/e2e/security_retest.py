"""Security re-test against the RUNNING mock (release test plan 5.5).

Run after the end-to-end flows (it also scans the mock logs those flows produced):
    python tests/e2e/security_retest.py [--base http://localhost:8080]

Talks to the site server like a browser would, and to the chatbot API and account
service directly on localhost where a check needs it (spoofed client addresses,
CORS, the internal service key).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

PIDS = json.loads((ROOT / "mock" / ".pids.json").read_text(encoding="utf-8-sig"))
API = f"http://127.0.0.1:{PIDS['apiPort']}"
ACCOUNT = f"http://127.0.0.1:{PIDS['accountPort']}"
EMAILS = ROOT / "mock" / ".data" / "account" / "emails.json"
LOGS = ROOT / "mock" / "logs"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail and not ok else ""), flush=True)


def post_retry(c: httpx.Client, url: str, **kw) -> httpx.Response:
    for _ in range(20):
        r = c.post(url, **kw)
        if r.status_code == 429 and r.json().get("detail") == "rate_limited":
            time.sleep(float(r.headers.get("retry-after", "5")) + 0.5)
            continue
        return r
    return r


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8080")
    base = ap.parse_args().base
    c = httpx.Client(base_url=base, timeout=40)

    # --- API surface through the proxy --------------------------------------------
    staff_paths = ["/api/emails", "/api/crm/cases", "/api/customers", "/api/chat-notifications",
                   "/api/queue/stats", "/api/llm/status", "/api/docs", "/api/openapi.json", "/api/lookup"]
    codes = {p: c.get(p).status_code for p in staff_paths}
    check("staff/doc routes 404 through the proxy", all(v == 404 for v in codes.values()), str(codes))
    direct = {p: httpx.get(API + p).status_code for p in ("/docs", "/openapi.json", "/uploads/x.png", "/api/emails")}
    check("staff/docs/uploads 404 on the API itself", all(v == 404 for v in direct.values()), str(direct))
    check("queue simulator not reachable", c.post("/api/queue/simulate", json={"action": "fill"}).status_code == 404)

    # --- Account service is internal-only -------------------------------------------
    r = httpx.post(ACCOUNT + "/v1/check", json={"account_name": "Jane Doe", "address": "100 Solar Way, Tampa, FL 33601"})
    check("account service rejects calls without the key", r.status_code == 401, str(r.status_code))
    check("account service not reachable through the site", c.post("/v1/check", json={}).status_code in (404, 405))

    # --- Sessions --------------------------------------------------------------------
    forged = "A" * 22
    r1 = c.post("/api/chat", json={"session_id": forged, "message": "hi"})
    r2 = c.post("/api/queue/status", json={"session_id": forged})
    r3 = c.post("/api/upload", data={"session_id": forged}, files={"file": ("a.png", b"\x89PNG\r\n", "image/png")})
    check("forged session ids are rejected (chat/status/upload)",
          (r1.status_code, r2.status_code, r3.status_code) == (404, 404, 404),
          f"{r1.status_code}/{r2.status_code}/{r3.status_code}")

    # --- Output encoding: hostile input never becomes markup for staff ---------------
    hostile = '<img src=x onerror=alert(1)> Jane Doe'
    emails_before = len(json.loads(EMAILS.read_text(encoding="utf-8"))) if EMAILS.exists() else 0
    s2 = post_retry(c, "/api/chat", json={"message": ""}).json()["session_id"]
    for msg in (hostile, "100 Solar Way, Tampa, FL 33601", "813-555-0199\r\nBcc: attacker@evil.test", "Electrical",
                "3", "skip", "Yes", "No", "No", "No", "<script>alert(1)</script> kitchen",
                "Outlets <b>dead</b> after the storm, <img src=x onerror=alert(2)>", "skip"):
        post_retry(c, "/api/chat", json={"session_id": s2, "message": msg})
    done = post_retry(c, "/api/chat", json={"session_id": s2, "message": "yes"}).json()
    rec = json.loads(EMAILS.read_text(encoding="utf-8"))[-1] if EMAILS.exists() else {}
    html = rec.get("html", "")
    check("hostile submission still completes", done.get("outcome") == "submitted", str(done.get("outcome")))
    check("staff email escapes markup", "<script>" not in html and "<img src=x" not in html and "&lt;" in html)
    check("subject stays one line (no header injection)", "\n" not in rec.get("subject", "") and "\r" not in rec.get("subject", ""))
    check("hostile input appears in chat replies only as text", all(
        "<script>" not in m["text"] for m in done.get("messages", [])))
    check("new email recorded", (len(json.loads(EMAILS.read_text(encoding="utf-8"))) if EMAILS.exists() else 0) > emails_before)

    # --- Rate limiting and client addresses -------------------------------------------
    spoof = [c.post("/api/queue/status", json={"session_id": forged},
                    headers={"X-Forwarded-For": f"10.0.0.{i}"}).status_code for i in range(80)]
    check("spoofed X-Forwarded-For through the proxy does not escape the limit", 429 in spoof,
          f"{spoof.count(429)} of 80 limited")
    time.sleep(61)  # let the window reset before the next checks
    a = [httpx.post(API + "/api/queue/status", json={"session_id": forged},
                    headers={"X-Forwarded-For": "203.0.113.10"}).status_code for _ in range(65)]
    b = httpx.post(API + "/api/queue/status", json={"session_id": forged},
                   headers={"X-Forwarded-For": "203.0.113.20"}).status_code
    check("two clients behind the trusted proxy get separate buckets (S-H2)", 429 in a and b == 404,
          f"A limited={429 in a}, B={b}")
    time.sleep(61)

    # --- CORS, headers, errors ----------------------------------------------------------
    r = httpx.options(API + "/api/chat", headers={"Origin": "https://evil.example",
                                                   "Access-Control-Request-Method": "POST"})
    check("CORS refuses a foreign origin", r.headers.get("access-control-allow-origin") not in ("*", "https://evil.example"))
    page = c.get("/service")
    h = {k.lower(): v for k, v in page.headers.items()}
    check("page headers: nosniff, noindex, referrer, permissions",
          all(k in h for k in ("x-content-type-options", "x-robots-tag", "referrer-policy", "permissions-policy")))
    api_h = {k.lower() for k in c.get("/api/health").headers}
    check("API responses keep CSP and frame denial through the proxy",
          {"content-security-policy", "x-frame-options"} <= api_h, str(sorted(api_h)))
    bad = c.post("/api/chat", content=b"{not json", headers={"Content-Type": "application/json"})
    big = c.post("/api/chat", json={"message": "x" * 5000})
    get = c.get("/api/chat")
    leak = any(t in (bad.text + big.text + get.text) for t in ("Traceback", "File \"", "backend/", "backend\\"))
    check("malformed / oversized / wrong-method requests: generic errors",
          bad.status_code == 422 and big.status_code == 422 and get.status_code in (404, 405) and not leak,
          f"{bad.status_code}/{big.status_code}/{get.status_code}")

    # --- Site server path traversal -------------------------------------------------------
    for p in ("/..%2f..%2fbackend%2fconfig.py", "/assets/../../.env", "/%2e%2e/%2e%2e/.env", "/mock/.pids.json"):
        r = c.get(p)
        check(f"traversal {p} serves only the app shell", r.status_code in (200, 404) and "VLLM" not in r.text
              and "import" not in r.text[:200])

    # --- Logs contain no personal data ---------------------------------------------------
    pii = ["Jane Doe", "Laura Chen", "813-555-0199", "100 Solar Way", "302 Voltage", "@example.com", "Marcus Bell"]
    hits = {}
    for f in LOGS.glob("*.log"):
        text = f.read_text(encoding="utf-8", errors="replace")
        found = [p for p in pii if p in text]
        if found:
            hits[f.name] = found
    check("mock logs contain no names, phones, emails or addresses (S-M5)", not hits, str(hits))

    failed = [r for r in results if not r[1]]
    print(f"\nsecurity_retest: {len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
