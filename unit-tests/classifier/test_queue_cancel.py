"""The queue: one payload shape, one runner, payloads that survive shutdown,
and a cancel that stops the work.

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

``POST /jobs/{id}/cancel`` (jobs/queue.py § Cancel): a pending job cancelled
is never claimed and is finished by the router's hook (payload gone, progress
``cancelled``, counted once); a running one is stopped by the pool's cancel
check — its runner sees the ``CancelledError``, the row stays ``cancelled``,
``finish_cancelled`` runs in the worker after the runner unwound, and the
worker takes the next job; a running job DELETED loses what it wrote after
the DELETE's hook; a reference job's reference fails with it; a cancel or a
delete that overtakes the submit is never undone by ``enqueue``'s flip; and
through the app, a finished job's cancel is a 409, an unknown one a 404, and
a long-poll on a cancelled job answers at once.

The ``handle_job`` and cancel tests need the classifier's Postgres (the
queue's registry is ``PostgresRegistry`` on ``db.database``, and
``handle_job`` reads the job's model-usage totals); they are marked
``postgres`` (or use the ``need_postgres`` app fixture) and skip without
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
import time
import uuid

import pytest
from prometheus_client import REGISTRY

from common.jobs.postgres import PostgresRegistry

import config
from api.schemas import AssessRequest, ClassifierMetadata
from db import database
from jobs import queue as queue_module
from jobs import runners
from jobs.payloads import PAYLOAD_SCHEMA, SubmittedDocument, build_assess_payload
from llm import usage as llm_usage
from references.store import reference_files, reference_registry
from regions.store import store as artifact_store


async def _make_queue():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="classifier-queue-test-"))
    # The session database, as the container uses it: the registry borrows
    # db.database (here, on pytest-asyncio's loop, one-off connections — see
    # db.py). Every test enqueues its own fresh ids, so sharing the table
    # with the rest of the session never makes a count here ambiguous.
    registry = PostgresRegistry(pool=database)
    await registry.init()
    q = queue_module.ClassifierQueue(registry)
    await q.progress.init()  # handle_job runs every job under a progress gauge
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


# ---------------------------------------------------------------------------
# Cancel — POST /jobs/{id}/cancel and a DELETE of a running job
# ---------------------------------------------------------------------------

# The pool's cancel check, shortened for the tests (the service's is
# config.CANCEL_CHECK_INTERVAL_S, pinned below).
CHECK_S = 0.05


def test_the_queues_pool_is_cancel_aware():
    pool = queue_module.queue.pool
    assert config.CANCEL_CHECK_INTERVAL_S == 1.0
    assert pool.cancel_check_interval == config.CANCEL_CHECK_INTERVAL_S
    assert pool.cancel_phase == "cancelled"
    assert pool.on_cancel == queue_module.queue._on_pool_cancel


def _cancelled_total(job_type: str = "assess") -> float:
    return REGISTRY.get_sample_value(
        "classifier_jobs_total", {"type": job_type, "status": "cancelled"}
    ) or 0.0


async def _quiet_table() -> None:
    """Rows earlier tests left queued — they call handle_job directly, never
    through a pool — moved out of the way of this test's workers, which would
    otherwise claim them (and fail them: their payloads are in another
    test's directory)."""
    async with database.acquire() as conn:
        await conn.execute(
            "UPDATE jobs SET phase = 'failed', error = 'left queued by an earlier test' "
            "WHERE phase IN ('staging', 'pending', 'processing')"
        )


async def _started_queue(monkeypatch, runner):
    """A queue whose pool runs ``runner`` as run_assess, checking for a
    cancel every CHECK_S. ``pool.start()``, not ``queue.start()``: no
    ``recover()`` of other tests' rows."""
    q, registry = await _make_queue()
    monkeypatch.setattr(queue_module, "run_assess", runner)
    await _quiet_table()
    q.pool.cancel_check_interval = CHECK_S
    q.pool.poll_interval = 0.05
    q.pool.start()
    return q, registry


async def _until(condition, timeout: float = 10.0) -> None:
    """Await ``condition()`` (a coroutine function) until it is true."""
    deadline = time.monotonic() + timeout
    while not await condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for the queue")
        await asyncio.sleep(0.02)


class _Hang:
    """A runner that waits until it is cancelled, recording that it was."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = 0

    async def __call__(self, payload):
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_a_pending_job_cancelled_is_finished_by_the_hook_and_never_claimed(monkeypatch):
    q, registry = await _make_queue()
    ran = []

    async def runner(payload):
        ran.append(payload)
        return {}

    monkeypatch.setattr(queue_module, "run_assess", runner)
    job_id = await _enqueue(q, registry, _payload())
    assert (await registry.get(job_id)).phase == "pending"
    before = _cancelled_total()

    # What the router does: the cancel, then its on_cancel hook.
    assert await registry.cancel(job_id) == (True, "pending")
    await q.on_cancel_request(job_id, "pending")

    job = await registry.get(job_id)
    assert job.phase == "cancelled" and job.result is None and job.error is None
    assert await q.payloads.read(job_id) is None
    assert job.metadata["progress"]["state"] == "cancelled"
    history = await q.progress.history(job_id)
    assert history["attempt"] == 0  # it never ran
    assert [e["state"] for e in history["events"]] == ["queued", "cancelled"]
    assert _cancelled_total() - before == 1
    # The cleanup is idempotent (enqueue and the pool may run it again).
    await q.finish_cancelled(job_id, job.metadata, gone=False)
    assert len((await q.progress.history(job_id))["events"]) == 2

    # Never claimed: nothing pending is left for a worker to take.
    await _quiet_table()
    assert await registry.claim_next() is None
    assert ran == []


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_a_running_job_cancelled_is_stopped_and_the_worker_goes_on(monkeypatch):
    hang = _Hang()
    q, registry = await _started_queue(monkeypatch, hang)
    try:
        job_id = await _enqueue(q, registry, _payload())
        await asyncio.wait_for(hang.started.wait(), 10)
        assert (await registry.get(job_id)).phase == "processing"
        assert await q.payloads.read(job_id) is not None
        before = _cancelled_total()

        asked = time.monotonic()
        assert await registry.cancel(job_id) == (True, "processing")
        await q.on_cancel_request(job_id, "processing")  # the pool's job, not the hook's

        async def finished() -> bool:
            job = await registry.get(job_id)
            return (job.metadata.get("progress") or {}).get("state") == "cancelled"

        await _until(finished)
        assert time.monotonic() - asked < 5
        assert hang.cancelled == 1
        job = await registry.get(job_id)
        assert (job.phase, job.result, job.error) == ("cancelled", None, None)
        assert await q.payloads.read(job_id) is None
        # The last event of the latest attempt (attempt 0's queued one when
        # the gauge was stopped before its first flush, about a second in).
        events = (await q.progress.history(job_id))["events"]
        assert events[-1]["state"] == "cancelled"
        # Counted once, by the pool's on_finish — the hook left a running job alone.
        assert _cancelled_total() - before == 1

        # The worker is free: the next job runs to completion.
        async def finish(payload):
            return {"ok": True}

        monkeypatch.setattr(queue_module, "run_assess", finish)
        nxt = await _enqueue(q, registry, _payload())

        async def done() -> bool:
            return (await registry.get(nxt)).phase == "completed"

        await _until(done)
        assert q.pool.running
    finally:
        await q.pool.stop()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_a_running_job_deleted_loses_what_it_wrote_after_the_delete(monkeypatch):
    """The DELETE's hook runs while the runner can still write; the pool's
    cleanup, after the runner unwound, takes the usage rows and the artifact
    directory it wrote since."""
    started, wrote = asyncio.Event(), asyncio.Event()

    async def writes_while_stopping(payload):
        job_id = llm_usage.job_id_var.get()
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            # A unit that finished as the job was deleted: its model call
            # recorded, its artifact directory written.
            await llm_usage.usage_store.record({
                "job_id": job_id, "job_type": "assess", "started_at": llm_usage.now_iso(),
                "label": "score/late", "kind": "score", "outcome": "ok", "seconds": 0.1,
            })
            artifact_store.write_json(job_id, "regions.json", {"regions": []})
            wrote.set()

    q, registry = await _started_queue(monkeypatch, writes_while_stopping)
    try:
        job_id = await _enqueue(q, registry, _payload())
        await asyncio.wait_for(started.wait(), 10)
        before = _cancelled_total()
        # What DELETE /jobs/{id} does: the on_delete hook, then the row.
        await queue_module.delete_job_side_data(job_id)
        assert await registry.delete(job_id)
        await asyncio.wait_for(wrote.wait(), 10)

        async def cleaned() -> bool:
            return (not artifact_store.exists(job_id)
                    and await llm_usage.usage_store.calls(job_id) == [])

        await _until(cleaned)
        assert await q.payloads.read(job_id) is None
        assert await registry.get(job_id) is None
        assert _cancelled_total() - before == 1  # a running job deleted counts as cancelled
    finally:
        await q.pool.stop()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_a_cancelled_reference_job_fails_its_reference(monkeypatch):
    await reference_registry.init()
    hang = _Hang()
    q, registry = await _started_queue(monkeypatch, _Hang())  # assess: unused here
    monkeypatch.setattr(queue_module, "run_reference", hang)
    try:
        reference_id = "r" + uuid.uuid4().hex[:12]
        job_id = await registry.register(
            ClassifierMetadata(type="reference", request_id="test", reference_id=reference_id),
            initial_phase="staging",
        )
        await reference_registry.create(reference_id, source_kind="upload", job_id=job_id)
        reference_files.write_json(reference_id, "record.json", {"criteria": []})
        await q.enqueue(job_id, {"type": "reference", "reference_id": reference_id})
        await asyncio.wait_for(hang.started.wait(), 10)
        before = _cancelled_total("reference")

        assert await registry.cancel(job_id) == (True, "processing")

        async def failed() -> bool:
            ref = await reference_registry.get(reference_id)
            return ref.status == "failed"

        await _until(failed)
        ref = await reference_registry.get(reference_id)
        # Failed by the cancel's cleanup, not by handle_job's failure path.
        assert ref.error == f"creation job {job_id} was cancelled"
        assert not reference_files.exists(reference_id)
        assert hang.cancelled == 1
        assert (await registry.get(job_id)).phase == "cancelled"
        assert _cancelled_total("reference") - before == 1
    finally:
        await q.pool.stop()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_a_cancel_or_delete_mid_submit_is_never_undone(monkeypatch):
    """The cancel lands between the register and the flip — here before the
    payload even exists, so the router's hook had nothing to clean. The
    guarded flip misses and enqueue cleans up what it just wrote."""
    q, registry = await _make_queue()
    job_id = await registry.register(
        ClassifierMetadata(type="assess", request_id="test"), initial_phase="staging"
    )
    assert await registry.cancel(job_id) == (True, "staging")
    await q.on_cancel_request(job_id, "staging")
    await q.enqueue(job_id, _payload())  # the submit, overtaken

    job = await registry.get(job_id)
    assert job.phase == "cancelled"
    assert await q.payloads.read(job_id) is None
    assert job.metadata["progress"]["state"] == "cancelled"
    assert (await q.progress.history(job_id))["events"][-1]["state"] == "cancelled"

    # Deleted mid-submit: no row to flip, nothing left on disk.
    gone = await registry.register(
        ClassifierMetadata(type="assess", request_id="test"), initial_phase="staging"
    )
    assert await registry.delete(gone)
    await q.enqueue(gone, _payload())
    assert await registry.get(gone) is None
    assert await q.payloads.read(gone) is None


# ---------------------------------------------------------------------------
# Through the app — kept last, and per test, so the app's own pool never runs
# beside the pools above (it would claim their jobs).
# ---------------------------------------------------------------------------


@pytest.fixture
def client(need_postgres):
    from fastapi.testclient import TestClient

    import main

    with TestClient(main.app) as c:
        yield c


def _wait_phase(client, job_id: str, phases: tuple[str, ...], timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        job = client.get(f"/jobs/{job_id}").json()
        if job.get("phase") in phases or time.monotonic() > deadline:
            return job
        time.sleep(0.05)


def _text_assess() -> dict:
    return {"document": {"type": "text", "data": "Notice to Owner"},
            "criteria": [{"name": "Notice to Owner", "type": "text"}]}


def test_cancel_through_the_app_stops_a_running_job(client, monkeypatch):
    hang = _Hang()
    monkeypatch.setattr(queue_module, "run_assess", hang)
    r = client.post("/assess", json=_text_assess())
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    assert _wait_phase(client, job_id, ("processing",))["phase"] == "processing"

    r = client.post(f"/jobs/{job_id}/cancel")
    assert r.status_code == 200
    assert r.json() == {"job_id": job_id, "cancelled": True, "was_phase": "processing"}
    # Cancelled is terminal for the long-poll: it answers at once.
    started = time.monotonic()
    job = client.get(f"/jobs/{job_id}", params={"wait": 30}).json()
    assert job["phase"] == "cancelled" and time.monotonic() - started < 5
    # Within about CANCEL_CHECK_INTERVAL_S the runner is stopped and the
    # progress reads cancelled.
    deadline = time.monotonic() + 10
    while client.get(f"/jobs/{job_id}/progress").json()["progress"]["state"] != "cancelled":
        assert time.monotonic() < deadline, "the running job was never stopped"
        time.sleep(0.05)
    assert hang.cancelled == 1
    r = client.post(f"/jobs/{job_id}/cancel")
    assert r.status_code == 409 and "already_cancelled" in r.json()["detail"]
    assert client.delete(f"/jobs/{job_id}").status_code == 204


def test_cancel_through_the_app_refuses_finished_and_unknown_jobs(client):
    r = client.post("/assess", json=_text_assess())
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    job = client.get(f"/jobs/{job_id}", params={"wait": 30}).json()
    assert job["phase"] == "completed", job
    r = client.post(f"/jobs/{job_id}/cancel")
    assert r.status_code == 409 and "already_completed" in r.json()["detail"]
    assert client.get(f"/jobs/{job_id}").json()["phase"] == "completed"
    assert client.post("/jobs/no-such-job/cancel").status_code == 404
    assert client.delete(f"/jobs/{job_id}").status_code == 204


def test_a_staged_job_cancelled_through_the_app_answers_a_wait_at_once(client):
    """A job no worker owns — finished by the router's hook in the request."""
    from jobs.queue import jobs_registry, queue

    job_id = asyncio.run(jobs_registry.register(
        ClassifierMetadata(type="assess", request_id="test"), initial_phase="staging"
    ))
    asyncio.run(queue.payloads.write(job_id, {"x": 1}))
    r = client.post(f"/jobs/{job_id}/cancel")
    assert r.status_code == 200 and r.json()["was_phase"] == "staging"
    started = time.monotonic()
    job = client.get(f"/jobs/{job_id}", params={"wait": 30}).json()
    assert job["phase"] == "cancelled" and time.monotonic() - started < 5
    assert job["metadata"]["progress"]["state"] == "cancelled"
    assert asyncio.run(queue.payloads.read(job_id)) is None
    assert client.delete(f"/jobs/{job_id}").status_code == 204
