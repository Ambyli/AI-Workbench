"""Dependency-free in-memory rate limiter (fixed window per client key).

A prototype-grade guard against abuse/DoS of the public POST endpoints. Keyed by
client IP; counts requests within a rolling fixed window and rejects once the
budget is exceeded. State is process-local (fine for a single-instance app); a
multi-instance deployment would swap this for Redis behind the same interface.
"""
from __future__ import annotations

import time
from collections import deque

from fastapi import HTTPException, Request

from .config import settings

# key -> deque[timestamps] of accepted requests within the current window
_HITS: dict[str, deque] = {}


def _client_key(request: Request) -> str:
    # Prefer the direct peer; fall back to a forwarded header's first hop if a
    # trusted proxy set it. (In production, validate XFF against known proxies.)
    if request.client and request.client.host:
        return request.client.host
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() or "unknown"


def check(request: Request) -> None:
    """Raise HTTP 429 if the caller has exceeded the configured budget."""
    limit = settings.RATE_LIMIT_REQUESTS
    window = settings.RATE_LIMIT_WINDOW_SECONDS
    if limit <= 0:
        return  # limiter disabled

    now = time.time()
    key = _client_key(request)
    bucket = _HITS.setdefault(key, deque())

    # Drop timestamps outside the window.
    cutoff = now - window
    while bucket and bucket[0] < cutoff:
        bucket.popleft()

    if len(bucket) >= limit:
        retry_after = int(window - (now - bucket[0])) + 1
        raise HTTPException(
            status_code=429,
            detail="Too many requests — please slow down.",
            headers={"Retry-After": str(max(1, retry_after))},
        )

    bucket.append(now)

    # Opportunistic cleanup so idle keys don't accumulate forever.
    if len(_HITS) > 10000:
        for k in [k for k, b in _HITS.items() if not b or b[-1] < cutoff]:
            _HITS.pop(k, None)
