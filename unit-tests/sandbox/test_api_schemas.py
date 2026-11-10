"""``sandbox-runner.schemas.json`` against the code (root ``CLAUDE.md``
§ Postman collections; the file-wide checks -- coverage, valid schemas, every
collection body, the rendered copies -- are ``unit-tests/apidocs``).

(a) **The generated schemas are current**: the request models
    (``models.RunRequest``, ``CreateRequest``, ``WriteFilesRequest``,
    ``EditFileRequest``, ``ExecRequest``, ``ToolRunRequest``), the response
    model ``RunResponse``, the shared jobs models (``common.jobs.model.JobBase``
    / ``JobsListResponse``) and each MCP tool's arguments (its FastMCP
    ``inputSchema``). ``API_SCHEMAS_UPDATE=1`` rewrites them and re-renders the
    copies::

        API_SCHEMAS_UPDATE=1 UV_LINK_MODE=copy uv run --no-sync python -m pytest \\
            unit-tests/sandbox/test_api_schemas.py -q -p no:cacheprovider

(b) **Real responses validate**, with no Docker and no Postgres: the real
    ``Spawner`` over ``sandbox_fakes``' fake Docker client (``/app`` in a dict;
    readiness, the health probe and the log tail stubbed), and the job table in
    memory (``MemoryJobs``) -- ``get`` / ``list_all`` build ``JobBase`` with
    ``common.jobs.postgres._row_to_jobbase``, and its pool answers the
    runner's own SELECTs with rows holding exactly the columns each one
    selects. Every request runs the real routes and ``operations``:

    * every HTTP endpoint -- spawn, overlay, self-heal, the file tools, exec,
      logs, downloads, teardown, ``/sessions``, ``/tool/*``, the browser-log
      ingest -- and ``/jobs`` through ``common.jobs.router.build_router``
      (what the lifespan mounts), with every structured error status;
    * every collection item, its ``{{variables}}`` pointing at a live session;
    * MCP, in memory through ``fastmcp.Client``: ``initialize``,
      ``tools/list``, every tool's ``structuredContent`` -- a result and its
      errors-as-data -- and every collection ``tools/call``.

    A status with no entry must be the generic ``{"detail": …}`` body; the
    last test checks that every declared status was produced.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import importlib
import io
import json
import os
import re
import tarfile
import uuid
from typing import Any, Dict, Optional

import pytest

from sandbox_fakes import RUNNER_DIR, FakeContainer, spawner_with  # puts ai/sandbox/runner on sys.path
import constants  # noqa: E402

from common.apidocs import generate, postman, render, schemas  # noqa: E402

import pathlib  # noqa: E402

SERVICE_DIR = pathlib.Path(RUNNER_DIR).resolve().parent
SCHEMAS_PATH = SERVICE_DIR / "sandbox-runner.schemas.json"
COLLECTION_PATH = SERVICE_DIR / "sandbox-runner.postman_collection.json"
PREFIX = "/v1/sandbox"            # the LiteLLM pass-through the collection calls through

GENERIC_ERROR = {
    "type": "object", "additionalProperties": False, "required": ["detail"],
    "properties": {"detail": {"anyOf": [
        {"type": "string"},
        {"type": "array", "items": {"type": "object", "required": ["loc", "msg", "type"]}},
    ]}},
}

SEEN: set = set()
RAN: set = set()
PRODUCERS = {"health", "spawn", "session", "files", "teardown", "tool", "collection", "mcp", "mcp_collection"}


@pytest.fixture(scope="module")
def app_module(tmp_path_factory):
    # app.py sets up logging into LOG_DIR at import; keep it out of /data.
    constants.LOG_DIR = str(tmp_path_factory.mktemp("sandbox-runner-logs"))
    return importlib.import_module("app")


@pytest.fixture(scope="module")
def schema_file():
    return schemas.load_schemas(SCHEMAS_PATH)


# -- (a) the generated schemas ---------------------------------------------------------------


def generated_schemas(app_module) -> Dict[tuple, Any]:
    import models
    from common.jobs.model import JobBase, JobsListResponse

    out: Dict[tuple, Any] = {}
    for path, model in (("run", models.RunRequest), ("create", models.CreateRequest),
                        ("session/:sandboxSessionId/files", models.WriteFilesRequest),
                        ("session/:sandboxSessionId/edit", models.EditFileRequest),
                        ("session/:sandboxSessionId/exec", models.ExecRequest),
                        ("tool/run", models.ToolRunRequest)):
        out[("endpoints", f"POST {PREFIX}/{path}", "request", "schema")] = generate.from_pydantic(model)
    out[("$defs", "run_response")] = generate.from_pydantic(models.RunResponse, mode="serialization")
    out[("$defs", "job_base")] = generate.from_pydantic(JobBase, mode="serialization")
    out[("$defs", "jobs_list_response")] = generate.from_pydantic(JobsListResponse, mode="serialization")
    for name, schema in asyncio.run(generate.from_mcp(app_module._mcp, source="app._mcp")).items():
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
    (the collection's path, ``:params`` included). Returns the parsed JSON
    body (None for a body that is not JSON)."""
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
    if spec.content_type is None:
        assert response.content == b"", (key, status)
        return None
    media = response.headers.get("content-type", "").split(";")[0].strip().lower()
    assert media == spec.content_type, (key, status, media)
    if spec.schema is None:
        return None
    body = response.json()
    errors = schemas.validate(endpoint.standalone(spec.schema), body)
    assert errors == [], f"{key} {status}: {errors}"
    return body


