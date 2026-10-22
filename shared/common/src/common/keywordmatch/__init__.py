"""common.keywordmatch — keyword rules with Go RE2's semantics, in Python.

A KEYWORD RULE (the shape vLLM Semantic Router's Go keyword classifier
reads, kept by the route-policy rulesets that replaced it)::

    name: code_kw
    operator: OR            # OR (default) | AND | NOR
    case_sensitive: false
    method: regex           # optional
    keywords: ['\\bpython\\b', ...]

This package compiles one rule with the semantics that classifier had
(``pkg/classification/keyword_classifier_regex.go`` at v0.3.0), so a rule
written for it still means the same thing:

* ``method: regex`` — each keyword is used AS a pattern; any other method —
  each keyword is a whole literal word (``\\b`` + escaped text + ``\\b``,
  the ``\\b``s only when the keyword has a word character);
* ``case_sensitive: false`` — ignore case (Go prepends ``(?i)``);
* ``\\b`` and ``\\w`` are ASCII, as in RE2 (``re.ASCII``).

Python's ``re`` accepts a superset of RE2 (lookaround, backreferences,
possessive quantifiers), so a pattern that compiles here is not
automatically portable; the consumer that owns the rules checks the RE2
subset (``unit-tests/route-policy/test_default_ruleset.py`` for
``ai/route-policy/rulesets/patterns/``).

Stdlib only and pure — no YAML: the caller loads the rule. Used by
route-policy's ``regex_text`` evaluator (``ai/route-policy``).

Public API:
    KeywordRule(operator, patterns).matches(text) -> bool
    compile_keyword_rule(rule: dict)  -> KeywordRule, or raises ValueError
"""

from .rules import KeywordRule, compile_keyword_rule

__all__ = ["KeywordRule", "compile_keyword_rule"]
