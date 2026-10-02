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

Two side channels open their own short-lived CDP connection to the same tab,
so the capture session never notices them: ``capture_screenshot`` /
``InterceptorClient.screenshot`` (an image of the page) and ``run_actions`` /
``InterceptorClient.run_actions`` (fill / click / press / evaluate steps that
make the page fire the requests the capture is waiting for).

The library never configures the root logger and creates no log files at
import time; a NullHandler is installed on ``logging.getLogger("cdp_interceptor")``.
"""

import logging as _logging

_logging.getLogger("cdp_interceptor").addHandler(_logging.NullHandler())

from .actions import (
    ACTION_TYPES,
    PRESS_KEYS,
    Action,
    ActionError,
    ActionResult,
    ActionsReport,
    parse_actions,
    run_actions,
)
from .client import InterceptorClient, ClientState, Capture
from .launcher import (
    BrowserNotFoundError,
    ChromeNotFoundError,
    find_browser,
    find_chrome,
)
from .screenshot import (
    SCREENSHOT_FORMATS,
    Screenshot,
    ScreenshotError,
    capture_screenshot,
)
from .sentinel import session_exists, mark_session_ok, clear_session

__all__ = [
    "InterceptorClient",
    "ClientState",
    "Capture",
    "Screenshot",
    "ScreenshotError",
    "SCREENSHOT_FORMATS",
    "capture_screenshot",
    "ACTION_TYPES",
    "PRESS_KEYS",
    "Action",
    "ActionError",
    "ActionResult",
    "ActionsReport",
    "parse_actions",
    "run_actions",
    "BrowserNotFoundError",
    "ChromeNotFoundError",
    "find_browser",
    "find_chrome",
    "session_exists",
    "mark_session_ok",
    "clear_session",
]
