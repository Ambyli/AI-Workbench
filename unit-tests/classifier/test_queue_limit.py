"""The queue limit: CLASSIFIER_QUEUE_LIMIT, a 429 + Retry-After at the cap.

``jobs/queue.py`` § Queue limit: the limit counts OPEN jobs (staging +
pending + processing — waiting AND running). ``check_room`` — the first line
of ``POST /assess`` and ``POST /references``, before the body is read — and
``admit`` — the registration itself, ``register(max_open=)``, atomic — both
raise ``QueueFullError``, which ``main`` answers 429 with FastAPI's
``detail`` and a ``Retry-After`` estimated from recent run times.

Pinned here:

* at the cap a submit is a 429 with an integer Retry-After and a detail
  naming CLASSIFIER_QUEUE_LIMIT; ``classifier_jobs_rejected_total`` counts
  it and ``jobs_total{status="pending"}`` does not; no row is written; once a
  job finishes the next submit is a 202 — through the pre-check AND through
  ``admit`` alone (the pre-check waved through, as when the queue fills
  between the two);
* a limit below CLASSIFIER_MAX_CONCURRENT narrows how many jobs run: under a
  burst of concurrent submits the pool never has more than ``limit`` in
  flight, and startup warns that workers will idle;
* ``POST /references`` at the cap leaves no reference, no job row;
* a full queue answers before a ``type: "url"`` document is fetched;
* open rows already over the limit (as after a restart: startup logs it)
  refuse until they drain, and the Retry-After grows with the excess;
* a limit of 0 admits every submit, and ``GET /document-kinds`` reports
  ``limits.queue_limit: null``;
* ``retry_after_estimate``: the default with no history, the median × excess
  ÷ busy arithmetic, rounding up, both clamps; ``_on_finish`` feeds it
  completed and failed run times, never a cancel's.

The app tests need the classifier's Postgres (they use ``need_postgres`` and
skip without ``TEST_POSTGRES_DSN`` — conftest.py). Each runs the app with
``run_assess`` replaced by a gate that holds every job until the test
releases it, so the open count is exactly what the test makes it, and moves
any earlier test's open rows out of the way first (the jobs table is shared
by the session).

Run with::

    TEST_POSTGRES_DSN=postgresql://postgres@localhost:5432/postgres \\
    UV_LINK_MODE=copy uv run --no-sync --with pytest --package classifier \\
        python -m pytest unit-tests/classifier/test_queue_limit.py -q -p no:cacheprovider
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Iterator, Optional

import pytest
from prometheus_client import REGISTRY

import config
from api.schemas import ClassifierMetadata
from common.jobs.model import JobBase
from db import database
from jobs import queue as queue_module
from jobs.queue import QueueFullError, retry_after_estimate
from llm import usage as llm_usage
from references.store import reference_registry


# ---------------------------------------------------------------------------
# The estimate — pure, no database
# ---------------------------------------------------------------------------


def test_the_constants_are_pinned():
    if "CLASSIFIER_QUEUE_LIMIT" not in os.environ:
        assert config.QUEUE_LIMIT == 0  # the default: no limit, today's behaviour
    assert (config.QUEUE_RETRY_AFTER_DEFAULT_S, config.QUEUE_RETRY_AFTER_MIN_S,
            config.QUEUE_RETRY_AFTER_MAX_S, config.QUEUE_RETRY_AFTER_HISTORY) == (10, 2, 60, 50)
    assert queue_module.queue.queue_limit == config.QUEUE_LIMIT
    assert queue_module.queue.durations.maxlen == config.QUEUE_RETRY_AFTER_HISTORY


def test_no_history_gives_the_default():
    assert retry_after_estimate([], 4, limit=4, workers=4) == 10
    assert retry_after_estimate(deque(), 40, limit=4, workers=4) == 10
    # No limit (a refusal cannot happen then, but the function stays total).
    assert retry_after_estimate([30.0], 4, limit=0, workers=4) == 10


def test_median_times_excess_over_busy():
    runs = [10.0, 20.0, 30.0, 400.0]  # median 25: one slow outlier does not move it
    # At the limit: one slot must free; four jobs finish in parallel.
    assert retry_after_estimate(runs, 4, limit=4, workers=4) == 7       # ceil(25 / 4)
    # Three over (a restart, a lowered limit): three slots must free.
    assert retry_after_estimate(runs, 6, limit=4, workers=4) == 19      # ceil(25 × 3 / 4)
    # A limit below the workers: only `limit` jobs run, so only they finish.
    assert retry_after_estimate(runs, 2, limit=2, workers=4) == 13      # ceil(25 / 2)
    # A limit above the workers: the workers are what finish jobs.
    assert retry_after_estimate(runs, 32, limit=32, workers=4) == 7     # ceil(25 / 4)
    # Open below the limit (a racing pre-check) still waits for one slot.
    assert retry_after_estimate(runs, 1, limit=4, workers=4) == 7
    # Rounded UP to a whole second.
    assert retry_after_estimate([9.0], 2, limit=2, workers=2) == 5      # 4.5 → 5


def test_both_clamps():
    assert retry_after_estimate([0.5, 1.0, 1.5], 4, limit=4, workers=4) == 2   # floor 2 s
    assert retry_after_estimate([600.0], 4, limit=4, workers=4) == 60          # ceiling 60 s
    assert retry_after_estimate([30.0], 40, limit=4, workers=4) == 60


class _StubRegistry:
    """count_open only — what check_room reads."""

    def __init__(self, open_jobs: int) -> None:
        self.open_jobs = open_jobs
        self.calls = 0

    async def count_open(self) -> int:
        self.calls += 1
        return self.open_jobs


def _job(job_type: str = "assess") -> JobBase:
    return JobBase(job_id="j0", phase="processing", created_at="2026-11-06T00:00:00+00:00",
                   updated_at="2026-11-06T00:00:00+00:00", elapsed_seconds=0.0,
                   metadata={"type": job_type})


def test_on_finish_feeds_the_estimate_completed_and_failed_only():
    q = queue_module.ClassifierQueue(_StubRegistry(0))
    q._on_finish(_job(), "completed", 12.0, None)
    q._on_finish(_job(), "failed", 30.0, "boom")
    q._on_finish(_job(), "cancelled", 0.4, None)  # when someone stopped it, not a run time
    assert list(q.durations) == [12.0, 30.0]
    q.queue_limit = 2
    assert q.retry_after_s(2) == retry_after_estimate([12.0, 30.0], 2, limit=2,
                                                      workers=q.pool.concurrency)


def test_check_room_reads_nothing_without_a_limit_and_refuses_at_it():
    registry = _StubRegistry(5)
    q = queue_module.ClassifierQueue(registry)
    q.queue_limit = 0
    asyncio.run(q.check_room("assess"))
    assert registry.calls == 0
    q.queue_limit = 6
    asyncio.run(q.check_room("assess"))  # 5 open < 6
    q.queue_limit = 5
    with pytest.raises(QueueFullError) as info:
        asyncio.run(q.check_room("reference"))
    err = info.value
    assert (err.job_type, err.open_jobs, err.limit, err.retry_after_s) == ("reference", 5, 5, 10)
    assert err.detail == ("the classifier queue is full: 5 jobs waiting or running, the "
                          "CLASSIFIER_QUEUE_LIMIT cap (5). Retry after 10 s.")


# ---------------------------------------------------------------------------
# The in-repo batch tools wait out a 429 and resubmit
# ---------------------------------------------------------------------------


class _Answer:
    def __init__(self, status_code: int, retry_after: Optional[str] = None) -> None:
        self.status_code = status_code
        self.headers = {"Retry-After": retry_after} if retry_after is not None else {}


def _reference_box_assess():
    """bin/reference_box_assess.py, loaded the way its own test loads it."""
    import importlib.util
    import pathlib
    import sys

    if "reference_box_assess" in sys.modules:
        return sys.modules["reference_box_assess"]
    path = pathlib.Path(__file__).resolve().parents[2] / "ai" / "classifier" / "bin" / \
        "reference_box_assess.py"
    spec = importlib.util.spec_from_file_location("reference_box_assess", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["reference_box_assess"] = module
    spec.loader.exec_module(module)
    return module


def test_the_batch_tools_sleep_retry_after_and_resubmit(monkeypatch, capsys):
    import regions_report

    rba = _reference_box_assess()
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", slept.append)

    class Client:
        def __init__(self, answers):
            self.answers, self.posts = list(answers), []

        def post(self, path, **kwargs):
            self.posts.append((path, kwargs))
            return self.answers.pop(0)

        def request(self, method, path, **kwargs):  # regions_report's transport
            return self.post(path, **kwargs)

    # reference_box_assess: two 429s (seconds, then no header → 10 s), then 202.
    client = Client([_Answer(429, "7"), _Answer(429), _Answer(202)])
    response = rba._post_admitted(client, "/assess", "POST /assess (p1 #0)", data={"a": "1"})
    assert response.status_code == 202
    assert slept == [7.0, 10.0]
    assert [p for p, _ in client.posts] == ["/assess"] * 3
    assert all(kw == {"data": {"a": "1"}} for _, kw in client.posts)  # the same request each time
    assert "queue is full (429)" in capsys.readouterr().out

    # A Retry-After past the tool's cap sleeps the cap.
    slept.clear()
    client = Client([_Answer(429, "3600"), _Answer(202)])
    assert rba._post_admitted(client, "/references", "x").status_code == 202
    assert slept == [rba.QUEUE_FULL_RETRY_MAX_S]

    # regions_report.submit: the same loop around a case.
    slept.clear()
    case = regions_report.Case(name="case", folder="f", index=0, method="POST",
                               path="/v1/classifier/assess", description="", headers={},
                               json_body={"documents": []})
    transport = Client([_Answer(429, "3"), _Answer(202)])
    assert regions_report.submit(case, transport).status_code == 202
    assert slept == [3.0] and len(transport.posts) == 2

    # The total wait is bounded: past it, the 429 comes back to the caller.
    monkeypatch.setattr(rba, "QUEUE_FULL_WAIT_S", 0.0)
    client = Client([_Answer(429, "5")])
    assert rba._post_admitted(client, "/assess", "x").status_code == 429
    assert len(client.posts) == 1


# ---------------------------------------------------------------------------
# Through the app
# ---------------------------------------------------------------------------


class _Gate:
    """A ``run_assess`` that holds each job until the test releases it.

    It runs on the app's event loop thread and the test releases from its
    own, so the state is plain sets under a lock, polled every 20 ms. It
    also records the pool's in-flight count while it waits — the most jobs
    the queue ever ran at once."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.started: list[str] = []
        self.released: set[str] = set()
        self.everything = False
        self.peak_in_flight = 0

    async def __call__(self, payload) -> dict:
        job_id = llm_usage.job_id_var.get()
        with self._lock:
            self.started.append(job_id)
        while True:
            with self._lock:
                self.peak_in_flight = max(self.peak_in_flight, queue_module.queue.pool.in_flight)
                if self.everything or job_id in self.released:
                    return {"ok": True}
            await asyncio.sleep(0.02)

    def release(self, job_id: str) -> None:
        with self._lock:
            self.released.add(job_id)

    def release_all(self) -> None:
        with self._lock:
            self.everything = True

    def wait_started(self, count: int, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                if len(self.started) >= count:
                    return
            assert time.monotonic() < deadline, f"only {len(self.started)} job(s) started"
            time.sleep(0.02)


async def _quiet_table() -> None:
    """Every open row an earlier test (or this one) left behind, failed — the
    queue limit counts the whole table."""
    async with database.acquire() as conn:
        await conn.execute(
            "UPDATE jobs SET phase = 'failed', error = 'left open by an earlier test' "
            "WHERE phase NOT IN ('completed', 'failed', 'cancelled')"
        )


def _counts() -> dict[str, int]:
    return asyncio.run(queue_module.jobs_registry.count_by_phase())


def _rows() -> int:
    return sum(_counts().values())


def _open() -> int:
    return asyncio.run(queue_module.jobs_registry.count_open())


def _sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _rejected(job_type: str) -> float:
    return _sample("classifier_jobs_rejected_total", type=job_type, reason="queue_full")


def _accepted(job_type: str) -> float:
    return _sample("classifier_jobs_total", type=job_type, status="pending")


@contextmanager
def _running_app(monkeypatch, limit: int, *, rows_before_start=None) -> Iterator[tuple]:
    """The app, with ``run_assess`` gated and CLASSIFIER_QUEUE_LIMIT =
    ``limit`` on its queue. ``rows_before_start`` (a callable) runs after the
    table is quieted and before the lifespan — rows it writes are what a
    restart would find. On exit every gated job is released and drained
    before the lifespan stops (a worker cancelled mid-job would leave its row
    processing for the next test's start to requeue)."""
    from fastapi.testclient import TestClient

    import main

    gate = _Gate()
    monkeypatch.setattr(queue_module, "run_assess", gate)
    monkeypatch.setattr(queue_module.queue, "queue_limit", limit)
    monkeypatch.setattr(queue_module.queue, "durations", deque(maxlen=50))
    asyncio.run(queue_module.jobs_registry.init())  # the table, when this file runs alone
    asyncio.run(_quiet_table())
    if rows_before_start is not None:
        rows_before_start()
    try:
        with TestClient(main.app) as client:
            try:
                yield client, gate
            finally:
                gate.release_all()
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline and (
                    queue_module.queue.pool.in_flight or _counts().get("pending", 0)
                ):
                    time.sleep(0.05)
    finally:
        asyncio.run(_quiet_table())


def _text_assess() -> dict:
    return {"document": {"type": "text", "data": "Notice to Owner"},
            "criteria": [{"name": "Notice to Owner", "type": "text"}]}


def _submit(client):
    return client.post("/assess", json=_text_assess())


def _accepted_id(response) -> str:
    assert response.status_code == 202, response.text
    return response.json()["job_id"]


def _until(condition, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


def _finish(client, gate: _Gate, job_id: str) -> None:
    gate.release(job_id)
    job = client.get(f"/jobs/{job_id}", params={"wait": 20}).json()
    assert job["phase"] == "completed", job


def _assert_refused(response, *, open_jobs: int, limit: int,
                    retry_after: Optional[int] = None) -> int:
    assert response.status_code == 429, response.text
    header = response.headers["Retry-After"]
    assert header.isdigit(), header
    seconds = int(header)
    assert 2 <= seconds <= 60
    if retry_after is not None:
        assert seconds == retry_after
    assert response.json() == {"detail": (
        f"the classifier queue is full: {open_jobs} jobs waiting or running, the "
        f"CLASSIFIER_QUEUE_LIMIT cap ({limit}). Retry after {seconds} s."
    )}
    return seconds


async def _waved_through(job_type: str) -> None:
    """check_room, out of the way: the request goes on to admit, as one does
    when the queue fills between the pre-check and the registration."""
    return None


def test_at_the_limit_a_submit_is_a_429_and_writes_nothing(need_postgres, monkeypatch):
    with _running_app(monkeypatch, limit=2) as (client, gate):
        a = _accepted_id(_submit(client))
        b = _accepted_id(_submit(client))
        gate.wait_started(2)
        rows, rejected, accepted = _rows(), _rejected("assess"), _accepted("assess")

        # The pre-check: no job has finished since this queue started, so the
        # default 10 s.
        _assert_refused(_submit(client), open_jobs=2, limit=2, retry_after=10)
        assert _rejected("assess") - rejected == 1
        assert _accepted("assess") == accepted  # never counted as a submit
        assert _rows() == rows
        assert _sample("classifier_jobs_open") == 2

        # The authoritative check alone (pre-check waved through): the body is
        # read and validated, and admit refuses before any row exists.
        check_room = queue_module.queue.check_room
        monkeypatch.setattr(queue_module.queue, "check_room", _waved_through)
        _assert_refused(_submit(client), open_jobs=2, limit=2, retry_after=10)
        assert _rejected("assess") - rejected == 2
        assert _rows() == rows
        monkeypatch.setattr(queue_module.queue, "check_room", check_room)

        # One finishes: its slot is free, and its run time now feeds the estimate.
        _finish(client, gate, a)
        _until(lambda: len(queue_module.queue.durations) == 1)
        c = _accepted_id(_submit(client))
        _assert_refused(_submit(client), open_jobs=2, limit=2)
        for job_id in (b, c):
            _finish(client, gate, job_id)
        assert _open() == 0


def test_a_limit_below_max_concurrent_narrows_how_many_jobs_run(
    need_postgres, monkeypatch, caplog
):
    caplog.set_level(logging.INFO, logger="classifier")
    workers = queue_module.queue.pool.concurrency
    assert workers == config.MAX_CONCURRENT >= 2
    with _running_app(monkeypatch, limit=1) as (client, gate):
        assert any(
            r.levelno == logging.WARNING and "workers stay idle" in r.getMessage()
            and f"CLASSIFIER_QUEUE_LIMIT=1 is below CLASSIFIER_MAX_CONCURRENT={workers}"
            in r.getMessage()
            for r in caplog.records
        ), [r.getMessage() for r in caplog.records]
        assert _sample("classifier_job_queue_limit") == 1
        for burst in range(3):
            # Six at once, from six threads: exactly one gets the slot.
            with ThreadPoolExecutor(max_workers=6) as pool:
                responses = list(pool.map(lambda _: _submit(client), range(6)))
            codes = sorted(r.status_code for r in responses)
            assert codes == [202] + [429] * 5, [r.text for r in responses]
            (job_id,) = [r.json()["job_id"] for r in responses if r.status_code == 202]
            gate.wait_started(burst + 1)
            time.sleep(0.3)  # idle workers poll; give them every chance to take more
            assert queue_module.queue.pool.in_flight == 1
            _finish(client, gate, job_id)
        assert gate.peak_in_flight == 1


def _png() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (400, 300), (180, 180, 180)).save(buf, format="PNG")
    return buf.getvalue()


def _reference_body() -> dict:
    name = "has a grey card"
    return {
        "document": {"type": "base64", "data": base64.b64encode(_png()).decode("ascii"),
                     "filename": "card.png"},
        "criteria": [{"name": name, "type": "llm", "options": {"hint": "presence"}}],
        "breakdown": {name: {"score": 10, "reason": "a grey card"}},
        "regions": {name: []},
        "description": "A grey test card.",
    }


def test_a_reference_at_the_cap_is_a_429_and_leaves_nothing(need_postgres, monkeypatch):
    with _running_app(monkeypatch, limit=1) as (client, gate):
        held = _accepted_id(_submit(client))
        gate.wait_started(1)
        references, rows = asyncio.run(reference_registry.count()), _rows()
        rejected = _rejected("reference")

        _assert_refused(client.post("/references", json=_reference_body()), open_jobs=1, limit=1)
        # admit alone (the pre-check waved through): the request is read and
        # resolved, and the creation job's registration — BEFORE the
        # reference row — refuses.
        monkeypatch.setattr(queue_module.queue, "check_room", _waved_through)
        _assert_refused(client.post("/references", json=_reference_body()), open_jobs=1, limit=1)

        assert asyncio.run(reference_registry.count()) == references
        assert _rows() == rows
        assert _rejected("reference") - rejected == 2
        _finish(client, gate, held)


def test_a_full_queue_answers_before_a_url_document_is_fetched(need_postgres, monkeypatch):
    import api.assess as assess_api

    fetched: list[str] = []
    real = assess_api.load_input_bytes

    async def recording(data, kind):
        if kind == "url":
            fetched.append(data)
            raise AssertionError("a URL was fetched for a submit the queue refuses")
        return await real(data, kind)

    monkeypatch.setattr(assess_api, "load_input_bytes", recording)
    with _running_app(monkeypatch, limit=1) as (client, gate):
        held = _accepted_id(_submit(client))
        gate.wait_started(1)
        r = client.post("/assess", json={
            "documents": [{"type": "url", "data": "https://example.com/notice.pdf"}],
            "criteria": [{"name": "Notice to Owner", "type": "text"}],
        })
        _assert_refused(r, open_jobs=1, limit=1)
        assert fetched == []
        _finish(client, gate, held)


def test_rows_open_over_the_limit_at_start_refuse_until_they_drain(
    need_postgres, monkeypatch, caplog
):
    """As after a restart: three jobs the last container left queued come
    back (recover), run, and hold three slots of a limit of two."""
    caplog.set_level(logging.INFO, logger="classifier")
    leftovers: list[str] = []

    def three_queued_jobs() -> None:
        for _ in range(3):
            job_id = asyncio.run(queue_module.jobs_registry.register(
                ClassifierMetadata(type="assess", request_id="restart"), initial_phase="pending"
            ))
            asyncio.run(queue_module.queue.payloads.write(job_id, {"x": 1}))
            leftovers.append(job_id)

    with _running_app(monkeypatch, limit=2, rows_before_start=three_queued_jobs) as (client, gate):
        assert any(
            "3 jobs are already open" in r.getMessage() and "answer 429 until it drains"
            in r.getMessage() for r in caplog.records
        ), [r.getMessage() for r in caplog.records]
        gate.wait_started(3)
        assert _open() == 3
        # A known history, so the excess shows: median 20 s, 3 open on a
        # limit of 2 → two slots must free, two jobs finish in parallel.
        monkeypatch.setattr(queue_module.queue, "durations", deque([20.0], maxlen=50))
        _assert_refused(_submit(client), open_jobs=3, limit=2, retry_after=20)

        _finish(client, gate, leftovers[0])
        _assert_refused(_submit(client), open_jobs=2, limit=2)  # at the limit: still full
        _finish(client, gate, leftovers[1])
        new = _accepted_id(_submit(client))
        for job_id in (leftovers[2], new):
            _finish(client, gate, job_id)


def test_a_limit_of_zero_admits_everything(need_postgres, monkeypatch):
    with _running_app(monkeypatch, limit=0) as (client, gate):
        responses = [_submit(client) for _ in range(10)]
        assert [r.status_code for r in responses] == [202] * 10
        assert _open() == 10
        body = client.get("/document-kinds").json()
        # The container's setting (config; 0 → null), not this test's.
        assert body["limits"]["queue_limit"] == (config.QUEUE_LIMIT or None)
        assert "429" in body["limits"]["queue"]
