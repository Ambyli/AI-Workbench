"""``options.mode: "adaptive"`` and the escalation model (ZT-603 phase 2).

No model server: requests are scripted at the bare transport
``llm.client._send`` (the model's limit, the label → kind mapping and the
usage row stay real), with ``test_fast_mode.py``'s helpers. What is pinned:

  * **The escalation model** — ``CLASSIFIER_ESCALATION_MODEL`` read by
    ``parse_registry`` from its env mapping: unset / empty = the default, an
    id that is not an entry a ``RegistryError``, decided on static facts
    (no host is ever resolved at load — a resolver that always fails); an
    unavailable entry loads, ``startup_check`` WARNs beside the
    ``[escalation]`` marker and adaptive is a 400; it IS its entry (one
    ``ConcurrencyLimit``, ``total_concurrency`` unchanged); GET /models
    ``escalation`` and ``fast.escalate_below``; GET /criterion-types ``mode``.
  * **The flow** — a readout under the threshold, an incomplete one and a
    capped one escalate to the escalation model's REASONED call (label
    ``escalate/<name>``, kind ``escalate``, its text budget, vision and
    confidence method, never guided); one at or over the threshold stands;
    ``options.escalate_below`` beats the entry's ``fast.escalate_below``;
    ``detail.escalation`` in every case and under ``score: false``; a failed
    escalation keeps a complete readout; an incomplete readout whose
    escalation failed errors the unit; the metric; the box loop on the
    criterion's own model.
  * **Submit** — every named 400 (``escalate_below`` without adaptive, on
    another type, out of range; adaptive without a fast block; with
    ``options.reference``; with the escalation model unavailable; a
    text-only escalation model on a photo with no OCR), the payload round
    trip, identity.
  * **References** — never guided; POST /references refuses explicit
    adaptive criteria; ``from_job`` strips mode / escalate_below and copies
    an adaptive unit only when it escalated (Postgres).
  * **Defaults unchanged** — a criterion without adaptive carries no new key,
    and the escalation env var changes no body.
  * **The calibration tool** — ``bin/confidence_calibration.py --mode fast`` on
    a scripted model: the temperature fit, the escalation curve, the block.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import importlib.util
import json
import logging
import pathlib
import re
import socket
import textwrap

import httpx
import pytest
import yaml

import analysis
import config
from analysis import llm_eval
from api.schemas import AssessRequest, CriterionInput
from llm import client as llm_client
from llm import confidence as llm_confidence
from llm import models as llm_models
from llm import usage as llm_usage
from llm.models import RegistryError, load_registry, parse_registry
from metrics import escalations_total
from test_fast_mode import (
    CONFIDENT_9,
    LOW_COVERAGE,
    QUICK_FAST,
    REASONED_BODY_PINS,
    SPREAD,
    TXT,
    _analyze,
    _data,
    _entries,
    _entry,
    _hash,
    _png,
    _refusal,
    _user_text,
    _wait,
    is_readout,
    readout_response,
)

ENV = config.ESCALATION_MODEL_ENV
UNSURE = {"9": 0.6, "1": 0.4}          # PASS 0.60 → confidence 60
NINETY = {"9": 0.9, "1": 0.1}          # PASS 0.90 → confidence 90

REGISTRY = _data(
    slow=_entry("http://slow:8000/v1/chat/completions", "slow-model",
                confidence={"method": "logprobs"}),
    quick=_entry("http://quick:8000/v1/chat/completions", "quick-model",
                 reasoning={"style": "chat_template_kwargs", "key": "reasoning_effort",
                            "value": "low"},
                 fast=QUICK_FAST),
    quicktext=_entry("http://quicktext:8000/v1/chat/completions", "quicktext-model",
                     vision=False, max_images_per_prompt=0, fast=QUICK_FAST),
    capped=_entry("http://capped:8000/v1/chat/completions", "capped-model",
                  fast={**QUICK_FAST, "top_logprobs": 11}),
    thresholded=_entry("http://thr:8000/v1/chat/completions", "thr-model",
                       fast={**QUICK_FAST, "escalate_below": {"rating": 95}}),
    # The escalation model: external (https), a small text budget, stated.
    reasoner=_entry("https://reasoner.example.com/v1/chat/completions", "reasoner-model",
                    text_char_budget=20),
    reasonertext=_entry("http://reasonertext:8000/v1/chat/completions", "rt-model",
                        vision=False, max_images_per_prompt=0, text_char_budget=10),
    keyed=_entry("https://claude.example.com/v1/chat/completions", "claude-x",
                 api_key_env="ESCALATION_TEST_KEY"),
)


def _registry(escalation: str | None = "reasoner", data=REGISTRY):
    return parse_registry(data, env={} if escalation is None else {ENV: escalation})


@pytest.fixture
def use():
    """``use(escalation)`` swaps the process registry for the test registry
    with that escalation model; restored afterwards."""
    previous = llm_models.use_registry(None)
    llm_models.use_registry(previous)

    def swap(escalation: str | None = "reasoner", data=REGISTRY):
        reg = _registry(escalation, data)
        llm_models.use_registry(reg)
        return reg

    yield swap
    llm_models.use_registry(previous)


@pytest.fixture
def registry(use):
    return use("reasoner")


def _criterion_of(prompt: dict) -> str:
    match = re.search(r"CRITERION: (.*)", _user_text(prompt))
    return match.group(1).strip() if match else ""


class Scripted:
    """A scripted ``_send``: a readout gets ``tops[criterion]`` (else
    ``top``); a reasoned scoring call ``answers[criterion]`` (else
    ``answer``); a box ask / verify JSON; a model id in ``fail`` raises on
    its JSON calls (its readouts still answer). Records ``(model id,
    prompt)``."""

    def __init__(self, top=CONFIDENT_9, *, tops=None, answer=(2, "FAIL", 70, "escalated: no"),
                 answers=None, fail=()):
        self.top = top
        self.tops = tops or {}
        self.answer = answer
        self.answers = answers or {}
        self.fail = set(fail)
        self.calls: list[tuple[str, dict]] = []

    def readouts(self) -> list[tuple[str, dict]]:
        return [(m, p) for m, p in self.calls if is_readout(p)]

    def reasoned(self) -> list[tuple[str, dict]]:
        return [(m, p) for m, p in self.calls if not is_readout(p)]

    async def __call__(self, prompt):
        spec = llm_client._target.get()
        self.calls.append((spec.id, prompt))
        if is_readout(prompt):
            return readout_response(self.tops.get(_criterion_of(prompt), self.top))
        if spec.id in self.fail:
            raise httpx.ConnectError("connection refused")
        system = prompt["messages"][0]["content"]
        if "You locate features" in system:
            body = {"bbox": [100, 200, 300, 400], "confidence": 90, "reason": "it is there"}
        elif "image crop" in system:
            body = {"score": 9, "reason": "it is there"}
        else:
            answer = self.answers.get(_criterion_of(prompt), self.answer)
            body = dict(zip(("score", "verdict", "confidence", "reason"), answer))
        return {"choices": [{"message": {"content": json.dumps(body)}, "finish_reason": "stop"}]}


def _ac(name="has a house", **options) -> CriterionInput:
    """An adaptive llm criterion on ``quick`` unless the options say otherwise."""
    return CriterionInput.model_validate({"name": name, "type": "llm", "options": {
        "model": "quick", "mode": "adaptive", **options}})


def _escalations(model: str, why: str, outcome: str) -> float:
    return escalations_total.labels(model=model, why=why, outcome=outcome)._value.get()


# ---------------------------------------------------------------------------
# The escalation model: CLASSIFIER_ESCALATION_MODEL
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("env", [{}, {ENV: ""}, {ENV: "   "}])
def test_unset_or_empty_is_the_default(env):
    reg = parse_registry(REGISTRY, env=env)
    assert reg.escalation_id == reg.default_id == "slow" and reg.escalation is reg.default


def test_the_escalation_model_is_named_by_the_env_var():
    reg = _registry("reasoner")
    assert reg.escalation_id == "reasoner" and reg.escalation is reg.get("reasoner")
    assert ENV == "CLASSIFIER_ESCALATION_MODEL"


def test_an_unknown_escalation_model_stops_startup():
    with pytest.raises(RegistryError) as info:
        parse_registry(REGISTRY, env={ENV: "gpt-9"})
    message = str(info.value)
    assert "CLASSIFIER_ESCALATION_MODEL='gpt-9' names no entry in models.yaml" in message
    assert "'reasoner'" in message and "the default 'slow'" in message


def test_the_escalation_model_is_its_entry_one_limit_one_pool_share():
    without = _registry(None)
    reg = _registry("reasoner")
    assert reg.escalation.limit is reg.get("reasoner").limit
    assert reg.total_concurrency() == without.total_concurrency()


def test_parse_registry_never_resolves_a_host(monkeypatch):
    """The image build and the offline registries call parse_registry: an
    external, keyed escalation model must load with no resolver at all."""
    def refuse(*args, **kwargs):
        raise AssertionError(f"parse_registry resolved a host: {args!r}")

    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "create_connection"):
        monkeypatch.setattr(socket, name, refuse)
    reg = parse_registry(REGISTRY, env={ENV: "keyed"})            # key unset: unavailable
    assert reg.escalation.id == "keyed" and not reg.escalation.available
    keyed = parse_registry(REGISTRY, env={ENV: "keyed", "ESCALATION_TEST_KEY": "k"})
    assert keyed.escalation.available and keyed.escalation.authenticated
    checked_in = load_registry(env={ENV: "qwen3.8-solo"})
    assert checked_in.escalation.id == "qwen3.8-solo"


def test_an_unavailable_escalation_model_warns_at_startup(use, caplog):
    use("keyed")
    with caplog.at_level(logging.INFO, logger="classifier"):
        llm_models.startup_check()
    lines = [r.getMessage() for r in caplog.records]
    assert any(line.startswith("models: keyed [escalation] →") for line in lines)
    assert any(line.startswith("models: slow [default] →") for line in lines)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
                and "escalation" in r.getMessage()]
    assert len(warnings) == 1
    assert "the escalation model keyed [escalation] (CLASSIFIER_ESCALATION_MODEL)" in warnings[0]
    assert "ESCALATION_TEST_KEY" in warnings[0] and "'adaptive' is refused" in warnings[0]


def test_an_available_escalation_model_does_not_warn(use, caplog):
    use("reasoner")
    with caplog.at_level(logging.INFO, logger="classifier"):
        llm_models.startup_check()
    assert not [r for r in caplog.records
                if r.levelno == logging.WARNING and "escalation" in r.getMessage()]
    assert any(r.getMessage().startswith("models: reasoner [escalation] →")
               for r in caplog.records)


def test_adaptive_is_a_400_while_the_escalation_model_is_unavailable(use):
    use("keyed")
    message = _refusal({"documents": [TXT], "criteria": [
        {"name": "x", "options": {"model": "quick", "mode": "adaptive"}}]})
    assert config.ADAPTIVE_ESCALATION_UNAVAILABLE.split("{")[0] in message
    assert "the escalation model 'keyed' (CLASSIFIER_ESCALATION_MODEL; GET /models escalation)" \
        in message
    assert "ESCALATION_TEST_KEY" in message
    # fast does not need it
    AssessRequest.model_validate({"documents": [TXT], "criteria": [
        {"name": "x", "options": {"model": "quick", "mode": "fast"}}]})


# ---------------------------------------------------------------------------
# models.yaml: fast.escalate_below
# ---------------------------------------------------------------------------


def test_escalate_below_defaults_per_answer_type_and_is_filled_in():
    reg = _registry()
    assert reg.get("quick").fast.escalate_below == {"rating": config.FAST_DEFAULT_ESCALATE_BELOW}
    assert config.FAST_DEFAULT_ESCALATE_BELOW == 80
    assert reg.get("thresholded").fast.escalate_below_for("rating") == 95
    assert load_registry().get("qwen3.8-solo").fast.escalate_below == {"rating": 80}


@pytest.mark.parametrize("value, needle", [
    ({"rating": 101}, "less than or equal to 100"),
    ({"rating": -1}, "greater than or equal to 0"),
    ({"yesno": 50}, "escalate_below"),
    ({"rating": "high"}, "escalate_below"),
])
def test_invalid_escalate_below_refuses_to_load(value, needle):
    with pytest.raises(RegistryError) as info:
        parse_registry(_data(a=_entry("http://a:1/x", "a",
                                      fast={**QUICK_FAST, "escalate_below": value})), env={})
    assert needle in str(info.value)


def test_escalate_below_bounds_are_accepted():
    for value in (config.ESCALATE_BELOW_MIN, config.ESCALATE_BELOW_MAX):
        fast = {**QUICK_FAST, "escalate_below": {"rating": value}}
        reg = parse_registry(_data(a=_entry("http://a:1/x", "a", fast=fast)), env={})
        assert reg.default.fast.escalate_below == {"rating": value}


# ---------------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------------


def test_get_models_reports_the_escalation_model(registry):
    from api.introspection import list_models

    payload = json.loads(list_models().body.decode("utf-8"))
    assert list(payload) == ["default", "escalation", "models"]
    assert payload["escalation"] == {"id": "reasoner", "host": "reasoner.example.com",
                                     "authenticated": False, "available": True}
    by_id = {m["id"]: m for m in payload["models"]}
    assert by_id["quick"]["fast"]["escalate_below"] == {"rating": 80}
    assert by_id["thresholded"]["fast"]["escalate_below"] == {"rating": 95}


def test_get_models_reports_an_unavailable_external_escalation_model(use):
    from api.introspection import list_models

    use("keyed")
    payload = json.loads(list_models().body.decode("utf-8"))
    assert payload["escalation"] == {"id": "keyed", "host": "claude.example.com",
                                     "authenticated": True, "available": False}
    assert "ESCALATION_TEST_KEY=" not in json.dumps(payload["escalation"])


def test_get_models_on_the_checked_in_registry_escalates_to_the_default():
    from api.introspection import list_models

    previous = llm_models.use_registry(load_registry(env={}))
    try:
        payload = json.loads(list_models().body.decode("utf-8"))
    finally:
        llm_models.use_registry(previous)
    assert payload["escalation"] == {"id": "muse-glimmer", "host": "muse-glimmer",
                                     "authenticated": False, "available": True}


def test_get_criterion_types_has_adaptive(registry):
    from api.introspection import list_criterion_types

    payload = json.loads(list_criterion_types().body.decode("utf-8"))
    block = payload["mode"]
    assert block["values"] == ["reasoned", "fast", "adaptive"]
    assert block["adaptive"] == config.MODE_ADAPTIVE_SUMMARY
    assert block["fast"] == config.MODE_FAST_SUMMARY
    assert block["escalation"] == {"id": "reasoner", "available": True,
                                   "env": "CLASSIFIER_ESCALATION_MODEL",
                                   "catalog": "GET /models escalation"}
    assert block["escalate_below"] == {"minimum": 0, "maximum": 100, "default": 80,
                                       "resolution": config.ESCALATE_BELOW_RESOLUTION}
    assert block["identity"] == config.MODE_IDENTITY_NOTE
    llm = payload["types"]["llm"]
    assert "escalate_below" in llm["options_schema"]["properties"]
    assert "escalate_below" not in llm["defaults"] and "mode" not in llm["defaults"]
    assert "escalation" in llm["result"]["fields"]


# ---------------------------------------------------------------------------
# Submit: the named 400s, the payload, identity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("options", [
    {"model": "quick", "escalate_below": 70},
    {"model": "quick", "mode": "fast", "escalate_below": 70},
    {"model": "quick", "mode": "reasoned", "escalate_below": 70},
])
def test_escalate_below_needs_adaptive(registry, options):
    message = _refusal({"documents": [TXT], "criteria": [{"name": "x", "options": options}]})
    assert config.ESCALATE_BELOW_NEEDS_ADAPTIVE in message


@pytest.mark.parametrize("value, needle", [
    (101, "less than or equal to 100"), (-1, "greater than or equal to 0"),
    ("80", "options.escalate_below"), (80.5, "options.escalate_below"),
    (True, "options.escalate_below"),
])
def test_escalate_below_is_an_int_0_100(registry, value, needle):
    message = _refusal({"documents": [TXT], "criteria": [{"name": "x", "options": {
        "model": "quick", "mode": "adaptive", "escalate_below": value}}]})
    assert needle in message


@pytest.mark.parametrize("type_", ["cv", "text", "detector"])
def test_escalate_below_on_another_type_is_named(registry, type_):
    message = _refusal({"documents": [TXT], "criteria": [
        {"name": "sharpness", "type": type_, "options": {"escalate_below": 70}}]})
    assert f"options.escalate_below: {config.ESCALATE_BELOW_NOT_LLM}" in message


def test_a_null_escalate_below_is_unset(registry):
    c = CriterionInput.model_validate({"name": "x", "options": {
        "model": "quick", "escalate_below": None}})
    assert "escalate_below" not in c.resolved_options()


def test_adaptive_on_a_model_without_a_fast_block_is_named(registry):
    message = _refusal({"documents": [TXT], "criteria": [
        {"name": "x", "options": {"model": "slow", "mode": "adaptive"}}]})
    assert "criterion 'x' has options.mode 'adaptive', and its model 'slow' (its options.model)" \
        in message
    assert "(adaptive starts with the fast readout)" in message
    assert "fast-capable models: ['quick', 'quicktext', 'capped', 'thresholded']" in message


def test_a_reference_on_an_adaptive_criterion_is_named(registry):
    message = _refusal({"documents": [TXT], "references": ["r0123456789ab"], "criteria": [
        {"name": "x", "options": {"model": "quick", "mode": "adaptive", "reference": {}}}]})
    assert f"options.reference: {config.ADAPTIVE_NOT_GUIDED} ('x')" in message


def test_adaptive_is_accepted_and_echoed_only_when_sent(registry):
    req = AssessRequest.model_validate({"documents": [TXT], "criteria": [
        {"name": "a", "options": {"model": "quick", "mode": "adaptive", "escalate_below": 65}},
        {"name": "b", "options": {"model": "quick", "mode": "adaptive"}},
        {"name": "c", "options": {"model": "quick"}},
    ]})
    a, b, c = req.criteria
    assert a.is_adaptive() and a.reads_out() and not a.is_fast() and not a.guided_by_llm()
    assert a.answered_by_model()
    ra, rb, rc = (x.resolved_options() for x in (a, b, c))
    assert (ra["mode"], ra["escalate_below"]) == ("adaptive", 65)
    assert list(ra)[-2:] == ["mode", "escalate_below"]
    assert rb["mode"] == "adaptive" and "escalate_below" not in rb
    assert "mode" not in rc and "escalate_below" not in rc


def test_adaptive_skips_the_references_image_rule(registry):
    # quicktext is text-only: a GUIDED criterion on it would be refused with
    # references on the request; an adaptive one is never guided.
    AssessRequest.model_validate({"documents": [TXT], "references": ["r0123456789ab"],
                                  "criteria": [{"name": "x", "options": {
                                      "model": "quicktext", "mode": "adaptive"}}]})


def test_a_text_only_escalation_model_is_refused_on_a_photo_without_ocr(use):
    from fastapi import HTTPException

    from api.assess import _check_documents, _Upload

    use("reasonertext")
    photo = _Upload(raw=_png(), filename="p.png", declared="image/png")
    req = AssessRequest.model_validate({"documents": [TXT], "criteria": [
        {"name": "q", "options": {"model": "quick", "mode": "adaptive"}}]})
    with pytest.raises(HTTPException) as info:
        _check_documents(req, [photo])        # the unit tests run with OCR engine "none"
    assert info.value.status_code == 400
    assert "'q' (escalation model 'reasonertext')" in info.value.detail
    assert "text-only model" in info.value.detail
    # A text document is fine; so is the same photo with a vision escalation model.
    _check_documents(req, [_Upload(raw=b"Total due $5", filename="t.txt", declared=None)])
    use("reasoner")
    _check_documents(AssessRequest.model_validate({"documents": [TXT], "criteria": [
        {"name": "q", "options": {"model": "quick", "mode": "adaptive"}}]}), [photo])


def test_criteria_round_trip_through_the_payload(registry):
    from jobs.payloads import SubmittedDocument, build_assess_payload

    req = AssessRequest.model_validate({"documents": [TXT], "criteria": [
        {"name": "adaptive", "options": {"model": "quick", "mode": "adaptive",
                                         "escalate_below": 70, "hint": "presence"}},
        {"name": "plain", "options": {"model": "quick", "mode": "adaptive"}},
        {"name": "fast", "options": {"model": "quick", "mode": "fast"}},
        {"name": "unset", "options": {"hint": "quality"}},
        {"name": "sharpness", "type": "cv"},
    ]})
    payload = build_assess_payload(req, [SubmittedDocument(b"x", "t.txt", None, "txt", 1)])
    again = [CriterionInput.model_validate(c) for c in json.loads(json.dumps(payload["criteria"]))]
    assert [c.resolved_options() for c in again] == [c.resolved_options() for c in req.criteria]
    assert [c.resolved_options().get("escalate_below") for c in again] == [70, None, None, None,
                                                                          None]
    assert [c.is_adaptive() for c in again] == [True, True, False, False, False]
    rebuilt = AssessRequest.model_validate({"documents": [TXT], "criteria": payload["criteria"]})
    assert [c.model_dump() for c in rebuilt.criteria] == payload["criteria"]


def test_identity_drops_mode_and_escalate_below(registry):
    from api.criterion_options import identity
    from api.references import _same_question
    from references.resolve import _same

    unset = CriterionInput.model_validate({"name": "x", "options": {"hint": "presence"}})
    for other in ({"mode": "adaptive", "model": "quick"},
                  {"mode": "adaptive", "model": "quick", "escalate_below": 55}):
        c = CriterionInput.model_validate({"name": "x", "options": {"hint": "presence", **other}})
        assert identity(c.resolved_options()) == identity(unset.resolved_options())
        assert _same(c, unset) and _same_question(c, unset)


def test_the_escalate_label_is_its_own_scoring_call_kind():
    assert llm_client.call_kind("escalate/has a house") == "escalate"
    assert "escalate" in llm_client.CALL_KINDS
    assert llm_client.call_kind("score/escalate/x") == "score"
    labels = [p for p, _ in llm_client._KIND_PREFIXES]
    assert labels.index("escalate/") < labels.index("score/")


def test_the_literal_mode_spells_out_its_config_tuple():
    import typing

    from api import criterion_options

    assert typing.get_args(criterion_options.Mode) == config.MODES
    assert "adaptive" in config.MODES and config.READOUT_MODES == ("fast", "adaptive")
    assert config.PROMPT_STYLES == ("reasoned", "fast")      # adaptive has no prompt of its own


# ---------------------------------------------------------------------------
# The flow, end to end through analyze_document
# ---------------------------------------------------------------------------


def _detail(entry: dict) -> dict:
    return entry["detail"]


def test_a_confident_readout_stands(registry, monkeypatch):
    model = Scripted(CONFIDENT_9)
    monkeypatch.setattr(llm_client, "_send", model)
    entry = _entries(_analyze(_png(), "p.png", [_ac(hint="presence")]))["has a house"]
    assert entry["status"] == "ok" and entry["score"] == 10 and entry["verdict"] == "PASS"
    assert entry["confidence"] == 98
    assert entry["reason"].startswith("Fast readout (no reasoning): PASS 0.98")
    detail = _detail(entry)
    assert detail["mode"] == "fast" and detail["model"] == "quick"
    assert detail["confidence"]["source"] == "readout"
    assert detail["escalation"] == {"escalated": False, "model": "reasoner", "threshold": 80,
                                    "signal": 98, "why": None, "error": None, "fast": None}
    assert entry["options_used"]["mode"] == "adaptive"
    assert "escalate_below" not in entry["options_used"]
    assert [m for m, _ in model.calls] == ["quick"] and len(model.readouts()) == 1


def test_low_confidence_escalates_to_the_reasoned_call(registry, monkeypatch):
    model = Scripted(UNSURE)
    monkeypatch.setattr(llm_client, "_send", model)
    rows = []

    class Store:
        async def record(self, row):
            rows.append(row)

    monkeypatch.setattr(llm_usage, "usage_store", Store())
    before = _escalations("reasoner", "confidence", "ok")
    entry = _entries(_analyze(_png(), "p.png", [_ac(hint="presence")]))["has a house"]
    assert entry["status"] == "ok"
    assert (entry["score"], entry["verdict"], entry["confidence"]) == (2, "FAIL", 70)
    assert entry["reason"] == "escalated: no"
    detail = _detail(entry)
    assert detail["mode"] == "reasoned" and detail["model"] == "reasoner"
    assert tuple(detail["confidence"]) == llm_confidence.BLOCK_KEYS
    assert detail["confidence"]["source"] == "stated" and detail["confidence"]["stated"] == 70
    assert detail["escalation"] == {
        "escalated": True, "model": "reasoner", "threshold": 80, "signal": 60,
        "why": "confidence 60 < 80", "error": None,
        "fast": {"model": "quick", "score": 10, "verdict": "PASS", "confidence": 60,
                 "coverage": 1.0},
    }
    assert [m for m, _ in model.calls] == ["quick", "reasoner"]
    assert [(r["kind"], r["label"], r["model_id"]) for r in rows] == [
        ("fast", "fast/has a house", "quick"),
        ("escalate", "escalate/has a house", "reasoner"),
    ]
    assert _escalations("reasoner", "confidence", "ok") == before + 1
    # The escalation is today's reasoned scoring call, on the escalation model.
    _, prompt = model.reasoned()[0]
    assert prompt["model"] == "reasoner-model"
    assert prompt["response_format"] == {"type": "json_object"} and prompt["max_tokens"] == 100
    assert "Score this one criterion" in _user_text(prompt)


@pytest.mark.parametrize("top, escalate_below, model_id, escalated", [
    (NINETY, None, "quick", False),          # 90 >= the entry's default 80
    (NINETY, None, "thresholded", True),     # 90 < the entry's 95
    (NINETY, 95, "quick", True),             # the criterion's own threshold wins …
    (NINETY, 50, "thresholded", False),      # … both ways
    (CONFIDENT_9, 99, "quick", True),
    (UNSURE, 60, "quick", False),            # at the threshold: stands (escalates UNDER it)
    (UNSURE, 0, "quick", False),             # 0: never on confidence
])
def test_the_threshold_is_the_criterions_else_its_models(registry, monkeypatch, top,
                                                         escalate_below, model_id, escalated):
    monkeypatch.setattr(llm_client, "_send", Scripted(top))
    options = {"model": model_id}
    if escalate_below is not None:
        options["escalate_below"] = escalate_below
    entry = _entries(_analyze(_png(), "p.png", [_ac(**options)]))["has a house"]
    escalation = _detail(entry)["escalation"]
    expected = escalate_below if escalate_below is not None else (95 if model_id == "thresholded"
                                                                  else 80)
    assert escalation["threshold"] == expected
    assert escalation["escalated"] is escalated
    assert _detail(entry)["mode"] == ("reasoned" if escalated else "fast")
    if escalate_below is not None:
        assert entry["options_used"]["escalate_below"] == escalate_below


@pytest.mark.parametrize("model_id, top, why, prefix", [
    ("quick", LOW_COVERAGE, "incomplete", "readout incomplete: coverage 0.20 < 0.5"),
    ("capped", SPREAD, "cap", "probability cap reached: 5 of 10 labels"),
])
def test_an_unreadable_readout_escalates(registry, monkeypatch, model_id, top, why, prefix):
    model = Scripted(top)
    monkeypatch.setattr(llm_client, "_send", model)
    before = _escalations("reasoner", why, "ok")
    entry = _entries(_analyze(_png(), "p.png", [_ac(model=model_id)]))["has a house"]
    assert entry["status"] == "ok" and entry["score"] == 2
    escalation = _detail(entry)["escalation"]
    assert escalation["escalated"] is True and escalation["signal"] is None
    assert escalation["why"].startswith(prefix)
    assert "use mode" not in escalation["why"]         # the summary, without the remedy
    fast = escalation["fast"]
    assert (fast["model"], fast["score"], fast["verdict"], fast["confidence"]) == (
        model_id, None, None, None)
    assert fast["coverage"] == pytest.approx(0.2 if why == "incomplete" else 0.6, abs=1e-4)
    assert _escalations("reasoner", why, "ok") == before + 1
    assert len(model.readouts()) == 1                   # never retried


def test_a_readout_without_logprobs_escalates_with_no_coverage(registry, monkeypatch):
    async def send(prompt):
        spec = llm_client._target.get()
        if is_readout(prompt):
            return {"choices": [{"message": {"content": "9"}, "finish_reason": "length"}]}
        assert spec.id == "reasoner"
        return {"choices": [{"message": {"content": json.dumps(
            {"score": 8, "verdict": "PASS", "confidence": 75, "reason": "seen"})}}]}

    monkeypatch.setattr(llm_client, "_send", send)
    entry = _entries(_analyze(_png(), "p.png", [_ac()]))["has a house"]
    escalation = _detail(entry)["escalation"]
    assert entry["score"] == 8 and escalation["escalated"] is True
    assert escalation["why"].startswith("readout incomplete: the response carried no token")
    assert escalation["fast"]["coverage"] is None


def test_a_failed_escalation_keeps_a_complete_readout(registry, monkeypatch):
    model = Scripted(UNSURE, fail={"reasoner"})
    monkeypatch.setattr(llm_client, "_send", model)
    before = _escalations("reasoner", "confidence", "failed")
    entry = _entries(_analyze(_png(), "p.png", [_ac()]))["has a house"]
    assert entry["status"] == "ok" and entry["complete"] is not False
    assert (entry["score"], entry["verdict"], entry["confidence"]) == (10, "PASS", 60)
    detail = _detail(entry)
    assert detail["mode"] == "fast" and detail["model"] == "quick"
    escalation = detail["escalation"]
    assert escalation["escalated"] is False and escalation["fast"] is None
    assert escalation["why"] == "confidence 60 < 80" and escalation["signal"] == 60
    assert escalation["error"].startswith("model 'reasoner' call failed:")
    assert _escalations("reasoner", "confidence", "failed") == before + 1
    assert [m for m, _ in model.calls] == ["quick", "reasoner"]   # an HTTP error: no retry


def test_an_unreadable_readout_whose_escalation_failed_errors_the_unit(registry, monkeypatch):
    model = Scripted(LOW_COVERAGE, fail={"reasoner"})
    monkeypatch.setattr(llm_client, "_send", model)
    before = _escalations("reasoner", "incomplete", "failed")
    entry = _entries(_analyze(_png(), "p.png", [_ac()]))["has a house"]
    assert entry["status"] == "error"
    assert entry["error"].startswith("readout incomplete: coverage 0.20 < 0.5")
    assert "— and the escalation to model 'reasoner' failed: model 'reasoner' call failed" \
        in entry["error"]
    assert _escalations("reasoner", "incomplete", "failed") == before + 1


def test_an_escalation_model_unavailable_at_run_time_keeps_the_readout(use, monkeypatch):
    use("keyed")                     # submitted elsewhere; unavailable when it runs
    model = Scripted(UNSURE)
    monkeypatch.setattr(llm_client, "_send", model)
    entry = _entries(_analyze(_png(), "p.png", [_ac()]))["has a house"]
    escalation = _detail(entry)["escalation"]
    assert entry["status"] == "ok" and escalation["escalated"] is False
    assert escalation["error"].startswith("escalation model 'keyed' is unavailable: its API key")
    assert [m for m, _ in model.calls] == ["quick"]


def test_the_escalation_uses_its_models_text_budget(registry, monkeypatch):
    model = Scripted(UNSURE)
    monkeypatch.setattr(llm_client, "_send", model)
    text = "Total due $5. " + "x" * 200
    doc = analysis.load_document_bytes(text.encode(), "t.txt", None, keep_source=True)
    entry = _entries(asyncio.run(analysis.analyze_document(doc, [_ac()])))["has a house"]
    assert _detail(entry)["escalation"]["escalated"] is True
    (_, readout), = model.readouts()
    (_, escalate), = model.reasoned()
    assert text.strip() in _user_text(readout)              # quick's budget is 1000
    assert text[:20] in _user_text(escalate) and text[:21] not in _user_text(escalate)
    assert "[text truncated at the configured character budget]" in _user_text(escalate)
    assert _detail(entry)["text_sent"] == {"chars": 20, "truncated": True, "budget": 20,
                                           "source": _detail(entry)["text_sent"]["source"]}
    assert _detail(entry)["image_sent"] is False


def test_the_escalation_sends_the_page_only_to_a_vision_model(use, monkeypatch):
    use("reasoner")
    model = Scripted(UNSURE)
    monkeypatch.setattr(llm_client, "_send", model)
    entry = _entries(_analyze(_png(), "p.png", [_ac()]))["has a house"]
    (_, escalate), = model.reasoned()
    assert any(part["type"] == "image_url" for part in escalate["messages"][1]["content"])
    assert _detail(entry)["image_sent"] is True
    # A text-only escalation model on a photo with no text: it cannot answer,
    # so the complete readout stands, saying why.
    use("reasonertext")
    model = Scripted(UNSURE)
    monkeypatch.setattr(llm_client, "_send", model)
    entry = _entries(_analyze(_png(), "p.png", [_ac()]))["has a house"]
    escalation = _detail(entry)["escalation"]
    assert escalation["escalated"] is False
    assert escalation["error"] == config.ESCALATION_NO_TEXT.format(
        model="reasonertext", source=_detail(entry)["text_sent"]["source"])
    assert not model.reasoned()


def test_the_escalation_is_measured_per_its_confidence_method(use, monkeypatch):
    use("slow")                      # confidence.method logprobs
    model = Scripted(UNSURE)
    monkeypatch.setattr(llm_client, "_send", model)
    entry = _entries(_analyze(_png(), "p.png", [_ac()]))["has a house"]
    (_, escalate), = model.reasoned()
    assert escalate["logprobs"] is True and escalate["top_logprobs"] == 20
    block = _detail(entry)["confidence"]
    assert block["method"] == "logprobs" and block["note"]       # nothing came back to read


def test_score_false_clears_the_escalations_judgement(registry, monkeypatch):
    from llm import boxes as llm_boxes

    monkeypatch.setattr(llm_boxes, "LLM_BBOX_REFINE", False)
    monkeypatch.setattr(llm_boxes, "LLM_BBOX_GRIDLINES", False)
    model = Scripted(UNSURE, answer=(9, "PASS", 70, "the escalation saw it"))
    monkeypatch.setattr(llm_client, "_send", model)
    c = CriterionInput.model_validate({"name": "has a house", "score": False, "options": {
        "hint": "presence", "boxes": True, "model": "quick", "mode": "adaptive"}})
    entry = _entries(_analyze(_png(), "p.png", [c]))["has a house"]
    assert entry["score"] is None and entry["verdict"] is None and entry["confidence"] is None
    detail = _detail(entry)
    assert detail["value"] is None and detail["confidence"] is None
    escalation = detail["escalation"]
    assert escalation["escalated"] is True and escalation["signal"] is None
    assert escalation["why"] == "confidence < 80"              # the number is the judgement
    assert escalation["fast"] == {"model": "quick", "score": None, "verdict": None,
                                  "confidence": None, "coverage": 1.0}
    assert entry["reason"] == "the escalation saw it"           # a model-written reason stays
    # The box loop ran on the criterion's OWN model, reasoned, gated on the
    # escalated answer.
    loop = [(m, p) for m, p in model.reasoned() if "CRITERION:" not in _user_text(p)]
    assert loop and {m for m, _ in loop} == {"quick"}
    for _, prompt in loop:
        assert prompt["response_format"] == {"type": "json_object"}
        assert prompt["chat_template_kwargs"] == {"reasoning_effort": "low"}
    assert entry["localization"]["accepted_attempt"] == 1


def test_score_false_on_a_readout_that_stood(registry, monkeypatch):
    from llm import boxes as llm_boxes

    monkeypatch.setattr(llm_boxes, "LLM_BBOX_REFINE", False)
    monkeypatch.setattr(llm_boxes, "LLM_BBOX_GRIDLINES", False)
    monkeypatch.setattr(llm_client, "_send", Scripted(CONFIDENT_9))
    c = CriterionInput.model_validate({"name": "has a house", "score": False, "options": {
        "hint": "presence", "boxes": True, "model": "quick", "mode": "adaptive"}})
    entry = _entries(_analyze(_png(), "p.png", [c]))["has a house"]
    assert entry["reason"] == config.FAST_REASON_UNJUDGED
    escalation = _detail(entry)["escalation"]
    assert escalation == {"escalated": False, "model": "reasoner", "threshold": 80,
                          "signal": None, "why": None, "error": None, "fast": None}


def _unparseable(top):
    """A scripted ``_send``: readouts get ``top``; every scoring answer states a
    verdict and a confidence but no score, so it never parses."""
    async def send(prompt):
        if is_readout(prompt):
            return readout_response(top)
        body = {"verdict": "PASS", "confidence": 95, "reason": "clearly a house, PASS"}
        return {"choices": [{"message": {"content": json.dumps(body)}, "finish_reason": "stop"}]}

    return send


@pytest.mark.parametrize("judged", [True, False])
def test_an_unparseable_escalation_is_not_quoted_under_score_false(registry, monkeypatch, judged):
    """The parse failure's own message quotes the answer's start — its verdict
    and confidence; a ``score: false`` unit reports the constant instead."""
    monkeypatch.setattr(llm_client, "_send", _unparseable(UNSURE))
    c = CriterionInput.model_validate({"name": "has a house", "score": judged, "options": {
        "hint": "presence", "model": "quick", "mode": "adaptive"}})
    entry = _entries(_analyze(_png(), "p.png", [c]))["has a house"]
    assert entry["status"] == "ok"
    escalation = _detail(entry)["escalation"]
    assert escalation["escalated"] is False
    if judged:
        assert escalation["error"].startswith("model 'reasoner''s answer could not be parsed")
        assert "'verdict': 'PASS'" in escalation["error"]
        return
    assert escalation["error"] == config.ESCALATION_UNPARSED_UNJUDGED.format(
        model="reasoner", attempts=config.MAX_LLM_RETRIES)
    assert entry["score"] is None and entry["verdict"] is None and entry["confidence"] is None
    assert entry["reason"] == config.FAST_REASON_UNJUDGED
    shown = json.dumps(_detail(entry))
    for word in ("PASS", "FAIL", "MARGINAL", "95"):
        assert word not in shown, word


def test_an_unreadable_readout_with_an_unparseable_escalation_under_score_false(registry,
                                                                             monkeypatch):
    monkeypatch.setattr(llm_client, "_send", _unparseable(LOW_COVERAGE))
    c = CriterionInput.model_validate({"name": "has a house", "score": False, "options": {
        "hint": "presence", "model": "quick", "mode": "adaptive"}})
    entry = _entries(_analyze(_png(), "p.png", [c]))["has a house"]
    assert entry["status"] == "error"
    assert entry["error"].endswith(config.ESCALATION_UNPARSED_UNJUDGED.format(
        model="reasoner", attempts=config.MAX_LLM_RETRIES))
    assert "PASS" not in entry["error"] and "95" not in entry["error"]


def test_the_adaptive_detail_matches_its_declaration(registry, monkeypatch):
    from analysis.result_specs import specs

    spec = specs()["llm"]
    for top in (CONFIDENT_9, UNSURE):
        monkeypatch.setattr(llm_client, "_send", Scripted(top))
        detail = _detail(_entries(_analyze(_png(), "p.png", [_ac()]))["has a house"])
        assert set(detail) <= set(spec.fields) and spec.required() <= set(detail)
        assert tuple(detail["escalation"]) == llm_eval.ESCALATION_KEYS
        if detail["escalation"]["fast"] is not None:
            assert tuple(detail["escalation"]["fast"]) == llm_eval.ESCALATION_FAST_KEYS
    field = spec.fields["escalation"]
    assert field.kind == "object" and field.when and not field.stable
    for key in llm_eval.ESCALATION_KEYS + llm_eval.ESCALATION_FAST_KEYS:
        assert key in field.description, key


def test_a_criterion_without_adaptive_carries_no_new_key(registry, monkeypatch):
    monkeypatch.setattr(llm_client, "_send", Scripted(CONFIDENT_9,
                                                      answer=(9, "PASS", 80, "reasoned")))
    result = _analyze(_png(), "p.png", [
        CriterionInput.model_validate({"name": "plain", "options": {"model": "quick"}}),
        CriterionInput.model_validate({"name": "fast", "options": {"model": "quick",
                                                                    "mode": "fast"}}),
    ])
    for name in ("plain", "fast"):
        entry = _entries(result)[name]
        assert "escalation" not in entry["detail"] and "escalate_below" not in entry["options_used"]
    plain = _entries(result)["plain"]
    assert "mode" not in plain["detail"] and "mode" not in plain["options_used"]
    assert list(plain["detail"]) == ["metric", "value", "hint", "model", "image_sent",
                                     "text_sent", "confidence"]


@pytest.mark.parametrize("key, digest", sorted(REASONED_BODY_PINS.items()))
def test_the_escalation_env_var_changes_no_body(key, digest):
    """Naming an escalation model changes nothing a request carries."""
    data = yaml.safe_load(llm_models.DEFAULT_PATH.read_text(encoding="utf-8"))
    reg = parse_registry(data, env={ENV: "qwen3.8-solo"})
    model_id, kind = key
    assert _hash(reg.get(model_id).body([{"role": "user", "content": "x"}], kind)) == digest


# ---------------------------------------------------------------------------
# References: never guided, never saved as a readout's answer
# ---------------------------------------------------------------------------


def test_the_job_guide_notes_why_an_adaptive_criterion_is_not_guided(registry):
    from analysis.references import JobReferences

    for mode in ("explicit", "auto"):
        refs = JobReferences(plan={"mode": mode, "criteria": {}})
        guide = refs.guide(_ac(), 0)
        assert guide is not None and not guide.applied
        assert guide.note == config.ADAPTIVE_NOT_GUIDED_NOTE
        assert not refs.needs_selection(_ac())


def test_an_adaptive_unit_in_a_request_with_references_is_unguided(registry, monkeypatch):
    monkeypatch.setattr(llm_client, "_send", Scripted(UNSURE))
    entry = _entries(_analyze(_png(), "p.png", [_ac()],
                              references_plan={"mode": "explicit", "criteria": {}}))["has a house"]
    reference = _detail(entry)["reference"]
    assert reference["applied"] is False and reference["note"] == config.ADAPTIVE_NOT_GUIDED_NOTE
    assert _detail(entry)["escalation"]["escalated"] is True


def test_explicit_adaptive_criteria_on_post_references_are_refused(registry):
    from api.reference_schemas import ReferenceRequest

    with pytest.raises(Exception) as info:
        ReferenceRequest.model_validate({
            "document": {"type": "base64", "data": base64.b64encode(_png()).decode()},
            "criteria": [{"name": "x", "options": {"model": "quick", "mode": "adaptive"}}],
        })
    assert config.REFERENCE_REFUSES_READOUT_MODE.format(mode="adaptive", names=["x"]) in str(
        info.value)


def test_from_job_strips_mode_and_escalate_below(registry):
    from api.references import _as_reference_criterion

    c = _as_reference_criterion(_ac(escalate_below=70))
    assert c.options.mode is None and c.options.escalate_below is None
    assert c.options.model == "quick" and c.guided_by_llm()
    again = CriterionInput.model_validate(c.model_dump())      # validates: no stray threshold
    assert again.resolved_options() == c.resolved_options()


@pytest.mark.parametrize("modes, unit, expected", [
    ({"x": "adaptive"}, {"status": "ok", "detail": {"mode": "reasoned",
                                                    "escalation": {"escalated": True}}}, None),
    ({"x": "adaptive"}, {"status": "ok", "detail": {"mode": "fast",
                                                    "escalation": {"escalated": False}}},
     "adaptive"),
    ({"x": "adaptive"}, {"status": "error", "detail": {}}, "adaptive"),
    ({"x": "fast"}, {"status": "ok", "detail": {"mode": "fast"}}, "fast"),
    ({"x": "fast"}, {"status": "error", "detail": {}}, "fast"),
    ({}, {"status": "ok", "detail": {"mode": "fast"}}, "fast"),
    ({}, {"status": "ok", "detail": {"mode": "reasoned"}}, None),
    ({}, None, None),
])
def test_which_job_units_are_readout_answers(modes, unit, expected):
    from api.references import _readout_answered

    assert _readout_answered("x", unit, modes) == expected


# ---------------------------------------------------------------------------
# (Postgres) through the API: from_job against an adaptive job
# ---------------------------------------------------------------------------

HOUSE = "has a house"
ROOF = "has a roof"


@pytest.fixture(scope="module")
def client(need_postgres):
    from fastapi.testclient import TestClient

    import main

    with TestClient(main.app) as c:
        yield c


def test_from_job_copies_an_adaptive_unit_only_when_it_escalated(client, monkeypatch):
    from llm import boxes as llm_boxes

    previous = llm_models.use_registry(load_registry(env={}))   # escalation: muse-glimmer
    try:
        monkeypatch.setattr(llm_boxes, "LLM_BBOX_REFINE", False)
        monkeypatch.setattr(llm_boxes, "LLM_BBOX_GRIDLINES", False)
        model = Scripted(tops={HOUSE: CONFIDENT_9, ROOF: UNSURE},
                         answer=(9, "PASS", 85, "the escalation reasoned"))
        monkeypatch.setattr(llm_client, "_send", model)
        doc = {"type": "base64", "data": base64.b64encode(_png()).decode("ascii"),
               "filename": "house.png"}
        options = {"hint": "presence", "boxes": True, "model": "qwen3.8-solo",
                   "mode": "adaptive", "escalate_below": 75}
        r = client.post("/assess", json={"document": doc, "criteria": [
            {"name": HOUSE, "options": options}, {"name": ROOF, "options": options}]})
        assert r.status_code == 202, r.text
        job = _wait(client, f"/jobs/{r.json()['job_id']}", ("completed", "failed"), "phase")
        assert job["phase"] == "completed", job
        per = job["result"]["assessment"]["per_criterion_scores"]
        assert per[HOUSE]["detail"]["escalation"]["escalated"] is False
        assert per[ROOF]["detail"]["escalation"]["escalated"] is True
        assert per[ROOF]["detail"]["model"] == "muse-glimmer"
        stored = {c["name"]: c for c in job["result"]["request"]["criteria"]}
        assert stored[HOUSE]["options"]["escalate_below"] == 75

        model.calls.clear()
        r = client.post("/references", json={"from_job": job["job_id"], "description": "x"})
        assert r.status_code == 202, r.text
        ref = _wait(client, f"/references/{r.json()['reference_id']}", ("ready", "failed"),
                    "status")
        assert ref["status"] == "ready", ref
        record = ref["record"]
        for name in (HOUSE, ROOF):
            options = record["criteria"][name]["input"]["options"]
            assert options.get("mode") is None and options.get("escalate_below") is None
        # The escalated unit's reasoned answer is copied …
        assert (record["criteria"][ROOF].get("observed") or {}).get("job_id") == job["job_id"]
        # … the readout that stood is not, and the warning says why.
        assert (record["criteria"][HOUSE].get("observed") or {}).get("job_id") != job["job_id"]
        assert "Fast readout" not in json.dumps(record)
        assert any("ran it adaptive" in w and "did not escalate" in w and HOUSE in w
                   for w in record["warnings"])
        assert not model.readouts()                   # the creation job scores reasoned

        r = client.post("/references", json={"document": doc, "criteria": [
            {"name": HOUSE, "options": {"model": "qwen3.8-solo", "mode": "adaptive"}}]})
        assert r.status_code == 400
        assert "options.mode 'adaptive' on ['has a house']" in r.json()["detail"]
    finally:
        llm_models.use_registry(previous)


# ---------------------------------------------------------------------------
# bin/confidence_calibration.py --mode fast
# ---------------------------------------------------------------------------

_TOOL = pathlib.Path(__file__).resolve().parents[2] / "ai" / "classifier" / "bin" / \
    "confidence_calibration.py"
_tool_spec = importlib.util.spec_from_file_location("confidence_calibration", _TOOL)
cc = importlib.util.module_from_spec(_tool_spec)
_tool_spec.loader.exec_module(cc)


def _row(logprobs: dict, label: str) -> dict:
    return {"logprobs": logprobs, "label": label, "coverage": 1.0, "unseen_bound": 0.0}


def test_the_temperature_fit_flattens_an_overconfident_readout():
    import math

    # Always 0.95 sure, right half the time: a temperature > 1 lowers the loss.
    rows = [_row({"9": math.log(0.95), "0": math.log(0.05)}, "PASS"),
            _row({"9": math.log(0.95), "0": math.log(0.05)}, "FAIL")] * 5
    fit = cc._fit_temperature(rows)
    assert fit["temperature"] > 1.0 and fit["log_loss"] < fit["log_loss_raw"]
    assert fit["temperature"] in cc.TEMPERATURE_GRID and fit["n"] == 10


def test_the_temperature_fit_sharpens_an_underconfident_readout():
    import math

    rows = [_row({"9": math.log(0.6), "0": math.log(0.4)}, "PASS")] * 6
    fit = cc._fit_temperature(rows)
    assert fit["temperature"] < 1.0 and fit["log_loss"] < fit["log_loss_raw"]
    assert cc._fit_temperature([]) is None
    assert 1.0 in cc.TEMPERATURE_GRID


def test_the_calibration_tool_fast_mode_on_a_scripted_model(use, monkeypatch, tmp_path, capsys):
    use("reasoner")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "p.png").write_bytes(_png())
    door = "has a door"
    criteria = tmp_path / "criteria.json"
    criteria.write_text(json.dumps([{"name": HOUSE, "options": {"hint": "presence"}},
                                    {"name": ROOF, "options": {"hint": "presence"}},
                                    {"name": door, "options": {"hint": "presence"}}]),
                        encoding="utf-8")
    labels = tmp_path / "labels.json"
    labels.write_text(json.dumps({"p.png": {HOUSE: "PASS", ROOF: "fail", door: "PASS"}}),
                      encoding="utf-8")
    model = Scripted(tops={HOUSE: CONFIDENT_9, ROOF: UNSURE, door: LOW_COVERAGE},
                     answers={HOUSE: (9, "PASS", 90, "y"), ROOF: (2, "FAIL", 80, "n"),
                              door: (8, "PASS", 85, "y")})
    monkeypatch.setattr(llm_client, "_send", model)
    # The tool installs its meter and readout capture process-wide; restored here.
    monkeypatch.setattr(llm_client, "call_readout", llm_client.call_readout)
    monkeypatch.setattr(llm_usage, "usage_store", llm_usage.usage_store)
    out = tmp_path / "out.json"
    args = argparse.Namespace(
        docs=str(docs), criteria=str(criteria), labels=str(labels), model="quick",
        escalation_model=None, concurrency=1, samples=1, export_labels=None,
        deliberate_reasoning="xhigh", deliberate_max_tokens=32768, deliberate_timeout=900,
        out=str(out), mode="fast", max_accuracy_loss=0.01, ground_truth="human",
    )
    summary = asyncio.run(cc.run(args))

    assert (summary["mode"], summary["model"], summary["escalation_model"]) == (
        "fast", "quick", "reasoner")
    assert summary["labelled"] == 3 and summary["unreadable"] == {"incomplete": 1}
    rows = summary["rows"]
    assert rows["fast_raw"]["n"] == 2 and rows["fast_raw"]["accuracy"] == 0.5
    assert rows["reasoned"]["n"] == 3 and rows["reasoned"]["accuracy"] == 1.0
    assert rows["reasoned"]["model"] == "reasoner"
    fit = summary["temperature_fit"]
    assert fit["temperature"] in cc.TEMPERATURE_GRID and fit["log_loss"] <= fit["log_loss_raw"]
    assert rows["fast_calibrated"]["temperature"] == fit["temperature"]
    # Each pass ran where it should: readouts on quick, reasoned calls on reasoner.
    assert {m for m, _ in model.readouts()} == {"quick"}
    assert {m for m, _ in model.reasoned()} == {"reasoner"}

    curve = summary["curve"]
    assert [c["threshold"] for c in curve] == list(range(50, 100, 5))
    rates = [c["escalation_rate"] for c in curve]
    assert rates == sorted(rates) and rates[0] >= round(1 / 3, 4)      # the door always escalates
    assert curve[0]["accuracy"] == pytest.approx(2 / 3, abs=1e-4)      # the roof stood, wrongly
    assert curve[-1]["accuracy"] == 1.0
    suggested = summary["suggested_escalate_below"]
    assert suggested == next(c["threshold"] for c in curve if c["accuracy"] >= 0.99)
    assert suggested > 50
    block = summary["fast_block"]
    assert block["calibration"]["rating"]["temperature"] == fit["temperature"]
    assert block["calibration"]["rating"]["ground_truth"] == "human"
    assert block["escalate_below"] == {"rating": suggested}

    # The printed block is a valid models.yaml `fast:` block.
    printed = capsys.readouterr().out
    assert "Escalation curve" in printed and "suggested escalate_below" in printed
    tail = printed[printed.index("      calibration:"):].splitlines()
    snippet = "\n".join(line for line in tail[:tail.index("")] if line.startswith("      "))
    loaded = yaml.safe_load(textwrap.dedent(snippet))
    fast = llm_models.FastConfig.model_validate({**QUICK_FAST, **loaded})
    assert fast.calibration["rating"].temperature == fit["temperature"]
    assert fast.escalate_below == {"rating": suggested}

    report = json.loads(out.read_text(encoding="utf-8"))
    by_name = {r["criterion"]: r for r in report["records"]}
    assert by_name[HOUSE]["readout"] == "ok" and set(by_name[HOUSE]["logprobs"]) >= {"9"}
    assert by_name[door]["readout"] == "incomplete" and by_name[door]["logprobs"] is None
    assert by_name[ROOF]["reasoned"] == {"verdict": "FAIL", "confidence": 80}
    assert all(r["fast_seconds"] >= 0 and r["reasoned_seconds"] >= 0 for r in report["records"])


def _tool_args(tmp_path, docs, criteria, labels, out=None) -> argparse.Namespace:
    return argparse.Namespace(
        docs=str(docs), criteria=str(criteria), labels=str(labels), model="quick",
        escalation_model=None, concurrency=1, samples=1, export_labels=None,
        deliberate_reasoning="xhigh", deliberate_max_tokens=32768, deliberate_timeout=900,
        out=str(out) if out else None, mode="fast", max_accuracy_loss=0.01,
        ground_truth="human",
    )


def test_the_calibration_tool_times_each_file_on_its_own(use, monkeypatch, tmp_path):
    """Item indices restart at 0 in every file, so a criterion shared by
    several files must not carry an earlier file's seconds."""
    use("reasoner")
    docs = tmp_path / "docs"
    docs.mkdir()
    names = ("a.png", "b.png", "c.png")
    for name in names:
        (docs / name).write_bytes(_png())
    criteria = tmp_path / "criteria.json"
    criteria.write_text(json.dumps([{"name": HOUSE, "options": {"hint": "presence"}}]),
                        encoding="utf-8")
    labels = tmp_path / "labels.json"
    labels.write_text(json.dumps({n: {HOUSE: "PASS"} for n in names}), encoding="utf-8")
    # A clock only the scripted model moves: every call takes exactly 1 s.
    clock = [0.0]
    scripted = Scripted(UNSURE, answer=(9, "PASS", 90, "y"))

    async def send(prompt):
        clock[0] += 1.0
        return await scripted(prompt)

    monkeypatch.setattr(cc, "time", argparse.Namespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(llm_client, "_send", send)
    monkeypatch.setattr(llm_client, "call_readout", llm_client.call_readout)
    monkeypatch.setattr(llm_usage, "usage_store", llm_usage.usage_store)
    out = tmp_path / "out.json"
    asyncio.run(cc.run(_tool_args(tmp_path, docs, criteria, labels, out)))
    records = json.loads(out.read_text(encoding="utf-8"))["records"]
    assert [(r["file"], r["fast_seconds"], r["reasoned_seconds"]) for r in records] == [
        (name, 1.0, 1.0) for name in names]
    assert all(c["model_seconds_per_item"] in (1.0, 2.0)
               for c in json.loads(out.read_text(encoding="utf-8"))["summary"]["curve"])


def _record(readout: str, label: str, logprobs=None, reasoned=None) -> dict:
    return {"readout": readout, "label": label, "logprobs": logprobs,
            "coverage": 1.0 if logprobs else None, "unseen_bound": 0.0 if logprobs else None,
            "fast_seconds": 1.0, "reasoned_seconds": 2.0 if reasoned else 0.0,
            "reasoned": reasoned}


def test_the_calibration_curve_and_baseline_count_the_same_units():
    """Production's adaptive, unit by unit: a readout whose own request
    failed (``error``) is not escalated — the unit errors and counts wrong;
    every row's ``accuracy_all`` and the curve divide by every labelled unit,
    a failed reasoned call counting wrong."""
    import math
    import types

    confident = {"9": math.log(0.98), "1": math.log(0.02)}       # PASS, confidence 98
    unsure = {"9": math.log(0.6), "1": math.log(0.4)}            # PASS, confidence 60
    records = [
        # stands at every threshold, right
        _record("ok", "PASS", confident, {"verdict": "PASS", "confidence": 90}),
        # the readout's request failed: the unit errors (its reasoned answer is
        # right, but adaptive never asks for it)
        _record("error", "PASS", None, {"verdict": "PASS", "confidence": 90}),
        # unsure and wrong; its reasoned call failed, so the readout stands
        _record("ok", "FAIL", unsure, None),
    ]
    curve = {c["threshold"]: c for c in cc._curve(records, 1.0)}
    assert curve[50] == {"threshold": 50, "escalation_rate": 0.0,
                         "accuracy": round(1 / 3, 4), "model_seconds_per_item": 1.0}
    assert curve[95] == {"threshold": 95, "escalation_rate": round(1 / 3, 4),
                         "accuracy": round(1 / 3, 4), "model_seconds_per_item": 1.0}

    args = types.SimpleNamespace(max_accuracy_loss=0.01, ground_truth="human")
    meter = types.SimpleNamespace(summary=lambda: {})
    spec = types.SimpleNamespace(id="quick")
    summary = cc._summarise_fast(records, spec, types.SimpleNamespace(id="reasoner"), args,
                                 meter, 0.0)
    rows = summary["rows"]
    assert (rows["fast_raw"]["n"], rows["fast_raw"]["accuracy"]) == (2, 0.5)
    assert rows["fast_raw"]["accuracy_all"] == round(1 / 3, 4)
    assert (rows["reasoned"]["n"], rows["reasoned"]["accuracy"]) == (2, 1.0)
    assert rows["reasoned"]["accuracy_all"] == round(2 / 3, 4)    # the failed call counts wrong
    assert summary["reasoned_failed"] == 1 and summary["unreadable"] == {"error": 1}


def test_the_calibration_tool_fast_mode_needs_labels_and_a_fast_model(use, tmp_path):
    use("reasoner")
    docs = tmp_path / "docs"
    docs.mkdir()
    criteria = tmp_path / "criteria.json"
    criteria.write_text("[]", encoding="utf-8")
    base = dict(docs=str(docs), criteria=str(criteria), labels=None, model="quick",
                escalation_model=None, concurrency=1, samples=1, export_labels=None, out=None,
                mode="fast", max_accuracy_loss=0.01, ground_truth="human")
    with pytest.raises(SystemExit, match="needs --labels"):
        asyncio.run(cc.run(argparse.Namespace(**base)))
    labels = tmp_path / "labels.json"
    labels.write_text(json.dumps({"p.png": {"x": "PASS"}}), encoding="utf-8")
    with pytest.raises(SystemExit, match="has no fast block"):
        asyncio.run(cc.run(argparse.Namespace(**{**base, "labels": str(labels), "model": "slow"})))
