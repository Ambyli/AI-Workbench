"""The model registry (models.yaml, llm/models.py) and per-model routing.

No model server: requests are scripted at the bare transport
``llm.client._send`` — which reads the target model from the ContextVar
``_post`` sets — so the per-model limit, URL and recording are all real.

Pinned:

  * the checked-in models.yaml loads, its default is muse-glimmer with the
    chat_template_kwargs reasoning, and qwen2.5-vl documents the fallback;
  * load-time refusals: invalid YAML, an unknown key, an unknown default, a
    key on plain http to a dotted host, a reasoning key set twice, a system
    reasoning line on Muse Glimmer, an unavailable DEFAULT; a non-default
    entry with its key variable unset loads as unavailable;
  * the `confidence` block: absent = stated (no body change); only the
    SCORE body gets the logprobs request / `n` + sampling extras; every
    load-time refusal (reserved keys, ranges, calibrated without points, a
    signal the method cannot produce, non-monotone points, n_param n above
    max_concurrent); GET /models reports {method, headline, calibrated} and
    never the points;
  * keys reach the request headers and NEVER GET /models;
  * the startup WARNING names a replaced env var still set;
  * precedence: criterion options.model > job model > registry default,
    seen in the URL posted to, the body's model and detail.model;
  * each submit refusal (unknown, unavailable, text-only + boxes /
    references / cv fallback / auto, a photo with no OCR, /references
    without vision);
  * per-model concurrency limits;
  * a text-only model is sent no image, and fails a page with no text;
  * (Postgres) model_id and api_url recorded per model, by_model grouping
    and the GET /usage model filter.
"""

from __future__ import annotations

import asyncio
import json
import logging

import numpy as np
import pytest
from fastapi import HTTPException

import analysis
from api.schemas import AssessRequest, CriterionInput
from llm import client as llm_client
from llm import models as llm_models
from llm.models import RegistryError, get_registry, load_registry, parse_registry
from llm.prompts import build_llm_prompt

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entry(url: str, served: str, **over) -> dict:
    entry = {
        "description": f"test model {served}",
        "url": url,
        "served_name": served,
        "vision": True,
        "max_images_per_prompt": 3,
        "reasoning": {"style": "none"},
        "max_tokens": {"score": 100, "box": 50, "select": 60, "describe": 70},
        "timeout_s": 5,
        "max_concurrent": 2,
        "text_char_budget": 1000,
    }
    entry.update(over)
    return entry


def _data(**models) -> dict:
    return {"default": next(iter(models)), "models": models}


THREE = _data(
    gpu=_entry("http://gpu:8000/v1/chat/completions", "gpu-model"),
    other=_entry("http://other:8000/v1/chat/completions", "other-model"),
    cpu=_entry("http://cpu:8080/v1/chat/completions", "cpu-model",
               vision=False, max_images_per_prompt=0, text_char_budget=50),
)


@pytest.fixture
def registry():
    """THREE swapped in as the process registry (and the file's back after)."""
    reg = parse_registry(THREE, env={})
    previous = llm_models.use_registry(reg)
    yield reg
    llm_models.use_registry(previous)


class Transport:
    """A scripted ``_send``: records (model id, url, body, headers) per call."""

    def __init__(self, delay: float = 0.0, body: dict | None = None):
        self.delay = delay
        self.body = body or {"score": 9, "verdict": "PASS", "confidence": 80, "reason": "ok"}
        self.calls: list[tuple[str, str, dict, dict]] = []
        self.in_flight: dict[str, int] = {}
        self.peak: dict[str, int] = {}

    async def __call__(self, prompt):
        spec = llm_client._target.get()
        self.calls.append((spec.id, spec.url, prompt, spec.headers()))
        self.in_flight[spec.id] = self.in_flight.get(spec.id, 0) + 1
        self.peak[spec.id] = max(self.peak.get(spec.id, 0), self.in_flight[spec.id])
        try:
            await asyncio.sleep(self.delay)
            return {"model": prompt.get("model"),
                    "choices": [{"message": {"content": json.dumps(self.body)},
                                 "finish_reason": "stop"}]}
        finally:
            self.in_flight[spec.id] -= 1


