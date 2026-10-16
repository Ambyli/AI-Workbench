"""common.keywordmatch — the vLLM Semantic Router's keyword rules, in Python.

The semantic router (``ai/semantic-router``, vllm-sr v0.3.0, Go) matches a
prompt against KEYWORD RULES declared under ``routing.signals.keywords`` in
its ``config.yaml``::

    - name: code_kw
      operator: OR            # OR (default) | AND | NOR
      case_sensitive: false
      method: regex           # optional
      keywords: ['\\bpython\\b', ...]

This package compiles one such rule the way the Go router does
(``pkg/classification/keyword_classifier_regex.go`` at v0.3.0), so a Python
consumer reading the SAME config file reaches the same verdict:

* ``method: regex`` — each keyword is used AS a pattern; any other method —
  each keyword is a whole literal word (``\\b`` + escaped text + ``\\b``,
  the ``\\b``s only when the keyword has a word character);
* ``case_sensitive: false`` — ignore case (Go prepends ``(?i)``);
* ``\\b`` and ``\\w`` are ASCII, as in RE2 (``re.ASCII``).

Python's ``re`` accepts a superset of RE2, so a pattern that compiles here
may still be refused by the Go router at startup (lookaround,
backreferences, possessive quantifiers); the router's own config test
(``unit-tests/semantic-router/test_router_config.py``) is the RE2 check.

Stdlib only and pure — no YAML: :func:`find_keyword_rule` searches an
already-parsed config tree, so the caller picks the loader. The LiteLLM
hooks (``ai/litellm/hooks``) are the first consumer (``code_kw`` for the
code cooldown).

Public API:
    KeywordRule(operator, patterns).matches(text) -> bool
    compile_keyword_rule(rule: dict)  -> KeywordRule, or raises ValueError
    find_keyword_rule(tree, name)     -> the rule dict named `name`, or None
"""

from .rules import KeywordRule, compile_keyword_rule, find_keyword_rule

__all__ = ["KeywordRule", "compile_keyword_rule", "find_keyword_rule"]
