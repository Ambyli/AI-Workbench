"""Tests for common.cdp_interceptor.actions.

No browser is launched. ``parse_actions`` is tested directly; ``run_actions``
is exercised end-to-end against a fake WebSocket whose ``FakePage`` answers the
CDP calls it makes — the readiness probe, the deep-query lookup, the per-step
JS, and the ``Input.*`` events — and records everything it was sent.
"""

from __future__ import annotations

import base64
import json
import threading
import time

import pytest

from common.cdp_interceptor import actions as actions_mod
from common.cdp_interceptor.actions import (
    ActionError,
    ActionsReport,
    parse_actions,
    run_actions,
)


# ── parse_actions ─────────────────────────────────────────────────────────────

def test_parse_actions_applies_defaults():
    acts = parse_actions([
        {"type": "wait_for", "selector": "input"},
        {"type": "fill", "selector": "input", "value": "532614044013"},
        {"type": "click", "selector": "button", "text": "Submit"},
        {"type": "press", "key": "Enter"},
        {"type": "wait", "seconds": 1},
        {"type": "evaluate", "script": "1 + 1"},
    ])
    assert [a.type for a in acts] == ["wait_for", "fill", "click", "press", "wait", "evaluate"]
    assert acts[0].state == "visible" and acts[0].timeout_s == 15.0
    assert acts[1].clear is True and acts[1].value == "532614044013"
    assert acts[2].method == "mouse" and acts[2].text == "Submit"
    assert acts[4].seconds == 1.0


def test_parse_actions_ignores_none_values_like_a_model_dump():
    # A pydantic model_dump() carries every field, unset ones as None.
    [a] = parse_actions([{"type": "click", "selector": "b", "text": None, "method": None,
                          "timeout_s": None}])
    assert a.method == "mouse" and a.text is None and a.timeout_s == 15.0


@pytest.mark.parametrize("raw, msg", [
    ({"type": "hover", "selector": "a"}, "unknown action type"),
    ({"selector": "a"}, "unknown action type"),
    ({"type": "fill", "selector": "a"}, "missing required field"),
    ({"type": "click"}, "missing required field"),
    ({"type": "click", "selector": "a", "value": "x"}, "unexpected field"),
    ({"type": "click", "selector": "  "}, "non-empty string"),
    ({"type": "wait_for", "selector": "a", "state": "hidden"}, "state must be"),
    ({"type": "click", "selector": "a", "method": "touch"}, "method must be"),
    ({"type": "press", "key": "NotAKey"}, "key must be"),
    ({"type": "wait", "seconds": -1}, "seconds must be"),
    ({"type": "fill", "selector": "a", "value": 5}, "value must be a string"),
    ({"type": "fill", "selector": "a", "value": "x", "clear": "yes"}, "clear must be"),
    ({"type": "click", "selector": "a", "timeout_s": 0}, "timeout_s must be"),
    ({"type": "click", "selector": "a", "timeout_s": True}, "timeout_s must be"),
])
def test_parse_actions_rejects_bad_steps(raw, msg):
    with pytest.raises(ActionError, match=msg) as ei:
        parse_actions([{"type": "wait", "seconds": 0}, raw])
    assert "actions[1]" in str(ei.value)


def test_parse_actions_accepts_single_character_keys_and_none():
    assert parse_actions(None) == []
    [a] = parse_actions([{"type": "press", "key": "a"}])
    assert a.key == "a"


# Mirrors the interceptor service's LOGIN_ACTION_TYPES.
LOGIN_TYPES = ("wait_for", "fill", "click", "press", "select", "wait", "scroll")


def test_parse_actions_label_prefixes_errors():
    with pytest.raises(ActionError, match=r"^login_actions\[1\] \(fill\): missing required field") as ei:
        parse_actions([{"type": "wait", "seconds": 0}, {"type": "fill", "selector": "a"}],
                      label="login_actions")
    assert "actions[1]" not in str(ei.value).replace("login_actions[1]", "")


def test_parse_actions_allowed_types_refuses_the_rest():
    with pytest.raises(ActionError, match=r"login_actions\[0\]: action type 'evaluate' is not allowed") as ei:
        parse_actions([{"type": "evaluate", "script": "1"}],
                      label="login_actions", allowed_types=LOGIN_TYPES)
    assert "evaluate" not in str(ei.value).split("use one of")[1]
    # An unknown type lists only the allowed ones.
    with pytest.raises(ActionError, match="use one of wait_for, fill, click, press, select, wait, scroll$"):
        parse_actions([{"type": "hover"}], label="login_actions", allowed_types=LOGIN_TYPES)
    # And an allowed one still parses — scroll included (a sign-in button
    # below the fold); screenshot is not.
    a, s = parse_actions([{"type": "fill", "selector": "#u", "value": "${username}"},
                          {"type": "scroll", "selector": "button[type=submit]"}],
                         label="login_actions", allowed_types=LOGIN_TYPES)
    assert a.value == "${username}" and s.type == "scroll"
    with pytest.raises(ActionError, match=r"login_actions\[0\]: action type 'screenshot' is not allowed"):
        parse_actions([{"type": "screenshot"}], label="login_actions", allowed_types=LOGIN_TYPES)


def test_parse_screenshot_defaults_and_overrides():
    [d, o] = parse_actions([
        {"type": "screenshot"},
        {"type": "screenshot", "format": "png", "quality": 50, "full_page": True,
         "scale": 0.5, "max_height": 4000, "timeout_s": 60},
    ])
    assert (d.format, d.quality, d.full_page, d.scale, d.max_height) == ("jpeg", 80, False, 1.0, 8000)
    assert d.timeout_s == 30.0  # not the 15 s other steps get — a full-page capture is slow
    assert (o.format, o.quality, o.full_page, o.scale, o.max_height, o.timeout_s) == (
        "png", 50, True, 0.5, 4000, 60.0)
    # A model_dump() of the service's model carries every field.
    [n] = parse_actions([{"type": "screenshot", "format": None, "quality": None, "full_page": None,
                          "scale": None, "max_height": None, "timeout_s": None}])
    assert n.format == "jpeg" and n.timeout_s == 30.0


