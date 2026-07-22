"""
PARSER — turns one raw Roofix email into a normalized event (Contract B).

What it extracts:
  event_type      from the "RFX | <type>" sender display name (reliable) with a
                  fallback to the subject. e.g. New Comment, New Task,
                  Estimate Complete, Estimate, HIC Executed, Install Date, ...
  project_id      the Bubble-format id from any roofix.io/project/<id> link in the
                  email (subject or body). This is the clean identity key.
  project_url     the full link, when present.
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

import re
import html as _html
from dataclasses import dataclass, field
from typing import Optional


# Event types whose real data lives behind the proposal link — thin by nature.
# For these, parse_complete is False unless we already have what we need.
_NEEDS_SCRAPE_EVENTS = {"Estimate Complete", "Estimate"}

# Roofix sends a good/better/best estimate ladder; these are informational, not
# corrections. (The brain applies the rule; the parser just classifies.)
_SENDER_RE = re.compile(r"^\s*RFX\s*\|\s*(?P<type>[^<]+?)\s*<", re.IGNORECASE)
_PROJECT_URL_RE = re.compile(
    r"https?://(?:www\.)?roofix\.io/project/(?P<id>[0-9]+x[0-9]+)", re.IGNORECASE)
# The email's tokenized tracking link (works without login; redirects to the proposal).
# Appears as href="http://urlNNNN.roofix.io/ls/click?upn=..." in the HTML body.
_TRACKING_URL_RE = re.compile(
    r"https?://url\d+\.roofix\.io/ls/click\?[^\s\"'<>]+", re.IGNORECASE)
# "<Name> - <Address>" — name on the left of the first " - ", address on the right.
_NAME_ADDR_RE = re.compile(r"(?P<name>[A-Za-z.''\- ]+?)\s+-\s+(?P<addr>\d[^\n\"\[\]]+)")
_MENTION_RE = re.compile(r"@([A-Za-z][A-Za-z0-9_]+)")
_QUOTE_RE = re.compile(r"\"(?P<quote>.+?)\"", re.DOTALL)


@dataclass
class ParsedEvent:
    event_type: str
    project_id: Optional[str] = None
    project_url: Optional[str] = None
    tracking_url: Optional[str] = None
    customer_name: Optional[str] = None
    address: Optional[str] = None
    address_suffix: Optional[str] = None      # e.g. "Reorder"
    comment_text: Optional[str] = None
    mentioned_users: list = field(default_factory=list)
    parse_complete: bool = False
    email_timestamp: Optional[str] = None
    raw_subject: Optional[str] = None
    notes: list = field(default_factory=list)  # parser observations / why-incomplete

    def as_dict(self) -> dict:
        return {
            "event_type": self.event_type,
            "project_id": self.project_id,
            "project_url": self.project_url,
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
        }


def _classify(sender: str, subject: str) -> str:
    m = _SENDER_RE.match(sender or "")
    if m:
        return _normalize_type(m.group("type"))
    if subject:
        return _normalize_type(subject.split(" - ")[0])
    return "Unknown"


def _normalize_type(t: str) -> str:
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


def _extract_project(subject: str, body: str) -> tuple[Optional[str], Optional[str]]:
    for text in (subject, body):
        if not text:
            continue
        m = _PROJECT_URL_RE.search(text)
        if m:
            return m.group("id"), m.group(0)
    return None, None


def _extract_name_address(subject: str, body: str, event_type: str = ""):
    """Try subject first, then body. Returns (name, address, suffix)."""
    candidates = []
    if subject:
        s = subject
        if " - " in s:
            head, rest = s.split(" - ", 1)
            if event_type and (event_type.lower() in head.lower()
                               or head.lower() in event_type.lower()
                               or head.lower().startswith(("new project", "new ", "estimate",
                                                            "install", "hic", "deposit", "job",
                                                            "send", "select", "submit", "sign"))):
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
                addr = addr[:sfx.start()].strip()
            return name, addr, suffix
    return None, None, None


def _extract_comment(body: str) -> tuple[Optional[str], list]:
    if not body:
        return None, []
    m = _QUOTE_RE.search(body)
    if not m:
        return None, []
    quote = m.group("quote").strip()
    mentions = _MENTION_RE.findall(quote)
    return quote, mentions


def parse_email(raw: dict) -> ParsedEvent:
    """raw is Contract A: {sender, subject, body_text, timestamp, to, ...}."""
    sender = raw.get("sender", "")
    subject = _html.unescape(raw.get("subject", "") or "")
    body = _html.unescape(raw.get("body_text", "") or "")

    event_type = _classify(sender, subject)
    project_id, project_url = _extract_project(subject, body)
    name, addr, suffix = _extract_name_address(subject, body, event_type)
    comment, mentions = (None, [])
    if event_type in ("New Comment",):
        comment, mentions = _extract_comment(body)

    raw_html = raw.get("body_html") or ""
    tm = _TRACKING_URL_RE.search(raw_html) or _TRACKING_URL_RE.search(body)
    tracking_url = tm.group(0) if tm else None

    ev = ParsedEvent(
        event_type=event_type,
        project_id=project_id,
        project_url=project_url or tracking_url,
        customer_name=name,
        address=addr,
        address_suffix=suffix,
        comment_text=comment,
        mentioned_users=mentions,
        email_timestamp=raw.get("timestamp"),
        raw_subject=subject,
    )
    ev.tracking_url = tracking_url

    have_identity = bool(project_id) or bool(name and addr)
    if not have_identity:
        ev.parse_complete = False
        ev.notes.append("no project_id and no name+address — cannot identify project")
    elif event_type in _NEEDS_SCRAPE_EVENTS:
        ev.parse_complete = False
        ev.notes.append(f"{event_type}: real data is behind the proposal link — needs scrape")
    elif event_type == "New Comment" and not comment:
        ev.parse_complete = False
        ev.notes.append("New Comment but no quoted text found")
    else:
        ev.parse_complete = True

    if name and not project_id:
        ev.notes.append("identified by name+address only (no link in email)")

    return ev
