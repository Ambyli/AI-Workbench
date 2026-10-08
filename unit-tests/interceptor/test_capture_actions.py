"""Tests for the interceptor service's page_script / actions / stop_when_matched.

No browser is launched: ``InterceptorClient`` is replaced with a fake that
"captures" on a timer and returns a canned ``ActionsReport``, which is enough
to exercise request validation, the capture-window loop, and the response
shape. The CDP side of actions is covered by
``shared/common/tests/test_cdp_actions.py``.

Run from the repo root:

    .venv/Scripts/python.exe -m pytest unit-tests/interceptor -q
"""

from __future__ import annotations

import os
import sys
import threading
import time

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "ai", "interceptor"))


@pytest.fixture(scope="module")
def app_mod(tmp_path_factory):
    # profiles.PROFILES_ROOT is read at import; point it somewhere disposable.
    os.environ["INTERCEPTOR_PROFILES_ROOT"] = str(tmp_path_factory.mktemp("profiles"))
    import app  # noqa: E402

    return app


@pytest.fixture()
def client(app_mod):
    from fastapi.testclient import TestClient

    # No `with`: the lifespan (temp-profile sweep + MCP) is not needed here.
    return TestClient(app_mod.app)


def _pool_untouched(client, app_mod):
    jobs = client.get("/jobs").json()
    assert jobs["active_count"] == 0
    assert jobs["available"] == app_mod.MAX_CONCURRENT
    assert app_mod._port_pool.qsize() == app_mod.MAX_CONCURRENT


BASE = {"url": "https://example.com/feoc", "url_patterns": ["api/apex/execute"], "profile": "enphase"}


# ── Validation: bad steps never take a port ─────────────────────────────────

def test_unknown_action_type_is_422_without_a_port(client, app_mod):
    r = client.post("/capture", json={**BASE, "actions": [{"type": "hover", "selector": "a"}]})
    assert r.status_code == 422
    _pool_untouched(client, app_mod)


def test_missing_action_field_is_422(client, app_mod):
    r = client.post("/capture", json={**BASE, "actions": [{"type": "fill", "selector": "input"}]})
    assert r.status_code == 422
    assert any("value" in e["loc"] for e in r.json()["detail"])
    _pool_untouched(client, app_mod)


def test_unknown_press_key_is_400_from_the_library_without_a_port(client, app_mod):
    r = client.post("/capture", json={**BASE, "actions": [{"type": "press", "key": "NotAKey"}]})
    assert r.status_code == 400
    assert "invalid action: actions[0] (press)" in r.json()["detail"]
    _pool_untouched(client, app_mod)


def test_stop_when_matched_needs_patterns(client, app_mod):
    r = client.post("/capture", json={
        "url": "https://example.com", "url_patterns": [], "profile": "x",
        "actions": [{"type": "screenshot"}], "stop_when_matched": True,
    })
    assert r.status_code == 422
    assert "stop_when_matched" in r.text
    _pool_untouched(client, app_mod)


def test_screenshot_endpoint_is_gone(client, app_mod):
    r = client.post("/screenshot", json={"url": "https://example.com", "profile": "x"})
    assert r.status_code == 404
    assert not hasattr(app_mod, "screenshot_url")


def test_empty_patterns_need_a_screenshot_action(client, app_mod):
    r = client.post("/capture", json={
        "url": "https://example.com", "url_patterns": [], "profile": "x",
        "actions": [{"type": "wait", "seconds": 0}],
    })
    assert r.status_code == 422
    assert "screenshot" in r.text and "url_patterns" in r.text
    _pool_untouched(client, app_mod)


def test_more_than_ten_screenshots_is_422(client, app_mod):
    r = client.post("/capture", json={
        **BASE, "actions": [{"type": "screenshot", "scale": 0.5}] * 11,
    })
    assert r.status_code == 422
    assert "at most 10 screenshot steps" in r.text
    _pool_untouched(client, app_mod)