@pytest.mark.parametrize("extra, msg", [
    ({"format": "gif"}, "format must be one of jpeg, png, webp"),
    ({"quality": 0}, "quality must be"),
    ({"quality": 101}, "quality must be"),
    ({"quality": 80.5}, "quality must be"),
    ({"quality": True}, "quality must be"),
    ({"full_page": "yes"}, "full_page must be"),
    ({"scale": 0}, "scale must be"),
    ({"scale": 2.5}, "scale must be"),
    ({"scale": True}, "scale must be"),
    ({"scale": "1"}, "scale must be"),
    ({"max_height": 99}, "max_height must be"),
    ({"max_height": 16385}, "max_height must be"),
    ({"max_height": 800.0}, "max_height must be"),
    ({"timeout_s": 0}, "timeout_s must be"),
    ({"selector": "body"}, "unexpected field"),
])
def test_parse_screenshot_rejects_bad_options(extra, msg):
    with pytest.raises(ActionError, match=msg) as ei:
        parse_actions([{"type": "wait", "seconds": 0}, {"type": "screenshot", **extra}])
    assert "actions[1] (screenshot)" in str(ei.value)


def test_parse_scroll_forms_and_defaults():
    el, top, by, framed, nudge = parse_actions([
        {"type": "scroll", "selector": "h2", "text": "Results"},
        {"type": "scroll", "to": "top"},
        {"type": "scroll", "by": -400},
        {"type": "scroll", "selector": "h2", "block": "center", "timeout_s": 5},
        {"type": "scroll", "selector": ".list", "by": 800},
    ])
    assert (el.selector, el.text, el.to, el.by, el.block, el.timeout_s) == (
        "h2", "Results", None, None, "start", 15.0)
    assert (top.selector, top.to, top.by, top.block) == (None, "top", None, "start")
    assert (by.to, by.by) == (None, -400)
    assert (framed.block, framed.timeout_s) == ("center", 5.0)
    assert (nudge.selector, nudge.by) == (".list", 800)
    # A model_dump() of the service's model carries every field.
    [n] = parse_actions([{"type": "scroll", "selector": None, "text": None, "to": "bottom",
                          "by": None, "block": None, "timeout_s": None}])
    assert (n.to, n.block, n.timeout_s) == ("bottom", "start", 15.0)


@pytest.mark.parametrize("extra, msg", [
    ({}, "give selector, to or by"),
    ({"to": "top", "by": 10}, "to and by are mutually exclusive"),
    ({"to": "top", "block": "start"}, "block only applies to the selector-only form"),
    ({"selector": "h2", "by": 10, "block": "center"}, "block only applies"),
    ({"to": "top", "text": "Results"}, "text needs a selector"),
    ({"text": "Results"}, "text needs a selector"),
    ({"to": "middle"}, "to must be 'top' or 'bottom'"),
    ({"selector": "h2", "block": "top"}, "block must be one of start, center, end, nearest"),
    ({"by": 1.5}, "by must be an integer"),
    ({"by": 100.0}, "by must be an integer"),
    ({"by": True}, "by must be an integer"),
    ({"by": 100001}, "by must be an integer in \\[-100000, 100000\\]"),
    ({"by": -100001}, "by must be an integer"),
    ({"selector": "  "}, "non-empty string"),
    ({"to": "top", "format": "png"}, "unexpected field"),
])
def test_parse_scroll_rejects_bad_steps(extra, msg):
    with pytest.raises(ActionError, match=msg) as ei:
        parse_actions([{"type": "wait", "seconds": 0}, {"type": "scroll", **extra}])
    assert "actions[1] (scroll)" in str(ei.value)


def test_scroll_fields_are_refused_on_other_steps():
    with pytest.raises(ActionError, match="unexpected field"):
        parse_actions([{"type": "click", "selector": "b", "block": "start"}])


def test_action_repr_hides_the_value():
    [a] = parse_actions([{"type": "fill", "selector": "#p", "value": "hunter2"}])
    assert "hunter2" not in repr(a) and "#p" in repr(a)


# ── Fake Chrome ──────────────────────────────────────────────────────────────

class WebSocketTimeoutException(Exception):
    """Same class name _Rpc checks for, so a missing reply reads as a timeout."""


READY = {"href": "https://support.example.com/feoc/", "rs": "complete", "hook": True}
SHOT_BYTES = b"\xff\xd8\xff\xe0fake-jpeg"
METRICS = {
    "cssLayoutViewport": {"clientWidth": 1920, "clientHeight": 1080, "pageX": 0, "pageY": 0},
    "cssVisualViewport": {"clientWidth": 1920, "clientHeight": 1080, "pageX": 0, "pageY": 0},
    "cssContentSize": {"width": 1920, "height": 5000},
}


