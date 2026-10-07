"""common.net — network-boundary helpers shared by every service that
fetches a caller-supplied URL.

Two things, one rule. :func:`validate_url` is the SSRF guard: a service
running on ``ai_shared`` can reach ``litellm``, every Postgres, and every vLLM
container by name, so "fetch this URL for me" is a request to proxy into the
private network unless something checks first. :func:`fetch_url` is the fetch
that always checks first — and then refuses redirects, bounds the body, and
optionally bounds the wall-clock time. Both live here rather than in any one
service because the classifier, the detector, and anything else that grows a
URL input need the identical rule — a blocklist that drifts between two
services is worse than no blocklist.

Deliberately free of FastAPI: ``common`` has no web-framework dependency, so
the guard raises :class:`BlockedURLError` (and the fetch :class:`FetchError`)
and each service turns that into whatever its own error shape is (the
classifier and the detector both map a refusal to an HTTP 400).

Public API:
    validate_url(url, blocked_networks=None)  → None, or raises BlockedURLError
    fetch_url(url, *, timeout, ...)           → bytes, or raises BlockedURLError
                                                / FetchError (async; needs the
                                                ``net`` extra — httpx)
    BlockedURLError                           — ValueError subclass
    FetchError                                — ValueError subclass, ``.reason``
    DEFAULT_BLOCKED_NETWORKS                  — the RFC1918 + loopback list
"""

from .fetch import FetchError, fetch_url
from .ssrf import (
    DEFAULT_BLOCKED_NETWORKS,
    BlockedURLError,
    validate_url,
)

__all__ = [
    "DEFAULT_BLOCKED_NETWORKS",
    "BlockedURLError",
    "FetchError",
    "fetch_url",
    "validate_url",
]
