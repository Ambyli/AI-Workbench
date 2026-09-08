"""Screenshot capture over a *second* CDP connection to a running Chrome.

``run_session`` (cdp_session.py) owns the primary WebSocket to the target tab
and runs it from a private worker thread, so nothing outside that thread can
issue CDP commands on it. Chrome allows many debugger clients per target, and
``Page.captureScreenshot`` is stateless (no ``Page.enable`` needed), so the
simplest robust design is to open an independent, short-lived WebSocket to the
same tab, take the shot, and close it. The interceptor session never notices.

Public API
----------
- ``capture_screenshot(debug_port, ...)`` — connect, wait for the document to
  settle, capture, return a ``Screenshot``.
- ``Screenshot`` — dataclass with the encoded bytes plus what was captured.
- ``ScreenshotError`` — raised on any failure (no tab, timeout, Chrome refused).

Sizing notes
------------
- ``format="jpeg"`` at ``quality=80`` is the default because a 1920×1080
  viewport comes out around 100–300 KB; the same frame as PNG is often 1–3 MB,
  which matters when the bytes are base64'd into an LLM tool result.
- ``full_page=True`` uses ``captureBeyondViewport`` with a clip covering the
  whole document. Chrome refuses very tall clips (GPU texture limits), so the
  height is clamped to ``max_height`` (default 8000 CSS px).
- ``scale`` multiplies the clip on the way out — ``0.5`` halves both axes and
  roughly quarters the payload.
"""

from __future__ import annotations

import base64
import json as _json
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger("cdp_interceptor")

SCREENSHOT_FORMATS: tuple[str, ...] = ("jpeg", "png", "webp")

# Chrome's practical ceiling for a single captureScreenshot clip. Above this
# the call fails with an opaque protocol error, so we clamp rather than guess.
CHROME_MAX_CLIP_PX = 16384


class ScreenshotError(RuntimeError):
    """Raised when a screenshot cannot be taken (no tab, timeout, CDP error)."""


@dataclass
class Screenshot:
    """One captured image.

    ``width`` / ``height`` are the *requested* output dimensions (CSS clip ×
    ``scale``, rounded) — we don't decode the image to read them back. Chrome's
    own rounding can differ by a pixel.
    """
    data: bytes
    format: str          # "jpeg" | "png" | "webp"
    width: int
    height: int
    full_page: bool
    page_url: str        # tab URL at capture time (after any redirects)

    @property
    def mime_type(self) -> str:
        return f"image/{self.format}"

    def to_base64(self) -> str:
        return base64.b64encode(self.data).decode("ascii")


# ── Pure helpers (unit-tested without a browser) ─────────────────────────────

def pick_page_tab(tabs: list[dict], url_hint: str = "") -> Optional[dict]:
    """Choose the tab to screenshot from Chrome's ``/json`` list.

    Same rule as ``run_session``: prefer a ``type=="page"`` tab whose URL
    contains ``url_hint``; otherwise the first page tab; ``None`` if there is
    no page tab at all (only service workers / extensions / etc.).
    """
    pages = [t for t in tabs if t.get("type") == "page"]
    if url_hint:
        for t in pages:
            if url_hint in t.get("url", ""):
                return t
    return pages[0] if pages else None


