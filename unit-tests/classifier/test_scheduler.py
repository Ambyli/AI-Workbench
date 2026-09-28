"""The scheduler: units, per-item gating, parallelism, isolation, the limits.

Driven through ``analysis.analyze_document`` on synthetic documents, with the
vision model scripted at the TRANSPORT (``llm.client._send``) — which is where
the process-wide model-call limit lives, so the limit is genuinely in play
and can be measured: the fake counts how many calls are in flight at once.

Run with::

    UV_LINK_MODE=copy uv run --no-sync --with pytest --package classifier \\
        python -m pytest unit-tests/classifier/test_scheduler.py -q -p no:cacheprovider
"""

from __future__ import annotations

import asyncio
import json

import httpx
import numpy as np
import pytest

import analysis
from analysis import scheduler
from api.schemas import CriterionInput
from llm import boxes as llm_boxes
from llm import client as llm_client


class SlowModel:
    """A transport stand-in that takes ``delay`` seconds and counts overlap.

    ``score_for`` maps a substring of the prompt to the score it answers
    with, so one fake can PASS one criterion and FAIL another. ``fail_on``
    substrings raise an HTTP error instead.
    """

    def __init__(self, delay=0.05, score_for=None, fail_on=(), default=9):
        self.delay = delay
        self.score_for = dict(score_for or {})
        self.fail_on = tuple(fail_on)
        self.default = default
        self.in_flight = 0
        self.peak = 0
        self.prompts: list[str] = []

    async def __call__(self, prompt):
        text = json.dumps(prompt)
        self.prompts.append(text)
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
            if any(s in text for s in self.fail_on):
                raise httpx.ConnectError("scripted outage")
            score = next((v for k, v in self.score_for.items() if k in text), self.default)
            body = {"score": score, "verdict": "x", "confidence": 70, "reason": "scripted"}
            return {"choices": [{"message": {"content": json.dumps(body)}}]}
        finally:
            self.in_flight -= 1

    def asked(self, name: str) -> bool:
        return any(f"CRITERION: {name}" in p for p in self.prompts)


def _png(width=300, height=200):
    import cv2

    return cv2.imencode(".png", np.full((height, width, 3), 170, dtype=np.uint8))[1].tobytes()


def _run(criteria, *, raw=None, name="page.png"):
    doc = analysis.load_document_bytes(raw or _png(), name, None, keep_source=True)
    return asyncio.run(analysis.analyze_document(doc, criteria))


def _llm(name, **kw):
    return CriterionInput(name=name, type="llm", **kw)


def _entries(result):
    return result["assessment"]["per_criterion_scores"]


@pytest.fixture
def model(monkeypatch):
    fake = SlowModel()
    monkeypatch.setattr(llm_client, "_send", fake)
    return fake


# ── Parallelism and the per-job cap ────────────────────────────────────────


def test_independent_criteria_run_in_parallel(monkeypatch, model):
    monkeypatch.setattr(scheduler, "MAX_UNITS_PER_JOB", 4)
    monkeypatch.setattr(llm_client.LLM_CALLS, "limit", 8)
    _run([_llm(f"c{i}") for i in range(4)])
    assert model.peak == 4


def test_the_per_job_cap_bounds_one_job(monkeypatch, model):
    monkeypatch.setattr(scheduler, "MAX_UNITS_PER_JOB", 2)
    monkeypatch.setattr(llm_client.LLM_CALLS, "limit", 8)
    _run([_llm(f"c{i}") for i in range(6)])
    assert model.peak == 2


def test_the_global_llm_limit_bounds_every_job_together(monkeypatch, model):
    """Two jobs × three criteria, one limit: in flight never exceeds it."""
    monkeypatch.setattr(scheduler, "MAX_UNITS_PER_JOB", 3)
    monkeypatch.setattr(llm_client.LLM_CALLS, "limit", 2)
    llm_client.LLM_CALLS.reset_peak()

    async def two_jobs():
        doc_a = analysis.load_document_bytes(_png(), "a.png", None)
        doc_b = analysis.load_document_bytes(_png(), "b.png", None)
        await asyncio.gather(
            analysis.analyze_document(doc_a, [_llm(f"a{i}") for i in range(3)]),
            analysis.analyze_document(doc_b, [_llm(f"b{i}") for i in range(3)]),
        )

    asyncio.run(two_jobs())
    assert model.peak == 2
    assert llm_client.LLM_CALLS.peak <= 2
    assert len(model.prompts) == 6