def check_mcp(schema_file, key: str, result: Dict[str, Any]) -> Any:
    """``result`` (a JSON-RPC result as sent) against an MCP entry: a
    ``tools/call``'s ``structuredContent``, another method's whole result."""
    endpoint = schema_file.endpoints[key]
    schema = endpoint.standalone(endpoint.entry["result"])
    payload = result
    if "arguments" in endpoint.entry:
        assert result.get("isError") is not True, result
        assert result["content"][0]["type"] == "text"
        payload = result["structuredContent"]
    errors = schemas.validate(schema, payload)
    assert errors == [], (key, errors)
    SEEN.add((key, "result"))
    return payload


# -- the job table in memory -------------------------------------------------------------------------


class _Conn:
    """What the runner's raw SQL needs from an asyncpg connection: its three
    SELECTs (``operations._find_running_session``,
    ``_find_active_session_by_chat``, ``_list_running_sessions_rows``),
    answered with rows holding exactly the columns each one selects."""

    _SELECT = re.compile(r"^SELECT (.+?) FROM jobs WHERE (.+?) ORDER BY (\w+) DESC(?: LIMIT (\d+))?$")

    def __init__(self, jobs):
        self.jobs = jobs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def fetchrow(self, sql, *args):
        rows = self._select(sql, args)
        return rows[0] if rows else None

    async def fetch(self, sql, *args):
        return self._select(sql, args)

    def _select(self, sql, args):
        match = self._SELECT.match(" ".join(sql.split()))
        assert match, f"SQL the fake does not know: {sql}"
        columns, where, order, limit = match.groups()
        rows = list(self.jobs.rows.values())
        if where == "metadata->>'session_id' = $1 AND phase = 'running'":
            rows = [r for r in rows if r["metadata"].get("session_id") == args[0] and r["phase"] == "running"]
        elif where == "metadata->>'chat_id' = $1 AND phase = ANY($2::text[])":
            rows = [r for r in rows if r["metadata"].get("chat_id") == args[0] and r["phase"] in args[1]]
        elif where == "phase = 'running'":
            rows = [r for r in rows if r["phase"] == "running"]
        else:
            raise AssertionError(f"a WHERE the fake does not know: {where}")
        rows.sort(key=lambda r: r[order], reverse=True)
        if limit:
            rows = rows[: int(limit)]
        return [{c.strip(): r[c.strip()] for c in columns.split(",")} for r in rows]


class _Pool:
    def __init__(self, jobs):
        self.jobs = jobs

    def acquire(self):
        return _Conn(self.jobs)


class MemoryJobs:
    """The ``PostgresRegistry`` calls the runner makes, over rows in memory
    (``id, phase, created_at, updated_at, metadata, result, error``)."""

    def __init__(self):
        self.rows: Dict[str, Dict[str, Any]] = {}
        self._pool = _Pool(self)
        self._tick = 0
        self.refuse_register = False

    def _now(self):
        self._tick += 1                      # strictly increasing, so ORDER BY is stable
        return dt.datetime(2026, 11, 10, 12, 0, tzinfo=dt.timezone.utc) + dt.timedelta(milliseconds=self._tick)

    async def register(self, metadata, initial_phase="pending", **_):
        if self.refuse_register:             # the one-sandbox-per-chat UNIQUE index refusing the row
            import operations

            raise operations._UniqueViolationError("duplicate key value violates unique constraint")
        job_id = uuid.uuid4().hex[:12]
        now = self._now()
        self.rows[job_id] = {"id": job_id, "phase": initial_phase, "created_at": now, "updated_at": now,
                             "metadata": json.loads(json.dumps(dict(metadata))), "result": None, "error": None}
        return job_id

    def _touch(self, job_id, **fields):
        row = self.rows.get(job_id)
        if row is None:
            return False
        row.update(fields, updated_at=self._now())
        return True

    async def set_phase(self, job_id, phase, **_):
        return self._touch(job_id, phase=phase)

    async def set_result(self, job_id, result, phase="completed", **_):
        return self._touch(job_id, phase=phase, result=json.loads(json.dumps(result)))

    async def set_error(self, job_id, error, phase="failed", **_):
        return self._touch(job_id, phase=phase, error=error)

    async def update_metadata(self, job_id, fields):
        if job_id in self.rows:
            self._touch(job_id, metadata={**self.rows[job_id]["metadata"], **json.loads(json.dumps(fields))})

    async def get(self, job_id):
        from common.jobs.postgres import _row_to_jobbase

        row = self.rows.get(job_id)
        return None if row is None else _row_to_jobbase(row)

    async def list_all(self, limit: int = 20):
        from common.jobs.model import JobsListResponse
        from common.jobs.postgres import _row_to_jobbase

        rows = sorted(self.rows.values(), key=lambda r: r["updated_at"], reverse=True)[:limit]
        snaps = [_row_to_jobbase(r) for r in rows]
        return JobsListResponse(active_count=len(snaps), jobs=snaps)

    async def delete(self, job_id):
        return self.rows.pop(job_id, None) is not None


