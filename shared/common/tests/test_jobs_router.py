"""Tests for common.jobs.router.build_router — sync + async backends.

Uses FastAPI's TestClient which drives the app end-to-end. Verifies that
both the InMemoryRegistry (sync) and SqliteRegistry (async) paths mount
cleanly and return the expected shapes, the cancel route with its
``on_cancel`` hook (200 / 404 / 409, the hook only after a real cancel, a
raising hook swallowed), and the long-poll (``terminal_phases`` +
``?wait=``) over a scripted async registry.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from common.jobs import InMemoryRegistry, JobBase
from common.jobs.router import build_router
from common.jobs.sqlite import SqliteRegistry


class _Meta(BaseModel):
    tag: str


# ── Sync (in-memory) backend ───────────────────────────────────────────────
def test_sync_router_get_list_empty_by_default():
    reg = InMemoryRegistry(max_concurrent=8)
    app = FastAPI()
    app.include_router(build_router(reg, include_cancel=True))
    with TestClient(app) as client:
        r = client.get("/jobs")
        assert r.status_code == 200
        body = r.json()
        assert body["active_count"] == 0
        assert body["jobs"] == []
        assert body["max_concurrent"] == 8
        assert body["available"] == 8


def test_sync_router_get_by_id_404_when_missing():
    reg = InMemoryRegistry()
    app = FastAPI()
    app.include_router(build_router(reg))
    with TestClient(app) as client:
        r = client.get("/jobs/nonexistent")
        assert r.status_code == 404


def test_sync_router_shows_active_job_and_supports_cancel():
    reg = InMemoryRegistry(max_concurrent=2)
    app = FastAPI()
    app.include_router(build_router(reg, include_cancel=True))
    observed: dict = {}

    def worker() -> None:
        with reg.job(_Meta(tag="t"), initial_phase="running") as job:
            observed["job_id"] = job.job_id
            observed["cancelled"] = job.wait_or_cancel(timeout=5.0)

    with TestClient(app) as client:
        t = threading.Thread(target=worker)
        t.start()
        # Wait for the job to register.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and "job_id" not in observed:
            time.sleep(0.02)
        assert "job_id" in observed

        # GET /jobs should show the active one.
        r = client.get("/jobs")
        body = r.json()
        assert body["active_count"] == 1
        assert body["jobs"][0]["job_id"] == observed["job_id"]
        assert body["available"] == 1  # max_concurrent=2 - 1 active

        # GET /jobs/{id} should return it.
        r = client.get(f"/jobs/{observed['job_id']}")
        assert r.status_code == 200
        assert r.json()["metadata"] == {"tag": "t"}

        # POST cancel.
        r = client.post(f"/jobs/{observed['job_id']}/cancel")
        assert r.status_code == 200
        assert r.json()["cancelled"] is True
        t.join(timeout=5.0)
        assert observed["cancelled"] is True

        # Job gone after context exit.
        r = client.get(f"/jobs/{observed['job_id']}")
        assert r.status_code == 404


def test_sync_router_cancel_404_when_missing():
    reg = InMemoryRegistry()
    app = FastAPI()
    app.include_router(build_router(reg, include_cancel=True))
    with TestClient(app) as client:
        r = client.post("/jobs/does-not-exist/cancel")
        assert r.status_code == 404


# ── Async (sqlite) backend ─────────────────────────────────────────────────
@pytest.fixture
def sqlite_app_factory(tmp_path: Path):
    """Build an app over a fresh SqliteRegistry, with router options."""

    def _build(**router_kwargs):
        db_path = str(tmp_path / "jobs.db")
        reg = SqliteRegistry(db_path)
        asyncio.run(reg.init())
        app = FastAPI()
        app.include_router(build_router(reg, include_delete=True, **router_kwargs))
        return app, reg

    return _build


@pytest.fixture
def sqlite_app(sqlite_app_factory):
    return sqlite_app_factory()


def test_async_router_lists_persistent_jobs(sqlite_app):
    app, reg = sqlite_app
    job_id = asyncio.run(reg.register(_Meta(tag="hello"), "pending"))
    with TestClient(app) as client:
        r = client.get("/jobs")
        assert r.status_code == 200
        body = r.json()
        assert body["active_count"] == 1
        assert body["jobs"][0]["job_id"] == job_id
        assert body["jobs"][0]["metadata"] == {"tag": "hello"}
        # SQLite backend doesn't populate max_concurrent.
        assert body["max_concurrent"] is None
        assert body["available"] is None


def test_async_router_get_and_delete(sqlite_app):
    app, reg = sqlite_app
    job_id = asyncio.run(reg.register(_Meta(tag="d"), "pending"))
    with TestClient(app) as client:
        r = client.get(f"/jobs/{job_id}")
        assert r.status_code == 200

        r = client.delete(f"/jobs/{job_id}")
        assert r.status_code == 204

        r = client.get(f"/jobs/{job_id}")
        assert r.status_code == 404

        r = client.delete(f"/jobs/{job_id}")
        assert r.status_code == 404


# ── on_delete hook ─────────────────────────────────────────────────────────
def test_async_router_calls_on_delete_before_removing_the_row(sqlite_app_factory):
    """The hook runs first, so a consumer can still read the job it is about
    to lose (the classifier reads nothing, but a future one might)."""
    seen: list[tuple[str, bool]] = []
    holder: dict = {}

    async def hook(job_id: str) -> None:
        # The row must still be there when the hook runs.
        seen.append((job_id, await holder["reg"].get(job_id) is not None))

    app, reg = sqlite_app_factory(on_delete=hook)
    holder["reg"] = reg
    job_id = asyncio.run(reg.register(_Meta(tag="d"), "pending"))
    with TestClient(app) as client:
        assert client.delete(f"/jobs/{job_id}").status_code == 204
    assert seen == [(job_id, True)]


def test_async_router_survives_an_on_delete_that_raises(sqlite_app_factory):
    """Side-cleanup that throws must not turn a successful delete into a 500."""

    async def hook(job_id: str) -> None:
        raise RuntimeError("artifact dir is on fire")

    app, reg = sqlite_app_factory(on_delete=hook)
    job_id = asyncio.run(reg.register(_Meta(tag="d"), "pending"))
    with TestClient(app) as client:
        assert client.delete(f"/jobs/{job_id}").status_code == 204
        assert client.get(f"/jobs/{job_id}").status_code == 404


def test_async_router_accepts_a_sync_on_delete(sqlite_app_factory):
    seen: list[str] = []
    app, reg = sqlite_app_factory(on_delete=seen.append)
    job_id = asyncio.run(reg.register(_Meta(tag="d"), "pending"))
    with TestClient(app) as client:
        assert client.delete(f"/jobs/{job_id}").status_code == 204
    assert seen == [job_id]


def test_async_router_runs_on_delete_even_for_an_unknown_job(sqlite_app_factory):
    """A 404 delete still sweeps: an artifact directory can outlive its row."""
    seen: list[str] = []
    app, _ = sqlite_app_factory(on_delete=seen.append)
    with TestClient(app) as client:
        assert client.delete("/jobs/ghost").status_code == 404
    assert seen == ["ghost"]


def test_sync_router_calls_a_sync_on_delete():
    seen: list[str] = []
    reg = InMemoryRegistry()
    app = FastAPI()
    app.include_router(build_router(reg, include_delete=True, on_delete=seen.append))
    with TestClient(app) as client:
        client.delete("/jobs/whatever")
    assert seen == ["whatever"]


# ── Cancel route + on_cancel hook (async, sqlite) ──────────────────────────
def test_async_cancel_route_answers_and_calls_on_cancel(sqlite_app_factory):
    """200 with the shape callers read, then the hook with the phase the job
    was in; a second cancel is a 409, an unknown job a 404 — and neither of
    those reaches the hook."""
    seen: list[tuple[str, str]] = []

    async def hook(job_id: str, was_phase: str) -> None:
        seen.append((job_id, was_phase))

    app, reg = sqlite_app_factory(include_cancel=True, on_cancel=hook)
    pending = asyncio.run(reg.register(_Meta(tag="p"), "pending"))
    running = asyncio.run(reg.register(_Meta(tag="r"), "processing"))
    done = asyncio.run(reg.register(_Meta(tag="d"), "pending"))
    asyncio.run(reg.set_result(done, {"ok": True}))
    with TestClient(app) as client:
        r = client.post(f"/jobs/{pending}/cancel")
        assert r.status_code == 200
        assert r.json() == {"job_id": pending, "cancelled": True, "was_phase": "pending"}
        assert client.get(f"/jobs/{pending}").json()["phase"] == "cancelled"
        assert client.post(f"/jobs/{running}/cancel").json()["was_phase"] == "processing"

        r = client.post(f"/jobs/{pending}/cancel")
        assert r.status_code == 409 and "already_cancelled" in r.json()["detail"]
        r = client.post(f"/jobs/{done}/cancel")
        assert r.status_code == 409 and "already_completed" in r.json()["detail"]
        assert client.get(f"/jobs/{done}").json()["phase"] == "completed"
        assert client.post("/jobs/ghost/cancel").status_code == 404
    assert seen == [(pending, "pending"), (running, "processing")]


def test_async_cancel_survives_an_on_cancel_that_raises(sqlite_app_factory):
    """The cancel already happened; side-cleanup that throws must not turn it
    into a 500."""

    def hook(job_id: str, was_phase: str) -> None:  # a sync hook on the async router
        raise RuntimeError("payload directory is on fire")

    app, reg = sqlite_app_factory(include_cancel=True, on_cancel=hook)
    job_id = asyncio.run(reg.register(_Meta(tag="x"), "pending"))
    with TestClient(app) as client:
        r = client.post(f"/jobs/{job_id}/cancel")
        assert r.status_code == 200 and r.json()["cancelled"] is True
        assert client.get(f"/jobs/{job_id}").json()["phase"] == "cancelled"


def test_sync_router_calls_on_cancel():
    reg = InMemoryRegistry()
    seen: list[tuple[str, str]] = []
    app = FastAPI()
    app.include_router(build_router(
        reg, include_cancel=True, on_cancel=lambda job_id, was: seen.append((job_id, was)),
    ))
    observed: dict = {}

    def worker() -> None:
        with reg.job(_Meta(tag="t"), initial_phase="running") as job:
            observed["job_id"] = job.job_id
            job.wait_or_cancel(timeout=5.0)

    with TestClient(app) as client:
        t = threading.Thread(target=worker)
        t.start()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and "job_id" not in observed:
            time.sleep(0.02)
        assert client.post(f"/jobs/{observed['job_id']}/cancel").status_code == 200
        t.join(timeout=5.0)
        assert client.post("/jobs/ghost/cancel").status_code == 404
    assert seen == [(observed["job_id"], "running")]


# ── Long-poll: terminal_phases + ?wait= ────────────────────────────────────
TERMINAL = ("completed", "failed")
# Small enough that the suite stays fast, large enough that a deadline is
# measurable on Windows' ~15 ms timer.
CHECK_S = 0.02


class _ScriptedRegistry:
    """An async registry whose n-th ``get`` of a job answers the n-th phase of
    its script (the last one repeats); ``None`` in a script is the row gone.
    Counts the reads, so a test can see how often the route looked."""

    def __init__(self, **scripts: list[Optional[str]]):
        self.scripts = scripts
        self.reads: dict[str, int] = {}

    async def get(self, job_id: str) -> Optional[JobBase]:
        script = self.scripts.get(job_id)
        if script is None:
            return None
        n = self.reads.get(job_id, 0)
        self.reads[job_id] = n + 1
        phase = script[min(n, len(script) - 1)]
        if phase is None:
            return None
        return JobBase(
            job_id=job_id, phase=phase, created_at="2026-11-06T00:00:00+00:00",
            updated_at="2026-11-06T00:00:00+00:00", elapsed_seconds=0.0, metadata={},
        )

    async def list_all(self):
        return {"active_count": 0, "jobs": []}


def _long_poll_app(registry, **router_kwargs) -> FastAPI:
    router_kwargs.setdefault("terminal_phases", TERMINAL)
    router_kwargs.setdefault("wait_check_interval_s", CHECK_S)
    app = FastAPI()
    app.include_router(build_router(registry, **router_kwargs))
    return app


def _timed_get(client: TestClient, path: str, **params):
    start = time.monotonic()
    response = client.get(path, params=params)
    return response, time.monotonic() - start


def test_long_poll_answers_at_once_when_the_job_is_already_terminal():
    reg = _ScriptedRegistry(j=["completed"])
    with TestClient(_long_poll_app(reg)) as client:
        r, took = _timed_get(client, "/jobs/j", wait=30)
    assert r.status_code == 200 and r.json()["phase"] == "completed"
    assert reg.reads["j"] == 1 and took < 5


def test_long_poll_wakes_when_the_job_finishes_mid_wait():
    reg = _ScriptedRegistry(j=["processing", "processing", "processing", "failed"])
    with TestClient(_long_poll_app(reg)) as client:
        r, took = _timed_get(client, "/jobs/j", wait=30)
    assert r.status_code == 200 and r.json()["phase"] == "failed"
    # Three re-reads after the first, then it answered — long before 30 s.
    assert reg.reads["j"] == 4 and 3 * CHECK_S * 0.5 <= took < 5


def test_long_poll_answers_the_running_job_at_the_deadline():
    reg = _ScriptedRegistry(j=["processing"])
    with TestClient(_long_poll_app(reg)) as client:
        r, took = _timed_get(client, "/jobs/j", wait=0.3)
    assert r.status_code == 200 and r.json()["phase"] == "processing"
    assert 0.25 <= took < 5
    assert reg.reads["j"] > 2      # it kept looking until the deadline


def test_long_poll_never_sleeps_past_the_deadline():
    """A check interval longer than the wait is cut to the wait."""
    reg = _ScriptedRegistry(j=["pending"])
    with TestClient(_long_poll_app(reg, wait_check_interval_s=10.0)) as client:
        r, took = _timed_get(client, "/jobs/j", wait=0.2)
    assert r.json()["phase"] == "pending"
    assert took < 2
    # The first read, then one at the deadline (two if the loop woke a clock
    # tick early — asyncio may, by its clock resolution — and slept the rest).
    assert 2 <= reg.reads["j"] <= 3


def test_long_poll_clamps_a_wait_above_the_cap():
    reg = _ScriptedRegistry(j=["processing"])
    with TestClient(_long_poll_app(reg, max_wait_s=0.2)) as client:
        r, took = _timed_get(client, "/jobs/j", wait=3600)
    assert r.status_code == 200 and r.json()["phase"] == "processing"
    assert 0.15 <= took < 5


def test_long_poll_answers_a_cancelled_job_at_once_when_cancelled_is_terminal():
    """A service whose jobs can end ``cancelled`` lists it, or every wait on
    a cancelled job runs to the cap (the classifier's terminal tuple)."""
    terminal = ("completed", "failed", "cancelled")
    reg = _ScriptedRegistry(j=["cancelled"], k=["processing", "processing", "cancelled"])
    with TestClient(_long_poll_app(reg, terminal_phases=terminal)) as client:
        r, took = _timed_get(client, "/jobs/j", wait=30)
        assert r.status_code == 200 and r.json()["phase"] == "cancelled"
        assert reg.reads["j"] == 1 and took < 5
        r, took = _timed_get(client, "/jobs/k", wait=30)
        assert r.json()["phase"] == "cancelled" and reg.reads["k"] == 3 and took < 5


def test_long_poll_404s_when_the_job_is_deleted_mid_wait():
    reg = _ScriptedRegistry(j=["processing", "processing", None])
    with TestClient(_long_poll_app(reg)) as client:
        r = client.get("/jobs/j", params={"wait": 30})
        assert r.status_code == 404 and r.json()["detail"] == "no job 'j'"
        assert client.get("/jobs/ghost", params={"wait": 30}).status_code == 404
    assert reg.reads["j"] == 3


def test_long_poll_without_wait_answers_at_once():
    reg = _ScriptedRegistry(j=["processing"])
    with TestClient(_long_poll_app(reg)) as client:
        r, took = _timed_get(client, "/jobs/j")
        assert r.json()["phase"] == "processing" and reg.reads["j"] == 1 and took < 5
        r = client.get("/jobs/j", params={"wait": 0})
        assert r.status_code == 200 and reg.reads["j"] == 2


@pytest.mark.parametrize("wait", ["-1", "nan", "inf", "soon"])
def test_long_poll_refuses_a_wait_that_is_not_a_finite_number_of_seconds(wait):
    reg = _ScriptedRegistry(j=["processing"])
    with TestClient(_long_poll_app(reg)) as client:
        assert client.get("/jobs/j", params={"wait": wait}).status_code == 422
    assert reg.reads == {}


def test_long_poll_stops_when_the_client_disconnects():
    """Driven as raw ASGI: the client is gone right after its request, so the
    wait ends at the first check instead of running for 30 s."""
    reg = _ScriptedRegistry(j=["processing"])
    app = _long_poll_app(reg)
    sent: list[dict] = []

    async def drive() -> None:
        messages = iter([{"type": "http.request", "body": b"", "more_body": False}])

        async def receive() -> dict:
            return next(messages, {"type": "http.disconnect"})

        async def send(message: dict) -> None:
            sent.append(message)

        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "GET", "scheme": "http", "path": "/jobs/j", "raw_path": b"/jobs/j",
            "root_path": "", "query_string": b"wait=30", "headers": [],
            "client": ("testclient", 1), "server": ("testserver", 80),
        }
        await asyncio.wait_for(app(scope, receive, send), timeout=10)

    start = time.monotonic()
    asyncio.run(drive())
    assert time.monotonic() - start < 5
    assert reg.reads["j"] <= 2


