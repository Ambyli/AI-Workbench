"""Degraded model gateway (release test plan 5.2): every failure mode fails closed.

A local fake OpenAI-compatible gateway is switched between modes mid-test while the
chatbot runs with LLM_REQUIRED=true (the production/mock setting):

  ok         normal answers                       -> chat works, health 200
  forbidden  403 on completions (key/team issue)  -> chat 503, health 503 (form fallback)
  recover    back to ok                           -> health 200 again, no restart
  slow       completions take longer than timeout -> turn fails within the time budget
  malformed  200 with non-JSON content            -> no crash, the question is asked again
  down       nothing listening                    -> health 503, chat 503 quickly
"""
from __future__ import annotations

import ast
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

MODEL = "fake-qwen"
STATE = {"mode": "ok"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeGateway(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code: int, body: dict | str) -> None:
        data = (body if isinstance(body, str) else json.dumps(body)).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # /v1/models
        self._send(200, {"data": [{"id": MODEL}]})

    def do_POST(self):  # /v1/chat/completions
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        mode = STATE["mode"]
        if mode == "forbidden":
            return self._send(403, {"error": {"message": "key not allowed to access model"}})
        if mode == "slow":
            time.sleep(4)
        if mode == "malformed":
            return self._send(200, {"choices": [{"message": {"content": "Sure! Here you go: banana"}}]})
        from backend import llm
        system, user = body["messages"][0]["content"], body["messages"][1]["content"]
        if "extraction" in system:
            ftype = re.search(r"\(type: ([a-z_0-9]+)\)", user).group(1)
            msg = re.search(r"<customer_message>\n(.*)\n</customer_message>", user, re.S).group(1)
            opts = re.search(r"Choose exactly one of: (\[.*?\])\.", user)
            content = {"value": llm._mock_extract(ftype, msg, ast.literal_eval(opts.group(1)) if opts else None)}
        else:
            content = {"responsive": True, "confidence": 0.95}
        self._send(200, {"model": MODEL, "choices": [{"message": {"content": json.dumps(content)}}]})


class QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):  # the client hung up on a slow reply: expected
        pass


def main() -> int:
    port = _free_port()
    server = QuietServer(("127.0.0.1", port), FakeGateway)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    os.environ.update({
        "USE_MOCK_LLM": "0", "LLM_BACKEND": "vllm", "LLM_REQUIRED": "true",
        "VLLM_BASE_URL": f"http://127.0.0.1:{port}/v1", "VLLM_MODEL": MODEL, "VLLM_API_KEY": "test",
        "LLM_TIMEOUT_SECONDS": "2", "LLM_PROBE_TTL_SECONDS": "1", "GEOCODER": "none",
        "DATA_DIR": tempfile.mkdtemp(prefix="degraded-"), "RATE_LIMIT_REQUESTS": "1000",
        "ZEO_ENV_FILE": str(Path(tempfile.mkdtemp()) / "none.env"),
    })
    from _session_client import SessionClient
    from backend import llm
    from backend.main import app
    c = SessionClient(app)
    failures: list[str] = []

    def check(cond: bool, what: str) -> None:
        print(("  ok   " if cond else "  FAIL ") + what)
        if not cond:
            failures.append(what)

    def health() -> int:
        llm.reset_mock_cache()
        return c.get("/api/health").status_code

    print("ok mode")
    STATE["mode"] = "ok"
    check(health() == 200, "health 200 with a working gateway")
    start = c.post("/api/chat", json={"session_id": "d1", "message": ""})
    check(start.status_code == 200, "chat starts")
    r = c.post("/api/chat", json={"session_id": "d1", "message": "Jane Doe"})
    check(r.status_code == 200 and r.json()["await_step"] == "account_address", "a turn works")

    print("forbidden mode (403)")
    STATE["mode"] = "forbidden"
    r = c.post("/api/chat", json={"session_id": "d1", "message": "100 Solar Way, Tampa, FL 33601"})
    check(r.status_code == 503 and r.json().get("detail") == "assistant_unavailable", "turn fails closed (503)")
    check(c.get("/api/health").status_code == 503, "health 503 right after a refusal (page switches to the form)")

    print("recover")
    STATE["mode"] = "ok"
    time.sleep(1.2)
    check(health() == 200, "health recovers without a restart")
    r = c.post("/api/chat", json={"session_id": "d1", "message": "100 Solar Way, Tampa, FL 33601"})
    check(r.status_code == 200, "the same chat continues after recovery")

    print("slow mode")
    STATE["mode"] = "slow"
    t0 = time.monotonic()
    r = c.post("/api/chat", json={"session_id": "d1", "message": "813-555-0199"})
    took = time.monotonic() - t0
    check(r.status_code == 503, f"slow model -> 503 (got {r.status_code})")
    check(took < 2 * 2 + 3, f"turn bounded by the timeout budget ({took:.1f}s)")

    print("malformed mode")
    STATE["mode"] = "ok"
    time.sleep(1.2)
    health()
    STATE["mode"] = "malformed"
    r = c.post("/api/chat", json={"session_id": "d1", "message": "813-555-0199"})
    body = r.json() if r.status_code == 200 else {}
    check(r.status_code == 200 and body.get("state") == "collect", "no crash on garbage output")
    check(body.get("await_step") == "contact", "the same question is asked again (nothing stored)")

    print("down mode")
    STATE["mode"] = "ok"
    server.shutdown()
    server.server_close()
    time.sleep(1.2)
    check(health() == 503, "health 503 when the gateway is down")
    t0 = time.monotonic()
    r = c.post("/api/chat", json={"session_id": "d1", "message": "813-555-0199"})
    check(r.status_code == 503 and time.monotonic() - t0 < 5, "chat 503 quickly when down")

    print(f"\ndegraded_gateway_test: {'PASS' if not failures else f'{len(failures)} failure(s)'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
