"""Tests for common.jobs.router.build_router — sync + async backends.

Uses FastAPI's TestClient which drives the app end-to-end. Verifies that
both the InMemoryRegistry (sync) and SqliteRegistry (async) paths mount
cleanly and return the expected shapes.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from common.jobs import InMemoryRegistry
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
def sqlite_app(tmp_path: Path):
    db_path = str(tmp_path / "jobs.db")
    reg = SqliteRegistry(db_path)
    asyncio.run(reg.init())
    app = FastAPI()
    app.include_router(build_router(reg, include_delete=True))
    return app, reg


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
