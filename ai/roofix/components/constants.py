"""
CONSTANTS — single source of truth for module-level constants across the
Roofix bridge components.

Everything here is either a plain literal, a compiled regex, or an env-var
read (``os.getenv`` with the same defaults each individual module used to
have). Consuming modules import from here and re-export the names they used
to define locally, so external callers (tests, other components) that do
``from components.parser import SCRAPE_EVENTS`` — or
``pc.AGENT_USER_ID = None`` to mutate a module attribute — keep working.
"""

from __future__ import annotations

import os
import re

# ── Runtime flags ──────────────────────────────────────────────────────────
# DRY_RUN is read by the orchestrator (skip Phoenix writes) and the phoenix
# client (skip DB writes at the low level). One env var, one binding.
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

# AGENT_PHASE gates which brain rules fire. "0" = chatter + milestones only;
# "1" adds create_project / notify. Kept as a string to match brain's compares.
PHASE = os.getenv("AGENT_PHASE", "0")


# ── CSV audit schema (orchestrator) ────────────────────────────────────────
LOG_COLUMNS = ["stage", "action", "ok", "detail", "event_type", "project_ref"]


# ── Event-type sets (parser / brain / orchestrator share this) ─────────────
# Events whose real data lives behind the proposal link — thin by nature.
# For these the parser sets parse_complete=False and the orchestrator will
# scrape after a Phoenix-lookup miss.
SCRAPE_EVENTS = {"Estimate Complete", "Estimate"}

# Events the brain routes to a milestone update (via Phoenix block/status).
MILESTONE_EVENTS = {
    "Install Date",
    "Job Scheduled",
    "Job In Progress",
    "Job Is Complete",
    "Deposit Invoice Sent",
    "Deposit Invoice Paid",
    "Job Approval Confirmed",
    "HIC Executed",
}

# Events the brain deliberately drops. These are Roofix-side prompts for a
# human action (assign a task, pick a funding option, etc.) that don't
# correspond to a state change we mirror into Phoenix. Phase 0 takes no
# action; a future Phase 1 may notify the rep instead.
IGNORE_EVENTS = {
    "New Task",
    "Select Funding",
    "Estimate Ready for Approval",
    "Submit Credit Application",
    "Approve Estimate",
    "Please have CPC signed (If applicable)",
    "Closeout Completed",
    "Send HIC to Homeowner",
}


# ── Parser: email-field extraction regexes ─────────────────────────────────
# Sender display name pattern: "RFX | <type>" is the most reliable event
# classifier; subject is a fallback.
SENDER_RE = re.compile(r"^\s*RFX\s*\|\s*(?P<type>[^<]+?)\s*<", re.IGNORECASE)

# Canonical roofix.io/project/<id> URL (rarely embedded in emails, but used
# as a tracking-URL fallback and for identifying the id from a URL).
PROJECT_URL_RE = re.compile(
    r"https?://(?:www\.)?roofix\.io/project/(?P<id>[0-9]+x[0-9]+)", re.IGNORECASE
)

# The email's tokenized tracking link (works without login; redirects to the
# proposal). Appears as href="http://urlNNNN.roofix.io/ls/click?upn=..." in
# the HTML body.
TRACKING_URL_RE = re.compile(
    r"https?://url\d+\.roofix\.io/ls/click\?[^\s\"'<>]+", re.IGNORECASE
)

# "<Name> - <Address>" — name on the left of the first " - ", address on the
# right. Names can carry a middle name and double spaces ("LaFonda Mcwilliams
# Wyatt"); addresses can have suffixes ("(Reorder)") which the parser strips.
NAME_ADDR_RE = re.compile(r"(?P<name>[A-Za-z.''\- ]+?)\s+-\s+(?P<addr>\d[^\n\"\[\]]+)")

# @Name mentions inside a quoted comment body.
MENTION_RE = re.compile(r"@([A-Za-z][A-Za-z0-9_]+)")

# Quoted comment text: "..." (multi-line via re.DOTALL).
QUOTE_RE = re.compile(r"\"(?P<quote>.+?)\"", re.DOTALL)


