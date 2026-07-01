"""InterceptorClient — high-level façade over Chrome launch + CDP interception.

Thread-safe. All configuration flows through the constructor — no module
globals. Callbacks fire on the background worker thread.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from cdp_interceptor.cdp_session import Capture, run_session
from cdp_interceptor.launcher import (
    ChromeNotFoundError,
    clear_singleton_locks,
    find_chrome,
    start_chrome,
)
from cdp_interceptor.sentinel import (
    clear_session,
    mark_session_ok,
    session_exists,
)

logger = logging.getLogger("cdp_interceptor")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BUNDLED_INTERCEPTOR_JS: str = open(
    os.path.join(_HERE, "interceptor.js"), encoding="utf-8"
).read()

ParseFn = Callable[[Capture], Optional[dict]]
OnData = Callable[[dict], None]
OnCapture = Callable[[Capture], None]
OnStatus = Callable[[str, Optional[str]], None]


@dataclass
class ClientState:
    """Snapshot of InterceptorClient state, safe to expose to callers."""
    status: str                     # "unlinked"|"loading"|"waiting_login"|"ok"|"error"
    headless: bool
    error: Optional[str]
    last_capture_at: Optional[float]  # time.monotonic() seconds; None if never captured


class InterceptorClient:
    """Launches an isolated Chrome, injects an interceptor into a target page,
    and streams captured JSON responses to caller callbacks.

    See ``cdp_interceptor/__init__.py`` for the parameter and usage overview.
    """

    def __init__(
        self,
        profile_dir: str,
        debug_port: int = 9222,
        *,
        debug_logging: bool = False,
        url_patterns: Optional[list[str | re.Pattern]] = None,
        parse_fn: Optional[ParseFn] = None,
        on_data: Optional[OnData] = None,
        on_capture: Optional[OnCapture] = None,
        on_status: Optional[OnStatus] = None,
        session_sentinel: bool = True,
        login_timeout: int = 300,
        capture_timeout: int = 30,
        capture_poll: float = 2.0,
        login_url_keywords: tuple[str, ...] = ("login", "signin", "/auth"),
        chrome_path: Optional[str] = None,
        interceptor_script: Optional[str] = None,
    ) -> None:
        self._profile_dir = profile_dir
        self._debug_port = debug_port
        self._debug_logging = debug_logging
        self._url_patterns: Optional[list[re.Pattern]] = (
            [re.compile(p) if isinstance(p, str) else p for p in url_patterns]
            if url_patterns else None
        )
        self._user_parse_fn = parse_fn
        self._user_on_data = on_data
        self._user_on_capture = on_capture
        self._user_on_status = on_status
        self._session_sentinel = session_sentinel
        self._login_timeout = login_timeout
        self._capture_timeout = capture_timeout
        self._capture_poll = capture_poll
        self._login_url_keywords = login_url_keywords
        self._chrome_path = chrome_path
        self._interceptor_script_override = interceptor_script

        # Runtime state (guarded by _lock)
        self._lock = threading.Lock()
        self._status = "unlinked"
        self._error: Optional[str] = None
        self._headless = False
        self._last_capture_at: Optional[float] = None

        # Worker plumbing
        self._proc: Optional[subprocess.Popen] = None
        self._target_url: Optional[str] = None
        self._reload_event = threading.Event()
        self._stop_event = threading.Event()
        self._worker: Optional[threading.Thread] = None

    # ── Public API ────────────────────────────────────────────────────────────

    @staticmethod
    def is_available() -> bool:
        """True if the runtime deps (``requests`` and ``websocket-client``)
        are importable."""
        try:
            import requests  # noqa: F401
            import websocket  # noqa: F401
            return True
        except ImportError as exc:
            logger.debug("InterceptorClient not available: %s", exc)
            return False

    @property
    def is_running(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def launch(self, target_url: str) -> None:
        """Start Chrome (headless if the sentinel exists) and begin the CDP
        loop. Non-blocking — spawns a worker thread. A second call while
        running is a no-op (logs a warning)."""
        if self.is_running:
            logger.warning("InterceptorClient.launch: already running — ignoring")
            return

        chrome = self._chrome_path or find_chrome()
        if chrome is None:
            with self._lock:
                self._status = "error"
                self._error = "Chrome not found — install Google Chrome"
            self._notify_status()
            raise ChromeNotFoundError("No Chrome executable found")

        os.makedirs(self._profile_dir, exist_ok=True)
        clear_singleton_locks(self._profile_dir)

        self._chrome_path = chrome
        self._target_url = target_url
        self._stop_event.clear()
        self._reload_event.clear()

        headless = self._session_sentinel and session_exists(self._profile_dir)
        with self._lock:
            self._headless = headless
        self._proc = start_chrome(
            chrome,
            headless=headless,
            debug_port=self._debug_port,
            profile_dir=self._profile_dir,
            target_url=target_url,
        )
        logger.debug("InterceptorClient.launch: Chrome started (pid=%s)", self._proc.pid)

        with self._lock:
            self._status = "loading"
            self._error = None
        self._notify_status()

        self._worker = threading.Thread(target=self._loop, daemon=True)
        self._worker.start()

    def fetch_now(self) -> None:
        """Signal the live CDP session to reload the target URL."""
        self._reload_event.set()

    def go_headless(self) -> None:
        """Relaunch Chrome headlessly. No-op if there's no session sentinel
        (an interactive login would still be required)."""
        if self._session_sentinel and not session_exists(self._profile_dir):
            logger.warning("go_headless: no session sentinel — cannot go headless")
            return
        self._relaunch(headless=True)

    def go_visible(self) -> None:
        """Relaunch Chrome visibly."""
        self._relaunch(headless=False)

    def quit(self) -> None:
        """Terminate the Chrome process and stop the worker thread. Safe to
        call multiple times."""
        self._stop_event.set()
        self._kill_chrome("quit")
        # Give the worker a moment to notice the stop; don't join indefinitely.
        if self._worker is not None and self._worker.is_alive():
            self._worker.join(timeout=2)
        self._worker = None

    def get_state(self) -> ClientState:
        with self._lock:
            return ClientState(
                status=self._status,
                headless=self._headless,
                error=self._error,
                last_capture_at=self._last_capture_at,
            )

    # ── Internal ──────────────────────────────────────────────────────────────

    @property
    def _interceptor_script(self) -> str:
        base = self._interceptor_script_override or _BUNDLED_INTERCEPTOR_JS
        flag = "true" if self._debug_logging else "false"
        return f"const DEBUG_LOGGING = {flag};\n" + base

    def _kill_chrome(self, label: str = "") -> None:
        if self._proc is None:
            return
        proc, self._proc = self._proc, None
        try:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except Exception:
                proc.kill()
                proc.wait(timeout=2)
        except Exception as exc:
            suffix = f" ({label})" if label else ""
            logger.warning("InterceptorClient._kill_chrome%s: %s", suffix, exc)

    def _relaunch(self, *, headless: bool) -> None:
        if not self._chrome_path or not self._target_url:
            logger.warning("relaunch: launch() has not been called yet")
            return
        # Ask the current worker to stop, kill Chrome, restart both.
        self._stop_event.set()
        self._kill_chrome(f"relaunch(headless={headless})")
        if self._worker is not None and self._worker.is_alive():
            self._worker.join(timeout=2)
        self._stop_event.clear()
        self._reload_event.clear()
        with self._lock:
            self._headless = headless
            self._status = "loading"
        self._proc = start_chrome(
            self._chrome_path,
            headless=headless,
            debug_port=self._debug_port,
            profile_dir=self._profile_dir,
            target_url=self._target_url,
        )
        logger.debug("InterceptorClient._relaunch: Chrome pid=%s headless=%s",
                     self._proc.pid, headless)
        self._notify_status()
        self._worker = threading.Thread(target=self._loop, daemon=True)
        self._worker.start()

    # ── Callbacks handed to run_session ───────────────────────────────────────

    def _on_data_inner(self, parsed: dict) -> None:
        """Wraps user's on_data so we can update state + mark sentinel."""
        with self._lock:
            self._status = "ok"
            self._error = None
            self._last_capture_at = time.monotonic()
        if self._session_sentinel:
            mark_session_ok(self._profile_dir)
        self._notify_status()
        if self._user_on_data:
            try:
                self._user_on_data(parsed)
            except Exception as exc:
                logger.warning("user on_data raised: %s", exc)

    def _on_capture_inner(self, cap: Capture) -> None:
        if self._user_on_capture:
            try:
                self._user_on_capture(cap)
            except Exception as exc:
                logger.warning("user on_capture raised: %s", exc)

    def _on_status_inner(self, status: str, error: Optional[str]) -> None:
        with self._lock:
            self._status = status
            if error is not None:
                self._error = error
        self._notify_status()

    def _notify_status(self) -> None:
        if self._user_on_status is None:
            return
        with self._lock:
            status, error = self._status, self._error
        try:
            self._user_on_status(status, error)
        except Exception as exc:
            logger.warning("user on_status raised: %s", exc)

    # ── Worker loop ───────────────────────────────────────────────────────────

    def _loop(self) -> None:
        """Reconnect-forever loop. Handles TimeoutError → sentinel-driven
        visible relaunch. Exits when stop_event is set."""
        time.sleep(4)  # give Chrome time to open its tab
        while not self._stop_event.is_set():
            with self._lock:
                self._status = "loading"
            self._notify_status()
            try:
                run_session(
                    debug_port=self._debug_port,
                    interceptor_script=self._interceptor_script,
                    target_url=self._target_url or "",
                    parse_fn=self._user_parse_fn,
                    on_data=self._on_data_inner,
                    on_capture=self._on_capture_inner,
                    on_status=self._on_status_inner,
                    reload_event=self._reload_event,
                    stop_event=self._stop_event,
                    login_timeout=self._login_timeout,
                    capture_timeout=self._capture_timeout,
                    capture_poll=self._capture_poll,
                    url_patterns=self._url_patterns,
                    login_url_keywords=self._login_url_keywords,
                )
            except TimeoutError as exc:
                # Login timed out. If we were running headless the session
                # expired — clear sentinel and relaunch visibly so user can
                # log in again.
                if (
                    self._session_sentinel
                    and session_exists(self._profile_dir)
                    and self._chrome_path
                ):
                    logger.warning(
                        "InterceptorClient._loop: headless session expired, relaunching visibly"
                    )
                    clear_session(self._profile_dir)
                    self._kill_chrome("session-expired")
                    with self._lock:
                        self._headless = False
                        self._error = "Session expired — please log in again"
                        self._status = "waiting_login"
                    self._proc = start_chrome(
                        self._chrome_path,
                        headless=False,
                        debug_port=self._debug_port,
                        profile_dir=self._profile_dir,
                        target_url=self._target_url or "",
                    )
                    self._notify_status()
                else:
                    with self._lock:
                        self._error = str(exc)
                        self._status = "error"
                    self._notify_status()
            except Exception as exc:
                logger.error("InterceptorClient._loop: %s", exc)
                with self._lock:
                    self._error = str(exc)
                    self._status = "error"
                self._notify_status()

            if self._stop_event.is_set():
                break
            logger.debug("InterceptorClient._loop: session ended, reconnecting in 15s")
            self._stop_event.wait(15)
