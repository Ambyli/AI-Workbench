"""regex_text -- one keyword rule on the latest user message.

The rule is the semantic router's own shape (ai/semantic-router/config.yaml
``routing.signals.keywords``)::

    name: code_kw
    operator: OR            # OR | AND | NOR
    case_sensitive: false
    method: regex           # absent = each keyword is a whole-word literal
    keywords: ['\\bpython\\b', ...]

compiled by ``common.keywordmatch`` with the Go router's semantics (RE2-style
ASCII ``\\b`` / ``\\w``, ``(?i)`` unless case-sensitive), so the same rule
reaches the same verdict here and in the router. A ruleset usually points at a
file instead of inlining it -- ``config: {patterns_ref: patterns/code.yaml}``
-- and the loader inlines that file as ``rule`` before the content hash, so a
changed pattern file is a changed ruleset version.

It reads the LATEST user message only (``scope: current_message``), exactly
what the router's keyword signals see. As a ``cooldown`` judge it reads each
past exchange's user message the same way.

``detail`` names WHICH patterns matched (by index), never the matched text:
decisions are federated into Trino, and the text is the user's.
"""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from common.keywordmatch import KeywordRule, compile_keyword_rule

from .base import Evaluator, EvaluatorConfig, FieldSpec, Outcome, OutputSpec, declares


class KeywordRuleSpec(BaseModel):
    """A router keyword rule. Only the keys common.keywordmatch implements:
    a ``fuzzy_match`` / ``bm25`` / ``ngram`` option the Go router knows would
    be ignored here and give a different verdict, so it is refused instead."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=64)
    operator: str = "OR"
    case_sensitive: bool = False
    method: Optional[Literal["regex"]] = None
    keywords: List[str] = Field(min_length=1)

    @field_validator("operator")
    @classmethod
    def _operator(cls, value: str) -> str:
        if value.upper() not in ("OR", "AND", "NOR"):
            raise ValueError("operator must be OR, AND or NOR")
        return value

    def as_router_rule(self) -> dict:
        rule = {"name": self.name, "operator": self.operator,
                "case_sensitive": self.case_sensitive, "keywords": list(self.keywords)}
        if self.method is not None:
            rule["method"] = self.method
        return rule


class RegexTextConfig(EvaluatorConfig):
    rule: KeywordRuleSpec
    # Provenance only: the file the loader inlined as `rule` (relative to the
    # rulesets directory). A rule given inline has none.
    patterns_ref: Optional[str] = None
    scope: Literal["current_message"] = "current_message"

    @model_validator(mode="after")
    def _compiles(self) -> "RegexTextConfig":
        compile_keyword_rule(self.rule.as_router_rule())  # ValueError -> a ruleset error
        return self


@declares(OutputSpec(
    type="regex_text",
    summary="A keyword rule (the semantic router's shape, Go-RE2 semantics via common.keywordmatch) "
            "on the latest user message.",
    votes="vote.route when the rule matches the latest user message; abstains otherwise, and when "
          "the conversation has no user message.",
    relevance="the exchange's user message matches the rule ('message matches <rule name>')",
    fields={
        "rule": FieldSpec("The rule's name (e.g. code_kw)", "string"),
        "scope": FieldSpec("Which text was searched: current_message", "string"),
        "method": FieldSpec("regex (keywords are patterns) | literal (whole-word literals)", "string"),
        "operator": FieldSpec("OR | AND | NOR", "string"),
        "matched": FieldSpec("Whether the rule matched (the vote)", "boolean"),
        "matched_patterns": FieldSpec("Indices into the rule's keywords that were found", "array"),
        "searched_chars": FieldSpec("Length of the text searched", "integer"),
    },
))
class RegexText(Evaluator):
    Config = RegexTextConfig

    def __init__(self, evaluator_id: str, config: RegexTextConfig) -> None:
        super().__init__(evaluator_id, config)
        self.rule_spec = config.rule
        self.rule: KeywordRule = compile_keyword_rule(config.rule.as_router_rule())

    def _detail(self, text: str, matched: bool) -> dict:
        return {
            "rule": self.rule_spec.name,
            "scope": self.config.scope,
            "method": "regex" if self.rule_spec.method == "regex" else "literal",
            "operator": self.rule.operator,
            "matched": matched,
            "matched_patterns": [i for i, p in enumerate(self.rule.patterns) if text and p.search(text)],
            "searched_chars": len(text),
        }

    async def evaluate(self, ctx) -> Outcome:
        text = ctx.current_text
        matched = self.rule.matches(text)
        detail = self._detail(text, matched)
        if not ctx.exchanges:
            return Outcome.abstain("no user message", detail)
        if matched:
            return Outcome.vote(f"latest user message matches {self.rule_spec.name}", detail)
        return Outcome.abstain(f"latest user message does not match {self.rule_spec.name}", detail)

    def exchange_relevance(self, text: str, rounds: List[dict]) -> Optional[str]:
        if self.rule.matches(text):
            return f"message matches {self.rule_spec.name}"
        return None
