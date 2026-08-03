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

# Constants live in components/constants.py; re-imported at module scope so
# tests that mutate ``brain_mod.PHASE`` still rebind the name locally.
from components.constants import (
    PHASE,
    MILESTONE_EVENTS,
    SCRAPE_EVENTS,
    IGNORE_EVENTS,
    SYSTEM_PROMPT as _SYSTEM_PROMPT,
)


@dataclass
class Decision:
    action: str
    event_type: str
    target: Optional[str] = None
    payload: dict = field(default_factory=dict)
    reasoning: str = ""
    needs_human: bool = False
    source: str = "rule"
    # The Gmail message id of the email this decision came from. Not set by
    # the brain (which doesn't care about mail plumbing) — the orchestrator
    # stamps it after decide() so downstream (app.py) can pair decision→email
    # for mark_read without reverse-lookups on subject / customer name.
    message_id: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "action": self.action,
            "event_type": self.event_type,
            "target": self.target,
            "payload": self.payload,
            "reasoning": self.reasoning,
            "needs_human": self.needs_human,
            "source": self.source,
            "message_id": self.message_id,
        }


# MILESTONE_EVENTS and SCRAPE_EVENTS all live in components/constants.py —
# imported at the top of this module.


async def decide(event: dict, context: dict) -> Decision:
    """
    event   = ParsedEvent.as_dict() from the parser.
    context = what Phoenix knows about this project.

    Async because the fallback path (``_escalate_to_ai`` → ``generate_ai_decision``)
    awaits AsyncOpenAI. The rules path itself does no I/O, so returning from
    inside a rule is effectively synchronous.
    """
    etype = event.get("event_type", "Unknown")
    found = context.get("found", False)
    ambiguous = context.get("ambiguous", False)

    if (
        not event.get("parse_complete")
        and not event.get("roofix_id")
        and not (event.get("customer_name") and event.get("address"))
    ):
        return Decision(
            "escalate",
            event_type=etype,
            reasoning=("Could not identify a project (no id, no usable name+address)."),
            needs_human=True,
        )

    if found and ambiguous:
        return Decision(
            "escalate",
            event_type=etype,
            reasoning=(
                f"Name/address matched {context.get('candidate_count','several')} Phoenix "
                f"projects; refusing to guess which one."
            ),
            needs_human=True,
        )

    if etype == "New Comment":
        if not found:
            return Decision(
                "escalate",
                event_type=etype,
                reasoning=(
                    "Comment for a customer not found in Phoenix. A project may have "
                    "advanced in Roofix without being mirrored here — needs a human to "
                    "decide whether to create it."
                ),
                needs_human=True,
            )
        note = event.get("comment_text") or ""
        if not note:
            return Decision(
                "escalate",
                event_type=etype,
                reasoning="New Comment but no comment text parsed.",
                needs_human=True,
            )
        prefix = "[Roofix] "
        mentions = event.get("mentioned_users") or []
        body = note + (f"\n(Mentions: {', '.join(mentions)})" if mentions else "")
        return Decision(
            "update_chatter",
            event_type=etype,
            target=str(context.get("project_id")),
            payload={"note_text": prefix + body},
            reasoning="New Roofix comment relayed to Phoenix chatter (append).",
        )

    if etype in MILESTONE_EVENTS:
        if not found:
            return Decision(
                "escalate",
                event_type=etype,
                reasoning=(
                    f"'{etype}' milestone for a project not in Phoenix — needs a human."
                ),
                needs_human=True,
            )
        # Update this project's milestone in Phoenix. With its pairing etype and action to perform specific phoenix tasks in orchestrator.py.
        return Decision(
            "update_milestone",
            event_type=etype,
            target=str(context.get("project_id")),
            reasoning=f"'{etype}' advances the project's milestone in Phoenix.",
        )

    if etype in SCRAPE_EVENTS:
        if PHASE == "0":
            return Decision(
                "ignore",
                event_type=etype,
                reasoning=(
                    f"'{etype}' is informational (good/better/best ladder). Phase 0 does "
                    f"not act on estimates; contract value is set by a signing event."
                ),
            )
        # Phase 1: Phoenix already has this project — either from direct
        # identity resolution before the scrape gate, or from re-resolving
        # after the scrape stamped the authoritative Roofix id. Nothing left
        # to do; a create would just log a duplicate. Terminate with a noop
        # so the response makes the "already found" state explicit instead
        # of hiding it behind a phantom create_project decision.
        project_id = context.get("project_id")
        if project_id:
            return Decision(
                "noop_project_exists",
                event_type=etype,
                target=str(project_id),
                reasoning=(
                    f"'{etype}' — Phoenix already has this project "
                    f"(id {project_id}); no create needed."
                ),
            )
        # Phoenix couldn't identify the project. If we have a tracking URL,
        # emit create_project — the orchestrator's scrape gate has already
        # run by this point (or was going to and couldn't); _execute will
        # use whatever _extracted_payload the scrape produced.
        tracking_url = event.get("tracking_url")
        if tracking_url:
            return Decision(
                "create_project",
                event_type=etype,
                target=tracking_url,
                payload={"tracking_url": tracking_url},
                reasoning=f"'{etype}' with tracking URL — scrape and evaluate acceptance.",
            )
        # No tracking URL — can't scrape, escalate.
        return Decision(
            "escalate",
            event_type=etype,
            reasoning=f"'{etype}' with no tracking URL — cannot scrape proposal.",
            needs_human=True,
        )

    if etype in IGNORE_EVENTS:
        return Decision(
            "ignore",
            event_type=etype,
            reasoning=(
                f"'{etype}' is a prompt for a human action in Roofix; Phase 0 "
                "takes no action. (Phase 1 may notify the rep.)"
            ),
        )

    return await _escalate_to_ai(
        event, context, why=f"No rule confidently handles event_type '{etype}'."
    )


