"""
BRAIN — the decision layer. Rules-first, with AI escalation.

Flow:
    parsed event + project context (from Phoenix) -> decide()
      1. deterministic RULES handle the clear cases and return a Decision.
      2. genuinely ambiguous cases ESCALATE to the AI (generate_ai_decision),
         which lives behind the swap seam: LiteLLM today, and whatever LiteLLM
         is routed to (Claude / vLLM / in-house GPU) tomorrow.
    Both paths return the SAME Decision shape, so the orchestrator is agnostic.

THE SWAP SEAM: all model access goes through generate_ai_decision(). The LiteLLM
model swap (Claude -> in-house GPU) is now a LiteLLM config change, not a code
change here.

Contract C (Decision):
    action       update_chatter | update_milestone | create_project
                 | notify_rep (Phase 1) | escalate | ignore
    target       what it acts on (project ref / milestone name), when known
    payload      values to write
    reasoning    why — read during the watch period
    needs_human  True -> surface to Jonathan
    source       "rule" | "ai"   (so we can see who decided)
"""

from __future__ import annotations

import os
import json
from dataclasses import dataclass, field
from typing import Optional


PHASE = os.getenv("AGENT_PHASE", "0")


@dataclass
class Decision:
    action: str
    target: Optional[str] = None
    payload: dict = field(default_factory=dict)
    reasoning: str = ""
    needs_human: bool = False
    source: str = "rule"

    def as_dict(self) -> dict:
        return {
            "action": self.action, "target": self.target, "payload": self.payload,
            "reasoning": self.reasoning, "needs_human": self.needs_human,
            "source": self.source,
        }


MILESTONE_EVENTS = {
    "HIC Executed", "Install Date", "Job Scheduled", "Job In Progress",
    "Job Is Complete", "Deposit Invoice Sent", "Deposit Invoice Paid",
    "Job Approval Confirmed",
}

SIGNING_EVENTS = {"Job Approval Confirmed", "HIC Executed"}  # confirm exact set w/ Jonathan

NEEDS_SCRAPE_EVENTS = {"Estimate Complete", "Estimate"}


def decide(event: dict, context: dict) -> Decision:
    """
    event   = ParsedEvent.as_dict() from the parser.
    context = what Phoenix knows about this project.
    """
    etype = event.get("event_type", "Unknown")
    found = context.get("found", False)
    ambiguous = context.get("ambiguous", False)

    if not event.get("parse_complete") and not event.get("project_id") \
            and not (event.get("customer_name") and event.get("address")):
        return Decision("escalate", reasoning=(
            "Could not identify a project (no id, no usable name+address)."),
            needs_human=True)

    if found and ambiguous:
        return Decision("escalate", reasoning=(
            f"Name/address matched {context.get('candidate_count','several')} Phoenix "
            f"projects; refusing to guess which one."), needs_human=True)

    if etype == "New Comment":
        if not found:
            return Decision("escalate", reasoning=(
                "Comment for a customer not found in Phoenix. A project may have "
                "advanced in Roofix without being mirrored here — needs a human to "
                "decide whether to create it."), needs_human=True)
        note = event.get("comment_text") or ""
        if not note:
            return Decision("escalate",
                reasoning="New Comment but no comment text parsed.", needs_human=True)
        prefix = "[Roofix] "
        mentions = event.get("mentioned_users") or []
        body = note + (f"\n(Mentions: {', '.join(mentions)})" if mentions else "")
        return Decision("update_chatter",
            target=str(context.get("phoenix_project_id")),
            payload={"note_text": prefix + body},
            reasoning="New Roofix comment relayed to Phoenix chatter (append).")

    if etype in MILESTONE_EVENTS:
        if not found:
            return Decision("escalate", reasoning=(
                f"'{etype}' milestone for a project not in Phoenix — needs a human."),
                needs_human=True)
        return Decision("update_milestone",
            target=str(context.get("phoenix_project_id")),
            payload={"roofix_event": etype},
            reasoning=f"'{etype}' advances the project's milestone in Phoenix.")

    if etype in NEEDS_SCRAPE_EVENTS:
        if PHASE == "0":
            return Decision("ignore", reasoning=(
                f"'{etype}' is informational (good/better/best ladder). Phase 0 does "
                f"not act on estimates; contract value is set by a signing event."))
        return _escalate_to_ai(event, context,
            why="Estimate event in Phase 1: new project vs. re-quote of existing?")

    if etype == "New Task":
        return Decision("ignore", reasoning=(
            "New Task is a prompt for a human action in Roofix; Phase 0 takes no "
            "action. (Phase 1 may notify the rep.)"))

    return _escalate_to_ai(event, context,
        why=f"No rule confidently handles event_type '{etype}'.")


def _escalate_to_ai(event: dict, context: dict, why: str) -> Decision:
    try:
        d = generate_ai_decision(event, context, why)
        d.source = "ai"
        return d
    except Exception as e:
        return Decision("escalate", source="ai",
            reasoning=f"AI escalation needed ({why}) but model call failed: {e}",
            needs_human=True)


# === THE SWAP SEAM =================================================================

_SYSTEM_PROMPT = (
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


def generate_ai_decision(event: dict, context: dict, why: str) -> Decision:
    """
    Ask the model to make a judgment call and return a Decision.

    Routes through the monorepo's LiteLLM proxy (OpenAI-compatible), so swapping
    the underlying model (Claude -> in-house GPU, etc.) is a LiteLLM config
    change with no code change here.

    Reads:
        LITELLM_URL         (default http://litellm:4000)
        LITELLM_API_KEY     (LiteLLM master or virtual key)
        BRAIN_MODEL         (LiteLLM model alias, e.g. "qwen3.6")
    """
    from openai import OpenAI  # local import so rules path has no hard SDK dep

    client = OpenAI(
        base_url=os.environ.get("LITELLM_URL", "http://litellm:4000").rstrip("/") + "/v1",
        api_key=os.environ["LITELLM_API_KEY"],
    )
    user = json.dumps({"why_escalated": why, "event": event, "phoenix_context": context})

    resp = client.chat.completions.create(
        model=os.environ.get("BRAIN_MODEL", "qwen3.6"),
        max_tokens=400,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
    )
    text = (resp.choices[0].message.content or "").strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    data = json.loads(text)
    return Decision(
        action=data.get("action", "escalate"),
        target=data.get("target"),
        payload=data.get("payload", {}) or {},
        reasoning=data.get("reasoning", ""),
        needs_human=bool(data.get("needs_human", False)),
    )