def test_the_llm_limit_also_bounds_the_box_loop(monkeypatch, model):
    """Scoring and the loop's ask/verify calls share one limit."""
    monkeypatch.setattr(scheduler, "MAX_UNITS_PER_JOB", 4)
    monkeypatch.setattr(llm_client.LLM_CALLS, "limit", 1)
    monkeypatch.setattr(llm_boxes, "LLM_BBOX_REFINE", False)
    monkeypatch.setattr(llm_boxes, "LLM_BBOX_GRIDLINES", False)
    # Every answer carries both a score and a bbox, so it serves as a scoring
    # answer, an ask answer and a verify answer alike.
    body = {"score": 9, "verdict": "PASS", "confidence": 70, "reason": "r",
            "bbox": [100, 100, 400, 400]}

    async def post(prompt):
        model.in_flight += 1
        model.peak = max(model.peak, model.in_flight)
        try:
            await asyncio.sleep(0.02)
            return {"choices": [{"message": {"content": json.dumps(body)}}]}
        finally:
            model.in_flight -= 1

    monkeypatch.setattr(llm_client, "_send", post)
    result = _run([
        _llm(f"has thing {i}", options={"hint": "presence", "boxes": True}) for i in range(3)
    ])
    assert model.peak == 1
    assert all(e["localization"]["accepted_attempt"] == 1 for e in _entries(result).values())


# ── Waves ──────────────────────────────────────────────────────────────────


def test_a_dependant_waits_and_runs_when_its_dependency_passes(model):
    result = _run([_llm("gate"), _llm("after", depends_on="gate")])
    assert _entries(result)["after"]["status"] == "ok"
    # Strictly after: the dependant's prompt was sent last.
    assert "CRITERION: after" in model.prompts[-1]


def test_a_failed_dependency_skips_without_spending_a_call(monkeypatch):
    fake = SlowModel(score_for={"CRITERION: gate": 2})
    monkeypatch.setattr(llm_client, "_send", fake)
    result = _run([
        _llm("gate"),
        _llm("after", depends_on="gate"),
        _llm("after that", depends_on="after"),  # chains propagate
        _llm("independent"),
    ])
    entries = _entries(result)
    assert entries["gate"]["verdict"] == "FAIL"
    assert entries["after"]["status"] == "skipped"
    assert "dependency 'gate' did not pass (verdict: FAIL)" in entries["after"]["reason"]
    assert entries["after that"]["status"] == "skipped"
    assert "dependency 'after' did not pass (verdict: skipped)" in entries["after that"]["reason"]
    assert not fake.asked("after") and not fake.asked("after that")
    assert fake.asked("independent")
    # Skipped criteria are excluded from the weighting, not scored 1.
    assert set(result["assessment"]["weighted_score_breakdown"]["per_criterion"]) == {
        "gate", "independent"
    }
    assert result["assessment"]["complete"] is True


def test_an_independent_criterion_is_not_held_back_by_a_slow_chain(monkeypatch):
    """Waves are per dependency, not global rounds."""
    order: list[str] = []

    async def slow_gate(c, ctx):
        await asyncio.sleep(0.1 if c.name == "gate" else 0)
        order.append(c.name)
        from analysis.outcome import Outcome

        return Outcome(method="text", score=10, verdict="PASS", confidence=100)

    monkeypatch.setitem(scheduler.EVALUATORS, "text", slow_gate)
    monkeypatch.setattr(scheduler, "MAX_UNITS_PER_JOB", 4)
    _run([
        CriterionInput(name="gate", type="text"),
        CriterionInput(name="after", type="text", depends_on="gate"),
        CriterionInput(name="free", type="text"),
    ])
    assert order == ["free", "gate", "after"]


# ── Error isolation ────────────────────────────────────────────────────────


