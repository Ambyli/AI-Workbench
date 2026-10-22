"""context_size -- the request's size, estimated the way LiteLLM's overflow
hook estimates it.

Pure (no sidecar). Config::

    max_tokens: 100000       # vote when the estimate is above this
    chars_per_token: 2.5     # the overflow hook's divisor (OVERFLOW_TOKENIZE_CHARS_PER_TOKEN)

The estimate is the overflow hook's (``ai/litellm/hooks/overflow/
context_fit.py::estimate_tokens``): the JSON length of ``messages`` plus
``tools``, divided by ``chars_per_token``, plus one -- deliberately high
(English is ~4 characters a token). On /decide the hook has already
replaced every inline base64 payload with a short placeholder, so an
attached image counts for little here, where the overflow hook counts it
in full.

**It rarely changes a route:** the overflow layer already handles context
fit inside a chain, and every chain's largest window is within 7 % of the
others'. The ``default-signals`` draft observes it (role ``observe``), to
log size against route; use it with role ``veto`` on a chain whose window
is smaller, if one ever is.
"""

from __future__ import annotations

import json

from pydantic import Field

from .base import Evaluator, EvaluatorConfig, FieldSpec, Outcome, OutputSpec, declares


class ContextSizeConfig(EvaluatorConfig):
    max_tokens: int = Field(ge=1, le=10_000_000)
    chars_per_token: float = Field(2.5, ge=0.5, le=10.0)


def estimate(messages, tools, chars_per_token: float) -> tuple:
    """(estimated tokens, characters): the overflow hook's estimate."""
    chars = len(json.dumps(messages, default=str))
    if tools:
        chars += len(json.dumps(tools, default=str))
    return int(chars / chars_per_token) + 1, chars


@declares(OutputSpec(
    type="context_size",
    summary="The request's estimated size in tokens: JSON characters of messages + tools / chars_per_token "
            "(the LiteLLM overflow hook's estimate). Pure, no sidecar.",
    votes="vote.route when the estimate is above `max_tokens`; abstains otherwise.",
    fields={
        "estimated_tokens": FieldSpec("JSON characters / chars_per_token, plus one", "integer"),
        "chars": FieldSpec("JSON characters of messages + tools", "integer"),
        "max_tokens": FieldSpec("The configured threshold", "integer"),
    },
))
class ContextSize(Evaluator):
    Config = ContextSizeConfig

    async def evaluate(self, ctx) -> Outcome:
        tokens, chars = estimate(ctx.messages, ctx.tools, self.config.chars_per_token)
        detail = {"estimated_tokens": tokens, "chars": chars, "max_tokens": self.config.max_tokens}
        if tokens > self.config.max_tokens:
            return Outcome.vote(f"~{tokens} tokens > {self.config.max_tokens}", detail)
        return Outcome.abstain(f"~{tokens} tokens <= {self.config.max_tokens}", detail)