def build_capture_params(
    metrics: dict,
    *,
    format: str,
    quality: int,
    full_page: bool,
    scale: float,
    max_height: int,
) -> tuple[dict, int, int]:
    """Turn ``Page.getLayoutMetrics`` output into ``Page.captureScreenshot``
    params. Returns ``(params, out_width, out_height)``.

    Newer Chrome reports CSS-pixel sizes under ``cssContentSize`` /
    ``cssLayoutViewport`` / ``cssVisualViewport``; older builds only have the
    device-pixel ``contentSize`` / ``layoutViewport`` / ``visualViewport``. We
    prefer the CSS variants and fall back.
    """
    if format not in SCREENSHOT_FORMATS:
        raise ScreenshotError(
            f"unsupported format {format!r} — use one of {SCREENSHOT_FORMATS}"
        )
    if scale <= 0:
        raise ScreenshotError("scale must be > 0")

    content = metrics.get("cssContentSize") or metrics.get("contentSize") or {}
    layout = metrics.get("cssLayoutViewport") or metrics.get("layoutViewport") or {}
    visual = metrics.get("cssVisualViewport") or metrics.get("visualViewport") or {}

    if full_page:
        width = int(content.get("width") or layout.get("clientWidth") or 0)
        height = int(content.get("height") or layout.get("clientHeight") or 0)
        limit = min(max_height, CHROME_MAX_CLIP_PX)
        height = min(height, limit)
        x, y = 0.0, 0.0
    else:
        width = int(layout.get("clientWidth") or visual.get("clientWidth") or 0)
        height = int(layout.get("clientHeight") or visual.get("clientHeight") or 0)
        # Capture what the user would see right now — honour the scroll offset.
        x = float(visual.get("pageX") or 0)
        y = float(visual.get("pageY") or 0)

    if width <= 0 or height <= 0:
        raise ScreenshotError(
            f"layout metrics reported an empty page ({width}x{height}) — "
            "the tab may still be on about:blank"
        )

    width = min(width, CHROME_MAX_CLIP_PX)

    params: dict[str, Any] = {
        "format": format,
        "captureBeyondViewport": bool(full_page),
        "clip": {
            "x": x,
            "y": y,
            "width": width,
            "height": height,
            "scale": scale,
        },
    }
    # Chrome rejects `quality` for PNG (lossless) — only send it where it means something.
    if format in ("jpeg", "webp"):
        params["quality"] = int(quality)

    return params, max(1, round(width * scale)), max(1, round(height * scale))


# ── Chrome plumbing (monkeypatched in tests) ─────────────────────────────────

# Chrome binds the debug server to 127.0.0.1 only. On Windows ``localhost``
# resolves to ::1 first, so every connection burns a ~2s IPv6 failure before
# falling back — that's 4s of dead time per screenshot (measured). We connect
# by IPv4 literal, but Chrome's ``--remote-allow-origins=http://localhost:<port>``
# (set by launcher.start_browser) still has to see ``localhost`` in the Origin
# header, so the WebSocket handshake pins that explicitly.

def _list_tabs(debug_port: int, timeout: float) -> list[dict]:
    import requests as _req

    return _req.get(f"http://127.0.0.1:{debug_port}/json", timeout=timeout).json()


def _open_ws(ws_url: str, timeout: float):
    import websocket as _ws_mod

    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(ws_url)
    origin = f"http://localhost:{parts.port}" if parts.port else None
    if parts.hostname == "localhost":
        netloc = f"127.0.0.1:{parts.port}" if parts.port else "127.0.0.1"
        ws_url = urlunsplit(parts._replace(netloc=netloc))
    return _ws_mod.create_connection(ws_url, timeout=timeout, origin=origin)


class _Rpc:
    """Minimal request/response CDP client over one WebSocket.

    Mirrors the ``rpc`` closure in ``cdp_session.run_session`` but raises on
    timeout / protocol error instead of returning ``{}`` — a screenshot either
    happens or it doesn't, and the caller needs to know which.
    """

    def __init__(self, ws) -> None:
        self._ws = ws
        self._next_id = 0

    def call(self, method: str, params: Optional[dict] = None, *, timeout: float) -> dict:
        self._next_id += 1
        my_id = self._next_id
        self._ws.send(_json.dumps({"id": my_id, "method": method, "params": params or {}}))
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ScreenshotError(f"{method} timed out after {timeout:.0f}s")
            # Short read timeout so a flood of unrelated events can't starve us
            # past the deadline; loop until *our* id shows up.
            self._ws.settimeout(min(1.0, remaining))
            try:
                raw = self._ws.recv()
            except Exception as exc:  # WebSocketTimeoutException and friends
                if type(exc).__name__ == "WebSocketTimeoutException":
                    continue
                raise ScreenshotError(f"{method}: websocket read failed: {exc}") from exc
            try:
                msg = _json.loads(raw)
            except Exception:
                continue
            if msg.get("id") != my_id:
                continue  # unsolicited event or another client's traffic
            if "error" in msg:
                err = msg["error"]
                raise ScreenshotError(
                    f"{method} failed: {err.get('message', err)} (code {err.get('code')})"
                )
            return msg.get("result", {}) or {}