class Slots:
    """The runner's slot semaphore: ``acquire`` waits forever when none is
    free (so ``_reserve_active_slot``'s ``wait_for`` turns it into a 429)."""

    def __init__(self, free: int):
        self.free = free

    async def acquire(self):
        if self.free <= 0:
            await asyncio.Event().wait()
        self.free -= 1
        return True

    def release(self):
        self.free += 1


class Runner:
    """Everything a test drives: the fake container, the spawner, the job
    table, and the knobs (``ready``, ``alive``)."""

    def __init__(self, app_module):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from common.jobs.router import build_router

        self.app = app_module
        self.container = FakeContainer()
        self.spawner = spawner_with(self.container)
        self.jobs = MemoryJobs()
        self.slots = Slots(64)
        self.ready = True
        self.alive = True
        self.logs = "  You can now view your Streamlit app in your browser.\n"
        self.stopped: list = []
        sp = self.spawner
        sp.readiness_ok = lambda *a, **k: self.ready
        sp.probe_health = lambda *a, **k: {"code": 200, "latency_ms": 7}
        sp.tail_logs = lambda name, n=100: self.logs
        sp.tail_logs_since_last_marker = lambda name, n=100: "  Rerunning: app.py changed\n"
        sp.container_exists = lambda name: self.alive
        sp.stop = lambda name: self.stopped.append(name)
        sp.append_to_log = lambda name, payload: None
        self.http = TestClient(app_module.app)            # no `with`: the lifespan needs Postgres
        jobs_app = FastAPI()
        jobs_app.include_router(build_router(self.jobs, include_delete=False))   # what the lifespan mounts
        self.jobs_http = TestClient(jobs_app)

    def session_of(self, sandbox_id: str) -> str:
        return self.jobs.rows[sandbox_id]["metadata"]["session_id"]


@pytest.fixture
def runner(app_module, monkeypatch):
    import operations
    import state

    r = Runner(app_module)
    monkeypatch.setattr(state, "registry", r.jobs)
    monkeypatch.setattr(state, "spawner", r.spawner)
    monkeypatch.setattr(state, "slot_sem", r.slots)
    monkeypatch.setattr(operations, "UPDATE_SETTLE_S", 0)

    async def ingest(sandbox_id, entries):          # the fire-and-forget docker exec, not under test here
        return None

    monkeypatch.setattr(app_module, "_do_browser_log_ingest", ingest)
    return r


LONG_APP = "".join(f"st.write('row {i}')\n" for i in range(40))
PY_FILES = {"app.py": "import streamlit as st\n" + LONG_APP, "src/util.py": "X = 1\n",
            "logo.png": {"encoding": "base64", "content": base64.b64encode(b"\x89PNG\r\n\x1a\n\x00\x01").decode()}}


def spawn_python(runner, schema_file, **extra) -> Dict[str, Any]:
    body = check(schema_file, "POST", "/run", runner.http.post("/run", json={
        "runtime": "python", "files": PY_FILES, **extra}))
    assert body["reused"] is False and body["app_status"]["note"] == "readiness ok"
    return body


def expect(schema_file, method, path, response, status: int) -> Any:
    assert response.status_code == status, (method, path, response.status_code, response.text[:500])
    return check(schema_file, method, path, response)


# -- (b) producers ------------------------------------------------------------------------------------


def test_health(schema_file, runner, monkeypatch):
    import state

    assert check(schema_file, "GET", "/health", runner.http.get("/health")) == {"status": "ok", "db": "up"}

    async def broken(limit=20):
        raise ConnectionError("sandbox-db is down")

    monkeypatch.setattr(runner.jobs, "list_all", broken)
    assert check(schema_file, "GET", "/health", runner.http.get("/health"))["db"] == "down"
    monkeypatch.setattr(state, "registry", None)
    assert check(schema_file, "GET", "/health", runner.http.get("/health"))["status"] == "degraded"
    RAN.add("health")


