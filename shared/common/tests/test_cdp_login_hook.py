"""Tests for the login-wall hook: ``run_session(on_login_wall=...)`` and
``InterceptorClient._on_login_wall`` (what runs ``login_actions``).

No browser is launched. ``run_session`` imports ``requests`` and ``websocket``
at call time, so patching ``requests.get`` / ``websocket.create_connection``
hands it a scripted tab whose ``location.href`` reads follow a fixed list.
The client hook is exercised with ``run_actions`` replaced by a recorder.
"""

from __future__ import annotations

import json
import threading
import time
import types

import pytest
import requests
import websocket

from common.cdp_interceptor import cdp_session as session_mod
from common.cdp_interceptor import client as client_mod
from common.cdp_interceptor.actions import ActionsReport, parse_actions
from common.cdp_interceptor.client import InterceptorClient

TARGET = "https://app.example.com/target"
SSO = "https://sso.example.com/login"


# ── run_session ──────────────────────────────────────────────────────────────

class ScriptedTab:
    """Answers run_session's CDP calls. ``location.href`` reads come from
    ``hrefs`` in order (the last repeats); every Page.navigate is recorded with
    how many href reads had happened by then."""

    def __init__(self, hrefs: list[str]):
        self.hrefs = list(hrefs)
        self.href_reads = 0
        self.navigations: list[tuple[str, int]] = []
        self.queue: list[str] = []
        self.closed = False

    # websocket surface
    def settimeout(self, _t):
        pass

    def send(self, raw: str):
        msg = json.loads(raw)
        self.queue.append(json.dumps({"id": msg["id"], "result": self.respond(msg["method"], msg.get("params", {}))}))

    def recv(self) -> str:
        if not self.queue:
            raise websocket.WebSocketTimeoutException()
        return self.queue.pop(0)

    def close(self):
        self.closed = True

    # CDP
    def respond(self, method: str, params: dict) -> dict:
        if method == "Page.navigate":
            self.navigations.append((params["url"], self.href_reads))
            return {}
        if method != "Runtime.evaluate":
            return {}
        expr = params.get("expression", "")
        if expr == "location.href":
            self.href_reads += 1
            href = self.hrefs.pop(0) if len(self.hrefs) > 1 else self.hrefs[0]
            return {"result": {"type": "string", "value": href}}
        if expr.startswith("JSON.stringify(window._capturedResponses"):
            caps = [{"seq": 1, "url": "https://app.example.com/api/data", "body": {"ok": True}}]
            return {"result": {"type": "string", "value": json.dumps(caps)}}
        return {"result": {"type": "undefined"}}


def _run_session(monkeypatch, tab: ScriptedTab, hook, *, login_timeout: int = 60):
    monkeypatch.setattr(requests, "get", lambda url, timeout: types.SimpleNamespace(
        json=lambda: [{"type": "page", "url": "about:blank", "webSocketDebuggerUrl": "ws://fake"}]
    ))
    monkeypatch.setattr(websocket, "create_connection", lambda url, timeout: tab)
    # The login poll sleeps 3 s per tick; keep the clock, drop the waiting.
    monkeypatch.setattr(session_mod, "time", types.SimpleNamespace(time=time.time, sleep=lambda s: None))

    events: list[tuple] = []
    stop = threading.Event()

    def on_data(_d):
        events.append(("data",))
        stop.set()

    session_mod.run_session(
        debug_port=9224,
        interceptor_script="/* interceptor */",
        target_url=TARGET,
        parse_fn=None,
        on_data=on_data,
        on_capture=None,
        on_status=lambda s, e: events.append(("status", s)),
        reload_event=threading.Event(),
        stop_event=stop,
        login_timeout=login_timeout,
        capture_timeout=30,
        capture_poll=0.0,
        login_url_keywords=(r"sso\.example\.com",),
        on_login_wall=hook,
    )
    return events


def test_hook_fires_once_then_the_poll_renavigates_and_empty_href_is_not_resolved(monkeypatch):
    tab = ScriptedTab([
        "about:blank",                     # 1: before the first navigate
        SSO,                               # 2: post-nav poll → login wall #1, hook runs
        "",                                # 3: failed read — NOT resolved
        SSO,                               # 4: still on the login page
        "https://app.example.com/home",    # 5: resolved → re-navigate
        SSO + "?again",                    # 6: login wall #2 — hook must not run again
        TARGET,                            # 7: resolved → re-navigate
        TARGET,                            # 8: capture poll → data
    ])
    calls: list[int] = []

    def hook():
        calls.append(tab.href_reads)

    events = _run_session(monkeypatch, tab, hook)

    assert calls == [2]  # once, right after the first wall was seen
    # Re-navigation only after a real non-login href — never after the "" at read 3.
    assert tab.navigations == [(TARGET, 1), (TARGET, 5), (TARGET, 7)]
    statuses = [e[1] for e in events if e[0] == "status"]
    assert statuses == ["waiting_login", "loading", "waiting_login", "loading"]
    assert events[-1] == ("data",)
    assert tab.closed


