"""common.keywordmatch — keyword rules compiled with the semantics of the Go
keyword classifier they were written for (RE2). Pure; no I/O.

    UV_LINK_MODE=copy uv run --no-sync pytest shared/common/tests/test_keywordmatch.py -q
"""

from __future__ import annotations

import dataclasses

import pytest

from common.keywordmatch import KeywordRule, compile_keyword_rule


def test_literal_keywords_are_whole_words_ignoring_case_by_default():
    rule = compile_keyword_rule({"keywords": ["function", "c++", "```"]})
    assert rule.operator == "OR" and isinstance(rule, KeywordRule) and dataclasses.is_dataclass(rule)
    assert rule.matches("a FUNCTION here")
    assert not rule.matches("functions")           # \b...\b around a word
    assert not rule.matches("dysfunction")
    assert rule.matches("see```x")                 # no word character -> a plain substring
    # The Go router's \b + QuoteMeta + \b, quirk included: the trailing \b
    # after "+" needs a word character next, so "C++ " is not a hit.
    assert rule.matches("C++x") and not rule.matches("I write C++ daily")


def test_regex_method_uses_each_keyword_as_a_pattern():
    rule = compile_keyword_rule({"method": "REGEX", "case_sensitive": False, "keywords": [r"\bfunctions?\b"]})
    assert rule.matches("Functions") and rule.matches("a function") and not rule.matches("dysfunctional")


def test_case_sensitive_and_ascii_word_boundaries():
    cased = compile_keyword_rule({"method": "regex", "case_sensitive": True, "keywords": ["Go"]})
    assert cased.matches("Go") and not cased.matches("go")
    # RE2's \w is ASCII: an accented letter is a boundary, as in the Go router.
    word = compile_keyword_rule({"keywords": ["caf"]})
    assert word.matches("café")


@pytest.mark.parametrize("operator,text,expected", [
    ("AND", "docker compose up", True), ("AND", "docker run", False),
    ("NOR", "code", True), ("NOR", "the weather", False),
    ("or", "the weather", True),
])
def test_operators(operator, text, expected):
    keywords = ["docker", "compose"] if operator == "AND" else ["weather"]
    assert compile_keyword_rule({"operator": operator, "keywords": keywords}).matches(text) is expected


def test_empty_text_never_matches_even_for_nor():
    rule = compile_keyword_rule({"operator": "NOR", "keywords": ["weather"]})
    assert not rule.matches("") and not rule.matches(None)
    assert not KeywordRule("OR", []).matches("anything")


@pytest.mark.parametrize("rule", [None, {}, {"keywords": []}, {"keywords": "x"}, {"keywords": [""]},
                                  {"keywords": [3]}, {"operator": "XOR", "keywords": ["a"]},
                                  {"method": "regex", "keywords": ["(unclosed"]}])
def test_bad_rules_raise_value_error(rule):
    with pytest.raises(ValueError):
        compile_keyword_rule(rule)

