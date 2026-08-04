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
    """A single JSON response body captured by the interceptor.

    ``body`` is typed as ``Any`` because JSON can be any of dict, list,
    string, number, bool, or null. Callers should type-check before assuming
    a shape — many APIs return top-level arrays (e.g. Bubble.io's init/data
    returns a 2-element array, not an object).
    """
    url: str
    body: Any


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
    # Local imports so the library can be imported for introspection even
    # if the runtime deps are missing (is_available() checks for these).
    import websocket as _ws_mod
    import requests as _req

    # ── Connect to Chrome's debug endpoint ────────────────────────────────────
    # Chrome exposes an HTTP endpoint at /json that lists all open tabs and
    # returns each one's WebSocket URL. We poll it until Chrome is ready to
    # answer — up to 15 attempts × 2s = 30s worst-case wait for launch.
    tabs = None
    for attempt in range(15):
        if stop_event.is_set():
            return
        try:
            tabs = _req.get(f"http://localhost:{debug_port}/json", timeout=3).json()
            break
        except Exception:
            if attempt == 14:
                # Give up — either Chrome didn't start, was killed, or the
                # debug port is blocked. Caller decides whether to retry.
                raise RuntimeError(
                    "Cannot connect to Chrome — make sure the window is still open"
                )
            time.sleep(2)

    # Pick which tab to attach to. Chrome's /json response includes tabs of
    # multiple types (page, background_page, service_worker, iframe, ...);
    # we only care about "page" — actual browsing tabs.
    tab = None

    # First preference: a page tab whose URL contains tab_url_hint. Lets
    # callers target a specific tab when multiple pages are open.
    if tab_url_hint:
        tab = next(
            (t for t in tabs if t.get("type") == "page" and tab_url_hint in t.get("url", "")),
            None,
        )

    # Fallback: any page tab (typically the about:blank that Chrome opens
    # by default on --new-window launch).
    if tab is None:
        tab = next((t for t in tabs if t.get("type") == "page"), None)

    if tab is None:
        raise RuntimeError("No page tab found in the debug-controlled Chrome")

    # Open a persistent WebSocket to this specific tab. All subsequent CDP
    # commands and events flow through this socket.
    ws = _ws_mod.create_connection(tab["webSocketDebuggerUrl"], timeout=15)

    # Monotonic message ID counter. Every CDP command needs a unique ID
    # so responses can be matched back to the request that produced them.
    # Wrapped in a list so nested closures can mutate it.
    _id = [0]

    # ── Low-level RPC ─────────────────────────────────────────────────────────

    def rpc(method: str, params: Optional[dict] = None, _timeout: float = 10) -> dict:
        """Send a CDP command and block until we receive the matching response.

        CDP is a request/response protocol multiplexed over a single WebSocket.
        Chrome also floods the socket with unsolicited events (page loads,
        console messages, execution contexts, etc.). We have to walk past all
        those events looking for the message whose `id` matches our request.
        """
        # Reserve a fresh ID for this specific call.
        _id[0] += 1
        my_id = _id[0]
        ws.send(_json.dumps({"id": my_id, "method": method, "params": params or {}}))

        # 1-second read timeout so we can periodically check stop_event and
        # tick past unrelated events without blocking forever.
        ws.settimeout(1)

        # Use a wall-clock deadline (not attempt count) so bursts of unrelated
        # events can't starve us — if Chrome sends 10k events in a second,
        # we keep going until the total time budget elapses.
        deadline = time.time() + _timeout
        try:
            while time.time() < deadline:
                if stop_event.is_set():
                    return {}
                try:
                    msg = _json.loads(ws.recv())
                except _ws_mod.WebSocketTimeoutException:
                    # No message this second — loop and check the deadline again.
                    continue
                if msg.get("id") == my_id:
                    # Found our response.
                    return msg.get("result", {})
                # Anything else is an unsolicited event; skip and keep reading.
        finally:
            # Restore blocking behavior so subsequent recv() calls in the
            # live loop see the timeout they set themselves.
            ws.settimeout(None)
        # Timed out. Callers treat empty {} as "no result" rather than error.
        return {}

    def eval_str(expr: str) -> str:
        """Evaluate a JS expression in the page and return its string value.
        Thin wrapper around Runtime.evaluate — Chrome returns the raw value
        inline when `returnByValue=True`, avoiding the object-handle dance."""
        result = rpc("Runtime.evaluate", {"expression": expr, "returnByValue": True})
        return result.get("result", {}).get("value", "") or ""

    # ── Capture handling helpers ──────────────────────────────────────────────

    def _url_matches(url: str) -> bool:
        """True if `url` matches any pattern in url_patterns.

        When url_patterns is None or empty, every URL passes — the library
        defaults to "no filter", letting parse_fn or the caller decide.
        """
        if not url_patterns:
            return True
        # `search` (not `match`) — patterns are substring-matching by default,
        # so callers don't need to anchor with ^ / $.
        return any(p.search(url) for p in url_patterns)

    # Compile the login-wall URL patterns once. Entries are regexes matched
    # with re.search against location.href AFTER navigation (see the login gate
    # in _navigate_and_capture). Regex — not substring — so callers can anchor
    # to a bare domain that a substring couldn't tell apart from an in-app URL,
    # e.g. r"^https?://roofix\.io/?$". An empty list disables detection.
    _login_res = [re.compile(k) for k in login_url_keywords]

    def _is_login(href: str) -> bool:
        """True if href matches any login_url_keywords pattern."""
        return any(rx.search(href) for rx in _login_res)

    def _process_capture(cap: Capture) -> Optional[dict]:
        """Run one Capture through the full pipeline:
          1. fire on_capture (unfiltered raw stream — always fires)
          2. drop it if the URL doesn't match url_patterns
          3. hand it to parse_fn (or return body as-is if no parse_fn)
          4. return whatever parse_fn returned, or None to skip

        Does NOT call on_data — the caller (initial-capture or live-loop)
        decides when to deliver the parsed result.
        """
        # Step 1: fire the raw capture callback unconditionally. This is the
        # "debug/inspection stream" — useful for discovering endpoints before
        # you know what URL to filter for.
        if on_capture:
            try:
                on_capture(cap)
            except Exception as exc:
                # Never let a user callback kill the session.
                logger.warning("cdp_session: on_capture raised: %s", exc)

        # Step 2: URL filter.
        if not _url_matches(cap.url):
            return None

        # Step 3a: no parse_fn — forward the raw body straight to on_data
        # (guarded by isinstance so junk types don't leak out).
        if parse_fn is None:
            return cap.body if isinstance(cap.body, dict) else None

        # Step 3b: caller-provided parser gets the full Capture (url + body).
        try:
            return parse_fn(cap)
        except Exception as exc:
            logger.warning("cdp_session: parse_fn raised for %s: %s", cap.url, exc)
            return None

    # Sequence ids of captures we've already delivered. interceptor.js stamps
    # every capture with a monotonic ``seq`` (unique per document) and sends
    # that same seq on BOTH delivery paths — the Runtime.bindingCalled fast
    # path and the JS-array poll fallback. Deduping on seq lets the two paths
    # race freely without either dropping or double-delivering a response.
    #
    # This replaces the old high-water-mark index (``_last_captured_idx``),
    # which advanced to len(array) whenever ANY item was delivered — so a
    # response that had been appended but not yet delivered (its binding event
    # still in flight, or destined to be dropped) got marked "seen" and then
    # skipped by the poll forever. That race is what intermittently lost
    # elasticsearch/mget. A per-seq set has no such window.
    _delivered_seqs: set = set()

    def _handle_item(item: dict, *, deliver_on_data: bool) -> Optional[dict]:
        """Dedup one raw ``{seq, url, body}`` entry by seq, then run it through
        ``_process_capture`` (which always fires ``on_capture``).

        Returns the parsed dict (or None). Fires ``on_data`` only when
        ``deliver_on_data`` is set and a parser produced a dict. Entries missing
        a seq (e.g. an older interceptor.js) fall back to no-dedup handling so
        nothing is silently dropped.
        """
        body = item.get("body")
        if body is None:
            return None
        seq = item.get("seq")
        if seq is not None:
            if seq in _delivered_seqs:
                return None
            _delivered_seqs.add(seq)
        parsed = _process_capture(Capture(url=item.get("url", ""), body=body))
        if parsed and deliver_on_data and on_data:
            on_data(parsed)
        return parsed

    def _find_initial(captured: list) -> Optional[dict]:
        """Handle every not-yet-seen entry (deduped by seq) and return the
        first whose parse produces a dict.

        We do NOT stop at the first parseable dict, because callers rely on
        ``on_capture`` seeing the full raw stream — app.py collects its
        url-pattern matches purely from ``on_capture``, so an entry we skip
        here (e.g. an mget sitting after the winning response in the same
        batch) would be lost. Every entry is handled; only the returned
        "winner" is the first dict, and the caller fires on_data for it once.
        """
        winner: Optional[dict] = None
        for item in captured:
            parsed = _handle_item(item, deliver_on_data=False)
            if parsed is not None and winner is None:
                winner = parsed
        return winner

    def _await_login_resolved(nav_url: str) -> None:
        """Handle a login wall detected AFTER navigating to nav_url.

        Signals ``waiting_login``, then polls location.href until the user
        leaves the login flow (success) or ``login_timeout`` expires. On
        success we re-navigate to nav_url — the user may have landed on a
        dashboard rather than the target — so the capture poll can proceed.

        If the caller sets ``stop_event`` mid-wait (e.g. the interceptor's
        capture window elapsed), we return with status left at ``waiting_login``
        — that's what surfaces as ``login_wall=true``. On timeout we raise
        ``TimeoutError``, matching the sentinel-expiry contract
        ``InterceptorClient._loop`` relies on.
        """
        on_status("waiting_login", None)
        logger.debug("cdp_session: login wall detected — awaiting authentication")
        deadline = time.time() + login_timeout
        while time.time() < deadline:
            if stop_event.is_set():
                return
            time.sleep(3)
            try:
                href = eval_str("location.href")
            except Exception:
                continue
            if not _is_login(href):
                logger.debug("cdp_session: login resolved — re-navigating to target")
                # New document incoming — reset dedup state so post-login
                # captures (whose per-document seq restarts at 1) aren't
                # mistaken for the login page's already-delivered ones.
                _delivered_seqs.clear()
                rpc("Page.addScriptToEvaluateOnNewDocument", {"source": interceptor_script})
                rpc("Page.navigate", {"url": nav_url})
                rpc("Runtime.evaluate", {"expression": interceptor_script})
                return
        raise TimeoutError(
            f"Login timed out ({login_timeout // 60} min) — retry launch"
        )

    def _navigate_and_capture(nav_url: str) -> dict:
        """Initial data capture: pre-register the interceptor, navigate to
        the target URL, then poll window._capturedResponses until we find a
        response we can parse — or capture_timeout expires.

        This is the "get the first result" phase. After it returns, the caller
        transitions into the live loop that streams subsequent updates.
        """
        # Register the interceptor to run on every subsequent document. This
        # HAS to be done BEFORE navigation so the interceptor is installed
        # before the page's own scripts fire off their fetch/XHR calls —
        # otherwise we miss the initial requests made during page load.
        rpc("Page.addScriptToEvaluateOnNewDocument", {"source": interceptor_script})
        logger.debug("cdp_session: interceptor pre-registered for next document")

        # Decide navigate vs reload. If we're already at the target URL,
        # reload is faster and preserves any SPA state. Otherwise navigate.
        #
        # `ignoreCache=True` forces the browser to refetch from the network
        # instead of serving from the HTTP cache. Without this, page-init
        # XHRs whose responses were already cached from the initial argv-
        # driven load never get refetched on the reload — and the interceptor
        # never sees them. Bubble.io's /api/1.1/init/data is a live example.
        href = eval_str("location.href")
        if nav_url and nav_url in href:
            logger.debug("cdp_session: already at target, reloading (ignoreCache)")
            rpc("Page.reload", {"ignoreCache": True})
        else:
            logger.debug("cdp_session: navigating to %s", nav_url)
            rpc("Page.navigate", {"url": nav_url})

        # Also inject into whatever is currently loaded. addScriptToEvaluateOn
        # NewDocument only affects future navigations; this evaluate patches
        # the interceptor into the current page too, in case navigation is
        # a no-op (e.g. hash-only change).
        rpc("Runtime.evaluate", {"expression": interceptor_script})

        # Poll window._capturedResponses on a fixed interval. Every capture_poll
        # seconds we read the JS array and try to parse each new entry. We
        # stop as soon as one parses successfully, or bail after capture_timeout.
        deadline = time.time() + capture_timeout
        attempt = 0
        while time.time() < deadline:
            if stop_event.is_set():
                raise RuntimeError("stopped")
            time.sleep(capture_poll)
            attempt += 1

            # Post-navigation login-wall check. By now the redirect chain (an
            # email click-tracker → app → maybe a login bounce) has had at least
            # one poll interval to settle, so location.href reflects where we
            # actually landed. If that's a login page, wait it out here instead
            # of polling fruitlessly for data that won't arrive until the user
            # authenticates. _await_login_resolved re-navigates to nav_url once
            # login clears; if it can't (headless, or the caller quits), it
            # leaves status at 'waiting_login' or raises TimeoutError.
            try:
                cur_href = eval_str("location.href")
            except Exception:
                cur_href = ""
            if cur_href and _is_login(cur_href):
                _await_login_resolved(nav_url)
                continue

            # Serialize the array to a JSON string on the JS side, then parse
            # in Python. Avoids the CDP object-handle protocol which is much
            # more work for the same result.
            raw = eval_str("JSON.stringify(window._capturedResponses || [])")
            captured = _json.loads(raw) if raw else []
            logger.debug("cdp_session: poll #%d — %d response(s)", attempt, len(captured))

            result = _find_initial(captured)
            if result:
                logger.debug("cdp_session: parsed usable data on poll #%d", attempt)
                return result

        # Ran out of time without finding a match. Common causes: the page
        # didn't hit the expected endpoint, url_patterns is too strict, or
        # parse_fn returns None for every body. Caller reconnects and retries.
        raise RuntimeError(
            f"No matching data found after {capture_timeout}s — the endpoint "
            "may not have been called, or url_patterns/parse_fn didn't match"
        )

    # ── Main session body ─────────────────────────────────────────────────────

    try:
        # ── Step 1: login detection moved POST-navigation ─────────────────────
        # We used to check location.href here, but at this point the browser is
        # still on about:blank — the real navigation happens in Step 3 — so a
        # redirect-to-login triggered by loading the target would be missed
        # entirely on the first pass. The login-wall check now lives inside
        # _navigate_and_capture, after the target (and any redirect chain) has
        # settled; see _await_login_resolved.

        # ── Step 2: enable the CDP domains we need ────────────────────────────
        # ORDER MATTERS. These calls set up Chrome-side machinery that later
        # steps depend on. Doing them out of order works most of the time but
        # can silently drop events during navigation.
        #
        # Page.enable — required for both Page.addScriptToEvaluateOnNewDocument
        # to actually persist, and for Page.loadEventFired events to reach us
        # in the live loop.
        rpc("Page.enable")

        # Runtime.enable — required for Runtime.addBinding to inject the
        # binding function into the page's JS context. Without it, the
        # binding is registered on Chrome's side but never appears as
        # window.__cdpNotify, so interceptor.js's typeof check fails and
        # we lose the fast-path binding events (poll fallback still works).
        # Side effect: Runtime.enable causes Chrome to flood us with
        # executionContext* events — the rpc() drain loop tolerates them.
        rpc("Runtime.enable")

        # Register window.__cdpNotify as a native-backed function. When
        # interceptor.js calls it with a JSON payload, Chrome fires a
        # Runtime.bindingCalled event we catch in the live loop.
        rpc("Runtime.addBinding", {"name": "__cdpNotify"})

        # Hide navigator.webdriver so sites can't easily detect that they're
        # being automated. addScriptToEvaluateOnNewDocument means this runs
        # before the page's own scripts on every navigation.
        rpc(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": (
                    "Object.defineProperty(navigator, 'webdriver', "
                    "{get: () => undefined});"
                )
            },
        )

        # ── Step 3: initial capture ───────────────────────────────────────────
        # Navigate to the target and poll until the first parseable response
        # arrives. Blocks up to capture_timeout seconds; may raise RuntimeError.
        data = _navigate_and_capture(target_url)
        if on_data:
            on_data(data)

        # ── Step 4: persistent live-update loop ───────────────────────────────
        # Two complementary paths deliver ongoing updates:
        #
        #   FAST PATH — Runtime.bindingCalled events. When interceptor.js
        #     captures a response, it calls window.__cdpNotify(payload) which
        #     Chrome converts into a bindingCalled event on our WebSocket.
        #     Near-zero latency but not 100% reliable — Chrome doesn't
        #     guarantee the binding is injected before addScriptToEvaluate
        #     OnNewDocument scripts run, so very early API calls may miss it.
        #
        #   RELIABLE FALLBACK — polling window._capturedResponses every 5s.
        #     Whatever the binding missed will still be sitting in the JS
        #     array. Both paths dedup through _delivered_seqs (keyed by the
        #     per-document seq interceptor.js stamps on each capture), so an
        #     item delivered by one path is a no-op on the other and nothing
        #     is processed twice.
        #
        # We do both. The binding gives snappy updates; the poll guarantees
        # nothing gets dropped.
        logger.debug("cdp_session: entering live-event loop")

        def _send(method: str, params: Optional[dict] = None) -> None:
            """Fire-and-forget CDP command. Unlike rpc(), doesn't wait for a
            response — safe to use inside the event loop where we can't
            block reading unrelated messages."""
            _id[0] += 1
            ws.send(_json.dumps({"id": _id[0], "method": method, "params": params or {}}))

        def _poll_captured() -> None:
            """Read window._capturedResponses and deliver any entries not yet
            in ``_delivered_seqs``. Idempotent — a poll with no new seqs is a
            no-op.

            We re-scan the full array every tick rather than slicing by a
            high-water index: a dropped binding event leaves a "hole" (an
            appended-but-undelivered entry) that a high-water index would jump
            past permanently. Deduping by seq makes the re-scan cheap in effect
            (already-delivered seqs return immediately) and, crucially, correct
            — holes get filled on the next tick. The array is bounded per
            document: a new page load resets it (and clears _delivered_seqs).
            """
            try:
                raw = eval_str("JSON.stringify(window._capturedResponses || [])")
                captured = _json.loads(raw) if raw else []
            except Exception:
                # Transient eval failures (e.g. during navigation) — skip
                # this tick, next one will pick up whatever's there.
                return
            for item in captured:
                if _handle_item(item, deliver_on_data=True) is not None:
                    logger.debug("cdp_session: live update via poll")

        # No index to prime: every entry the initial-capture phase delivered is
        # already recorded in _delivered_seqs, so the first poll naturally skips
        # them and only picks up genuinely new captures.

        # 5-second read timeout on the socket. Each timeout is our chance to:
        #   1. check stop_event and exit if the caller asked to quit
        #   2. check reload_event and navigate if a refresh was requested
        #   3. poll _capturedResponses for anything the binding missed
        ws.settimeout(5)

        while not stop_event.is_set():
            # ── Reload handling ──
            # Caller sets reload_event to ask us to re-navigate (e.g. widget's
            # "Refresh Now" button). We clear it, reset the poll index (fresh
            # document = fresh array), and re-inject the interceptor so it's
            # ready for the reloaded page's requests.
            if reload_event.is_set():
                reload_event.clear()
                # New document ⇒ interceptor.js's per-document seq restarts at
                # 1, so clear the delivered set to avoid colliding with stale
                # seqs from the previous page.
                _delivered_seqs.clear()
                logger.debug("cdp_session: reload requested — navigating")
                _send("Page.addScriptToEvaluateOnNewDocument", {"source": interceptor_script})
                _send("Page.navigate", {"url": target_url})

            # ── Read one event from the socket ──
            try:
                msg = _json.loads(ws.recv())
            except _ws_mod.WebSocketTimeoutException:
                # 5s went by with no events. This is our keep-alive tick —
                # poll _capturedResponses to catch anything the binding may
                # have missed, then loop back to check stop/reload state.
                _poll_captured()
                continue

            method = msg.get("method", "")

            # ── Page.loadEventFired ──
            # A new document finished loading (SPA route change with pushState
            # counts too). We need to re-register the binding (page context
            # was destroyed and rebuilt) and re-inject the interceptor. Reset
            # the poll index because the new document has an empty
            # _capturedResponses array.
            if method == "Page.loadEventFired":
                logger.debug("cdp_session: page loaded — re-registering binding/interceptor")
                # Fresh document ⇒ _capturedResponses is empty and seq restarts
                # at 1; drop delivered seqs so the new page's captures aren't
                # mistaken for already-seen ones.
                _delivered_seqs.clear()
                _send("Runtime.addBinding", {"name": "__cdpNotify"})
                _send("Runtime.evaluate", {"expression": interceptor_script})
                continue

            # ── Runtime.bindingCalled — the fast-path event ──
            # interceptor.js called window.__cdpNotify(JSON.stringify({url, body}));
            # Chrome delivered that payload here as an event. Parse, process,
            # deliver to on_data.
            if (
                method == "Runtime.bindingCalled"
                and msg.get("params", {}).get("name") == "__cdpNotify"
            ):
                try:
                    payload = _json.loads(msg["params"].get("payload", "{}"))
                    # payload carries the same seq as the JS-array entry, so
                    # _handle_item dedups this delivery against the poll
                    # fallback — whichever path arrives first wins, the other
                    # is a no-op. No index bookkeeping: the seq set is the
                    # single source of truth for what's been delivered.
                    if _handle_item(payload, deliver_on_data=True) is not None:
                        logger.debug("cdp_session: live update via binding")
                except Exception as exc:
                    logger.warning("cdp_session: error processing binding event: %s", exc)

            # Any other CDP event (executionContextCreated, consoleAPICalled,
            # etc.) is silently ignored — we only care about page loads and
            # our own binding.

    finally:
        # Always close the socket, even if we hit an exception on the way out.
        # Chrome cleans up its side once the connection drops.
        try:
            ws.close()
        except Exception:
            pass
