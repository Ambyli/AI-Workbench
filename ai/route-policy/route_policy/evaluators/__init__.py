"""Evaluator plugins. Each votes for a route or abstains; see base.py.

    base.py          Evaluator, EvaluatorConfig, Outcome, OutputSpec / FieldSpec,
                     @declares and the type registry
    regex_text.py    a keyword rule (Go-RE2 semantics) on the latest user message
    tool_calls.py    a tool the conversation CALLED, by full-name regex
    cooldown.py      a vote that holds for N exchanges after the last relevant one
    backend_load.py  a vote from the overflow layer's backend snapshot
    static.py        a fixed vote, for tests and overrides

Not built yet: llm_classifier (phase 4); context_size (ROUTE_POLICY_PLAN
§ 4, unscheduled).
"""

from .base import (
    Evaluator,
    EvaluatorConfig,
    FieldSpec,
    Outcome,
    OutputSpec,
    declares,
    evaluator_type,
    registry,
)

__all__ = [
    "Evaluator",
    "EvaluatorConfig",
    "FieldSpec",
    "Outcome",
    "OutputSpec",
    "declares",
    "evaluator_type",
    "registry",
]