def _png(width=300, height=200) -> bytes:
    import cv2

    return cv2.imencode(".png", np.full((height, width, 3), 170, dtype=np.uint8))[1].tobytes()


def _pdf_with_text(text: str = "Total due $5 on the account") -> bytes:
    import fitz

    doc = fitz.open()
    page = doc.new_page(width=400, height=300)
    page.insert_text((40, 80), text, fontsize=14)
    return doc.tobytes()


def _analyze(raw: bytes, name: str, criteria, **kw) -> dict:
    doc = analysis.load_document_bytes(raw, name, None, keep_source=True)
    return asyncio.run(analysis.analyze_document(doc, criteria, **kw))


def _entries(result):
    return result["assessment"]["per_criterion_scores"]


def _refusal(body: dict) -> str:
    with pytest.raises(Exception) as info:
        AssessRequest.model_validate(body)
    return str(info.value)


TXT = {"type": "text", "data": "Total due $5"}

# ---------------------------------------------------------------------------
# The checked-in file
# ---------------------------------------------------------------------------


def test_the_checked_in_registry_loads():
    reg = load_registry()
    assert reg.default_id == "muse-glimmer"
    muse = reg.get("muse-glimmer")
    assert (muse.url, muse.served_name) == (
        "http://muse-glimmer:8000/v1/chat/completions", "muse-glimmer")
    assert muse.vision and muse.max_images_per_prompt == 3
    assert muse.reasoning == {"style": "chat_template_kwargs", "key": "reasoning_strength",
                              "value": "low"}
    assert muse.reasoning_line is None
    assert muse.max_tokens == {"score": 8192, "box": 1024, "select": 2048, "describe": 1024}
    assert (muse.timeout_s, muse.max_concurrent, muse.text_char_budget) == (240, 6, 60000)
    assert muse.json_mode and muse.extra_body == {}
    qwen = reg.get("qwen2.5-vl")
    assert qwen.served_name == "Qwen/Qwen2.5-VL-7B-Instruct"
    assert qwen.max_images_per_prompt == 1 and not qwen.supports_references
    assert qwen.reasoning == {"style": "none"}
    # Its whole budget fits vllm-qwen-vl's --max-model-len 8192.
    assert max(qwen.max_tokens.values()) <= 2048


def test_the_default_body_carries_reasoning_as_a_template_variable():
    default = get_registry().default
    body = default.body([{"role": "user", "content": "x"}], "box")
    assert list(body) == ["model", "messages", "max_tokens", "response_format",
                          "chat_template_kwargs"]
    assert body["chat_template_kwargs"] == {"reasoning_strength": "low"}
    # The SCORE body alone also asks for the logprobs its confidence is
    # measured from (models.yaml confidence: {method: logprobs}).
    score = default.body([{"role": "user", "content": "x"}], "score")
    assert list(score) == ["model", "messages", "max_tokens", "response_format",
                           "logprobs", "top_logprobs", "chat_template_kwargs"]
    assert (score["logprobs"], score["top_logprobs"]) == (True, 20)


def test_the_checked_in_confidence_methods():
    reg = load_registry()
    methods = {mid: spec.confidence.method for mid, spec in reg.models.items()}
    assert methods == {"muse-glimmer": "logprobs", "qwen2.5-vl": "stated",
                       "qwen3.5-4b": "logprobs", "qwen3.8-solo": "logprobs"}
    # Shadow: no entry is calibrated yet, so every headline is the stated number.
    assert all(s.confidence.headline == "stated" and s.confidence.calibration is None
               for s in reg.models.values())


# ---------------------------------------------------------------------------
# The confidence block
# ---------------------------------------------------------------------------


def test_no_confidence_block_is_stated_and_changes_no_body():
    reg = parse_registry(_data(a=_entry("http://a:1/x", "a")), env={})
    spec = reg.default
    assert spec.confidence.method == "stated" and spec.confidence.headline == "stated"
    assert spec.confidence.slots == 1
    for kind in ("score", "box", "select", "describe"):
        body = spec.body([], kind)
        assert set(body) == {"model", "messages", "max_tokens", "response_format"}
    assert spec.public()["confidence"] == {"method": "stated", "headline": "stated",
                                           "calibrated": False}


