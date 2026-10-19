"""Request models -- the API contract of ROUTE_POLICY_PLAN § 7 ("Phase 2
build spec"), which the LiteLLM client (ai/litellm/hooks/routing/) is built
against. Responses are plain dicts built by the engine
(``engine.pipeline.Decision.response``) so their keys are exactly the
contract's.

Lenient where a strict check would only lose data: the hook calls /decide and
/observe fire-and-forget, so a 422 is invisible to everyone. Unknown fields
are ignored; enum-like strings (verdict, status, finish_reason,
overflow_reason) are bounded in length, not in value -- the documented values
are in ROUTE_POLICY.md. Strict where the database needs it: ``decision_id``
must be a UUID (it is the primary key the two calls meet on).
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..engine.context import BackendView, RequestContext

ERROR_MAX_CHARS = 2000


def _id() -> Any:
    """An optional caller id (chat, message, user, key alias, call, response)."""
    return Field(None, max_length=256)


class BackendIn(BaseModel):
    """One backend of the overflow layer's snapshot (key: its api_base)."""

    model_config = ConfigDict(extra="ignore")

    verdict: str = Field("unknown", max_length=32)   # ok | busy | down | stale | unknown
    waiting: Optional[float] = None
    running: Optional[float] = None


class _Conversation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    messages: List[Any] = Field(default_factory=list)
    tools: Optional[List[Any]] = None
    backends: Optional[Dict[str, BackendIn]] = None

    @field_validator("messages", mode="before")
    @classmethod
    def _messages(cls, value: Any) -> Any:
        return [] if value is None else value

    def backend_views(self) -> Dict[str, BackendView]:
        return {base: BackendView(base, b.verdict, b.waiting, b.running)
                for base, b in (self.backends or {}).items()}


class DecideRequest(_Conversation):
    decision_id: uuid.UUID
    requested_model: str = Field(min_length=1, max_length=256)
    litellm_call_id: Optional[str] = _id()
    chat_id: Optional[str] = _id()
    message_id: Optional[str] = _id()
    user_id: Optional[str] = _id()
    key_alias: Optional[str] = _id()

    def context(self) -> RequestContext:
        return RequestContext(
            requested_model=self.requested_model,
            messages=self.messages,
            tools=self.tools,
            backends=self.backend_views(),
            chat_id=self.chat_id,
            message_id=self.message_id,
            user_id=self.user_id,
            key_alias=self.key_alias,
            litellm_call_id=self.litellm_call_id,
        )


class RulesetRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    version: Optional[int] = Field(None, ge=1)    # None = the highest version


class EvaluateRequest(_Conversation):
    """POST /evaluate: a stored ruleset (``ruleset``) or an unsaved one
    (``document``, refs allowed), run on ``messages`` / ``tools`` /
    ``backends``. Writes nothing."""

    ruleset: Optional[RulesetRef] = None
    document: Optional[Dict[str, Any]] = None
    requested_model: str = Field("auto", min_length=1, max_length=256)

    @model_validator(mode="after")
    def _one_source(self) -> "EvaluateRequest":
        if (self.ruleset is None) == (self.document is None):
            raise ValueError("give exactly one of `ruleset` ({id, version}) or `document`")
        return self

    def context(self) -> RequestContext:
        return RequestContext(requested_model=self.requested_model, messages=self.messages,
                              tools=self.tools, backends=self.backend_views())


class ObserveRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    decision_id: uuid.UUID
    litellm_call_id: Optional[str] = _id()
    response_id: Optional[str] = _id()
    served_model_group: Optional[str] = _id()
    served_deployment: Optional[str] = _id()
    overflow_reason: Optional[str] = Field(None, max_length=32)   # busy | down | null
    finish_reason: Optional[str] = Field(None, max_length=64)     # stop | tool_calls | length | null
    tool_calls: List[str] = Field(default_factory=list, max_length=256)
    status: str = Field("success", min_length=1, max_length=16)   # success | failure
    error: Optional[str] = None

    @field_validator("tool_calls", mode="before")
    @classmethod
    def _tool_calls(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(v)[:256] for v in value if v is not None]
        return value

    @field_validator("error")
    @classmethod
    def _error(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and len(value) > ERROR_MAX_CHARS:
            return value[: ERROR_MAX_CHARS - 1] + "…"
        return value
