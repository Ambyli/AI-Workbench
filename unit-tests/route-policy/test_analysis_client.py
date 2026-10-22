"""The /assess cycle (route_policy/analysis/classifier_client.py) against a
fake document classifier (rp_helpers.FakeClassifier, an httpx.MockTransport).

* every status: ok, incomplete, failed (job failed, nothing scored),
  refused (4xx, unknown / unavailable model), unavailable (down, 5xx),
  timeout, not_local;
* the locality guard: an authenticated entry, a host resolving to a public
  address (or to a mix), and a host that does not resolve are refused
  BEFORE anything is submitted; private / loopback / IPv6-ULA hosts pass;
  /models and each host's verdict are cached;
* the DELETE is always sent for a job that was created -- after a cancel on
  a timeout, and when the task itself is cancelled;
* the submitted body is exactly one text document and one presence llm
  criterion per question; no error ever carries the submitted text.
"""

from __future__ import annotations

import asyncio
import socket

import pytest

from rp_helpers import FakeClassifier, fake_resolver, model_entry

TEXT = "User: please fix the failing test in billing.py for Jane Example at 12 Main Street"
CODE = "The latest user request in this conversation is about code."
MATH = "The latest user request needs careful multi-step reasoning."


def assess(fake: FakeClassifier, model: str = "qwen3.5-4b", criteria=(CODE, MATH), text: str = TEXT, **kw):
    async def go():
        client = fake.client(**kw)
        try:
            return await client.assess(model=model, text=text, criteria=list(criteria))
        finally:
            await client.aclose()

    return asyncio.run(go())


def test_ok_reads_scores_reasons_and_usage_then_deletes_the_job():
    fake = FakeClassifier(score={CODE: 9, MATH: 3}, waiting_polls=2, running_polls=1)
    out = assess(fake)
    assert out.status == "ok" and out.error is None and out.job_id == "job0001"
    assert {k: v.score for k, v in out.scores.items()} == {CODE: 9, MATH: 3}
    assert all(len(v.reason) == 200 and v.reason.endswith("…") for v in out.scores.values())
    assert (out.calls, out.prompt_tokens, out.completion_tokens, out.model_seconds) == (2, 1400, 80, 3.0)
    assert out.queue_ms is not None and out.queue_ms > 0 and out.latency_ms >= out.queue_ms
    # One text document, one presence llm criterion per question, the model.
    (body,) = fake.submitted
    assert body == {"documents": [{"type": "text", "data": TEXT}],
                    "criteria": [{"name": CODE, "type": "llm", "options": {"hint": "presence", "ocr": "never"}},
                                 {"name": MATH, "type": "llm", "options": {"hint": "presence", "ocr": "never"}}],
                    "model": "qwen3.5-4b"}
    assert fake.deleted == ["job0001"] and fake.cancelled == [] and fake.jobs == {}
    assert ("GET", "/models") in fake.requests


def test_incomplete_keeps_the_partial_scores():
    fake = FakeClassifier(score={CODE: 8, MATH: None})
    out = assess(fake)
    assert out.status == "incomplete"
    assert out.scores[CODE].score == 8 and out.scores[MATH].score is None and out.scores[MATH].reason is None
    assert "1 of 2 criteria not scored: model HTTP 500" == out.error
    assert fake.deleted == ["job0001"]


def test_nothing_scored_is_failed_and_a_failed_job_too():
    fake = FakeClassifier(default_score=None)
    out = assess(fake)
    assert out.status == "failed" and out.scores == {} and "no criterion was scored" in out.error
    assert fake.deleted == ["job0001"]
    fake = FakeClassifier(final="failed")
    out = assess(fake)
    assert out.status == "failed" and out.error == "the job failed: the payload could not be read"
    assert fake.deleted == ["job0001"] and fake.cancelled == []


def test_refused_on_a_4xx_and_unknown_or_unavailable_models_never_submit():
    fake = FakeClassifier(assess_status=400, assess_detail="criteria.0: options.boxes needs a vision model")
    out = assess(fake)
    assert out.status == "refused" and out.job_id is None
    assert out.error == "POST /assess answered 400: criteria.0: options.boxes needs a vision model"
    assert fake.deleted == []
    fake = FakeClassifier(models=[model_entry("qwen3.5-4b", available=False)])
    out = assess(fake)
    assert out.status == "refused" and "not available" in out.error and fake.submitted == []
    out = assess(FakeClassifier(), model="gpt-9")
    assert out.status == "refused" and "not in the classifier's registry" in out.error


def test_unavailable_when_down_or_5xx():
    fake = FakeClassifier(down=True)
    out = assess(fake)
    assert out.status == "unavailable" and out.job_id is None and "GET /models" in out.error
    fake = FakeClassifier(assess_status=503)
    out = assess(fake)
    assert out.status == "unavailable" and out.error == "POST /assess answered 503"


def test_timeout_cancels_then_deletes():
    fake = FakeClassifier(never_finish=True)
    out = assess(fake, timeout_s=0.3)
    assert out.status == "timeout" and "no result within 0.3s" in out.error and "processing" in out.error
    assert out.scores == {} and out.job_id == "job0001"
    assert fake.cancelled == ["job0001"] and fake.deleted == ["job0001"]
    assert fake.requests.index(("POST", "/jobs/job0001/cancel")) < fake.requests.index(("DELETE", "/jobs/job0001"))


