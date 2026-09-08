"""Tests for common.cdp_interceptor.screenshot.

No browser is launched. The pure helpers (tab selection, clip construction)
are tested directly; ``capture_screenshot`` is exercised end-to-end against a
fake WebSocket that answers the three CDP calls it makes.
"""

from __future__ import annotations

import base64
import json

import pytest

from common.cdp_interceptor import screenshot as shot_mod
from common.cdp_interceptor.screenshot import (
    CHROME_MAX_CLIP_PX,
    ScreenshotError,
    build_capture_params,
    capture_screenshot,
    pick_page_tab,
)


# ── pick_page_tab ─────────────────────────────────────────────────────────────

def test_pick_page_tab_skips_non_page_targets():
    tabs = [
        {"type": "service_worker", "url": "https://x/sw.js"},
        {"type": "page", "url": "https://x/app"},
    ]
    assert pick_page_tab(tabs)["url"] == "https://x/app"


def test_pick_page_tab_prefers_hint_then_falls_back():
    tabs = [
        {"type": "page", "url": "about:blank"},
        {"type": "page", "url": "https://roofix.io/project/1"},
    ]
    assert pick_page_tab(tabs, "roofix.io")["url"] == "https://roofix.io/project/1"
    assert pick_page_tab(tabs, "nomatch")["url"] == "about:blank"


def test_pick_page_tab_none_when_no_pages():
    assert pick_page_tab([{"type": "background_page", "url": "x"}]) is None
    assert pick_page_tab([]) is None


# ── build_capture_params ─────────────────────────────────────────────────────

_METRICS = {
    "cssContentSize": {"x": 0, "y": 0, "width": 1920, "height": 12000},
    "cssLayoutViewport": {"pageX": 0, "pageY": 0, "clientWidth": 1920, "clientHeight": 1080},
    "cssVisualViewport": {"pageX": 0, "pageY": 600, "clientWidth": 1920, "clientHeight": 1080},
}


def test_viewport_clip_uses_layout_viewport_and_scroll_offset():
    params, w, h = build_capture_params(
        _METRICS, format="jpeg", quality=70, full_page=False, scale=1.0, max_height=8000
    )
    assert params["captureBeyondViewport"] is False
    assert params["clip"] == {"x": 0.0, "y": 600.0, "width": 1920, "height": 1080, "scale": 1.0}
    assert params["quality"] == 70
    assert (w, h) == (1920, 1080)


def test_full_page_clip_is_clamped_to_max_height():
    params, w, h = build_capture_params(
        _METRICS, format="png", quality=80, full_page=True, scale=1.0, max_height=8000
    )
    assert params["captureBeyondViewport"] is True
    assert params["clip"]["height"] == 8000
    assert params["clip"]["width"] == 1920
    assert params["clip"]["x"] == 0.0 and params["clip"]["y"] == 0.0
    assert (w, h) == (1920, 8000)


def test_full_page_never_exceeds_chrome_limit_even_if_max_height_is_higher():
    tall = {**_METRICS, "cssContentSize": {"width": 800, "height": 50000}}
    params, _, h = build_capture_params(
        tall, format="jpeg", quality=80, full_page=True, scale=1.0, max_height=999999
    )
    assert params["clip"]["height"] == CHROME_MAX_CLIP_PX
    assert h == CHROME_MAX_CLIP_PX


def test_png_omits_quality():
    params, _, _ = build_capture_params(
        _METRICS, format="png", quality=50, full_page=False, scale=1.0, max_height=8000
    )
    assert "quality" not in params


def test_scale_shrinks_reported_output_dimensions():
    params, w, h = build_capture_params(
        _METRICS, format="webp", quality=80, full_page=False, scale=0.5, max_height=8000
    )
    assert params["clip"]["scale"] == 0.5
    assert (w, h) == (960, 540)


def test_falls_back_to_device_pixel_metrics_on_old_chrome():
    old = {
        "contentSize": {"width": 1000, "height": 3000},
        "layoutViewport": {"clientWidth": 1000, "clientHeight": 700},
        "visualViewport": {"pageX": 0, "pageY": 0, "clientWidth": 1000, "clientHeight": 700},
    }
    params, w, h = build_capture_params(
        old, format="jpeg", quality=80, full_page=True, scale=1.0, max_height=8000
    )
    assert params["clip"]["height"] == 3000
    assert (w, h) == (1000, 3000)


