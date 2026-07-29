"""
ROOFIX SCRAPER CLIENT — thin HTTP client for the sibling roofix-scraper service.

The scraper drives Chrome/Chromium via common.cdp_interceptor and owns the
Roofix login session as a `--user-data-dir` profile. This client just makes
the service look like a Python function to the bridge.

Reads:
    ROOFIX_SCRAPER_URL   default http://roofix-scraper:8080
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Union

import httpx


class RoofixScraperClient:
    def __init__(self, url: Optional[str] = None, timeout: float = 60.0):
        self.url = (
            url or os.getenv("ROOFIX_SCRAPER_URL", "http://roofix-scraper:8080")
        ).rstrip("/")
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

    def profile_status(self) -> dict:
        """Report whether the scraper has a persisted profile loaded."""
        r = self._client.get(f"{self.url}/profile")
        r.raise_for_status()
        return r.json()

    def get_proposal(self, tracking_url: Optional[str] = None) -> dict:
        """Fetch a proposal by Roofix project id. Optionally pass a tracking_url
        (from the email) if the id-based lookup isn't available."""
        params = {}
        if tracking_url:
            params["tracking_url"] = tracking_url
        r = self._client.get(f"{self.url}/proposal/{roofix_project_id}", params=params)
        r.raise_for_status()
        return r.json()

    def refresh_profile(self, archive: Union[str, Path, bytes]) -> dict:
        """Upload a captured Chrome profile (tar.gz of a `--user-data-dir`)
        to the scraper. Replaces whatever profile was there.

        ``archive`` may be a filesystem path (str/Path) or raw bytes. Login
        must have been completed OUT-OF-BAND on the operator's laptop before
        producing the tar — the container cannot present a login UI itself.
        """
        if isinstance(archive, (str, Path)):
            with open(archive, "rb") as fh:
                files = {"archive": ("profile.tgz", fh, "application/gzip")}
                r = self._client.post(f"{self.url}/profile/refresh", files=files)
        else:
            files = {"archive": ("profile.tgz", archive, "application/gzip")}
            r = self._client.post(f"{self.url}/profile/refresh", files=files)
        r.raise_for_status()
        return r.json()
