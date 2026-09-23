"""A job interrupted by shutdown must keep its payload so it can be re-run.

`docker compose stop` (and therefore `make up classifier` on a rebuild) sends
SIGTERM; uvicorn runs the lifespan shutdown, ``ClassifierQueue.stop()`` cancels
the worker tasks, and the row is left in "processing" for the next start's
``recover()`` to requeue. That only works if the job's INPUT is still on disk
when the requeued row is claimed — so a cancelled ``handle_job`` must NOT
delete the payload, while a completed or failed one still must.

Run with::

    UV_LINK_MODE=copy uv run --no-sync --with pytest --package classifier \\
        python -m pytest unit-tests/classifier/test_queue_cancel.py -q -p no:cacheprovider
"""

from __future__ import annotations

import asyncio
import pathlib
import tempfile

import pytest

from common.jobs.sqlite import SqliteRegistry

from api.schemas import ClassifierMetadata
from jobs import queue as queue_module


async def _make_queue():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="classifier-queue-test-"))
    registry = SqliteRegistry(str(tmp / "jobs.db"))
    await registry.init()
    q = queue_module.ClassifierQueue(registry)
    # Keep this test's payloads away from the session-wide PAYLOAD_DIR.
    q.payloads = type(q.payloads)(str(tmp / "payloads"))
    return q, registry


async def _enqueue(q, registry, payload: dict) -> str:
    job_id = await registry.register(
        ClassifierMetadata(type="assess", request_id="test"), initial_phase="staging"
    )
    await q.enqueue(job_id, payload)
    return job_id


@pytest.mark.asyncio
async def test_cancelled_job_keeps_its_payload(monkeypatch):
    q, registry = await _make_queue()
    started = asyncio.Event()

    async def hang_forever(payload):
        started.set()
        await asyncio.Event().wait()  # never returns — the job is "in flight"

    monkeypatch.setitem(queue_module._RUNNERS, "assess", hang_forever)
    job_id = await _enqueue(q, registry, {"file_b64": "", "criteria": [], "ocr": "auto"})
    job = await registry.get(job_id)

    task = asyncio.create_task(q.handle_job(job))
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The input survives so the next start can re-run the job...
    assert await q.payloads.read(job_id) is not None
    # ...and a recover() → claim → handle_job cycle then finds it.
    async def finish(payload):
        return {"ok": True}

    monkeypatch.setitem(queue_module._RUNNERS, "assess", finish)
    assert await q.handle_job(job) == {"ok": True}
    assert await q.payloads.read(job_id) is None  # consumed on completion


@pytest.mark.asyncio
async def test_completed_and_failed_jobs_delete_their_payload(monkeypatch):
    q, registry = await _make_queue()

    async def succeed(payload):
        return {"ok": True}

    async def explode(payload):
        raise RuntimeError("boom")

    monkeypatch.setitem(queue_module._RUNNERS, "assess", succeed)
    done_id = await _enqueue(q, registry, {"x": 1})
    await q.handle_job(await registry.get(done_id))
    assert await q.payloads.read(done_id) is None

    monkeypatch.setitem(queue_module._RUNNERS, "assess", explode)
    failed_id = await _enqueue(q, registry, {"x": 2})
    with pytest.raises(RuntimeError):
        await q.handle_job(await registry.get(failed_id))
    assert await q.payloads.read(failed_id) is None