def test_spawn_paths(schema_file, runner, monkeypatch):
    http = runner.http
    static = check(schema_file, "POST", "/run", http.post("/run", json={
        "runtime": "static", "files": {"index.html": "<h1>hi</h1>"}, "chat_id": "chat-a"}))
    sid = static["session_id"]
    # Follow-up: overlay (reused), then the chat's running container reused with no session_id.
    again = check(schema_file, "POST", "/run", http.post("/run", json={
        "runtime": "static", "session_id": sid, "files": {"about.html": "<p>x</p>"}}))
    assert again["reused"] is True and again["sandbox_id"] == static["sandbox_id"]
    by_chat = check(schema_file, "POST", "/run", http.post("/run", json={
        "runtime": "static", "chat_id": "chat-a", "files": {"more.html": "<p>y</p>"}}))
    assert by_chat["session_id"] == sid
    py = spawn_python(runner, schema_file)
    # 400: unknown runtime, an entrypoint on static (plain), static lint (structured).
    expect(schema_file, "POST", "/run", http.post("/run", json={"runtime": "cobol"}), 400)
    expect(schema_file, "POST", "/run", http.post("/run", json={"runtime": "static", "entrypoint": "x"}), 400)
    lint = expect(schema_file, "POST", "/run", http.post("/run", json={
        "runtime": "python", "files": {"app.py": "def broken(:\n"}}), 400)
    assert lint["detail"]["errors"][0]["path"] == "app.py"
    # 409: the rewrite guard, a gone container, the chat reservation.
    rewrite = expect(schema_file, "POST", "/run", http.post("/run", json={
        "runtime": "python", "session_id": py["session_id"],
        "files": {"app.py": PY_FILES["app.py"].replace("row 3", "row three")}}), 409)
    assert rewrite["detail"]["kind"] == "rewrite_of_existing_file" and rewrite["detail"]["tool"] == "run"
    runner.alive = False
    expect(schema_file, "POST", "/run", http.post("/run", json={
        "runtime": "static", "session_id": sid, "recreate_if_gone": False}), 409)
    healed = check(schema_file, "POST", "/run", http.post("/run", json={
        "runtime": "static", "session_id": sid, "files": {"index.html": "<h1>back</h1>"}}))
    assert healed["session_id"] == sid and healed["sandbox_id"] != static["sandbox_id"]
    runner.alive = True
    runner.jobs.refuse_register = True
    reserve = expect(schema_file, "POST", "/run", http.post("/run", json={"runtime": "static", "chat_id": "chat-z"}), 409)
    assert reserve["detail"]["chat_id"] == "chat-z"
    expect(schema_file, "POST", "/create", http.post("/create", json={"runtime": "static", "chat_id": "chat-z"}), 409)
    runner.jobs.refuse_register = False
    # 504: readiness never answered (the row ends `failed`).
    runner.ready = False
    not_ready = expect(schema_file, "POST", "/run", http.post("/run", json={"runtime": "static"}), 504)
    assert not_ready["detail"]["logs"]
    expect(schema_file, "POST", "/create", http.post("/create", json={"runtime": "node"}), 504)
    runner.ready = True
    # /create: a warming container, then the chat's duplicate (RunResponse drops the flags).
    created = check(schema_file, "POST", "/create", http.post("/create", json={
        "runtime": "python", "ttl_seconds": 600, "env": {"DEMO_MODE": "1"}, "chat_id": "chat-c"}))
    duplicate = check(schema_file, "POST", "/create", http.post("/create", json={"runtime": "python", "chat_id": "chat-c"}))
    assert duplicate["sandbox_id"] == created["sandbox_id"] and duplicate["reused"] is True
    expect(schema_file, "POST", "/create", http.post("/create", json={"runtime": "nope"}), 400)
    # Generic errors: 429 the pool, 500 a spawn error, 422 the validators.
    free = runner.slots.free
    runner.slots.free = 0
    expect(schema_file, "POST", "/run", http.post("/run", json={"runtime": "static"}), 429)
    runner.slots.free = free

    def explode(*a, **k):
        raise RuntimeError("docker create failed for image 'sandbox-static'")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(runner.spawner, "spawn", explode)
        expect(schema_file, "POST", "/run", http.post("/run", json={"runtime": "static"}), 500)
    import models

    monkeypatch.setattr(models, "MAX_FILE_BYTES", 8)
    expect(schema_file, "POST", "/run", http.post("/run", json={"runtime": "static", "files": {"a": "x" * 9}}), 422)
    expect(schema_file, "POST", "/run", http.post("/run", json={"runtime": "static", "env": {"HTTP_PROXY": "x"}}), 422)
    RAN.add("spawn")


