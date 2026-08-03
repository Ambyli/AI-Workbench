"""
GMAIL CLIENT — direct Gmail API listener via google-auth-oauthlib.

Reverted from the Gmail MCP variant because the MCP isn't ready in time. When
a Gmail MCP is available, we can swap back — the Contract A output shape is
unchanged so the parser/orchestrator don't care which backend delivered the
mail.

Produces, per unread Roofix email, the raw shape the parser expects:
    {
      "sender": str,        # e.g. "RFX | New Comment <no-reply@roofix.io>"
      "subject": str,
      "body_text": str,
      "body_html": str | None,
      "timestamp": str,     # ISO
      "to": [str],
      "message_id": str,    # Gmail message id (for mark-as-read)
      "attachments": [{"filename": str, "mime_type": str}],
    }

Auth: OAuth 2.0. credentials.json + token.json live in a mounted config/
volume (git-ignored). credentials.json is the client secrets you download
from GCP; token.json is the refresh token written on first successful login.

First-time login is INTERACTIVE — run the listener once with a browser
available, complete the OAuth flow, then ship the resulting token.json into
the container (or mount the same config path).
"""

from __future__ import annotations

# Load .env before the module-level env reads below, so this file works both
# when imported by app.py (which also loads .env) and when run directly as
# __main__ for the smoke test. load_env is idempotent — safe to call twice.
from common.env import load_env

load_env()

import base64
from typing import Optional
import html as _html
import os
import threading
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# Constants live in components/constants.py; re-imported here (with the local
# aliases the rest of the file already uses) so `from components.gmail_client
# import QUERY` etc. keeps working.
from components.constants import (
    GMAIL_SCOPES as SCOPES,
    ROOFIX_SENDER,
    GMAIL_CREDENTIALS_PATH as CREDENTIALS_PATH,
    GMAIL_TOKEN_PATH as TOKEN_PATH,
    LISTENER_QUERY as QUERY,
    STYLE_SCRIPT_RE as _STYLE_SCRIPT_RE,
    BR_RE as _BR_RE,
    BLOCK_CLOSE_RE as _BLOCK_CLOSE_RE,
    TAG_RE as _TAG_RE,
    INLINE_WS_RE as _INLINE_WS_RE,
    MULTI_NL_RE as _MULTI_NL_RE,
)


def _html_to_text(html: str) -> str:
    """Cheap HTML -> plaintext for the body fallback: some Roofix notifications
    have no text/plain part, and without this the message would fall through
    to Gmail's snippet (hard-capped ~200 chars), truncating comments and
    quotes."""
    if not html:
        return ""
    s = _STYLE_SCRIPT_RE.sub("", html)
    s = _BR_RE.sub("\n", s)
    s = _BLOCK_CLOSE_RE.sub("\n", s)
    s = _TAG_RE.sub("", s)
    s = _html.unescape(s)
    s = _INLINE_WS_RE.sub(" ", s)
    s = _MULTI_NL_RE.sub("\n\n", s)
    return s.strip()


