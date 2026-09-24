"""Integration tests for the Gmail send (with attachments) + Google Chat notifier.

All offline: a FAKE Gmail service exercises the real draft/send + MIME logic, and
httpx.post is monkeypatched to capture the Chat webhook payload. No creds, no
network, nothing sent to real inboxes. Run:
    USE_MOCK_LLM=1 venv/bin/python integration_gmail_chat_test.py
"""
import base64
import io
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

from email import message_from_bytes
from email.header import decode_header, make_header

from PIL import Image
from fastapi.testclient import TestClient

from backend import chat_notifier, mailer, pipeline
from backend.main import app
from backend.schemas import ChatNotification, EmailMessage

c = TestClient(app)
c.__enter__()


def _upload_png() -> str:
    """Upload a real PNG and return its stored (UUID) filename."""
    buf = io.BytesIO()
    Image.new("RGB", (20, 20), (10, 90, 200)).save(buf, "PNG")
    raw = buf.getvalue()
    r = c.post("/api/upload", files={"file": ("roof.png", raw, "image/png")})
    assert r.status_code == 200, r.text
    return r.json()["filename"], raw


# ---------------------------------------------------------------------------
# Fake Gmail service — records calls, mimics the users().drafts()/messages() API
# ---------------------------------------------------------------------------
class _Exec:
    def __init__(self, result, log, label, body):
        self._result = result
        log.append((label, body))

    def execute(self):
        return self._result


class _Drafts:
    def __init__(self, log):
        self.log = log

    def create(self, userId, body):
        return _Exec({"id": "draft-123"}, self.log, "drafts.create", body)

    def send(self, userId, body):
        return _Exec({"id": "msg-sent-456"}, self.log, "drafts.send", body)


class _Messages:
    def __init__(self, log):
        self.log = log

    def send(self, userId, body):
        return _Exec({"id": "msg-direct-789"}, self.log, "messages.send", body)


class _Users:
    def __init__(self, log):
        self._d = _Drafts(log)
        self._m = _Messages(log)

    def drafts(self):
        return self._d

    def messages(self):
        return self._m


class FakeGmail:
    def __init__(self):
        self.log = []
        self._users = _Users(self.log)

    def users(self):
        return self._users


# ---------------------------------------------------------------------------
# MIME construction
# ---------------------------------------------------------------------------
def test_mime_no_attachments():
    print("Gmail: MIME without attachments...")
    m = mailer.build_mime(EmailMessage(
        to="service-team@zeoenergy.com", subject="Hi", html="<b>body</b>",
        sender="bot@zeoenergy.com", reply_to="pat@example.com",
    ))
    assert m.get_content_type() == "text/html"
    assert m["To"] == "service-team@zeoenergy.com"
    assert m["From"] == "bot@zeoenergy.com"
    assert m["Subject"] == "Hi"
    assert m["Reply-To"] == "pat@example.com"
    print("  ok: single text/html part with correct headers")


def test_mime_with_attachment():
    print("Gmail: MIME with a real photo attachment...")
    fname, raw = _upload_png()
    m = mailer.build_mime(EmailMessage(
        to="x@zeoenergy.com", subject="Roof case", html="<p>see photo</p>",
        attachments=[fname],
    ))
    assert m.is_multipart(), "should be multipart when attachments present"
    parts = m.get_payload()
    html_parts = [p for p in parts if p.get_content_type() == "text/html"]
    att_parts = [p for p in parts if p.get("Content-Disposition", "").startswith("attachment")]
    assert html_parts and att_parts, "must have an html body + an attachment part"
    att = att_parts[0]
    assert fname in att.get("Content-Disposition"), "attachment keeps the stored filename"
    assert att.get_payload(decode=True) == raw, "attachment bytes must match the uploaded file"
    print("  ok: multipart with html + base64 photo, bytes round-trip")


def test_mime_missing_attachment_skipped():
    print("Gmail: missing/suspicious attachment is skipped, email still builds...")
    m = mailer.build_mime(EmailMessage(
        to="x@zeoenergy.com", subject="s", html="<p>b</p>",
        attachments=["../etc/passwd", "does-not-exist.png"],
    ))
    # Both rejected/missing -> falls back to a plain text/html message, no crash.
    assert m.get_content_type() == "text/html"
    print("  ok: traversal + missing names skipped; delivery not blocked")


