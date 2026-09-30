"""Security tests for input validation, sanitization, and prompt injection hardening."""
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
from backend.llm import sanitize_user_input

c = TestClient(app)
c.__enter__()


def test_input_sanitization():
    print("Testing input sanitization...")
    # XML/HTML tags should be stripped
    assert sanitize_user_input("<script>alert(1)</script>") == "alert(1)"
    assert sanitize_user_input("</customer_message> hello") == "hello"
    assert sanitize_user_input("<customer_message>hello") == "hello"
    assert sanitize_user_input("some <tag> content") == "some  content"
    # Length limits
    huge = "a" * 2000
    sanitized_huge = sanitize_user_input(huge)
    assert len(sanitized_huge) == 1000
    print("  ✓ Sanitization checks passed")


def test_session_id_validation():
    print("Testing session ID validation...")
    # Valid session ID
    r = c.post("/api/chat", json={"session_id": "valid_session-123", "message": ""})
    assert r.status_code == 200

    # Invalid session ID (path traversal attempt)
    r = c.post("/api/chat", json={"session_id": "../invalid/session", "message": ""})
    assert r.status_code == 422
    assert "session_id" in r.text

    # Too long session ID
    r = c.post("/api/chat", json={"session_id": "a" * 65, "message": ""})
    assert r.status_code == 422
    print("  ✓ Session ID validation checks passed")


def test_message_length_validation():
    print("Testing message length validation...")
    # Too long message (over 1000 characters)
    r = c.post("/api/chat", json={"session_id": "test_session", "message": "a" * 1001})
    assert r.status_code == 422
    assert "message" in r.text
    print("  ✓ Message length validation checks passed")


def test_attachment_validation():
    print("Testing attachment validation...")
    # Path traversal in filename
    r = c.post("/api/chat", json={
        "session_id": "test_session",
        "message": "skip",
        "attachments": ["../../etc/passwd"]
    })
    assert r.status_code == 422
    assert "attachments" in r.text

    # Invalid characters in filename
    r = c.post("/api/chat", json={
        "session_id": "test_session",
        "message": "skip",
        "attachments": ["photo<script>.png"]
    })
    assert r.status_code == 422

    # Too many attachments
    r = c.post("/api/chat", json={
        "session_id": "test_session",
        "message": "skip",
        "attachments": [f"photo{i}.jpg" for i in range(11)]
    })
    assert r.status_code == 422
    print("  ✓ Attachment validation checks passed")


def test_lookup_endpoint_validation():
    print("Testing lookup endpoint validation...")
    # Extremely long name/address/email to crash or overflow lookup
    r = c.post("/api/lookup", json={
        "name": "a" * 256,
        "address": "b" * 256,
        "email": "c" * 256
    })
    assert r.status_code == 422
    print("  ✓ Lookup endpoint validation checks passed")