def _wait_for_ready(rpc: _Rpc, settle_timeout: float, rpc_timeout: float) -> None:
    """Poll ``document.readyState`` until ``complete`` or ``settle_timeout``
    elapses. Best-effort — a page that never finishes loading (long-poll
    beacons, etc.) still gets screenshotted once the budget is spent."""
    deadline = time.monotonic() + settle_timeout
    while time.monotonic() < deadline:
        try:
            res = rpc.call(
                "Runtime.evaluate",
                {"expression": "document.readyState", "returnByValue": True},
                timeout=rpc_timeout,
            )
        except ScreenshotError as exc:
            logger.debug("screenshot: readyState probe failed: %s", exc)
            return
        if res.get("result", {}).get("value") == "complete":
            return
        time.sleep(0.25)


def capture_screenshot(
    debug_port: int,
    *,
    format: str = "jpeg",
    quality: int = 80,
    full_page: bool = False,
    scale: float = 1.0,
    max_height: int = 8000,
    tab_url_hint: str = "",
    settle_timeout: float = 5.0,
    timeout: float = 30.0,
) -> Screenshot:
    """Take a screenshot of the page tab in the Chrome listening on ``debug_port``.

    Opens its own WebSocket (does not touch any ``run_session`` connection),
    waits up to ``settle_timeout`` seconds for ``document.readyState ==
    "complete"``, reads layout metrics, captures, and closes the socket.

    Parameters
    ----------
    format : "jpeg" | "png" | "webp"
    quality : 1–100, JPEG/WebP only (ignored for PNG).
    full_page : capture the whole document (clamped to ``max_height``) instead
        of the visible viewport.
    scale : output scale factor applied to the clip (0.5 = half size).
    max_height : full-page height clamp in CSS px (also capped at Chrome's
        16384 limit).
    tab_url_hint : prefer the page tab whose URL contains this substring.
    settle_timeout : max seconds to wait for the document to finish loading.
    timeout : per-CDP-call budget; ``Page.captureScreenshot`` on a large
        full-page clip can legitimately take several seconds.

    Raises
    ------
    ScreenshotError on any failure — the caller decides whether that is fatal.
    """
    try:
        tabs = _list_tabs(debug_port, timeout=min(5.0, timeout))
    except Exception as exc:
        raise ScreenshotError(
            f"cannot reach Chrome debug endpoint on port {debug_port}: {exc}"
        ) from exc

    tab = pick_page_tab(tabs, tab_url_hint)
    if tab is None or not tab.get("webSocketDebuggerUrl"):
        raise ScreenshotError("no page tab found in the debug-controlled Chrome")

    try:
        ws = _open_ws(tab["webSocketDebuggerUrl"], timeout=min(15.0, timeout))
    except Exception as exc:
        raise ScreenshotError(f"cannot attach to tab for screenshot: {exc}") from exc

    try:
        rpc = _Rpc(ws)
        _wait_for_ready(rpc, settle_timeout=settle_timeout, rpc_timeout=timeout)

        metrics = rpc.call("Page.getLayoutMetrics", timeout=timeout)
        params, out_w, out_h = build_capture_params(
            metrics,
            format=format,
            quality=quality,
            full_page=full_page,
            scale=scale,
            max_height=max_height,
        )
        result = rpc.call("Page.captureScreenshot", params, timeout=timeout)
        b64 = result.get("data")
        if not b64:
            raise ScreenshotError("Page.captureScreenshot returned no image data")
        try:
            data = base64.b64decode(b64)
        except Exception as exc:
            raise ScreenshotError(f"could not decode screenshot payload: {exc}") from exc

        # Re-read the URL from the tab list entry we attached to; it's current
        # as of the /json call, which is close enough for "where did we land".
        page_url = tab.get("url", "")
        logger.debug(
            "screenshot: %s %dx%d full_page=%s bytes=%d url=%s",
            format, out_w, out_h, full_page, len(data), page_url,
        )
        return Screenshot(
            data=data,
            format=format,
            width=out_w,
            height=out_h,
            full_page=full_page,
            page_url=page_url,
        )
    finally:
        try:
            ws.close()
        except Exception:
            pass


__all__ = [
    "Screenshot",
    "ScreenshotError",
    "SCREENSHOT_FORMATS",
    "capture_screenshot",
    "pick_page_tab",
    "build_capture_params",
]