# ---------------------------------------------------------------------------
# Gmail draft-and-send / direct-send orchestration (fake service)
# ---------------------------------------------------------------------------
def test_gmail_draft_then_send():
    print("Gmail: draft-then-send orchestration...")
    os.environ["GMAIL_CREATE_DRAFT"] = "true"
    fname, raw = _upload_png()
    fake = FakeGmail()
    res = mailer.GoogleWorkspaceMailer()._deliver(fake, EmailMessage(
        to="service-team@zeoenergy.com", subject="Roof — Pat", html="<p>hi</p>",
        attachments=[fname],
    ))
    labels = [label for label, _ in fake.log]
    assert labels == ["drafts.create", "drafts.send"], f"expected draft then send, got {labels}"
    assert res.delivered and res.provider == "google_workspace"
    assert res.message_id == "msg-sent-456"
    # The raw MIME handed to Gmail must decode back to a multipart with the photo.
    raw_b64 = fake.log[0][1]["message"]["raw"]
    decoded = message_from_bytes(base64.urlsafe_b64decode(raw_b64))
    assert decoded.is_multipart(), "raw MIME should be multipart (has an attachment)"
    subject = str(make_header(decode_header(decoded["Subject"])))
    assert subject == "Roof — Pat", f"subject round-trip failed: {subject!r}"
    assert any(p.get("Content-Disposition", "").startswith("attachment")
               for p in decoded.get_payload()), "decoded MIME must carry the attachment"
    print("  ok: create->send, correct message_id, raw carries the attachment")


def test_gmail_direct_send():
    print("Gmail: direct-send mode (GMAIL_CREATE_DRAFT=false)...")
    os.environ["GMAIL_CREATE_DRAFT"] = "false"
    fake = FakeGmail()
    res = mailer.GoogleWorkspaceMailer()._deliver(fake, EmailMessage(
        to="x@zeoenergy.com", subject="s", html="<p>b</p>",
    ))
    assert [label for label, _ in fake.log] == ["messages.send"]
    assert res.message_id == "msg-direct-789"
    os.environ["GMAIL_CREATE_DRAFT"] = "true"  # restore default
    print("  ok: single messages.send call")


# ---------------------------------------------------------------------------
# Google Chat notifier
# ---------------------------------------------------------------------------
def test_chat_space_routing():
    print("Chat: space routing mirrors mailer.route_for...")
    assert chat_notifier.space_for({"issue_type": "solar"}, True) == "solar"
    assert chat_notifier.space_for({"issue_type": "roof"}, True) == "disposition"
    assert chat_notifier.space_for({"issue_type": "roof"}, False) == "unverified"
    print("  ok: solar->solar, matched->disposition, else->unverified")


def test_chat_stub_feed():
    print("Chat: stub backend records to the in-app feed...")
    chat_notifier._FEED.clear()
    n = ChatNotification(space="disposition", team_label="Service team",
                         title="Roof case — Pat", text="new case", fields=[("Issue", "Roof")])
    res = chat_notifier.StubChatNotifier().notify(n)
    assert res.delivered and res.provider == "stub_feed"
    feed = chat_notifier.list_notifications()
    assert feed and feed[0]["title"] == "Roof case — Pat"
    assert feed[0]["fields"] == [["Issue", "Roof"]]
    print("  ok: feed entry created, newest-first, fields preserved")