def test_only_the_score_body_gets_the_confidence_request():
    reg = parse_registry(_data(
        lp=_entry("http://lp:1/x", "lp", extra_body={"temperature": 0.7},
                  confidence={"method": "logprobs",
                              "logprobs": {"request": {"logprobs": True, "top_logprobs": 5}}}),
        nn=_entry("http://nn:1/x", "nn", extra_body={"temperature": 0.7}, max_concurrent=4,
                  confidence={"method": "sampling",
                              "sampling": {"n": 4, "extra_body": {"temperature": 1.0}}}),
        rp=_entry("http://rp:1/x", "rp", confidence={"method": "sampling",
                                                     "sampling": {"n": 3, "via": "repeat"}}),
    ), env={})
    lp, nn, rp = reg.get("lp"), reg.get("nn"), reg.get("rp")
    assert lp.body([], "score")["top_logprobs"] == 5
    assert "logprobs" not in lp.body([], "box") and "logprobs" not in lp.body([], "describe")
    score = nn.body([], "score")
    # Sampling's extra_body wins over the entry's for the score call alone.
    assert (score["n"], score["temperature"]) == (4, 1.0)
    assert "n" not in nn.body([], "select") and nn.body([], "select")["temperature"] == 0.7
    assert nn.confidence.slots == 4
    # via repeat: n requests of the plain body, one slot each.
    assert "n" not in rp.body([], "score") and rp.confidence.slots == 1
    assert rp.max_concurrent == 2  # below n is fine for repeat


_CAL = {"signal": "verdict_probability", "points": [[0.2, 30], [0.9, 85]]}


@pytest.mark.parametrize("confidence, needle", [
    ({"method": "bayes"}, "method"),
    ({"method": "logprobs", "colour": "red"}, "Extra inputs"),
    ({"logprobs": {"request": {"logprobs": True, "max_tokens": 9}}}, "may not set ['max_tokens']"),
    ({"logprobs": {"request": {"n": 2}}}, "may not set ['n']"),
    ({"logprobs": {"request": {"chat_template_kwargs": {}}}}, "chat_template_kwargs"),
    ({"logprobs": {"min_coverage": 1.5}}, "min_coverage"),
    ({"logprobs": {"number_tokens": "bytes"}}, "number_tokens"),
    ({"sampling": {"n": 1}}, "sampling.n"),
    ({"sampling": {"extra_body": {"model": "x"}}}, "may not set ['model']"),
    ({"method": "sampling", "sampling": {"n": 5}}, "max_concurrent 2"),
    ({"headline": "calibrated"}, "needs confidence.calibration"),
    ({"method": "stated", "calibration": _CAL}, "not one method 'stated' produces"),
    ({"method": "logprobs", "calibration": {**_CAL, "signal": "agreement"}},
     "not one method 'logprobs' produces"),
    ({"method": "logprobs", "calibration": {**_CAL, "points": [[0.5, 60], [0.4, 70]]}},
     "non-decreasing"),
    ({"method": "logprobs", "calibration": {**_CAL, "points": [[0.5, 60], [0.6, 50]]}},
     "non-decreasing"),
    ({"method": "logprobs", "calibration": {**_CAL, "points": [[1.5, 60]]}}, "x in [0, 1]"),
    ({"method": "logprobs", "calibration": {**_CAL, "points": [[0.5, 160]]}}, "y in [0, 100]"),
    ({"method": "logprobs", "calibration": {**_CAL, "points": []}}, "points"),
])
def test_invalid_confidence_blocks_refuse_to_load(confidence, needle):
    with pytest.raises(RegistryError) as info:
        parse_registry(_data(a=_entry("http://a:1/x", "a", confidence=confidence)), env={})
    assert needle in str(info.value)