def test_bad_screenshot_options_are_422(client, app_mod):
    for bad in ({"scale": 3}, {"format": "gif"}, {"quality": 0}, {"max_height": 50},
                {"selector": "body"}):
        r = client.post("/capture", json={**BASE, "actions": [{"type": "screenshot", **bad}]})
        assert r.status_code == 422, bad
    _pool_untouched(client, app_mod)


def test_mcp_capture_url_returns_bad_actions_in_the_payload(app_mod):
    res = app_mod.capture_url(
        url="https://example.com", url_patterns=["x"], profile="p",
        actions=[{"type": "click"}],
    )
    payload = res.structured_content
    assert payload["status"] == "error"
    assert "actions.0.click.selector" in payload["error"]
    assert payload["actions_report"] is None and payload["ended_early"] is False
    assert app_mod._port_pool.qsize() == app_mod.MAX_CONCURRENT


def test_mcp_tools_pass_login_url_patterns_through(app_mod, monkeypatch):
    seen: list = []

    def _stop(req):
        seen.append(req)
        raise app_mod.HTTPException(status_code=429, detail="stop here")

    monkeypatch.setattr(app_mod, "_run_capture", _stop)
    sso = ["login", r"sso\.enphaseenergy\.com"]

    app_mod.capture_url(url="https://e.com", url_patterns=["x"], profile="p",
                        login_url_patterns=sso, actions_ready_timeout_seconds=45)
    app_mod.capture_url(url="https://e.com", url_patterns=["x"], profile="p")
    assert seen[0].login_url_patterns == sso
    assert seen[0].actions_ready_timeout_seconds == 45
    # Omitted → the model's defaults, not an empty list (which disables detection).
    assert seen[1].login_url_patterns == ["login", "signin", "/auth"]

    # url_patterns may be omitted when an action is a screenshot.
    app_mod.capture_url(url="https://e.com", profile="p", actions=[{"type": "screenshot"}])
    assert seen[2].url_patterns == [] and seen[2].actions[0].type == "screenshot"


def test_mcp_capture_url_without_patterns_or_screenshot_is_an_error_payload(app_mod):
    res = app_mod.capture_url(url="https://example.com", profile="p")
    payload = res.structured_content
    assert payload["status"] == "error" and "screenshot" in payload["error"]
    assert payload["matches"] == {}
    assert "screenshot_error" not in payload and "screenshot" not in payload


# ── The capture loop, against a fake InterceptorClient ──────────────────────

class _FakeClient:
    """Stands in for InterceptorClient: launch() 'captures' one matching body
    after ``capture_after`` seconds; run_actions() takes ``actions_take`` s."""

    capture_after = 0.3
    actions_take = 0.1
    instances: list["_FakeClient"] = []

    def __init__(self, *, on_capture, on_status, **kw):
        self._on_capture = on_capture
        self.kwargs = kw  # everything else the service passed (login_actions, …)
        self.quit_called = False
        self.actions_called_with = None
        _FakeClient.instances.append(self)

    def launch(self, target_url):
        from common.cdp_interceptor import Capture

        def fire():
            time.sleep(self.capture_after)
            self._on_capture(Capture(url="https://example.com/webruntime/api/apex/execute?x=1",
                                     body={"returnValue": {"ok": True}}))

        threading.Thread(target=fire, daemon=True).start()

    def run_actions(self, actions, **kw):
        from common.cdp_interceptor import ActionResult, ActionsReport

        self.actions_called_with = (actions, kw)
        time.sleep(self.actions_take)
        return ActionsReport(
            page_script=None,
            actions=[ActionResult(index=i, type=a.type, ok=True, elapsed_ms=1) for i, a in enumerate(actions)],
        )

    def get_state(self):
        from common.cdp_interceptor import ClientState

        return ClientState(status="ok", headless=True, error=None, last_capture_at=None)

    def get_login_report(self):
        return None  # no login wall in these tests

    def quit(self):
        self.quit_called = True


@pytest.fixture()
def fake_client(app_mod, monkeypatch):
    _FakeClient.instances = []
    monkeypatch.setattr(app_mod, "InterceptorClient", _FakeClient)
    return _FakeClient