def test_a_cancelled_cycle_still_deletes_its_job():
    fake = FakeClassifier(never_finish=True)

    async def go():
        client = fake.client(timeout_s=30)
        task = asyncio.ensure_future(client.assess(model="qwen3.5-4b", text=TEXT, criteria=[CODE]))
        while not fake.jobs:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await client.aclose()

    asyncio.run(go())
    assert fake.cancelled == ["job0001"] and fake.deleted == ["job0001"]


@pytest.mark.parametrize("entry, why", [
    (model_entry("qwen3.5-4b", authenticated=True), "API key"),
    (model_entry("qwen3.5-4b", "api.example.com"), "resolves to a public address"),
    (model_entry("qwen3.5-4b", "mixed.example.com"), "resolves to a public address"),
    (model_entry("qwen3.5-4b", "nowhere.invalid"), "could not be resolved (gaierror)"),
    ({**model_entry("qwen3.5-4b"), "endpoint": None}, "no endpoint host"),
])
def test_the_locality_guard_never_submits_to_a_non_local_model(entry, why):
    fake = FakeClassifier(models=[entry])
    out = assess(fake)
    assert out.status == "not_local" and why in out.error, out.error
    assert fake.submitted == [] and fake.deleted == [] and out.job_id is None


def test_private_loopback_and_ipv6_ula_hosts_are_local():
    def resolver(host):
        return {"loop": ["127.0.0.1", "::1"], "ula": ["fd12::5"], "rfc": ["192.168.5.240"]}[host]

    for host in ("loop", "ula", "rfc", ):
        fake = FakeClassifier(models=[model_entry("m", host)])
        assert assess(fake, model="m", resolver=resolver).status == "ok", host
    # The real model services, through the default fake resolver.
    for model in ("qwen3.8-solo", "muse-glimmer"):
        assert assess(FakeClassifier(), model=model).status == "ok"


def test_models_and_host_verdicts_are_cached():
    fake = FakeClassifier(score={CODE: 8})
    calls = []

    def resolver(host):
        calls.append(host)
        return fake_resolver(host)

    async def go():
        client = fake.client(resolver=resolver, models_ttl_s=60)
        for _ in range(3):
            assert (await client.assess(model="qwen3.5-4b", text=TEXT, criteria=[CODE])).status == "ok"
        expired = fake.client(resolver=resolver, models_ttl_s=0)
        for _ in range(2):
            await expired.assess(model="qwen3.5-4b", text=TEXT, criteria=[CODE])
        await client.aclose()
        await expired.aclose()

    asyncio.run(go())
    assert fake.requests.count(("GET", "/models")) == 1 + 2
    assert calls == ["qwen3.5-4b"] * 3


def test_a_failed_resolution_is_not_local_and_resolution_errors_are_named():
    def boom(host):
        raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")

    out = assess(FakeClassifier(), resolver=boom)
    assert out.status == "not_local" and "could not be resolved" in out.error


def test_an_unresolved_host_is_cached_briefly_and_a_resolved_one_for_the_ttl():
    # Docker DNS answers only for running containers: a restarted model must
    # be classified again within seconds, not after models_ttl_s.
    fake = FakeClassifier(score={CODE: 8})
    now, up, calls = [100.0], [False], []

    def resolver(host):
        calls.append(host)
        if not up[0]:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return fake_resolver(host)

    async def go():
        client = fake.client(resolver=resolver, clock=lambda: now[0])
        statuses = []
        for step, back in ((0.0, False), (1.0, True), (1.5, True), (30.0, True)):
            now[0] += step
            up[0] = back
            statuses.append((await client.assess(model="qwen3.5-4b", text=TEXT, criteria=[CODE])).status)
        await client.aclose()
        return statuses

    # +1 s: still the cached failure (unresolved_ttl_s = 2); +2.5 s: resolved
    # again; +32.5 s: that verdict is still cached (models_ttl_s = 60).
    assert asyncio.run(go()) == ["not_local", "not_local", "ok", "ok"]
    assert calls == ["qwen3.5-4b"] * 2


def test_no_error_carries_the_submitted_text():
    # A 4xx whose detail echoes the document is withheld, not returned.
    fake = FakeClassifier(assess_status=422, assess_detail=f"bad document: {TEXT[10:80]}")
    out = assess(fake)
    assert out.status == "refused" and TEXT[10:40] not in out.error
    assert out.error == "refused: the classifier's message quoted the submitted text and was withheld"
    for fake in (FakeClassifier(), FakeClassifier(final="failed"), FakeClassifier(never_finish=True),
                 FakeClassifier(down=True), FakeClassifier(score={CODE: None})):
        out = assess(fake, timeout_s=0.2)
        assert out.error is None or (len(out.error) <= 300 and "Jane Example" not in out.error)


def test_a_failed_delete_is_counted_not_raised():
    from prometheus_client import REGISTRY

    before = REGISTRY.get_sample_value("route_policy_errors_total", {"kind": "classifier_delete"}) or 0.0
    fake = FakeClassifier(delete_status=500)
    assert assess(fake).status == "ok"
    assert REGISTRY.get_sample_value("route_policy_errors_total", {"kind": "classifier_delete"}) == before + 1
