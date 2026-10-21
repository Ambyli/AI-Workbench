"""Evaluator plugins. Each votes for a route or abstains; see base.py.

    base.py          Evaluator, EvaluatorConfig, Outcome, OutputSpec / FieldSpec,
                     @declares and the type registry
    regex_text.py    a keyword rule (Go-RE2 semantics) on the latest user message
    tool_calls.py    a tool the conversation CALLED, by full-name regex
    cooldown.py      a vote that holds for N exchanges after the last relevant one
    backend_load.py  a vote from the overflow layer's backend snapshot
    static.py        a fixed vote, for tests and overrides
    llm_classifier.py  the verdict a local model gave the conversation, classified in the
                     background through the document classifier (route_policy.analysis)
    pii.py           sensitive data in user messages / data-tool results: common.pii's
                     patterns in-process + the signals sidecar's NER
    nearest_examples.py  the route of the curated example prompts nearest to the latest
                     user message (embedding kNN in the sidecar)
    prompt_injection.py  a jailbreak / injection score from the sidecar
    language.py      the latest user message's language, from the sidecar
    text_shape.py    does the latest user message look like pasted code (pure)
    context_size.py  the request's estimated size in tokens (pure)
    _signals.py      what the signal evaluators share (not a type)
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
