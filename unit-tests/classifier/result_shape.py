"""The keys every criterion of an assess result carries, whatever its type or
status (API.md § Result shape).

Shared by ``test_classifier_endpoint.py``, which checks the API's real
results against it, and ``test_api_schemas.py``, which checks that
``classifier.schemas.json``'s ``criterion_entry`` declares exactly these
keys — so the schema and the endpoint test cannot drift apart.
"""

ENTRY_KEYS = frozenset({
    "status", "type", "key", "method", "scored", "score", "verdict", "confidence",
    "reason", "detail", "regions", "regions_truncated", "artifacts",
    "localization", "options_used", "error", "complete", "aggregate_used", "items",
})