class FakePage:
    """Scripted page. Override the hooks per test; every CDP call is recorded
    as ``(method, params)`` in ``calls``."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.probes = [READY]          # consumed in order; the last one repeats
        self.lookups = [{"ok": True, "matched": 1, "visible": 1, "element": "<input>"}]
        self.fill_prep = {"focused": True, "element": "<input>"}
        self.click_box = {"x": 100.0, "y": 50.0, "element": "<button>"}
        self.eval_result = {"result": {"type": "number", "value": 2}}
        self.origins = ["https://support.example.com"]  # location.origin; last repeats
        self.href = "https://support.example.com/feoc/results"  # location.href
        self.metrics = METRICS                                   # Page.getLayoutMetrics
        self.shot = {"data": base64.b64encode(SHOT_BYTES).decode()}  # Page.captureScreenshot
        self.scroll = {"target": "window", "scroll_top": 0, "scroll_height": 5000,  # _SCROLL_JS
                       "client_height": 1080, "at_bottom": False}

    @staticmethod
    def _next(seq):
        return seq.pop(0) if len(seq) > 1 else seq[0]

    def value(self, v):
        return {"result": {"type": "object", "value": v}}

    def respond(self, method: str, params: dict):
        self.calls.append((method, params))
        if method == "Page.getLayoutMetrics":
            return self.metrics
        if method == "Page.captureScreenshot":
            return self.shot  # {"error": …} becomes a CDP error reply
        if method != "Runtime.evaluate":
            return {}
        expr = params["expression"]
        if expr == actions_mod._PROBE_JS:
            return self.value(self._next(self.probes))
        if expr == actions_mod._ORIGIN_JS:
            return self.value(self._next(self.origins))
        if expr == actions_mod._HREF_JS:
            return self.value(self.href)
        if actions_mod._FIND_JS in expr:
            # The deep query reports bad selectors as a value ({error: …}),
            # not by throwing, so every scripted lookup is returned by value.
            return self.value(self._next(self.lookups))
        if actions_mod._FILL_PREP_JS in expr:
            # The prep reports the origin it focused in; same source as the probe.
            return self.value({"origin": self._next(self.origins), **self.fill_prep})
        if actions_mod._FILL_DONE_JS in expr:
            return self.value({"element": "<input>", "value_length": 12})
        if actions_mod._CLICK_PREP_JS in expr:
            return self.value(self.click_box)
        if actions_mod._CLICK_JS_JS in expr:
            return self.value({"element": "<button>"})
        if actions_mod._FOCUS_JS in expr:
            return self.value({"focused": True, "element": "<input>"})
        if actions_mod._SELECT_JS in expr:
            return self.value({"element": "<select>", "value": "US"})
        if actions_mod._SCROLL_JS in expr:
            return self.value(self.scroll)
        return self.eval_result

    # helpers for assertions
    def methods(self) -> list[str]:
        return [m for m, _ in self.calls]

    def lookups_sent(self) -> int:
        return sum(1 for m, p in self.calls
                   if m == "Runtime.evaluate" and actions_mod._FIND_JS in p["expression"])


class FakeWs:
    def __init__(self, page: FakePage, drop_after: int | None = None):
        self.page = page
        self._queue: list[str] = []
        self.closed = False
        self._drop_after = drop_after
        self._sends = 0

    def settimeout(self, _t):
        pass

    def send(self, raw: str):
        self._sends += 1
        if self._drop_after is not None and self._sends > self._drop_after:
            raise ConnectionResetError("socket is already closed")
        msg = json.loads(raw)
        # An unsolicited event before every reply exercises the skip-other-ids path.
        self._queue.append(json.dumps({"method": "Runtime.consoleAPICalled", "params": {}}))
        out = self.page.respond(msg["method"], msg.get("params", {}))
        reply = out if set(out) == {"error"} else {"result": out}
        self._queue.append(json.dumps({"id": msg["id"], **reply}))

    def recv(self) -> str:
        if not self._queue:
            raise WebSocketTimeoutException()
        return self._queue.pop(0)

    def close(self):
        self.closed = True


def _patch_chrome(monkeypatch, page: FakePage, sockets=None):
    """Point actions.py's tab listing / socket opening at the fake. ``sockets``
    is an optional list of FakeWs handed out one per connect."""
    opened: list[FakeWs] = []

    def open_ws(url, timeout):
        ws = sockets.pop(0) if sockets else FakeWs(page)
        opened.append(ws)
        return ws

    monkeypatch.setattr(actions_mod, "_list_tabs", lambda port, timeout: [
        {"type": "page", "url": READY["href"], "webSocketDebuggerUrl": "ws://fake"}
    ])
    monkeypatch.setattr(actions_mod, "_open_ws", open_ws)
    return opened


def _run(acts, **kw):
    kw.setdefault("ready_timeout_s", 2.0)
    kw.setdefault("poll_interval_s", 0.01)
    return run_actions(9224, parse_actions(acts), **kw)


# ── Readiness gate ───────────────────────────────────────────────────────────

def test_gate_waits_through_blank_loading_and_missing_hook_then_runs(monkeypatch):
    page = FakePage()
    page.probes = [
        {"href": "about:blank", "rs": "complete", "hook": True},
        {"href": READY["href"], "rs": "loading", "hook": False},
        {"href": READY["href"], "rs": "complete", "hook": False},
        READY,
    ]
    opened = _patch_chrome(monkeypatch, page)

    report = _run([{"type": "wait_for", "selector": "input"}])

    assert report.ok and report.aborted_reason is None
    assert [r.ok for r in report.actions] == [True]
    probes = [m for m, p in page.calls if p.get("expression") == actions_mod._PROBE_JS]
    assert len(probes) == 4
    assert page.lookups_sent() == 1
    assert opened and opened[0].closed  # side socket closed on the way out


def test_gate_times_out_and_skips_everything(monkeypatch):
    page = FakePage()
    page.probes = [{"href": READY["href"], "rs": "interactive", "hook": True}]
    _patch_chrome(monkeypatch, page)

    report = _run([{"type": "click", "selector": "b"}, {"type": "wait", "seconds": 0}],
                  page_script="1", ready_timeout_s=0.2)

    assert report.aborted_reason.startswith("not ready: document.readyState='interactive'")
    assert report.page_script.error == "skipped"
    assert [(r.index, r.ok, r.error) for r in report.actions] == [(0, False, "skipped"), (1, False, "skipped")]
    assert page.lookups_sent() == 0


def test_gate_never_acts_on_a_login_page(monkeypatch):
    page = FakePage()
    page.probes = [{"href": "https://sso.example.com/login?next=/feoc", "rs": "complete", "hook": True}]
    _patch_chrome(monkeypatch, page)

    report = _run([{"type": "fill", "selector": "input", "value": "x"}],
                  ready_timeout_s=0.2, login_url_patterns=[r"sso\.example\.com"])

    assert "login page https://sso.example.com/login" in report.aborted_reason
    assert page.lookups_sent() == 0
    assert "Input.insertText" not in page.methods()


SSO = "https://sso.example.com/login?next=/feoc"


@pytest.mark.parametrize("gate, acts", [("page", False), ("login", True)])
def test_login_gate_acts_on_the_login_page_the_default_gate_refuses(monkeypatch, gate, acts):
    # The login page has no capture hook and matches login_url_patterns:
    # the default gate waits it out, gate="login" types into it.
    page = FakePage()
    page.probes = [{"href": SSO, "rs": "complete", "hook": False}]
    page.origins = ["https://sso.example.com"]
    _patch_chrome(monkeypatch, page)

    report = _run([{"type": "fill", "selector": "input[type=password]", "value": "pw"}],
                  ready_timeout_s=0.2, login_url_patterns=[r"sso\.example\.com"], gate=gate,
                  fill_origins=["https://sso.example.com"] if gate == "login" else None)

    assert report.ok is acts, report.aborted_reason
    assert ("Input.insertText" in page.methods()) is acts
    if not acts:
        assert "login page https://sso.example.com/login" in report.aborted_reason


def test_login_gate_still_waits_for_a_real_url_and_complete(monkeypatch):
    page = FakePage()
    page.probes = [
        {"href": "about:blank", "rs": "complete", "hook": False},
        {"href": SSO, "rs": "interactive", "hook": False},
        {"href": SSO, "rs": "complete", "hook": False},
    ]
    _patch_chrome(monkeypatch, page)

    report = _run([{"type": "wait", "seconds": 0}], gate="login",
                  login_url_patterns=[r"sso\.example\.com"])

    assert report.ok
    assert sum(1 for _, p in page.calls if p.get("expression") == actions_mod._PROBE_JS) == 3


def test_login_gate_regate_after_navigation_does_not_refuse_the_login_url(monkeypatch):
    # A multi-page login (email → password) replaces the document between
    # steps: the lookup's re-gate must use the login gate too.
    page = FakePage()
    page.probes = [{"href": SSO, "rs": "complete", "hook": False}]
    _patch_chrome(monkeypatch, page)
    real_respond = page.respond
    state = {"armed": True}

    def respond(method, params):
        if (state["armed"] and method == "Runtime.evaluate"
                and actions_mod._FIND_JS in params["expression"]):
            state["armed"] = False
            page.calls.append((method, params))
            return {"error": {"code": -32000, "message": "Execution context was destroyed."}}
        return real_respond(method, params)

    page.respond = respond
    report = _run([{"type": "wait_for", "selector": "input[type=password]", "timeout_s": 1}],
                  gate="login", login_url_patterns=[r"sso\.example\.com"])

    assert report.ok, report.aborted_reason
    assert page.lookups_sent() == 2


def test_fill_origins_blocks_a_wrong_origin_before_touching_the_page(monkeypatch):
    page = FakePage()
    page.probes = [{"href": "https://evil.example.net/login", "rs": "complete", "hook": False}]
    page.origins = ["https://evil.example.net"]
    _patch_chrome(monkeypatch, page)

    report = _run([{"type": "fill", "selector": "input", "value": "hunter2"},
                   {"type": "click", "selector": "button"}],
                  gate="login", fill_origins=["https://sso.example.com"], label="login_actions")

    assert report.actions[0].error == "origin https://evil.example.net not allowed for this fill"
    assert report.aborted_reason == (
        "login_actions[0] (fill) failed: origin https://evil.example.net not allowed for this fill"
    )
    assert report.actions[1].error == "skipped"
    assert page.lookups_sent() == 0
    assert "Input.insertText" not in page.methods()
    assert not any(actions_mod._FILL_PREP_JS in p.get("expression", "") for _, p in page.calls)
    assert "hunter2" not in json.dumps(report.to_dict())


def test_fill_origins_allows_the_listed_origin(monkeypatch):
    page = FakePage()
    page.origins = ["https://sso.example.com"]
    _patch_chrome(monkeypatch, page)

    report = _run([{"type": "fill", "selector": "input", "value": "hunter2"}],
                  fill_origins=["https://sso.example.com"])

    assert report.ok, report.aborted_reason
    assert next(p for m, p in page.calls if m == "Input.insertText") == {"text": "hunter2"}
    assert report.actions[0].value == {"element": "<input>", "value_length": 12}


def test_fill_origins_rechecks_at_focus_time(monkeypatch):
    # The page navigated between the origin check and the focus: the prep
    # reports the new origin and nothing is typed.
    page = FakePage()
    page.origins = ["https://sso.example.com", "https://elsewhere.example.org"]
    _patch_chrome(monkeypatch, page)

    report = _run([{"type": "fill", "selector": "input", "value": "hunter2"}],
                  fill_origins=["https://sso.example.com"])

    assert report.actions[0].error == "origin https://elsewhere.example.org not allowed for this fill"
    assert "Input.insertText" not in page.methods()


def test_without_fill_origins_no_origin_probe_is_sent(monkeypatch):
    page = FakePage()
    _patch_chrome(monkeypatch, page)

    assert _run([{"type": "fill", "selector": "input", "value": "x"}]).ok
    assert not any(p.get("expression") == actions_mod._ORIGIN_JS for _, p in page.calls)


def test_gate_tolerates_chrome_not_up_yet(monkeypatch):
    page = FakePage()
    attempts = {"n": 0}

    def flaky_tabs(port, timeout):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConnectionRefusedError("refused")
        return [{"type": "page", "url": "about:blank", "webSocketDebuggerUrl": "ws://fake"}]

    monkeypatch.setattr(actions_mod, "_list_tabs", flaky_tabs)
    monkeypatch.setattr(actions_mod, "_open_ws", lambda url, timeout: FakeWs(page))

    report = _run([{"type": "wait", "seconds": 0}])
    assert report.ok and attempts["n"] == 3


# ── Steps ────────────────────────────────────────────────────────────────────

def test_fill_focuses_then_inserts_trusted_text_then_fires_change(monkeypatch):
    page = FakePage()
    _patch_chrome(monkeypatch, page)

    report = _run([{"type": "fill", "selector": "lightning-input", "value": "532614044013"}])

    assert report.ok, report.aborted_reason
    evals = [(m, p) for m, p in page.calls if m != "Runtime.evaluate"
             or p["expression"] != actions_mod._PROBE_JS]
    kinds = []
    for m, p in evals:
        expr = p.get("expression", "")
        if actions_mod._FIND_JS in expr:
            kinds.append("find")
        elif actions_mod._FILL_PREP_JS in expr:
            kinds.append("focus+clear")
            assert expr.endswith("(true)")  # clear defaults on
        elif actions_mod._FILL_DONE_JS in expr:
            kinds.append("change+blur")
        else:
            kinds.append(m)
    assert kinds == ["find", "focus+clear", "Input.insertText", "change+blur"]
    insert = next(p for m, p in page.calls if m == "Input.insertText")
    assert insert == {"text": "532614044013"}
    assert report.actions[0].value == {"element": "<input>", "value_length": 12}


def test_fill_fails_when_focus_does_not_land(monkeypatch):
    page = FakePage()
    page.fill_prep = {"focused": False, "element": "<input>"}
    _patch_chrome(monkeypatch, page)

    report = _run([{"type": "fill", "selector": "input", "value": "x"}])

    assert not report.ok and "could not focus" in report.actions[0].error
    assert "Input.insertText" not in page.methods()


def test_click_mouse_presses_and_releases_at_box_centre(monkeypatch):
    page = FakePage()
    page.click_box = {"x": 412.5, "y": 318.0, "element": "<button>"}
    _patch_chrome(monkeypatch, page)

    report = _run([{"type": "click", "selector": "button", "text": "Check"}])

    assert report.ok, report.aborted_reason
    mouse = [p for m, p in page.calls if m == "Input.dispatchMouseEvent"]
    assert [p["type"] for p in mouse] == ["mouseMoved", "mousePressed", "mouseReleased"]
    assert all((p["x"], p["y"]) == (412.5, 318.0) for p in mouse)
    assert mouse[1]["button"] == "left" and mouse[1]["clickCount"] == 1 and mouse[1]["buttons"] == 1
    assert mouse[2]["button"] == "left" and mouse[2]["clickCount"] == 1
    # the lookup carries the selector and the text filter, JSON-encoded
    find = next(p["expression"] for m, p in page.calls
                if m == "Runtime.evaluate" and actions_mod._FIND_JS in p["expression"])
    assert find.endswith('("button", "Check", "visible")')


def test_click_js_uses_element_click_and_no_input_events(monkeypatch):
    page = FakePage()
    _patch_chrome(monkeypatch, page)

    report = _run([{"type": "click", "selector": "button", "method": "js"}])

    assert report.ok
    assert "Input.dispatchMouseEvent" not in page.methods()
    assert any(actions_mod._CLICK_JS_JS in p.get("expression", "") for _, p in page.calls)


def test_press_enter_sends_keydown_with_carriage_return_then_keyup(monkeypatch):
    page = FakePage()
    _patch_chrome(monkeypatch, page)

    report = _run([{"type": "press", "key": "Enter", "selector": "input"}])

    assert report.ok, report.aborted_reason
    keys = [p for m, p in page.calls if m == "Input.dispatchKeyEvent"]
    assert [k["type"] for k in keys] == ["keyDown", "keyUp"]
    assert keys[0]["key"] == "Enter" and keys[0]["windowsVirtualKeyCode"] == 13
    assert keys[0]["text"] == "\r"
    assert any(actions_mod._FOCUS_JS in p.get("expression", "") for _, p in page.calls)


def test_select_passes_value(monkeypatch):
    page = FakePage()
    _patch_chrome(monkeypatch, page)

    report = _run([{"type": "select", "selector": "select", "value": "US"}])

    assert report.ok and report.actions[0].value == {"element": "<select>", "value": "US"}
    sel = next(p["expression"] for _, p in page.calls if actions_mod._SELECT_JS in p.get("expression", ""))
    assert sel.endswith('("US")')


def test_evaluate_returns_value_with_await_and_user_gesture(monkeypatch):
    page = FakePage()
    page.eval_result = {"result": {"type": "object", "value": {"rows": [1, 2]}}}
    _patch_chrome(monkeypatch, page)

    report = _run([{"type": "evaluate", "script": "(async () => ({rows: [1, 2]}))()"}])

    assert report.ok and report.actions[0].value == {"rows": [1, 2]}
    params = next(p for m, p in page.calls if p.get("expression", "").startswith("(async"))
    assert params["awaitPromise"] is True and params["returnByValue"] is True
    assert params["userGesture"] is True


def test_evaluate_exception_fails_the_step(monkeypatch):
    page = FakePage()
    page.eval_result = {
        "result": {"type": "object"},
        "exceptionDetails": {"text": "Uncaught", "exception": {"description": "TypeError: x is null"}},
    }
    _patch_chrome(monkeypatch, page)

    report = _run([{"type": "evaluate", "script": "x.y"}, {"type": "wait", "seconds": 0}])

    assert report.actions[0].error == "TypeError: x is null"
    assert report.actions[1].error == "skipped"
    assert report.aborted_reason == "actions[0] (evaluate) failed: TypeError: x is null"


def test_page_script_is_rerun_once_when_its_document_navigates_away(monkeypatch):
    # The gate can pass on a post-login landing page just before the capture
    # session re-navigates to the target: the script's context is destroyed.
    page = FakePage()
    page.eval_result = {"result": {"type": "object", "value": ["fields"]}}
    _patch_chrome(monkeypatch, page)
    real_respond = page.respond
    state = {"armed": True}

    def respond(method, params):
        if state["armed"] and params.get("expression") == "dump()":
            state["armed"] = False
            page.calls.append((method, params))
            return {"error": {"code": -32000, "message": "Execution context was destroyed."}}
        return real_respond(method, params)

    page.respond = respond
    report = _run([], page_script="dump()")

    assert report.ok and report.page_script.value == ["fields"]
    assert sum(1 for _, p in page.calls if p.get("expression") == "dump()") == 2


def test_page_script_runs_first_and_its_value_is_reported(monkeypatch):
    page = FakePage()
    page.eval_result = {"result": {"type": "object", "value": [{"tag": "input"}]}}
    _patch_chrome(monkeypatch, page)

    report = _run([{"type": "wait", "seconds": 0}], page_script="(async () => [])()")

    assert report.page_script.index == -1 and report.page_script.type == "page_script"
    assert report.page_script.ok and report.page_script.value == [{"tag": "input"}]
    assert report.ok and report.actions[0].ok
    assert report.to_dict()["page_script"]["value"] == [{"tag": "input"}]


def test_first_failure_skips_the_rest(monkeypatch):
    page = FakePage()
    page.lookups = [{"ok": False, "reason": "3 matched the selector, none visible"}]
    _patch_chrome(monkeypatch, page)

    report = _run([
        {"type": "wait_for", "selector": "button", "timeout_s": 0.2},
        {"type": "click", "selector": "button"},
        {"type": "wait", "seconds": 0},
    ])

    first, *rest = report.actions
    assert not first.ok
    assert "timed out after 0.2s waiting for 'button' (visible): 3 matched the selector, none visible" \
        in first.error
    assert [(r.index, r.error) for r in rest] == [(1, "skipped"), (2, "skipped")]
    assert report.aborted_reason.startswith("actions[0] (wait_for) failed:")
    assert "Input.dispatchMouseEvent" not in page.methods()


def test_invalid_selector_fails_immediately_without_polling(monkeypatch):
    page = FakePage()
    page.lookups = [{"error": "invalid selector \"[[\": not a valid selector"}]
    _patch_chrome(monkeypatch, page)

    t0 = time.monotonic()
    report = _run([{"type": "wait_for", "selector": "[[", "timeout_s": 5}])

    assert time.monotonic() - t0 < 1.0
    assert "invalid selector" in report.actions[0].error
    assert page.lookups_sent() == 1


def test_lookup_regates_after_navigation_then_finds_the_element(monkeypatch):
    page = FakePage()
    _patch_chrome(monkeypatch, page)
    # The first lookup lands mid-navigation: CDP has no context for it.
    real_respond = page.respond
    state = {"armed": True}

    def respond(method, params):
        if (state["armed"] and method == "Runtime.evaluate"
                and actions_mod._FIND_JS in params["expression"]):
            state["armed"] = False
            page.calls.append((method, params))
            return {"error": {"code": -32000, "message": "Cannot find context with specified id"}}
        return real_respond(method, params)

    page.respond = respond
    report = _run([{"type": "wait_for", "selector": ".results"}])

    assert report.ok, report.aborted_reason
    assert page.lookups_sent() == 2
    probes = [1 for m, p in page.calls if p.get("expression") == actions_mod._PROBE_JS]
    assert len(probes) == 2  # initial gate + the re-gate after the lost context


def test_dropped_side_socket_is_reopened(monkeypatch):
    page = FakePage()
    # First socket dies after the gate probe; the lookup's send fails, the
    # side channel reconnects on the next call and the step still succeeds.
    sockets = [FakeWs(page, drop_after=1), FakeWs(page)]
    opened = _patch_chrome(monkeypatch, page, sockets=sockets)

    report = _run([{"type": "wait_for", "selector": "input"}])

    assert report.ok, report.aborted_reason
    assert len(opened) == 2


def _kinds(page: FakePage) -> list[str]:
    """The page's calls with the readiness probes dropped, named by what they did."""
    out = []
    for m, p in page.calls:
        expr = p.get("expression", "")
        if m == "Runtime.evaluate" and expr == actions_mod._PROBE_JS:
            continue
        if actions_mod._FIND_JS in expr:
            out.append("find")
        elif expr == actions_mod._HREF_JS:
            out.append("href")
        elif m == "Runtime.evaluate":
            out.append("eval")
        else:
            out.append(m)
    return out


