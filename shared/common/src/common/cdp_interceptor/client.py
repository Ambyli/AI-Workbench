"""InterceptorClient — high-level façade over Chrome launch + CDP interception.

Thread-safe. All configuration flows through the constructor — no module
globals. Callbacks fire on the background worker thread.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .cdp_session import Capture, run_session
from .launcher import (
    BrowserNotFoundError,
    clear_singleton_locks,
    find_browser,
    kill_chrome_by_profile,
    start_browser,
)
from .sentinel import (
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
        # Guard against accidental double-launch from the same client. Callers
        # who want a fresh session should quit() first.
        if self.is_running:
            logger.warning("InterceptorClient.launch: already running — ignoring")
            return

        # Locate the browser executable. `chrome_path` overrides `find_browser()`
        # if the caller supplied one; otherwise Windows finds installed Chrome
        # and Linux/mac finds Playwright's bundled chromium.
        browser = self._chrome_path or find_browser()
        if browser is None:
            with self._lock:
                self._status = "error"
                self._error = (
                    "No browser found — install Google Chrome (Windows) or "
                    "run `playwright install chromium` (Linux/mac)"
                )
            self._notify_status()
            raise BrowserNotFoundError("No browser executable found")

        # Ensure the profile dir exists and clear any stale singleton locks
        # left over from a prior Chrome crash (see launcher.clear_singleton_locks).
        os.makedirs(self._profile_dir, exist_ok=True)
        clear_singleton_locks(self._profile_dir)

        # Remember the resolved browser path and target URL so relaunch/reload
        # paths can reuse them without the caller re-supplying.
        self._chrome_path = browser
        self._target_url = target_url

        # Fresh events for this worker generation. See _relaunch for why we
        # ROTATE rather than clear — old workers keep a reference to the old
        # (set) events and exit; new worker looks at the new (unset) ones.
        self._stop_event = threading.Event()
        self._reload_event = threading.Event()

        # Decide whether to launch headless. Only headless if:
        #   1. session_sentinel is enabled (caller opted in), AND
        #   2. a sentinel file exists in the profile dir (we've had at least
        #      one successful login on this profile before).
        # First-ever launch always goes visible so the user can log in.
        headless = self._session_sentinel and session_exists(self._profile_dir)
        with self._lock:
            self._headless = headless

        # Fork Chrome. Popen returns immediately; Chrome takes ~1-3s to
        # start the debug server, which the worker thread handles by polling.
        self._proc = start_browser(
            browser,
            headless=headless,
            debug_port=self._debug_port,
            profile_dir=self._profile_dir,
            target_url=target_url,
        )
        logger.debug("InterceptorClient.launch: Chrome started (pid=%s)", self._proc.pid)

        # Report "loading" so caller UI can show a spinner or similar.
        with self._lock:
            self._status = "loading"
            self._error = None
        self._notify_status()

        # Spawn the worker thread. daemon=True so it dies with the process
        # if the caller forgets to quit() explicitly. The events are passed
        # by value (reference) so the worker holds its own copy and can't be
        # confused by a later _relaunch rotating self._{stop,reload}_event.
        self._worker = threading.Thread(
            target=self._loop,
            args=(self._stop_event, self._reload_event),
            daemon=True,
        )
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
        """Return the JS to inject, with DEBUG_LOGGING prepended as a constant.

        Callers can override the bundled interceptor.js entirely via the
        constructor's `interceptor_script` param; otherwise we use the file
        that shipped with the package. Either way, we prepend a `const
        DEBUG_LOGGING = <bool>;` line so the injected script can gate its
        own console.log calls without needing runtime configuration.
        """
        base = self._interceptor_script_override or _BUNDLED_INTERCEPTOR_JS
        flag = "true" if self._debug_logging else "false"
        return f"const DEBUG_LOGGING = {flag};\n" + base

    def _kill_chrome(self, label: str = "") -> None:
        """Terminate the current Chrome process AND its children.

        On Windows, ``proc.terminate()`` only kills Chrome's main process —
        its child processes (GPU, renderer, network service) survive as
        orphans and continue to hold the profile's named mutex. A subsequent
        relaunch against the same ``--user-data-dir`` then IPC-hands-off the
        URL to a dying renderer and exits with code 21 (NORMAL_EXIT_PROCESS_
        NOTIFIED) — no visible window ever appears.

        Fix: kill the whole process tree. On Windows we use ``taskkill /F /T
        /PID``; elsewhere we fall back to terminate()+kill() on the main
        process, which is enough on POSIX because setsid isn't in play.
        """
        if self._proc is None:
            return
        # Move self._proc into a local var before killing so a concurrent
        # quit() can't double-kill the same handle.
        proc, self._proc = self._proc, None
        pid = proc.pid
        try:
            if sys.platform == "win32":
                # /F force-kill, /T kill process tree, /PID target the tree root.
                # We don't check the returncode: taskkill returns non-zero if
                # any process was already gone, which is fine — we still want
                # to reap the main handle below to release the OS resource.
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=5,
                )
            else:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except Exception:
                    proc.kill()

            # Reap the main handle to release the Popen wait state, whichever
            # branch we took above.
            try:
                proc.wait(timeout=3)
            except Exception:
                pass
        except Exception as exc:
            suffix = f" ({label})" if label else ""
            logger.warning("InterceptorClient._kill_chrome%s: %s", suffix, exc)

    def _relaunch(self, *, headless: bool) -> None:
        """Stop the current session and start a fresh one in the requested mode.

        Called by go_headless() / go_visible(). We can't just re-navigate the
        existing tab because the launch flags (`--headless=new`, window size,
        user-agent) are set at process start — the only way to change them is
        to kill and restart Chrome.
        """
        if not self._chrome_path or not self._target_url:
            # Nothing to relaunch — launch() was never called successfully.
            logger.warning("relaunch: launch() has not been called yet")
            return

        import os as _os
        import time as _time

        logger.debug("relaunch: begin headless=%s profile=%s target=%s",
                     headless, self._profile_dir, self._target_url)

        # Signal the worker to stop, then wait briefly for it to exit its
        # inner loops. Chrome dies first so any in-flight CDP calls fail fast.
        self._stop_event.set()
        self._kill_chrome(f"relaunch(headless={headless})")
        worker_alive_before = self._worker is not None and self._worker.is_alive()
        if worker_alive_before:
            self._worker.join(timeout=2)
        logger.debug("relaunch: kill done, old worker alive_before=%s alive_after=%s",
                     worker_alive_before,
                     self._worker is not None and self._worker.is_alive())

        # Rotate to fresh events for the new worker. The old worker is still
        # holding a reference to the OLD stop_event (which is set) — it will
        # exit on its next check even if it wakes up much later (e.g. from
        # the 15s reconnect backoff). If we merely cleared and reused the
        # same events, the old worker would come back to life and race the
        # new one against the same debug port, producing double navigation
        # and double capture updates.
        self._stop_event = threading.Event()
        self._reload_event = threading.Event()

        # Clear the singleton locks left behind by the just-killed Chrome.
        # Without this, the new browser sees SingletonLock/Cookie/Socket in
        # the user-data-dir, believes another Chrome is still using this
        # profile, and hands off the URL via IPC instead of opening a new
        # window. The IPC target (the dead headless Chrome) can't display
        # it, so the visible window never appears. `launch()` does the same
        # thing for the same reason.
        locks_before = [
            lf for lf in ("SingletonLock", "SingletonCookie", "SingletonSocket")
            if _os.path.exists(_os.path.join(self._profile_dir, lf))
        ]
        clear_singleton_locks(self._profile_dir)
        locks_after = [
            lf for lf in ("SingletonLock", "SingletonCookie", "SingletonSocket")
            if _os.path.exists(_os.path.join(self._profile_dir, lf))
        ]
        logger.debug("relaunch: singleton locks before=%s after=%s",
                     locks_before, locks_after)

        # Windows only: reap any chrome.exe that survived the process-tree kill
        # (updater, crashpad, utility procs) and is still bound to this profile.
        # If any linger, the new browser IPC-hands-off to them and exits with
        # code 21 instead of opening a window. Then wait for the OS to actually
        # release the mutex.
        killed = kill_chrome_by_profile(self._profile_dir)
        logger.debug("relaunch: kill_chrome_by_profile killed=%d", killed)
        _time.sleep(1.0 if killed else 0.5)

        # Update our state to reflect the new mode BEFORE spawning so any
        # get_state() call in between sees consistent data.
        with self._lock:
            self._headless = headless
            self._status = "loading"

        # Launch Chrome again with the new headless flag.
        self._proc = start_browser(
            self._chrome_path,
            headless=headless,
            debug_port=self._debug_port,
            profile_dir=self._profile_dir,
            target_url=self._target_url,
        )
        logger.debug("relaunch: start_browser returned pid=%s headless=%s",
                     self._proc.pid, headless)

        # Chrome, on Windows, sometimes IPC-hands-off to an existing process
        # and immediately exits. Poll briefly to detect this — if the launcher
        # process is gone within 1s, the visible window didn't actually open.
        for _i in range(5):
            _time.sleep(0.2)
            code = self._proc.poll()
            if code is not None:
                logger.error(
                    "relaunch: launcher process exited immediately with code=%s — "
                    "this usually means Chrome detected a singleton and handed off "
                    "the URL to an existing (or dying) process. Profile: %s",
                    code, self._profile_dir,
                )
                break
        else:
            logger.debug("relaunch: launcher process alive after 1s — new Chrome is up")

        self._notify_status()

        # Spawn a fresh worker with the new events captured as thread args.
        self._worker = threading.Thread(
            target=self._loop,
            args=(self._stop_event, self._reload_event),
            daemon=True,
        )
        self._worker.start()

    # ── Callbacks handed to run_session ───────────────────────────────────────
    # These wrap the user's callbacks so we can update our own state (status,
    # sentinel, last_capture_at) before/after forwarding.

    def _on_data_inner(self, parsed: dict) -> None:
        """Called by run_session each time a parseable body arrives.

        Order of operations matters:
          1. Update internal state to "ok" so subsequent get_state() reflects
             a successful capture.
          2. Write the sentinel file — this is what enables future headless
             launches. Only do it if the caller opted into sentinel mode.
          3. Notify status listeners so callers see "ok".
          4. Fire the user's on_data callback with the parsed dict.
        """
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
                # User callback exceptions must not crash the worker.
                logger.warning("user on_data raised: %s", exc)

    def _on_capture_inner(self, cap: Capture) -> None:
        """Forward every raw capture to the user's on_capture, if any.
        We don't touch state here — on_capture is a pure inspection hook,
        not a "success" signal (a capture may arrive that doesn't parse)."""
        if self._user_on_capture:
            try:
                self._user_on_capture(cap)
            except Exception as exc:
                logger.warning("user on_capture raised: %s", exc)

    def _on_status_inner(self, status: str, error: Optional[str]) -> None:
        """run_session reports status changes ("waiting_login", etc.) here.
        Merge them into our state and notify the user's on_status callback."""
        with self._lock:
            self._status = status
            if error is not None:
                self._error = error
        self._notify_status()

    def _notify_status(self) -> None:
        """Invoke the user's on_status callback with a lock-guarded snapshot.
        Snapshot-then-release means the callback doesn't hold the lock while
        it runs, so it can safely call back into get_state() if it wants."""
        if self._user_on_status is None:
            return
        with self._lock:
            status, error = self._status, self._error
        try:
            self._user_on_status(status, error)
        except Exception as exc:
            logger.warning("user on_status raised: %s", exc)

    # ── Worker loop ───────────────────────────────────────────────────────────

    def _loop(self, stop_event: threading.Event, reload_event: threading.Event) -> None:
        """Reconnect-forever loop. Handles TimeoutError → sentinel-driven
        visible relaunch. Exits when the local stop_event is set.

        stop_event and reload_event are passed in explicitly (rather than
        read from self.*) so each worker generation has a private cancellation
        token. When _relaunch spawns a new worker, it also assigns fresh
        events to self._stop_event / self._reload_event — the old worker
        keeps its reference to the OLD (set) events and exits, while the new
        worker sees the new (unset) events. This prevents a stale worker
        from reconnecting to the freshly-launched Chrome.
        """
        # Give Chrome time to open its tab and start the debug endpoint before
        # we try to connect. Without this we hit a race where run_session's
        # /json poll starts before Chrome is ready, wastes retries, and fails.
        time.sleep(4)

        while not stop_event.is_set():
            # Report "loading" at the top of each attempt. Status may have
            # been "ok" from a previous session that just died — reset so
            # callers see we're re-establishing.
            with self._lock:
                self._status = "loading"
            self._notify_status()

            try:
                # Run one CDP session — blocks until the WebSocket dies,
                # stop_event fires, or an exception is raised.
                run_session(
                    debug_port=self._debug_port,
                    interceptor_script=self._interceptor_script,
                    target_url=self._target_url or "",
                    parse_fn=self._user_parse_fn,
                    on_data=self._on_data_inner,
                    on_capture=self._on_capture_inner,
                    on_status=self._on_status_inner,
                    reload_event=reload_event,
                    stop_event=stop_event,
                    login_timeout=self._login_timeout,
                    capture_timeout=self._capture_timeout,
                    capture_poll=self._capture_poll,
                    url_patterns=self._url_patterns,
                    login_url_keywords=self._login_url_keywords,
                )
            except TimeoutError as exc:
                # TimeoutError specifically means the user didn't complete
                # login in `login_timeout` seconds. Two possible scenarios:
                #
                # A) We were running HEADLESS and the sentinel says we've
                #    logged in before — this means the persisted session
                #    expired. Clear the sentinel, kill the headless Chrome,
                #    and relaunch VISIBLY so the user can log in again.
                #    Status → "waiting_login" so the caller's UI shows a
                #    login prompt. The next loop iteration will start a
                #    fresh session against the visible Chrome.
                #
                # B) Anything else (visible mode, or no prior sentinel) —
                #    plain error, report it and let the loop reconnect.
                if (
                    self._session_sentinel
                    and session_exists(self._profile_dir)
                    and self._chrome_path
                ):
                    # Case A: headless session expired.
                    logger.warning(
                        "InterceptorClient._loop: headless session expired, relaunching visibly"
                    )
                    # Clear sentinel so we DON'T immediately go headless
                    # again — the user has to log in first and produce a
                    # fresh on_data, which will re-write the sentinel.
                    clear_session(self._profile_dir)
                    self._kill_chrome("session-expired")
                    with self._lock:
                        self._headless = False
                        self._error = "Session expired — please log in again"
                        self._status = "waiting_login"
                    # Launch a fresh visible Chrome. The next loop iteration
                    # will connect to it and wait for the user to complete login.
                    self._proc = start_browser(
                        self._chrome_path,
                        headless=False,
                        debug_port=self._debug_port,
                        profile_dir=self._profile_dir,
                        target_url=self._target_url or "",
                    )
                    self._notify_status()
                else:
                    # Case B: not a sentinel-recoverable scenario.
                    with self._lock:
                        self._error = str(exc)
                        self._status = "error"
                    self._notify_status()
            except Exception as exc:
                # Any other exception — record it and let the loop retry.
                # We don't crash the worker because the caller may not have
                # a way to notice and restart us.
                logger.error("InterceptorClient._loop: %s", exc)
                with self._lock:
                    self._error = str(exc)
                    self._status = "error"
                self._notify_status()

            # Check for shutdown before waiting — quit() may have been called
            # while we were mid-session; no point sleeping if we're stopping.
            if stop_event.is_set():
                break

            # Back off before reconnecting. Using stop_event.wait() instead
            # of time.sleep() so quit() can wake us early.
            logger.debug("InterceptorClient._loop: session ended, reconnecting in 15s")
            stop_event.wait(15)