def test_session_writes_logs_and_jobs(schema_file, runner):
    http = runner.http
    py = spawn_python(runner, schema_file)
    sid, sandbox = py["session_id"], py["sandbox_id"]
    path = "/session/:sandboxSessionId/files"
    written = check(schema_file, "POST", path, http.post(f"/session/{sid}/files", json={
        "files": {"pages/new.py": "import streamlit as st\n"}, "deletes": ["old.txt"]}))
    assert written["reused"] is True and written["app_status"] == {"code": 200, "latency_ms": 7}
    expect(schema_file, "POST", path, http.post(f"/session/{sid}/files", json={
        "files": {"app.py": PY_FILES["app.py"] + "st.write('end')\n"}}), 409)
    expect(schema_file, "POST", path, http.post(f"/session/{sid}/files", json={"files": {"bad.py": "if:\n"}}), 400)
    expect(schema_file, "POST", path, http.post("/session/bad.id/files", json={"files": {}}), 400)
    expect(schema_file, "POST", path, http.post(f"/session/{sid}/files", json={"files": {"../x": "y"}}), 400)
    expect(schema_file, "POST", path, http.post("/session/nosuchsession/files", json={"files": {}}), 404)
    # Container gone: 409 without recreate_if_gone; a self-heal; then one that never gets ready.
    runner.alive = False
    expect(schema_file, "POST", path, http.post(f"/session/{sid}/files", json={"files": {}}), 409)
    healed = check(schema_file, "POST", path, http.post(f"/session/{sid}/files", json={
        "files": {"app.py": "import streamlit as st\n"}, "recreate_if_gone": True}))
    assert healed["recreated"] is True and healed["session_id"] == sid
    runner.ready = False
    expect(schema_file, "POST", path, http.post(f"/session/{sid}/files", json={"recreate_if_gone": True}), 504)
    runner.ready = True
    other = spawn_python(runner, schema_file)
    runner.jobs.rows[other["sandbox_id"]]["metadata"]["runtime"] = None         # a row written without one
    failed = expect(schema_file, "POST", path, http.post(f"/session/{other['session_id']}/files", json={
        "recreate_if_gone": True}), 500)
    assert failed["detail"]["error"] == "cannot self-heal: runtime metadata missing"
    runner.jobs.rows[other["sandbox_id"]]["metadata"]["runtime"] = "python"
    runner.alive = True
    # Logs, downloads, jobs.
    live = spawn_python(runner, schema_file)
    sid, sandbox = live["session_id"], live["sandbox_id"]
    logs = check(schema_file, "GET", "/session/:sandboxSessionId/logs", http.get(f"/session/{sid}/logs?lines=50"))
    assert logs["lines_requested"] == 50
    check(schema_file, "GET", "/jobs/:sandboxId/logs", http.get(f"/jobs/{sandbox}/logs?lines=5000"))
    tar = http.get(f"/session/{sid}/download")
    check(schema_file, "GET", "/session/:sandboxSessionId/download", tar)
    with tarfile.open(fileobj=io.BytesIO(tar.content)) as archive:
        assert "app/app.py" in archive.getnames()
    check(schema_file, "GET", "/jobs/:sandboxId/download", http.get(f"/jobs/{sandbox}/download"))
    listing = check(schema_file, "GET", "/sessions", http.get("/sessions"))
    assert listing["count"] == len(listing["sessions"]) >= 1
    jobs = check(schema_file, "GET", "/jobs", runner.jobs_http.get("/jobs"))
    assert {"running", "failed", "expired"} <= {j["phase"] for j in jobs["jobs"]}
    one = check(schema_file, "GET", "/jobs/:sandboxId", runner.jobs_http.get(f"/jobs/{sandbox}"))
    assert one["result"]["container_name"] == f"sandbox-{sandbox}"
    failed_row = next(j["job_id"] for j in jobs["jobs"] if j["phase"] == "failed")
    assert check(schema_file, "GET", "/jobs/:sandboxId", runner.jobs_http.get(f"/jobs/{failed_row}"))["error"]
    for method, p, response in (
        ("GET", "/jobs/:sandboxId", runner.jobs_http.get("/jobs/000000000000")),
        ("GET", "/jobs/:sandboxId/logs", http.get("/jobs/000000000000/logs")),
        ("GET", "/jobs/:sandboxId/logs", http.get(f"/jobs/{failed_row}/logs")),
        ("GET", "/jobs/:sandboxId/download", http.get(f"/jobs/{failed_row}/download")),
        ("GET", "/session/:sandboxSessionId/logs", http.get("/session/bad.id/logs")),
        ("GET", "/session/:sandboxSessionId/logs", http.get("/session/nosuchsession/logs")),
        ("GET", "/session/:sandboxSessionId/download", http.get("/session/nosuchsession/download")),
    ):
        assert response.status_code in (400, 404, 409), (p, response.status_code)
        check(schema_file, method, p, response)
    RAN.add("session")


