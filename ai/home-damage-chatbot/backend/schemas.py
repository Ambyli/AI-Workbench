"""Pydantic models for the API surface and the case record.

These define the *shapes* that cross boundaries (browser <-> FastAPI) and the
validated case record the email is rendered from. Per-slot extraction schemas
live in state_machine.py since they drive the deterministic flow.
"""
from __future__ import annotations

from enum import Enum
import re
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


class Mode(str, Enum):
    STANDARD = "standard"
    LOOKUP = "lookup"


class IssueType(str, Enum):
    ROOF = "roof"
    ELECTRICAL = "electrical"
    SOLAR = "solar"
    MISC = "misc"


# ---------------------------------------------------------------------------
# API: /api/chat
# ---------------------------------------------------------------------------
class ChatRequest(BaseModel):
    session_id: str
    mode: Mode = Mode.STANDARD
    message: str = ""
    # Mock uploads: filenames only (no real file processing in the prototype).
    attachments: list[str] = Field(default_factory=list)

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, v: str) -> str:
        if not re.match(r"^[a-zA-Z0-9_-]{1,64}$", v):
            raise ValueError("session_id must be 1-64 characters and contain only letters, numbers, underscores, or dashes")
        return v

    @field_validator("message")
    @classmethod
    def validate_message(cls, v: str) -> str:
        if len(v) > 1000:
            raise ValueError("message must be 1000 characters or less")
        return v

    @field_validator("attachments")
    @classmethod
    def validate_attachments(cls, v: list[str]) -> list[str]:
        if len(v) > 10:
            raise ValueError("At most 10 attachments are allowed per message")
        for filename in v:
            if len(filename) > 255:
                raise ValueError("Attachment filename must be 255 characters or less")
            if ".." in filename or "/" in filename or "\\" in filename:
                raise ValueError("Attachment filename cannot contain path traversal sequences")
            if not re.match(r"^[a-zA-Z0-9_.-]+$", filename):
                raise ValueError("Attachment filename must contain only letters, numbers, dots, hyphens, and underscores")
        return v


class BotMessage(BaseModel):
    """A single chat bubble from the bot."""
    text: str
    # "normal" | "safety" | "system" — lets the frontend style it (e.g. red 911).
    kind: Literal["normal", "safety", "system"] = "normal"


class ChatResponse(BaseModel):
    session_id: str
    messages: list[BotMessage]
    quick_replies: list[str] = Field(default_factory=list)
    # Whether this turn expects a file upload affordance to be shown.
    allow_upload: bool = False
    state: str
    done: bool = False
    email_id: Optional[str] = None
    await_step: Optional[str] = None
    account_address: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    queue_position: Optional[int] = None
    queued: bool = False


# ---------------------------------------------------------------------------
# Account lookup (Lookup mode)
# ---------------------------------------------------------------------------
class LookupRequest(BaseModel):
    name: str = ""
    address: str = ""
    email: str = ""

    @field_validator("name", "address", "email")
    @classmethod
    def validate_lookup_fields(cls, v: str) -> str:
        if len(v) > 255:
            raise ValueError("Lookup fields must be 255 characters or less")
        return v


class LookupResult(BaseModel):
    """Result for the Database-tab 'Try a lookup' demo."""
    ok: bool
    kind: Literal["match", "nomatch", "need"]
    title: str
    body: str


class AccountMatch(BaseModel):
    """Minimal, least-privilege result attached to a case on a successful match.

    Deliberately NOT the full customer record — only what staff need to route.
    """
    account_number: str
    service_address: str
    system_size: str
    install_date: str


# ---------------------------------------------------------------------------
# CRM pipeline (adapter -> disposition). See backend/crm.py and backend/pipeline.py.
# ---------------------------------------------------------------------------
class CRMMatch(BaseModel):
    """A confident CRM account match for the requester.

    Carries the routing-relevant subset plus the confidence score the adapter
    used to clear the high-confidence threshold. Never the full CRM record.
    """
    account_number: str
    service_address: str
    system_size: str
    install_date: str
    confidence: float  # 0..1; only returned when >= settings.CRM_MATCH_THRESHOLD

    def to_account_match(self) -> AccountMatch:
        return AccountMatch(
            account_number=self.account_number,
            service_address=self.service_address,
            system_size=self.system_size,
            install_date=self.install_date,
        )


class CaseResult(BaseModel):
    """Result of opening a CRM case for disposition."""
    case_id: str
    status: str = "pending_disposition"
    backend: str = "stub"


class EmailMessage(BaseModel):
    """A message handed to the mailer adapter for delivery."""
    to: str
    subject: str
    html: str
    sender: str = ""
    reply_to: str = ""
    # Upload filenames (as stored under backend/data/uploads) to attach. These
    # are the UUID-safe names produced by upload.validate_and_save_upload; the
    # mailer reads the bytes from disk and MIME-encodes them.
    attachments: list[str] = Field(default_factory=list)


class SendResult(BaseModel):
    """Outcome of a mailer send attempt."""
    delivered: bool
    provider: str          # "google_workspace" | "inbox_store"
    message_id: str = ""
    detail: str = ""


# ---------------------------------------------------------------------------
# Google Chat notification (adapter -> team space). See backend/chat_notifier.py.
# ---------------------------------------------------------------------------
class ChatNotification(BaseModel):
    """A summary posted to a team's Google Chat space alongside the handoff email."""
    space: str                       # routing key: "disposition" | "unverified" | "solar"
    team_label: str = ""             # human-readable team name for the space
    title: str = ""                  # card title
    subtitle: str = ""               # card subtitle
    text: str = ""                   # plain-text fallback (non-card clients / notifications)
    fields: list[tuple[str, str]] = Field(default_factory=list)  # (label, value) rows
    thread_key: str = ""             # optional threading key (e.g. case_id / email_id)
    attachment_count: int = 0


class ChatSendResult(BaseModel):
    """Outcome of a Google Chat notification attempt."""
    delivered: bool
    provider: str          # "stub_feed" | "google_chat_webhook" | "google_chat_api"
    space: str = ""
    detail: str = ""


class DispatchResult(BaseModel):
    """What the disposition pipeline did with a finalized case."""
    email_id: str
    routed_to: str
    matched: bool
    match_confidence: float = 0.0
    case_id: Optional[str] = None
    sent: bool = False
    # Google Chat notification outcome (in addition to the email).
    chat_notified: bool = False
    chat_space: str = ""


# ---------------------------------------------------------------------------
# Email record (rendered handoff stored for the Inbox tab)
# ---------------------------------------------------------------------------
class EmailRecord(BaseModel):
    id: str
    created_at: str
    subject: str
    issue_type: IssueType
    issue_label: str
    urgency: int
    mode: Mode
    mode_label: str
    to: str
    html: str  # fully rendered, autoescaped handoff email body
    # Lightweight fields for the inbox list + gmail header chrome:
    customer_name: str
    matched: bool = False
    tier_label: str = "Standard"
    tier_color: str = "#3a8d5b"
    summary: str = ""  # used as the inbox-row snippet
    # Disposition-pipeline metadata (populated by backend/pipeline.py):
    routed_to: str = ""          # mailbox the handoff was addressed to
    match_confidence: float = 0.0
    case_id: Optional[str] = None  # CRM case id when an account matched
