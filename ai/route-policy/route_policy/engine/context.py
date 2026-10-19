"""The request as every evaluator sees it: one object, parsed once.

``RequestContext`` wraps a /decide (or /evaluate) body -- the OpenAI
``messages`` exactly as LiteLLM received them, the offered ``tools``, the
caller's ids, and the overflow layer's backend snapshot -- and derives the
views evaluators ask for, each computed on first use and cached:

    exchanges       the history as (user text, [messages after it]), one per
                    user message, oldest first (common.conversation.split_exchanges)
    current_text    the LATEST user message's text -- what the semantic
                    router's keyword signals read
    current_rounds  the messages after it: the model rounds and tool results
                    of the exchange in progress
    called_tools    every tool the conversation CALLED, most recent first

Nothing here reads a ruleset or keeps state between requests: two copies of
the service, or a restart, decide the same history the same way.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from typing import Any, Dict, List, Optional, Tuple

from common.conversation import called_tool_names, split_exchanges

# The verdicts the LiteLLM overflow layer reports per backend
# (ai/litellm/hooks/overflow/state.py). Anything else a caller sends is kept
# as sent and treated like "unknown" by evaluators that read it.
KNOWN_VERDICTS = ("ok", "busy", "down", "stale", "unknown")


@dataclass(frozen=True)
class BackendView:
    """One backend from the overflow snapshot, keyed by its api_base."""

    api_base: str
    verdict: str = "unknown"
    waiting: Optional[float] = None
    running: Optional[float] = None


@dataclass
class RequestContext:
    requested_model: str
    messages: List[Any] = field(default_factory=list)
    tools: Optional[List[Any]] = None
    backends: Dict[str, BackendView] = field(default_factory=dict)
    chat_id: Optional[str] = None
    message_id: Optional[str] = None
    user_id: Optional[str] = None
    key_alias: Optional[str] = None
    litellm_call_id: Optional[str] = None

    @cached_property
    def exchanges(self) -> List[Tuple[str, List[dict]]]:
        return split_exchanges(self.messages)

    @cached_property
    def current_text(self) -> str:
        return self.exchanges[-1][0] if self.exchanges else ""

    @cached_property
    def current_rounds(self) -> List[dict]:
        return self.exchanges[-1][1] if self.exchanges else []

    @cached_property
    def called_tools(self) -> List[str]:
        return called_tool_names(self.messages)

    @property
    def message_count(self) -> int:
        return len(self.messages) if isinstance(self.messages, list) else 0

    @property
    def tool_count(self) -> int:
        return len(self.tools) if isinstance(self.tools, list) else 0
