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
import os
import sys
from collections import Counter
from typing import Optional

from playwright.async_api import async_playwright

from session import load as load_session


PROJECT_URL_TEMPLATE = "https://roofix.io/project/{project_id}"

# Local-dev knob: set ROOFIX_HEADLESS=false to launch a visible Chromium so you
# can watch the scrape happen. In Docker keep the default (true).
HEADLESS = os.environ.get("ROOFIX_HEADLESS", "true").lower() != "false"


def _log(msg: str) -> None:
    """Progress log to stderr (bypasses uvicorn's default access-log filtering)."""
    print(f"[scraper] {msg}", file=sys.stderr, flush=True)


_LOGIN_URL_MARKERS = ("/login", "/signin", "sign_in", "signup")


def _looks_like_login(url: str) -> bool:
    u = (url or "").lower()
    return any(m in u for m in _LOGIN_URL_MARKERS)


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
    _log(f"start  headless={HEADLESS}  url={url}")

    storage_state = load_session()
    ckw: dict = {"accept_downloads": True,
                 "viewport": {"width": 1440, "height": 2200}}
    if storage_state:
        ckw["storage_state"] = storage_state
        _log("session cookies loaded")
    else:
        _log("no session cookies — direct /project/ URLs will hit login")

    bodies: list[str] = []
    captured_urls: list[str] = []
    landed_url: Optional[str] = None

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS)
        try:
            context = await browser.new_context(**ckw)
            page = await context.new_page()

            async def on_response(resp):
                try:
                    if "elasticsearch/mget" in resp.url or "/api/1.1/" in resp.url:
                        ct = (resp.headers or {}).get("content-type", "")
                        if "json" in ct:
                            body = await resp.text()
                            bodies.append(body)
                            captured_urls.append(resp.url)
                            _log(f"captured  {resp.status}  {resp.url[:110]}  ({len(body):,}B)")
                except Exception as e:
                    _log(f"on_response error: {e}")

            page.on("response", on_response)

            _log("goto (wait_until=networkidle) ...")
            await page.goto(url, wait_until="networkidle", timeout=60000)
            landed_url = page.url
            _log(f"landed on: {landed_url}")

            # If Roofix bounced us to a login page (no session, or session
            # expired), don't try to scroll — the proposal data won't be
            # coming and scrolling races Bubble's client-side navigation.
            if _looks_like_login(landed_url):
                _log("login wall detected — session needed. Skipping scroll.")
            else:
                await page.wait_for_timeout(4000)
                _log("scrolling to trigger lazy-loaded sections ...")
                for y in range(0, 6000, 800):
                    try:
                        # mouse.wheel survives Bubble's client-side navigations
                        # (page.evaluate throws "execution context destroyed").
                        await page.mouse.wheel(0, 800)
                    except Exception as e:
                        _log(f"scroll interrupted (likely navigation): {e}")
                        break
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

    project_ids = sorted({
        d.get("_id") for d in docs
        if d.get("_type") == "project" and d.get("_id")
    })
    login_wall = _looks_like_login(landed_url or "")
    _log(f"done  login_wall={login_wall}  responses={len(bodies)}  "
         f"docs={len(docs)}  types={dict(doc_types)}  project_ids={project_ids}")

    return {
        "url": url,
        "landed_url": landed_url,
        "login_wall": login_wall,
        "docs": docs,
        "doc_types": dict(doc_types),
        "response_count": len(bodies),
        "captured_urls": captured_urls,
        "project_ids": project_ids,
    }