def test_file_tools(schema_file, runner):
    http = runner.http
    sid = spawn_python(runner, schema_file)["session_id"]
    files = check(schema_file, "GET", "/session/:sandboxSessionId/files", http.get(f"/session/{sid}/files"))
    assert {"app.py", "src/util.py", "logo.png"} <= {f["path"] for f in files["files"]}
    read = check(schema_file, "GET", "/session/:sandboxSessionId/files", http.get(
        f"/session/{sid}/files", params={"paths": "app.py,logo.png,missing.txt,../x,src", "max_bytes_per_file": 64}))
    by_path = {f["path"]: f for f in read["files"]}
    assert by_path["logo.png"]["encoding"] == "base64" and by_path["app.py"]["truncated"] is True
    assert by_path["missing.txt"]["error"] == "not found"
    for response in (http.get("/session/bad.id/files"), http.get("/session/nosuchsession/files")):
        check(schema_file, "GET", "/session/:sandboxSessionId/files", response)
    # read_file
    path = "/session/:sandboxSessionId/file"
    window = check(schema_file, "GET", path, http.get(f"/session/{sid}/file", params={"path": "app.py", "offset": 2, "limit": 3}))
    assert (window["start_line"], window["end_line"], window["truncated"]) == (2, 4, True)
    for params, status in (({"path": "../etc/passwd"}, 400), ({"path": "nope.txt"}, 404), ({"path": "logo.png"}, 409),
                           ({"path": "src"}, 409)):
        expect(schema_file, "GET", path, http.get(f"/session/{sid}/file", params=params), status)
    expect(schema_file, "GET", path, http.get("/session/nosuchsession/file", params={"path": "a"}), 404)
    expect(schema_file, "GET", path, http.get("/session/bad.id/file", params={"path": "a"}), 400)
    expect(schema_file, "GET", path, http.get(f"/session/{sid}/file"), 422)
    runner.alive = False
    expect(schema_file, "GET", path, http.get(f"/session/{sid}/file", params={"path": "app.py"}), 409)
    runner.alive = True
    # edit_file
    path = "/session/:sandboxSessionId/edit"
    edited = check(schema_file, "POST", path, http.post(f"/session/{sid}/edit", json={
        "path": "app.py", "old_string": "st.write('row 5')", "new_string": "st.write('row five')"}))
    assert edited["replacements"] == 1 and edited["snippet"]
    check(schema_file, "POST", path, http.post(f"/session/{sid}/edit", json={
        "path": "app.py", "old_string": "st.write", "new_string": "st.text", "replace_all": True}))
    for body, status, kind in (
        ({"path": "app.py", "old_string": "st.text('row 7') ", "new_string": "x"}, 409, "not_found"),
        ({"path": "app.py", "old_string": "st.text(", "new_string": "st.write("}, 409, "multiple_matches"),
        ({"path": "app.py", "old_string": "8\tst.text('row 6')", "new_string": "x"}, 409, "line_number_prefix"),
        ({"path": "app.py", "old_string": "row 9", "new_string": "row 9"}, 409, "no_change"),
        ({"path": "app.py", "old_string": "", "new_string": "x"}, 409, "empty_old_string"),
        ({"path": "logo.png", "old_string": "PNG", "new_string": "GIF"}, 409, "binary_file"),
        ({"path": "src", "old_string": "a", "new_string": "b"}, 409, "is_directory"),
        ({"path": "nope.py", "old_string": "a", "new_string": "b"}, 404, "missing_file"),
        ({"path": "/etc/passwd", "old_string": "a", "new_string": "b"}, 400, "unsafe_path"),
    ):
        detail = expect(schema_file, "POST", path, http.post(f"/session/{sid}/edit", json=body), status)["detail"]
        assert detail["kind"] == kind, (kind, detail)
    lint = expect(schema_file, "POST", path, http.post(f"/session/{sid}/edit", json={
        "path": "app.py", "old_string": "import streamlit as st", "new_string": "import streamlit as"}), 400)
    assert lint["detail"]["error"] == "static lint failed"
    expect(schema_file, "POST", path, http.post("/session/nosuchsession/edit", json={
        "path": "a", "old_string": "a", "new_string": "b"}), 404)
    expect(schema_file, "POST", path, http.post(f"/session/{sid}/edit", json={"path": " ", "old_string": "a",
                                                                              "new_string": "b"}), 422)
    import operations

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(operations, "MAX_FILE_BYTES", 64)
        too_large = expect(schema_file, "POST", path, http.post(f"/session/{sid}/edit", json={
            "path": "app.py", "old_string": "row 1'", "new_string": "row one'"}), 413)
    assert too_large["detail"]["kind"] == "too_large"
    runner.alive = False
    expect(schema_file, "POST", path, http.post(f"/session/{sid}/edit", json={
        "path": "app.py", "old_string": "a", "new_string": "b"}), 409)
    runner.alive = True
    # exec
    path = "/session/:sandboxSessionId/exec"
    ran = check(schema_file, "POST", path, http.post(f"/session/{sid}/exec", json={"command": "ls -la"}))
    assert ran["exit_code"] == 0 and ran["output"] == "ok\n"
    expect(schema_file, "POST", path, http.post("/session/nosuchsession/exec", json={"command": "ls"}), 404)
    expect(schema_file, "POST", path, http.post(f"/session/{sid}/exec", json={"command": ""}), 400)
    expect(schema_file, "POST", path, http.post(f"/session/{sid}/exec", json={"timeout_seconds": 3}), 422)
    RAN.add("files")


def test_teardown_and_browser_log(schema_file, runner):
    http = runner.http
    py = spawn_python(runner, schema_file)
    check(schema_file, "DELETE", "/session/:sandboxSessionId", http.delete(f"/session/{py['session_id']}"))
    check(schema_file, "DELETE", "/session/:sandboxSessionId", http.delete(f"/session/{py['session_id']}"))
    expect(schema_file, "DELETE", "/session/:sandboxSessionId", http.delete("/session/bad.id"), 400)
    other = spawn_python(runner, schema_file)
    check(schema_file, "DELETE", "/jobs/:sandboxId", http.delete(f"/jobs/{other['sandbox_id']}"))
    expect(schema_file, "DELETE", "/jobs/:sandboxId", http.delete(f"/jobs/{other['sandbox_id']}"), 404)
    log = "/internal/browser-log/:sandboxId"
    entries = {"entries": [{"level": "error", "ts": 1735689600123, "message": "TypeError: x", "line": 4}]}
    check(schema_file, "POST", log, http.post(f"/internal/browser-log/{py['sandbox_id']}", json=entries))
    check(schema_file, "POST", log, http.post("/internal/browser-log/NOT-AN-ID", json=entries))
    check(schema_file, "POST", log, http.post(f"/internal/browser-log/{py['sandbox_id']}", content=b"{not json"))
    import constants as runner_constants

    big = json.dumps({"entries": [{"level": "log", "message": "x" * runner_constants.BROWSER_LOG_MAX_BODY_BYTES}]})
    expect(schema_file, "POST", log, http.post(f"/internal/browser-log/{py['sandbox_id']}", content=big,
                                               headers={"content-type": "application/json"}), 413)
    RAN.add("teardown")


