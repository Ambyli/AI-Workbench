"""Email delivery adapter — Google Workspace (Gmail API) placeholder.

The disposition pipeline renders a handoff email and hands it here to be sent.
Two delivery paths, chosen by `settings.EMAIL_SEND_ENABLED`:

    false (default) -> InboxStoreMailer: "delivers" to the in-memory Inbox the
                       demo UI reads (backend/email_render._STORE). No real mail
                       leaves the machine. This keeps the prototype fully usable.
    true            -> GoogleWorkspaceMailer: sends via the Gmail API using a
                       service account with domain-wide delegation. Stubbed with
                       the exact call shape; flip the flag + provide creds to arm.

Like the CRM adapter, the contract is a small Protocol so swapping providers (or
adding SMTP/SendGrid later) touches only this file.
"""
from __future__ import annotations

import logging
import mimetypes
import os
from typing import Optional, Protocol

from .config import settings
from .schemas import EmailMessage, SendResult

logger = logging.getLogger("chatbot.mailer")


# ---------------------------------------------------------------------------
# MIME construction (pure — no Google deps, fully unit-testable)
# ---------------------------------------------------------------------------
def _safe_upload_path(filename: str) -> Optional["os.PathLike"]:
    """Resolve an attachment filename to a path INSIDE the uploads dir, or None.

    Defense in depth: attachment names already pass the Pydantic edge validator
    (no traversal, safe charset) and are UUID names, but the mailer independently
    rejects anything with a path separator or '..' before touching disk.
    """
    from .upload import UPLOAD_DIR

    base = os.path.basename(filename)
    if not filename or base != filename or filename in {".", ".."}:
        logger.warning("Mailer: rejecting suspicious attachment name %r.", filename)
        return None
    return UPLOAD_DIR / base


def build_mime(message: EmailMessage):
    """Build an email.message object for `message`, attaching any upload files.

    Returns a MIMEText when there are no attachments, else a multipart/mixed with
    the HTML body plus each attachment as a base64 part. Missing attachment files
    are skipped with a warning (delivery of the email itself must not fail).
    """
    from email.mime.base import MIMEBase
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email import encoders

    present = []
    for fname in message.attachments:
        path = _safe_upload_path(fname)
        if path is None or not os.path.exists(path):
            logger.warning("Mailer: attachment %r not found in uploads — skipping.", fname)
            continue
        present.append(path)

    if not present:
        mime = MIMEText(message.html, "html", "utf-8")
    else:
        mime = MIMEMultipart("mixed")
        mime.attach(MIMEText(message.html, "html", "utf-8"))
        for path in present:
            with open(path, "rb") as fh:
                data = fh.read()
            ctype, _enc = mimetypes.guess_type(str(path))
            maintype, _, subtype = (ctype or "application/octet-stream").partition("/")
            part = MIMEBase(maintype, subtype or "octet-stream")
            part.set_payload(data)
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", "attachment", filename=os.path.basename(path))
            mime.attach(part)

    mime["To"] = message.to
    mime["From"] = message.sender or settings.EMAIL_FROM
    mime["Subject"] = message.subject
    if message.reply_to:
        mime["Reply-To"] = message.reply_to
    return mime


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------
class Mailer(Protocol):
    def send(self, message: EmailMessage) -> SendResult:
        ...


# ---------------------------------------------------------------------------
# Dev / default backend — deliver to the in-memory Inbox store.
# ---------------------------------------------------------------------------
class InboxStoreMailer:
    """No-network mailer: logs the send and reports success.

    The actual EmailRecord is persisted by the pipeline via email_render, which
    powers the Inbox tab — so there's nothing to write here. This class exists so
    the pipeline can call `send()` unconditionally regardless of configuration.
    """

    def send(self, message: EmailMessage) -> SendResult:
        logger.info("Inbox(no-send) handoff -> %s | subject=%r", message.to, message.subject)
        return SendResult(
            delivered=True,
            provider="inbox_store",
            detail="EMAIL_SEND_ENABLED is false — delivered to in-memory Inbox only.",
        )


