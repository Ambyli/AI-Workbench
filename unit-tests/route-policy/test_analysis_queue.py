"""The analysis queue (route_policy/analysis/queue.py), with a fake classifier
and an in-memory ``analyses`` table:

* dedupe by input_key while queued or running; overflow dropped and counted;
* a key that already has a verdict is skipped without a classifier call;
* the worker writes one row: scores by ROUTE, the route verdict.choose picks,
  the winner's reason (<= 200), usage, provenance -- failures too, with no
  scores;
* a write failure is counted, never raised;
* shutdown drops what waits, gives the running ones the drain time, cancels
  the rest -- whose classifier jobs are still deleted -- and closes the client.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from prometheus_client import REGISTRY

from rp_helpers import FakeClassifier

from route_policy.analysis.model import AnalysisRequest
from route_policy.analysis.queue import AnalysisQueue
from route_policy.db.repositories import analyses as analyses_repo

CRITERIA = {"local-code": "About code.", "local-reasoning": "Needs reasoning.", "local-private": "Personal data."}


def request(key: str = "k1", **kw) -> AnalysisRequest:
    base = dict(input_key=key, fingerprint="f" * 64, model="qwen3.5-4b", criteria=dict(CRITERIA),
                otherwise="local-general", min_score=7, exchange_index=3, input_chars=42, evaluator_id="topic-llm",
                text="User: write me a parser", chat_id="chat-1", message_id="msg-3", ruleset_id="default-llm",
                ruleset_version=1, decision_id=uuid.UUID(int=7))
    base.update(kw)
    return AnalysisRequest(**base)


class FakeDatabase:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    @asynccontextmanager
    async def acquire(self):
        if self.fail:
            raise ConnectionError("route-policy-db is down")
        yield SimpleNamespace()


@pytest.fixture
def table(monkeypatch):
    rows = {}

    async def exists(conn, key):
        return key in rows

    async def insert(conn, record):
        if record.input_key in rows:
            return False
        rows[record.input_key] = record
        return True

    monkeypatch.setattr(analyses_repo, "exists", exists)
    monkeypatch.setattr(analyses_repo, "insert", insert)
    return rows


def _sample(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _queue(fake: FakeClassifier, database=None, **kw) -> AnalysisQueue:
    kw.setdefault("maxsize", 8)
    kw.setdefault("concurrency", 2)
    kw.setdefault("drain_s", 1.0)
    client_kw = kw.pop("client_kw", {})
    return AnalysisQueue(database if database is not None else FakeDatabase(), fake.client(**client_kw), **kw)


async def _settle(queue: AnalysisQueue, timeout_s: float = 5.0) -> None:
    await asyncio.wait_for(queue._queue.join(), timeout_s)


def test_a_worker_classifies_and_writes_one_row(table):
    fake = FakeClassifier(score={"About code.": 9, "Needs reasoning.": 8, "Personal data.": None})

    async def go():
        queue = _queue(fake)
        queue.start()
        assert queue.submit(request())
        await _settle(queue)
        await queue.stop()

    asyncio.run(go())
    row = table["k1"]
    assert (row.route, row.confidence, row.status) == ("local-code", 0.9, "incomplete")
    assert row.scores == {"local-code": 9, "local-reasoning": 8, "local-private": None}
    assert row.reason.startswith("scored 9: ") and len(row.reason) == 200
    assert (row.calls, row.prompt_tokens, row.completion_tokens, row.model_seconds) == (3, 2100, 120, 4.5)
    assert row.classifier_job_id == "job0001" and row.queue_ms is not None and row.latency_ms > 0
    assert (row.ruleset_id, row.ruleset_version, row.evaluator_id, row.chat_id, row.message_id,
            row.decision_id, row.exchange_index, row.input_chars, row.fingerprint, row.model_id) == (
        "default-llm", 1, "topic-llm", "chat-1", "msg-3", uuid.UUID(int=7), 3, 42, "f" * 64, "qwen3.5-4b")
    assert not any("write me a parser" in str(v) for v in vars(row).values())
    assert fake.deleted == ["job0001"]
    # The criteria went in tie order, as the texts; the transcript as the one document.
    (body,) = fake.submitted
    assert [c["name"] for c in body["criteria"]] == list(CRITERIA.values())
    assert body["documents"] == [{"type": "text", "data": "User: write me a parser"}]


def test_otherwise_verdict_and_the_best_reason():
    fake = FakeClassifier(score={"About code.": 5, "Needs reasoning.": 2, "Personal data.": 1})
    outcome = asyncio.run(_assess(fake))
    row = AnalysisQueue.record(request(), outcome)
    assert row.route == "local-general" and row.confidence == pytest.approx(5 / 9)
    assert row.reason.startswith("scored 5: ")   # the best-scoring criterion's, though none won
    no_otherwise = AnalysisQueue.record(request(otherwise=None), outcome)
    assert no_otherwise.route is None and no_otherwise.confidence is None


async def _assess(fake):
    client = fake.client()
    try:
        return await client.assess(model="qwen3.5-4b", text="User: hi", criteria=list(CRITERIA.values()))
    finally:
        await client.aclose()


def test_failures_are_written_with_their_status_and_no_scores(table):
    fake = FakeClassifier(models=[])   # qwen3.5-4b unknown -> refused, nothing submitted

    async def go():
        queue = _queue(fake)
        queue.start()
        queue.submit(request("refused-one"))
        await _settle(queue)
        await queue.stop()

    before = _sample("route_policy_analyses_total", model="qwen3.5-4b", status="refused")
    asyncio.run(go())
    row = table["refused-one"]
    assert (row.status, row.route, row.confidence, row.scores, row.reason) == ("refused", None, None, {}, None)
    assert "not in the classifier's registry" in row.error and row.classifier_job_id is None
    assert _sample("route_policy_analyses_total", model="qwen3.5-4b", status="refused") == before + 1


def test_dedupe_while_queued_and_skip_when_already_stored(table):
    fake = FakeClassifier(score={"About code.": 9})

    async def go():
        queue = _queue(fake, concurrency=1)
        queue.start()
        assert queue.submit(request("a")) is True
        assert queue.submit(request("a")) is False      # queued: not again
        assert queue.submit(request("b")) is True
        await _settle(queue)
        assert len(fake.submitted) == 2
        assert queue.submit(request("a")) is True       # done: accepted, then skipped (already stored)
        await _settle(queue)
        assert len(fake.submitted) == 2
        await queue.stop()

    asyncio.run(go())
    assert set(table) == {"a", "b"}


def test_overflow_is_dropped_and_counted(table):
    fake = FakeClassifier(never_finish=True)

    async def go():
        queue = _queue(fake, maxsize=2, concurrency=1, client_kw={"timeout_s": 30})
        queue.start()
        dropped = _sample("route_policy_analysis_dropped_total", reason="queue_full")
        # No await in between: the worker has not taken anything yet.
        results = [queue.submit(request(k)) for k in ("a", "b", "c")]
        assert results == [True, True, False]
        assert _sample("route_policy_analysis_dropped_total", reason="queue_full") == dropped + 1
        assert _sample("route_policy_analysis_queue_depth") == 2
        await queue.stop(timeout_s=0.1)

    asyncio.run(go())


def test_shutdown_drops_waiting_cancels_running_and_still_deletes(table):
    fake = FakeClassifier(never_finish=True)

    async def go():
        queue = _queue(fake, maxsize=8, concurrency=1, client_kw={"timeout_s": 30})
        queue.start()
        for k in ("a", "b", "c"):
            queue.submit(request(k))
        while not fake.jobs:   # the worker has job0001 running
            await asyncio.sleep(0.01)
        shutdown = _sample("route_policy_analysis_dropped_total", reason="shutdown")
        cancelled = await queue.stop(timeout_s=0.1)
        assert cancelled == 1
        assert _sample("route_policy_analysis_dropped_total", reason="shutdown") == shutdown + 2
        assert queue.submit(request("late")) is False   # stopped
        assert queue.client._http.is_closed

    asyncio.run(go())
    assert fake.cancelled == ["job0001"] and fake.deleted == ["job0001"]
    assert table == {}   # nothing finished, nothing written


def test_shutdown_waits_only_as_long_as_the_running_work(table):
    fake = FakeClassifier(running_polls=10)   # ~0.1 s of polling

    async def go():
        queue = _queue(fake, concurrency=1)
        queue.start()
        queue.submit(request("a"))
        while not fake.jobs:
            await asyncio.sleep(0.005)
        started = time.monotonic()
        assert await queue.stop(timeout_s=10) == 0
        return time.monotonic() - started

    assert asyncio.run(go()) < 3    # not the 10 s drain time
    assert "a" in table and fake.deleted == ["job0001"]


def test_idle_shutdown_is_immediate():
    async def go():
        queue = _queue(FakeClassifier())
        queue.start()
        assert await queue.stop(timeout_s=5) == 0

    asyncio.run(go())


def test_a_write_failure_is_counted_and_the_worker_goes_on(table):
    fake = FakeClassifier(score={"About code.": 9})

    async def go():
        queue = _queue(fake, database=FakeDatabase(fail=True), concurrency=1)
        queue.start()
        errors = _sample("route_policy_errors_total", kind="analysis_write")
        queue.submit(request("a"))
        queue.submit(request("b"))
        await _settle(queue)
        assert _sample("route_policy_errors_total", kind="analysis_write") == errors + 2
        assert len(fake.submitted) == 2   # the failing pre-check did not stop the classification
        await queue.stop()

    asyncio.run(go())
    assert fake.deleted == ["job0001", "job0002"]