def test_screenshot_between_steps_returns_the_image(monkeypatch):
    page = FakePage()
    _patch_chrome(monkeypatch, page)

    report = _run([
        {"type": "fill", "selector": "input", "value": "532614044013"},
        {"type": "screenshot", "scale": 0.5},
        {"type": "click", "selector": "button"},
    ])

    assert report.ok, report.aborted_reason
    assert [(r.type, r.ok) for r in report.actions] == [("fill", True), ("screenshot", True), ("click", True)]
    shot = report.actions[1].value
    assert shot == {
        "format": "jpeg", "mime_type": "image/jpeg", "width": 960, "height": 540,
        "full_page": False, "bytes": len(SHOT_BYTES),
        "page_url": "https://support.example.com/feoc/results",
        "data_base64": base64.b64encode(SHOT_BYTES).decode(),
    }
    # Taken between the fill and the click, on the same side socket.
    kinds = _kinds(page)
    i = kinds.index("Page.captureScreenshot")
    assert kinds.index("Input.insertText") < kinds.index("href") < kinds.index("Page.getLayoutMetrics") < i
    assert i < kinds.index("Input.dispatchMouseEvent")
    params = next(p for m, p in page.calls if m == "Page.captureScreenshot")
    assert params == {"format": "jpeg", "captureBeyondViewport": False, "quality": 80,
                      "clip": {"x": 0.0, "y": 0.0, "width": 1920, "height": 1080, "scale": 0.5}}


