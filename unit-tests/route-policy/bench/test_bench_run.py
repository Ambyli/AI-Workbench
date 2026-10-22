"""bench.run / bench.criteria: the classification loop, with a fake client.

No network: a ``FakeClient`` stands in for route-policy's ``ClassifierClient``
(same ``assess`` / ``aclose`` surface, returning real ``AssessOutcome``s).
Pins what is sent (the evaluator's own rendering, the criteria texts in
order), the result line's exact shape and its route (``verdict.choose``),
resume / ``--retry-failed``, the concurrency cap, and criteria-set loading.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from bench_fixtures import CRITERIA, RULESETS_DIR, Branch, write_ruleset

from bench import criteria as criteria_mod
from bench import results as results_mod
from bench import run as run_mod
from bench.jsonl import read_jsonl, write_jsonl

from common.conversation import render_transcript
from route_policy.analysis.classifier_client import AssessOutcome, CriterionScore

RESULT_KEYS = {"point_id", "model", "criteria_set", "criteria_fingerprint", "route", "confidence", "scores",
               "reason", "status", "error", "latency_ms", "queue_ms", "model_seconds", "calls",
               "prompt_tokens", "completion_tokens", "input_chars", "at"}


def test_the_result_line_schema_is_one_list():
    """results.FIELDS (what the report reads) is exactly what run writes, in order."""
    assert set(results_mod.FIELDS) == RESULT_KEYS and len(results_mod.FIELDS) == len(RESULT_KEYS)


def outcome(scores, status="ok", **kw):
    """An AssessOutcome from {criterion text: score}."""
    return AssessOutcome(
        status=status,
        scores={text: CriterionScore(score, f"because {score}") for text, score in scores.items()},
        job_id=kw.get("job_id", "job-1"), calls=kw.get("calls", len(scores)), prompt_tokens=kw.get("prompt_tokens", 900),
        completion_tokens=kw.get("completion_tokens", 60), model_seconds=kw.get("model_seconds", 2.5),
        queue_ms=kw.get("queue_ms", 120.0), latency_ms=kw.get("latency_ms", 3100.0), error=kw.get("error"))


class FakeClient:
    """Answers every assess() with ``answer(model, text, criteria)``."""

    def __init__(self, answer=None, delay=0.0):
        self.answer = answer or (lambda model, text, criteria: outcome({c: 2 for c in criteria}))
        self.delay = delay
        self.calls = []
        self.in_flight = 0
        self.peak = 0
        self.closed = False

    async def assess(self, *, model, text, criteria):
        self.calls.append((model, text, list(criteria)))
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
            return self.answer(model, text, list(criteria))
        finally:
            self.in_flight -= 1

    async def aclose(self):
        self.closed = True


class ListSink:
    def __init__(self):
        self.lines = []

    def write(self, record):
        self.lines.append(record)


@pytest.fixture
def cset(tmp_path):
    return criteria_mod.load_criteria_set(str(write_ruleset(tmp_path)), "topic-llm")


def make_points(n=2):
    out = []
    for i in range(n):
        b = Branch()
        b.user(f"first question {i}")
        b.assistant("answer")
        b.user(f"write a python script {i}")
        msgs = [{"role": m["role"], "content": m["content"]} for m in b.messages]
        out.append({"point_id": f"chat-{i}:m3", "k": 2, "messages": msgs, "trimmed_exchanges": 0})
    return out


# -- criteria sets ------------------------------------------------------------------------


def test_criteria_set_from_a_ruleset_file(cset):
    assert cset.id == "bench-test@1/topic-llm"
    assert cset.routes == list(CRITERIA)
    assert cset.texts == list(CRITERIA.values())
    assert (cset.otherwise, cset.min_score, cset.exchanges, cset.max_chars) == ("local-general", 7, 4, 6000)
    assert len(cset.fingerprint) == 64


def test_fingerprint_ignores_the_model_and_follows_everything_else(tmp_path):
    base = criteria_mod.load_criteria_set(str(write_ruleset(tmp_path, "a.yaml")), "topic-llm")
    other_version = criteria_mod.load_criteria_set(str(write_ruleset(tmp_path, "b.yaml", version=2)), "topic-llm")
    assert other_version.fingerprint == base.fingerprint and other_version.id == "bench-test@2/topic-llm"
    for kw in (dict(min_score=8), dict(otherwise=None), dict(exchanges=3), dict(max_chars=4000)):
        changed = criteria_mod.load_criteria_set(str(write_ruleset(tmp_path, "c.yaml", **kw)), "topic-llm")
        assert changed.fingerprint != base.fingerprint, kw
    doc = base.config.model_copy(update={"model": "muse-glimmer"})
    assert criteria_mod.fingerprint(doc, base.order) == base.fingerprint


def test_tie_order_is_the_rulesets_routes_not_the_mapping(tmp_path):
    """As in production (the stored jsonb document keeps no key order), the
    criteria mapping's order changes nothing: tie order = the ruleset's routes."""
    base = criteria_mod.load_criteria_set(str(write_ruleset(tmp_path, "a.yaml")), "topic-llm")
    flipped = criteria_mod.load_criteria_set(
        str(write_ruleset(tmp_path, "d.yaml", criteria=dict(reversed(list(CRITERIA.items()))))), "topic-llm")
    assert flipped.routes == base.routes == list(CRITERIA)
    assert flipped.texts == base.texts and flipped.fingerprint == base.fingerprint


def test_criteria_set_errors(tmp_path):
    path = str(write_ruleset(tmp_path))
    with pytest.raises(ValueError, match="no evaluator"):
        criteria_mod.load_criteria_set(path, "missing")
    with pytest.raises(ValueError, match="not an llm_classifier"):
        criteria_mod.load_criteria_set(str(RULESETS_DIR / "default.yaml"), "privacy")
    with pytest.raises(FileNotFoundError):
        criteria_mod.load_criteria_set(str(tmp_path / "nope.yaml"), "topic-llm")


def test_the_draft_ruleset_loads_when_present():
    path = RULESETS_DIR / "default-llm.yaml"
    if not path.exists():
        pytest.skip("rulesets/default-llm.yaml not written yet")
    loaded = criteria_mod.load_criteria_set("rulesets/default-llm.yaml", "topic-llm")
    assert loaded.id.startswith("default-llm@") and loaded.routes


def test_sets_from_flags(tmp_path):
    path = str(write_ruleset(tmp_path))
    assert [s.id for s in run_mod.criteria_sets([path], ["topic-llm"])] == ["bench-test@1/topic-llm"]
    # The same set twice is one set; mismatched pairs are refused.
    assert len(run_mod.criteria_sets([path, path], ["topic-llm", "topic-llm"])) == 1
    with pytest.raises(ValueError):
        run_mod.criteria_sets([path, path], ["topic-llm"])


# -- one classification --------------------------------------------------------------------


def test_classify_sends_the_evaluators_rendering_and_maps_scores_to_routes(cset):
    point = make_points(1)[0]
    texts = list(CRITERIA.values())
    client = FakeClient(lambda m, t, c: outcome({texts[0]: 9, texts[1]: 9, texts[2]: 3}))
    line = asyncio.run(run_mod.classify(client, run_mod.WorkItem(point, "qwen3.5-4b", cset)))
    (model, text, sent) = client.calls[0]
    assert model == "qwen3.5-4b" and sent == texts
    assert text == render_transcript(point["messages"], exchanges=4, max_chars=6000, upto_user=2)
    assert tuple(line) == results_mod.FIELDS
    assert line["route"] == "local-code"          # a tie at 9 goes to the first listed
    assert line["confidence"] == 0.9
    assert line["scores"] == {"local-code": 9, "local-reasoning": 9, "local-private": 3}
    assert line["reason"] == "because 9"
    assert (line["status"], line["calls"], line["input_chars"]) == ("ok", 3, len(text))
    assert line["criteria_set"] == cset.id and line["criteria_fingerprint"] == cset.fingerprint
    json.dumps(line)  # serialisable


def test_nothing_reaches_min_score_goes_to_otherwise(cset):
    point = make_points(1)[0]
    client = FakeClient(lambda m, t, c: outcome({c[0]: 4, c[1]: 1, c[2]: 1}))
    line = asyncio.run(run_mod.classify(client, run_mod.WorkItem(point, "m", cset)))
    assert line["route"] == "local-general"
    assert line["confidence"] == pytest.approx(round(6 / 9, 4))
    assert line["reason"] is None    # no criterion won


def test_incomplete_scores_still_choose_and_failures_do_not(cset):
    point = make_points(1)[0]
    texts = list(CRITERIA.values())
    partial = FakeClient(lambda m, t, c: outcome({texts[2]: 8}, status="incomplete"))
    line = asyncio.run(run_mod.classify(partial, run_mod.WorkItem(point, "m", cset)))
    assert line["route"] == "local-private" and line["scores"]["local-code"] is None
    failed = FakeClient(lambda m, t, c: AssessOutcome("timeout", {}, None, None, None, None, None, None, 180000.0,
                                                      "no result within 180 s"))
    line = asyncio.run(run_mod.classify(failed, run_mod.WorkItem(point, "m", cset)))
    assert (line["route"], line["confidence"], line["status"]) == (None, None, "timeout")
    assert line["scores"] == {"local-code": None, "local-reasoning": None, "local-private": None}
    assert line["error"] == "no result within 180 s"


def test_a_point_without_a_transcript_is_skipped_not_sent(cset):
    point = {**make_points(1)[0], "k": 9}
    client = FakeClient()
    line = asyncio.run(run_mod.classify(client, run_mod.WorkItem(point, "m", cset)))
    assert client.calls == []
    assert tuple(line) == results_mod.FIELDS and line["status"] == "skipped" and "k=9" in line["error"]


# -- the loop -------------------------------------------------------------------------------


def test_plan_is_point_major_and_skips_finished_keys(cset):
    pts = make_points(2)
    done = {(pts[0]["point_id"], "b", cset.fingerprint)}
    work = run_mod.plan(pts, ["a", "b"], [cset], done)
    assert [(w.point["point_id"], w.model) for w in work] == [
        (pts[0]["point_id"], "a"), (pts[1]["point_id"], "a"), (pts[1]["point_id"], "b")]


def test_finished_keys_and_retry_failed():
    rows = [
        {"point_id": "p1", "model": "a", "criteria_fingerprint": "f", "status": "ok"},
        {"point_id": "p2", "model": "a", "criteria_fingerprint": "f", "status": "timeout"},
        {"point_id": "p3", "model": "a", "criteria_fingerprint": "f", "status": "timeout"},
        {"point_id": "p3", "model": "a", "criteria_fingerprint": "f", "status": "incomplete"},  # the last wins
    ]
    assert run_mod.finished_keys(rows) == {("p1", "a", "f"), ("p2", "a", "f"), ("p3", "a", "f")}
    assert run_mod.finished_keys(rows, retry_failed=True) == {("p1", "a", "f"), ("p3", "a", "f")}


@pytest.mark.parametrize("concurrency, peak", [(1, 1), (2, 2)])
def test_concurrency_is_respected(cset, concurrency, peak):
    pts = make_points(3)
    client = FakeClient(delay=0.01)
    sink = ListSink()
    work = run_mod.plan(pts, ["a", "b"], [cset], set())
    statuses = asyncio.run(run_mod.run_bench(work, client, sink, concurrency=concurrency))
    assert client.peak == peak
    assert len(sink.lines) == 6 and statuses == {"ok": 6}


def test_more_than_two_in_flight_is_refused(cset):
    with pytest.raises(ValueError):
        asyncio.run(run_mod.run_bench([], FakeClient(), ListSink(), concurrency=3))
    with pytest.raises(SystemExit):
        run_mod.main(["--points", "p", "--results", "r", "--concurrency", "3"])


def test_command_appends_and_resumes(tmp_path, cset):
    ruleset = str(write_ruleset(tmp_path))
    pts = make_points(2)
    write_jsonl(tmp_path / "points.jsonl", pts)
    results = tmp_path / "results.jsonl"
    texts = list(CRITERIA.values())
    flaky = {"fail": True}

    def answer(model, text, criteria):
        if model == "b" and "0" in text.splitlines()[-1] and flaky["fail"]:
            return AssessOutcome("unavailable", {}, None, None, None, None, None, None, 5.0, "connect error")
        return outcome({texts[0]: 8, texts[1]: 2, texts[2]: 1})

    clients = []

    def factory(args):
        assert args.classifier_url == "http://classifier:8000" and args.timeout_s == 180.0
        clients.append(FakeClient(answer))
        return clients[-1]

    argv = ["--points", str(tmp_path / "points.jsonl"), "--results", str(results), "--models", "a,b",
            "--ruleset", ruleset, "--evaluator", "topic-llm", "--progress-every", "1"]
    assert run_mod.main(argv, client_factory=factory) == 0
    lines, bad = read_jsonl(results)
    assert bad == 0 and len(lines) == 4 and all(set(l) == RESULT_KEYS for l in lines)
    assert sorted(l["status"] for l in lines) == ["ok", "ok", "ok", "unavailable"]
    assert clients[0].closed

    # Same command: nothing left to do, no client is even made.
    assert run_mod.main(argv, client_factory=factory) == 0
    assert len(clients) == 1

    # A killed run's half line: the next run starts a fresh line and redoes it.
    with open(results, "a", encoding="utf-8", newline="\n") as fh:
        fh.write('{"point_id": "chat-1:m3", "mod')
    flaky["fail"] = False
    assert run_mod.main(argv + ["--retry-failed"], client_factory=factory) == 0
    lines, bad = read_jsonl(results, skip_bad=True)
    assert bad == 1
    assert len(lines) == 5 and lines[-1]["status"] == "ok" and lines[-1]["model"] == "b"
    assert len(clients[-1].calls) == 1


def test_only_points_and_limit(tmp_path):
    ruleset = str(write_ruleset(tmp_path))
    pts = make_points(3)
    write_jsonl(tmp_path / "points.jsonl", pts)
    write_jsonl(tmp_path / "labels.jsonl", [{"point_id": pts[2]["point_id"], "label": "local-code"}])
    made = []

    def factory(args):
        made.append(FakeClient())
        return made[-1]

    base = ["--points", str(tmp_path / "points.jsonl"), "--models", "a", "--ruleset", ruleset,
            "--evaluator", "topic-llm"]
    run_mod.main(base + ["--results", str(tmp_path / "r1.jsonl"), "--only-points", str(tmp_path / "labels.jsonl")],
                 client_factory=factory)
    assert [l["point_id"] for l in read_jsonl(tmp_path / "r1.jsonl")[0]] == [pts[2]["point_id"]]
    run_mod.main(base + ["--results", str(tmp_path / "r2.jsonl"), "--limit", "2"], client_factory=factory)
    assert len(read_jsonl(tmp_path / "r2.jsonl")[0]) == 2


def test_with_the_real_client_against_a_mock_classifier(tmp_path):
    """run.py through route-policy's own ClassifierClient (no network: an
    httpx MockTransport plays the classifier, a fake resolver its DNS) --
    what is submitted, that every job is deleted, and the locality refusal."""
    import httpx

    from route_policy.analysis.classifier_client import ClassifierClient

    ruleset = str(write_ruleset(tmp_path))
    pts = make_points(2)
    write_jsonl(tmp_path / "points.jsonl", pts)
    submitted, deleted, scores = [], [], [8, 3, 1]

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/models":
            return httpx.Response(200, json={"models": [
                {"id": "qwen3.5-4b", "authenticated": False, "available": True, "endpoint": {"host": "qwen3.5-4b"}},
                {"id": "hosted", "authenticated": True, "available": True, "endpoint": {"host": "api.example.com"}},
            ]})
        if request.method == "POST" and path == "/assess":
            submitted.append(json.loads(request.content))
            return httpx.Response(202, json={"job_id": f"job-{len(submitted)}"})
        if request.method == "GET" and path.startswith("/jobs/"):
            body = submitted[int(path.rsplit("-", 1)[1]) - 1]
            per = {c["name"]: {"status": "ok", "score": s, "reason": f"r{s}"}
                   for c, s in zip(body["criteria"], scores)}
            return httpx.Response(200, json={"phase": "completed", "result": {
                "assessment": {"per_criterion_scores": per, "complete": True},
                "usage": {"calls": 3, "prompt_tokens": 1200, "completion_tokens": 45, "seconds": 2.5}}})
        if request.method == "DELETE" and path.startswith("/jobs/"):
            deleted.append(path.rsplit("/", 1)[1])
            return httpx.Response(204)
        return httpx.Response(404)

    def factory(args):
        return ClassifierClient(args.classifier_url, timeout_s=args.timeout_s, poll_s=0.01,
                                transport=httpx.MockTransport(handler), resolver=lambda host: ["172.18.0.9"])

    argv = ["--points", str(tmp_path / "points.jsonl"), "--results", str(tmp_path / "results.jsonl"),
            "--models", "qwen3.5-4b,hosted", "--ruleset", ruleset, "--evaluator", "topic-llm"]
    assert run_mod.main(argv, client_factory=factory) == 0
    lines, _ = read_jsonl(tmp_path / "results.jsonl")
    by_model = {}
    for line in lines:
        by_model.setdefault(line["model"], []).append(line)
    local = by_model["qwen3.5-4b"]
    assert [(l["status"], l["route"], l["confidence"]) for l in local] == [("ok", "local-code", 0.8)] * 2
    assert local[0]["scores"] == {"local-code": 8, "local-reasoning": 3, "local-private": 1}
    assert (local[0]["calls"], local[0]["prompt_tokens"], local[0]["model_seconds"]) == (3, 1200, 2.5)
    assert [l["status"] for l in by_model["hosted"]] == ["not_local", "not_local"]
    # Only the local model's classifications were submitted, and every job was deleted.
    assert len(submitted) == 2 and deleted == ["job-1", "job-2"]
    body = submitted[0]
    assert body["model"] == "qwen3.5-4b"
    assert body["documents"] == [{"type": "text", "data": render_transcript(
        pts[0]["messages"], exchanges=4, max_chars=6000, upto_user=2)}]
    assert [c["name"] for c in body["criteria"]] == list(CRITERIA.values())


def test_trimmed_points_are_reported(cset):
    pts = [{"point_id": "a", "k": 2, "trimmed_exchanges": 3}, {"point_id": "b", "k": 5, "trimmed_exchanges": 3},
           {"point_id": "c", "k": 1, "trimmed_exchanges": 0}]
    assert run_mod.trimmed_too_far(pts, cset) == 1
