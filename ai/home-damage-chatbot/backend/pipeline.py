"""Disposition pipeline — what happens when an intake is finalized.

This is the orchestration the user described:

    1. Check the CRM for the requester by name + address (fuzzy, high confidence).
    2. If a confident account match is found:
         - open a CRM case for disposition, and
         - route the rendered handoff email to the disposition queue.
    3. If no confident match:
         - route the rendered handoff email to the unverified-intake mailbox
           (the "proper place" for manual review).

Everything authorization-related (the match, the threshold, the routing) is
decided here in backend code — never by the LLM. The CRM and mailer are pluggable
adapters (backend/crm.py, backend/mailer.py); this module just sequences them.
"""
from __future__ import annotations

import logging
import re

from . import chat_notifier, crm, email_render, mailer
from .config import settings
from .schemas import DispatchResult, EmailMessage

logger = logging.getLogger("chatbot.pipeline")


def _email_or_none(value):
    """Only treat a contact as an email second factor if it looks like one."""
    if value and re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", value):
        return value
    return None


def _redact(value: str) -> str:
    """Coarse PII redaction for audit logs (keep first char + length signal)."""
    if not value:
        return "—"
    value = str(value)
    return f"{value[0]}***({len(value)})"


def dispatch(case: dict) -> DispatchResult:
    """Run match -> (open case | route to review) -> render -> send. Returns a
    DispatchResult and persists the EmailRecord for the Inbox tab."""
    # Match on the Solar Account name + address (falling back to the requester's
    # own name/address for back-compat with older/seeded cases).
    account_name = case.get("account_name") or case.get("name")
    account_address = case.get("account_address") or case.get("address")
    contact = case.get("contact") or case.get("email")

    # 1) Fuzzy-but-high-confidence CRM match on account name + address.
    match = crm.find_account(account_name, account_address, _email_or_none(contact))
    matched = match is not None
    confidence = match.confidence if match else 0.0

    # 2) Decide routing + open a case on a match.
    routed_to = mailer.route_for(case, matched)
    case_id = None
    if matched:
        payload = {
            "issue_type": case.get("issue_type"),
            "urgency": case.get("urgency"),
            "summary": email_render._summary(case, match.to_account_match()),
            "requester_name": case.get("name"),
            "account_name": account_name,
            "account_address": account_address,
            "contact": contact,
        }
        case_result = crm.open_case(match, payload)
        case_id = case_result.case_id

    logger.info(
        "DISPOSITION acct_name=%s acct_addr=%s matched=%s conf=%.2f case=%s -> %s",
        _redact(account_name), _redact(account_address), matched, confidence, case_id or "—", routed_to,
    )

    # 3) Render the handoff email (with routing metadata) and persist it.
    account_match = match.to_account_match() if match else None
    record = email_render.render_and_store(
        case,
        account_match,
        to=routed_to,
        case_id=case_id,
        match_confidence=confidence,
    )

    # 4) Hand to the mailer (Inbox store by default; Gmail when armed). Photos the
    #    homeowner uploaded ride along as real attachments on the Gmail path.
    send_result = mailer.send(EmailMessage(
        to=routed_to,
        subject=record.subject,
        html=record.html,
        sender=settings.EMAIL_FROM,
        reply_to=_email_or_none(contact) or "",
        attachments=case.get("attachments", []),
    ))

    # 5) Notify the routed team in Google Chat, in addition to the email
    #    (in-app feed by default; per-team space webhook when armed).
    chat_result = chat_notifier.notify_for_case(
        case,
        routed_to=routed_to,
        matched=matched,
        case_id=case_id,
        email_subject=record.subject,
        email_id=record.id,
        match=account_match,
        match_confidence=confidence,
    )

    return DispatchResult(
        email_id=record.id,
        routed_to=routed_to,
        matched=matched,
        match_confidence=round(confidence, 3),
        case_id=case_id,
        sent=send_result.delivered,
        chat_notified=chat_result.delivered,
        chat_space=chat_result.space,
    )