def test_screenshot_full_page_png_uses_the_document_size(monkeypatch):
    page = FakePage()
    _patch_chrome(monkeypatch, page)

    report = _run([{"type": "screenshot", "format": "png", "full_page": True, "max_height": 3000}])

    assert report.ok
    assert (report.actions[0].value["width"], report.actions[0].value["height"]) == (1920, 3000)
    params = next(p for m, p in page.calls if m == "Page.captureScreenshot")
    assert params["captureBeyondViewport"] is True and "quality" not in params


def test_failed_screenshot_is_recorded_and_the_run_continues(monkeypatch):
    page = FakePage()
    page.shot = {"error": {"code": -32000, "message": "Unable to capture screenshot"}}
    _patch_chrome(monkeypatch, page)

    report = _run([
        {"type": "screenshot"},
        {"type": "click", "selector": "button"},
        {"type": "screenshot"},
    ])

    assert report.aborted_reason is None and report.ok
    first, click, last = report.actions
    assert not first.ok and "Unable to capture screenshot" in first.error and first.value is None
    assert click.ok and "Input.dispatchMouseEvent" in page.methods()
    assert not last.ok and last.error != "skipped"


def test_screenshot_of_an_empty_page_fails_without_capturing(monkeypatch):
    page = FakePage()
    page.metrics = {"cssLayoutViewport": {"clientWidth": 0, "clientHeight": 0}}
    _patch_chrome(monkeypatch, page)

    report = _run([{"type": "screenshot"}, {"type": "wait", "seconds": 0}])

    assert "layout metrics reported an empty page" in report.actions[0].error
    assert report.actions[1].ok and report.aborted_reason is None
    assert "Page.captureScreenshot" not in page.methods()


