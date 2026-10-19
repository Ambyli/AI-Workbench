"""backend_load -- a vote from the overflow layer's backend snapshot.

Every /decide carries the LiteLLM overflow hook's view of each local backend,
keyed by api_base (``backends: {"http://qwen3.8-solo:8000/v1": {"verdict":
"busy", "waiting": 2.0, "running": 3.0}}``; verdicts from
ai/litellm/hooks/overflow/state.py: ok | busy | down | stale | unknown). This
evaluator lets a ruleset be LOAD-AWARE without ever picking a deployment --
it only votes for a chain, e.g. "prefer local-reasoning (muse-glimmer first)
while qwen3.8-solo is busy". Choosing the deployment inside the chain stays
the overflow layer's job (ROUTE_POLICY_PLAN § 2).

Config:
    backends     regexes searched in each snapshot key (api_base), e.g.
                 ['qwen3\\.8-solo'] -- every key that matches one is watched
    verdicts     vote when a watched backend's verdict is one of these
                 (default busy, down)
    min_waiting  ... or when its waiting queue is at least this
    require      any (default): one watched backend is enough;
                 all: every watched backend must qualify

Abstains when no snapshot key matches -- an empty snapshot (the hook could
not see) is never evidence of load. The `default` ruleset does not use it:
capacity is the overflow layer's job inside each chain.
"""

from __future__ import annotations

import re
from typing import List, Literal, Optional

from pydantic import Field, field_validator

from ..engine.context import KNOWN_VERDICTS
from .base import Evaluator, EvaluatorConfig, FieldSpec, Outcome, OutputSpec, declares


class BackendLoadConfig(EvaluatorConfig):
    backends: List[str] = Field(min_length=1)
    verdicts: List[str] = Field(default_factory=lambda: ["busy", "down"])
    min_waiting: Optional[float] = Field(None, ge=0)
    require: Literal["any", "all"] = "any"

    @field_validator("backends")
    @classmethod
    def _compile(cls, value: List[str]) -> List[str]:
        for pattern in value:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"backend pattern {pattern!r} does not compile: {exc}") from exc
        return value

    @field_validator("verdicts")
    @classmethod
    def _known(cls, value: List[str]) -> List[str]:
        unknown = sorted(set(value) - set(KNOWN_VERDICTS))
        if unknown:
            raise ValueError(f"unknown verdict(s) {unknown}; expected some of {list(KNOWN_VERDICTS)}")
        return value


@declares(OutputSpec(
    type="backend_load",
    summary="Votes from the overflow layer's backend snapshot (busy / down / queue depth), "
            "so a ruleset can be load-aware without picking deployments.",
    votes="vote.route when any (require: any) or every (require: all) watched backend has one of "
          "`verdicts`, or a waiting queue of at least `min_waiting`; abstains when none qualifies "
          "or no snapshot key matches `backends`.",
    fields={
        "require": FieldSpec("any | all", "string"),
        "watched": FieldSpec("{api_base: verdict} for every snapshot key a `backends` pattern matched",
                             "object"),
        "qualifying": FieldSpec("The watched api_bases that met the condition", "array"),
    },
))
class BackendLoad(Evaluator):
    Config = BackendLoadConfig

    def __init__(self, evaluator_id: str, config: BackendLoadConfig) -> None:
        super().__init__(evaluator_id, config)
        self.patterns = [re.compile(p) for p in config.backends]

    def _qualifies(self, view) -> bool:
        if view.verdict in self.config.verdicts:
            return True
        return (self.config.min_waiting is not None and view.waiting is not None
                and view.waiting >= self.config.min_waiting)

    async def evaluate(self, ctx) -> Outcome:
        watched = {base: view for base, view in sorted(ctx.backends.items())
                   if any(p.search(base) for p in self.patterns)}
        qualifying = [base for base, view in watched.items() if self._qualifies(view)]
        detail = {
            "require": self.config.require,
            "watched": {base: view.verdict for base, view in watched.items()},
            "qualifying": qualifying,
        }
        if not watched:
            return Outcome.abstain("no backend in the snapshot matches", detail)
        enough = bool(qualifying) if self.config.require == "any" else len(qualifying) == len(watched)
        if enough:
            listed = ", ".join(f"{b} is {watched[b].verdict}"
                               + (f" (waiting {watched[b].waiting:g})" if watched[b].waiting is not None else "")
                               for b in qualifying)
            return Outcome.vote(listed, detail)
        return Outcome.abstain("watched backends are not loaded", detail)
