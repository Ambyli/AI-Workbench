"""
fetcher.py
----------
Thin wrapper around ``cdp_interceptor.InterceptorClient`` that preserves the
BrowserLinker interface the rest of the widget depends on. The claude.ai
response shapes are parsed by ``response_parser.parse_response``; everything
else (Chrome launching, CDP session, headless sentinel handling) lives in
the generic ``cdp_interceptor`` library.

Public API
----------
BrowserLinker.is_available() -> bool
BrowserLinker()
    .launch(on_update)  — open Chrome, start polling; on_update(state_dict)
                          is called from the worker thread on every change
    .fetch_now()        — trigger an immediate re-fetch
    .quit()             — terminate the Chrome process
    .get_state() -> dict
"""

import threading
from datetime import datetime

from common.cdp_interceptor import ChromeNotFoundError, InterceptorClient

from claude_observer import config
from claude_observer.browser.response_parser import parse_response
from claude_observer.logging_setup import log


class BrowserLinker:
    """
    Delegates to InterceptorClient. Exposes the same state dict and callback
    shape the widget has always used so ``core/widget.py`` needs no changes.
    """

    USAGE_URL = "https://claude.ai/settings/usage"
    LOGIN_TIMEOUT = 300
    CAPTURE_TIMEOUT = 30
    CAPTURE_POLL = 2

    def __init__(self):
        log.debug("Starting BrowserLinker.__init__")
        self._data: dict | None = None
        self._error: str | None = None
        self._status = "unlinked"
        self._fetched_at: datetime | None = None
        self._on_update = None
        self._lock = threading.Lock()
        self._client: InterceptorClient | None = None
        log.debug("Finished BrowserLinker.__init__")

    # ── Public ────────────────────────────────────────────────────────────────

    @staticmethod
    def is_available() -> bool:
        return InterceptorClient.is_available()

    def launch(self, on_update):
        """Open Chrome at the usage URL and begin the polling loop.
        on_update(state_dict) is called from the worker thread on every change."""
        log.debug("Starting BrowserLinker.launch")
        self._on_update = on_update

        self._client = InterceptorClient(
            profile_dir=config.BROWSER_PROFILE_DIR,
            debug_port=config.BROWSER_DEBUG_PORT,
            debug_logging=config.DEBUG_LOGGING,
            # url_patterns intentionally omitted — parse_response is strict
            # about body shape, so today's widget lets every capture through
            # the parser and takes the first match. Preserve that.
            parse_fn=lambda cap: parse_response(cap.body),
            on_data=self._on_data,
            on_status=self._on_cdp_status,
            login_timeout=self.LOGIN_TIMEOUT,
            capture_timeout=self.CAPTURE_TIMEOUT,
            capture_poll=self.CAPTURE_POLL,
        )
        try:
            self._client.launch(self.USAGE_URL)
        except ChromeNotFoundError:
            log.error("BrowserLinker.launch: Chrome not found")
            with self._lock:
                self._status = "error"
                self._error = (
                    "Chrome not found — install Google Chrome to use account stats"
                )
            self._notify()
        log.debug("Finished BrowserLinker.launch")

    def fetch_now(self):
        log.debug("BrowserLinker.fetch_now")
        if self._client is not None:
            self._client.fetch_now()

    def go_headless(self):
        log.debug("BrowserLinker.go_headless")
        if self._client is not None:
            self._client.go_headless()

    def go_visible(self):
        log.debug("BrowserLinker.go_visible")
        if self._client is not None:
            self._client.go_visible()

    def quit(self):
        log.debug("BrowserLinker.quit")
        if self._client is not None:
            self._client.quit()

    def get_state(self) -> dict:
        client_state = self._client.get_state() if self._client is not None else None
        with self._lock:
            return {
                "status": self._status,
                "data": self._data,
                "error": self._error,
                "fetched_at": self._fetched_at,
                "headless": client_state.headless if client_state is not None else False,
            }

    # ── Callbacks from InterceptorClient (worker thread) ──────────────────────

    def _on_data(self, parsed: dict):
        with self._lock:
            self._data = parsed
            self._error = None
            self._status = "ok"
            self._fetched_at = datetime.now()
        self._notify()

    def _on_cdp_status(self, status: str, error: str | None):
        with self._lock:
            self._status = status
            if error is not None:
                self._error = error
        self._notify()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _notify(self):
        if self._on_update:
            try:
                self._on_update(self.get_state())
            except Exception as exc:
                log.error("Error in BrowserLinker._notify callback: %s", exc)