def test_screenshot_is_taken_even_when_the_page_never_settles(monkeypatch):
    # The initial gate passes; by the time the screenshot step runs the page
    # is loading again (a click navigated) and never finishes.
    monkeypatch.setattr(actions_mod, "_SCREENSHOT_SETTLE_S", 0.2)
    page = FakePage()
    page.probes = [READY, {"href": READY["href"], "rs": "loading", "hook": False}]
    _patch_chrome(monkeypatch, page)

    report = _run([{"type": "screenshot"}])

    assert report.ok and report.actions[0].ok
    assert report.actions[0].value["bytes"] == len(SHOT_BYTES)


def test_cancel_during_a_screenshot_aborts_the_run(monkeypatch):
    page = FakePage()
    _patch_chrome(monkeypatch, page)
    flag = threading.Event()
    real_respond = page.respond

    def respond(method, params):
        if method == "Page.getLayoutMetrics":
            flag.set()  # cancelled while the step is underway
        return real_respond(method, params)

    page.respond = respond
    report = _run([{"type": "screenshot"}, {"type": "wait", "seconds": 0}], cancel=flag.is_set)

    assert report.actions[0].error == "cancelled"
    assert report.actions[1].error == "skipped"
    assert report.aborted_reason == "cancelled"
    assert "Page.captureScreenshot" not in page.methods()


