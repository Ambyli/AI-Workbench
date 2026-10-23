"""Measured confidence: llm/confidence.py, the client's extraction, the headline.

No model server: responses are scripted at the bare transport
``llm.client._send`` (so ``_post``'s slot, timer and usage row run for real),
shaped like what the box returned on 2026-10-22 for
``logprobs: true, top_logprobs: 20``:

  * muse-glimmer — ``10`` is ONE token, its reasoning precedes the answer in
    the stream, the score's top list holds numbers and junk;
  * qwen — digits split (``1``, ``0``), reasoning in the stream with a decoy
    ``"score": 3``.

Pinned:

  * ``band_mass`` (unresolved mass is a lower bound), the ``signals`` block,
    ``mixture``, isotonic ``fit_isotonic`` / ``apply``;
  * ``call_model`` extracts the distribution for both tokenisations, attaches
    it, and drops the logprob payload — nothing of it reaches ``llm_calls``;
  * sampling: ``n_param`` sends ``n`` in ONE request holding n slots, reports
    the first parseable choice and drops unparseable ones; ``repeat`` sends
    n plain requests;
  * fallback: no logprobs, coverage under ``min_coverage``, or a test that
    replaces ``call_model`` → source ``stated`` with a note, never an error;
  * the headline: stated in shadow mode; the calibrated number when the entry
    says ``headline: calibrated`` and the signal is there; stated (with a
    note) when it is not;
  * only score calls are measured; ``clamp_answer``'s ``stated_defaulted``.
"""

from __future__ import annotations

import asyncio
import json
import math

import cv2
import numpy as np
import pytest

import analysis
from api.schemas import CriterionInput
from common.logprobs import Distribution, Unresolved, from_samples
from llm import client as llm_client
from llm import confidence as conf_mod
from llm import models as llm_models
from llm import usage as llm_usage
from llm.models import parse_registry
from llm.validate import answer_validator, clamp_answer

# ---------------------------------------------------------------------------
# Probe-shaped responses
# ---------------------------------------------------------------------------


def _e(token: str, top: list[tuple[str, float]] | None = None) -> dict:
    top = top or [(token, 1.0)]
    p = dict(top).get(token, 1.0)
    return {"token": token, "logprob": math.log(p), "bytes": list(token.encode()),
            "top_logprobs": [{"token": t, "logprob": math.log(q)} for t, q in top]}


MUSE_REASONING = ["<|channel|>", "analysis", "<|message|>", "The", " meter", " is",
                  " visible", ".", "<|end|>", "<|start|>", "assistant", "<|channel|>",
                  "final", "<|message|>"]
QWEN_REASONING = ["<think>", "Maybe", ' {"', "score", '":', " ", "3", "}", "?", " No", ".",
                  "</think>", "\n\n"]
TAIL = [' "verdict', '":', ' "PASS', '",', ' "confidence', '":', " 95", ",", ' "reason',
        '":', ' "I', " see", " it", '."}']