async def _escalate_to_ai(event: dict, context: dict, why: str) -> Decision:
    etype = event.get("event_type", "Unknown")
    try:
        d = await generate_ai_decision(event, context, why)
        d.source = "ai"
        return d
    except Exception as e:
        return Decision(
            "escalate",
            event_type=etype,
            source="ai",
            reasoning=f"AI escalation needed ({why}) but model call failed: {e}",
            needs_human=True,
        )


# === THE SWAP SEAM =================================================================

# _SYSTEM_PROMPT lives in components/constants.py — imported at the top of
# this module. The name is aliased with a leading underscore because it's
# only meant to be read by generate_ai_decision below.


async def generate_ai_decision(event: dict, context: dict, why: str) -> Decision:
    """
    Ask the model to make a judgment call and return a Decision.

    Routes through the monorepo's LiteLLM proxy (OpenAI-compatible), so swapping
    the underlying model (Claude -> in-house GPU, etc.) is a LiteLLM config
    change with no code change here.

    Uses AsyncOpenAI so a slow LLM call doesn't tie up the event loop while
    other events in the same tick continue processing.

    Reads:
        ROOFIX_LLM_URL      (default http://litellm:4000)
        ROOFIX_LLM_API_KEY  (LiteLLM master or virtual key)
        BRAIN_MODEL         (LiteLLM model alias, e.g. "qwen3.6")
        BRAIN_MAX_TOKENS    (max tokens per AI decision, default 400)
    """
    from openai import AsyncOpenAI  # local import so rules path has no hard SDK dep

    etype = event.get("event_type", "Unknown")

    client = AsyncOpenAI(
        base_url=os.environ.get("ROOFIX_LLM_URL", "http://litellm:4000").rstrip("/")
        + "/v1",
        api_key=os.environ["ROOFIX_LLM_API_KEY"],
    )
    user = json.dumps(
        {"why_escalated": why, "event": event, "phoenix_context": context}
    )

    resp = await client.chat.completions.create(
        model=os.environ.get("BRAIN_MODEL", "qwen3.6"),
        max_tokens=int(os.environ.get("BRAIN_MAX_TOKENS", "400")),
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
    )
    text = (resp.choices[0].message.content or "").strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()

    # Validate the response is actually JSON before parsing
    if not text:
        raise ValueError("Empty response from model")

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"Model returned non-JSON response: {text[:200]}") from e

    # Validate required fields exist
    if "action" not in data:
        raise ValueError("Model response missing 'action' field")

    return Decision(
        action=data.get("action", "escalate"),
        event_type=etype,
        target=data.get("target"),
        payload=data.get("payload", {}) or {},
        reasoning=data.get("reasoning", ""),
        needs_human=bool(data.get("needs_human", False)),
    )
