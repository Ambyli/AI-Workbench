"""
ROOFIX SCRAPER CLIENT — thin HTTP client for the sibling roofix-scraper service.

The scraper handles Playwright + session cookies; this client just makes the
service look like a Python function to the bridge.

Reads:
    ROOFIX_SCRAPER_URL   default http://roofix-scraper:8080
"""

from __future__ import annotations

import os
from typing import Optional

import httpx


class RoofixScraperClient:
    def __init__(self, url: Optional[str] = None, timeout: float = 60.0):
        self.url = (url or os.getenv("ROOFIX_SCRAPER_URL",
                                     "http://roofix-scraper:8080")).rstrip("/")
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def health(self) -> dict:
        r = self._client.get(f"{self.url}/health")
        r.raise_for_status()
        return r.json()

    def get_proposal(self, roofix_project_id: str,
                     tracking_url: Optional[str] = None) -> dict:
        """Fetch a proposal by Roofix project id. Optionally pass a tracking_url
        (from the email) if the id-based lookup isn't available."""
        params = {}
        if tracking_url:
            params["tracking_url"] = tracking_url
        r = self._client.get(f"{self.url}/proposal/{roofix_project_id}",
                             params=params)
        r.raise_for_status()
        return r.json()

    def refresh_session(self) -> dict:
        """Kick off a session refresh. Returns status; the actual login is
        interactive and happens inside the scraper container."""
        r = self._client.post(f"{self.url}/session/refresh")
        r.raise_for_status()
        return r.json()
