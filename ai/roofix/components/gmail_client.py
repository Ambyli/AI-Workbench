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
import os
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build


SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

ROOFIX_SENDER = os.getenv("ROOFIX_SENDER", "no-reply@roofix.io")
CREDENTIALS_PATH = os.getenv("GMAIL_CREDENTIALS_PATH", "config/credentials.json")
TOKEN_PATH = os.getenv("GMAIL_TOKEN_PATH", "config/token.json")
# Search query: unread, from the Roofix sender. Override via env for narrowing.
QUERY = os.getenv("LISTENER_QUERY") or f"is:unread from:{ROOFIX_SENDER}"


class GmailClient:
    def __init__(self):
        self._service = None

    def _auth(self):
        creds = None
        if os.path.exists(TOKEN_PATH):
            creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
                creds = flow.run_local_server(port=0)
            os.makedirs(os.path.dirname(TOKEN_PATH) or ".", exist_ok=True)
            with open(TOKEN_PATH, "w") as f:
                f.write(creds.to_json())
        return creds

    def service(self):
        if self._service is None:
            self._service = build("gmail", "v1", credentials=self._auth())
        return self._service

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

    def fetch(self, max_results: int = 25, query: str | None = None) -> list:
        """Return raw emails (Contract A) for matching mail. Does NOT mark
        read — the caller marks read only after successful processing, so a
        crash never silently drops an event.

        Retries on transient API errors (rate limits, precondition failures)
        with exponential backoff. Skips messages that fail after all retries.
        """
        import time
        from googleapiclient.errors import HttpError

        svc = self.service()
        q = query or QUERY
        resp = svc.users().messages().list(
            userId="me", q=q, maxResults=max_results).execute()
        out = []
        for ref in resp.get("messages", []):
            msg = None
            retries = 3
            for attempt in range(retries):
                try:
                    msg = svc.users().messages().get(
                        userId="me", id=ref["id"], format="full").execute()
                    break
                except HttpError as e:
                    if e.resp.status in (429, 500, 502, 503, 504):
                        wait = 2 ** attempt + 0.5
                        time.sleep(wait)
                        continue
                    raise
            if msg is None:
                continue
            payload = msg.get("payload", {})
            headers = payload.get("headers", [])
            text, html = self._extract_bodies(payload)
            ts = self._header(headers, "Date")
            try:
                ts_iso = parsedate_to_datetime(ts).astimezone(timezone.utc).isoformat()
            except Exception:
                ts_iso = datetime.now(timezone.utc).isoformat()
            out.append({
                "sender": self._header(headers, "From"),
                "subject": self._header(headers, "Subject"),
                "body_text": text or msg.get("snippet", ""),
                "body_html": html,
                "timestamp": ts_iso,
                "to": [a.strip() for a in self._header(headers, "To").split(",") if a],
                "message_id": ref["id"],
                "attachments": self._attachments(payload),
            })
        return out

    def mark_read(self, message_id: str) -> None:
        self.service().users().messages().modify(
            userId="me", id=message_id, body={"removeLabelIds": ["UNREAD"]}).execute()


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