def test_chat_webhook_payload():
    print("Chat: webhook backend posts a cardsV2 payload to the team URL...")
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json

        class _R:
            def raise_for_status(self):
                return None
        return _R()

    orig = chat_notifier.httpx.post
    chat_notifier.httpx.post = fake_post
    os.environ["CHAT_SEND_ENABLED"] = "true"
    os.environ["CHAT_BACKEND"] = "webhook"
    os.environ["CHAT_WEBHOOK_SOLAR"] = "https://chat.googleapis.com/v1/spaces/AAA/messages?key=k&token=t"
    try:
        n = ChatNotification(space="solar", title="Solar case — Pat",
                             text="new solar case", fields=[("Issue", "Solar"), ("Urgency", "8/10")],
                             thread_key="CASE-1")
        res = chat_notifier.get_notifier().notify(n)
        assert res.delivered and res.provider == "google_chat_webhook"
        assert captured["url"].startswith("https://chat.googleapis.com/")
        body = captured["json"]
        assert body["text"] == "new solar case"
        assert body["cardsV2"][0]["card"]["header"]["title"] == "Solar case — Pat"
        assert body["thread"]["threadKey"] == "CASE-1"
        widgets = body["cardsV2"][0]["card"]["sections"][0]["widgets"]
        labels = [w["decoratedText"]["topLabel"] for w in widgets]
        assert "Issue" in labels and "Urgency" in labels
    finally:
        chat_notifier.httpx.post = orig
        for k in ("CHAT_SEND_ENABLED", "CHAT_BACKEND", "CHAT_WEBHOOK_SOLAR"):
            os.environ.pop(k, None)
    print("  ok: correct URL, cardsV2 title, thread key, field widgets")


def test_chat_webhook_unconfigured_falls_back():
    print("Chat: webhook enabled but URL missing -> safe feed fallback...")
    os.environ["CHAT_SEND_ENABLED"] = "true"
    os.environ["CHAT_BACKEND"] = "webhook"
    try:
        res = chat_notifier.get_notifier().notify(
            ChatNotification(space="disposition", title="t", text="x"))
        assert res.provider == "stub_feed", "missing webhook URL should fall back to feed"
        assert res.delivered
    finally:
        for k in ("CHAT_SEND_ENABLED", "CHAT_BACKEND"):
            os.environ.pop(k, None)
    print("  ok: falls back to in-app feed, no crash")


# ---------------------------------------------------------------------------
# Pipeline wiring: attachments reach the mailer + chat gets notified
# ---------------------------------------------------------------------------
def test_pipeline_wires_attachments_and_chat():
    print("Pipeline: dispatch attaches photos to email + notifies chat...")
    fname, raw = _upload_png()
    chat_notifier._FEED.clear()
    captured = {}
    orig_send = mailer.send

    def capture_send(message):
        captured["msg"] = message
        return orig_send(message)

    mailer.send = capture_send
    try:
        case = {
            "session_id": "pipe1", "mode": "standard", "issue_type": "misc",
            "name": "Pat Tester", "account_name": "Pat Tester",
            "account_address": "500 Test Blvd, Tampa, FL", "contact": "pat@example.com",
            "urgency": 6, "what_damaged": "the fence gate", "cause_of_damage": "wind",
            "verbatim": ["the fence gate was torn off"], "attachments": [fname],
            "_unconfirmed": [],
        }
        result = pipeline.dispatch(case)
    finally:
        mailer.send = orig_send
    assert captured["msg"].attachments == [fname], "uploaded photo must reach the mailer"
    assert result.chat_notified is True, "chat notification should fire"
    assert result.chat_space == "unverified", "unmatched misc -> unverified team space"
    feed = chat_notifier.list_notifications()
    assert feed and feed[0]["attachment_count"] == 1, "chat card reports the attachment count"
    print("  ok: attachment on email, chat notified, routed to", result.chat_space)


def test_chat_notifications_endpoint():
    print("API: /api/chat-notifications returns the feed...")
    r = c.get("/api/chat-notifications")
    assert r.status_code == 200
    assert isinstance(r.json(), list)
    print("  ok: endpoint returns", len(r.json()), "notifications")


if __name__ == "__main__":
    test_mime_no_attachments()
    test_mime_with_attachment()
    test_mime_missing_attachment_skipped()
    test_gmail_draft_then_send()
    test_gmail_direct_send()
    test_chat_space_routing()
    test_chat_stub_feed()
    test_chat_webhook_payload()
    test_chat_webhook_unconfigured_falls_back()
    test_pipeline_wires_attachments_and_chat()
    test_chat_notifications_endpoint()
    print("\nALL GMAIL/CHAT INTEGRATION CHECKS PASSED")