def test_stop_when_matched_ends_early_after_actions(client, app_mod, fake_client):
    t0 = time.monotonic()
    r = client.post("/capture", json={
        **BASE,
        "capture_window_seconds": 30,
        "stop_when_matched": True,
        "actions": [
            {"type": "fill", "selector": "lightning-input", "value": "532614044013"},
            {"type": "click", "selector": "button", "text": "Submit"},
        ],
    })
    elapsed = time.monotonic() - t0

    assert r.status_code == 200, r.text
    body = r.json()
    assert elapsed < 5
    assert body["ended_early"] is True
    assert len(body["matches"]["api/apex/execute"]) == 1
    rep = body["actions_report"]
    assert rep["aborted_reason"] is None
    assert [(a["index"], a["type"], a["ok"]) for a in rep["actions"]] == [(0, "fill", True), (1, "click", True)]
    actions, kw = fake_client.instances[0].actions_called_with
    assert [a.value for a in actions if a.type == "fill"] == ["532614044013"]
    assert kw["ready_timeout_s"] == 30.0  # defaults to the capture window
    assert fake_client.instances[0].quit_called
    _pool_untouched(client, app_mod)


def test_stop_when_matched_waits_for_actions_to_finish(client, app_mod, fake_client, monkeypatch):
    # The match lands at 0.3s but the actions take 1.2s: the window must not
    # end before the actions are done.
    monkeypatch.setattr(fake_client, "actions_take", 1.2)
    t0 = time.monotonic()
    r = client.post("/capture", json={
        **BASE, "capture_window_seconds": 30, "stop_when_matched": True,
        "actions": [{"type": "wait", "seconds": 1}],
    })
    elapsed = time.monotonic() - t0
    assert r.json()["ended_early"] is True
    assert 1.2 <= elapsed < 5


def test_without_actions_the_response_is_unchanged(client, app_mod, fake_client):
    t0 = time.monotonic()
    r = client.post("/capture", json={**BASE, "capture_window_seconds": 1})
    elapsed = time.monotonic() - t0

    body = r.json()
    assert r.status_code == 200
    assert elapsed >= 1.0  # no stop_when_matched → the full window
    assert body["ended_early"] is False
    assert body["actions_report"] is None
    assert body["login_actions_report"] is None
    assert fake_client.instances[0].actions_called_with is None
    # No login_actions → nothing login-related handed to the client.
    kw = fake_client.instances[0].kwargs
    assert kw["login_actions"] == [] and kw["login_fill_origins"] == () and kw["login_lock"] is None
    assert len(body["matches"]["api/apex/execute"]) == 1
    _pool_untouched(client, app_mod)


JPEG = b"\xff\xd8\xff\xe0fake-jpeg"
PNG = b"\x89PNG\r\n\x1a\nfake-png"


def _shot(fmt: str, data: bytes, url: str) -> dict:
    import base64

    return {"format": fmt, "mime_type": f"image/{fmt}", "width": 960, "height": 540,
            "full_page": False, "bytes": len(data), "page_url": url,
            "data_base64": base64.b64encode(data).decode()}


def _screenshot_run(self, actions, **kw):
    """run_actions stand-in: screenshot steps return an image (the one at
    index 1 fails), every other step succeeds."""
    from common.cdp_interceptor import ActionResult, ActionsReport

    self.actions_called_with = (actions, kw)
    out = []
    for i, a in enumerate(actions):
        if a.type != "screenshot":
            out.append(ActionResult(index=i, type=a.type, ok=True, elapsed_ms=1))
        elif i == 1:
            out.append(ActionResult(index=i, type="screenshot", ok=False, elapsed_ms=1,
                                    error="Page.captureScreenshot failed: boom (code -32000)"))
        else:
            data = PNG if a.format == "png" else JPEG
            out.append(ActionResult(index=i, type="screenshot", ok=True, elapsed_ms=1,
                                    value=_shot(a.format, data, f"https://example.com/step{i}")))
    return ActionsReport(actions=out)


