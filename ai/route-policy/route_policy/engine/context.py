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
    user_items      every user message's text as a ``SignalText`` with id
                    ``u<k>`` (1-based, oldest first; common.conversation.
                    user_texts, Open WebUI's RAG block stripped) -- what a
                    signal evaluator sends the sidecar
    tool_items      every tool result as (``SignalText`` with id ``t<k>``,
                    tool name), oldest first (common.conversation.tool_results)
    latest_user_position
                    the message index of the latest user message (None
                    without one): a tool result after it belongs to the
                    exchange in progress
    latest_user_message
                    that message itself (its content parts: text_shape
                    counts attachments)

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
    signals         the sidecar's answer to the ONE ``POST /signals`` the
                    ruleset's evaluators asked for (``Evaluator.
                    signal_requests``, merged by ``engine.pipeline.
                    signal_requests``): task -> ``signals.model.SignalTask``
    signals_error   why that call did not answer (``timed out`` | ``failed``
                    | ``unavailable`` | ``not local``); None when it did, or
                    was not needed
    signals_ms      how long it took (None when there was no call)
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

from common.conversation import called_tool_names, split_exchanges, tool_results, user_texts

from ..analysis.model import Verdict
from ..signals.model import SignalTask, SignalText

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
    signals: Mapping[str, SignalTask] = field(default_factory=dict)
    signals_error: Optional[str] = None
    signals_ms: Optional[float] = None
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

    @cached_property
    def _user_positions(self) -> List[int]:
        if not isinstance(self.messages, list):
            return []
        return [i for i, m in enumerate(self.messages) if isinstance(m, dict) and m.get("role") == "user"]

    @cached_property
    def user_items(self) -> List[SignalText]:
        texts = user_texts(self.messages)
        positions = self._user_positions
        if len(positions) != len(texts):  # never, by user_texts' contract: one per user message
            positions = [-1] * len(texts)
        return [SignalText(f"u{k}", text if isinstance(text, str) else "", (positions[k - 1], k))
                for k, text in enumerate(texts, 1)]

    @cached_property
    def tool_items(self) -> List[Tuple[SignalText, str]]:
        out: List[Tuple[SignalText, str]] = []
        for k, (index, name, text) in enumerate(tool_results(self.messages), 1):
            out.append((SignalText(f"t{k}", text if isinstance(text, str) else "",
                                   (index if isinstance(index, int) else -1, k)),
                        name if isinstance(name, str) else ""))
        return out

    @cached_property
    def latest_user_position(self) -> Optional[int]:
        positions = self._user_positions
        return positions[-1] if positions else None

    @cached_property
    def latest_user_message(self) -> Optional[dict]:
        position = self.latest_user_position
        return self.messages[position] if position is not None else None

    @property
    def message_count(self) -> int:
        return len(self.messages) if isinstance(self.messages, list) else 0

    @property
    def tool_count(self) -> int:
        return len(self.tools) if isinstance(self.tools, list) else 0
