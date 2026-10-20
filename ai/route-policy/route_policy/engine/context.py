"""The request as every evaluator sees it: one object, parsed once.

``RequestContext`` wraps a /decide (or /evaluate) body -- the OpenAI
``messages`` exactly as LiteLLM received them, the offered ``tools``, the
caller's ids, the overflow layer's backend snapshot, and
``background_task`` (the hook's verdict that this is an Open WebUI
background task -- a title, tags, follow-ups -- not a chat answer) -- and
derives the views evaluators ask for, each computed on first use and cached:

    exchanges       the history as (user text, [messages after it]), one per
                    user message, oldest first (common.conversation.split_exchanges)
    current_text    the LATEST user message's text -- what regex_text
                    searches
    current_rounds  the messages after it: the model rounds and tool results
                    of the exchange in progress
    called_tools    every tool the conversation CALLED, most recent first

Plus what the SERVICE puts on it before the pipeline runs:

    analyses        stored verdicts (``analyses`` rows) by key, read in one
                    query for the keys the ruleset's evaluators named
                    (``Evaluator.analysis_lookups``) -- what an
                    ``llm_classifier`` votes from
    analysis_lookup_error
                    why that read did not happen (``timed out`` | ``failed``
                    | ``unavailable (no database)``); None when it did, or was
                    not needed
    turn_pin        the FIRST stored decision of this turn (same chat id,
                    message id and ruleset id) for a ruleset with
                    ``pin: turn`` -- its id and route; None when this call is
                    the turn's first, the ruleset does not pin, an id is
                    missing, or the read did not happen in time
    memo            scratch space for values an evaluator derives from the
                    request once and reuses across its hooks (the
                    llm_classifier's rendered prefixes)

Nothing here reads a ruleset or keeps state between requests: two copies of
the service, or a restart, decide the same history the same way -- the
verdicts are content-addressed rows any copy reads alike, and so is a turn's
first decision.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any, Dict, List, Mapping, Optional, Tuple

from common.conversation import called_tool_names, split_exchanges

from ..analysis.model import Verdict

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


@dataclass(frozen=True)
class TurnPin:
    """The first decision of a turn, as a ``pin: turn`` ruleset holds the
    turn's later calls to it (``engine.pipeline.run``)."""

    decision_id: uuid.UUID
    route: str


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
    background_task: bool = False
    analyses: Mapping[str, Verdict] = field(default_factory=dict)
    analysis_lookup_error: Optional[str] = None
    turn_pin: Optional[TurnPin] = None
    memo: Dict[Any, Any] = field(default_factory=dict, repr=False, compare=False)

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
