"""The live rule-based ruleset, run offline: the benchmark's baseline.

The promotion rule (ROUTE_POLICY_PLAN § 10) compares a classifier with what
routing does TODAY, so the baseline is not a re-implementation of the rules:
it is ``rulesets/default.yaml`` itself, loaded with route-policy's own loader
(refs inlined), compiled by its own validator and run by its own engine
(``engine.pipeline.run``) on a :class:`RequestContext` built from a decision
point's messages -- no database, no network. A point's ``rules_route`` is
therefore exactly the route ``/decide`` would return for those messages under
that ruleset version.

Two of its evaluators are also the weak labellers (``points.py``):

* the privacy rule (default id ``privacy``, a ``regex_text`` -- its
  ``common.keywordmatch`` rule over ``patterns/privacy.yaml``) on the latest
  user message: a hit is the ``local-private`` weak label, and the
  ``privacy_kw_route`` baseline the classifier's ``local-private`` recall is
  measured against;
* the code-tools rule (default id ``code-tools``, a ``tool_calls`` -- its
  full-name regex) on the tools the ANSWER called: a hit is the
  ``local-code`` weak label.

Both are read from the compiled ruleset by evaluator id, so a changed pattern
file or regex is picked up with the ruleset, never copied here.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence, Tuple

from common.keywordmatch import KeywordRule

from route_policy.engine.context import RequestContext
from route_policy.engine.pipeline import run as run_pipeline
from route_policy.evaluators.tool_calls import first_full_match
from route_policy.rulesets.loader import load_file
from route_policy.rulesets.model import CompiledRuleset
from route_policy.rulesets.validate import compile_ruleset

SERVICE_DIR = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_RULESET = "rulesets/default.yaml"
PRIVATE_ROUTE = "local-private"
CODE_ROUTE = "local-code"
# The requested model every decision point stands for.
REQUESTED_MODEL = "auto"


def resolve_path(path: str) -> pathlib.Path:
    """``path`` as given if it exists, else relative to ``ai/route-policy``
    (so ``rulesets/default.yaml`` works from any working directory)."""
    candidate = pathlib.Path(path)
    if candidate.exists():
        return candidate
    alternative = SERVICE_DIR / path
    return alternative if alternative.exists() else candidate


def load_ruleset(path: str) -> CompiledRuleset:
    """A ruleset file, refs resolved against its own directory, validated and
    compiled exactly as route-policy seeds it."""
    resolved = resolve_path(path)
    if not resolved.is_file():
        raise FileNotFoundError(f"ruleset file {path!r} not found")
    loaded = load_file(resolved, resolved.parent)
    return compile_ruleset(loaded.document, sha256=loaded.sha256)


@dataclass(frozen=True)
class RulesDecision:
    route: str
    decided_by: Optional[str]     # the evaluator that decided; None = the default route


class RuleBaseline:
    def __init__(self, ruleset: CompiledRuleset, *, privacy_evaluator: str = "privacy",
                 code_tools_evaluator: str = "code-tools") -> None:
        entries = {e.id: e for e in ruleset.entries}
        privacy = entries.get(privacy_evaluator)
        code_tools = entries.get(code_tools_evaluator)
        rule = getattr(privacy.evaluator, "rule", None) if privacy else None
        pattern = getattr(code_tools.evaluator, "pattern", None) if code_tools else None
        if not isinstance(rule, KeywordRule):
            raise ValueError(f"{ruleset.id}@{ruleset.version}: no regex_text evaluator {privacy_evaluator!r}")
        if pattern is None or not hasattr(pattern, "fullmatch"):
            raise ValueError(f"{ruleset.id}@{ruleset.version}: no tool_calls evaluator {code_tools_evaluator!r}")
        self.ruleset = ruleset
        self.privacy_rule: KeywordRule = rule
        self.code_tools_pattern = pattern

    @classmethod
    def from_file(cls, path: str = DEFAULT_RULESET, **kw: Any) -> "RuleBaseline":
        return cls(load_ruleset(path), **kw)

    @property
    def ref(self) -> str:
        return f"{self.ruleset.id}@{self.ruleset.version}"

    def privacy_matches(self, text: str) -> bool:
        return self.privacy_rule.matches(text)

    def code_tool(self, called: Sequence[str]) -> Optional[str]:
        """The most recent called tool the code-tools regex matches in full."""
        return first_full_match(list(called), self.code_tools_pattern)

    async def decide(self, messages: List[Any]) -> RulesDecision:
        decision = await run_pipeline(self.ruleset, RequestContext(requested_model=REQUESTED_MODEL,
                                                                   messages=messages))
        return RulesDecision(decision.route, decision.decided_by)


def weak_label(privacy_matched: bool, code_tool: Optional[str],
               decision_route: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """(label, source) by precedence: the privacy rule on the user message ->
    ``local-private``; a code tool called by the answer -> ``local-code``;
    the primary decision's chosen route; else none."""
    if privacy_matched:
        return PRIVATE_ROUTE, "privacy_rule"
    if code_tool is not None:
        return CODE_ROUTE, "code_tool"
    if decision_route:
        return decision_route, "decision"
    return None, None
