"""Google Chat notification adapter — ping the routed team, in addition to email.

When the disposition pipeline routes a finalized case to a team's mailbox, it
also posts a concise summary to that team's Google Chat space so a human sees it
immediately. Like the mailer and CRM, this is a pluggable adapter so the rest of
the app depends only on the interface.

Backends (settings.CHAT_BACKEND), armed by settings.CHAT_SEND_ENABLED:

    stub (default) -> StubChatNotifier: append to an in-memory feed (mirrored to
                      backend/data/chat_notifications.json) that the demo UI /
                      /api/chat-notifications can display. No network, no creds —
                      keeps the prototype fully usable.
    webhook        -> WebhookChatNotifier: POST a Chat cardsV2 message to the
                      team's incoming-webhook URL (CHAT_WEBHOOK_*). No OAuth.
    api            -> ApiChatNotifier: scaffolded seam (NotImplementedError) for a
                      Chat app + service account (needed only to DM individuals /
                      post to spaces the app manages).

Routing mirrors mailer.route_for: solar -> performance space, matched ->
disposition space, otherwise -> the unverified-intake space. Google Chat incoming
webhooks cannot carry file uploads, so photos ride on the email; the Chat card
reports the attachment count and points at the email instead.

Authorization (which team, whether matched) is decided in backend code, never by
the LLM — same boundary as the rest of the pipeline.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Protocol

import httpx

from .config import settings
from .schemas import AccountMatch, ChatNotification, ChatSendResult

logger = logging.getLogger("chatbot.chat")

# Human-readable labels for each routing space.
_TEAM_LABELS = {
    "disposition": "Service / Disposition team",
    "unverified": "Intake-review team",
    "solar": "Solar Performance team",
}


# ---------------------------------------------------------------------------
# In-memory feed (demo store; mirrored to disk like the email store)
# ---------------------------------------------------------------------------
_DATA_DIR = Path(__file__).parent / "data"
_FEED_FILE = _DATA_DIR / "chat_notifications.json"
_FEED: list[dict] = []


def _persist() -> None:
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        _FEED_FILE.write_text(json.dumps(_FEED, indent=2), encoding="utf-8")
    except OSError as exc:  # pragma: no cover - disk issues shouldn't crash a turn
        logger.warning("Chat feed persist failed: %s", exc)


def _load() -> None:
    if _FEED:
        return
    try:
        if _FEED_FILE.exists():
            _FEED.extend(json.loads(_FEED_FILE.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Chat feed load failed: %s", exc)


def list_notifications() -> list[dict]:
    """Newest-first view of posted notifications (powers the demo UI / API)."""
    _load()
    return list(reversed(_FEED))


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------
class ChatNotifier(Protocol):
    def notify(self, notification: ChatNotification) -> ChatSendResult:
        ...


# ---------------------------------------------------------------------------
# Google Chat cardsV2 payload (shared by webhook + api backends)
# ---------------------------------------------------------------------------
def build_card_payload(n: ChatNotification) -> dict:
    """Render a ChatNotification into a Google Chat message body.

    Includes both a `text` fallback (for notifications / plain clients) and a
    cardsV2 block for a rich, scannable layout.
    """
    widgets = [
        {"decoratedText": {"topLabel": label, "text": value, "wrapText": True}}
        for label, value in n.fields
    ]
    body: dict = {
        "text": n.text,
        "cardsV2": [
            {
                "cardId": "case-handoff",
                "card": {
                    "header": {"title": n.title or "New service case", "subtitle": n.subtitle},
                    "sections": [{"widgets": widgets}] if widgets else [],
                },
            }
        ],
    }
    if n.thread_key:
        # Group posts about the same case into one thread.
        body["thread"] = {"threadKey": n.thread_key}
    return body


# ---------------------------------------------------------------------------
# Dev / default backend — append to the in-memory feed (no network).
# ---------------------------------------------------------------------------
class StubChatNotifier:
    """No-network notifier: records the message to the demo feed and succeeds."""

    def notify(self, n: ChatNotification) -> ChatSendResult:
        _load()
        _FEED.append(
            {
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "space": n.space,
                "team_label": n.team_label or _TEAM_LABELS.get(n.space, n.space),
                "title": n.title,
                "subtitle": n.subtitle,
                "text": n.text,
                "fields": [list(f) for f in n.fields],
                "thread_key": n.thread_key,
                "attachment_count": n.attachment_count,
                "provider": "stub_feed",
            }
        )
        _persist()
        logger.info("Chat(stub-feed) -> %s | %s", n.team_label or n.space, n.title)
        return ChatSendResult(
            delivered=True,
            provider="stub_feed",
            space=n.space,
            detail="CHAT_SEND_ENABLED is false — recorded to in-app feed only.",
        )


# ---------------------------------------------------------------------------
# Google Chat incoming-webhook backend.
# ---------------------------------------------------------------------------
def webhook_url_for(space: str) -> str:
    return {
        "disposition": settings.CHAT_WEBHOOK_DISPOSITION,
        "unverified": settings.CHAT_WEBHOOK_UNVERIFIED,
        "solar": settings.CHAT_WEBHOOK_SOLAR,
    }.get(space, "")


class WebhookChatNotifier:
    """Post a Chat card to the team space's incoming-webhook URL."""

    def notify(self, n: ChatNotification) -> ChatSendResult:
        url = webhook_url_for(n.space)
        if not url:
            logger.warning(
                "Chat webhook for space %r not configured (CHAT_WEBHOOK_*) — "
                "falling back to in-app feed.", n.space,
            )
            return StubChatNotifier().notify(n)
        try:
            r = httpx.post(url, json=build_card_payload(n), timeout=10.0)
            r.raise_for_status()
            logger.info("Chat(webhook) -> %s | %s", n.team_label or n.space, n.title)
            # Mirror to the feed too, so the demo UI reflects real sends.
            StubChatNotifier().notify(n)
            return ChatSendResult(delivered=True, provider="google_chat_webhook", space=n.space)
        except Exception as exc:  # noqa: BLE001 - never let a notify crash the turn
            logger.error("Chat webhook post failed (%s) — falling back to in-app feed.", exc)
            res = StubChatNotifier().notify(n)
            res.delivered = False
            res.provider = "google_chat_webhook"
            res.detail = f"webhook failed: {exc}"
            return res