# ── Gmail: HTML → plaintext fallback regexes ───────────────────────────────
# Used when a Roofix email has no text/plain part and we'd otherwise fall
# through to Gmail's snippet (hard-capped ~200 chars, truncating comments).
STYLE_SCRIPT_RE = re.compile(
    r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL
)
BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
BLOCK_CLOSE_RE = re.compile(r"</(p|div|li|tr|h[1-6])>", re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")
INLINE_WS_RE = re.compile(r"[ \t]+")
MULTI_NL_RE = re.compile(r"\n{3,}")


# ── Gmail: OAuth + listener config ─────────────────────────────────────────
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

ROOFIX_SENDER = os.getenv("ROOFIX_SENDER", "no-reply@roofix.io")
GMAIL_CREDENTIALS_PATH = os.getenv("GMAIL_CREDENTIALS_PATH", "config/credentials.json")
GMAIL_TOKEN_PATH = os.getenv("GMAIL_TOKEN_PATH", "config/token.json")
# Listener query: unread, from the Roofix sender. Override via env for narrowing.
LISTENER_QUERY = os.getenv("LISTENER_QUERY") or f"is:unread from:{ROOFIX_SENDER}"

# Comma-separated recipient list for forwarded escalations. Empty disables
# forwarding — escalates then stay unread in Gmail for direct operator review.
# When populated, a successful forward triggers mark_read on the original;
# a failed forward (or empty list) leaves the original unread so a human sees it.
ESCALATION_RECIPIENTS: list[str] = [
    a.strip() for a in os.getenv("ESCALATION_RECIPIENTS", "").split(",") if a.strip()
]


# ── Phoenix client: object/relationship type ids ───────────────────────────
# Values derived from Phoenix data (grep phoenix.object_type /
# relationship_type / phoenix.project GROUP BY company_id, etc.). Overridable
# via env for a non-default Phoenix deployment.
PHOENIX_COMPANY_ID = int(os.getenv("PHOENIX_COMPANY_ID", "1"))
PHOENIX_PROJECT_OBJECT_TYPE_ID = int(
    os.getenv("PHOENIX_PROJECT_OBJECT_TYPE_ID", "7")
)  # "R&R / Roof"
PHOENIX_ENTITY_OBJECT_TYPE_ID = int(
    os.getenv("PHOENIX_ENTITY_OBJECT_TYPE_ID", "8")
)  # "Lead"
PHOENIX_HOMEOWNER_REL_TYPE_ID = int(
    os.getenv("PHOENIX_HOMEOWNER_REL_TYPE_ID", "7")
)  # "Homeowner"
PHOENIX_PROJECT_START_STATUS_ID = int(
    os.getenv("PHOENIX_PROJECT_START_STATUS_ID", "4")
)  # "Qualification"
PHOENIX_AGENT_USER_ID = os.getenv("PHOENIX_AGENT_USER_ID")
PHOENIX_ROOFIX_ID_COLUMN = os.getenv(
    "PHOENIX_ROOFIX_ID_COLUMN", "migration_external_id"
)


# ── Proposal extractor: Bubble document types ──────────────────────────────
LOOKUP_SEP = "__LOOKUP__"
ORDER_TYPE = "custom.order1"
HOMEOWNER_TYPE = "custom.homeowner"
HIC_TYPE = "custom.hic"
JOB_TYPE = "custom.job1"
WARRANTY_TYPE = "custom.warranty"
ESTIMATE_TYPE = "custom.estimate1"


# ── Orchestrator: extracted-proposal payload whitelist ─────────────────────
# Fields the orchestrator forwards from ExtractedProposal into the payload it
# hands ``phoenix.ensure_entity_and_project``. Kept exhaustive so downstream
# gets everything the extractor produced — Phoenix ignores keys it doesn't use.
EXTRACTED_PAYLOAD_FIELDS = (
    "roofix_project_id",
    "is_accepted",
    "display_text",
    "customer_name",
    "full_name",
    "first_name",
    "last_name",
    "email",
    "phone",
    "street_address",
    "city",
    "state_text",
    "state_abbr",
    "zip_code",
    "contract_price",
    "actual_contract_price",
    "funding_type",
    "trade",
    "job_status",
    "hic_status",
    "hic_signature_present",
    "acceptance_signals",
    "error",
)


# ── Scraper client: interceptor-api defaults ───────────────────────────────
DEFAULT_INTERCEPTOR_URL = "http://interceptor-api:8080"
DEFAULT_INIT_DATA_PATTERN = os.getenv(
    "ROOFIX_INIT_DATA_URL_PATTERN", r"roofix\.io/api/1\.1/init/data"
)
DEFAULT_MGET_PATTERN = os.getenv(
    "ROOFIX_MGET_URL_PATTERN", r"roofix\.io/elasticsearch/mget"
)
DEFAULT_PROFILE = os.getenv("ROOFIX_PROFILE_NAME", "roofix")

# Max concurrent scrape requests the bridge will have in flight against
# interceptor-api. MUST match (or be smaller than) interceptor-api's own
# INTERCEPTOR_MAX_CONCURRENT — the bridge queues locally so we never blow
# past the server's cap and get 409'd. asyncio.Semaphore serves acquirers
# FIFO, so first-come-first-serve is automatic.
INTERCEPTOR_MAX_CONCURRENT = int(os.getenv("INTERCEPTOR_MAX_CONCURRENT", "8"))

# Timeout on the full scrape (including queue wait + capture + reshape).
# Extra long by design — the queue wait can be minutes on a burst tick.
# Reached only if interceptor-api itself hangs or the capture window doesn't
# resolve; the scraper's own request timeout is capture_window_seconds + 30.
SCRAPE_TIMEOUT_SECONDS = int(os.getenv("SCRAPE_TIMEOUT_SECONDS", "600"))


# ── Brain: LLM system prompt ───────────────────────────────────────────────
SYSTEM_PROMPT = (
    "You are the decision layer of an internal agent that mirrors Roofix project "
    "events into the Phoenix CRM. You NEVER act inside Roofix and NEVER contact "
    "customers. You return ONE decision as strict JSON, no prose.\n"
    "Allowed actions: update_chatter, update_milestone, create_project, "
    "notify_rep, escalate, ignore.\n"
    "Rules you must honor:\n"
    "- Comments append; never overwrite.\n"
    "- Estimate emails are informational options (good/better/best); contract "
    "value is set by a signing/approval event, not by recency.\n"
    "- Never fabricate a project from a comment; if a project isn't in Phoenix, "
    "escalate with needs_human=true.\n"
    "- When unsure, escalate with needs_human=true. Prefer caution.\n"
    'Return JSON: {"action","target","payload","reasoning","needs_human"}.'
)