def test_one_failure_fails_alone(monkeypatch):
    fake = SlowModel(fail_on=("CRITERION: broken",))
    monkeypatch.setattr(llm_client, "_send", fake)
    result = _run([
        _llm("broken"),
        _llm("fine"),
        CriterionInput(name="sharpness", type="cv"),
        _llm("needs broken", depends_on="broken"),
    ])
    entries = _entries(result)
    assert entries["broken"]["status"] == "error"
    assert "scripted outage" in entries["broken"]["error"]
    assert entries["broken"]["score"] is None
    assert entries["fine"]["status"] == "ok"
    assert entries["sharpness"]["status"] == "ok"
    assert entries["needs broken"]["status"] == "skipped"
    assessment = result["assessment"]
    assert assessment["complete"] is False
    assert assessment["overall_verdict"] is None and assessment["overall_score"] is None
    assert assessment["weighted_score_breakdown"]["partial"] is True


def test_an_unparseable_answer_after_retries_is_an_error_not_a_score(monkeypatch):
    async def garbage(prompt):
        return {"choices": [{"message": {"content": "I think it looks nice"}}]}

    monkeypatch.setattr(llm_client, "_send", garbage)
    entry = _entries(_run([_llm("x")]))["x"]
    assert entry["status"] == "error"
    assert "could not be parsed after" in entry["error"]


def test_a_crashing_evaluator_is_contained(monkeypatch):
    async def boom(c, ctx):
        raise ZeroDivisionError("bug")

    monkeypatch.setitem(scheduler.EVALUATORS, "cv", boom)
    result = _run([CriterionInput(name="sharpness", type="cv"),
                   CriterionInput(name="x", type="text")])
    entries = _entries(result)
    assert entries["sharpness"]["status"] == "error" and entries["sharpness"]["error"] == "bug"
    assert entries["x"]["status"] == "ok"


def test_an_unscored_criterion_never_reaches_the_weighting(model):
    result = _run([
        _llm("has a roof", score=False, options={"hint": "presence", "boxes": True}),
        _llm("scored"),
    ])
    breakdown = result["assessment"]["weighted_score_breakdown"]
    assert list(breakdown["per_criterion"]) == ["scored"]
    assert breakdown["excluded"] == {"has a roof": "score: false"}


# ── cv fallback ────────────────────────────────────────────────────────────


def test_a_cv_name_with_no_detector_falls_back_to_the_llm(model):
    entry = _entries(_run([CriterionInput(name="has bicycle", type="cv")]))["has bicycle"]
    assert entry["method"] == "llm" and entry["status"] == "ok"
    assert "answered by the llm fallback" in entry["reason"]
    assert model.asked("has bicycle")


def test_a_cv_name_on_a_text_document_is_skipped(model):
    doc = analysis.load_document_bytes(b"just text here", "t.txt", None)
    result = asyncio.run(analysis.analyze_document(doc, [CriterionInput(name="sharpness", type="cv")]))
    assert _entries(result)["sharpness"]["status"] == "skipped"


def test_the_detector_path_scores_from_boxes(monkeypatch):
    """A `detector` criterion with a stubbed service: boxes in, a score out."""
    from common.vision import Region
    from detector import client as detector_client

    monkeypatch.setattr(detector_client, "DETECTOR_URL", "http://stub")
    seen = {}

    async def detect_page(image, labels, geometry, *, min_score, stats):
        seen["min_score"] = min_score
        stats.calls += 1
        return {labels[0]: [Region(page=0, kind="box", points=[(1, 1), (50, 50)],
                                   label=labels[0], score=0.8, source="detector")]}

    monkeypatch.setattr(detector_client, "detect_page", detect_page)
    result = _run([CriterionInput(name="has bicycle", type="detector",
                                  options={"threshold": 0.4})])
    entry = _entries(result)["has bicycle"]
    assert seen["min_score"] == 0.4
    assert entry["method"] == "detector" and entry["score"] == 10
    assert entry["regions"][0]["source"] == "detector"
    assert result["detector"]["used"] is True


def test_a_detector_outage_fails_only_its_criterion(monkeypatch):
    from detector import client as detector_client

    monkeypatch.setattr(detector_client, "DETECTOR_URL", "http://stub")

    async def detect_page(*args, **kwargs):
        raise detector_client.DetectorUnavailable("the detector at stub could not be reached")

    monkeypatch.setattr(detector_client, "detect_page", detect_page)
    result = _run([CriterionInput(name="has bicycle", type="detector"),
                   CriterionInput(name="sharpness", type="cv")])
    entries = _entries(result)
    assert entries["has bicycle"]["status"] == "error"
    assert "could not be reached" in entries["has bicycle"]["error"]
    assert entries["sharpness"]["status"] == "ok"
    assert result["assessment"]["complete"] is False