# ---------------------------------------------------------------------------
# Google Chat app + service-account backend (scaffolded seam).
# ---------------------------------------------------------------------------
class ApiChatNotifier:
    """Placeholder for the Chat REST API path (spaces.messages.create).

    Needs a Google Chat app configured in Google Cloud and a service account
    with the Chat scopes. Required only to post to app-managed spaces or DM
    named individuals — the webhook backend covers per-team space notifications
    without it. Wire when that requirement lands.
    """

    def notify(self, n: ChatNotification) -> ChatSendResult:  # pragma: no cover - seam
        raise NotImplementedError(
            "Google Chat API path is a scaffolded seam. Configure a Chat app + "
            "service account and implement spaces.messages.create here."
        )


# ---------------------------------------------------------------------------
# Backend selection + high-level helper
# ---------------------------------------------------------------------------
def get_notifier() -> ChatNotifier:
    if not settings.CHAT_SEND_ENABLED:
        return StubChatNotifier()
    backend = settings.CHAT_BACKEND
    if backend == "webhook":
        return WebhookChatNotifier()
    if backend == "api":
        return ApiChatNotifier()
    return StubChatNotifier()


def space_for(case: dict, matched: bool) -> str:
    """Routing key for the team space — mirrors mailer.route_for."""
    if case.get("issue_type") == "solar":
        return "solar"
    if matched:
        return "disposition"
    return "unverified"


def build_notification(
    case: dict,
    *,
    routed_to: str,
    matched: bool,
    case_id: Optional[str],
    email_subject: str,
    email_id: str,
    match: Optional[AccountMatch] = None,
    match_confidence: float = 0.0,
) -> ChatNotification:
    """Assemble the team Chat summary for a finalized case (deterministic)."""
    space = space_for(case, matched)
    team_label = _TEAM_LABELS.get(space, space)
    issue = str(case.get("issue_type", "misc")).title()
    urgency = case.get("urgency") or "?"
    requester = case.get("name") or "Unknown requester"
    account_name = case.get("account_name") or case.get("name") or "—"
    attachments = case.get("attachments") or []

    fields: list[tuple[str, str]] = [
        ("Issue", issue),
        ("Urgency", f"{urgency}/10"),
        ("Requester", requester),
        ("Solar account", account_name),
        (
            "CRM match",
            f"matched ({match.account_number}, {round(match_confidence * 100)}%)"
            if matched and match
            else "no confident match — needs review",
        ),
    ]
    if case_id:
        fields.append(("CRM case", case_id))
    fields.append(("Handoff email", f"{email_subject} → {routed_to}"))
    if attachments:
        fields.append(("Photos", f"{len(attachments)} attached to the email"))

    text = (
        f"*New service case* — {issue} (urgency {urgency}/10) from {requester}. "
        f"Routed to {routed_to}."
        + (f" CRM case {case_id}." if case_id else " No confident CRM match — needs review.")
    )
    return ChatNotification(
        space=space,
        team_label=team_label,
        title=f"{issue} case — {requester}",
        subtitle=f"Urgency {urgency}/10 · routed to {routed_to}",
        text=text,
        fields=fields,
        thread_key=case_id or email_id,
        attachment_count=len(attachments),
    )


def notify_for_case(
    case: dict,
    *,
    routed_to: str,
    matched: bool,
    case_id: Optional[str],
    email_subject: str,
    email_id: str,
    match: Optional[AccountMatch] = None,
    match_confidence: float = 0.0,
) -> ChatSendResult:
    """Build + dispatch the team notification for a finalized case."""
    notification = build_notification(
        case,
        routed_to=routed_to,
        matched=matched,
        case_id=case_id,
        email_subject=email_subject,
        email_id=email_id,
        match=match,
        match_confidence=match_confidence,
    )
    return get_notifier().notify(notification)