def test_screenshot_only_capture_ends_when_the_actions_finish(client, app_mod, fake_client, monkeypatch):
    monkeypatch.setattr(fake_client, "run_actions", _screenshot_run)
    t0 = time.monotonic()
    r = client.post("/capture", json={
        "url": "https://example.com", "profile": "x", "capture_window_seconds": 30,
        "actions": [{"type": "screenshot"}],
    })
    elapsed = time.monotonic() - t0

    assert r.status_code == 200, r.text
    body = r.json()
    assert elapsed < 5  # not the 30 s window
    assert body["ended_early"] is True and body["matches"] == {}
    assert "screenshot" not in body and "screenshot_error" not in body
    step = body["actions_report"]["actions"][0]
    assert step["ok"] and step["value"]["data_base64"] and step["value"]["page_url"].endswith("step0")
    _pool_untouched(client, app_mod)


def test_screenshot_steps_reach_the_library_with_their_options(client, app_mod, fake_client, capfd):
    r = client.post("/capture", json={
        **BASE, "capture_window_seconds": 1,
        "actions": [{"type": "screenshot", "format": "png", "full_page": True, "scale": 0.5},
                    {"type": "click", "selector": "button"},
                    {"type": "screenshot"}],
    })
    assert r.status_code == 200, r.text
    actions, _ = fake_client.instances[0].actions_called_with
    first, _, last = actions
    assert (first.format, first.full_page, first.scale, first.timeout_s) == ("png", True, 0.5, 30.0)
    assert (last.format, last.quality, last.timeout_s) == ("jpeg", 80, 30.0)
    assert "screenshots=2 taken, 0 failed" in capfd.readouterr().err


def test_mcp_capture_url_returns_labelled_images_in_step_order(app_mod, fake_client, monkeypatch, capfd):
    import base64
    import json as _json

    monkeypatch.setattr(fake_client, "run_actions", _screenshot_run)
    res = app_mod.capture_url(
        url="https://example.com", profile="p",
        actions=[{"type": "screenshot", "scale": 0.5},
                 {"type": "screenshot"},  # fails
                 {"type": "wait", "seconds": 0},
                 {"type": "screenshot", "format": "png"}],
    )

    kinds = [(c.type, getattr(c, "text", None)) for c in res.content]
    assert [k for k, _ in kinds] == ["text", "text", "image", "text", "image"]
    assert kinds[1][1] == "actions[0] screenshot — https://example.com/step0, 960x540"
    assert kinds[3][1] == "actions[3] screenshot — https://example.com/step3, 960x540"
    assert res.content[2].mimeType == "image/jpeg" and base64.b64decode(res.content[2].data) == JPEG
    assert res.content[4].mimeType == "image/png" and base64.b64decode(res.content[4].data) == PNG
    # The JSON keeps the metadata but never the base64.
    for doc in (res.content[0].text, _json.dumps(res.structured_content)):
        assert "data_base64" not in doc
    steps = res.structured_content["actions_report"]["actions"]
    assert steps[0]["value"]["bytes"] == len(JPEG) and steps[0]["value"]["format"] == "jpeg"
    assert not steps[1]["ok"] and "boom" in steps[1]["error"]
    assert res.structured_content["ended_early"] is True
    # And nothing image-sized in the job log either.
    err = capfd.readouterr().err
    assert "screenshots=2 taken, 1 failed" in err
    assert base64.b64encode(JPEG).decode() not in err


def test_page_script_alone_runs_and_window_end_relabels_cancel(client, app_mod, fake_client, monkeypatch):
    # Actions still running when a 1s window ends: the library sees the
    # window's cancel and says "cancelled"; the service relabels it.
    from common.cdp_interceptor import ActionsReport

    def slow_actions(self, actions, *, cancel, **kw):
        self.actions_called_with = (actions, {"cancel": cancel, **kw})
        while not cancel():
            time.sleep(0.02)
        return ActionsReport.not_run(actions, kw.get("page_script"), "cancelled")

    monkeypatch.setattr(fake_client, "run_actions", slow_actions)
    r = client.post("/capture", json={
        **BASE, "capture_window_seconds": 1, "page_script": "document.title",
    })
    rep = r.json()["actions_report"]
    assert rep["aborted_reason"] == "capture window ended before actions finished"
    assert rep["page_script"]["error"] == "skipped"
    assert r.json()["ended_early"] is False