def test_a_calibrated_entry_loads_and_get_models_says_so():
    from api.introspection import list_models

    reg = parse_registry(_data(a=_entry("http://a:1/x", "a", confidence={
        "method": "logprobs", "headline": "calibrated",
        "calibration": {**_CAL, "n": 150, "fitted_on": "2026-10-23", "ground_truth": "deliberate"},
    })), env={})
    spec = reg.default
    assert spec.confidence.calibration.points == [(0.2, 30.0), (0.9, 85.0)]
    previous = llm_models.use_registry(reg)
    try:
        payload = json.loads(list_models().body.decode("utf-8"))
    finally:
        llm_models.use_registry(previous)
    block = payload["models"][0]["confidence"]
    assert block == {"method": "logprobs", "headline": "calibrated", "calibrated": True}
    assert "points" not in json.dumps(payload) and "top_logprobs" not in json.dumps(payload)


# ---------------------------------------------------------------------------
# Loading and validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("data, needle", [
    ({"default": "nope", "models": {"a": _entry("http://a:1/v1/chat/completions", "a")}},
     "default 'nope' is not one of the models"),
    ({"default": "a", "models": {"a": {**_entry("http://a:1/x", "a"), "colour": "red"}}},
     "Extra inputs"),
    ({"default": "a", "models": {"a": _entry("ftp://a/x", "a")}}, "http:// or https://"),
    ({"default": "a", "models": {"a": _entry("http://a:1/x", "a",
                                             extra_body={"max_tokens": 5})}},
     "extra_body may not set"),
    ({"default": "a", "models": {"a": _entry(
        "http://a:1/x", "a",
        reasoning={"style": "chat_template_kwargs", "key": "reasoning_effort", "value": "low"},
        extra_body={"chat_template_kwargs": {"reasoning_effort": "high"}})}},
     "set twice"),
    ({"default": "a", "models": {"a": _entry(
        "http://a:1/x", "a", reasoning={"style": "system_line", "value": "Think hard."},
        extra_body={"chat_template_kwargs": {"reasoning_strength": "high"}})}},
     "one way, not both"),
    ({"default": "a", "models": {"a": _entry("http://a:1/x", "a", vision=False,
                                             max_images_per_prompt=2)}},
     "takes 0 images"),
    ({"default": "a", "models": {"bad id!": _entry("http://a:1/x", "a")}}, "model ids"),
])
def test_invalid_registries_refuse_to_load(data, needle):
    with pytest.raises(RegistryError) as info:
        parse_registry(data, env={})
    assert needle in str(info.value)


def test_invalid_yaml_and_a_missing_file_refuse_to_load(tmp_path):
    bad = tmp_path / "models.yaml"
    bad.write_text("default: [unclosed\n", encoding="utf-8")
    with pytest.raises(RegistryError, match="not valid YAML"):
        load_registry(bad)
    with pytest.raises(RegistryError, match="cannot read"):
        load_registry(tmp_path / "absent.yaml")


def test_a_system_reasoning_line_on_muse_glimmer_is_refused():
    data = _data(m=_entry("http://muse-glimmer:8000/x", "muse-glimmer",
                          reasoning={"style": "system_line", "value": "Reasoning strength: low"}))
    with pytest.raises(RegistryError, match="overrides that variable"):
        parse_registry(data, env={})


def test_a_system_line_model_gets_its_line_and_no_kwargs():
    reg = parse_registry(_data(m=_entry(
        "http://m:1/x", "plain", reasoning={"style": "system_line", "value": "Think briefly."})),
        env={})
    prompt = build_llm_prompt("ZmFrZQ==", "has a house", model=reg.default)
    assert prompt["messages"][0]["content"].endswith("\nThink briefly.")
    assert "chat_template_kwargs" not in prompt


