"""tool_calls -- a tool the conversation actually CALLED.

What the conversation DID, not what it was offered: this reads the calls
themselves (``common.conversation.called_tool_names``:
OpenAI ``tool_calls``, legacy ``function_call``, Anthropic ``tool_use``,
named tool results). Offered definitions (the request's ``tools``) never
count: a chat with the sandbox attached but unused is not a coding chat.

``pattern`` is a Python regex matched against the WHOLE tool name
(``re.fullmatch``, no anchors needed). The `default` ruleset's
``code-tools`` matches the sandbox MCP tools (``sandbox-mcp_.+``) and Open
Terminal's distinctive operationIds.

``scope``:
    current_exchange  the rounds after the latest user message (default) --
                      "in this exchange"
    conversation      every call in the history
"""

from __future__ import annotations

import re
from typing import Any, List, Literal, Optional

from pydantic import Field, field_validator

from common.conversation import called_tool_names

from .base import Evaluator, EvaluatorConfig, FieldSpec, Outcome, OutputSpec, declares

# How many called tool names `detail.called` lists (most recent first). The
# names are tool identifiers, never arguments or results.
DETAIL_MAX_NAMES = 20


class ToolCallsConfig(EvaluatorConfig):
    pattern: str = Field(min_length=1, max_length=2000)
    scope: Literal["current_exchange", "conversation"] = "current_exchange"

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, value: str) -> str:
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"pattern does not compile: {exc}") from exc
        return value


def first_full_match(names: Any, pattern: "re.Pattern[str]") -> Optional[str]:
    """The first of ``names`` that ``pattern`` matches in FULL, else None.
    Non-strings are skipped."""
    if not isinstance(names, list):
        return None
    for name in names:
        if isinstance(name, str) and pattern.fullmatch(name):
            return name
    return None


@declares(OutputSpec(
    type="tool_calls",
    summary="A tool the conversation CALLED (not merely offered), matched by a full-name regex.",
    votes="vote.route when a called tool in `scope` matches `pattern` in full; abstains otherwise.",
    relevance="a tool matching `pattern` was called during the exchange ('called <tool>')",
    fields={
        "scope": FieldSpec("current_exchange | conversation", "string"),
        "pattern": FieldSpec("The full-name pattern", "string"),
        "called": FieldSpec(f"Tools called in scope, most recent first (at most {DETAIL_MAX_NAMES})", "array"),
        "matched": FieldSpec("The most recent called tool that matched, or null", "string", nullable=True),
    },
))
class ToolCalls(Evaluator):
    Config = ToolCallsConfig

    def __init__(self, evaluator_id: str, config: ToolCallsConfig) -> None:
        super().__init__(evaluator_id, config)
        self.pattern = re.compile(config.pattern)

    async def evaluate(self, ctx) -> Outcome:
        if self.config.scope == "current_exchange":
            names: List[str] = called_tool_names(ctx.current_rounds)
            where = "in this exchange"
        else:
            names = ctx.called_tools
            where = "in this conversation"
        tool = first_full_match(names, self.pattern)
        detail = {
            "scope": self.config.scope,
            "pattern": self.config.pattern,
            "called": names[:DETAIL_MAX_NAMES],
            "matched": tool,
        }
        if tool is not None:
            return Outcome.vote(f"called {tool} {where}", detail)
        if not names:
            return Outcome.abstain(f"no tool called {where}", detail)
        return Outcome.abstain(f"no called tool matches {where}", detail)

    def exchange_relevance(self, text: str, rounds: List[dict]) -> Optional[str]:
        tool = first_full_match(called_tool_names(rounds), self.pattern)
        return f"called {tool}" if tool is not None else None