def test_wait_is_ignored_without_terminal_phases():
    """The route is today's: no ``wait`` parameter, so FastAPI drops it and a
    client sending it to an older server just gets an immediate answer."""
    reg = _ScriptedRegistry(j=["processing"])
    app = _long_poll_app(reg, terminal_phases=None)
    with TestClient(app) as client:
        r, took = _timed_get(client, "/jobs/j", wait=30)
        assert r.status_code == 200 and r.json()["phase"] == "processing"
        assert reg.reads["j"] == 1 and took < 5
        params = client.get("/openapi.json").json()["paths"]["/jobs/{job_id}"]["get"]["parameters"]
    assert [p["name"] for p in params] == ["job_id"]

    sync_app = FastAPI()
    sync_app.include_router(build_router(InMemoryRegistry()))
    with TestClient(sync_app) as client:
        assert client.get("/jobs/nope", params={"wait": 30}).status_code == 404


def test_the_wait_parameter_is_documented_with_its_cap():
    with TestClient(_long_poll_app(_ScriptedRegistry(), max_wait_s=60.0)) as client:
        params = client.get("/openapi.json").json()["paths"]["/jobs/{job_id}"]["get"]["parameters"]
    (wait,) = [p for p in params if p["name"] == "wait"]
    assert wait["in"] == "query" and wait["required"] is False
    assert "clamped to 60" in wait["description"] and "completed, failed" in wait["description"]


def test_terminal_phases_with_a_sync_registry_is_refused():
    with pytest.raises(ValueError, match="async registry"):
        build_router(InMemoryRegistry(), terminal_phases=TERMINAL)


@pytest.mark.parametrize(
    "kwargs, error",
    [
        ({"terminal_phases": ()}, ValueError),
        ({"terminal_phases": "completed"}, TypeError),
        ({"terminal_phases": TERMINAL, "max_wait_s": -1.0}, ValueError),
        ({"terminal_phases": TERMINAL, "max_wait_s": float("inf")}, ValueError),
        ({"terminal_phases": TERMINAL, "wait_check_interval_s": 0.0}, ValueError),
    ],
)
def test_long_poll_settings_that_could_never_end_are_refused(kwargs, error):
    with pytest.raises(error):
        build_router(_ScriptedRegistry(), **kwargs)
