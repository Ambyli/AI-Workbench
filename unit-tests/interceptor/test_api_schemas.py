"""``interceptor.schemas.json`` against the code (root ``CLAUDE.md``
§ Postman collections; the file-wide checks -- coverage, valid schemas, every
collection body, the rendered copies -- are ``unit-tests/apidocs``).

(a) **The generated schemas are current**: ``POST /capture``'s request
    (``app.CaptureRequest``) and response (``app.CaptureResponse``), the
    shared jobs models (``common.jobs.model.JobBase`` / ``JobsListResponse``)
    and the interceptor's job ``metadata`` (``app.InterceptorMetadata``), and
    each MCP tool's arguments (its FastMCP ``inputSchema``).
    ``API_SCHEMAS_UPDATE=1`` rewrites them and re-renders the copies::

        API_SCHEMAS_UPDATE=1 UV_LINK_MODE=copy uv run --no-sync python -m pytest \\
            unit-tests/interceptor/test_api_schemas.py -q -p no:cacheprovider

(b) **Real responses validate**, with no browser: ``InterceptorClient`` is
    replaced by a fake that "captures" a body, reports each action step (a
    screenshot step an image) and, when asked, a login report, so every
    request runs the real validation, port pool, profile lock / clone, job
    registry, capture window and response building:

    * ``/health``; the profile routes on a temporary profiles root and logins
      dir -- an uploaded profile (a real ``.tar.gz``), an empty one, a missing
      one, a usable and an unusable logins file -- and their errors;
    * ``POST /capture`` -- plain, actions with screenshots, login actions,
      screenshot-only, every collection body, a cancelled capture -- and its
      errors (400 / 422 / 429 / 500), which must be the generic body;
    * ``/jobs`` while captures are in flight (fast path, a slow-path clone,
      ``actions``), ``/jobs/{id}``, the cancel and its 404 / 409;
    * MCP, in memory through ``fastmcp.Client``: ``initialize``,
      ``tools/list`` and every tool's ``structuredContent`` (a result and an
      error-as-data each).

    The last test checks that every declared status was produced.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import pathlib
import sys
import tarfile
import threading
import time
from typing import Any, Dict

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVICE_DIR = ROOT / "ai" / "interceptor"
sys.path.insert(0, str(SERVICE_DIR))

from common.apidocs import generate, postman, render, schemas  # noqa: E402

SCHEMAS_PATH = SERVICE_DIR / "interceptor.schemas.json"
COLLECTION_PATH = SERVICE_DIR / "interceptor.postman_collection.json"
PREFIX = "/v1/interceptor"         # the LiteLLM pass-through the collection calls through

# What a status with no entry must answer: FastAPI's {"detail": "<str>"}, or
# its 422 list.
GENERIC_ERROR = {
    "type": "object", "additionalProperties": False, "required": ["detail"],
    "properties": {"detail": {"anyOf": [
        {"type": "string"},
        {"type": "array", "items": {"type": "object", "required": ["loc", "msg", "type"]}},
    ]}},
}

SEEN: set = set()
RAN: set = set()
PRODUCERS = {"health", "profiles", "capture", "jobs", "mcp"}


@pytest.fixture(scope="module")
def app_module(tmp_path_factory):
    # profiles.PROFILES_ROOT is read at import (a no-op when another test
    # module imported app first); each test points it at its own directory.
    os.environ.setdefault("INTERCEPTOR_PROFILES_ROOT", str(tmp_path_factory.mktemp("profiles")))
    import app

    return app


@pytest.fixture(scope="module")
def schema_file():
    return schemas.load_schemas(SCHEMAS_PATH)


# -- (a) the generated schemas ---------------------------------------------------------------


def generated_schemas(app_module) -> Dict[tuple, Any]:
    from common.jobs.model import JobBase, JobsListResponse

    out: Dict[tuple, Any] = {
        ("endpoints", f"POST {PREFIX}/capture", "request", "schema"): generate.from_pydantic(app_module.CaptureRequest),
        ("$defs", "capture_response"): generate.from_pydantic(app_module.CaptureResponse, mode="serialization"),
        ("$defs", "interceptor_metadata"): generate.from_pydantic(app_module.InterceptorMetadata,
                                                                  mode="serialization"),
        ("$defs", "job_base"): generate.from_pydantic(JobBase, mode="serialization"),
        ("$defs", "jobs_list_response"): generate.from_pydantic(JobsListResponse, mode="serialization"),
    }
    for name, schema in asyncio.run(generate.from_mcp(app_module.mcp, source="app.mcp")).items():
        out[("endpoints", f"MCP {name}", "arguments")] = schema
    return out


def test_generated_schemas_are_current(app_module):
    data = json.loads(postman.read_text(SCHEMAS_PATH))
    changed = generate.apply_generated(data, generated_schemas(app_module))
    if changed and os.environ.get("API_SCHEMAS_UPDATE") == "1":
        schemas.dump_schemas(data, SCHEMAS_PATH)
        render.render_service(SCHEMAS_PATH)
        changed = []
    assert changed == [], (f"{SCHEMAS_PATH.name} is stale at {changed}: rerun this test with "
                           "API_SCHEMAS_UPDATE=1 (it rewrites them and re-renders the copies)")


# -- checking a response --------------------------------------------------------------------------


def check(schema_file, method: str, path: str, response) -> Any:
    """Validate ``response`` against the entry of ``method <PREFIX>path``
    (the collection's path, ``:params`` included). Returns the parsed body."""
    key = f"{method} {PREFIX}{path}"
    endpoint = schema_file.endpoints[key]
    status = str(response.status_code)
    spec = endpoint.responses().get(status)
    if spec is None:
        assert response.status_code >= 400, f"{key}: {status} has no entry in {SCHEMAS_PATH.name}"
        errors = schemas.validate(GENERIC_ERROR, response.json())
        assert errors == [], f"{key}: {status} is not the generic error body, so it needs an entry: {errors}"
        return response.json()
    SEEN.add((key, status))
    media = response.headers.get("content-type", "").split(";")[0].strip().lower()
    assert media == spec.content_type, (key, status, media)
    body = response.json()
    errors = schemas.validate(endpoint.standalone(spec.schema), body)
    assert errors == [], f"{key} {status}: {errors}"
    return body


def check_mcp(schema_file, key: str, result: Dict[str, Any]) -> Any:
    """``result`` (a JSON-RPC result as sent) against an MCP entry: a
    ``tools/call``'s ``structuredContent`` (whose JSON is also the first text
    block), another method's whole result."""
    endpoint = schema_file.endpoints[key]
    schema = endpoint.standalone(endpoint.entry["result"])
    if "arguments" not in endpoint.entry:
        errors = schemas.validate(schema, result)
        assert errors == [], (key, errors)
        SEEN.add((key, "result"))
        return result
    assert result.get("isError") is not True, result
    structured = result["structuredContent"]
    errors = schemas.validate(schema, structured)
    assert errors == [], (key, errors)
    assert json.loads(result["content"][0]["text"]) == structured
    SEEN.add((key, "result"))
    return structured


# -- the fake browser -------------------------------------------------------------------------------

MATCH_URL = "https://example.com/webruntime/api/apex/execute?cacheable=false"
JPEG = b"\xff\xd8\xff\xe0fake-jpeg"


class FakeChrome:
    """``InterceptorClient`` stand-in. ``launch`` captures one JSON body
    after ``capture_after`` s; ``run_actions`` reports every step (a
    screenshot step an image, the step at ``fail_index`` a failure) -- or,
    with ``block_actions``, waits for the cancel and reports them skipped;
    ``get_login_report`` returns ``login_report``."""

    capture_after = 0.05
    block_actions = False
    fail_index = None
    login_report = None
    launch_error = None

    def __init__(self, *, on_capture, on_status, **kw):
        self._on_capture = on_capture
        self._on_status = on_status

    def launch(self, target_url):
        from common.cdp_interceptor import Capture

        if type(self).launch_error is not None:
            raise type(self).launch_error

        def fire():
            time.sleep(type(self).capture_after)
            self._on_capture(Capture(url=MATCH_URL, body={"returnValue": {"compliant": True, "serial": "1"}}))

        threading.Thread(target=fire, daemon=True).start()

    def run_actions(self, actions, *, page_script=None, cancel=None, on_progress=None, **kw):
        from common.cdp_interceptor import ActionResult, ActionsReport

        if type(self).block_actions:
            while not cancel():
                time.sleep(0.02)
            return ActionsReport.not_run(actions, page_script, "cancelled")
        script = None
        if page_script is not None:
            script = ActionResult(index=-1, type="page_script", ok=True, elapsed_ms=3, value=["serial"])
        out = []
        for i, a in enumerate(actions):
            if i == type(self).fail_index:
                out.append(ActionResult(index=i, type=a.type, ok=False, elapsed_ms=15004,
                                        error="timed out after 15s waiting for '.result' (visible)"))
            elif a.type == "screenshot":
                out.append(ActionResult(index=i, type="screenshot", ok=True, elapsed_ms=380, value={
                    "format": a.format, "mime_type": f"image/{a.format}", "width": 960, "height": 540,
                    "full_page": a.full_page, "bytes": len(JPEG), "page_url": f"https://example.com/step{i}",
                    "data_base64": base64.b64encode(JPEG).decode()}))
            elif a.type == "fill":
                out.append(ActionResult(index=i, type="fill", ok=True, elapsed_ms=220,
                                        value={"element": "<textarea>", "value_length": len(a.value or "")}))
            else:
                out.append(ActionResult(index=i, type=a.type, ok=True, elapsed_ms=5))
            if on_progress:
                on_progress(i + 1, len(actions))
        reason = None if type(self).fail_index is None else f"actions[{type(self).fail_index}] failed: timed out"
        return ActionsReport(page_script=script, actions=out, aborted_reason=reason)

    def get_state(self):
        from common.cdp_interceptor import ClientState

        return ClientState(status="ok", headless=True, error=None, last_capture_at=None)

    def get_login_report(self):
        return type(self).login_report

    def quit(self):
        pass


def login_report():
    from common.cdp_interceptor import ActionResult, ActionsReport

    return ActionsReport(actions=[
        ActionResult(index=0, type="fill", ok=True, elapsed_ms=5, value={"element": "<input>", "value_length": 9}),
        ActionResult(index=1, type="fill", ok=True, elapsed_ms=5, value={"element": "<input>", "value_length": 7}),
        ActionResult(index=2, type="click", ok=True, elapsed_ms=5,
                     value={"element": "<input type=submit>", "x": 10.0, "y": 20.0}),
    ])


@pytest.fixture
def env(app_module, monkeypatch, tmp_path):
    """A fresh profiles root (``example`` and ``busy`` exist, ``example``
    with a usable logins file and ``broken`` with an unusable one) and the
    fake browser."""
    for attr, value in (("block_actions", False), ("fail_index", None), ("login_report", None),
                        ("launch_error", None)):
        monkeypatch.setattr(FakeChrome, attr, value)
    root = tmp_path / "profiles"
    logins_dir = tmp_path / "logins"
    for d in (root / "example", root / "busy", logins_dir):
        d.mkdir(parents=True)
    (root / "example" / "Local State").write_text("{}", encoding="utf-8")
    (root / "example" / "session_ok").touch()
    monkeypatch.setattr(app_module.profiles, "PROFILES_ROOT", str(root))
    monkeypatch.setattr(app_module.profiles, "TEMP_ROOT", root / ".temp")
    monkeypatch.setattr(app_module.logins, "LOGINS_DIR", str(logins_dir))
    monkeypatch.setenv("INTERCEPTOR_LOGIN_EXAMPLE_USERNAME", "svc-user1")
    monkeypatch.setenv("INTERCEPTOR_LOGIN_EXAMPLE_PASSWORD", "secret7")
    (logins_dir / "example.json").write_text(json.dumps({
        "allowed_origins": ["https://sso.enphaseenergy.com"],
        "values": {"username": "${ENV:INTERCEPTOR_LOGIN_EXAMPLE_USERNAME}",
                   "password": "${ENV:INTERCEPTOR_LOGIN_EXAMPLE_PASSWORD}"},
    }), encoding="utf-8")
    (logins_dir / "broken.json").write_text(json.dumps({"values": {"username": "literal"}}), encoding="utf-8")
    monkeypatch.setattr(app_module, "InterceptorClient", FakeChrome)
    return app_module


def client_of(app_module):
    from fastapi.testclient import TestClient

    return TestClient(app_module.app)            # no `with`: the lifespan (temp sweep + MCP) is not needed


def wait_for_jobs(client, n: int, predicate=lambda jobs: True, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        jobs = client.get("/jobs").json()["jobs"]
        if len(jobs) >= n and predicate(jobs):
            return jobs
        time.sleep(0.02)
    raise AssertionError(f"never saw {n} job(s) in GET /jobs")


def in_thread(fn):
    box: Dict[str, Any] = {}
    thread = threading.Thread(target=lambda: box.setdefault("r", fn()), daemon=True)
    thread.start()
    return thread, box


def tgz(files: Dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


# -- (b) producers ------------------------------------------------------------------------------------


def test_health(schema_file, app_module):
    assert check(schema_file, "GET", "/health", client_of(app_module).get("/health")) == {"status": "ok"}
    RAN.add("health")


def test_profiles(schema_file, env):
    client = client_of(env)
    archive = tgz({"Default/Cookies": b"sqlite", "Local State": b"{}"})
    body = check(schema_file, "POST", "/profiles/:profileName/refresh",
                 client.post("/profiles/fresh/refresh", files={"archive": ("fresh.tgz", archive, "application/gzip")}))
    assert body["unpacked"] is True and body["sentinel_present"] is True
    (env.profiles.profile_path("broken")).mkdir()
    listing = check(schema_file, "GET", "/profiles", client.get("/profiles"))
    by_name = {p["name"]: p for p in listing["profiles"]}
    assert set(by_name) == {"busy", "broken", "example", "fresh"}
    assert by_name["example"]["login_keys"] == ["password", "username"] and "login_error" in by_name["broken"]
    assert by_name["busy"]["present"] is False and by_name["busy"]["sentinel_present"] is False
    assert check(schema_file, "GET", "/profiles/:profileName", client.get("/profiles/example"))["present"] is True
    missing = check(schema_file, "GET", "/profiles/:profileName", client.get("/profiles/nosuch"))
    assert "sentinel_present" not in missing and missing["size_bytes"] == 0
    assert check(schema_file, "DELETE", "/profiles/:profileName", client.delete("/profiles/fresh"))["deleted"] is True
    assert check(schema_file, "DELETE", "/profiles/:profileName", client.delete("/profiles/fresh"))["deleted"] is False
    for method, path, response in (
        ("GET", "/profiles/:profileName", client.get("/profiles/Bad.Name")),
        ("DELETE", "/profiles/:profileName", client.delete("/profiles/Bad.Name")),
        ("POST", "/profiles/:profileName/refresh",
         client.post("/profiles/Bad.Name/refresh", files={"archive": ("x.tgz", archive, "application/gzip")})),
        ("POST", "/profiles/:profileName/refresh",
         client.post("/profiles/junk/refresh", files={"archive": ("x.zip", b"PK\x03\x04 not a tar", "application/zip")})),
        ("POST", "/profiles/:profileName/refresh", client.post("/profiles/junk/refresh", files={"other": ("x", b"x")})),
    ):
        assert response.status_code in (400, 422), response.text
        check(schema_file, method, path, response)
    RAN.add("profiles")


CAPTURE = {"url": "https://example.com/feoc", "url_patterns": ["webruntime/api/apex/execute"], "profile": "example"}


def test_capture(schema_file, env, monkeypatch):
    client = client_of(env)
    plain = check(schema_file, "POST", "/capture", client.post("/capture", json={**CAPTURE, "capture_window_seconds": 1}))
    assert plain["status"] == "ok" and plain["actions_report"] is None and len(plain["matches"][CAPTURE["url_patterns"][0]]) == 1
    driven = check(schema_file, "POST", "/capture", client.post("/capture", json={
        **CAPTURE, "capture_window_seconds": 10, "stop_when_matched": True, "page_script": "document.title",
        "actions": [{"type": "fill", "selector": "textarea", "value": "532614044013"},
                    {"type": "click", "selector": "button", "text": "Submit"},
                    {"type": "screenshot", "scale": 0.5}]}))
    assert driven["ended_early"] is True and driven["actions_report"]["actions"][2]["value"]["data_base64"]
    monkeypatch.setattr(FakeChrome, "fail_index", 1)
    failed = check(schema_file, "POST", "/capture", client.post("/capture", json={
        **CAPTURE, "capture_window_seconds": 1,
        "actions": [{"type": "wait", "seconds": 0}, {"type": "wait_for", "selector": ".result"}]}))
    assert failed["actions_report"]["aborted_reason"].startswith("actions[1]")
    monkeypatch.setattr(FakeChrome, "fail_index", None)
    monkeypatch.setattr(FakeChrome, "login_report", login_report())
    login = check(schema_file, "POST", "/capture", client.post("/capture", json={
        **CAPTURE, "capture_window_seconds": 10, "stop_when_matched": True,
        "login_url_patterns": [r"sso\.enphaseenergy\.com"],
        "login_actions": [{"type": "fill", "selector": "#username", "value": "${username}"},
                          {"type": "fill", "selector": "#password", "value": "${password}"},
                          {"type": "click", "selector": "input[type=submit]"}]}))
    assert login["login_actions_report"]["actions"][1]["value"] == {"element": "<input>", "value_length": 7}
    monkeypatch.setattr(FakeChrome, "login_report", None)
    shot = check(schema_file, "POST", "/capture", client.post("/capture", json={
        "url": "https://example.com", "profile": "example", "actions": [{"type": "screenshot"}]}))
    assert shot["matches"] == {} and shot["ended_early"] is True

    # Every collection body (each window capped: the fake page never stops on its own).
    collection = postman.load(COLLECTION_PATH)
    values = postman.variables(collection)
    monkeypatch.setattr(FakeChrome, "login_report", login_report())
    sent_any = 0
    for req in postman.requests(collection):
        if postman.endpoint_key(req) != f"POST {PREFIX}/capture":
            continue
        sent = postman.body(req, values).value
        response = client.post("/capture", json={**sent, "capture_window_seconds": 2})
        assert response.status_code == 200, (req.label, response.text)
        check(schema_file, "POST", "/capture", response)
        sent_any += 1
    assert sent_any == 4

    # The errors: the generic body each.
    monkeypatch.setattr(FakeChrome, "login_report", None)
    for payload in (
        {**CAPTURE, "profile": "Bad.Name"},
        {**CAPTURE, "url_patterns": ["("]},
        {**CAPTURE, "login_url_patterns": ["("]},
        {**CAPTURE, "actions": [{"type": "press", "key": "NotAKey"}]},
        {**CAPTURE, "profile": "busy", "login_actions": [{"type": "click", "selector": "button"}]},
        {**CAPTURE, "login_actions": [{"type": "fill", "selector": "#p", "value": "${pin}"}]},
    ):
        response = client.post("/capture", json=payload)
        assert response.status_code == 400, (payload, response.text)
        check(schema_file, "POST", "/capture", response)
    for payload in ({"url": "https://e.com"}, {**CAPTURE, "url_patterns": []},
                    {**CAPTURE, "actions": [{"type": "hover"}]}):
        response = client.post("/capture", json=payload)
        assert response.status_code == 422, response.text
        check(schema_file, "POST", "/capture", response)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(env, "_acquire_port", lambda: None)
        response = client.post("/capture", json=CAPTURE)
        assert response.status_code == 429
        check(schema_file, "POST", "/capture", response)
    from common.cdp_interceptor import BrowserNotFoundError

    monkeypatch.setattr(FakeChrome, "launch_error", BrowserNotFoundError("no Chromium-family browser found"))
    response = client.post("/capture", json=CAPTURE)
    assert response.status_code == 500
    check(schema_file, "POST", "/capture", response)
    assert env._port_pool.qsize() == env.MAX_CONCURRENT
    RAN.add("capture")


def test_jobs_in_flight_and_cancel(schema_file, env, monkeypatch):
    client = client_of(env)
    check(schema_file, "GET", "/jobs", client.get("/jobs"))
    # A fast-path capture on `busy`, then a second on the same profile: a slow-path clone.
    first, first_box = in_thread(lambda: client.post("/capture", json={**CAPTURE, "profile": "busy",
                                                                         "capture_window_seconds": 30}))
    wait_for_jobs(client, 1)
    second, second_box = in_thread(lambda: client.post("/capture", json={**CAPTURE, "profile": "busy",
                                                                           "capture_window_seconds": 30}))
    jobs = wait_for_jobs(client, 2, lambda js: any(j["metadata"]["temp_dir"] for j in js))
    listing = check(schema_file, "GET", "/jobs", client.get("/jobs"))
    assert listing["active_count"] == 2 and {j["metadata"]["used_base_profile"] for j in listing["jobs"]} == {True, False}
    one = check(schema_file, "GET", "/jobs/:jobId", client.get(f"/jobs/{jobs[0]['job_id']}"))
    assert one["phase"] == "capturing" and one["result"] is None
    mcp_job = asyncio.run(mcp_calls(env.mcp, [("get_job", {"job_id": one["job_id"]}), ("list_jobs", {})]))
    check_mcp(schema_file, "MCP get_job", mcp_job[0])
    check_mcp(schema_file, "MCP list_jobs", mcp_job[1])
    for job in jobs:
        cancelled = check(schema_file, "POST", "/jobs/:jobId/cancel", client.post(f"/jobs/{job['job_id']}/cancel"))
        assert cancelled["was_phase"] == "capturing"
    for thread, box in ((first, first_box), (second, second_box)):
        thread.join(10)
        assert check(schema_file, "POST", "/capture", box["r"])["status"] == "cancelled"
    # A capture whose actions are still running: phase `actions`, counters set.
    monkeypatch.setattr(FakeChrome, "block_actions", True)
    third, third_box = in_thread(lambda: client.post("/capture", json={
        **CAPTURE, "capture_window_seconds": 30, "page_script": "1",
        "actions": [{"type": "click", "selector": "button"}]}))
    (job,) = wait_for_jobs(client, 1, lambda js: js[0]["phase"] == "actions")
    shown = check(schema_file, "GET", "/jobs/:jobId", client.get(f"/jobs/{job['job_id']}"))
    assert shown["metadata"]["actions_total"] == 1
    assert check(schema_file, "POST", "/jobs/:jobId/cancel",
                 client.post(f"/jobs/{job['job_id']}/cancel"))["was_phase"] == "actions"
    third.join(10)
    report = check(schema_file, "POST", "/capture", third_box["r"])["actions_report"]
    assert report["aborted_reason"] == "cancelled" and report["page_script"]["error"] == "skipped"
    # The errors: 404 unknown, 409 a job already cleaning up.
    assert client.get("/jobs/000000000000").status_code == 404
    check(schema_file, "GET", "/jobs/:jobId", client.get("/jobs/000000000000"))
    check(schema_file, "POST", "/jobs/:jobId/cancel", client.post("/jobs/000000000000/cancel"))
    handle = env._registry.register(env.InterceptorMetadata(profile="busy", url="https://e.com", port=1,
                                                            used_base_profile=True), initial_phase="cleaning_up")
    try:
        response = client.post(f"/jobs/{handle.job_id}/cancel")
        assert response.status_code == 409
        check(schema_file, "POST", "/jobs/:jobId/cancel", response)
    finally:
        env._registry.unregister(handle.job_id)
    assert env._port_pool.qsize() == env.MAX_CONCURRENT
    RAN.add("jobs")


# -- MCP, in memory ----------------------------------------------------------------------------------


def _wire(model) -> Dict[str, Any]:
    return model.model_dump(mode="json", by_alias=True, exclude_none=True)


async def mcp_calls(mcp, calls):
    from fastmcp import Client

    async with Client(mcp) as client:
        return [_wire(await client.call_tool_mcp(name, arguments)) for name, arguments in calls]


def test_mcp_tools(schema_file, env, monkeypatch):
    from fastmcp import Client

    collection = postman.load(COLLECTION_PATH)
    values = postman.variables(collection)
    (item,) = [r for r in postman.requests(collection) if postman.endpoint_key(r) == "MCP capture_url"]
    collection_args = postman.mcp_instance(item, values)

    async def go():
        async with Client(env.mcp) as client:
            check_mcp(schema_file, "MCP initialize", _wire(client.initialize_result))
            listed = check_mcp(schema_file, "MCP tools/list", _wire(await client.list_tools_mcp()))
            assert {t["name"] for t in listed["tools"]} == {"capture_url", "list_profiles", "list_jobs", "get_job"}
            out = {}
            raw = _wire(await client.call_tool_mcp("capture_url", {
                "url": "https://example.com", "profile": "example",
                "actions": [{"type": "scroll", "to": "top"}, {"type": "screenshot", "scale": 0.5}]}))
            out["shot"] = (raw, check_mcp(schema_file, "MCP capture_url", raw))
            raw = _wire(await client.call_tool_mcp("capture_url", {**collection_args, "capture_window_seconds": 1}))
            out["collection"] = check_mcp(schema_file, "MCP capture_url", raw)
            out["error"] = check_mcp(schema_file, "MCP capture_url", _wire(await client.call_tool_mcp(
                "capture_url", {"url": "https://example.com", "profile": "example"})))
            out["bad_step"] = check_mcp(schema_file, "MCP capture_url", _wire(await client.call_tool_mcp(
                "capture_url", {**CAPTURE, "actions": [{"type": "click"}]})))
            out["profiles"] = check_mcp(schema_file, "MCP list_profiles",
                                        _wire(await client.call_tool_mcp("list_profiles", {})))
            out["jobs"] = check_mcp(schema_file, "MCP list_jobs", _wire(await client.call_tool_mcp("list_jobs", {})))
            out["no_job"] = check_mcp(schema_file, "MCP get_job",
                                      _wire(await client.call_tool_mcp("get_job", {"job_id": "nope"})))
            return out

    out = asyncio.run(go())
    raw, shot = out["shot"]
    step = shot["actions_report"]["actions"][1]
    assert step["type"] == "screenshot" and "data_base64" not in step["value"]
    assert [c["type"] for c in raw["content"]] == ["text", "text", "image"]
    assert out["collection"]["status"] == "ok"
    assert out["error"]["status"] == "error" and out["error"]["job_id"] == ""
    assert "actions.0.click.selector" in out["bad_step"]["error"]
    assert {p["name"] for p in out["profiles"]["profiles"]} == {"busy", "example"}
    assert out["jobs"]["active_count"] == 0
    assert set(out["no_job"]) == {"error"}
    RAN.add("mcp")


# -- last: every declared status was produced --------------------------------------------------------


def test_every_declared_response_was_produced(schema_file):
    if RAN != PRODUCERS:
        pytest.skip(f"needs the whole module (ran: {sorted(RAN)})")
    declared = {(key, status) for key, e in schema_file.endpoints.items() for status in e.responses()}
    declared |= {(key, "result") for key, e in schema_file.endpoints.items() if e.is_mcp}
    assert declared - SEEN == set(), "declared but never produced by a real response"
