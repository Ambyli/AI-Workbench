"""Tests for the interceptor service's ``login_actions`` + per-profile logins.

No browser is launched: ``InterceptorClient`` is replaced with a fake that
records what the service handed it and returns a canned login report. The
CDP side (the login gate, ``fill_origins``, the session hook) is covered by
``shared/common/tests/test_cdp_actions.py`` and ``test_cdp_login_hook.py``.

The point of most of these tests is that the secret goes exactly one place —
into the ``Action`` objects the client receives — and nowhere else: not the
response, not ``GET /jobs/{id}``, not the job log, not an error message.

Run from the repo root (in its own pytest process, not with shared/common):

    .venv/Scripts/python.exe -m pytest unit-tests/interceptor -q
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "ai", "interceptor"))

SECRET = "hunter2-SECRET-VALUE"
USER = "svc-enphase@zeoenergy.com"
PROFILE = "svcprof"

LOGIN_STEPS = [
    {"type": "fill", "selector": "input[type=email]", "value": "${username}"},
    {"type": "fill", "selector": "input[type=password]", "value": "${password}"},
    {"type": "click", "selector": "button[type=submit]"},
]
BASE = {
    "url": "https://app.example.com/feoc",
    "url_patterns": ["api/apex/execute"],
    "profile": PROFILE,
    "capture_window_seconds": 1,
    "login_url_patterns": [r"sso\.example\.com"],
    "login_actions": LOGIN_STEPS,
}


@pytest.fixture(scope="module")
def app_mod(tmp_path_factory):
    # profiles.PROFILES_ROOT is read at import; point it somewhere disposable
    # (a no-op when test_capture_actions.py already imported app).
    os.environ.setdefault("INTERCEPTOR_PROFILES_ROOT", str(tmp_path_factory.mktemp("profiles")))
    import app  # noqa: E402

    return app


@pytest.fixture()
def client(app_mod):
    from fastapi.testclient import TestClient

    return TestClient(app_mod.app)


USER_ENV = "INTERCEPTOR_LOGIN_SVCPROF_USERNAME"
PASS_ENV = "INTERCEPTOR_LOGIN_SVCPROF_PASSWORD"


@pytest.fixture()
def logins_dir(app_mod, tmp_path, monkeypatch):
    monkeypatch.setattr(app_mod.logins, "LOGINS_DIR", str(tmp_path))
    monkeypatch.setenv(USER_ENV, USER)
    monkeypatch.setenv(PASS_ENV, SECRET)
    _write(tmp_path, PROFILE, {
        "allowed_origins": ["HTTPS://SSO.Example.com:443/"],
        "values": {"username": f"${{ENV:{USER_ENV}}}", "password": f"${{ENV:{PASS_ENV}}}"},
    })
    return tmp_path


def _write(d, profile, doc) -> None:
    (d / f"{profile}.json").write_text(json.dumps(doc), encoding="utf-8")


def _pool_untouched(client, app_mod):
    jobs = client.get("/jobs").json()
    assert jobs["active_count"] == 0
    assert app_mod._port_pool.qsize() == app_mod.MAX_CONCURRENT


# ── logins.py ────────────────────────────────────────────────────────────────

def _parsed(steps):
    from common.cdp_interceptor import parse_actions

    return parse_actions(steps, label="login_actions")


def test_load_normalizes_origins_and_keeps_values(app_mod, logins_dir):
    cfg = app_mod.logins.load(PROFILE)
    assert cfg.allowed_origins == ("https://sso.example.com",)
    assert dict(cfg.values) == {"username": USER, "password": SECRET}
    assert SECRET not in repr(cfg)


def test_resolve_substitutes_and_escapes(app_mod, logins_dir):
    cfg = app_mod.logins.load(PROFILE)
    steps = _parsed([
        {"type": "fill", "selector": "#u", "value": "${username}"},
        {"type": "fill", "selector": "#p", "value": "pre-${password}-$$-$5-post"},
        {"type": "click", "selector": "[name$=submit]"},  # a CSS `$=` is not a reference
    ])
    assert app_mod.logins.referenced_keys(steps) == {"username", "password"}
    out = app_mod.logins.resolve(steps, cfg)
    assert [a.value for a in out[:2]] == [USER, f"pre-{SECRET}-$-$5-post"]
    assert out[2].selector == "[name$=submit]"
    assert steps[1].value == "pre-${password}-$$-$5-post"  # inputs untouched


@pytest.mark.parametrize("step, msg", [
    ({"type": "fill", "selector": "#p", "value": "${pin}"},
     r"login_actions\[0\] \(fill\) value: references \$\{pin\}, which the profile's logins "
     r"file does not define \(it defines: password, username\)"),
    ({"type": "fill", "selector": "#${password}", "value": "x"},
     r"login_actions\[0\] \(fill\): selector contains '\$\{' — credential references are only allowed"),
    ({"type": "click", "selector": "button", "text": "${username}"},
     r"login_actions\[0\] \(click\): text contains"),
    ({"type": "select", "selector": "select", "value": "${password}"},
     r"login_actions\[0\] \(select\): value contains"),
    ({"type": "fill", "selector": "#p", "value": "${password"}, "unterminated reference"),
    ({"type": "fill", "selector": "#p", "value": "${1abc}"}, "not a valid reference name"),
])
def test_resolve_rejects_bad_references_without_leaking(app_mod, logins_dir, step, msg):
    cfg = app_mod.logins.load(PROFILE)
    with pytest.raises(app_mod.logins.LoginConfigError, match=msg) as ei:
        app_mod.logins.resolve(_parsed([step]), cfg)
    assert SECRET not in str(ei.value) and USER not in str(ei.value)


@pytest.mark.parametrize("doc, msg", [
    ({"values": {"password": SECRET}}, "allowed_origins is required"),
    ({"allowed_origins": [], "values": {"password": SECRET}}, "allowed_origins is required"),
    ({"allowed_origins": ["sso.example.com"], "values": {}}, "is not an origin"),
    ({"allowed_origins": ["https://sso.example.com/login"], "values": {}}, "must be a bare origin"),
    ({"allowed_origins": ["https://a.example"], "values": {"password": 5}}, "values.password must be a string"),
    ({"allowed_origins": ["https://a.example"], "values": {"pass word": SECRET}}, "is not valid"),
    ({"allowed_origins": ["https://a.example"], "values": {}, "password": SECRET}, "unexpected key"),
])
def test_load_rejects_bad_files_without_leaking(app_mod, logins_dir, doc, msg):
    _write(logins_dir, "bad", doc)
    with pytest.raises(app_mod.logins.LoginConfigError, match=msg) as ei:
        app_mod.logins.load("bad")
    assert SECRET not in str(ei.value)


@pytest.mark.parametrize("value, msg", [
    (SECRET, "must be an environment reference"),
    ("${ENV:DEFAULT_LITELLM_MASTER_KEY}", "must be an environment reference"),
    ("${ENV:interceptor_login_lower}", "must be an environment reference"),
    (f"pre-${{ENV:{PASS_ENV}}}", "must be an environment reference"),
])
def test_load_refuses_values_that_are_not_login_env_refs(app_mod, logins_dir, value, msg):
    _write(logins_dir, "bad", {"allowed_origins": ["https://a.example"], "values": {"password": value}})
    with pytest.raises(app_mod.logins.LoginConfigError, match=msg) as ei:
        app_mod.logins.load("bad")
    assert SECRET not in str(ei.value)


def test_load_names_an_unset_or_empty_variable(app_mod, logins_dir, monkeypatch):
    monkeypatch.delenv(PASS_ENV)
    with pytest.raises(app_mod.logins.LoginConfigError, match=f"reads {PASS_ENV}, which is not set"):
        app_mod.logins.load(PROFILE)
    monkeypatch.setenv(PASS_ENV, "")
    with pytest.raises(app_mod.logins.LoginConfigError, match=f"reads {PASS_ENV}, which is not set"):
        app_mod.logins.load(PROFILE)
    info = app_mod.logins.describe(PROFILE)
    assert info["keys"] == [] and PASS_ENV in info["error"] and USER not in json.dumps(info)


def test_unset_variable_is_400_naming_it(client, app_mod, logins_dir, monkeypatch):
    monkeypatch.delenv(PASS_ENV)
    r = client.post("/capture", json=BASE)
    assert r.status_code == 400
    assert PASS_ENV in r.json()["detail"] and USER not in r.text


def test_checked_in_enphase_file_is_reference_only(app_mod, monkeypatch):
    here = os.path.join(_HERE, "..", "..", "ai", "interceptor", "logins")
    monkeypatch.setattr(app_mod.logins, "LOGINS_DIR", here)
    monkeypatch.setenv("INTERCEPTOR_LOGIN_ENPHASE_USERNAME", USER)
    monkeypatch.setenv("INTERCEPTOR_LOGIN_ENPHASE_PASSWORD", SECRET)
    cfg = app_mod.logins.load("enphase")
    assert cfg.allowed_origins == ("https://sso.enphaseenergy.com",)
    assert dict(cfg.values) == {"username": USER, "password": SECRET}


def test_load_missing_file_and_invalid_json(app_mod, logins_dir):
    with pytest.raises(app_mod.logins.LoginConfigError, match="has no logins file"):
        app_mod.logins.load("nofile")
    (logins_dir / "broken.json").write_text('{"values": {"password": "' + SECRET, encoding="utf-8")
    with pytest.raises(app_mod.logins.LoginConfigError, match="not valid JSON") as ei:
        app_mod.logins.load("broken")
    assert SECRET not in str(ei.value)


def test_describe_never_returns_values(app_mod, logins_dir):
    assert app_mod.logins.describe(PROFILE) == {
        "keys": ["password", "username"], "allowed_origins": ["https://sso.example.com"],
    }
    assert app_mod.logins.describe("nofile") == {"keys": [], "allowed_origins": []}
    _write(logins_dir, "bad", {"allowed_origins": [], "values": {"password": SECRET}})
    bad = app_mod.logins.describe("bad")
    assert bad["keys"] == [] and "allowed_origins is required" in bad["error"]
    assert SECRET not in json.dumps(bad)


# ── /capture validation: every refusal before a port is taken ────────────────

def test_missing_logins_file_is_400(client, app_mod, logins_dir):
    r = client.post("/capture", json={**BASE, "profile": "nofile"})
    assert r.status_code == 400
    assert "profile 'nofile' has no logins file" in r.json()["detail"]
    _pool_untouched(client, app_mod)


def test_missing_key_is_400_naming_the_key(client, app_mod, logins_dir):
    r = client.post("/capture", json={**BASE, "login_actions": [
        {"type": "fill", "selector": "#pin", "value": "${pin}"},
    ]})
    assert r.status_code == 400
    assert "${pin}" in r.json()["detail"] and SECRET not in r.text
    _pool_untouched(client, app_mod)


def test_reference_outside_fill_value_is_400(client, app_mod, logins_dir):
    r = client.post("/capture", json={**BASE, "login_actions": [
        {"type": "wait_for", "selector": "input[value='${password}']"},
    ]})
    assert r.status_code == 400
    assert "login_actions[0] (wait_for): selector contains" in r.json()["detail"]
    _pool_untouched(client, app_mod)


def test_evaluate_in_login_actions_is_refused(client, app_mod, logins_dir):
    # Not in the LoginActionModel union, so pydantic refuses it (422) before
    # the library's allowed_types check (400) would.
    r = client.post("/capture", json={**BASE, "login_actions": [
        {"type": "evaluate", "script": "document.querySelector('input[type=password]').value"},
    ]})
    assert r.status_code == 422
    assert "login_actions" in json.dumps(r.json()["detail"])
    _pool_untouched(client, app_mod)


def test_screenshot_in_login_actions_is_refused(client, app_mod, logins_dir):
    # An image of the login form would carry the username into the response.
    r = client.post("/capture", json={**BASE, "login_actions": [
        *LOGIN_STEPS[:2], {"type": "screenshot"}, LOGIN_STEPS[2],
    ]})
    assert r.status_code == 422
    assert "login_actions" in json.dumps(r.json()["detail"])
    _pool_untouched(client, app_mod)


def test_login_actions_need_login_detection(client, app_mod, logins_dir):
    r = client.post("/capture", json={**BASE, "login_url_patterns": []})
    assert r.status_code == 422 and "login_url_patterns" in r.text
    # Same rule for a screenshot-only capture.
    r = client.post("/capture", json={
        "url": "https://e.com", "profile": PROFILE, "url_patterns": [],
        "actions": [{"type": "screenshot"}], "login_url_patterns": [],
        "login_actions": LOGIN_STEPS,
    })
    assert r.status_code == 422 and "login_url_patterns" in r.text
    _pool_untouched(client, app_mod)


# ── The capture, against a fake client ───────────────────────────────────────

def _login_report(error=None):
    from common.cdp_interceptor import ActionResult, ActionsReport

    if error == "cancelled":
        return ActionsReport.not_run(_parsed(LOGIN_STEPS), None, "cancelled")
    return ActionsReport(actions=[
        ActionResult(index=0, type="fill", ok=True, elapsed_ms=5,
                     value={"element": "<input>", "value_length": len(USER)}),
        ActionResult(index=1, type="fill", ok=True, elapsed_ms=5,
                     value={"element": "<input>", "value_length": len(SECRET)}),
        ActionResult(index=2, type="click", ok=True, elapsed_ms=5,
                     value={"element": "<button>", "x": 1.0, "y": 2.0}),
    ])


class _LoginFake:
    """InterceptorClient stand-in. ``login_report`` is what get_login_report
    returns — None means the capture never hit a login wall."""

    instances: list["_LoginFake"] = []
    login_report = None

    def __init__(self, *, on_capture, on_status, **kw):
        self.kw = kw
        self._on_status = on_status
        _LoginFake.instances.append(self)

    def launch(self, target_url):
        # What the real session would log on a wall; must not carry secrets.
        self._on_status("waiting_login", None)

    def run_actions(self, actions, **kw):
        from common.cdp_interceptor import ActionsReport

        return ActionsReport()

    def get_state(self):
        from common.cdp_interceptor import ClientState

        return ClientState(status="ok", headless=True, error=None, last_capture_at=None)

    def get_login_report(self):
        return type(self).login_report

    def quit(self):
        pass


@pytest.fixture()
def fake(app_mod, monkeypatch):
    _LoginFake.instances = []
    _LoginFake.login_report = None
    monkeypatch.setattr(app_mod, "InterceptorClient", _LoginFake)
    return _LoginFake


def test_client_receives_resolved_values_origins_lock_and_timeout(client, app_mod, logins_dir, fake):
    r = client.post("/capture", json={**BASE, "login_actions_timeout_seconds": 90})
    assert r.status_code == 200, r.text
    kw = fake.instances[0].kw
    assert [a.value for a in kw["login_actions"]] == [USER, SECRET, None]
    assert kw["login_fill_origins"] == ("https://sso.example.com",)
    assert kw["login_actions_timeout_s"] == 90.0
    assert kw["login_lock"] is app_mod._get_login_lock(PROFILE)
    # No wall → no report, even though login_actions were sent.
    assert r.json()["login_actions_report"] is None
    _pool_untouched(client, app_mod)


def test_default_login_timeout_is_60(client, app_mod, logins_dir, fake):
    assert client.post("/capture", json=BASE).status_code == 200
    assert fake.instances[0].kw["login_actions_timeout_s"] == 60.0


def test_secret_never_leaves_the_client(client, app_mod, logins_dir, fake, capfd):
    fake.login_report = _login_report()
    box: dict = {}

    def post():
        box["r"] = client.post("/capture", json={**BASE, "capture_window_seconds": 2})

    t = threading.Thread(target=post)
    t.start()
    job_views: list[str] = []
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not job_views:
        jobs = client.get("/jobs").json()["jobs"]
        if jobs:
            job_views.append(json.dumps(jobs))
            job_views.append(client.get(f"/jobs/{jobs[0]['job_id']}").text)
        time.sleep(0.05)
    t.join(10)

    r = box["r"]
    assert r.status_code == 200, r.text
    assert job_views, "the capture never showed up in GET /jobs"
    rep = r.json()["login_actions_report"]
    assert rep["aborted_reason"] is None
    assert rep["actions"][1]["value"] == {"element": "<input>", "value_length": len(SECRET)}
    out, err = capfd.readouterr()
    assert "login_actions ok=True failed_at=-" in err
    for text in (r.text, *job_views, out, err):
        assert SECRET not in text and USER not in text
    _pool_untouched(client, app_mod)


def test_window_end_relabels_a_cancelled_login(client, app_mod, logins_dir, fake, capfd):
    fake.login_report = _login_report("cancelled")
    r = client.post("/capture", json=BASE)
    rep = r.json()["login_actions_report"]
    assert rep["aborted_reason"] == "capture window ended before login_actions finished"
    assert [a["error"] for a in rep["actions"]] == ["skipped"] * 3
    assert "login_actions ok=False failed_at=0/fill" in capfd.readouterr().err


def test_screenshot_only_capture_passes_login_actions_through(client, app_mod, logins_dir, fake):
    fake.login_report = _login_report()
    r = client.post("/capture", json={
        "url": "https://app.example.com", "profile": PROFILE, "url_patterns": [],
        "capture_window_seconds": 1, "actions": [{"type": "screenshot"}],
        "login_url_patterns": [r"sso\.example\.com"], "login_actions": LOGIN_STEPS,
    })
    assert r.status_code == 200, r.text
    assert [a.value for a in fake.instances[0].kw["login_actions"]][:2] == [USER, SECRET]
    assert r.json()["login_actions_report"]["aborted_reason"] is None
    assert SECRET not in r.text and USER not in r.text


# ── MCP + profile listing ────────────────────────────────────────────────────

def test_mcp_tools_pass_login_actions_through(app_mod, monkeypatch):
    seen: list = []

    def _stop(req):
        seen.append(req)
        raise app_mod.HTTPException(status_code=429, detail="stop here")

    monkeypatch.setattr(app_mod, "_run_capture", _stop)
    sso = [r"sso\.enphaseenergy\.com"]

    res = app_mod.capture_url(url="https://e.com", url_patterns=["x"], profile="p",
                              login_url_patterns=sso, login_actions=LOGIN_STEPS)
    assert res.structured_content["login_actions_report"] is None
    app_mod.capture_url(url="https://e.com", profile="p", actions=[{"type": "screenshot"}],
                        login_url_patterns=sso, login_actions=LOGIN_STEPS)
    app_mod.capture_url(url="https://e.com", url_patterns=["x"], profile="p")

    # The references travel as-is; resolution happens inside _run_capture.
    for req in seen[:2]:
        assert [(a.type, getattr(a, "value", None)) for a in req.login_actions] == [
            ("fill", "${username}"), ("fill", "${password}"), ("click", None),
        ]
        assert req.login_url_patterns == sso
    assert seen[2].login_actions == []


def test_mcp_capture_url_reports_an_evaluate_login_step_in_the_payload(app_mod):
    res = app_mod.capture_url(
        url="https://e.com", url_patterns=["x"], profile="p",
        login_actions=[{"type": "evaluate", "script": "1"}],
    )
    payload = res.structured_content
    assert payload["status"] == "error" and "login_actions.0" in payload["error"]
    assert app_mod._port_pool.qsize() == app_mod.MAX_CONCURRENT
    res = app_mod.capture_url(
        url="https://e.com", url_patterns=["x"], profile="p",
        login_actions=[{"type": "screenshot"}],
    )
    assert res.structured_content["status"] == "error"
    assert "login_actions.0" in res.structured_content["error"]


def test_profiles_listing_shows_login_keys_not_values(client, app_mod, logins_dir):
    root = app_mod.profiles.PROFILES_ROOT
    os.makedirs(os.path.join(root, PROFILE), exist_ok=True)
    os.makedirs(os.path.join(root, "nologin"), exist_ok=True)

    listing = client.get("/profiles")
    assert listing.status_code == 200
    by_name = {p["name"]: p for p in listing.json()["profiles"]}
    assert by_name[PROFILE]["login_keys"] == ["password", "username"]
    assert by_name[PROFILE]["login_origins"] == ["https://sso.example.com"]
    assert by_name["nologin"]["login_keys"] == [] and "login_error" not in by_name["nologin"]
    one = client.get(f"/profiles/{PROFILE}").json()
    assert one["login_keys"] == ["password", "username"]
    mcp = app_mod.list_profiles()
    assert {p["name"]: p for p in mcp["profiles"]}[PROFILE]["login_origins"] == ["https://sso.example.com"]
    for text in (listing.text, json.dumps(one), json.dumps(mcp)):
        assert SECRET not in text and USER not in text