def test_a_key_on_plain_http_to_a_dotted_host_is_refused():
    for url in ("http://api.example.com/v1/chat/completions", "http://192.168.5.240:8000/x"):
        data = _data(a=_entry("http://a:1/x", "a"), b=_entry(url, "b", api_key_env="B_KEY"))
        with pytest.raises(RegistryError, match="clear text"):
            parse_registry(data, env={"B_KEY": "secret"})
    # A service name on the Docker network, or https, is fine.
    parse_registry(_data(a=_entry("http://svc:1/x", "a", api_key_env="K")), env={"K": "k"})
    parse_registry(_data(a=_entry("https://api.example.com/x", "a", api_key_env="K")),
                   env={"K": "k"})


def test_an_unset_key_makes_an_entry_unavailable_not_fatal():
    data = _data(a=_entry("http://a:1/x", "a"),
                 ext=_entry("https://api.example.com/v1/chat/completions", "ext",
                            api_key_env="EXT_KEY"))
    reg = parse_registry(data, env={"EXT_KEY": "  "})
    ext = reg.get("ext")
    assert not ext.available and "EXT_KEY" in ext.unavailable_reason
    assert ext.headers() == {}
    with pytest.raises(ValueError, match="unavailable"):
        reg.check_selectable("ext")
    with pytest.raises(ValueError, match="unknown model 'zzz'.*'a', 'ext'"):
        reg.check_selectable("zzz")


def test_an_unavailable_default_stops_startup():
    data = _data(ext=_entry("https://api.example.com/x", "ext", api_key_env="EXT_KEY"))
    with pytest.raises(RegistryError, match="default model 'ext' is unavailable"):
        parse_registry(data, env={})


def test_keys_reach_the_headers_and_never_get_models():
    from api.introspection import list_models

    secret = "sk-very-secret-value-123"
    data = _data(a=_entry("http://a:1/x", "a"),
                 bearer=_entry("https://api.example.com/x", "b", api_key_env="BEARER_KEY"),
                 raw=_entry("https://raw.example.com/x", "r", api_key_env="RAW_KEY",
                            api_key_header="x-api-key"))
    reg = parse_registry(data, env={"BEARER_KEY": secret, "RAW_KEY": secret})
    assert reg.get("bearer").headers() == {"Authorization": f"Bearer {secret}"}
    assert reg.get("raw").headers() == {"x-api-key": secret}
    assert secret not in repr(reg.get("bearer"))

    previous = llm_models.use_registry(reg)
    try:
        body = list_models().body.decode("utf-8")
    finally:
        llm_models.use_registry(previous)
    assert secret not in body
    payload = json.loads(body)
    by_id = {m["id"]: m for m in payload["models"]}
    assert payload["default"] == "a"
    assert by_id["bearer"]["authenticated"] is True and by_id["a"]["authenticated"] is False
    assert by_id["raw"]["endpoint"] == {"scheme": "https", "host": "raw.example.com",
                                        "port": None}
    assert set(by_id["a"]["supports"]) == {"boxes", "references", "contrastive_references",
                                           "text_only_scoring"}


def test_the_startup_warning_names_removed_env_vars(monkeypatch, caplog):
    monkeypatch.setenv("VISION_LLM_API", "http://old:8000")
    monkeypatch.setenv("CLASSIFIER_MAX_LLM_CALLS", "4")
    with caplog.at_level(logging.WARNING, logger="classifier"):
        llm_models.startup_check()
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "VISION_LLM_API" in warnings[0] and "CLASSIFIER_MAX_LLM_CALLS" in warnings[0]


# ---------------------------------------------------------------------------
# Precedence: criterion > job > default
# ---------------------------------------------------------------------------


def test_precedence_criterion_then_job_then_default(registry, monkeypatch):
    fake = Transport()
    monkeypatch.setattr(llm_client, "_send", fake)
    criteria = [
        CriterionInput(name="plain", type="llm"),
        CriterionInput(name="own", type="llm", options={"model": "other"}),
    ]

    result = _analyze(_png(), "p.png", criteria)
    entries = _entries(result)
    assert entries["plain"]["detail"]["model"] == "gpu"      # the registry default
    assert entries["own"]["detail"]["model"] == "other"      # its options.model
    assert result["request"]["model"] == "gpu"
    def sent(name):
        (call,) = [c for c in fake.calls if f"CRITERION: {name}\\n" in json.dumps(c[2])]
        return call[0], call[1], call[2]["model"]

    assert sent("plain") == ("gpu", "http://gpu:8000/v1/chat/completions", "gpu-model")
    assert sent("own") == ("other", "http://other:8000/v1/chat/completions", "other-model")

    fake.calls.clear()
    result = _analyze(_png(), "p.png", criteria, default_model="other")
    entries = _entries(result)
    assert entries["plain"]["detail"]["model"] == "other"    # the job's model
    assert result["request"]["model"] == "other"
    assert {mid for mid, *_ in fake.calls} == {"other"}


