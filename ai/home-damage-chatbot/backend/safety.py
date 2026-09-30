"""Deterministic 911 safety classifier.

This runs on EVERY inbound user message, before the flow logic and independent
of the LLM. Life-safety escalation must be predictable, so it is a hard-coded
keyword/phrase check -- never an LLM judgment.

The bot never contacts 911 itself; it surfaces guidance and pauses intake so the
human can dial.
"""
from __future__ import annotations

import re

# Phrases/words that indicate a possible immediate threat to life or property.
# Kept intentionally broad and conservative (false positives are acceptable for
# a life-safety control; false negatives are not).
_SAFETY_TERMS = [
    "fire", "smoke", "burning", "burnt", "flames", "flame",
    "spark", "sparks", "sparking", "arcing",
    "gas leak", "smell gas", "smell of gas", "natural gas", "rotten egg",
    "shock", "shocked", "electrocut", "live wire", "exposed wire",
    "injured", "injury", "hurt", "bleeding", "unconscious", "collapsed",
    "someone's hurt", "someone is hurt", "trapped", "can't breathe",
    "carbon monoxide", "explosion", "explode",
    "911", "emergency",
]

# Compile word-ish boundaries so "fired" / "sparkle" don't falsely trigger where
# possible, while still catching stems like "electrocut(ed/ion)".
_PATTERN = re.compile(
    r"(?<![a-z])(" + "|".join(re.escape(t) for t in _SAFETY_TERMS) + r")",
    re.IGNORECASE,
)

SAFETY_MESSAGE = (
    "If there is any threat of harm to you or anyone at your home, please "
    "call 911 now. I can't contact emergency services for you — please dial 911 "
    "directly if anyone is in danger.\n\n"
    "When it's safe, let me know and we can continue with your service request."
)


def is_safety_concern(message: str) -> bool:
    """Return True if the message contains potential life-safety language."""
    if not message:
        return False
    return _PATTERN.search(message) is not None
