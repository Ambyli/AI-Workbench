"""common.net — network-boundary helpers shared by every service that
fetches a caller-supplied URL.

Today that is exactly one thing: the SSRF guard. A service running on
``ai_shared`` can reach ``litellm``, every Postgres, and every vLLM container
by name, so "fetch this URL for me" is a request to proxy into the private
network unless something checks first. :func:`validate_url` is that check, and
it lives here rather than in any one service because the classifier, the
detector, and anything else that grows a URL input need the identical rule —
a blocklist that drifts between two services is worse than no blocklist.

Deliberately free of FastAPI: ``common`` has no web-framework dependency, so
the guard raises :class:`BlockedURLError` and each service turns that into
whatever its own error shape is (the classifier and the detector both map it
to an HTTP 400).

Public API:
    validate_url(url, blocked_networks=None)  → None, or raises BlockedURLError
    BlockedURLError                           — ValueError subclass
    DEFAULT_BLOCKED_NETWORKS                  — the RFC1918 + loopback list
"""

from .ssrf import (
    DEFAULT_BLOCKED_NETWORKS,
    BlockedURLError,
    validate_url,
)

__all__ = [
    "DEFAULT_BLOCKED_NETWORKS",
    "BlockedURLError",
    "validate_url",
]
