"""The /signals request and answer: ``common.signals``' wire format, on HTTP.

The wire format is shared with route-policy's client (ROUTE_POLICY_PHASE5_PLAN
§ 3.1); its dataclasses validate themselves, so this module only adapts them
to HTTP:

* ``parse(body)`` -- raw bytes -> ``SignalsRequest`` through the wire's
  strict ``from_json`` (an unknown key, a param a task does not take, a text
  id no text has, two tasks of one name: a ``WireError`` -> 422). A
  ``WireError`` message names the field and the rule, never a value, so the
  422 can carry it as is. A body that is not JSON is a ``WireError`` too.
* ``SignalsRequest.cap_problem()`` -- the size caps (20,000 characters per
  text, 200,000 per request, 64 texts, 1,000 examples of 300 characters) ->
  413, before any work.
* ``answer(...)`` -- the engine's ``TaskResult``s as a ``SignalsResponse``
  (which validates itself again: a feature outside the vocabulary, a score
  outside [0, 1], a neighbour list longer than k_max cannot be sent) and its
  JSON.

The request::

    {"request_id": "<decision id: logs only>", "deadline_ms": 80,     # null = no deadline
     "texts": [{"id": "u7", "text": "..."}, ...],                     # newest first
     "tasks": [{"task": "pii", "texts": ["u7", "t2"]},
               {"task": "nearest", "texts": ["u7"], "max_tokens": 256, "k": 5,
                "examples": {"fingerprint": "<sha256>", "items": [{"id", "route", "text"}, ...]}},
               {"task": "injection", "texts": ["u7"], "max_tokens": 256, "windows": "head_tail"},
               {"task": "language", "texts": ["u7"]}]}

The answer::

    {"request_id", "latency_ms",
     "models": {"pii": "<repo>@<rev12>", "embed": ..., "injection": ..., "language": ...},
     "results": {"<task>": {"status": "ok" | "partial" | "deadline" | "skipped" | "warming"
                                      | "error" | "unavailable",
                            "ms": <float> | null,
                            "texts": {"<id>": <features>},
                            "error": "<ExceptionType>" | "loading"}}}     # error / unavailable, and a
                                                                          # partial whose missing texts failed

Features per task (never text, spans or offsets):

    pii        {"entities": {<type>: {"count", "max_score"}}, "complete", "cached"}
    nearest    {"neighbours": [{"id", "route", "similarity"}], "routes": {<route>: {"share", "best", "count"}},
                "cached"}
    injection  {"score", "windows", "cached"}
    language   {"language", "confidence", "cached"}
"""

from __future__ import annotations

import json
from typing import Any, Dict, Mapping

from common.signals import SignalsRequest, SignalsResponse, TaskResult, WireError


def parse(body: bytes) -> SignalsRequest:
    """The request in ``body``. Raises ``WireError`` (-> 422) for anything
    that is not a well-formed request; the message never quotes the body."""
    try:
        value = json.loads(body)
    except (ValueError, RecursionError):  # UnicodeDecodeError and JSONDecodeError are ValueErrors
        raise WireError("body: not a JSON document") from None
    return SignalsRequest.from_json(value)


def texts_by_id(request: SignalsRequest) -> Dict[str, str]:
    return {item.id: item.text for item in request.texts}


def answer(request: SignalsRequest, latency_ms: float, models: Mapping[str, str],
           results: Mapping[str, TaskResult]) -> Dict[str, Any]:
    """The JSON answer: the wire's ``SignalsResponse``, built (and so
    validated) from the engine's results."""
    response = SignalsResponse(dict(results), request_id=request.request_id, latency_ms=round(latency_ms, 2),
                               models=dict(models))
    return response.to_json()