def test_slow_hook_does_not_eat_the_login_timeout(monkeypatch):
    # The hook takes longer than login_timeout. The wait's deadline starts
    # when the hook returns, so the poll still gets to see the login resolve
    # instead of raising TimeoutError straight away.
    tab = ScriptedTab(["about:blank", SSO, TARGET])
    order: list[str] = []

    def hook():
        order.append(f"hook@{tab.href_reads}")
        time.sleep(1.2)

    events = _run_session(monkeypatch, tab, hook, login_timeout=1)

    assert events[0] == ("status", "waiting_login")  # reported before the hook
    assert order == ["hook@2"]
    assert tab.navigations[-1] == (TARGET, 3)
    assert events[-1] == ("data",)


def test_a_raising_hook_is_swallowed(monkeypatch):
    tab = ScriptedTab(["about:blank", SSO, TARGET])

    def hook():
        raise RuntimeError("boom")

    events = _run_session(monkeypatch, tab, hook)
    assert events[-1] == ("data",)
    assert len(tab.navigations) == 2


def test_without_a_hook_the_login_wait_is_unchanged(monkeypatch):
    tab = ScriptedTab(["about:blank", SSO, "", TARGET])
    events = _run_session(monkeypatch, tab, None)
    assert tab.navigations == [(TARGET, 1), (TARGET, 4)]
    assert events[-1] == ("data",)


# ── InterceptorClient._on_login_wall ─────────────────────────────────────────

LOGIN_STEPS = [
    {"type": "fill", "selector": "input[type=password]", "value": "hunter2"},
    {"type": "click", "selector": "button[type=submit]"},
]


def _client(tmp_path, **kw) -> InterceptorClient:
    kw.setdefault("login_actions", parse_actions(LOGIN_STEPS, label="login_actions"))
    kw.setdefault("login_fill_origins", ["https://sso.example.com"])
    return InterceptorClient(profile_dir=str(tmp_path), debug_port=9230, **kw)


def test_client_hook_runs_login_actions_once_with_the_login_gate(tmp_path, monkeypatch):
    seen: list[tuple] = []
    lock = threading.Lock()

    def fake_run_actions(port, actions, **kw):
        seen.append((port, list(actions), kw, lock.locked()))
        return ActionsReport()

    monkeypatch.setattr(client_mod, "run_actions", fake_run_actions)
    c = _client(tmp_path, login_lock=lock, login_actions_timeout_s=45)
    assert c.get_login_report() is None

    stop = threading.Event()
    c._on_login_wall(stop)
    c._on_login_wall(stop)  # one attempt per client — never retried

    assert len(seen) == 1
    port, actions, kw, held = seen[0]
    assert port == 9230 and held  # the per-profile lock is held during the run
    assert [a.type for a in actions] == ["fill", "click"] and actions[0].value == "hunter2"
    assert kw["gate"] == "login"
    assert kw["fill_origins"] == ("https://sso.example.com",)
    assert kw["ready_timeout_s"] == 45 and kw["label"] == "login_actions"
    assert kw["cancel"]() is False
    stop.set()
    assert kw["cancel"]() is True
    assert not lock.locked()
    assert c.get_login_report().ok


def test_client_hook_relabels_its_own_timeout(tmp_path, monkeypatch):
    def slow(port, actions, *, cancel, **kw):
        while not cancel():
            time.sleep(0.01)
        return ActionsReport.not_run(actions, None, "cancelled")

    monkeypatch.setattr(client_mod, "run_actions", slow)
    c = _client(tmp_path, login_actions_timeout_s=0.1)
    c._on_login_wall(threading.Event())
    assert c.get_login_report().aborted_reason == "login_actions did not finish within 0.1s"


def test_client_hook_gives_up_waiting_for_the_lock_on_stop(tmp_path, monkeypatch):
    monkeypatch.setattr(client_mod, "run_actions",
                        lambda *a, **k: pytest.fail("must not run without the lock"))
    lock = threading.Lock()
    lock.acquire()  # another capture on the profile is logging in
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    c = _client(tmp_path, login_lock=lock)

    c._on_login_wall(stop)

    rep = c.get_login_report()
    assert rep.aborted_reason == "cancelled while another capture on this profile was logging in"
    assert [r.error for r in rep.actions] == ["skipped", "skipped"]


def test_client_hook_report_is_a_placeholder_while_running(tmp_path, monkeypatch):
    started, release = threading.Event(), threading.Event()

    def blocking(port, actions, **kw):
        started.set()
        release.wait(5)
        return ActionsReport()

    monkeypatch.setattr(client_mod, "run_actions", blocking)
    c = _client(tmp_path)
    t = threading.Thread(target=c._on_login_wall, args=(threading.Event(),))
    t.start()
    assert started.wait(5)
    assert c.get_login_report().aborted_reason == "login_actions still running when the capture ended"
    release.set()
    t.join(5)
    assert c.get_login_report().ok


def test_client_without_login_actions_never_runs_anything(tmp_path, monkeypatch):
    monkeypatch.setattr(client_mod, "run_actions", lambda *a, **k: pytest.fail("ran"))
    c = _client(tmp_path, login_actions=())
    c._on_login_wall(threading.Event())
    assert c.get_login_report() is None