class GmailClient:
    def __init__(self):
        # googleapiclient's Resource / httplib2.Http is documented as NOT
        # thread-safe. Concurrent .execute() calls from multiple worker
        # threads on the same service can corrupt httplib2's internal state
        # (segfault-adjacent). Use threading.local so each thread lazily
        # builds its own service on first access; underneath, credentials
        # are shared and refresh is thread-safe per google-auth's design.
        self._local = threading.local()
        self._creds: Optional[Credentials] = None
        self._creds_lock = threading.Lock()
        # Cache of label name → id, populated on first lookup. Gmail label
        # ids are stable per user, so this only misses once per process
        # lifetime. Avoids listing all labels on every tick. CPython's GIL
        # keeps dict.get / assignment atomic; setdefault handles the race.
        self._label_id_cache: dict[str, str] = {}

    def _auth(self) -> Credentials:
        """Load or refresh OAuth credentials.

        Shared across threads — google-auth's Credentials object is
        documented as thread-safe (refresh uses an internal lock). We
        additionally guard the token.json read/write pair here so two
        concurrent expired-token threads don't race on the file.
        """
        if self._creds is not None and self._creds.valid:
            return self._creds
        with self._creds_lock:
            if self._creds is not None and self._creds.valid:
                return self._creds
            creds = None
            if os.path.exists(TOKEN_PATH):
                creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
            if not creds or not creds.valid:
                if creds and creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                else:
                    flow = InstalledAppFlow.from_client_secrets_file(
                        CREDENTIALS_PATH, SCOPES
                    )
                    creds = flow.run_local_server(port=0)
                os.makedirs(os.path.dirname(TOKEN_PATH) or ".", exist_ok=True)
                with open(TOKEN_PATH, "w") as f:
                    f.write(creds.to_json())
            self._creds = creds
            return creds

    def service(self):
        """Return the calling thread's Gmail service instance, building it
        on first access. Each thread gets its own httplib2.Http so parallel
        ``.execute()`` calls don't clobber each other's connection state."""
        svc = getattr(self._local, "service", None)
        if svc is None:
            svc = build("gmail", "v1", credentials=self._auth(), cache_discovery=False)
            self._local.service = svc
        return svc

    def close(self) -> None:
        # Present for API parity with the MCP client's context-manager form.
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # --- helpers ----------------------------------------------------------------

    @staticmethod
    def _header(headers, name, default=""):
        for h in headers:
            if h.get("name", "").lower() == name.lower():
                return h.get("value", default)
        return default

    @staticmethod
    def _decode(data: str) -> str:
        if not data:
            return ""
        return base64.urlsafe_b64decode(data.encode()).decode("utf-8", errors="replace")

    def _extract_bodies(self, payload) -> tuple[str, str | None]:
        """Return (text, html). Walks multipart parts."""
        text, html = "", None
        mimetype = payload.get("mimeType", "")
        body_data = payload.get("body", {}).get("data")
        if mimetype == "text/plain" and body_data:
            text = self._decode(body_data)
        elif mimetype == "text/html" and body_data:
            html = self._decode(body_data)
        for part in payload.get("parts", []) or []:
            t, h = self._extract_bodies(part)
            if t and not text:
                text = t
            if h and not html:
                html = h
        return text, html

    @staticmethod
    def _attachments(payload) -> list:
        out = []
        for part in payload.get("parts", []) or []:
            fn = part.get("filename")
            if fn:
                out.append({"filename": fn, "mime_type": part.get("mimeType", "")})
            out.extend(GmailClient._attachments(part))
        return out

    # --- public -----------------------------------------------------------------

    def _get_message(self, message_id: str):
        """Fetch one Gmail message by id with the same retry policy as ``fetch``.

        Returns the raw API dict or ``None`` if all retries exhausted. A
        non-transient HttpError (e.g. 404) propagates so the caller can
        distinguish "message doesn't exist" from "transient failure."
        """
        import time
        from googleapiclient.errors import HttpError

        svc = self.service()
        for attempt in range(3):
            try:
                return svc.users().messages().get(
                    userId="me", id=message_id, format="full").execute()
            except HttpError as e:
                if e.resp.status in (429, 500, 502, 503, 504):
                    time.sleep(2 ** attempt + 0.5)
                    continue
                raise
        return None

    def _to_email_dict(self, msg: dict) -> dict:
        """Convert a raw Gmail API message dict into the flat "Contract A"
        dict every downstream component (parser, brain, orchestrator) expects."""
        payload = msg.get("payload", {})
        headers = payload.get("headers", [])
        text, html = self._extract_bodies(payload)
        ts = self._header(headers, "Date")
        try:
            ts_iso = parsedate_to_datetime(ts).astimezone(timezone.utc).isoformat()
        except Exception:
            ts_iso = datetime.now(timezone.utc).isoformat()
        return {
            "sender": self._header(headers, "From"),
            "subject": self._header(headers, "Subject"),
            "body_text": text or _html_to_text(html) or msg.get("snippet", ""),
            "body_html": html,
            "timestamp": ts_iso,
            "to": [a.strip() for a in self._header(headers, "To").split(",") if a],
            "message_id": msg.get("id"),
            "attachments": self._attachments(payload),
        }

    def fetch(self, max_results: int = 25, query: str | None = None) -> list:
        """Return raw emails (Contract A) for matching mail. Does NOT mark
        read — the caller marks read only after successful processing, so a
        crash never silently drops an event.

        Retries on transient API errors (rate limits, precondition failures)
        with exponential backoff. Skips messages that fail after all retries.
        """
        svc = self.service()
        q = query or QUERY
        resp = svc.users().messages().list(
            userId="me", q=q, maxResults=max_results).execute()
        out = []
        for ref in resp.get("messages", []):
            msg = self._get_message(ref["id"])
            if msg is None:
                continue
            out.append(self._to_email_dict(msg))
        return out

    def fetch_one(self, message_id: str) -> dict | None:
        """Return one Contract-A email dict for ``message_id`` regardless of
        read/unread state, or ``None`` if the message isn't retrievable.

        Used by the manual ``/execute/{message_id}`` endpoint to re-run a
        specific email through the pipeline — skips the label query used by
        ``fetch``. A 404 from Gmail (message doesn't exist / not visible to
        this token) propagates as ``HttpError`` so the caller can 404 too.
        """
        from googleapiclient.errors import HttpError
        try:
            msg = self._get_message(message_id)
        except HttpError as e:
            if e.resp.status == 404:
                return None
            raise
        return self._to_email_dict(msg) if msg else None

    def mark_read(self, message_id: str) -> None:
        self.service().users().messages().modify(
            userId="me", id=message_id, body={"removeLabelIds": ["UNREAD"]}).execute()

    def get_or_create_label(self, name: str) -> str:
        """Return the Gmail label id for ``name``, creating it if missing.

        Cached in-process — first tick after boot pays for a `labels.list`;
        subsequent ticks hit the cache. If the operator deletes the label
        in Gmail's UI while the container is running, we'll try to re-create
        it on the next call (create() raises 409 on collision, which we
        treat as "someone else created it in the meantime" and re-list).
        """
        cached = self._label_id_cache.get(name)
        if cached:
            return cached
        svc = self.service()
        listing = svc.users().labels().list(userId="me").execute()
        for lb in listing.get("labels", []):
            if lb.get("name") == name:
                self._label_id_cache[name] = lb["id"]
                return lb["id"]
        # Not found — create. `nameSpace/child` in the display name gives
        # a nested folder in Gmail's sidebar without needing separate calls
        # for the parent, which matches how operators expect to see it.
        created = svc.users().labels().create(
            userId="me",
            body={
                "name": name,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
            },
        ).execute()
        self._label_id_cache[name] = created["id"]
        return created["id"]

    def apply_label(self, message_id: str, label_id: str) -> None:
        """Add a single label to a message. Idempotent — Gmail returns 200
        with no side effect if the label was already present."""
        self.service().users().messages().modify(
            userId="me",
            id=message_id,
            body={"addLabelIds": [label_id]},
        ).execute()

    def forward_email(self, to: list[str], reason: str, original: dict,
                      event_type: str = "", record: dict | None = None) -> None:
        """Forward `original` (a raw email dict from `fetch()`) to `to`,
        prefaced with the escalation `reason`, the parsed `event_type`, and
        (if provided) the ``record`` — the bridge's own view of the parsed
        event + brain decision so the operator can see what the bridge saw
        without having to reconstruct it from the raw email.

        ``record`` is rendered as a pretty-printed JSON block between the
        header and the forwarded original. Typical shape from the caller:
        ``{"event": {...parsed fields...}, "decision": {...}}``. Include the
        ctx too if it's in scope. Non-JSON-serializable values are stringified
        via ``default=str``.

        Preserves both text and html parts of the original when available so
        the recipient sees the full formatting Roofix used.

        No-op if `to` is empty. Raises the underlying HttpError on send
        failure — the caller (orchestrator) decides how to handle it.

        Uses the same ``gmail.modify`` scope as fetch/mark_read — no re-consent
        needed on existing tokens.
        """
        if not to:
            return
        import json as _json
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart

        orig_subject = original.get("subject") or "(no subject)"
        orig_sender = original.get("sender") or ""
        orig_ts = original.get("timestamp") or ""
        orig_to = ", ".join(original.get("to") or [])
        orig_text = original.get("body_text") or ""
        orig_html = original.get("body_html")

        record_block = ""
        if record is not None:
            try:
                record_json = _json.dumps(
                    record, indent=2, sort_keys=False, default=str
                )
            except Exception as e:
                record_json = f"(record could not be serialized: {e})"
            record_block = (
                "--- Bridge record (what the bridge saw + decided) ---\n"
                f"{record_json}\n"
                "\n"
            )

        header = (
            "[Roofix Bridge] This email was escalated for human review.\n"
            "\n"
            f"Event type: {event_type or '(unknown)'}\n"
            f"Reason: {reason}\n"
            "\n"
            f"{record_block}"
            "--- Forwarded message ---\n"
            f"From: {orig_sender}\n"
            f"Date: {orig_ts}\n"
            f"Subject: {orig_subject}\n"
            f"To: {orig_to}\n"
            "\n"
        )

        text_part = MIMEText(header + orig_text, "plain", "utf-8")

        if orig_html:
            # Wrap the plaintext header in <pre> for the html part so it still
            # renders as readable context above the original html body.
            import html as _std_html
            html_header = f"<pre>{_std_html.escape(header)}</pre>"
            msg = MIMEMultipart("alternative")
            msg.attach(text_part)
            msg.attach(MIMEText(html_header + orig_html, "html", "utf-8"))
        else:
            msg = text_part

        msg["to"] = ", ".join(to)
        msg["subject"] = f"[Roofix Escalation] {orig_subject}"
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        self.service().users().messages().send(
            userId="me", body={"raw": raw}).execute()


def make_listener_callable(max_results: int = 25):
    """Return a zero-arg callable for orchestrator.run(listener=...)."""
    gc = GmailClient()
    return lambda: gc.fetch(max_results=max_results)


if __name__ == "__main__":
    # Server / laptop smoke test: print what the listener sees. Marks nothing read.
    import traceback
    print(f"GmailClient starting. query={QUERY}")
    print(f"credentials: {CREDENTIALS_PATH}  token: {TOKEN_PATH}")
    try:
        gc = GmailClient()
        emails = gc.fetch()
        print(f"\nfetched {len(emails)} email(s)\n")
        for e in emails[:10]:
            print(f"- {e['subject'][:70]}  | from {e['sender'][:40]}  | to {e['to']}")
    except Exception:
        print("\n!!! Fetch failed:\n")
        traceback.print_exc()
