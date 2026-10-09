"""Python equivalent of PostgreSQL pg_trgm `similarity()`.

Used by the stub CRM so local scoring behaves like the phoenix queries, and by
tests to calibrate thresholds without a database. Mirrors pg_trgm's defaults:
lower-case, words are runs of alphanumerics, each word padded with two spaces in
front and one behind, similarity = |shared trigrams| / |union of trigrams|.
"""
from __future__ import annotations

import re
from typing import Optional

_WORD = re.compile(r"[0-9a-z]+")


def trigrams(text: Optional[str]) -> set[str]:
    out: set[str] = set()
    for word in _WORD.findall((text or "").lower()):
        padded = f"  {word} "
        out.update(padded[i:i + 3] for i in range(len(padded) - 2))
    return out


def similarity(a: Optional[str], b: Optional[str]) -> float:
    ta, tb = trigrams(a), trigrams(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)
