"""
SCRAPER — pulls the full field set from a Roofix proposal page.

Proposal data arrives in the browser via roofix.io/elasticsearch/mget and
/api/1.1/ endpoints (multiple responses; project doc in one, customer/contact
in another). We drive a headless Chromium against the URL, capture every
matching JSON response, and merge the `docs` arrays into a single blob.

Session cookies (Playwright storage-state) are loaded from disk if present.
Tracking URLs from Roofix notification emails redirect to the proposal page
without login, but session cookies are still preferred where available.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Optional

from playwright.async_api import async_playwright

from session import load as load_session


PROJECT_URL_TEMPLATE = "https://roofix.io/project/{project_id}"


async def fetch_proposal(project_id: Optional[str] = None,
                          tracking_url: Optional[str] = None) -> dict:
    """Fetch a proposal by project_id or a tracking URL.

    Returns:
        {
          "url": <the URL loaded>,
          "docs": [...],              # merged elasticsearch/mget docs
          "doc_types": {"<type>": count, ...},
          "response_count": <number of JSON responses captured>,
        }
    """
    if not project_id and not tracking_url:
        raise ValueError("fetch_proposal needs project_id or tracking_url")

    url = tracking_url or PROJECT_URL_TEMPLATE.format(project_id=project_id)

    storage_state = load_session()
    ckw: dict = {"accept_downloads": True,
                 "viewport": {"width": 1440, "height": 2200}}
    if storage_state:
        ckw["storage_state"] = storage_state

    bodies: list[str] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context = await browser.new_context(**ckw)
            page = await context.new_page()

            async def on_response(resp):
                try:
                    if "elasticsearch/mget" in resp.url or "/api/1.1/" in resp.url:
                        ct = (resp.headers or {}).get("content-type", "")
                        if "json" in ct:
                            bodies.append(await resp.text())
                except Exception:
                    pass

            page.on("response", lambda r: __import__("asyncio").ensure_future(on_response(r)))

            await page.goto(url, wait_until="networkidle", timeout=60000)
            await page.wait_for_timeout(4000)
            for y in range(0, 6000, 800):
                await page.evaluate(f"window.scrollTo(0,{y})")
                await page.wait_for_timeout(300)
            await page.wait_for_timeout(2000)
        finally:
            await browser.close()

    docs: list[dict] = []
    for b in bodies:
        try:
            d = json.loads(b)
            if isinstance(d, dict) and isinstance(d.get("docs"), list):
                docs.extend(d["docs"])
        except Exception:
            pass

    doc_types = Counter(x.get("_type", "?") for x in docs)
    return {
        "url": url,
        "docs": docs,
        "doc_types": dict(doc_types),
        "response_count": len(bodies),
    }
