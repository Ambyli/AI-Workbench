"""
PARSER — turns one raw Roofix email into a normalized event (Contract B).

What it extracts:
  event_type      from the "RFX | <type>" sender display name (reliable) with a
                  fallback to the subject. e.g. New Comment, New Task,
                  Estimate Complete, Estimate, HIC Executed, Install Date, ...
  project_id      the Bubble-format id from any roofix.io/project/<id> link in the
                  email (subject or body). This is the clean identity key.
  tracking_url    a URL pointing at the proposal, preferentially the tokenized
                  ``urlNNNN.roofix.io/ls/click?…`` link Roofix embeds in the HTML
                  body (redirects without login). Falls back to the canonical
                  ``roofix.io/project/<id>`` link when the email doesn't include a
                  tokenized one. Either form is what the scraper follows.
  customer_name   parsed from the "<Name> - <Address>" pattern.
  address         the address half of that pattern.
  comment_text    the quoted comment, for New Comment events.
  mentioned_users @Name tokens found in the comment.
  parse_complete  False when the email is too thin to act on without scraping
                  (e.g. an estimate/creation event whose data lives behind the link).

Design notes:
  * Classification keys off the SENDER display name prefix ("RFX | X") because the
    inbox shows that is the most consistent signal; subject is the fallback.
  * The "<Name> - <Address>" pattern appears in subjects AND bodies. We try subject
    first, then body. Names can carry a middle name and double spaces ("LaFonda
    Mcwilliams Wyatt"); addresses can have suffixes ("(Reorder)") which we strip
    from the address but keep a note of.
  * The parser NEVER guesses a Phoenix record. Identity resolution is Phoenix's job.
"""

from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass, field
from typing import Optional

# All parser-relevant constants live in components/constants.py so the brain
# and orchestrator share one source of truth. Re-imported at module scope so
# external code that does `from components.parser import NEEDS_SCRAPE_EVENTS`
# still works.
from components.constants import (
    NEEDS_SCRAPE_EVENTS,
    SENDER_RE as _SENDER_RE,
    PROJECT_URL_RE as _PROJECT_URL_RE,
    TRACKING_URL_RE as _TRACKING_URL_RE,
    NAME_ADDR_RE as _NAME_ADDR_RE,
    MENTION_RE as _MENTION_RE,
    QUOTE_RE as _QUOTE_RE,
)


@dataclass
class ParsedEvent:
    event_type: str
    project_id: Optional[str] = None
    tracking_url: Optional[str] = None
    customer_name: Optional[str] = None
    address: Optional[str] = None
    address_suffix: Optional[str] = None  # e.g. "Reorder"
    comment_text: Optional[str] = None
    mentioned_users: list = field(default_factory=list)
    parse_complete: bool = False
    email_timestamp: Optional[str] = None
    raw_subject: Optional[str] = None
    notes: list = field(default_factory=list)  # parser observations / why-incomplete
    message_id: Optional[str] = None  # Gmail message id for mark_read tracking

    def as_dict(self) -> dict:
        return {
            "event_type": self.event_type,
            "project_id": self.project_id,
            "tracking_url": self.tracking_url,
            "customer_name": self.customer_name,
            "address": self.address,
            "address_suffix": self.address_suffix,
            "comment_text": self.comment_text,
            "mentioned_users": self.mentioned_users,
            "parse_complete": self.parse_complete,
            "email_timestamp": self.email_timestamp,
            "raw_subject": self.raw_subject,
            "notes": self.notes,
            "message_id": self.message_id,
        }


def _classify(sender: str, subject: str) -> str:
    """Determine the event type from the email sender display name or subject line.

    Priority: sender display name ("RFX | New Comment") is the most reliable signal.
    Falls back to the subject line's leading segment before the first " - ".

    Called by: `parse_email` (first extraction step).
    """
    m = _SENDER_RE.match(sender or "")
    if m:
        return _normalize_type(m.group("type"))
    if subject:
        return _normalize_type(subject.split(" - ")[0])
    return "Unknown"


def _normalize_type(t: str) -> str:
    """Canonicalize event type strings by resolving abbreviations and synonyms.

    Roofix sends inconsistent type labels across sender names and subjects.
    This maps them to a stable set of canonical types used downstream by the brain.

    Canonical types: "New Comment", "New Task", "Estimate Complete", "Estimate",
    "Deposit Invoice Sent", "Install Date", "HIC Executed", "Unknown".

    Called by: `_classify`.
    """
    t = t.strip()
    aliases = {
        "Estimate Comp.": "Estimate Complete",
        "Estimate Comp": "Estimate Complete",
        "Deposit Invoi.": "Deposit Invoice Sent",
        "Install Date .": "Install Date",
        "Install Date.": "Install Date",
        "New Project Mention": "New Comment",
        "New Project Comment": "New Comment",
        "Estimate in Progress": "Estimate",
        "New Project Task": "New Task",
    }
    return aliases.get(t, t)