def test_representative_routing():
    print("Testing representative routing...")

    # Case 1: Fresh session, immediately asks for representative. With no issue type
    # known yet, the bot walks them through it by first asking which team applies.
    r = c.post("/api/chat", json={"session_id": "rep_test_1", "message": "I need a representative"})
    assert r.status_code == 200
    res = r.json()
    assert res["done"] is False
    assert any("right team" in m["text"].lower() or "which best describes" in m["text"].lower()
               for m in res["messages"]), "should ask which team first"
    assert res["quick_replies"], "should offer team quick replies"
    # Pick a team -> routed to the matching department with full handoff details.
    r = c.post("/api/chat", json={"session_id": "rep_test_1", "message": "Solar production"})
    res = r.json()
    assert res["done"] is True
    text = res["messages"][-1]["text"]
    assert "727-349-4057" in text and "727-382-0075" not in text
    print("  ✓ Fresh session guided handoff passed")

    # Case 2: Active session with home damage/roof issue, then asks for representative
    c.post("/api/chat", json={"session_id": "rep_test_2", "message": ""}) # greeting
    c.post("/api/chat", json={"session_id": "rep_test_2", "message": "John Doe"})      # name
    c.post("/api/chat", json={"session_id": "rep_test_2", "message": "John Doe"})      # account name
    c.post("/api/chat", json={"session_id": "rep_test_2", "message": "123 Main St"})   # account address
    c.post("/api/chat", json={"session_id": "rep_test_2", "message": "555-123-4567"})  # contact
    c.post("/api/chat", json={"session_id": "rep_test_2", "message": "Roof"}) # issue_type is now roof
    c.post("/api/chat", json={"session_id": "rep_test_2", "message": "5"}) # urgency
    c.post("/api/chat", json={"session_id": "rep_test_2", "message": "skip"}) # third parties
    
    # Now ask for representative
    r = c.post("/api/chat", json={"session_id": "rep_test_2", "message": "connect me to an agent"})
    assert r.status_code == 200
    res = r.json()
    assert res["done"] is True
    text = res["messages"][-1]["text"]
    assert "727-382-0075" in text
    assert "727-349-4057" not in text
    print("  ✓ Roof issue session routing passed")

    # Case 3: Active session with solar issue, then asks for representative
    c.post("/api/chat", json={"session_id": "rep_test_3", "message": ""}) # greeting
    c.post("/api/chat", json={"session_id": "rep_test_3", "message": "Maria Doe"})       # name
    c.post("/api/chat", json={"session_id": "rep_test_3", "message": "Maria Doe"})       # account name
    c.post("/api/chat", json={"session_id": "rep_test_3", "message": "123 Solar St"})    # account address
    c.post("/api/chat", json={"session_id": "rep_test_3", "message": "555-987-6543"})    # contact
    c.post("/api/chat", json={"session_id": "rep_test_3", "message": "Solar not producing"}) # issue_type is solar
    c.post("/api/chat", json={"session_id": "rep_test_3", "message": "3"}) # urgency
    c.post("/api/chat", json={"session_id": "rep_test_3", "message": "skip"}) # third parties

    # Now ask for representative
    r = c.post("/api/chat", json={"session_id": "rep_test_3", "message": "speak to a representative please"})
    assert r.status_code == 200
    res = r.json()
    assert res["done"] is True
    text = res["messages"][-1]["text"]
    assert "727-349-4057" in text
    assert "727-382-0075" not in text
    print("  ✓ Solar issue session routing passed")


def test_file_upload_security():
    print("Testing file upload security...")
    import io

    # 1. Invalid content type (MIME type check)
    r = c.post(
        "/api/upload",
        files={"file": ("test.txt", io.BytesIO(b"hello world"), "text/plain")}
    )
    assert r.status_code == 400
    assert "Only JPG, PNG, GIF, and WebP are allowed" in r.json()["detail"]

    # 2. Magic byte spoofing check (MIME type matches PNG, but magic bytes are text)
    r = c.post(
        "/api/upload",
        files={"file": ("test.png", io.BytesIO(b"hello fake png"), "image/png")}
    )
    assert r.status_code == 400
    assert "File signature check failed" in r.json()["detail"]

    # 3. File too large (size check)
    huge_data = b"\x89PNG\r\n\x1a\n" + b"x" * (5 * 1024 * 1024 + 10)
    r = c.post(
        "/api/upload",
        files={"file": ("test.png", io.BytesIO(huge_data), "image/png")}
    )
    assert r.status_code == 413
    assert "File too large" in r.json()["detail"]

    # 4. Successful clean image upload (magic number validation + secure name check)
    valid_png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    r = c.post(
        "/api/upload",
        files={"file": ("malicious_../../name.png", io.BytesIO(valid_png), "image/png")}
    )
    assert r.status_code == 200
    res = r.json()
    assert "filename" in res
    filename = res["filename"]
    assert "malicious" not in filename
    assert "/" not in filename
    assert ".." not in filename
    assert filename.endswith(".png")
    print("  ✓ File upload validation and signature checks passed")


if __name__ == "__main__":
    try:
        test_input_sanitization()
        test_session_id_validation()
        test_message_length_validation()
        test_attachment_validation()
        test_lookup_endpoint_validation()
        test_representative_routing()
        test_file_upload_security()
        print("\nALL SECURITY CHECKS PASSED")
    except AssertionError as e:
        print(f"\nSECURITY CHECK FAILED: {e}")
        sys.exit(1)