def _scroll_exprs(page: FakePage) -> list[str]:
    return [p["expression"] for m, p in page.calls
            if m == "Runtime.evaluate" and actions_mod._SCROLL_JS in p["expression"]]


def test_scroll_to_an_element_looks_it_up_first_then_frames_it(monkeypatch):
    page = FakePage()
    page.scroll = {"target": '<div class="slds-scrollable_y">', "element": "<h2>",
                   "scroll_top": 1200, "scroll_height": 4000, "client_height": 900,
                   "at_bottom": False}
    _patch_chrome(monkeypatch, page)

    report = _run([{"type": "scroll", "selector": "h2", "text": "Results"}])

    assert report.ok, report.aborted_reason
    assert report.actions[0].value == page.scroll
    assert _kinds(page) == ["find", "eval"]  # the deep lookup, then the scroll
    find = next(p["expression"] for m, p in page.calls
                if m == "Runtime.evaluate" and actions_mod._FIND_JS in p["expression"])
    assert find.endswith('("h2", "Results", "visible")')
    [expr] = _scroll_exprs(page)
    assert expr.startswith(actions_mod._HELPER_JS)  # the helper is re-installed first
    assert expr.endswith('(true, null, null, "start")')


@pytest.mark.parametrize("step, args", [
    ({"to": "bottom"}, '(false, "bottom", null, "start")'),
    ({"to": "top"}, '(false, "top", null, "start")'),
    ({"by": -300}, '(false, null, -300, "start")'),
])
def test_scroll_without_a_selector_makes_no_lookup(monkeypatch, step, args):
    page = FakePage()
    # Nothing on the page scrolls: a no-op, reported, not a failure.
    page.scroll = {"target": None, "scroll_top": 0, "scroll_height": 900,
                   "client_height": 900, "at_bottom": True}
    _patch_chrome(monkeypatch, page)

    report = _run([{"type": "scroll", **step}])

    assert report.ok, report.aborted_reason
    assert report.actions[0].value["target"] is None
    assert page.lookups_sent() == 0
    [expr] = _scroll_exprs(page)
    assert expr.endswith(args)


