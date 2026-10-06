"""Stage 1R regression tests: public API surface, queue abuse, fail-closed model.

Each test maps to a finding in docs/DELIVERY_PLAN.md and would have FAILED on the
pre-remediation code:
  S-C1  public surface is exactly the contract allow-list; staff/demo routes absent
  S-C1  staff routes require X-Staff-Key when STAFF_API_KEY is set
  S-H1  queue status is read-only (cannot grant or squat a slot); simulate absent
  S-H4  a failed model probe is not cached forever (recovers on its own)
  S-H4  LLM_REQUIRED + model down -> /api/health 503 and /api/chat 503 (no silent mock)
  S-L1  public /api/health leaks no host/model details
"""
from __future__ import annotations

import importlib
import os
import tempfile
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("GEOCODER", "none")  # hermetic: no public geocoder calls
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="chatbot-test-"))  # never touch real backend/data
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# Production-like defaults: staff API OFF, mock model.
os.environ.pop("ENABLE_STAFF_API", None)
os.environ.pop("STAFF_API_KEY", None)
os.environ["USE_MOCK_LLM"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

import backend.main as main_mod  # noqa: E402
from backend import llm, queue_manager  # noqa: E402

STAFF_PATHS = [
    ("GET", "/api/emails"), ("GET", "/api/emails/abc"), ("GET", "/api/customers"),
    ("POST", "/api/lookup"), ("GET", "/api/crm/cases"), ("GET", "/api/crm/cases/x"),
    ("POST", "/api/crm/cases/x/status"), ("GET", "/api/chat-notifications"),
    ("GET", "/api/queue/stats"), ("POST", "/api/queue/simulate"), ("GET", "/api/llm/status"),
    ("GET", "/docs"), ("GET", "/openapi.json"), ("GET", "/uploads/anything.png"), ("GET", "/"),
]


def _api_routes(app) -> set[tuple[str, str]]:
    out = set()
    for r in app.routes:
        for m in (getattr(r, "methods", None) or []):
            if m in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                out.add((m, r.path))
    return out


def test_public_surface_allowlist():
    print("S-C1 public surface == contract allow-list ...")
    assert _api_routes(main_mod.app) == main_mod.PUBLIC_ROUTES, _api_routes(main_mod.app)
    c = TestClient(main_mod.app)
    for verb, path in STAFF_PATHS:
        r = c.request(verb, path, json={})
        assert r.status_code in (404, 405), f"{verb} {path} reachable publicly: {r.status_code}"
    print("  ✓ only", sorted(main_mod.PUBLIC_ROUTES), "are routable; staff/demo/docs/uploads 404")


def test_health_is_minimal():
    print("S-L1 public health leaks nothing ...")
    body = TestClient(main_mod.app).get("/api/health").json()
    assert set(body) == {"ok", "assistant_available"}, body
    print("  ✓", body)


def test_queue_status_is_read_only():
    print("S-H1 queue status cannot grant or squat a slot ...")
    queue_manager.reset()
    c = TestClient(main_mod.app)
    before = queue_manager.get_stats()["active_users"]
    for i in range(10):
        r = c.post("/api/queue/status", json={"session_id": f"squatter_{i}"})
        assert r.status_code == 404 and r.json() == {"detail": "session_expired"}, r.text
    after = queue_manager.get_stats()
    assert after["active_users"] == before and after["queued_users"] == 0, after
    print("  ✓ 10 status polls with forged ids -> 404, 0 slots taken")


def _start(c) -> dict:
    r = c.post("/api/chat", json={"message": ""})
    assert r.status_code == 200, r.text
    return r.json()


def _png(size=(8, 8)) -> bytes:
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, (90, 120, 60)).save(buf, "PNG")
    return buf.getvalue()


