"""The ``results.jsonl`` line: its fields and its identity.

``run.py`` writes one line per classification and ``report.py`` reads them;
both take the schema from here, so the report never needs the HTTP client
``run.py`` imports. A line (BENCH.md § results.jsonl)::

    {"point_id", "model", "criteria_set", "criteria_fingerprint", "route",
     "confidence", "scores", "reason", "status", "error", "latency_ms",
     "queue_ms", "model_seconds", "calls", "prompt_tokens",
     "completion_tokens", "input_chars", "at"}

A result's IDENTITY is (point, model, criteria fingerprint): the file is
append-only, so a key can appear more than once (``--retry-failed``, a
criteria set run under two ids) and the LAST line for a key is the one that
counts -- for the resume and for the report alike.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Tuple

FIELDS = ("point_id", "model", "criteria_set", "criteria_fingerprint", "route", "confidence", "scores",
          "reason", "status", "error", "latency_ms", "queue_ms", "model_seconds", "calls", "prompt_tokens",
          "completion_tokens", "input_chars", "at")

ResultKey = Tuple[str, str, str]   # (point_id, model, criteria fingerprint)


def result_key(result: Dict[str, Any]) -> ResultKey:
    return (str(result.get("point_id")), str(result.get("model")), str(result.get("criteria_fingerprint")))


def latest(results: Iterable[Dict[str, Any]]) -> Dict[ResultKey, Dict[str, Any]]:
    """{key: the last line with that key}, in first-seen key order."""
    out: Dict[ResultKey, Dict[str, Any]] = {}
    for result in results:
        out[result_key(result)] = result
    return out
