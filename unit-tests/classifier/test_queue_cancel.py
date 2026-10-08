"""The queue: one payload shape, one runner, and payloads that survive shutdown.

`docker compose stop` (and therefore `make up classifier` on a rebuild) sends
SIGTERM; uvicorn runs the lifespan shutdown, ``ClassifierQueue.stop()`` cancels
the worker tasks, and the row is left in "processing" for the next start's
``recover()`` to requeue. That only works if the job's INPUT is still on disk
when the requeued row is claimed — so a cancelled ``handle_job`` must NOT
delete the payload, while a completed or failed one still must.

Also pinned: the queue runs exactly one job type ("assess"); a row of a
removed type ("compare" / "locate", queued by an older container) fails with
a message naming it, and a payload without ``schema: 3`` (a list of
documents) is refused by the runner rather than half-run — schema 2's single
document included.

The four ``handle_job`` tests need the classifier's Postgres (the queue's
registry is ``PostgresRegistry`` on ``db.database``, and ``handle_job`` reads
the job's model-usage totals); they are marked ``postgres`` and skip without
``TEST_POSTGRES_DSN`` — see conftest.py.

Run with::

    TEST_POSTGRES_DSN=postgresql://postgres@localhost:5432/postgres \\
    UV_LINK_MODE=copy uv run --no-sync --with pytest --package classifier \\
        python -m pytest unit-tests/classifier/test_queue_cancel.py -q -p no:cacheprovider
"""

from __future__ import annotations

import asyncio
import pathlib
import tempfile

import pytest

from common.jobs.postgres import PostgresRegistry

from api.schemas import AssessRequest, ClassifierMetadata
from db import database
from jobs import queue as queue_module
from jobs import runners
from jobs.payloads import PAYLOAD_SCHEMA, SubmittedDocument, build_assess_payload


async def _make_queue():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="classifier-queue-test-"))
    # The session database, as the container uses it: the registry borrows
    # db.database (here, on pytest-asyncio's loop, one-off connections — see
    # db.py). Every test enqueues its own fresh ids, so sharing the table
    # with the rest of the session never makes a count here ambiguous.
    registry = PostgresRegistry(pool=database)
    await registry.init()
    q = queue_module.ClassifierQueue(registry)
    # Keep this test's payloads away from the session-wide PAYLOAD_DIR.
    q.payloads = type(q.payloads)(str(tmp / "payloads"))
    return q, registry


async def _enqueue(q, registry, payload: dict, job_type: str = "assess") -> str:
    job_id = await registry.register(
        ClassifierMetadata(type=job_type, request_id="test"), initial_phase="staging"
    )
    await q.enqueue(job_id, payload)
    return job_id


def _payload(job_id=None) -> dict:
    request = AssessRequest.model_validate(
        {
            "document": {"type": "text", "data": "Notice to Owner"},
            "criteria": [{"name": "Notice to Owner", "type": "text"}],
        }
    )
    return build_assess_payload(
        request,
        [SubmittedDocument(raw=b"Notice to Owner", filename="inline.txt",
                           content_type=None, kind="txt", pages=1)],
        job_id=job_id,
    )


def test_the_payload_shape():
    payload = _payload("abc")
    assert payload["schema"] == PAYLOAD_SCHEMA == 3
    assert set(payload) == {"schema", "documents", "criteria", "job_id"}
    (doc,) = payload["documents"]
    assert set(doc) == {"file_b64", "filename", "content_type", "kind", "pages"}
    assert doc["pages"] == 1 and doc["kind"] == "txt"
    assert payload["criteria"][0]["options"]["match"] == "contains"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_cancelled_job_keeps_its_payload(monkeypatch):
    q, registry = await _make_queue()
    started = asyncio.Event()

    async def hang_forever(payload):
        started.set()
        await asyncio.Event().wait()  # never returns — the job is "in flight"

    monkeypatch.setattr(queue_module, "run_assess", hang_forever)
    job_id = await _enqueue(q, registry, _payload())
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
        return {"ok": True, "schema": payload["schema"]}

    monkeypatch.setattr(queue_module, "run_assess", finish)
    result = await q.handle_job(job)
    # handle_job adds the job's model-usage totals (none here: no model call).
    assert result.pop("usage")["calls"] == 0
    assert result == {"ok": True, "schema": 3}
    assert await q.payloads.read(job_id) is None  # consumed on completion


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_completed_and_failed_jobs_delete_their_payload(monkeypatch):
    q, registry = await _make_queue()

    async def succeed(payload):
        return {"ok": True}

    async def explode(payload):
        raise RuntimeError("boom")

    monkeypatch.setattr(queue_module, "run_assess", succeed)
    done_id = await _enqueue(q, registry, {"x": 1})
    await q.handle_job(await registry.get(done_id))
    assert await q.payloads.read(done_id) is None

    monkeypatch.setattr(queue_module, "run_assess", explode)
    failed_id = await _enqueue(q, registry, {"x": 2})
    with pytest.raises(RuntimeError):
        await q.handle_job(await registry.get(failed_id))
    assert await q.payloads.read(failed_id) is None


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("job_type", ["compare", "locate"])
async def test_removed_job_types_fail_by_name(monkeypatch, job_type):
    q, registry = await _make_queue()
    called = []

    async def runner(payload):
        called.append(payload)
        return {}

    monkeypatch.setattr(queue_module, "run_assess", runner)
    job_id = await _enqueue(q, registry, {"request": {}}, job_type=job_type)
    with pytest.raises(RuntimeError, match=f"job type '{job_type}' no longer exists"):
        await q.handle_job(await registry.get(job_id))
    assert called == []
    assert await q.payloads.read(job_id) is None


@pytest.mark.asyncio
async def test_a_stale_payload_is_refused_by_the_runner():
    """An old-shape payload (no schema, or schema 2) must not be run against new code."""
    with pytest.raises(runners.StalePayloadError, match="resubmit"):
        await runners.run_assess({"file_b64": "", "criteria": [], "ocr": "auto"})
    with pytest.raises(runners.StalePayloadError, match="payload schema 2"):
        await runners.run_assess({"schema": 2, "file_b64": "", "criteria": []})


@pytest.mark.asyncio
async def test_the_runner_runs_a_real_payload():
    """End to end below the HTTP layer: a text document, no model involved."""
    result = await runners.run_assess(_payload())
    entry = result["assessment"]["per_criterion_scores"]["Notice to Owner"]
    assert result["schema_version"] == 3
    assert [d["kind"] for d in result["documents"]] == ["txt"]
    assert entry["status"] == "ok" and entry["verdict"] == "PASS"
    assert result["artifacts"] is None  # no job id → nothing written
