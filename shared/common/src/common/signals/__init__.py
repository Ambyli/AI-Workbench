"""common.signals — the ``signals`` sidecar's wire format and its client.

The sidecar (``ai/signals``) runs the small CPU models whose features
route-policy's evaluators vote on within the awaited ``/decide``: PII NER,
nearest-example topic embeddings, a prompt-injection score, and language
identification (ROUTE_POLICY_PHASE5_PLAN § 3). Everything both sides must
agree on lives here, once:

* :mod:`.wire` — the ``POST /signals`` request and response as frozen,
  self-validating dataclasses (``to_json`` / ``from_json``), the task and
  status vocabularies, the request caps, the per-task feature shapes and the
  example-set fingerprint. Stdlib only: the sidecar builds its answers with
  these classes and parses its requests with them; route-policy builds its
  requests and parses the answers.
* :mod:`.client` — :class:`SignalsClient`, the async caller: a locality
  guard (the sidecar's host must resolve only to this box's networks, or no
  text is sent), a wall-clock budget, and a :class:`SignalsOutcome` instead
  of an exception for every remote failure. httpx is imported on first use.

A request names texts by caller-chosen ids and lists each task once with the
ids it should scan, newest first; a response gives each task a status
(:data:`STATUSES`) and per text id its features — counts, scores, example
ids, a language code. **Never text, never spans**, in either direction's
answer, its errors, or anything the client returns.

Public API:
    SignalsRequest(texts, tasks, request_id=None, deadline_ms=None)
        .to_json() / .from_json(obj) / .cap_problem() -> str | None / .task(name)
    TextItem(id, text)
    TaskRequest(task, texts=(), max_tokens=None, k=None, windows=None, examples=None)
    Example(id, route, text);  ExampleSet(fingerprint, items) / ExampleSet.build(items, model=...)
    fingerprint_examples(items, model) -> sha256 hex
    SignalsResponse(results, request_id=None, latency_ms=None, models={})
        .to_json() / .from_json(obj) / .for_request(request) / .result(task) / .model_for(task)
    TaskResult(status, ms=None, texts={}, error=None)  .usable
    PiiFeatures(entities, complete, cached);  EntityStat(count, max_score)
    NearestFeatures(neighbours, routes, cached);  Neighbour(id, route, similarity);
        RouteShare(share, best, count)
    InjectionFeatures(score, windows, cached);  LanguageFeatures(language, confidence, cached)
    FEATURES        task -> its features class
    TASKS           ("pii", "nearest", "injection", "language")
    STATUSES        ("ok", "partial", "deadline", "skipped", "warming", "error", "unavailable")
    USABLE_STATUSES ("ok", "partial")
    TASK_MODELS     task -> its key under ``models`` (nearest -> "embed")
    MAX_TEXT_CHARS, MAX_REQUEST_CHARS, MAX_TEXTS, MAX_EXAMPLES, MAX_EXAMPLE_CHARS,
    MAX_TOKENS_CAP, MAX_K, INJECTION_WINDOWS
    WireError       ValueError; names the field and rule, never a value
    SignalsClient(base_url, *, models_ttl_s=60, transport=None, resolver=None)
        await .fetch(request, *, budget_s) -> SignalsOutcome
        await .warm(examples, *, budget_s, max_tokens=None, request_id=None) -> SignalsOutcome
        await .aclose()
    SignalsOutcome(status, response, ms, error)  .ok
    OUTCOME_STATUSES ("ok", "timeout", "error", "unavailable", "not_local")
"""

from .client import OUTCOME_STATUSES, SignalsClient, SignalsOutcome
from .wire import (
    FEATURES,
    INJECTION_WINDOWS,
    MAX_EXAMPLE_CHARS,
    MAX_EXAMPLES,
    MAX_K,
    MAX_REQUEST_CHARS,
    MAX_TEXT_CHARS,
    MAX_TEXTS,
    MAX_TOKENS_CAP,
    STATUSES,
    TASK_MODELS,
    TASKS,
    USABLE_STATUSES,
    EntityStat,
    Example,
    ExampleSet,
    Features,
    InjectionFeatures,
    LanguageFeatures,
    NearestFeatures,
    Neighbour,
    PiiFeatures,
    RouteShare,
    SignalsRequest,
    SignalsResponse,
    TaskRequest,
    TaskResult,
    TextItem,
    WireError,
    fingerprint_examples,
)

__all__ = [
    "FEATURES",
    "INJECTION_WINDOWS",
    "MAX_EXAMPLE_CHARS",
    "MAX_EXAMPLES",
    "MAX_K",
    "MAX_REQUEST_CHARS",
    "MAX_TEXT_CHARS",
    "MAX_TEXTS",
    "MAX_TOKENS_CAP",
    "OUTCOME_STATUSES",
    "STATUSES",
    "TASK_MODELS",
    "TASKS",
    "USABLE_STATUSES",
    "EntityStat",
    "Example",
    "ExampleSet",
    "Features",
    "InjectionFeatures",
    "LanguageFeatures",
    "NearestFeatures",
    "Neighbour",
    "PiiFeatures",
    "RouteShare",
    "SignalsClient",
    "SignalsOutcome",
    "SignalsRequest",
    "SignalsResponse",
    "TaskRequest",
    "TaskResult",
    "TextItem",
    "WireError",
    "fingerprint_examples",
]