def test_scroll_with_a_selector_and_to_targets_its_container(monkeypatch):
    page = FakePage()
    _patch_chrome(monkeypatch, page)

    report = _run([{"type": "scroll", "selector": ".results-list", "to": "bottom"}])

    assert report.ok, report.aborted_reason
    assert page.lookups_sent() == 1
    [expr] = _scroll_exprs(page)
    assert expr.endswith('(true, "bottom", null, "start")')


def test_scroll_error_fails_the_step_and_aborts_the_run(monkeypatch):
    page = FakePage()
    gone = "the matched element is gone (page re-rendered or navigated)"
    page.scroll = {"error": gone}
    _patch_chrome(monkeypatch, page)

    report = _run([{"type": "scroll", "selector": "h2"}, {"type": "screenshot"}])

    assert report.actions[0].error == gone
    assert report.actions[1].error == "skipped"
    assert report.aborted_reason == f"actions[0] (scroll) failed: {gone}"
    assert "Page.captureScreenshot" not in page.methods()


def test_scroll_between_a_click_and_a_screenshot_runs_in_order(monkeypatch):
    page = FakePage()
    _patch_chrome(monkeypatch, page)

    report = _run([
        {"type": "click", "selector": "button"},
        {"type": "scroll", "to": "top"},
        {"type": "screenshot"},
    ])

    assert report.ok, report.aborted_reason
    assert [r.type for r in report.actions] == ["click", "scroll", "screenshot"]
    calls = page.calls
    last_mouse = max(i for i, (m, _) in enumerate(calls) if m == "Input.dispatchMouseEvent")
    scroll_at = next(i for i, (m, p) in enumerate(calls)
                     if actions_mod._SCROLL_JS in p.get("expression", ""))
    shot_at = next(i for i, (m, _) in enumerate(calls) if m == "Page.captureScreenshot")
    assert last_mouse < scroll_at < shot_at


def test_scroll_runs_under_the_login_gate(monkeypatch):
    page = FakePage()
    page.probes = [{"href": SSO, "rs": "complete", "hook": False}]
    _patch_chrome(monkeypatch, page)

    report = run_actions(
        9224,
        parse_actions([{"type": "scroll", "selector": "input[type=submit]", "block": "center"}],
                      label="login_actions", allowed_types=LOGIN_TYPES),
        gate="login", login_url_patterns=[r"sso\.example\.com"], label="login_actions",
        ready_timeout_s=2.0, poll_interval_s=0.01,
    )

    assert report.ok, report.aborted_reason
    [expr] = _scroll_exprs(page)
    assert expr.endswith('(true, null, null, "center")')


def test_scroll_js_is_standalone_and_leaves_the_helper_alone():
    # The container logic lives in _SCROLL_JS; the helper's v: 1 contract is
    # untouched, and the scroll script only uses what the helper exposes.
    assert "window.__ciActions.v === 1" in actions_mod._HELPER_JS
    assert "scrollingElement" not in actions_mod._HELPER_JS
    assert actions_mod._TARGET_GONE in actions_mod._SCROLL_JS
    assert actions_mod._SCROLL_JS.startswith("(function (useTarget, to, by, block) {")
    assert "behavior: 'instant'" in actions_mod._SCROLL_JS


def test_cancel_stops_a_long_wait_promptly(monkeypatch):
    page = FakePage()
    _patch_chrome(monkeypatch, page)
    flag = threading.Event()
    threading.Timer(0.2, flag.set).start()

    t0 = time.monotonic()
    report = _run([{"type": "wait", "seconds": 30}, {"type": "click", "selector": "b"}],
                  cancel=flag.is_set)

    assert time.monotonic() - t0 < 2.0
    assert report.actions[0].error == "cancelled"
    assert report.actions[1].error == "skipped"
    assert report.aborted_reason == "cancelled"


def test_cancel_during_gate(monkeypatch):
    page = FakePage()
    page.probes = [{"href": "about:blank", "rs": "complete", "hook": False}]
    _patch_chrome(monkeypatch, page)
    flag = threading.Event()
    threading.Timer(0.1, flag.set).start()

    t0 = time.monotonic()
    report = _run([{"type": "wait", "seconds": 0}], ready_timeout_s=30, cancel=flag.is_set)

    assert time.monotonic() - t0 < 2.0
    assert report.aborted_reason == "cancelled"


def test_on_progress_counts_executed_steps(monkeypatch):
    page = FakePage()
    _patch_chrome(monkeypatch, page)
    seen = []

    _run([{"type": "wait", "seconds": 0}, {"type": "wait", "seconds": 0}],
         on_progress=lambda done, total: seen.append((done, total)))

    assert seen == [(1, 2), (2, 2)]


def test_not_run_report_lists_every_step_as_skipped():
    acts = parse_actions([{"type": "wait", "seconds": 0}])
    r = ActionsReport.not_run(acts, "1", "browser is not running")
    assert r.aborted_reason == "browser is not running" and not r.ok
    assert r.page_script.error == "skipped" and r.actions[0].error == "skipped"


def test_helper_js_is_a_single_guarded_install():
    # Every lookup/act expression re-installs the helper (a navigation wipes
    # window.__ciActions); the guard keeps that idempotent within a document.
    expr = actions_mod._call_js(actions_mod._FIND_JS, "a", None, "visible")
    assert expr.startswith(actions_mod._HELPER_JS)
    assert "window.__ciActions.v === 1" in actions_mod._HELPER_JS
    assert expr.endswith('window.__ciActions.find("a", null, "visible")')