def test_server_issued_sessions():
    print("S-M2 sessions are server-issued; forged ids rejected everywhere ...")
    from backend import sessions
    sessions.reset(); queue_manager.reset()
    c = TestClient(main_mod.app)
    d = _start(c)
    sid = d["session_id"]
    assert isinstance(sid, str) and len(sid) >= 22, sid
    # forged / guessed ids
    for forged in ("t1", "session-123", sid[:-1] + ("A" if sid[-1] != "A" else "B")):
        assert c.post("/api/chat", json={"session_id": forged, "message": "hi"}).status_code == 404
        assert c.post("/api/queue/status", json={"session_id": forged}).status_code == 404
        up = c.post("/api/upload", data={"session_id": forged},
                    files={"file": ("a.png", _png(), "image/png")})
        assert up.status_code == 404, up.text
    assert c.post("/api/chat", json={"session_id": sid, "message": "Jane Doe"}).status_code == 200
    print("  ✓ issued 128-bit token; forged ids -> 404 on chat, queue/status, upload")


def test_uploads_bound_to_session():
    print("S-H3 uploads are session-bound and fail closed ...")
    from backend import sessions, upload
    sessions.reset(); queue_manager.reset()
    c = TestClient(main_mod.app)
    a, b = _start(c)["session_id"], _start(c)["session_id"]
    name = c.post("/api/upload", data={"session_id": a},
                  files={"file": ("a.png", _png(), "image/png")}).json()["filename"]
    # B may not reference A's upload, as an attachment or as its pointer image.
    r = c.post("/api/chat", json={"session_id": b, "message": "", "attachments": [name]})
    assert r.status_code == 422 and r.json()["detail"] == "unknown_attachment", r.text
    r = c.post("/api/chat", json={"session_id": b, "message": name})
    assert r.status_code == 422, r.text
    # Undecodable image with a valid PNG signature -> 400 and nothing written.
    before = set(p.name for p in upload.UPLOAD_DIR.iterdir())
    bad = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    r = c.post("/api/upload", data={"session_id": a}, files={"file": ("bad.png", bad, "image/png")})
    assert r.status_code == 400, r.text
    assert set(p.name for p in upload.UPLOAD_DIR.iterdir()) == before, "rejected upload left a file"
    print("  ✓ cross-session attachment 422; undecodable image 400 with no file written")


def test_gps_exif_stripped():
    print("S-H3 GPS EXIF removed from stored JPEG ...")
    import io
    from PIL import Image
    from backend import sessions, upload
    sessions.reset(); queue_manager.reset()
    c = TestClient(main_mod.app)
    sid = _start(c)["session_id"]
    img = Image.new("RGB", (32, 32), (10, 20, 30))
    exif = Image.Exif()
    exif[0x8825] = {1: "N", 2: (27.0, 57.0, 1.0), 3: "W", 4: (82.0, 27.0, 1.0)}  # GPSInfo IFD
    exif[0x010F] = "PhoneMaker"                                                     # Make
    buf = io.BytesIO(); img.save(buf, "JPEG", exif=exif); raw = buf.getvalue()
    assert Image.open(io.BytesIO(raw)).getexif().get(0x010F) == "PhoneMaker"      # precondition
    name = c.post("/api/upload", data={"session_id": sid},
                  files={"file": ("gps.jpg", raw, "image/jpeg")}).json()["filename"]
    stored = Image.open(upload.UPLOAD_DIR / name)
    ex = stored.getexif()
    assert len(ex) == 0 and 0x8825 not in ex and "exif" not in stored.info, dict(ex)
    print("  ✓ stored file has no EXIF/GPS")


def test_decompression_bomb_rejected():
    print("S-H3 decompression bomb rejected ...")
    from backend import sessions
    sessions.reset(); queue_manager.reset()
    c = TestClient(main_mod.app)
    sid = _start(c)["session_id"]
    bomb = _png(size=(6000, 5000))  # 30 MP > 25 MP ceiling, tiny on disk
    r = c.post("/api/upload", data={"session_id": sid}, files={"file": ("big.png", bomb, "image/png")})
    assert r.status_code == 400, r.status_code
    print(f"  ✓ {len(bomb)//1024} KB / 30 MP image -> 400")


