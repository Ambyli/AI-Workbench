"""Invariants of ai/semantic-router/config.yaml that `vllm-sr validate` cannot see.

The Python CLI's KeywordSignal ignores `method`, so `vllm-sr validate` (and
the Envoy render vllm-sr-models-init diffs) never look at a keyword rule's
regexes. The Go router reads the file directly and compiles each `method:
regex` keyword with Go's regexp (RE2), "(?i)" prepended when
case_sensitive is false (pkg/classification/keyword_classifier_regex.go::
regexPatterns, v0.3.0) -- a pattern that does not compile stops the router
at startup. So this file checks, before the box does:

* code_kw is a regex rule, and every pattern avoids the syntax RE2 rejects
  (lookaround, backreferences, possessive / atomic groups, \\Z) and compiles;
* Python's `re` with re.ASCII stands in for RE2 (both read \\b and \\w as
  ASCII; the patterns use nothing where the two engines differ);
* real prompts from the "Scientific Calculator" chat route the way the plan
  intends -- including the ones that used to miss;
* the decision priorities are unchanged (privacy 300 > code 200 > reasoning
  150 > general 10).

    python -m pytest unit-tests/semantic-router -q -p no:cacheprovider
"""

from __future__ import annotations

import pathlib
import re

import pytest

yaml = pytest.importorskip("yaml")

_ROOT = pathlib.Path(__file__).resolve().parents[2]
ROUTER_CONFIG = _ROOT / "ai" / "semantic-router" / "config.yaml"

# From the "Scientific Calculator" chat (2026-10). These turns are why
# code_kw became regexes: whole-word literals missed "functions", "JS" and
# "Make me a ... calculator".
SHOULD_MATCH = [
    "Make me a scientific calculator!",
    "there should be a test for each function",
    "the same functions the calculator uses.",
    "move everything into a different environment so you can run a headless JS test then",
    # A few more shapes code_kw is meant for.
    "Why does app.py raise a KeyError here?",
    "```python\nprint(1)\n```",
    "Can you refactor this class into two modules?",
    "Build a small dashboard app for the sales numbers",
    "I get a stack trace when I run docker compose up",
]
SHOULD_NOT_MATCH = [
    "How can I determine how far the earth is to the nearest star",
    "Looks the same",
    "What is the capital of France?",
    "Summarise this email for me, please.",
]

# Syntax RE2 does not support (https://github.com/google/re2/wiki/Syntax):
# lookahead / lookbehind, backreferences (numbered and named), atomic groups,
# possessive quantifiers, and Python's \Z (RE2 spells it \z).
RE2_UNSUPPORTED = [
    (re.compile(r"\(\?<?[=!]"), "lookaround"),
    (re.compile(r"\\[1-9]"), "numbered backreference"),
    (re.compile(r"\(\?P="), "named backreference"),
    (re.compile(r"\\k<"), "named backreference"),
    (re.compile(r"\(\?>"), "atomic group"),
    (re.compile(r"[*+?}]\+"), "possessive quantifier"),
    (re.compile(r"\\Z"), "\\Z (RE2 has \\z)"),
]


def _cfg() -> dict:
    return yaml.safe_load(ROUTER_CONFIG.read_text(encoding="utf-8"))


def _keyword_rule(name: str) -> dict:
    rules = {r["name"]: r for r in _cfg()["routing"]["signals"]["keywords"]}
    assert name in rules, f"no keyword signal {name!r}"
    return rules[name]


def _go_patterns(rule: dict) -> list:
    """What the Go router compiles for each keyword of a regex rule."""
    prefix = "" if rule.get("case_sensitive") else "(?i)"
    return [prefix + k for k in rule["keywords"]]


def _compiled(rule: dict) -> list:
    return [re.compile(p, re.ASCII) for p in _go_patterns(rule)]


def _first_hit(patterns: list, text: str):
    for p in patterns:
        m = p.search(text)
        if m:
            return m.group(0)
    return None


def test_code_kw_is_a_case_insensitive_regex_rule():
    rule = _keyword_rule("code_kw")
    assert rule["method"] == "regex"
    assert rule["case_sensitive"] is False
    assert rule["operator"] == "OR"
    assert rule["keywords"] and all(isinstance(k, str) and k for k in rule["keywords"])


def test_privacy_and_reasoning_stay_whole_word_literals():
    for name in ("privacy_kw", "reasoning_kw"):
        assert "method" not in _keyword_rule(name), f"{name} was not meant to change"


@pytest.mark.parametrize("pattern", _keyword_rule("code_kw")["keywords"])
def test_code_kw_pattern_is_re2_compatible(pattern):
    # A double-quoted YAML string turns "\b" into a backspace; the patterns
    # must be single-quoted so the backslash reaches the router.
    assert not any(ord(c) < 0x20 for c in pattern), f"control character in {pattern!r} (YAML double quotes?)"
    for detector, what in RE2_UNSUPPORTED:
        assert not detector.search(pattern), f"{what} is not RE2 syntax: {pattern!r}"
    re.compile("(?i)" + pattern, re.ASCII)


@pytest.mark.parametrize("text", SHOULD_MATCH)
def test_code_prompts_match(text):
    hit = _first_hit(_compiled(_keyword_rule("code_kw")), text)
    assert hit is not None, f"code_kw should match {text!r}"


@pytest.mark.parametrize("text", SHOULD_NOT_MATCH)
def test_non_code_prompts_do_not_match(text):
    hit = _first_hit(_compiled(_keyword_rule("code_kw")), text)
    assert hit is None, f"code_kw matched {hit!r} in {text!r}"


def test_decision_priorities_unchanged():
    decisions = {d["name"]: d for d in _cfg()["routing"]["decisions"]}
    assert {n: d["priority"] for n, d in decisions.items()} == {
        "privacy_local": 300, "code": 200, "reasoning": 150, "general": 10}
    assert decisions["code"]["rules"]["conditions"] == [{"type": "keyword", "name": "code_kw"}]
    assert decisions["code"]["modelRefs"] == [{"model": "local-code"}]
    assert decisions["privacy_local"]["modelRefs"] == [{"model": "local-private"}]
    assert all(d["algorithm"] == {"type": "static"} for d in decisions.values())
