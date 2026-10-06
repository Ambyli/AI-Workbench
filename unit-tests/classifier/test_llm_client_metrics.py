"""Per-call token usage metrics and the per-request log line (llm/client.py).

No vLLM: the model is scripted at the bare transport ``llm.client._send`` —
exactly where every other suite scripts it — so ``_post`` (the slot, the
timer, the usage counters, the log line) runs for real.

  * ``call_kind`` maps every label a call site actually passes to the fixed
    kind set, and anything else to ``other`` — never a criterion name;
  * ``usage_counts`` reads vLLM's ``usage`` and never raises on a missing
    or odd one;
  * a successful request adds its tokens to the four per-kind counters and
    observes ``classifier_llm_call_seconds{kind, outcome="ok"}``; a failed
    one observes ``outcome="error"`` and still raises;
  * one INFO line per request, retries included.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
import pytest
from prometheus_client import REGISTRY

import llm.client as llm_client


def _sample(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _response(content: str = '{"score": 9}', usage=None) -> dict:
    data = {"choices": [{"message": {"content": content}}]}
    if usage is not None:
        data["usage"] = usage
    return data


FULL_USAGE = {
    "prompt_tokens": 1200,
    "completion_tokens": 300,
    "total_tokens": 1500,
    "prompt_tokens_details": {"cached_tokens": 1024, "multimodal_tokens": {"image": 800}},
    "completion_tokens_details": {"reasoning_tokens": 250},
}


# ── call_kind ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("label, kind", [
    ("score/has solar panels", "score"),
    ("score/has solar panels/ref1", "score"),
    ("bbox/has solar panels#2", "ask"),
    ("refine/has solar panels#1", "refine"),
    ("verify/has solar panels#3", "verify"),
    ("reference/select#4", "select"),
    ("reference/describe", "describe"),
    ("", "other"),
    ("has solar panels", "other"),  # a bare criterion name is never a kind
])
def test_call_kind(label, kind):
    assert llm_client.call_kind(label) == kind
    assert kind in llm_client.CALL_KINDS


# ── usage_counts ───────────────────────────────────────────────────────────


def test_usage_counts_reads_every_field():
    assert llm_client.usage_counts({"usage": FULL_USAGE}) == {
        "prompt": 1200, "cached": 1024, "completion": 300, "reasoning": 250,
    }


@pytest.mark.parametrize("data", [
    {},
    {"usage": None},
    {"usage": "lots"},
    {"usage": {"prompt_tokens": "12", "completion_tokens": -1,
               "prompt_tokens_details": None, "completion_tokens_details": []}},
    {"usage": {"prompt_tokens": True, "prompt_tokens_details": {"cached_tokens": None}}},
    None,
    [],
])
def test_usage_counts_never_raises_on_odd_usage(data):
    assert llm_client.usage_counts(data) == {
        "prompt": None, "cached": None, "completion": None, "reasoning": None,
    }


def test_usage_counts_partial():
    counts = llm_client.usage_counts({"usage": {"prompt_tokens": 10, "completion_tokens": 0}})
    assert counts == {"prompt": 10, "cached": None, "completion": 0, "reasoning": None}


# ── _post records by kind ──────────────────────────────────────────────────


def test_a_scoring_call_records_tokens_latency_and_one_log_line(monkeypatch, caplog):
    async def send(prompt):
        return _response(usage=FULL_USAGE)

    monkeypatch.setattr(llm_client, "_send", send)
    before = {
        "prompt": _sample("classifier_llm_prompt_tokens_total", kind="score"),
        "cached": _sample("classifier_llm_cached_prompt_tokens_total", kind="score"),
        "completion": _sample("classifier_llm_completion_tokens_total", kind="score"),
        "reasoning": _sample("classifier_llm_reasoning_tokens_total", kind="score"),
        "count": _sample("classifier_llm_call_seconds_count", kind="score", outcome="ok"),
    }
    with caplog.at_level(logging.INFO, logger=llm_client.logger.name):
        answer = asyncio.run(llm_client.call_vllm({}, label="score/has a house"))
    assert answer == {"score": 9}

    assert _sample("classifier_llm_prompt_tokens_total", kind="score") - before["prompt"] == 1200
    assert _sample("classifier_llm_cached_prompt_tokens_total", kind="score") - before["cached"] == 1024
    assert (_sample("classifier_llm_completion_tokens_total", kind="score")
            - before["completion"]) == 300
    assert (_sample("classifier_llm_reasoning_tokens_total", kind="score")
            - before["reasoning"]) == 250
    assert (_sample("classifier_llm_call_seconds_count", kind="score", outcome="ok")
            - before["count"]) == 1

    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("llm call ")]
    assert len(lines) == 1
    assert "kind=score" in lines[0] and "label=score/has a house" in lines[0]
    assert "prompt=1200 cached=1024 completion=300 reasoning=250" in lines[0]


def test_no_usage_records_no_tokens_and_does_not_raise(monkeypatch):
    async def send(prompt):
        return _response('{"bbox": null}')

    monkeypatch.setattr(llm_client, "_send", send)
    before = _sample("classifier_llm_prompt_tokens_total", kind="ask")
    count = _sample("classifier_llm_call_seconds_count", kind="ask", outcome="ok")
    assert asyncio.run(llm_client.call_vllm_json({}, label="bbox/x#1")) == {"bbox": None}
    assert _sample("classifier_llm_prompt_tokens_total", kind="ask") == before
    assert _sample("classifier_llm_call_seconds_count", kind="ask", outcome="ok") - count == 1


def test_every_retry_is_its_own_request(monkeypatch, caplog):
    answers = iter([_response("not json", {"prompt_tokens": 5, "completion_tokens": 7}),
                    _response('{"score": 3}', {"prompt_tokens": 5, "completion_tokens": 2})])

    async def send(prompt):
        return next(answers)

    monkeypatch.setattr(llm_client, "_send", send)
    before = _sample("classifier_llm_completion_tokens_total", kind="verify")
    count = _sample("classifier_llm_call_seconds_count", kind="verify", outcome="ok")
    with caplog.at_level(logging.INFO, logger=llm_client.logger.name):
        assert asyncio.run(llm_client.call_vllm_json({}, label="verify/x#1")) == {"score": 3}
    # The failed parse still spent tokens; both requests are counted.
    assert _sample("classifier_llm_completion_tokens_total", kind="verify") - before == 9
    assert _sample("classifier_llm_call_seconds_count", kind="verify", outcome="ok") - count == 2
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("llm call ")]
    assert [("attempt=1" in m, "attempt=2" in m) for m in lines] == [(True, False), (False, True)]


def test_an_http_error_is_observed_as_error_and_still_fails_the_call(monkeypatch):
    async def down(prompt):
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(llm_client, "_send", down)
    count = _sample("classifier_llm_call_seconds_count", kind="describe", outcome="error")
    with pytest.raises(llm_client.LLMCallError):
        asyncio.run(llm_client.call_vllm({}, label="reference/describe"))
    assert (_sample("classifier_llm_call_seconds_count", kind="describe", outcome="error")
            - count) == 1