def test_session_and_upload_caps():
    print("S-H1/S-M3 per-client session cap and per-session upload cap ...")
    from backend import sessions
    sessions.reset(); queue_manager.reset()
    with patch.dict(os.environ, {"MAX_SESSIONS_PER_CLIENT": "2", "MAX_UPLOADS_PER_SESSION": "2"}):
        c = TestClient(main_mod.app)
        s1 = _start(c)["session_id"]; _start(c)
        r = c.post("/api/chat", json={"message": ""})
        assert r.status_code == 429 and r.json()["detail"] == "too_many_sessions", r.text
        for _ in range(2):
            assert c.post("/api/upload", data={"session_id": s1},
                          files={"file": ("a.png", _png(), "image/png")}).status_code == 200
        r = c.post("/api/upload", data={"session_id": s1}, files={"file": ("a.png", _png(), "image/png")})
        assert r.status_code == 429 and r.json()["detail"] == "upload_limit", r.text
    sessions.reset(); queue_manager.reset()
    print("  ✓ 3rd session from one client -> 429; 3rd upload in a session -> 429")


def test_output_escaping():
    print("S-M1/S-L3 hostile customer text is inert in staff HTML and email headers ...")
    from backend import crm_store, mailer
    from backend.schemas import EmailMessage
    evil = '<a href=//evil.example style=position:fixed;inset:0>click</a>'
    note = crm_store.generate_chatter_note({
        "name": "Bob " + evil, "contact": "555-0100 " + evil, "summary": evil,
        "account_address": "1 Main St " + evil, "verbatim": [evil], "issue_type": "misc",
        "urgency": 5, "attachments": ["../../etc/passwd", "x' onerror='alert(1)"],
        "damage_pointer": "y' onclick='steal()",
    })
    assert "<a href" not in note and "&lt;a href=//evil.example" in note, note[:400]
    assert "etc/passwd" not in note and "alert(1)" not in note and "steal()" not in note
    mime = mailer.build_mime(EmailMessage(
        to="svc@example.com", subject="[High] Roof — Bob\r\nBcc: victim@example.com", html="<p>x</p>"))
    assert "\n" not in mime["Subject"] and "\r" not in mime["Subject"], repr(mime["Subject"])
    assert mime["Bcc"] is None, "header injection created a Bcc header"
    print("  ✓ markup escaped, crafted file names dropped, CR/LF header injection neutralised")


def test_retention_purge():
    print("S-M6 retention purges expired customer data, keeps recent + demo seeds ...")
    from datetime import datetime, timedelta, timezone
    from backend import email_render, retention
    now = datetime.now(timezone.utc)
    iso = lambda d: (now - timedelta(days=d)).isoformat()  # noqa: E731
    email_render._STORE[:] = [
        {"id": "old", "created_at": iso(45)},
        {"id": "recent", "created_at": iso(2)},
        {"id": "seed", "created_at": iso(400), "_seed": True},
    ]
    with patch.dict(os.environ, {"RETENTION_DAYS": "30"}):
        counts = retention.purge(now)
    assert [r["id"] for r in email_render._STORE] == ["recent", "seed"], email_render._STORE
    assert counts["emails"] == 1, counts
    with patch.dict(os.environ, {"RETENTION_DAYS": "0"}):
        email_render._STORE.append({"id": "ancient", "created_at": iso(9999)})
        assert retention.purge(now)["emails"] == 0  # 0 disables purging
    email_render._STORE.clear()
    print("  ✓ 45-day-old record purged; recent + seed kept; RETENTION_DAYS=0 disables")