def _extract_name_address(subject: str, body: str, event_type: str = ""):
    """Extract customer name and address from the "<Name> - <Address>" pattern.

    Strategy: try subject first, then body. The subject may start with an event-type
    prefix (e.g. "New Comment - ") which would be mistaken for the name — this function
    detects and skips that prefix.

    Strips trailing address suffixes like "(Reorder)" and returns them separately as
    ``address_suffix``. Collapses multiple spaces in names.

    Called by: `parse_email`.
    """
    candidates = []
    if subject:
        s = subject
        if " - " in s:
            head, rest = s.split(" - ", 1)
            if event_type and (
                event_type.lower() in head.lower()
                or head.lower() in event_type.lower()
                or head.lower().startswith(
                    (
                        "new project",
                        "new ",
                        "estimate",
                        "install",
                        "hic",
                        "deposit",
                        "job",
                        "send",
                        "select",
                        "submit",
                        "sign",
                    )
                )
            ):
                s = rest
        candidates.append(s)
    if body:
        candidates.append(body)

    for text in candidates:
        m = _NAME_ADDR_RE.search(text)
        if m:
            name = re.sub(r"\s{2,}", " ", m.group("name")).strip()
            addr = m.group("addr").strip().rstrip(".")
            suffix = None
            sfx = re.search(r"\(([^)]+)\)\s*$", addr)
            if sfx:
                suffix = sfx.group(1).strip()
                addr = addr[: sfx.start()].strip()
            return name, addr, suffix
    return None, None, None


def _extract_comment(body: str) -> tuple[Optional[str], list]:
    """Extract a quoted comment and any ``@Username`` mentions from the email body.

    Looks for text wrapped in double quotes (``"..."``). Scans the quoted text for
    ``@Name`` tokens.

    Called by: `parse_email` (only for ``"New Comment"`` events).
    """
    if not body:
        return None, []
    m = _QUOTE_RE.search(body)
    if not m:
        return None, []
    quote = m.group("quote").strip()
    mentions = _MENTION_RE.findall(quote)
    return quote, mentions


def parse_email(raw: dict) -> ParsedEvent:
    """Parse a raw email (Contract A) into a structured event (Contract B).

    Pure function of the email itself — no network, no scraping, no Phoenix
    lookups. The orchestrator handles scrape gating after its own Phoenix
    resolve step, so the parser only reports what the email carries.

    Pipeline:
      1. **Classify** — determine event_type from sender name or subject.
      2. **Extract name/address** — parse "<Name> - <Address>" pattern.
      3. **Extract comment** — for New Comment events, pull quoted text + @mentions.
      4. **Extract tracking URL** — find the email's tokenized tracking link
         (or fall back to a raw ``/project/<id>`` link) for the scraper to follow.
      5. **Set parse_complete** — False if the email is too thin to act on
         (no identity, needs scraping, or missing comment text).

    Called by: `process_batch` in the orchestrator (maps over the raw email list).
             Also used by tests with sample email dicts.

    Args:
        raw: Raw email dict with keys like ``sender``, ``subject``,
            ``body_text``, ``body_html``, ``timestamp``, ``to``,
            ``message_id`` (optional, for mark_read tracking).

    Returns:
        A ``ParsedEvent`` dataclass with all extracted fields populated.
    """
    sender = raw.get("sender", "")
    subject = _html.unescape(raw.get("subject", "") or "")
    body = _html.unescape(raw.get("body_text", "") or "")

    event_type = _classify(sender, subject)
    name, addr, suffix = _extract_name_address(subject, body, event_type)
    comment, mentions = (None, [])
    if event_type in ("New Comment",):
        comment, mentions = _extract_comment(body)

    # Prefer the tokenized tracking link (works without login). Fall back to the
    # canonical roofix.io/project/<id> URL if the email doesn't include one — that
    # form requires a live session but the scraper has a Roofix profile anyway.
    raw_html = raw.get("body_html") or ""
    tm = (
        _TRACKING_URL_RE.search(raw_html)
        or _TRACKING_URL_RE.search(body)
        or _PROJECT_URL_RE.search(subject)
        or _PROJECT_URL_RE.search(body)
    )
    tracking_url = tm.group(0) if tm else None

    ev = ParsedEvent(
        event_type=event_type,
        project_id=None,
        tracking_url=tracking_url,
        customer_name=name,
        address=addr,
        address_suffix=suffix,
        comment_text=comment,
        mentioned_users=mentions,
        email_timestamp=raw.get("timestamp"),
        raw_subject=subject,
        message_id=raw.get("message_id"),
    )

    have_identity = bool(ev.project_id) or bool(ev.customer_name and ev.address)
    if not have_identity:
        ev.parse_complete = False
        ev.notes.append("no project_id and no name+address — cannot identify project")
    elif event_type in NEEDS_SCRAPE_EVENTS:
        ev.parse_complete = False
        ev.notes.append(
            f"{event_type}: real data is behind the proposal link — needs scrape"
        )
    elif event_type == "New Comment" and not comment:
        ev.parse_complete = False
        ev.notes.append("New Comment but no quoted text found")
    else:
        ev.parse_complete = True

    if ev.customer_name and not ev.project_id:
        ev.notes.append("identified by name+address only (no link in email)")

    return ev
