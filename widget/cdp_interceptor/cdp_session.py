"""CDP session — WebSocket connection to a running Chrome debug endpoint.

Injects the fetch/XHR interceptor script into the target tab, then delivers
captured JSON response bodies to caller callbacks. Blocks until the WebSocket
dies or ``stop_event`` is set.

Public API
----------
- ``run_session(...)`` — main session driver (blocks; caller reconnects on return)
- ``Capture`` — dataclass with ``url`` and ``body``
"""

from __future__ import annotations

import json as _json
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

logger = logging.getLogger("cdp_interceptor")


@dataclass
class Capture:
    """A single JSON response body captured by the interceptor."""
    url: str
    body: dict


def run_session(
    *,
    debug_port: int,
    interceptor_script: str,
    target_url: str,
    parse_fn: Optional[Callable[[Capture], Optional[dict]]],
    on_data: Optional[Callable[[dict], None]],
    on_capture: Optional[Callable[[Capture], None]],
    on_status: Callable[[str, Optional[str]], None],
    reload_event: threading.Event,
    stop_event: threading.Event,
    login_timeout: int,
    capture_timeout: int,
    capture_poll: float,
    url_patterns: Optional[list[re.Pattern]] = None,
    login_url_keywords: tuple[str, ...] = ("login", "signin", "/auth"),
    tab_url_hint: str = "",
) -> None:
    """Persistent CDP session: initial capture, then live binding-event loop.

    Blocks until either the WebSocket connection dies, ``stop_event`` is set,
    or a fatal error is raised. Returns cleanly so callers can decide whether
    to reconnect.

    Parameters
    ----------
    debug_port : int
        Chrome remote-debugging port.
    interceptor_script : str
        JS source injected into the target tab (already prefixed with
        ``const DEBUG_LOGGING = ...;`` by the caller).
    target_url : str
        URL the caller wants Chrome to be at. If Chrome is currently on a
        login page, we wait; otherwise we navigate/reload to this URL.
    parse_fn, on_data, on_capture, on_status
        Callbacks. ``on_capture`` fires for every capture (unfiltered).
        ``parse_fn`` is invoked only on URL-pattern-matched captures.
        ``on_data`` fires when ``parse_fn`` returns a truthy dict (or on
        every url_pattern match when ``parse_fn`` is None, with the raw body).
    reload_event : threading.Event
        Caller sets this to request a live-loop page reload.
    stop_event : threading.Event
        Caller sets this to request clean shutdown.
    url_patterns : list[re.Pattern] | None
        When set, only captures whose URL matches at least one pattern reach
        ``parse_fn`` / ``on_data``. Non-matching captures still fire ``on_capture``.
    login_url_keywords : tuple[str, ...]
        Substrings in ``location.href`` that indicate a login page.
    tab_url_hint : str
        Substring used to prefer a specific existing tab when Chrome has
        multiple pages open. Empty = pick any ``type=="page"`` tab.
    """
    import websocket as _ws_mod
    import requests as _req

    # ── Connect to Chrome's debug endpoint ────────────────────────────────────
    tabs = None
    for attempt in range(15):
        if stop_event.is_set():
            return
        try:
            tabs = _req.get(f"http://localhost:{debug_port}/json", timeout=3).json()
            break
        except Exception:
            if attempt == 14:
                raise RuntimeError(
                    "Cannot connect to Chrome — make sure the window is still open"
                )
            time.sleep(2)

    tab = None
    if tab_url_hint:
        tab = next(
            (t for t in tabs if t.get("type") == "page" and tab_url_hint in t.get("url", "")),
            None,
        )
    if tab is None:
        tab = next((t for t in tabs if t.get("type") == "page"), None)
    if tab is None:
        raise RuntimeError("No page tab found in the debug-controlled Chrome")

    ws = _ws_mod.create_connection(tab["webSocketDebuggerUrl"], timeout=15)
    _id = [0]

    # ── Low-level RPC ─────────────────────────────────────────────────────────

    def rpc(method: str, params: Optional[dict] = None, _timeout: float = 10) -> dict:
        _id[0] += 1
        my_id = _id[0]
        ws.send(_json.dumps({"id": my_id, "method": method, "params": params or {}}))
        ws.settimeout(1)
        deadline = time.time() + _timeout
        try:
            while time.time() < deadline:
                if stop_event.is_set():
                    return {}
                try:
                    msg = _json.loads(ws.recv())
                except _ws_mod.WebSocketTimeoutException:
                    continue
                if msg.get("id") == my_id:
                    return msg.get("result", {})
        finally:
            ws.settimeout(None)
        return {}

    def eval_str(expr: str) -> str:
        result = rpc("Runtime.evaluate", {"expression": expr, "returnByValue": True})
        return result.get("result", {}).get("value", "") or ""

    # ── Capture handling helpers ──────────────────────────────────────────────

    def _url_matches(url: str) -> bool:
        if not url_patterns:
            return True
        return any(p.search(url) for p in url_patterns)

    def _process_capture(cap: Capture) -> Optional[dict]:
        """Fire on_capture (unfiltered), apply url_patterns, run parse_fn,
        return the parsed dict (or None). Does NOT call on_data — caller decides."""
        if on_capture:
            try:
                on_capture(cap)
            except Exception as exc:
                logger.warning("cdp_session: on_capture raised: %s", exc)
        if not _url_matches(cap.url):
            return None
        if parse_fn is None:
            return cap.body if isinstance(cap.body, dict) else None
        try:
            return parse_fn(cap)
        except Exception as exc:
            logger.warning("cdp_session: parse_fn raised for %s: %s", cap.url, exc)
            return None

    def _find_initial(captured: list) -> Optional[dict]:
        """Walk existing captures in insertion order; first parseable wins."""
        for item in captured:
            url = item.get("url", "")
            body = item.get("body")
            if not isinstance(body, dict):
                continue
            parsed = _process_capture(Capture(url=url, body=body))
            if parsed:
                return parsed
        return None

    def _navigate_and_capture(nav_url: str) -> dict:
        """Pre-register the interceptor, navigate/reload, then poll
        _capturedResponses until a URL-pattern-matched, parseable body appears
        or capture_timeout expires."""
        rpc("Page.addScriptToEvaluateOnNewDocument", {"source": interceptor_script})
        logger.debug("cdp_session: interceptor pre-registered for next document")

        href = eval_str("location.href")
        # Reload if we're already at the target, otherwise navigate.
        if nav_url and nav_url in href:
            logger.debug("cdp_session: already at target, reloading")
            rpc("Page.reload", {})
        else:
            logger.debug("cdp_session: navigating to %s", nav_url)
            rpc("Page.navigate", {"url": nav_url})

        # Also inject into the current document.
        rpc("Runtime.evaluate", {"expression": interceptor_script})

        deadline = time.time() + capture_timeout
        attempt = 0
        while time.time() < deadline:
            if stop_event.is_set():
                raise RuntimeError("stopped")
            time.sleep(capture_poll)
            attempt += 1
            raw = eval_str("JSON.stringify(window._capturedResponses || [])")
            captured = _json.loads(raw) if raw else []
            logger.debug("cdp_session: poll #%d — %d response(s)", attempt, len(captured))
            result = _find_initial(captured)
            if result:
                logger.debug("cdp_session: parsed usable data on poll #%d", attempt)
                return result

        raise RuntimeError(
            f"No matching data found after {capture_timeout}s — the endpoint "
            "may not have been called, or url_patterns/parse_fn didn't match"
        )

    # ── Main session body ─────────────────────────────────────────────────────

    try:
        href = eval_str("location.href")
        if any(kw in href for kw in login_url_keywords):
            on_status("waiting_login", None)
            logger.debug("cdp_session: waiting for user to log in")
            deadline = time.time() + login_timeout
            while time.time() < deadline:
                if stop_event.is_set():
                    return
                time.sleep(3)
                href = eval_str("location.href")
                if not any(kw in href for kw in login_url_keywords):
                    break
            else:
                raise TimeoutError(
                    f"Login timed out ({login_timeout // 60} min) — retry launch"
                )

        # Enable the Page domain so addScriptToEvaluateOnNewDocument sticks
        # and Page.loadEventFired fires in the live loop.
        rpc("Page.enable")
        # Runtime.enable so Runtime.addBinding actually injects
        # window.__cdpNotify into the page context.
        rpc("Runtime.enable")
        rpc("Runtime.addBinding", {"name": "__cdpNotify"})
        # Hide navigator.webdriver so sites don't flag automation.
        rpc(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": (
                    "Object.defineProperty(navigator, 'webdriver', "
                    "{get: () => undefined});"
                )
            },
        )

        data = _navigate_and_capture(target_url)
        if on_data:
            on_data(data)

        # ── Persistent live-update loop ───────────────────────────────────────
        logger.debug("cdp_session: entering live-event loop")

        def _send(method: str, params: Optional[dict] = None) -> None:
            _id[0] += 1
            ws.send(_json.dumps({"id": _id[0], "method": method, "params": params or {}}))

        def _poll_captured() -> None:
            """Read window._capturedResponses and process any new items."""
            nonlocal _last_captured_idx
            try:
                raw = eval_str("JSON.stringify(window._capturedResponses || [])")
                captured = _json.loads(raw) if raw else []
            except Exception:
                return
            for item in captured[_last_captured_idx:]:
                url = item.get("url", "")
                body = item.get("body")
                if not isinstance(body, dict):
                    continue
                parsed = _process_capture(Capture(url=url, body=body))
                if parsed and on_data:
                    logger.debug("cdp_session: live update via poll (idx=%d)", _last_captured_idx)
                    on_data(parsed)
            _last_captured_idx = len(captured)

        _last_captured_idx = 0
        try:
            raw = eval_str("JSON.stringify(window._capturedResponses || [])")
            _last_captured_idx = len(_json.loads(raw)) if raw else 0
        except Exception:
            pass

        ws.settimeout(5)
        while not stop_event.is_set():
            if reload_event.is_set():
                reload_event.clear()
                _last_captured_idx = 0
                logger.debug("cdp_session: reload requested — navigating")
                _send("Page.addScriptToEvaluateOnNewDocument", {"source": interceptor_script})
                _send("Page.navigate", {"url": target_url})

            try:
                msg = _json.loads(ws.recv())
            except _ws_mod.WebSocketTimeoutException:
                # Keep-alive tick — poll for anything the binding may have
                # missed (early-load API calls).
                _poll_captured()
                continue

            method = msg.get("method", "")

            if method == "Page.loadEventFired":
                logger.debug("cdp_session: page loaded — re-registering binding/interceptor")
                _last_captured_idx = 0
                _send("Runtime.addBinding", {"name": "__cdpNotify"})
                _send("Runtime.evaluate", {"expression": interceptor_script})
                continue

            if (
                method == "Runtime.bindingCalled"
                and msg.get("params", {}).get("name") == "__cdpNotify"
            ):
                try:
                    payload = _json.loads(msg["params"].get("payload", "{}"))
                    url = payload.get("url", "")
                    body = payload.get("body")
                    if isinstance(body, dict):
                        parsed = _process_capture(Capture(url=url, body=body))
                        if parsed and on_data:
                            logger.debug("cdp_session: live update via binding")
                            on_data(parsed)
                            # Advance poll index to match so the next tick
                            # doesn't re-process the same item.
                            try:
                                raw = eval_str("JSON.stringify(window._capturedResponses || [])")
                                _last_captured_idx = len(_json.loads(raw)) if raw else _last_captured_idx
                            except Exception:
                                pass
                except Exception as exc:
                    logger.warning("cdp_session: error processing binding event: %s", exc)

    finally:
        try:
            ws.close()
        except Exception:
            pass