def test_the_cv_llm_fallback_uses_the_cv_criterions_model(registry, monkeypatch):
    fake = Transport()
    monkeypatch.setattr(llm_client, "_send", fake)
    c = CriterionInput(name="has a bicycle", type="cv",
                       options={"fallback": "llm", "model": "other"})
    result = _analyze(_png(), "p.png", [c])
    entry = _entries(result)["has a bicycle"]
    assert entry["method"] == "llm" and entry["detail"]["model"] == "other"
    assert [mid for mid, *_ in fake.calls] == ["other"]


def test_a_job_payload_model_reaches_the_pipeline(registry, monkeypatch):
    import base64

    from jobs import runners
    from jobs.payloads import PAYLOAD_SCHEMA

    fake = Transport()
    monkeypatch.setattr(llm_client, "_send", fake)
    raw = _png()
    payload = {
        "schema": PAYLOAD_SCHEMA,
        "documents": [{"file_b64": base64.b64encode(raw).decode(), "filename": "p.png",
                       "content_type": "image/png", "kind": "image", "pages": 1}],
        "criteria": [{"name": "q", "type": "llm"}],
        "job_id": None,
        "model": "other",
    }
    result = asyncio.run(runners.run_assess(payload))
    assert _entries(result)["q"]["detail"]["model"] == "other"


def test_the_payload_carries_model_only_when_given(registry):
    from jobs.payloads import SubmittedDocument, build_assess_payload

    req = AssessRequest.model_validate({"documents": [TXT]})
    doc = SubmittedDocument(raw=b"x", filename="t.txt", content_type=None, kind="txt", pages=1)
    assert "model" not in build_assess_payload(req, [doc])
    assert build_assess_payload(req, [doc], model="other")["model"] == "other"


# ---------------------------------------------------------------------------
# Submit refusals
# ---------------------------------------------------------------------------


def test_unknown_and_unavailable_models_are_refused(monkeypatch):
    data = _data(a=_entry("http://a:1/x", "a"),
                 ext=_entry("https://api.example.com/x", "ext", api_key_env="EXT_KEY"))
    previous = llm_models.use_registry(parse_registry(data, env={}))
    try:
        assert "unknown model 'nope'" in _refusal({"documents": [TXT], "model": "nope"})
        assert "valid models: ['a', 'ext']" in _refusal({"documents": [TXT], "model": "nope"})
        assert "is unavailable" in _refusal({"documents": [TXT], "model": "ext"})
        assert "unknown model 'nope'" in _refusal({
            "documents": [TXT], "criteria": [{"name": "q", "options": {"model": "nope"}}]})
        assert "Extra inputs" in _refusal({
            "documents": [TXT],
            "criteria": [{"name": "q", "type": "text", "options": {"model": "a"}}]})
    finally:
        llm_models.use_registry(previous)


