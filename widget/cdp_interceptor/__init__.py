"""cdp_interceptor — site-agnostic Chrome DevTools Protocol interceptor.

Launches (or connects to) an isolated Chrome instance, injects a fetch/XHR
interceptor into a target page, and streams captured JSON response bodies
to caller-provided callbacks.

Callers control three orthogonal knobs:
- Target URL passed to ``InterceptorClient.launch(target_url)`` — the page
  to load in Chrome.
- ``url_patterns`` regex list — isolates the specific network request(s)
  whose response body to extract, among all fetch/XHR calls the page makes.
- ``parse_fn`` — receives each URL-pattern-matched ``Capture`` (url + body)
  and returns an extracted dict, or ``None`` to skip.

The library never configures the root logger and creates no log files at
import time; a NullHandler is installed on ``logging.getLogger("cdp_interceptor")``.
"""

import logging as _logging

_logging.getLogger("cdp_interceptor").addHandler(_logging.NullHandler())

from cdp_interceptor.client import InterceptorClient, ClientState, Capture
from cdp_interceptor.launcher import find_chrome, ChromeNotFoundError
from cdp_interceptor.sentinel import session_exists, mark_session_ok, clear_session

__all__ = [
    "InterceptorClient",
    "ClientState",
    "Capture",
    "find_chrome",
    "ChromeNotFoundError",
    "session_exists",
    "mark_session_ok",
    "clear_session",
]