def test_tool_server(schema_file, runner):
    http = runner.http
    spec = check(schema_file, "GET", "/tool/openapi.json", http.get("/tool/openapi.json"))
    assert spec["info"]["title"] == "sandbox-runner tools"
    kinds = check(schema_file, "GET", "/tool/get_runtime_types", http.get("/tool/get_runtime_types"))
    assert {k["name"] for k in kinds} >= {"static", "python", "node"}
    page = http.post("/tool/run", json={"runtime": "static", "files": {"index.html": "<h1>x</h1>"}})
    check(schema_file, "POST", "/tool/run", page)
    assert page.headers["content-disposition"] == "inline" and "<" in page.text
    expect(schema_file, "POST", "/tool/run", http.post("/tool/run", json={"runtime": "static", "session_id": "bad id"}), 400)
    expect(schema_file, "POST", "/tool/run", http.post("/tool/run", json={"runtime": "python", "files": {"a.py": "(\n"}}), 400)
    runner.jobs.refuse_register = True
    expect(schema_file, "POST", "/tool/run", http.post("/tool/run", json={"runtime": "static", "chat_id": "c1"}), 409)
    runner.jobs.refuse_register = False
    runner.ready = False
    expect(schema_file, "POST", "/tool/run", http.post("/tool/run", json={"runtime": "static"}), 504)
    runner.ready = True
    RAN.add("tool")


def _url(req: postman.Request, values: Dict[str, str]) -> str:
    """The item's path under the runner (no pass-through prefix), path
    params and query resolved."""
    path = req.path[len(PREFIX):]
    for name, value in values.items():
        path = path.replace(f":{name}", value)
    url = req.request.get("url") or {}
    query = [q for q in (url.get("query") or []) if not q.get("disabled")]
    if query:
        path += "?" + "&".join(f"{q['key']}={postman.resolve(str(q.get('value', '')), values)}" for q in query)
    return path


def test_every_collection_item(schema_file, runner):
    collection = postman.load(COLLECTION_PATH)
    live = spawn_python(runner, schema_file, chat_id="chat-live")
    values = {**postman.variables(collection), "sandboxSessionId": live["session_id"],
              "sandboxId": live["sandbox_id"], "chatId": "chat-live"}
    items = [r for r in postman.requests(collection) if not postman.endpoint_key(r).startswith("MCP ")]
    items.sort(key=lambda r: r.method == "DELETE")          # tear down last
    statuses = {}
    for req in items:
        key = postman.endpoint_key(req)
        sent = postman.body(req, values)
        client = runner.jobs_http if key in (f"GET {PREFIX}/jobs", f"GET {PREFIX}/jobs/:sandboxId") else runner.http
        response = client.request(req.method, _url(req, values), json=sent.value if sent else None)
        check(schema_file, req.method, key[len(req.method) + 1 + len(PREFIX):], response)
        statuses[req.label] = response.status_code
    assert all(s < 300 for s in statuses.values()), statuses
    RAN.add("collection")


# -- MCP, in memory ----------------------------------------------------------------------------------


def _wire(model) -> Dict[str, Any]:
    return model.model_dump(mode="json", by_alias=True, exclude_none=True)


def mcp_calls(mcp, calls, *, handshake=None):
    from fastmcp import Client

    async def go():
        async with Client(mcp) as client:
            if handshake is not None:
                handshake["initialize"] = _wire(client.initialize_result)
                handshake["tools/list"] = _wire(await client.list_tools_mcp())
            return [_wire(await client.call_tool_mcp(name, arguments)) for name, arguments in calls]

    return asyncio.run(go())


