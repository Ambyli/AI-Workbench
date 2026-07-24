"""cdp_interceptor — site-agnostic Chrome DevTools Protocol interceptor.

Launches (or connects to) an isolated Chrome (Windows) or Playwright chromium
(Linux/mac), injects a fetch/XHR interceptor into a target page, and streams
captured JSON response bodies to caller-provided callbacks.

Callers control three orthogonal knobs:
- Target URL passed to ``InterceptorClient.launch(target_url)`` — the page
  to load in the browser.
- ``url_patterns`` regex list — isolates the specific network request(s)
  whose response body to extract, among all fetch/XHR calls the page makes.
- ``parse_fn`` — receives each URL-pattern-matched ``Capture`` (url + body)
  and returns an extracted dict, or ``None`` to skip.

The library never configures the root logger and creates no log files at
import time; a NullHandler is installed on ``logging.getLogger("cdp_interceptor")``.
"""

import logging as _logging

_logging.getLogger("cdp_interceptor").addHandler(_logging.NullHandler())

from .client import InterceptorClient, ClientState, Capture
from .launcher import (
    BrowserNotFoundError,
    ChromeNotFoundError,
    find_browser,
    find_chrome,
)
from .sentinel import session_exists, mark_session_ok, clear_session

__all__ = [
    "InterceptorClient",
    "ClientState",
    "Capture",
    "BrowserNotFoundError",
    "ChromeNotFoundError",
    "find_browser",
    "find_chrome",
    "session_exists",
    "mark_session_ok",
    "clear_session",
]