def _response(score_entries: list[dict], reasoning: list[str], *, logprobs=True,
              confidence_tail=TAIL) -> dict:
    answer = [_e(t) for t in ('{"', "score", '":', " ")] + score_entries + \
        [_e(t) for t in confidence_tail]
    content = "".join(e["token"] for e in answer)
    choice = {"index": 0, "message": {"role": "assistant", "content": content,
                                      "reasoning_content": "".join(reasoning)},
              "finish_reason": "stop"}
    if logprobs:
        choice["logprobs"] = {"content": [_e(t) for t in reasoning] + answer}
    return {"model": "m", "choices": [choice],
            "usage": {"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140}}


def _sampled(top, sampled=None):
    return sampled if sampled is not None else max(top, key=lambda r: r[1])[0]


def muse(top, next_token=",", sampled=None):
    """One whole-number score token (default: the likeliest), then ``,``."""
    return _response([_e(_sampled(top, sampled), top), _e(next_token)], MUSE_REASONING)


def qwen(first_top, second_top):
    """Two positions (split digits); a ``,`` follows a second digit."""
    second = _sampled(second_top)
    entries = [_e(_sampled(first_top), first_top), _e(second, second_top)]
    if second.isdigit():
        entries.append(_e(","))
    return _response(entries, QWEN_REASONING)


def _plain_choice(score, confidence=80) -> dict:
    return {"message": {"content": json.dumps(
        {"score": score, "verdict": "x", "confidence": confidence, "reason": "r"})},
        "finish_reason": "stop"}


# ---------------------------------------------------------------------------
# A registry with one entry per method
# ---------------------------------------------------------------------------


def _entry(served: str, confidence: dict | None = None, **over) -> dict:
    entry = {
        "description": f"test model {served}",
        "url": f"http://{served}:8000/v1/chat/completions",
        "served_name": served,
        "vision": True,
        "max_images_per_prompt": 3,
        "reasoning": {"style": "none"},
        "max_tokens": {"score": 100, "box": 50, "select": 60, "describe": 70},
        "timeout_s": 5,
        "max_concurrent": 3,
        "text_char_budget": 1000,
    }
    if confidence is not None:
        entry["confidence"] = confidence
    entry.update(over)
    return entry


CAL = {"signal": "verdict_probability", "points": [[0.0, 10.0], [1.0, 90.0]]}

REGISTRY = {
    "default": "lp",
    "models": {
        "lp": _entry("lp", {"method": "logprobs"}),
        "st": _entry("st"),
        "nparam": _entry("nparam", {"method": "sampling", "sampling": {"n": 3}}),
        "repeat": _entry("repeat", {"method": "sampling",
                                    "sampling": {"n": 3, "via": "repeat"}}, max_concurrent=1),
        "cal": _entry("cal", {"method": "logprobs", "headline": "calibrated", "calibration": CAL}),
        "shadowcal": _entry("shadowcal", {"method": "logprobs", "calibration": CAL}),
    },
}


@pytest.fixture
def registry():
    reg = parse_registry(REGISTRY, env={})
    previous = llm_models.use_registry(reg)
    yield reg
    llm_models.use_registry(previous)


class Script:
    """A scripted ``_send``: a response per call from ``make(prompt, spec)``,
    every prompt recorded."""

    def __init__(self, make):
        self.make = make
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, prompt):
        spec = llm_client._target.get()
        self.calls.append((spec.id, prompt))
        await asyncio.sleep(0.005)
        return self.make(prompt, spec)


def _call(monkeypatch, spec, make, label="score/q"):
    script = Script(make if callable(make) else (lambda p, s, r=make: json.loads(json.dumps(r))))
    monkeypatch.setattr(llm_client, "_send", script)
    answer = asyncio.run(llm_client.call_model(
        spec.body([{"role": "user", "content": "q"}], "score"), label=label,
        validator=answer_validator("q"), model=spec))
    return answer, script


# ---------------------------------------------------------------------------
# Pure signal code
# ---------------------------------------------------------------------------


def _dist(probs, unresolved=(), coverage=1.0, sampled=9):
    return Distribution(probs=probs, unresolved=tuple(unresolved), coverage=coverage,
                        sampled=sampled, lo=1, hi=10)


def test_band_mass_counts_straddling_unresolved_mass_nowhere():
    d = _dist({9: 0.6, 5: 0.1, 2: 0.05},
              [Unresolved((1, 10), 0.2), Unresolved((8, 9), 0.05)])
    bands = conf_mod.band_mass(d)
    assert bands == pytest.approx({"PASS": 0.65, "MARGINAL": 0.1, "FAIL": 0.05})
    assert sum(bands.values()) == pytest.approx(0.8)  # a lower bound


def test_signals_block_with_and_without_a_distribution():
    d = _dist({9: 0.75, 4: 0.25}, coverage=0.98)
    block = conf_mod.signals(d, "PASS", 95, False, method="logprobs", source="logprobs")
    assert tuple(block) == conf_mod.BLOCK_KEYS
    assert block["verdict_probability"] == 0.75
    assert block["band"] == {"PASS": 0.75, "MARGINAL": 0.25, "FAIL": 0.0}
    assert list(block["distribution"]) == [str(v) for v in range(1, 11)]
    assert block["distribution"]["9"] == 0.75 and block["distribution"]["1"] == 0.0
    assert block["expected_score"] == 7.75 and block["coverage"] == 0.98
    assert block["entropy_bits"] == pytest.approx(0.8113, abs=1e-4)
    assert block["certainty"] == pytest.approx(1 - 0.8113 / math.log2(10), abs=1e-4)
    assert block["unresolved"] == [] and block["samples"] is None
    assert (block["stated"], block["stated_defaulted"], block["calibrated"]) == (95, False, None)

    empty = conf_mod.signals(None, "PASS", 50, True)
    assert empty["source"] == "stated" and empty["stated_defaulted"] is True
    assert all(empty[k] is None for k in ("verdict_probability", "band", "distribution",
                                          "expected_score", "entropy_bits", "certainty",
                                          "coverage", "unresolved", "samples"))


def test_mixture_averages_the_calls():
    a = _dist({9: 1.0})
    b = _dist({9: 0.5, 3: 0.5}, [Unresolved((1, 10), 0.0)], coverage=0.8)
    m = conf_mod.mixture([a, b])
    assert m.probs == pytest.approx({3: 0.25, 9: 0.75})
    assert m.coverage == pytest.approx(0.9) and m.sampled == 9
    with pytest.raises(ValueError):
        conf_mod.mixture([])


def test_isotonic_fit_pools_violators_and_apply_interpolates():
    xs = [0.1, 0.2, 0.3, 0.4, 0.5, 0.5]
    ys = [0, 100, 0, 100, 100, 0]
    points = conf_mod.fit_isotonic(xs, ys)
    assert points == [[0.1, 0.0], [0.25, 50.0], [0.4667, 66.6667]]
    assert all(p[1] <= q[1] and p[0] <= q[0] for p, q in zip(points, points[1:]))
    assert conf_mod.apply(points, 0.0) == 0.0          # flat before the first
    assert conf_mod.apply(points, 1.0) == 66.6667      # flat after the last
    assert conf_mod.apply(points, 0.175) == pytest.approx(25.0)
    assert conf_mod.apply([[0.5, 70.0]], 0.9) == 70.0
    with pytest.raises(ValueError):
        conf_mod.fit_isotonic([], [])
    with pytest.raises(ValueError):
        conf_mod.apply([], 0.5)


def test_clamp_answer_says_when_the_confidence_was_made_up():
    assert clamp_answer({"score": 8, "confidence": 70})["stated_defaulted"] is False
    for bad in ({"score": 8}, {"score": 8, "confidence": "high"},
                {"score": 8, "confidence": None}, {"score": 8, "confidence": True}):
        out = clamp_answer(bad)
        assert out["confidence"] == 50 and out["stated_defaulted"] is True
    # The client's private keys survive clamping.
    out = clamp_answer({"score": 8, "_score_dist": "d", "_confidence_note": "n"})
    assert (out["_score_dist"], out["_confidence_note"]) == ("d", "n")


# ---------------------------------------------------------------------------
# The client: extraction for both tokenisations
# ---------------------------------------------------------------------------


def test_muse_shaped_logprobs_are_extracted(registry, monkeypatch):
    spec = registry.get("lp")
    top = [("10", 0.97), ("9", 0.02), (" tenth", 0.004), ("<|start|>", 0.003), ("8", 0.003)]
    answer, script = _call(monkeypatch, spec, muse(top))
    ((_, body),) = script.calls
    assert (body["logprobs"], body["top_logprobs"]) == (True, 20)
    assert answer["score"] == 10 and answer["confidence"] == 95
    dist = answer["_score_dist"]
    assert dist.sampled == 10 and dist.coverage == pytest.approx(0.993)
    assert dist.probs[10] == pytest.approx(0.97 / 0.993)
    block, headline = conf_mod.assess(answer, spec.confidence)
    assert block["source"] == "logprobs" and headline == 95
    assert block["verdict_probability"] == pytest.approx(1.0)


def test_qwen_shaped_split_digits_and_reasoning_decoy(registry, monkeypatch):
    spec = registry.get("lp")
    resp = qwen([("1", 0.9), ("9", 0.06), ("8", 0.04)], [("0", 0.8), (",", 0.2)])
    answer, _ = _call(monkeypatch, spec, resp)
    dist = answer["_score_dist"]
    assert answer["score"] == 10 and dist.sampled == 10       # not the decoy's 3
    assert dist.probs == pytest.approx({1: 0.18, 8: 0.04, 9: 0.06, 10: 0.72})
    block, _ = conf_mod.assess(answer, spec.confidence)
    assert block["verdict_probability"] == pytest.approx(0.82)
    assert block["band"]["FAIL"] == pytest.approx(0.18)


def test_the_logprob_payload_is_dropped_and_never_stored(registry, monkeypatch):
    rows = []

    class Capture:
        async def record(self, row):
            rows.append(row)
            return True

    monkeypatch.setattr(llm_usage, "usage_store", Capture())
    seen = []

    def make(prompt, spec):
        resp = muse([("7", 0.9), ("6", 0.1)])
        seen.append(resp)
        return resp

    answer, _ = _call(monkeypatch, registry.get("lp"), make)
    assert answer["_score_dist"].sampled == 7
    assert "logprobs" not in seen[0]["choices"][0]          # dropped after extraction
    (row,) = rows
    assert "logprob" not in json.dumps(row, default=str)    # only usage / finish_reason
    assert row["usage"] == {"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140}


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------


def test_sampling_n_param_is_one_request_holding_n_slots(registry, monkeypatch):
    spec = registry.get("nparam")
    spec.limit.reset_peak()

    def make(prompt, s):
        assert prompt["n"] == 3
        return {"choices": [_plain_choice(8, 90), {"message": {"content": "not json"}},
                            _plain_choice(3, 60)]}

    answer, script = _call(monkeypatch, spec, make)
    assert len(script.calls) == 1 and spec.limit.peak == 3
    assert answer["score"] == 8 and answer["confidence"] == 90   # the first sample's
    assert answer["_score_dist"].probs == pytest.approx({3: 0.5, 8: 0.5})
    block, headline = conf_mod.assess(answer, spec.confidence)
    assert block["source"] == "sampling" and headline == 90
    assert block["samples"] == {"asked": 3, "answered": 2, "agreement": 0.5, "stated_mean": 75.0}
    assert block["verdict_probability"] == 0.5 and block["coverage"] == 1.0


def test_sampling_all_unparseable_is_the_normal_parse_retry(registry, monkeypatch):
    spec = registry.get("nparam")
    attempts = []

    def make(prompt, s):
        attempts.append(1)
        if len(attempts) == 1:
            return {"choices": [{"message": {"content": "nope"}}] * 3}
        return {"choices": [_plain_choice(9)] * 3}

    answer, _ = _call(monkeypatch, spec, make)
    assert len(attempts) == 2 and answer["score"] == 9
    assert answer["_samples"]["verdicts"] == ["PASS"] * 3


def test_sampling_repeat_sends_n_plain_requests(registry, monkeypatch):
    spec = registry.get("repeat")
    scores = iter([4, 9, 9])

    def make(prompt, s):
        return {"choices": [_plain_choice(next(scores))]}

    answer, script = _call(monkeypatch, spec, make)
    assert len(script.calls) == 3 and all("n" not in body for _, body in script.calls)
    assert spec.limit.peak == 1                                 # one slot per request
    assert answer["score"] == 4                                 # the first answer
    block, _ = conf_mod.assess(answer, spec.confidence)
    assert block["samples"]["agreement"] == pytest.approx(0.6667, abs=1e-4)
    assert block["distribution"]["9"] == pytest.approx(0.6667, abs=1e-4)


# ---------------------------------------------------------------------------
# Fallback, and what is not measured
# ---------------------------------------------------------------------------


def test_missing_logprobs_fall_back_to_stated(registry, monkeypatch):
    spec = registry.get("lp")
    answer, _ = _call(monkeypatch, spec, _response([_e("8"), _e(",")], [], logprobs=False))
    assert answer["score"] == 8 and answer["_score_dist"] is None
    block, headline = conf_mod.assess(answer, spec.confidence)
    assert (block["method"], block["source"], headline) == ("logprobs", "stated", 95)
    assert "no entries at choices.0.logprobs.content" in block["note"]
    assert block["verdict_probability"] is None


def test_low_coverage_falls_back_to_stated(registry, monkeypatch):
    spec = registry.get("lp")
    answer, _ = _call(monkeypatch, spec, muse([("8", 0.3), (" eight", 0.7)], sampled="8"))
    assert answer["_score_dist"].coverage == pytest.approx(0.3)
    block, _ = conf_mod.assess(answer, spec.confidence)
    assert block["source"] == "stated" and "coverage 0.300" in block["note"]


def test_a_plain_answer_from_a_replaced_call_model_is_stated(registry):
    plain = {"score": 8, "verdict": "PASS", "confidence": 70, "reason": "r"}
    for mid in ("lp", "st", "nparam"):
        block, headline = conf_mod.assess(plain, registry.get(mid).confidence)
        assert block["source"] == "stated" and headline == 70
        assert (block["note"] is None) == (mid == "st")


def test_only_score_calls_are_measured(registry, monkeypatch):
    spec = registry.get("nparam")
    script = Script(lambda p, s: {"choices": [{"message": {"content": '{"description": "d"}'}}]})
    monkeypatch.setattr(llm_client, "_send", script)
    out = asyncio.run(llm_client.call_model(spec.body([], "describe"),
                                            label="reference/describe", model=spec))
    assert out == {"description": "d"} and len(script.calls) == 1
    assert "n" not in script.calls[0][1]


# ---------------------------------------------------------------------------
# End to end: detail.confidence and the headline
# ---------------------------------------------------------------------------


def _png() -> bytes:
    return cv2.imencode(".png", np.full((200, 300, 3), 150, dtype=np.uint8))[1].tobytes()


def _analyze(model_id, criteria=None):
    doc = analysis.load_document_bytes(_png(), "p.png", None, keep_source=True)
    criteria = criteria or [CriterionInput(name="has a meter", type="llm")]
    return asyncio.run(analysis.analyze_document(doc, criteria, default_model=model_id))


def _detail(result, name="has a meter"):
    return result["assessment"]["per_criterion_scores"][name]


@pytest.mark.parametrize("model_id, calibrated, headline_kind", [
    ("lp", None, "stated"),           # shadow: no calibration at all
    ("shadowcal", 74, "stated"),      # calibrated number computed, NOT the headline
    ("cal", 74, "calibrated"),        # flipped: the calibrated number is the headline
])
def test_the_headline_is_stated_until_calibrated(registry, monkeypatch, model_id,
                                                 calibrated, headline_kind):
    # PASS mass 0.8 → apply([[0, 10], [1, 90]], 0.8) = 74.
    monkeypatch.setattr(llm_client, "_send",
                        Script(lambda p, s: muse([("9", 0.6), ("8", 0.2), ("3", 0.2)])))
    result = _analyze(model_id)
    # The result is stored as JSON (jobs.result): the client's private keys
    # (a Distribution object) must never reach it.
    stored = json.dumps(result)
    assert "_score_dist" not in stored and "_confidence_note" not in stored
    entry = _detail(result)
    block = entry["detail"]["confidence"]
    assert block["stated"] == 95 and block["calibrated"] == calibrated
    assert block["headline"] == headline_kind
    assert entry["confidence"] == (74 if headline_kind == "calibrated" else 95)
    assert entry["score"] == 9 and entry["verdict"] == "PASS"   # never moved
    assert block["verdict_probability"] == pytest.approx(0.8)


def test_calibrated_headline_without_its_signal_is_stated_with_a_note(registry, monkeypatch):
    monkeypatch.setattr(llm_client, "_send", Script(
        lambda p, s: _response([_e("9"), _e(",")], [], logprobs=False)))
    entry = _detail(_analyze("cal"))
    block = entry["detail"]["confidence"]
    assert block["headline"] == "stated" and entry["confidence"] == 95
    assert "headline calibrated on 'verdict_probability'" in block["note"]


def test_score_false_clears_the_confidence_block(registry, monkeypatch):
    monkeypatch.setattr(llm_client, "_send", Script(lambda p, s: muse([("9", 1.0)])))
    c = CriterionInput(name="has a meter", type="llm", score=False)
    entry = _detail(_analyze("lp", [c]))
    assert entry["confidence"] is None and entry["detail"]["confidence"] is None


def test_combined_answer_mixes_member_distributions():
    members = [
        {"score": 9, "verdict": "PASS", "confidence": 90, "stated_defaulted": False,
         "_score_dist": from_samples([9], 1, 10)},
        {"score": 3, "verdict": "FAIL", "confidence": 60, "stated_defaulted": True,
         "_score_dist": from_samples([3], 1, 10)},
        {"score": 6, "verdict": "MARGINAL", "confidence": 70, "stated_defaulted": False},
    ]
    combined = conf_mod.combined_answer(
        {"score": 6, "verdict": "MARGINAL", "confidence": 73, "reason": "r"}, members)
    assert combined["stated_defaulted"] is True
    assert combined["_score_dist"].probs == pytest.approx({3: 0.5, 9: 0.5})
    assert "2 of 3" in combined["_confidence_note"]