def test_mcp_tools(schema_file, runner, monkeypatch):
    mcp = runner.app._mcp
    handshake: Dict[str, Any] = {}
    (kinds, created, duplicate, bad_runtime, reserved_env, ran) = mcp_calls(mcp, [
        ("get_runtime_types", {}),
        ("create", {"runtime": "python", "chat_id": "chat-m"}),
        ("create", {"runtime": "python", "chat_id": "chat-m"}),
        ("create", {"runtime": "cobol"}),
        ("create", {"runtime": "python", "env": {"HTTP_PROXY": "x"}}),
        ("run", {"runtime": "python", "files": PY_FILES}),
    ], handshake=handshake)
    check_mcp(schema_file, "MCP initialize", handshake["initialize"])
    listed = check_mcp(schema_file, "MCP tools/list", handshake["tools/list"])
    assert len(listed["tools"]) == 12
    check_mcp(schema_file, "MCP get_runtime_types", kinds)
    assert check_mcp(schema_file, "MCP create", created)["warming"] is True
    assert check_mcp(schema_file, "MCP create", duplicate)["duplicate_create_prevented"] is True
    assert check_mcp(schema_file, "MCP create", bad_runtime) == {"ok": False, "status": 400,
                                                                  "error": bad_runtime["structuredContent"]["error"]}
    assert check_mcp(schema_file, "MCP create", reserved_env)["detail"]["error"] == "invalid arguments"
    run = check_mcp(schema_file, "MCP run", ran)
    sid = run["session_id"]
    big = {"session_id": sid, "files": {"app.py": PY_FILES["app.py"].replace("row 2", "row two")}}
    results = mcp_calls(mcp, [
        ("run", {"runtime": "python", "session_id": sid, "files": {"extra.py": "X = 2\n"}}),
        ("run", {"runtime": "python", "files": {"app.py": "def f(:\n"}}),
        ("write_files", {"session_id": sid, "files": {"pages/two.py": "import streamlit\n"}}),
        ("write_files", big),
        ("write_files", {"session_id": "nosuchsession", "files": {}}),
        ("read_file", {"session_id": sid, "path": "app.py", "limit": 5}),
        ("read_file", {"session_id": sid, "path": "logo.png"}),
        ("edit_file", {"session_id": sid, "path": "app.py", "old_string": "row 4'", "new_string": "row four'"}),
        ("edit_file", {"session_id": sid, "path": "app.py", "old_string": "row 44'", "new_string": "x"}),
        ("get_files", {"session_id": sid}),
        ("get_files", {"session_id": sid, "paths": ["app.py", "logo.png", "missing.txt"]}),
        ("get_files", {"session_id": "nosuchsession"}),
        ("get_logs", {"session_id": sid, "lines": 20}),
        ("get_logs", {"session_id": "bad.id"}),
        ("exec", {"session_id": sid, "command": "python -V"}),
        ("exec", {"session_id": "nosuchsession", "command": "ls"}),
        ("preview", {"session_id": sid}),
        ("preview", {"session_id": "nosuchsession"}),
        ("list_sessions", {}),
        ("close", {"session_id": sid}),
        ("close", {"session_id": sid}),
        ("close", {"session_id": "bad.id"}),
    ])
    keys = ["run", "run", "write_files", "write_files", "write_files", "read_file", "read_file", "edit_file",
            "edit_file", "get_files", "get_files", "get_files", "get_logs", "get_logs", "exec", "exec", "preview",
            "preview", "list_sessions", "close", "close", "close"]
    out = [check_mcp(schema_file, f"MCP {key}", result) for key, result in zip(keys, results)]
    overlay, lint, wrote, rewrite, no_session, window, binary, edit, missed = out[:9]
    assert overlay["reused"] is True and "download_url" in overlay
    assert lint["ok"] is False and lint["detail"]["errors"]
    assert wrote["ok"] is True and rewrite["detail"]["kind"] == "rewrite_of_existing_file"
    assert no_session["status"] == 404 and window["end_line"] == 5 and binary["detail"]["kind"] == "binary_file"
    assert edit["replacements"] == 1 and "snippet" not in edit and missed["detail"]["kind"] == "not_found"
    listing, contents, files_404, logs, logs_400, ran_exec, exec_404, preview, preview_404, sessions = out[9:19]
    assert all("content" not in f for f in contents["files"]) and files_404 == {
        "ok": False, "status": 404, "error": files_404["error"]}
    assert logs["logs"] and logs_400["status"] == 400 and ran_exec["exit_code"] == 0 and exec_404["status"] == 404
    assert preview["iframe_html"] and preview_404["detail"]["error"] == "no running sandbox for this session"
    assert sessions["count"] >= 1
    closed, again, closed_400 = out[19:]
    assert closed["was_running"] is True and again["was_running"] is False and closed_400["status"] == 400
    # An empty log, a payload over the cap (413, before anything is written).
    runner.logs = ""
    import models

    monkeypatch.setattr(models, "MAX_FILE_BYTES", 8)
    sid2 = check_mcp(schema_file, "MCP run", mcp_calls(mcp, [("run", {"runtime": "static"})])[0])["session_id"]
    empty, too_big = mcp_calls(mcp, [("get_logs", {"session_id": sid2}),
                                     ("write_files", {"session_id": sid2, "files": {"a.txt": "x" * 9}})])
    assert check_mcp(schema_file, "MCP get_logs", empty)["empty"] is True
    too_big = check_mcp(schema_file, "MCP write_files", too_big)
    assert too_big["status"] == 413 and too_big["detail"]["error"] == "payload too large"
    RAN.add("mcp")


def test_every_collection_tool_call(schema_file, runner):
    collection = postman.load(COLLECTION_PATH)
    live = spawn_python(runner, schema_file, chat_id="chat-live")
    values = {**postman.variables(collection), "sandboxSessionId": live["session_id"],
              "sandboxId": live["sandbox_id"], "chatId": "chat-live"}
    calls, keys = [], []
    for req in postman.requests(collection):
        key = postman.endpoint_key(req)
        if key.startswith("MCP ") and key not in ("MCP initialize", "MCP tools/list"):
            calls.append((key[4:], postman.mcp_instance(req, values)))
            keys.append(key)
    assert len(calls) == 12
    for key, result in zip(keys, mcp_calls(runner.app._mcp, calls)):
        check_mcp(schema_file, key, result)
    RAN.add("mcp_collection")


# -- last: every declared status was produced --------------------------------------------------------


def test_every_declared_response_was_produced(schema_file):
    if RAN != PRODUCERS:
        pytest.skip(f"needs the whole module (ran: {sorted(RAN)})")
    declared = {(key, status) for key, e in schema_file.endpoints.items() for status in e.responses()}
    declared |= {(key, "result") for key, e in schema_file.endpoints.items() if e.is_mcp}
    assert declared - SEEN == set(), "declared but never produced by a real response"