def test_text_only_refusals(registry):
    boxes = {"name": "has a house", "options": {"hint": "presence", "boxes": True}}
    assert "cannot locate" in _refusal({"documents": [TXT], "model": "cpu", "criteria": [boxes]})
    assert "cannot locate" in _refusal({
        "documents": [TXT],
        "criteria": [{**boxes, "options": {**boxes["options"], "model": "cpu"}}]})
    # A vision options.model on a text-only job is fine.
    AssessRequest.model_validate({
        "documents": [TXT], "model": "cpu",
        "criteria": [{**boxes, "options": {**boxes["options"], "model": "gpu"}}]})

    assert "example cannot be shown" in _refusal({
        "documents": [TXT], "model": "cpu", "criteria": [{"name": "q"}],
        "references": ["r000000000000"]})
    # ...unless that criterion opts out of the references.
    AssessRequest.model_validate({
        "documents": [TXT], "model": "cpu", "references": ["r000000000000"],
        "criteria": [{"name": "q", "options": {"reference": {"use": False}}}]})

    assert "question about the image" in _refusal({
        "documents": [TXT], "model": "cpu",
        "criteria": [{"name": "has a bicycle", "type": "cv", "options": {"fallback": "llm"}}]})
    assert "sends the page image" in _refusal({
        "documents": [TXT], "model": "cpu", "criteria": [{"name": "q"}], "references": "auto"})
    # A cv name an OpenCV detector answers takes no options.model.
    assert "options.model" in _refusal({
        "documents": [TXT],
        "criteria": [{"name": "has sky", "type": "cv", "options": {"model": "gpu"}}]})


def test_a_one_image_model_refuses_references(registry):
    data = _data(one=_entry("http://one:1/x", "one", max_images_per_prompt=1))
    previous = llm_models.use_registry(parse_registry(data, env={}))
    try:
        assert "takes 1 image per prompt" in _refusal({
            "documents": [TXT], "criteria": [{"name": "q"}], "references": ["r000000000000"]})
    finally:
        llm_models.use_registry(previous)


def test_a_photo_without_ocr_is_refused_for_a_text_only_model(registry):
    from api.assess import _check_documents, _Upload

    photo = _Upload(raw=_png(), filename="p.png", declared="image/png")
    req = AssessRequest.model_validate({"documents": [TXT], "model": "cpu",
                                        "criteria": [{"name": "q"}]})
    with pytest.raises(HTTPException) as info:
        _check_documents(req, [photo])  # the unit tests run with OCR engine "none"
    assert info.value.status_code == 400 and "text-only model" in info.value.detail
    # The same photo on a vision model is fine; a text document is fine on cpu.
    _check_documents(AssessRequest.model_validate({"documents": [TXT]}), [photo])
    text = _Upload(raw=b"Total due $5", filename="t.txt", declared=None)
    _check_documents(req, [text])


def test_references_need_a_vision_model(registry):
    from api.reference_schemas import ReferenceRequest

    body = {"document": {"type": "base64", "data": "eA=="}, "criteria": [{"name": "q"}]}
    ReferenceRequest.model_validate({**body, "model": "other"})
    for bad in ({**body, "model": "cpu"},
                {**body, "criteria": [{"name": "q", "options": {"model": "cpu"}}]}):
        with pytest.raises(Exception, match="text-only"):
            ReferenceRequest.model_validate(bad)


def test_options_model_is_not_part_of_a_criterions_identity():
    from api.criterion_options import without_model
    from references.resolve import _same

    a = CriterionInput(name="q", options={"hint": "presence"})
    b = CriterionInput(name="q", options={"hint": "presence", "model": "muse-glimmer"})
    assert "model" not in a.resolved_options() and b.resolved_options()["model"] == "muse-glimmer"
    assert without_model(a.resolved_options()) == without_model(b.resolved_options())
    assert _same(a, b)


# ---------------------------------------------------------------------------
# Per-model concurrency
# ---------------------------------------------------------------------------


def test_each_model_has_its_own_concurrency_limit(monkeypatch):
    data = _data(one=_entry("http://one:1/x", "one", max_concurrent=1),
                 two=_entry("http://two:1/x", "two", max_concurrent=2))
    reg = parse_registry(data, env={})
    fake = Transport(delay=0.03)
    monkeypatch.setattr(llm_client, "_send", fake)

    async def run():
        calls = [llm_client.call_model({"n": i}, label="score/x", model=reg.get(mid))
                 for i, mid in enumerate(["one"] * 4 + ["two"] * 4)]
        await asyncio.gather(*calls)

    asyncio.run(run())
    # A full slow model never takes the other's slots, and vice versa.
    assert fake.peak == {"one": 1, "two": 2}
    assert reg.get("one").limit.peak == 1 and reg.get("two").limit.peak == 2
    assert [c[0] for c in fake.calls].count("one") == 4


