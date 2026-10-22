"""Keyword rules compiled with Go RE2's semantics. See the package docstring."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, List, Optional

OPERATORS = ("OR", "AND", "NOR")


@dataclass
class KeywordRule:
    """One compiled keyword rule: `operator` over `patterns`.

    OR: any pattern found; AND: every pattern found; NOR: none found. An
    empty text (or None) never matches, whatever the operator."""

    operator: str
    patterns: List["re.Pattern[str]"]

    def matches(self, text: Optional[str]) -> bool:
        if not text or not self.patterns:
            return False
        hits = [bool(p.search(text)) for p in self.patterns]
        if self.operator == "AND":
            return all(hits)
        if self.operator == "NOR":
            return not any(hits)
        return any(hits)


def compile_keyword_rule(rule: Any) -> KeywordRule:
    """Compile one keyword rule ({name, operator, keywords, case_sensitive,
    method}) with Go RE2's semantics. Raises ValueError on a bad rule: no
    keywords, an unknown operator, an empty / non-string keyword, or a
    pattern that does not compile."""
    if not isinstance(rule, dict) or not isinstance(rule.get("keywords"), list) or not rule["keywords"]:
        raise ValueError("keyword rule has no keywords")
    operator = str(rule.get("operator") or "OR").upper()
    if operator not in OPERATORS:
        raise ValueError(f"unsupported operator {operator!r}")
    flags = re.ASCII | (0 if rule.get("case_sensitive") else re.IGNORECASE)
    raw_regex = str(rule.get("method") or "").lower() == "regex"
    patterns = []
    for keyword in rule["keywords"]:
        if not isinstance(keyword, str) or not keyword:
            raise ValueError(f"bad keyword {keyword!r}")
        if raw_regex:
            source = keyword
        else:
            source = re.escape(keyword)
            if re.search(r"\w", keyword, re.ASCII):
                source = rf"\b{source}\b"
        try:
            patterns.append(re.compile(source, flags))
        except re.error as exc:
            raise ValueError(f"keyword {keyword!r} does not compile: {exc}") from exc
    return KeywordRule(operator, patterns)