# ---------------------------------------------------------------------------
# Google Workspace backend (placeholder — arm with creds + flag).
# ---------------------------------------------------------------------------
class GoogleWorkspaceMailer:
    """Send via the Gmail API using a service account + domain-wide delegation.

    The send path is fully implemented (MIME with photo attachments; drafts then
    sends per GMAIL_CREATE_DRAFT). To arm it, install `google-api-python-client`
    / `google-auth` (see requirements.txt), set EMAIL_SEND_ENABLED=true, and
    provide:
        GOOGLE_SA_CREDENTIALS_FILE  — path to the service-account JSON key
        GOOGLE_DELEGATED_USER       — the Workspace user the SA impersonates
        EMAIL_FROM                  — the From address

    If creds are missing or a send raises (including the google libs not being
    installed), it falls back to the Inbox store so a misconfiguration never
    hard-fails an intake turn.
    """

    def send(self, message: EmailMessage) -> SendResult:
        creds_file = settings.GOOGLE_SA_CREDENTIALS_FILE
        delegated = settings.GOOGLE_DELEGATED_USER
        if not creds_file or not delegated:
            logger.warning(
                "GoogleWorkspaceMailer missing creds (GOOGLE_SA_CREDENTIALS_FILE / "
                "GOOGLE_DELEGATED_USER) — falling back to Inbox store."
            )
            return InboxStoreMailer().send(message)

        try:
            return self._send_via_gmail(message, creds_file, delegated)
        except Exception as exc:  # noqa: BLE001 - never let delivery crash the turn
            logger.error("GoogleWorkspaceMailer send failed (%s) — falling back to Inbox store.", exc)
            res = InboxStoreMailer().send(message)
            res.detail = f"Gmail send failed: {exc}"
            res.delivered = False
            return res

    # Gmail OAuth scopes: compose (create drafts) + send.
    _SCOPES = [
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/gmail.send",
    ]

    def _build_service(self, creds_file: str, delegated: str):
        """Build an authenticated Gmail API client (service-account + DWD).

        Imported lazily so google-api-python-client / google-auth are only a
        dependency when real sending is armed (EMAIL_SEND_ENABLED=true).
        """
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        creds = service_account.Credentials.from_service_account_file(
            creds_file, scopes=self._SCOPES, subject=delegated,
        )
        return build("gmail", "v1", credentials=creds, cache_discovery=False)

    def _deliver(self, service, message: EmailMessage) -> SendResult:
        """Encode `message` (with attachments) and deliver it via `service`.

        Split out from _send_via_gmail so tests can inject a fake Gmail service
        and exercise the full draft/send + MIME-encoding logic offline. When
        GMAIL_CREATE_DRAFT is true (default) it drafts then sends the draft
        ("draft and send"); otherwise it sends directly.
        """
        import base64

        mime = build_mime(message)
        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()

        if settings.GMAIL_CREATE_DRAFT:
            draft = service.users().drafts().create(
                userId="me", body={"message": {"raw": raw}},
            ).execute()
            sent = service.users().drafts().send(
                userId="me", body={"id": draft["id"]},
            ).execute()
            return SendResult(
                delivered=True,
                provider="google_workspace",
                message_id=sent.get("id", ""),
                detail=f"drafted then sent (draft {draft.get('id', '')})",
            )

        sent = service.users().messages().send(
            userId="me", body={"raw": raw},
        ).execute()
        return SendResult(
            delivered=True,
            provider="google_workspace",
            message_id=sent.get("id", ""),
            detail="sent directly",
        )

    def _send_via_gmail(self, message: EmailMessage, creds_file: str, delegated: str) -> SendResult:
        service = self._build_service(creds_file, delegated)
        return self._deliver(service, message)


# ---------------------------------------------------------------------------
# Backend selection + routing
# ---------------------------------------------------------------------------
def get_mailer() -> Mailer:
    if settings.EMAIL_SEND_ENABLED:
        return GoogleWorkspaceMailer()
    return InboxStoreMailer()


def route_for(case: dict, matched: bool) -> str:
    """Decide which mailbox a finalized case is addressed to.

    - Solar production concerns -> the performance team (policy deflection).
    - A confident CRM match      -> the disposition queue.
    - No match                   -> the unverified-intake review mailbox.
    """
    if case.get("issue_type") == "solar":
        return settings.MAILBOX_SOLAR
    if matched:
        return settings.MAILBOX_DISPOSITION
    return settings.MAILBOX_UNVERIFIED


def send(message: EmailMessage) -> SendResult:
    return get_mailer().send(message)