def test_no_demo_seed_in_production():
    print("S-M6 production (staff API off) never seeds fake demo cases ...")
    from backend import email_render
    email_render._STORE.clear()
    with TestClient(main_mod.app):   # runs startup
        assert email_render.is_empty(), "demo seed data written to a production inbox"
    print("  ✓ inbox empty after startup with ENABLE_STAFF_API off")


def test_contract_v2_shape():
    print("Contract v2: location only at the map step, no internal ids leaked ...")
    from backend import sessions
    sessions.reset(); queue_manager.reset()
    c = TestClient(main_mod.app)
    d = _start(c)
    assert d["location"] is None and d["summary"] is None and "email_id" not in d, d.keys()
    assert "latitude" not in d and "account_address" not in d, d.keys()
    print("  ✓ greeting carries no address/coordinates/email_id")


def test_probe_failure_not_cached_forever():
    print("S-H4 failed model probe recovers without restart ...")
    with patch.dict(os.environ, {"USE_MOCK_LLM": "", "LLM_BACKEND": "vllm",
                                 "LLM_PROBE_TTL_SECONDS": "0"}):
        llm.reset_mock_cache()
        with patch("backend.llm._probe_backend", return_value=False):
            assert llm.using_mock() is True          # degraded while down
        with patch("backend.llm._probe_backend", return_value=True):
            assert llm.using_mock() is False         # recovers on next probe
    llm.reset_mock_cache()
    print("  ✓ down -> mock, back up -> model (no process restart)")


def test_llm_required_fails_closed():
    print("S-H4 LLM_REQUIRED + model down -> 503, never silent mock ...")
    with patch.dict(os.environ, {"USE_MOCK_LLM": "", "LLM_BACKEND": "vllm",
                                 "LLM_REQUIRED": "true", "LLM_PROBE_TTL_SECONDS": "0"}):
        llm.reset_mock_cache()
        with patch("backend.llm._probe_backend", return_value=False):
            c = TestClient(main_mod.app)
            h = c.get("/api/health")
            assert h.status_code == 503 and h.json() == {"ok": False, "assistant_available": False}, h.text
            sid = _start(c)["session_id"]                                   # greeting (no LLM call)
            r = c.post("/api/chat", json={"session_id": sid, "message": "Jane Doe"})
            assert r.status_code == 503 and r.json() == {"detail": "assistant_unavailable"}, r.text
    llm.reset_mock_cache()
    queue_manager.reset()
    print("  ✓ health 503, chat turn 503 assistant_unavailable")


def test_staff_key_required_when_configured():
    print("S-C1 staff routes require X-Staff-Key when STAFF_API_KEY is set ...")
    with patch.dict(os.environ, {"ENABLE_STAFF_API": "true", "STAFF_API_KEY": "test-staff-key"}):
        staff_mod = importlib.reload(main_mod)
        c = TestClient(staff_mod.app)
        assert c.get("/api/emails").status_code == 401
        assert c.get("/api/emails", headers={"X-Staff-Key": "wrong"}).status_code == 401
        assert c.get("/api/emails", headers={"X-Staff-Key": "test-staff-key"}).status_code == 200
    importlib.reload(main_mod)  # restore production-like app for any later use
    print("  ✓ 401 without/with wrong key, 200 with key")


if __name__ == "__main__":
    tests = [test_public_surface_allowlist, test_health_is_minimal, test_queue_status_is_read_only,
             test_server_issued_sessions, test_uploads_bound_to_session, test_gps_exif_stripped,
             test_decompression_bomb_rejected, test_session_and_upload_caps, test_output_escaping,
             test_retention_purge, test_no_demo_seed_in_production, test_contract_v2_shape,
             test_probe_failure_not_cached_forever, test_llm_required_fails_closed,
             test_staff_key_required_when_configured]
    try:
        for t in tests:
            t()
        print("\nALL EXPOSURE / HARDENING CHECKS PASSED")
    except AssertionError as e:
        print(f"\nEXPOSURE CHECK FAILED: {e}")
        sys.exit(1)
