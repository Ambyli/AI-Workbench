"""Answer-validation gate — "has this question actually been answered?"

The state machine used to treat a slot as answered whenever extraction returned
a non-None value. For free-text fields that's almost always true (the extractor
echoes the text back), so non-answers like "idk", "why do you need that?", or an
off-topic ramble slipped through and the flow advanced with wrong data.

This module adds an explicit gate the state machine consults BEFORE advancing:

    validate_answer(step, raw_message, value, question) -> Verdict(ok, reason)

Two layers, cheapest first:
  1. Deterministic checks (no LLM): typed-field validity, non-answer markers,
     per-field format (name / address / minimum-substance description).
  2. An advisory LLM responsiveness check (llm.is_responsive) for required
     free-text and identity fields, gated on a high-confidence threshold.

The gate only ever decides accept-vs-reprompt. It never changes flow, routing,
or authorization — those stay owned by the deterministic state machine.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from . import llm, lookup
from .config import settings

# Fields where a wrong value is most damaging downstream — these are matched
# against the CRM, so an unvalidated value must be flagged, never silently kept.
IDENTITY_FIELDS = {"account_name", "account_address"}

# Typed fields whose validity is already fully captured by extraction.
_TYPED = {"yesno", "int_1_10", "email", "enum", "pointer"}


@dataclass
class Verdict:
    ok: bool
    reason: str = ""  # user-facing re-prompt text when ok is False


def validate_answer(step, raw_message: str, value: Any, question: str) -> Verdict:
    """Decide whether `step` is satisfactorily answered."""
    # 1) Typed fields: extraction (with its None-gating) is authoritative.
    if step.field_type in _TYPED:
        if value is None:
            return Verdict(False, _typed_reason(step))
        return Verdict(True)

    # 2) Free text.
    text = (value if isinstance(value, str) else (raw_message or "")).strip()
    if not text:
        return Verdict(False, "I didn't catch that — could you type your answer?")

    # Optional non-identity free text: accept anything non-empty. Explicit skips
    # are handled upstream in the state machine; we don't nag on optional fields.
    if not (step.required or step.key in IDENTITY_FIELDS):
        return Verdict(True)

    # --- From here: required and/or identity-critical fields only. ---
    if llm._looks_like_non_answer(text):
        return Verdict(False, _reason_for(step))

    # 3) Per-field format checks (deterministic, no LLM).
    if step.key in {"name", "account_name"} and not _looks_like_name(text):
        return Verdict(False, _reason_for(step))
    if step.key == "account_address" and not _looks_like_address(text):
        return Verdict(
            False,
            "Please include a street number and street name for the Solar Account "
            "address (e.g. 123 Main St, City).",
        )
    if step.key == "contact" and not _looks_like_contact(text):
        return Verdict(False, "Please share a phone number or email so we can reach you.")
    if step.verbatim and len(text.split()) < 2:
        return Verdict(False, "Could you give me a bit more detail so the team can act on it?")

    # 4) Advisory LLM responsiveness gate (high-confidence required to accept).
    responsive, confidence = llm.is_responsive(question, text)
    if not responsive or confidence < settings.ANSWER_CONFIDENCE_THRESHOLD:
        return Verdict(False, _reason_for(step))

    return Verdict(True)


# ---------------------------------------------------------------------------
# Format heuristics
# ---------------------------------------------------------------------------
def _looks_like_name(text: str) -> bool:
    if any(ch.isdigit() for ch in text):
        return False
    if "@" in text or "http" in text.lower():
        return False
    alpha_tokens = [t for t in re.findall(r"[A-Za-z]+", text) if len(t) >= 2]
    return len(alpha_tokens) >= 2


def _looks_like_address(text: str) -> bool:
    # Reuse the lookup module's street-number primitive for consistency with the
    # matcher, then require at least one alphabetic street-name token.
    if not lookup._street_number(text):
        return False
    alpha_tokens = [t for t in re.findall(r"[A-Za-z]+", text) if len(t) >= 2]
    return len(alpha_tokens) >= 1


def _looks_like_contact(text: str) -> bool:
    # A usable contact is either an email or something with enough digits to be a
    # phone number.
    if re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text):
        return True
    return len(re.sub(r"\D", "", text)) >= 7


# ---------------------------------------------------------------------------
# Re-prompt copy
# ---------------------------------------------------------------------------
def _typed_reason(step) -> str:
    if step.field_type == "yesno":
        return "Sorry, was that a yes or a no?"
    if step.field_type == "int_1_10":
        return "Please give me a number from 1 to 10 for urgency."
    if step.field_type == "email":
        return "That didn't look like an email — please enter a valid address, or say 'skip'."
    if step.field_type == "enum":
        opts = ", ".join(step.options or []) or "one of the options"
        return f"Please pick one: {opts}."
    if step.field_type == "pointer":
        return "Please click on the image to drop a pointer indicating the damage location, then click Confirm."
    return "Could you rephrase that for me?"


def _reason_for(step) -> str:
    key = step.key
    if key == "name":
        return "I still need your name to continue — please share it."
    if key == "account_name":
        return "Please share the name exactly as it appears on your Solar Account."
    if key == "account_address":
        return "I need the address on your Solar Account (street number, street, and city) to continue."
    if key == "contact":
        return "Please share a phone number or email so we can reach you."
    if key == "description":
        return "Please describe the problem itself in a sentence or two so the team can help."
    if key == "what_damaged":
        return "Tell me what was damaged (for example: 'the roof over the garage')."
    return "That didn't look like an answer to my question — could you try again?"