# ---------------------------------------------------------------------------
# Text-only scoring
# ---------------------------------------------------------------------------


def test_a_text_only_prompt_has_no_image_and_says_so(registry):
    cpu = registry.get("cpu")
    prompt = build_llm_prompt("ZmFrZQ==", "mentions a total", "auto", "Total due $5",
                              document_kind="pdf", model=cpu)
    user = prompt["messages"][1]["content"]
    assert [part["type"] for part in user] == ["text"]
    assert "ZmFrZQ==" not in json.dumps(prompt)
    assert user[0]["text"].startswith("You are assessing the text of a single document page")
    assert "extracted text only" in prompt["messages"][0]["content"]
    assert prompt["model"] == "cpu-model" and prompt["max_tokens"] == 100
    # The vision default on the same input still sends the image.
    assert "ZmFrZQ==" in json.dumps(build_llm_prompt("ZmFrZQ==", "x", model=registry.default))


def test_text_only_scoring_reads_the_text_and_sends_no_image(registry, monkeypatch):
    fake = Transport()
    monkeypatch.setattr(llm_client, "_send", fake)
    result = _analyze(_pdf_with_text(), "bill.pdf", [CriterionInput(name="mentions a total")],
                      default_model="cpu")
    entry = _entries(result)["mentions a total"]
    assert entry["status"] == "ok" and entry["detail"]["model"] == "cpu"
    assert entry["detail"]["image_sent"] is False
    assert entry["detail"]["text_sent"]["budget"] == 50
    ((mid, url, prompt, _),) = fake.calls
    assert mid == "cpu" and url == "http://cpu:8080/v1/chat/completions"
    assert "image_url" not in json.dumps(prompt)
    assert "Total due" in json.dumps(prompt)


def test_a_text_only_model_fails_a_page_with_no_text(registry, monkeypatch):
    fake = Transport()
    monkeypatch.setattr(llm_client, "_send", fake)
    result = _analyze(_png(), "p.png", [CriterionInput(name="q")], default_model="cpu")
    entry = _entries(result)["q"]
    assert entry["status"] == "error"
    assert "no text available for a text-only model" in entry["error"]
    assert fake.calls == []


# ---------------------------------------------------------------------------
# Recording per model (Postgres)
# ---------------------------------------------------------------------------


@pytest.mark.postgres
def test_usage_records_model_id_and_url_per_model(registry, monkeypatch):
    from db import database
    from llm import usage as llm_usage

    async def setup():
        fresh = llm_usage.UsageStore(database)
        await fresh.init()
        async with database.acquire() as conn:
            await conn.execute("TRUNCATE llm_calls")
        return fresh

    store = asyncio.run(setup())
    monkeypatch.setattr(llm_usage, "usage_store", store)
    monkeypatch.setattr(llm_client, "_send", Transport())

    async def job():
        token = llm_usage.job_id_var.set("job-models")
        try:
            await llm_client.call_model({"model": "gpu-model"}, label="score/a")
            await llm_client.call_model({"model": "other-model"}, label="score/b",
                                       model=registry.get("other"))
            await llm_client.call_model({"model": "other-model"}, label="score/c",
                                       model=registry.get("other"))
        finally:
            llm_usage.job_id_var.reset(token)

    asyncio.run(job())
    rows = asyncio.run(store.calls("job-models"))
    assert [(r["model_id"], r["api_url"]) for r in rows] == [
        ("gpu", "http://gpu:8000/v1/chat/completions"),
        ("other", "http://other:8000/v1/chat/completions"),
        ("other", "http://other:8000/v1/chat/completions"),
    ]
    totals = asyncio.run(store.totals("job-models"))
    assert [(b["model"], b["calls"]) for b in totals["by_model"]] == [("gpu", 1), ("other", 2)]
    summary = asyncio.run(store.summary(model="other"))
    assert summary["totals"]["calls"] == 2
    assert [b["model"] for b in summary["by_model"]] == ["other"]
