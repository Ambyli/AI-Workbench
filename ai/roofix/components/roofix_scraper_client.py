"""
ROOFIX SCRAPER CLIENT — calls the generic interceptor-api service for Roofix
proposal captures.

Before this rewrite there was a sibling ``roofix-scraper`` FastAPI service that
owned Chrome-under-CDP directly. That service was a Roofix-shaped wrapper around
``common.cdp_interceptor`` — every capability it had is now a strict subset of
``ai/interceptor-api``'s ``POST /capture``. Rather than run two Chrome-driving
containers with two profiles to keep fresh, the bridge now talks to
``interceptor-api`` directly and this client owns the Roofix-specific
reshaping (init/data + mget aggregation, doc-type counting, login-wall flag).

Public surface intentionally kept small so ``parser.py`` / ``orchestrator.py``
don't move:

    with RoofixScraperClient() as c:
        r = c.get_proposal(tracking_url="https://roofix.io/…")

``r`` has the exact same keys the old scraper's ``/proposal/{id}`` response had,
so ``proposal_extractor.extract_proposal(r)`` keeps working unchanged.

Reads:
    INTERCEPTOR_API_URL             default http://interceptor-api:8080
    ROOFIX_INIT_DATA_URL_PATTERN    default roofix\\.io/api/1\\.1/init/data
    ROOFIX_MGET_URL_PATTERN         default roofix\\.io/elasticsearch/mget
    ROOFIX_PROFILE_NAME             default "roofix"
    ROOFIX_CAPTURE_WINDOW_SECONDS   default 30
    ROOFIX_LOGIN_TIMEOUT            default 300
    ROOFIX_MAX_MATCHES_PER_PATTERN  default 5
"""

from __future__ import annotations

import os
from typing import Optional

import httpx


# DEFAULT_INTERCEPTOR_URL, DEFAULT_INIT_DATA_PATTERN, DEFAULT_MGET_PATTERN,
# and DEFAULT_PROFILE all live in components/constants.py; re-imported so
# `from components.roofix_scraper_client import DEFAULT_PROFILE` still works.
from components.constants import (
    DEFAULT_INTERCEPTOR_URL,
    DEFAULT_INIT_DATA_PATTERN,
    DEFAULT_MGET_PATTERN,
    DEFAULT_PROFILE,
)


class RoofixScraperClient:
    """Thin HTTP wrapper around ``interceptor-api``'s ``POST /capture`` that
    reshapes the response into the dict shape the bridge's parser + extractor
    have always consumed."""

    def __init__(
        self,
        url: Optional[str] = None,
        timeout: float = 90.0,
        *,
        profile: str = DEFAULT_PROFILE,
        init_data_pattern: str = DEFAULT_INIT_DATA_PATTERN,
        mget_pattern: str = DEFAULT_MGET_PATTERN,
        capture_window_seconds: int = int(
            os.getenv("ROOFIX_CAPTURE_WINDOW_SECONDS", "30")
        ),
        login_timeout: int = int(os.getenv("ROOFIX_LOGIN_TIMEOUT", "300")),
        max_matches_per_pattern: int = int(
            os.getenv("ROOFIX_MAX_MATCHES_PER_PATTERN", "5")
        ),
    ):
        self.url = (
            url or os.getenv("INTERCEPTOR_API_URL", DEFAULT_INTERCEPTOR_URL)
        ).rstrip("/")
        self.profile = profile
        self.init_data_pattern = init_data_pattern
        self.mget_pattern = mget_pattern
        self.capture_window_seconds = capture_window_seconds
        self.login_timeout = login_timeout
        self.max_matches_per_pattern = max_matches_per_pattern
        # Timeout must exceed capture_window_seconds — interceptor-api blocks
        # for capture_window_seconds on each request. Add a safety margin.
        self._client = httpx.AsyncClient(
            timeout=max(timeout, capture_window_seconds + 30.0)
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    async def get_proposal(self, tracking_url: str) -> dict:
        """Load ``tracking_url`` under the Roofix profile in interceptor-api,
        watch for init/data and elasticsearch/mget XHRs, and return the same
        response shape the old ``roofix-scraper`` `/proposal/{id}` endpoint did.

        The old client accepted an optional ``tracking_url`` because Roofix
        offers two entry points — a stable ``/project/{id}`` URL and a
        tokenized email link that redirects without login. The tokenized link
        is what we actually get from Gmail, so ``tracking_url`` is now
        required. If a project-id lookup is ever needed, build the URL
        (``https://roofix.io/project/{id}``) upstream and pass it here.
        """
        body = {
            "url": tracking_url,
            "url_patterns": [self.init_data_pattern, self.mget_pattern],
            "profile": self.profile,
            "capture_window_seconds": self.capture_window_seconds,
            "keep_open": False,
            "login_timeout": self.login_timeout,
            "max_matches_per_pattern": self.max_matches_per_pattern,
            "debug_logging": False,
        }
        r = await self._client.post(f"{self.url}/capture", json=body)
        r.raise_for_status()
        return self._reshape(r.json(), tracking_url)

    def _reshape(self, raw: dict, tracking_url: str) -> dict:
        """Convert an interceptor-api ``/capture`` response into the legacy
        roofix-scraper ``/proposal/{id}`` shape.

        Direct port of the ``on_capture`` closure + post-processing block that
        lived in the old ``ai/roofix/scraper/app.py`` (`init_data` last-wins,
        `mget_docs` flattened across every mget response, `_type` breakdown,
        `login_wall` derived from status + error string).
        """
        matches = raw.get("matches", {}) or {}
        init_bucket = matches.get(self.init_data_pattern, []) or []
        mget_bucket = matches.get(self.mget_pattern, []) or []

        init_bodies = [
            m["body"] for m in init_bucket if isinstance(m, dict) and m.get("body") is not None
        ]

        mget_docs: list[dict] = []
        for m in mget_bucket:
            if not isinstance(m, dict):
                continue
            body = m.get("body")
            if isinstance(body, dict):
                docs = body.get("docs")
                if isinstance(docs, list):
                    mget_docs.extend(docs)

        mget_type_counts: dict[str, int] = {}
        for d in mget_docs:
            if isinstance(d, dict):
                t = d.get("_type")
            else:
                t = None
            mget_type_counts[t or "?"] = mget_type_counts.get(t or "?", 0) + 1

        status = raw.get("status")
        error = raw.get("error")
        login_wall = bool(raw.get("login_wall")) or (
            status == "waiting_login"
            or (isinstance(error, str) and "login" in error.lower())
        )

        return {
            "url": tracking_url,
            "status": status,
            "error": error,
            "login_wall": login_wall,
            # ── init/data — Bubble's page-hydration endpoint ─────────────────
            "init_data": init_bodies[-1] if init_bodies else None,
            "init_data_all": init_bodies,
            "init_data_count": len(init_bodies),
            # ── mget — elasticsearch batch-get, aggregated across all captures ──
            "mget_docs": mget_docs,
            "mget_capture_count": len(mget_bucket),
            "mget_type_counts": mget_type_counts,
            # Every JSON XHR/fetch URL the interceptor saw — diagnostics.
            "captured_urls": raw.get("captured_urls", []) or [],
        }