def test_rejects_unknown_format_and_empty_page():
    with pytest.raises(ScreenshotError):
        build_capture_params(
            _METRICS, format="gif", quality=80, full_page=False, scale=1.0, max_height=8000
        )
    with pytest.raises(ScreenshotError):
        build_capture_params({}, format="jpeg", quality=80, full_page=False, scale=1.0, max_height=8000)


# ── capture_screenshot against a fake CDP socket ─────────────────────────────

class _FakeWs:
    """Answers the CDP methods capture_screenshot issues, in order of arrival.

    Also interleaves an unsolicited event before every response so the reader
    loop's "skip anything that isn't my id" path is exercised.
    """

    def __init__(self, image: bytes, ready_states=("loading", "complete"), fail_capture=False):
        self._image = image
        self._ready = list(ready_states)
        self._fail_capture = fail_capture
        self._queue: list[str] = []
        self.sent: list[dict] = []
        self.closed = False

    def settimeout(self, _t):
        pass

    def send(self, raw: str):
        msg = json.loads(raw)
        self.sent.append(msg)
        self._queue.append(json.dumps({"method": "Runtime.consoleAPICalled", "params": {}}))
        method = msg["method"]
        if method == "Runtime.evaluate":
            state = self._ready.pop(0) if len(self._ready) > 1 else self._ready[0]
            result = {"result": {"type": "string", "value": state}}
        elif method == "Page.getLayoutMetrics":
            result = _METRICS
        elif method == "Page.captureScreenshot":
            if self._fail_capture:
                self._queue.append(json.dumps({
                    "id": msg["id"], "error": {"code": -32000, "message": "Unable to capture screenshot"}
                }))
                return
            result = {"data": base64.b64encode(self._image).decode()}
        else:
            result = {}
        self._queue.append(json.dumps({"id": msg["id"], "result": result}))

    def recv(self) -> str:
        return self._queue.pop(0)

    def close(self):
        self.closed = True


def _patch_chrome(monkeypatch, ws: _FakeWs, tabs=None):
    tabs = tabs if tabs is not None else [
        {"type": "page", "url": "https://example.com/final", "webSocketDebuggerUrl": "ws://fake"}
    ]
    monkeypatch.setattr(shot_mod, "_list_tabs", lambda port, timeout: tabs)
    monkeypatch.setattr(shot_mod, "_open_ws", lambda url, timeout: ws)


def test_capture_screenshot_end_to_end(monkeypatch):
    ws = _FakeWs(b"\xff\xd8JPEGBYTES")
    _patch_chrome(monkeypatch, ws)

    shot = capture_screenshot(9224, format="jpeg", quality=75, settle_timeout=2.0)

    assert shot.data == b"\xff\xd8JPEGBYTES"
    assert shot.format == "jpeg" and shot.mime_type == "image/jpeg"
    assert (shot.width, shot.height) == (1920, 1080)
    assert shot.full_page is False
    assert shot.page_url == "https://example.com/final"
    assert shot.to_base64() == base64.b64encode(b"\xff\xd8JPEGBYTES").decode()
    assert ws.closed

    methods = [m["method"] for m in ws.sent]
    # readyState polled until "complete" (2 probes), then metrics, then capture.
    assert methods == [
        "Runtime.evaluate", "Runtime.evaluate", "Page.getLayoutMetrics", "Page.captureScreenshot"
    ]
    cap = ws.sent[-1]["params"]
    assert cap["quality"] == 75 and cap["format"] == "jpeg"


def test_capture_screenshot_surfaces_cdp_error(monkeypatch):
    ws = _FakeWs(b"", ready_states=("complete",), fail_capture=True)
    _patch_chrome(monkeypatch, ws)
    with pytest.raises(ScreenshotError, match="Unable to capture screenshot"):
        capture_screenshot(9224)
    assert ws.closed


def test_capture_screenshot_no_page_tab(monkeypatch):
    ws = _FakeWs(b"x")
    _patch_chrome(monkeypatch, ws, tabs=[{"type": "service_worker", "url": "x"}])
    with pytest.raises(ScreenshotError, match="no page tab"):
        capture_screenshot(9224)


def test_capture_screenshot_unreachable_chrome(monkeypatch):
    def boom(port, timeout):
        raise ConnectionError("refused")

    monkeypatch.setattr(shot_mod, "_list_tabs", boom)
    with pytest.raises(ScreenshotError, match="cannot reach Chrome"):
        capture_screenshot(9224)
