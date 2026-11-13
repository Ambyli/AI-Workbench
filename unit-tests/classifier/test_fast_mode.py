"""``options.mode: "fast"`` — the one-token readout for the 1-10 rating (ZT-603 phase 1).

No model server: requests are scripted at the bare transport
``llm.client._send`` (the model's limit, the label → kind mapping and the
usage row stay real). What is pinned:

  * **Nothing reasoned moved.** The reasoned scoring prompts and every call
    kind's body of every checked-in ``models.yaml`` entry hash exactly as
    they did BEFORE phase 1 (captured on the branch point, ``REASONED_PINS``);
    ``test_reference_prompts.py`` still pins the default model's.
  * **models.yaml** — the checked-in ``fast`` blocks (qwen3.8-solo: vision,
    3 images, 3 slots, top_logprobs 64 = its ``--max-logprobs``, the template's
    thinking markers; qwen3.5-4b: top 20; none on muse-glimmer / qwen2.5-vl),
    the load-time refusals, ``fast_body`` exactly (one token, no
    response_format / n / confidence keys, the reasoning key dropped,
    ``fast.extra_body`` then ``fast.chat_template_kwargs`` last) and by hash.
  * **The fast prompt** — pinned by hash (``FAST_GOLDEN``); the reasoned
    prompt's prefix (image, context line, DOCUMENT TEXT) kept, so fast
    criteria on a page share it; ``strip_markers`` out of the name and text
    (fast only); no reasoning line; the text-only sentence; the refusals.
  * **call_readout** — ONE request whatever happens (no retry on low
    coverage, a cap or a refusal), call kind ``fast``, a usage row, the slot;
    ``ReadoutIncomplete`` (not a ValueError) / ``ReadoutCapped`` (fires on a
    spread readout, quiet on a confident one with ~0 tails) and their metric;
    a server 4xx about logprobs becomes the named error, a 2xx that is not
    JSON a named ``LLMCallError``; a refusal prints its value across the
    threshold ("0.4996 < 0.5", never "0.50 < 0.5").
  * **The result** — ``rate_readout`` (verdict = the band with the most mass,
    score = the best score inside it, confidence, the reason line, the block
    with ``unseen_bound``, calibration), and end to end through
    ``analyze_document``: ``detail.mode`` only when sent, a default criterion
    byte-for-byte as before, an incomplete / capped readout failing its unit
    naming mode 'reasoned', the box loop reasoned after a fast score,
    ``score: false`` (the band line withheld from the reason too).
  * **Every named 400** — request-level ``mode`` (JSON, multipart, POST
    /references both ways), a criterion's top-level ``mode``, ``options.mode``
    on cv / text / detector, ``adaptive``, fast on a model without a fast block
    (naming where the model came from and the fast-capable ids),
    ``options.reference`` on a fast criterion, explicit fast criteria on POST
    /references.
  * **The payload round trip** (``model_dump`` → JSON → validate, as the
    payload, the runner, inheritance and ``from_job`` do).
  * **References** — a fast criterion is answered but never guided (no plan
    entry, no ``auto`` target, an empty guide with its note); identity drops
    ``model`` and ``mode``; (Postgres) ``from_job`` strips the mode and never
    copies a fast unit's answer or observation — an errored readout's too.
  * **Introspection** — GET /models ``supports.fast`` / ``fast``, GET
    /criterion-types ``mode``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import time

import httpx
import numpy as np
import pytest

import analysis
from api.schemas import AssessRequest, CriterionInput
from llm import client as llm_client
from llm import confidence as llm_confidence
from llm import models as llm_models
from llm.models import RegistryError, load_registry, parse_registry
from llm.prompts import build_llm_prompt

DIGITS = tuple("0123456789")


def _hash(obj) -> str:
    raw = json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# Registries
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


QUICK_FAST = {"chat_template_kwargs": {"enable_thinking": False}, "top_logprobs": 20}

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
    calibrated=_entry("http://cal:8000/v1/chat/completions", "cal-model",
                      fast={**QUICK_FAST, "calibration": {"rating": {
                          "temperature": 2.0, "n": 300, "fitted_on": "2026-11-12",
                          "ground_truth": "deliberate"}}}),
)


@pytest.fixture
def registry():
    reg = parse_registry(REGISTRY, env={})
    previous = llm_models.use_registry(reg)
    yield reg
    llm_models.use_registry(previous)


@pytest.fixture
def checked_in():
    """The checked-in models.yaml as the process registry."""
    reg = load_registry()
    previous = llm_models.use_registry(reg)
    yield reg
    llm_models.use_registry(previous)


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


def readout_response(top: dict[str, float], token: str | None = None) -> dict:
    """A one-token completion with its alternatives (``{token: p}``)."""
    token = token if token is not None else max(top, key=top.get)
    entry = {
        "token": token,
        "logprob": math.log(top.get(token, 1e-9)),
        "top_logprobs": [{"token": t, "logprob": math.log(p)} for t, p in top.items()],
    }
    return {
        "model": "x",
        "choices": [{"message": {"content": token}, "finish_reason": "length",
                     "logprobs": {"content": [entry]}}],
        "usage": {"prompt_tokens": 900, "completion_tokens": 1},
    }


CONFIDENT_9 = {"9": 0.90, "8": 0.04, "[9": 0.02, "7": 0.01, "5": 0.01, "<|im_end|>": 0.005,
               "1": 0.005, **{f"x{i}": 1e-6 for i in range(13)}}
LOW_COVERAGE = {"I": 0.6, "The": 0.2, "9": 0.1, "8": 0.1}
SPREAD = {"0": 0.12, "2": 0.12, "4": 0.12, "6": 0.12, "8": 0.12, "a": 0.05, "b": 0.05,
          "c": 0.05, "d": 0.05, "e": 0.05, "f": 0.05}


def is_readout(prompt: dict) -> bool:
    return prompt.get("logprobs") is True and "response_format" not in prompt and (
        prompt.get("max_tokens") == 1 or prompt.get("max_completion_tokens") == 1)


class Model:
    """A scripted ``_send``: readouts get ``top``; the reasoned scoring call,
    the box ask and the verify get JSON. Records ``(model id, prompt)``."""

    def __init__(self, top=None, *, score=9, bbox=(100, 200, 300, 400), verify=9, fail=None):
        self.top = top or CONFIDENT_9
        self.score = score
        self.bbox = list(bbox)
        self.verify = verify
        self.fail = fail
        self.calls: list[tuple[str, dict]] = []

    def readouts(self) -> list[dict]:
        return [p for _, p in self.calls if is_readout(p)]

    async def __call__(self, prompt):
        spec = llm_client._target.get()
        self.calls.append((spec.id, prompt))
        if self.fail is not None:
            raise self.fail
        if is_readout(prompt):
            return readout_response(self.top)
        system = prompt["messages"][0]["content"]
        if "You locate features" in system:
            body = {"bbox": self.bbox, "confidence": 90, "reason": "it is there"}
        elif "image crop" in system:
            body = {"score": self.verify, "reason": "it is there"}
        else:
            body = {"score": self.score, "verdict": "PASS", "confidence": 80,
                    "reason": "the model reasoned"}
        return {"choices": [{"message": {"content": json.dumps(body)},
                             "finish_reason": "stop"}]}


def _png(width=400, height=300, value=170) -> bytes:
    import cv2

    return cv2.imencode(".png", np.full((height, width, 3), value, dtype=np.uint8))[1].tobytes()


def _analyze(raw: bytes, name: str, criteria, **kw) -> dict:
    doc = analysis.load_document_bytes(raw, name, None, keep_source=True)
    return asyncio.run(analysis.analyze_document(doc, criteria, **kw))


def _entries(result):
    return result["assessment"]["per_criterion_scores"]


def _c(name="has a house", **options) -> CriterionInput:
    return CriterionInput.model_validate({"name": name, "type": "llm", "options": options})


def _refusal(body: dict) -> str:
    with pytest.raises(Exception) as info:
        AssessRequest.model_validate(body)
    return str(info.value)


TXT = {"type": "text", "data": "Total due $5"}


# ---------------------------------------------------------------------------
# Nothing reasoned moved (captured before phase 1)
# ---------------------------------------------------------------------------

# sha256 of json.dumps(..., sort_keys=True, ensure_ascii=False) on the
# checked-in models.yaml, taken BEFORE phase 1 changed any code: the reasoned
# scoring prompt per entry (qwen3.5-4b is route-policy's path) and every call
# kind's body. qwen3.8-solo's reasoned prompt for a page WITH an image changes
# on purpose (it is a vision entry now); on a .txt document it must not.
REASONED_PROMPT_PINS = [
    ("qwen3.5-4b", (None, "the user asks about a customer", "presence",
                    "USER: hello\nASSISTANT: hi"), {"document_kind": "txt"},
     "9b17725fa703c2f1b224f07a7a6f13efc783e4877a2dadb36823a7dc75800ede"),
    ("qwen3.5-4b", (None, "mentions a warranty", "auto", "Limited Warranty text"),
     {"document_kind": "txt", "text_truncated": True},
     "265091a15ca17c120ad897bf93a55e042c4eb3efe689cb7f671e4b3034010ded"),
    ("qwen3.8-solo", (None, "mentions a warranty", "auto", "Limited Warranty text"),
     {"document_kind": "txt", "text_truncated": True},
     "0dc89122e64994df1136abe59d470d89be3b376607a7c1f026810f24d75eb71a"),
    ("muse-glimmer", ("ZmFrZQ==", "has a house"), {},
     "b17bed897e20abe65816c1c3d46d9445c22ea1805d84752549b1b38b58117965"),
    # A page image handed to a text-only entry: the third branch of the
    # reasoned source sentence ("… say so and score it 5"), moved into
    # prompts._source_sentence by phase 1. Hash taken on master.
    ("qwen3.5-4b", ("ZmFrZQ==", "has a house", "presence", "Total due $5"),
     {"document_kind": "pdf"},
     "32384b7c7213807e7a74460f142fb7224e5423e9c6059033cb881440f2d8a4a6"),
]
REASONED_BODY_PINS = {
    ("qwen3.5-4b", "score"): "afbfdca2d908b72a98e394e9cf6df678faafce4e1be1bdba8d9473ee5a150f4b",
    ("qwen3.5-4b", "box"): "b3f5f88afeae24d39f82d006890a9d4a0ce615bb82208d3cc6c6d7702f99154a",
    ("qwen3.5-4b", "select"): "b3f5f88afeae24d39f82d006890a9d4a0ce615bb82208d3cc6c6d7702f99154a",
    ("qwen3.5-4b", "describe"): "b3f5f88afeae24d39f82d006890a9d4a0ce615bb82208d3cc6c6d7702f99154a",
    ("qwen3.8-solo", "score"): "e8f80d0f6b9ee8e19f7e1afaa766d1f1de0089cf815181426be714020f05ae72",
    ("qwen3.8-solo", "box"): "b34580d9380b634f298d6d3b9034b6f19f53e98e987c0b9a733bfad08713ffcf",
    ("qwen3.8-solo", "select"): "05fe927adb96ce4157b64987b247ce800ac291e2619beba3948a96df99eafa84",
    ("qwen3.8-solo", "describe"): "b34580d9380b634f298d6d3b9034b6f19f53e98e987c0b9a733bfad08713ffcf",
    ("muse-glimmer", "score"): "583cce516c9d91b932e9272b92b1a131134089047bb7fd429d36598f4a8437e3",
    ("muse-glimmer", "box"): "07c4954cabfa64e35330b059b4c49b7bdf7eec17b54ebec061ce6d8329e2ee9b",
    ("muse-glimmer", "select"): "c9de99df08de43860e4ce12652934c77bb72a746323df19dce4f1f44a97fe6af",
    ("muse-glimmer", "describe"): "07c4954cabfa64e35330b059b4c49b7bdf7eec17b54ebec061ce6d8329e2ee9b",
    ("qwen2.5-vl", "score"): "dd4441f1b17acdd6832afc5e7c048ec5f86b7766cbb24743fa37e153d4de005d",
    ("qwen2.5-vl", "box"): "6665d2cdf12f7cbc2545e50071eab22438892ad3aef31348e338ce12b6a8d035",
    ("qwen2.5-vl", "select"): "c2c00c8355a40a8daf3558d37e74368db324552f5be925c150c5ecce11df6c32",
    ("qwen2.5-vl", "describe"): "c2c00c8355a40a8daf3558d37e74368db324552f5be925c150c5ecce11df6c32",
}


@pytest.mark.parametrize("model_id, args, kwargs, digest", REASONED_PROMPT_PINS)
def test_reasoned_prompts_are_byte_identical_to_before(checked_in, model_id, args, kwargs, digest):
    spec = checked_in.get(model_id)
    assert _hash(build_llm_prompt(*args, **kwargs, model=spec)) == digest
    # style="reasoned" spelled out is the same request.
    assert _hash(build_llm_prompt(*args, **kwargs, model=spec, style="reasoned")) == digest


@pytest.mark.parametrize("key, digest", sorted(REASONED_BODY_PINS.items()))
def test_reasoned_bodies_are_byte_identical_to_before(checked_in, key, digest):
    model_id, kind = key
    body = checked_in.get(model_id).body([{"role": "user", "content": "x"}], kind)
    assert _hash(body) == digest


# ---------------------------------------------------------------------------
# models.yaml: the fast block
# ---------------------------------------------------------------------------


def test_the_checked_in_fast_blocks(checked_in):
    solo = checked_in.get("qwen3.8-solo")
    assert solo.vision and solo.max_images_per_prompt == 3 and solo.max_concurrent == 3
    assert solo.supports_references and solo.supports_contrastive and solo.supports_boxes
    assert solo.fast.top_logprobs == 64
    assert solo.fast.chat_template_kwargs == {"enable_thinking": False}
    assert solo.fast.extra_body == {"temperature": 0}
    assert (solo.fast.min_coverage, solo.fast.max_unseen_mass) == (0.5, 0.02)
    assert solo.fast.calibration == {}
    # Every marker the shared template turns thinking back ON for.
    template = (llm_models.DEFAULT_PATH.parents[2] / "ai" / "jinja"
                / "qwen_fixed_chat_template.jinja").read_text(encoding="utf-8")
    import re
    switch_on = set(re.findall(r"'(<\|think_(?!off)[a-z]+\|>)' in msg\.content", template))
    assert switch_on and switch_on == set(solo.fast.strip_markers)
    small = checked_in.get("qwen3.5-4b")
    assert small.fast.top_logprobs == 20 and not small.vision
    assert small.fast.chat_template_kwargs == {"enable_thinking": False}
    assert checked_in.get("muse-glimmer").fast is None
    assert checked_in.get("qwen2.5-vl").fast is None
    assert checked_in.fast_ids() == ["qwen3.5-4b", "qwen3.8-solo"]


def test_qwen38_solo_top_logprobs_equals_its_max_logprobs():
    """The coupling: fast.top_logprobs <= the vLLM server's --max-logprobs."""
    import pathlib
    import re

    import yaml

    compose = pathlib.Path(llm_models.DEFAULT_PATH).parents[1] / "vllm" / "docker-compose.vllm.yml"
    command = yaml.safe_load(compose.read_text(encoding="utf-8"))["services"]["qwen3.8-solo"]["command"]
    flag = int(re.search(r"--max-logprobs (\d+)", command).group(1))
    assert load_registry().get("qwen3.8-solo").fast.top_logprobs <= flag == 64


@pytest.mark.parametrize("fast, needle", [
    ({"top_logprobs": 10}, "top_logprobs"),
    ({"extra_body": {"max_tokens": 5}}, "fast.extra_body may not set ['max_tokens']"),
    ({"extra_body": {"logprobs": False}}, "may not set ['logprobs']"),
    ({"extra_body": {"top_logprobs": 5}}, "may not set ['top_logprobs']"),
    ({"extra_body": {"chat_template_kwargs": {}}}, "may not set ['chat_template_kwargs']"),
    ({"extra_body": {"n": 2}}, "may not set ['n']"),
    ({"extra_body": {"response_format": {}}}, "may not set ['response_format']"),
    ({"extra_body": {"stream": True}}, "may not set ['stream']"),
    ({"min_coverage": 1.5}, "min_coverage"),
    ({"max_unseen_mass": -0.1}, "max_unseen_mass"),
    # 0 would cap every readout, even one listing all ten digits (estimate 0.0).
    ({"max_unseen_mass": 0.0}, "max_unseen_mass"),
    ({"strip_markers": [""]}, "strip_markers"),
    ({"calibration": {"yesno": {"temperature": 1.0}}}, "calibration"),
    ({"calibration": {"rating": {"temperature": 0}}}, "temperature"),
    ({"colour": "red"}, "Extra inputs"),
])
def test_invalid_fast_blocks_refuse_to_load(fast, needle):
    with pytest.raises(RegistryError) as info:
        parse_registry(_data(a=_entry("http://a:1/x", "a", fast=fast)), env={})
    assert needle in str(info.value)


def test_fast_body_exactly():
    reg = parse_registry(_data(q=_entry(
        "http://q:1/x", "q-model", max_concurrent=2,
        reasoning={"style": "chat_template_kwargs", "key": "reasoning_effort", "value": "low"},
        extra_body={"temperature": 0.7, "top_p": 0.9, "n": 2,
                    "chat_template_kwargs": {"keep": 1}},
        confidence={"method": "sampling", "sampling": {"n": 2}},
        fast={"chat_template_kwargs": {"enable_thinking": False, "keep": 2}, "top_logprobs": 32},
    )), env={})
    spec = reg.default
    messages = [{"role": "user", "content": "x"}]
    body = spec.fast_body(messages)
    assert body == {
        "model": "q-model", "messages": messages, "max_tokens": 1,
        "temperature": 0, "top_p": 0.9, "logprobs": True, "top_logprobs": 32,
        # the entry's own kwargs, its reasoning key DROPPED, fast's merged last
        "chat_template_kwargs": {"keep": 2, "enable_thinking": False},
    }
    assert list(body) == ["model", "messages", "max_tokens", "temperature", "top_p",
                          "logprobs", "top_logprobs", "chat_template_kwargs"]
    # The reasoned score body is untouched by the fast block.
    score = spec.body(messages, "score")
    assert score["chat_template_kwargs"] == {"keep": 1, "reasoning_effort": "low"}
    assert score["n"] == 2 and score["response_format"] == {"type": "json_object"}


def test_fast_body_on_a_token_param_entry_and_without_a_block():
    reg = parse_registry(_data(
        o=_entry("http://o:1/x", "o", token_param="max_completion_tokens", json_mode=False,
                 fast={}),
        n=_entry("http://n:1/x", "n"),
    ), env={})
    body = reg.get("o").fast_body([])
    assert body["max_completion_tokens"] == 1 and "max_tokens" not in body
    assert body["top_logprobs"] == 20 and body["temperature"] == 0
    assert "chat_template_kwargs" not in body
    with pytest.raises(ValueError, match="no fast block"):
        reg.get("n").fast_body([])


def test_the_checked_in_fast_bodies_are_pinned(checked_in):
    messages = [{"role": "user", "content": "x"}]
    solo = checked_in.get("qwen3.8-solo").fast_body(messages)
    assert solo == {"model": "qwen3.8-solo", "messages": messages, "max_tokens": 1,
                    "logprobs": True, "top_logprobs": 64, "temperature": 0,
                    "chat_template_kwargs": {"enable_thinking": False}}
    small = checked_in.get("qwen3.5-4b").fast_body(messages)
    assert small["top_logprobs"] == 20
    assert small["chat_template_kwargs"] == {"enable_thinking": False}


# ---------------------------------------------------------------------------
# The fast prompt
# ---------------------------------------------------------------------------

# sha256 of json.dumps(build_llm_prompt(..., style="fast"), sort_keys=True,
# ensure_ascii=False) on the checked-in models.yaml, pinned when phase 1 added
# it. The wording is config.py § Prompt wording (FAST_*); regenerate on
# purpose, and say so.
FAST_GOLDEN = [
    ("qwen3.8-solo", ("ZmFrZQ==", "has a house"), {},
     "9379b405b0e06c5f47f1c43e878b817132b3f56315b0281f9a765ae2206cd957"),
    ("qwen3.8-solo", ("ZmFrZQ==", "has a house", "presence", "Total due $5"),
     {"document_kind": "pdf"},
     "dcd2869efb04c3c7140d295cd525dc44e48cf60be147d133691bb8508043cd00"),
    ("qwen3.8-solo", (None, "mentions a warranty", "auto", "Limited Warranty text"),
     {"document_kind": "txt", "text_truncated": True},
     "2460deb9f31c41f39f68ff9b00825eb5de4012bde1e52a2379a58302b0574ba3"),
    ("qwen3.8-solo", ("ZmFrZQ==", "image sharpness", "quality"), {},
     "4b37531f60bc3afab5e26fe32be5d4803c9d85f4c3af7ae6c43efef770a14f73"),
    ("qwen3.5-4b", ("ZmFrZQ==", "has a house", "presence", "Total due $5"),
     {"document_kind": "pdf"},
     "4f2de1ccccd047bf92e61eb6f9c9e7438e568726aec4084f4333799b9750c2ea"),
]


@pytest.mark.parametrize("model_id, args, kwargs, digest", FAST_GOLDEN)
def test_the_fast_prompt_is_pinned(checked_in, model_id, args, kwargs, digest):
    prompt = build_llm_prompt(*args, **kwargs, model=checked_in.get(model_id), style="fast")
    assert _hash(prompt) == digest


def _user_text(prompt: dict) -> str:
    return next(p["text"] for p in prompt["messages"][1]["content"] if p["type"] == "text")


def test_the_fast_prompt_keeps_the_reasoned_prefix_and_asks_for_one_digit(registry):
    from config import (
        DOCUMENT_TEXT_HEADING, FAST_INSTRUCTION, FAST_RUBRICS, FAST_SYSTEM_LINE, HINT_RUBRICS,
    )

    spec = registry.get("quick")
    args = ("ZmFrZQ==", "has a house", "presence", "Total due $5")
    reasoned = build_llm_prompt(*args, document_kind="pdf", model=spec)
    fast = build_llm_prompt(*args, document_kind="pdf", model=spec, style="fast")
    # The image part is the same, and the text runs identically through the
    # DOCUMENT TEXT block — the per-item prefix.
    assert fast["messages"][1]["content"][0] == reasoned["messages"][1]["content"][0]
    prefix = _user_text(reasoned).split(HINT_RUBRICS["presence"]["heading"])[0]
    assert DOCUMENT_TEXT_HEADING in prefix and _user_text(fast).startswith(prefix)
    text = _user_text(fast)
    assert FAST_RUBRICS["presence"]["heading"] in text
    assert "CRITERION: has a house" in text and text.endswith(FAST_INSTRUCTION)
    assert "JSON" not in text and "score" not in text.split("CRITERION:")[1]
    system = fast["messages"][0]["content"]
    assert system.startswith("You are a document assessment expert. ")
    assert system.endswith(FAST_SYSTEM_LINE) and "JSON" not in system
    # The body: one token, logprobs, no response_format, reasoning off.
    assert fast["max_tokens"] == 1 and fast["top_logprobs"] == 20 and fast["logprobs"] is True
    assert "response_format" not in fast
    assert fast["chat_template_kwargs"] == {"enable_thinking": False}


def test_fast_criteria_on_one_page_share_a_prefix_through_the_document_text(registry):
    spec = registry.get("quick")
    a = json.dumps(build_llm_prompt("ZmFrZQ==", "has a house", "presence", "Total due $5",
                                    document_kind="pdf", model=spec, style="fast"))
    b = json.dumps(build_llm_prompt("ZmFrZQ==", "has a roof", "quality", "Total due $5",
                                    document_kind="pdf", model=spec, style="fast"))
    shared = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
    assert "Total due $5" in a[:shared] and "---" in a[:shared]


def test_markers_are_stripped_from_the_fast_prompt_only(checked_in):
    solo = checked_in.get("qwen3.8-solo")
    name = "has a house <|think_on|>"
    text = "Total <|think_high|>due <|think<|think_low|>_on|>$5"
    fast = build_llm_prompt("ZmFrZQ==", name, "presence", text, document_kind="pdf",
                            model=solo, style="fast")
    dumped = json.dumps(fast)
    assert "<|think" not in dumped
    assert "CRITERION: has a house \n" in _user_text(fast)
    assert "Total due $5" in _user_text(fast)
    reasoned = json.dumps(build_llm_prompt("ZmFrZQ==", name, "presence", text,
                                           document_kind="pdf", model=solo))
    assert "<|think_on|>" in reasoned and "<|think_high|>" in reasoned


def test_a_system_line_model_gets_no_reasoning_line_in_a_fast_prompt():
    reg = parse_registry(_data(s=_entry("http://s:1/x", "s",
                                        reasoning={"style": "system_line", "value": "Think hard."},
                                        fast={})), env={})
    spec = reg.default
    assert "Think hard." in build_llm_prompt("ZmFrZQ==", "x", model=spec)["messages"][0]["content"]
    assert "Think hard." not in json.dumps(build_llm_prompt("ZmFrZQ==", "x", model=spec,
                                                            style="fast"))


def test_a_text_only_fast_prompt_answers_4_when_it_cannot_judge(registry):
    from config import FAST_TEXT_ONLY_SENTENCE

    prompt = build_llm_prompt("ZmFrZQ==", "has a house", "presence", "Total due $5",
                              document_kind="pdf", model=registry.get("quicktext"), style="fast")
    assert prompt["messages"][0]["content"].startswith(
        "You are a document assessment expert. " + FAST_TEXT_ONLY_SENTENCE)
    assert all(p["type"] == "text" for p in prompt["messages"][1]["content"])


def test_fast_prompt_refusals(registry):
    from llm.prompts import ReferenceExample

    example = ReferenceExample(criterion="x", verdict="PASS", score=9, reason="", image_b64="AA")
    with pytest.raises(ValueError, match="never reference-guided"):
        build_llm_prompt("ZmFrZQ==", "x", model=registry.get("quick"), style="fast",
                         references=[example])
    with pytest.raises(ValueError, match="no fast block"):
        build_llm_prompt("ZmFrZQ==", "x", model=registry.get("slow"), style="fast")
    with pytest.raises(ValueError, match="style"):
        build_llm_prompt("ZmFrZQ==", "x", model=registry.get("quick"), style="adaptive")


# ---------------------------------------------------------------------------
# call_readout
# ---------------------------------------------------------------------------


def _readout_call(spec, *, label="fast/has a house"):
    prompt = build_llm_prompt("ZmFrZQ==", "has a house", model=spec, style="fast")
    return asyncio.run(llm_client.call_readout(prompt, labels=DIGITS, label=label, model=spec))


def _metric(model: str, reason: str) -> float:
    return llm_client.readout_incomplete.labels(model=model, kind="fast", reason=reason)._value.get()


def test_the_fast_label_is_its_own_call_kind():
    assert llm_client.call_kind("fast/has a house") == "fast"
    assert "fast" in llm_client.CALL_KINDS
    assert llm_client.call_kind("score/fast/x") == "score"


def test_a_confident_readout_is_one_request(registry, monkeypatch):
    model = Model()
    monkeypatch.setattr(llm_client, "_send", model)
    rows = []

    class Store:
        async def record(self, row):
            rows.append(row)

    from llm import usage as llm_usage

    monkeypatch.setattr(llm_usage, "usage_store", Store())
    readout = _readout_call(registry.get("quick"))
    assert readout.best() == "9" and readout.coverage == pytest.approx(0.985)
    assert len(model.calls) == 1 and model.calls[0][0] == "quick"
    assert [(r["kind"], r["label"], r["max_tokens"]) for r in rows] == [
        ("fast", "fast/has a house", 1)]


def test_low_coverage_is_one_request_and_never_a_value_error(registry, monkeypatch):
    model = Model(LOW_COVERAGE)
    monkeypatch.setattr(llm_client, "_send", model)
    before = _metric("quick", "coverage")
    with pytest.raises(llm_client.ReadoutIncomplete) as info:
        _readout_call(registry.get("quick"))
    assert not isinstance(info.value, ValueError)
    assert not isinstance(info.value, llm_client.ReadoutCapped)
    assert info.value.reason == "coverage"
    assert "readout incomplete: coverage 0.20 < 0.5" in str(info.value)
    assert len(model.calls) == 1                       # no retry: same logits
    assert _metric("quick", "coverage") == before + 1


def test_a_response_without_logprobs_is_incomplete(registry, monkeypatch):
    async def send(prompt):
        return {"choices": [{"message": {"content": "9"}, "finish_reason": "length"}]}

    monkeypatch.setattr(llm_client, "_send", send)
    with pytest.raises(llm_client.ReadoutIncomplete, match="no token probabilities"):
        _readout_call(registry.get("quick"))


def test_the_probability_cap_fires_on_a_spread_readout(registry, monkeypatch):
    model = Model(SPREAD)
    monkeypatch.setattr(llm_client, "_send", model)
    before = _metric("capped", "cap")
    with pytest.raises(llm_client.ReadoutCapped) as info:
        _readout_call(registry.get("capped"))
    assert isinstance(info.value, llm_client.ReadoutIncomplete) and info.value.reason == "cap"
    assert str(info.value) == (
        "probability cap reached: 5 of 10 labels were not among the top 11 alternatives and "
        "could hold up to 0.25 (fast.max_unseen_mass 0.02) — raise models.yaml "
        "fast.top_logprobs together with the server's --max-logprobs, or use mode 'reasoned'"
    )
    assert len(model.calls) == 1
    assert _metric("capped", "cap") == before + 1


def test_the_probability_cap_stays_quiet_on_a_confident_readout(registry, monkeypatch):
    # Five digits seen, five missing — but the list's tail is ~1e-6, so the
    # missing ones could hold at most 5e-6.
    monkeypatch.setattr(llm_client, "_send", Model(CONFIDENT_9))
    readout = _readout_call(registry.get("quick"))
    assert readout.n_unseen == 5 and readout.unseen_bound == pytest.approx(5e-6)


def test_a_server_refusing_top_logprobs_is_named(registry, monkeypatch):
    calls = []

    async def send(prompt):
        calls.append(prompt)
        request = httpx.Request("POST", "http://quick:8000/v1/chat/completions")
        response = httpx.Response(400, request=request, json={
            "object": "error", "message": "top_logprobs (64) exceeds max_logprobs (20)"})
        raise httpx.HTTPStatusError("400 Bad Request", request=request, response=response)

    monkeypatch.setattr(llm_client, "_send", send)
    with pytest.raises(llm_client.LLMCallError) as info:
        _readout_call(registry.get("quick"))
    assert str(info.value).startswith(
        "the server refused top_logprobs 20 — its --max-logprobs is lower: raise it "
        "(ai/vllm) or lower models.yaml fast.top_logprobs")
    assert len(calls) == 1


def test_other_http_errors_are_plain_call_errors(registry, monkeypatch):
    async def send(prompt):
        request = httpx.Request("POST", "http://quick:8000/x")
        raise httpx.HTTPStatusError("500", request=request,
                                    response=httpx.Response(500, request=request, text="boom"))

    monkeypatch.setattr(llm_client, "_send", send)
    with pytest.raises(llm_client.LLMCallError) as info:
        _readout_call(registry.get("quick"))
    assert "refused top_logprobs" not in str(info.value) and "call failed" in str(info.value)


def test_a_2xx_that_is_not_json_is_a_named_call_error_posted_once(registry, monkeypatch):
    """A 200 whose body is not JSON (a proxy's HTML page) goes through the
    REAL ``_send`` — its ``response.json()`` raises JSONDecodeError, a
    ValueError — and comes out a named LLMCallError, counted failed, once."""
    posts = []

    class Client:
        async def post(self, url, **kw):
            posts.append(url)
            return httpx.Response(200, text="<html>bad gateway</html>",
                                  request=httpx.Request("POST", url))

    monkeypatch.setattr(llm_client, "_client", lambda: Client())
    failed = llm_client.llm_calls_total.labels(status="failed", model="quick")._value.get()
    with pytest.raises(llm_client.LLMCallError) as info:
        _readout_call(registry.get("quick"))
    assert not isinstance(info.value, llm_client.ReadoutIncomplete)
    assert str(info.value).startswith("unexpected response from model 'quick': not JSON (")
    assert len(posts) == 1                                  # never retried
    assert llm_client.llm_calls_total.labels(
        status="failed", model="quick")._value.get() == failed + 1


def test_a_non_json_2xx_fails_its_unit_with_the_model_named(registry, monkeypatch):
    calls = []

    async def send(prompt):
        calls.append(prompt)
        raise json.JSONDecodeError("Expecting value", "", 0)

    monkeypatch.setattr(llm_client, "_send", send)
    entry = _entries(_analyze(_png(), "p.png", [_c(model="quick", mode="fast")]))["has a house"]
    assert entry["status"] == "error" and len(calls) == 1
    assert entry["error"].startswith("unexpected response from model 'quick': not JSON")


@pytest.mark.parametrize("value, threshold, below, shown", [
    (0.20, 0.5, True, "0.20"),
    (0.4996, 0.5, True, "0.4996"),      # never "0.50 < 0.5"
    (0.49999999, 0.5, True, "0.49999999"),
    (0.25, 0.02, False, "0.25"),
    (0.0249, 0.02, False, "0.025"),     # never "0.02 (fast.max_unseen_mass 0.02)"
    (0.02, 0.02, False, "0.02"),        # exactly at the cap: at or above fires
    (0.0200001, 0.02, False, "0.0200001"),
])
def test_refusal_messages_print_the_value_across_the_threshold(value, threshold, below, shown):
    assert llm_client._beside(value, threshold, below=below) == shown


def test_a_near_threshold_refusal_reads_consistently(registry, monkeypatch):
    # coverage 0.4996 (digits) under fast.min_coverage 0.5.
    monkeypatch.setattr(llm_client, "_send", Model({"9": 0.4996, "I": 0.5004}))
    with pytest.raises(llm_client.ReadoutIncomplete) as info:
        _readout_call(registry.get("quick"))
    assert "readout incomplete: coverage 0.4996 < 0.5 " in str(info.value)


def test_a_readout_holds_its_models_slot(registry, monkeypatch):
    peak = {"now": 0, "max": 0}

    async def send(prompt):
        peak["now"] += 1
        peak["max"] = max(peak["max"], peak["now"])
        await asyncio.sleep(0.02)
        peak["now"] -= 1
        return readout_response(CONFIDENT_9)

    monkeypatch.setattr(llm_client, "_send", send)
    spec = registry.get("quick")                    # max_concurrent 2
    prompt = build_llm_prompt("ZmFrZQ==", "x", model=spec, style="fast")

    async def many():
        await asyncio.gather(*(llm_client.call_readout(prompt, labels=DIGITS, label="fast/x",
                                                       model=spec) for _ in range(6)))

    asyncio.run(many())
    assert peak["max"] == 2


def test_the_logprob_payload_is_dropped(registry, monkeypatch):
    seen = []

    async def send(prompt):
        data = readout_response(CONFIDENT_9)
        seen.append(data)
        return data

    monkeypatch.setattr(llm_client, "_send", send)
    _readout_call(registry.get("quick"))
    assert "logprobs" not in seen[0]["choices"][0]


# ---------------------------------------------------------------------------
# The rating from a readout
# ---------------------------------------------------------------------------


def _readout(top):
    from common.logprobs import option_distribution

    return option_distribution(readout_response(top)["choices"][0]["logprobs"]["content"], DIGITS)


def test_rate_readout_picks_the_band_then_the_score_inside_it():
    rated = llm_confidence.rate_readout(_readout(
        {"9": 0.50, "8": 0.10, "6": 0.33, "2": 0.02, "1": 0.05}))
    # PASS (7-10) = digits 6-9 = 0.93, MARGINAL 0.00, FAIL 0.07 ...
    assert rated["verdict"] == "PASS" and rated["score"] == 10 and rated["confidence"] == 93
    assert rated["reason"] == "Fast readout (no reasoning): PASS 0.93, MARGINAL 0.00, FAIL 0.07."
    # ... and the band beats the single digit: MARGINAL wins on mass here.
    rated = llm_confidence.rate_readout(_readout({"9": 0.40, "5": 0.30, "4": 0.30}))
    assert rated["verdict"] == "MARGINAL" and rated["score"] == 5 and rated["confidence"] == 60


def test_an_over_counted_stream_still_rates_within_0_100():
    # "9" and "[9" each near their own ceiling: the matched rows add past 1.
    rated = llm_confidence.rate_readout(_readout({"9": 0.7, "[9": 0.5}))
    assert rated["verdict"] == "PASS" and rated["score"] == 10
    assert rated["confidence"] == 100
    assert rated["reason"] == "Fast readout (no reasoning): PASS 1.00, MARGINAL 0.00, FAIL 0.00."
    assert sum(rated["block"]["distribution"].values()) == pytest.approx(1.0, abs=1e-3)


def test_rate_readout_ties_go_to_the_lower_band_and_score():
    rated = llm_confidence.rate_readout(_readout({"8": 0.5, "1": 0.25, "2": 0.25}))
    assert rated["verdict"] == "FAIL" and rated["score"] == 2


def test_the_readout_block_keeps_the_shape_and_adds_unseen_bound():
    readout = _readout(CONFIDENT_9)
    rated = llm_confidence.rate_readout(readout)
    block = rated["block"]
    assert tuple(block) == llm_confidence.READOUT_BLOCK_KEYS
    assert block["method"] == block["source"] == "readout" and block["headline"] == "readout"
    assert block["stated"] is None and block["stated_defaulted"] is False
    assert block["calibrated"] is None and block["samples"] is None
    assert list(block["distribution"]) == [str(v) for v in range(1, 11)]
    assert block["distribution"]["10"] == pytest.approx(0.92 / 0.985, abs=1e-4)
    assert block["verdict_probability"] == pytest.approx(rated["confidence"] / 100, abs=0.006)
    assert block["coverage"] == pytest.approx(0.985, abs=1e-4)
    assert block["unseen_bound"] == pytest.approx(readout.unseen_bound, abs=1e-4)


def test_a_fitted_temperature_moves_the_headline_and_says_so(registry):
    readout = _readout({"9": 0.7, "1": 0.3})
    raw = llm_confidence.rate_readout(readout)
    cal = llm_confidence.rate_readout(readout, registry.get("calibrated").fast.calibration["rating"])
    assert raw["confidence"] == 70 and cal["confidence"] < 70 and cal["verdict"] == "PASS"
    assert cal["block"]["headline"] == "calibrated"
    assert cal["block"]["calibrated"] == cal["confidence"]


def test_signals_accepts_no_stated_number():
    block = llm_confidence.signals(None, "PASS", None, False, method="readout", source="readout")
    assert block["stated"] is None
    assert llm_confidence.signal_value(block, "stated") is None


# ---------------------------------------------------------------------------
# End to end: analyze_document
# ---------------------------------------------------------------------------


def test_a_fast_criterion_end_to_end(registry, monkeypatch):
    model = Model()
    monkeypatch.setattr(llm_client, "_send", model)
    result = _analyze(_png(), "p.png", [
        _c(hint="presence", model="quick", mode="fast"),
        _c("has a roof", hint="presence", model="quick"),
        _c("has a door", hint="presence", model="quick", mode="reasoned"),
    ])
    fast, plain, reasoned = (_entries(result)[n] for n in ("has a house", "has a roof", "has a door"))
    assert fast["status"] == "ok" and fast["score"] == 10 and fast["verdict"] == "PASS"
    assert fast["confidence"] == 98
    assert fast["reason"] == "Fast readout (no reasoning): PASS 0.98, MARGINAL 0.01, FAIL 0.01."
    assert fast["detail"]["mode"] == "fast" and fast["options_used"]["mode"] == "fast"
    assert fast["detail"]["confidence"]["source"] == "readout"
    assert "unseen_bound" in fast["detail"]["confidence"]
    assert fast["detail"]["model"] == "quick"
    # One readout for the fast criterion; reasoned calls for the others.
    assert len(model.readouts()) == 1
    # Unset: exactly as before — no mode anywhere, the reasoned block.
    assert "mode" not in plain["detail"] and "mode" not in plain["options_used"]
    assert tuple(plain["detail"]["confidence"]) == llm_confidence.BLOCK_KEYS
    assert plain["reason"] == "the model reasoned"
    # Explicit reasoned: the reasoned call, and the echo of what was sent.
    assert reasoned["detail"]["mode"] == "reasoned" and reasoned["reason"] == "the model reasoned"


def test_the_fast_detail_matches_its_declaration(registry, monkeypatch):
    from analysis.result_specs import specs

    monkeypatch.setattr(llm_client, "_send", Model())
    result = _analyze(_png(), "p.png", [_c(model="quick", mode="fast")])
    detail = _entries(result)["has a house"]["detail"]
    spec = specs()["llm"]
    assert set(detail) <= set(spec.fields) and spec.required() <= set(detail)
    assert spec.fields["mode"].stable and spec.fields["mode"].when
    for key in llm_confidence.READOUT_BLOCK_KEYS:
        assert key in spec.fields["confidence"].description, key


@pytest.mark.parametrize("top, needle", [
    (LOW_COVERAGE, "readout incomplete: coverage 0.20 < 0.5"),
    (SPREAD, "probability cap reached: 5 of 10 labels were not among the top 11"),
])
def test_an_incomplete_or_capped_readout_fails_its_unit(registry, monkeypatch, top, needle):
    model = Model(top)
    monkeypatch.setattr(llm_client, "_send", model)
    result = _analyze(_png(), "p.png", [_c(model="capped", mode="fast")])
    entry = _entries(result)["has a house"]
    assert entry["status"] == "error"
    assert needle in entry["error"]
    assert entry["error"].endswith("— use mode 'reasoned' for this criterion")
    assert len(model.calls) == 1


def test_the_box_loop_runs_reasoned_after_a_fast_score(registry, monkeypatch):
    from llm import boxes as llm_boxes

    monkeypatch.setattr(llm_boxes, "LLM_BBOX_REFINE", False)
    monkeypatch.setattr(llm_boxes, "LLM_BBOX_GRIDLINES", False)
    model = Model()
    monkeypatch.setattr(llm_client, "_send", model)
    result = _analyze(_png(), "p.png", [_c(hint="presence", boxes=True, model="quick", mode="fast")])
    entry = _entries(result)["has a house"]
    assert entry["status"] == "ok" and entry["score"] == 10
    assert len(model.readouts()) == 1
    loop = [p for _, p in model.calls if not is_readout(p)]
    assert loop, "the loop made no call"
    for prompt in loop:   # every loop call is today's reasoned box body
        assert prompt["response_format"] == {"type": "json_object"}
        assert prompt["max_tokens"] == 50
        assert prompt["chat_template_kwargs"] == {"reasoning_effort": "low"}
    assert entry["localization"]["accepted_attempt"] == 1


def test_score_false_clears_a_fast_judgement(registry, monkeypatch):
    from llm import boxes as llm_boxes

    monkeypatch.setattr(llm_boxes, "LLM_BBOX_REFINE", False)
    monkeypatch.setattr(llm_boxes, "LLM_BBOX_GRIDLINES", False)
    monkeypatch.setattr(llm_client, "_send", Model())
    c = CriterionInput.model_validate({"name": "has a house", "score": False, "options": {
        "hint": "presence", "boxes": True, "model": "quick", "mode": "fast"}})
    entry = _entries(_analyze(_png(), "p.png", [c]))["has a house"]
    assert entry["score"] is None and entry["detail"]["value"] is None
    assert entry["detail"]["confidence"] is None and entry["detail"]["mode"] == "fast"
    # The band line IS the judgement: the reason keeps no band probabilities.
    assert entry["reason"] == llm_confidence.FAST_REASON_UNJUDGED == "Fast readout (no reasoning)."
    assert not any(band in entry["reason"] for band in ("PASS", "MARGINAL", "FAIL"))
    # The loop still ran on the readout's (withheld) score.
    assert entry["localization"]["accepted_attempt"] == 1


def test_a_calibrated_entry_reports_a_calibrated_headline(registry, monkeypatch):
    monkeypatch.setattr(llm_client, "_send", Model({"9": 0.7, "1": 0.3}))
    entry = _entries(_analyze(_png(), "p.png", [_c(model="calibrated", mode="fast")]))["has a house"]
    block = entry["detail"]["confidence"]
    assert block["headline"] == "calibrated" and entry["confidence"] == block["calibrated"] < 70


def test_a_text_only_fast_model_reads_the_text(registry, monkeypatch):
    model = Model()
    monkeypatch.setattr(llm_client, "_send", model)
    doc = analysis.load_document_bytes(b"Total due $5", "t.txt", None, keep_source=True)
    result = asyncio.run(analysis.analyze_document(doc, [_c(model="quicktext", mode="fast")]))
    entry = _entries(result)["has a house"]
    assert entry["status"] == "ok" and entry["detail"]["image_sent"] is False
    assert "Total due $5" in _user_text(model.readouts()[0])


# ---------------------------------------------------------------------------
# The named 400s
# ---------------------------------------------------------------------------


def test_a_request_level_mode_is_named(registry):
    message = _refusal({"documents": [TXT], "mode": "fast",
                        "criteria": [{"name": "x", "options": {"model": "quick"}}]})
    assert "mode is per criterion: set options.mode on each llm criterion" in message
    assert "nothing job-wide has a mode" in message


def test_a_request_level_mode_is_named_on_multipart_too(registry):
    from fastapi import HTTPException
    from starlette.datastructures import FormData

    from api import assess as assess_api
    from api import references as references_api

    class Request:
        def __init__(self, items):
            self._form = FormData(items)

        async def form(self):
            return self._form

    for module, items in ((assess_api, [("text", "hi"), ("mode", "fast")]),
                          (references_api, [("mode", "fast"), ("from_job", "0123456789ab")])):
        with pytest.raises(HTTPException) as info:
            asyncio.run(module._from_form(Request(items)))
        assert info.value.status_code == 400
        assert "mode is per criterion" in info.value.detail


def test_a_request_level_mode_on_post_references_is_named(registry):
    from api.reference_schemas import ReferenceRequest

    with pytest.raises(Exception) as info:
        ReferenceRequest.model_validate({"from_job": "0123456789ab", "mode": "fast"})
    assert "mode is per criterion" in str(info.value)


def test_a_criterions_top_level_mode_belongs_in_options(registry):
    message = _refusal({"documents": [TXT], "criteria": [{"name": "x", "mode": "fast"}]})
    assert "mode belongs in options: options.mode on an llm criterion" in message


@pytest.mark.parametrize("type_, needle", [
    ("cv", "a cv criterion is answered by an OpenCV detector"),
    ("text", "a text criterion is a deterministic search"),
    ("detector", "a detector criterion is answered by the open-vocabulary detector"),
])
def test_options_mode_on_another_type_is_named(registry, type_, needle):
    message = _refusal({"documents": [TXT], "criteria": [
        {"name": "sharpness", "type": type_, "options": {"mode": "fast"}}]})
    assert "options.mode: only an llm criterion has a mode" in message and needle in message


@pytest.mark.parametrize("mode", ["adaptive", "FAST", 1])
def test_unknown_modes_are_refused(registry, mode):
    message = _refusal({"documents": [TXT], "criteria": [
        {"name": "x", "options": {"model": "quick", "mode": mode}}]})
    assert "options.mode" in message


@pytest.mark.parametrize("criterion, job_model, source", [
    ({"options": {"model": "slow", "mode": "fast"}}, None, "its options.model"),
    ({"options": {"mode": "fast"}}, "slow", "the request's `model`"),
    ({"options": {"mode": "fast"}}, None, "the registry default"),
])
def test_fast_on_a_model_without_a_fast_block_is_named(registry, criterion, job_model, source):
    body = {"documents": [TXT], "criteria": [{"name": "x", **criterion}]}
    if job_model:
        body["model"] = job_model
    message = _refusal(body)
    assert "criterion 'x' has options.mode 'fast', and its model 'slow'" in message
    assert f"({source})" in message
    assert "fast-capable models: ['quick', 'quicktext', 'capped', 'calibrated']" in message


def test_fast_on_a_fast_capable_model_is_accepted(registry):
    req = AssessRequest.model_validate({"documents": [TXT], "model": "quick", "criteria": [
        {"name": "x", "options": {"mode": "fast"}},
        {"name": "y", "options": {"model": "slow"}},
    ]})
    assert req.criteria[0].is_fast() and not req.criteria[1].is_fast()


def test_a_reference_on_a_fast_criterion_is_named(registry):
    message = _refusal({"documents": [TXT], "references": ["r0123456789ab"], "criteria": [
        {"name": "x", "options": {"model": "quick", "mode": "fast", "reference": {}}}]})
    assert "options.reference: references guide the reasoned scoring call" in message


def test_a_fast_criterion_skips_the_references_image_rule(registry):
    # quicktext is text-only: a GUIDED criterion on it would be refused with
    # references on the request; a fast one is never guided.
    AssessRequest.model_validate({"documents": [TXT], "references": ["r0123456789ab"],
                                  "criteria": [{"name": "x", "options": {
                                      "model": "quicktext", "mode": "fast"}}]})


def test_explicit_fast_criteria_on_post_references_are_refused(registry):
    from api.reference_schemas import ReferenceRequest

    with pytest.raises(Exception) as info:
        ReferenceRequest.model_validate({
            "document": {"type": "base64", "data": base64.b64encode(_png()).decode()},
            "criteria": [{"name": "x", "options": {"model": "quick", "mode": "fast"}}],
        })
    assert "options.mode 'fast' on ['x']: a reference records reasoned 1-10 answers" in str(
        info.value)


# ---------------------------------------------------------------------------
# The payload round trip
# ---------------------------------------------------------------------------


def test_criteria_round_trip_through_the_payload(registry):
    from jobs.payloads import SubmittedDocument, build_assess_payload

    req = AssessRequest.model_validate({"documents": [TXT], "criteria": [
        {"name": "fast", "options": {"model": "quick", "mode": "fast", "hint": "presence"}},
        {"name": "reasoned", "options": {"mode": "reasoned"}},
        {"name": "unset", "options": {"hint": "quality"}},
        {"name": "sharpness", "type": "cv"},
    ]})
    payload = build_assess_payload(req, [SubmittedDocument(b"x", "t.txt", None, "txt", 1)])
    again = [CriterionInput.model_validate(c) for c in json.loads(json.dumps(payload["criteria"]))]
    assert [c.resolved_options() for c in again] == [c.resolved_options() for c in req.criteria]
    assert [c.resolved_options().get("mode") for c in again] == ["fast", "reasoned", None, None]
    assert [c.is_fast() for c in again] == [True, False, False, False]
    # And through a second request built from them (inheritance, from_job).
    rebuilt = AssessRequest.model_validate({"documents": [TXT], "criteria": payload["criteria"]})
    assert [c.model_dump() for c in rebuilt.criteria] == payload["criteria"]


# ---------------------------------------------------------------------------
# References and identity
# ---------------------------------------------------------------------------


def test_a_fast_criterion_is_answered_but_never_guided(registry):
    fast = _c(model="quick", mode="fast")
    assert fast.answered_by_model() and not fast.guided_by_llm()
    assert _c(model="quick").guided_by_llm() and _c(mode="reasoned").guided_by_llm()


def test_the_job_guide_notes_why_a_fast_criterion_is_not_guided(registry):
    from analysis.references import FAST_NOT_GUIDED_NOTE, JobReferences

    for mode in ("explicit", "auto"):
        refs = JobReferences(plan={"mode": mode, "criteria": {}})
        guide = refs.guide(_c(model="quick", mode="fast"), 0)
        assert guide is not None and not guide.applied and guide.note == FAST_NOT_GUIDED_NOTE
        detail = guide.unapplied_detail()
        assert detail["applied"] is False and detail["note"] == FAST_NOT_GUIDED_NOTE
        assert not refs.needs_selection(_c(model="quick", mode="fast"))


class _Refs:
    """A reference store stub: get() by id, list() for the auto pool."""

    def __init__(self, refs):
        self.refs = {r.id: r for r in refs}

    async def get(self, ref_id):
        return self.refs.get(ref_id)

    async def list(self, **_):
        return list(self.refs.values()), len(self.refs)


def _reference(ref_id: str, *names: str):
    from references.model import Reference

    return Reference(
        id=ref_id, status="ready", source_kind="document", created_at="", updated_at="",
        record={"page": {}, "criteria": {
            n: {"usable": True, "expected": {"score": 9, "verdict": "PASS", "reason": "seen"},
                "input": {"name": n, "type": "llm", "options": {"hint": "presence"}},
                "region_source": "whole_page", "regions": [], "composite": None}
            for n in names}},
    )


def test_explicit_and_auto_references_skip_a_fast_criterion(registry):
    from references.resolve import auto_pool, resolve_references

    store = _Refs([_reference("r0123456789ab", "has a house", "has a roof")])
    req = AssessRequest.model_validate({"documents": [TXT], "references": ["r0123456789ab"],
                                        "criteria": [
        {"name": "has a house", "options": {"model": "quick", "mode": "fast"}},
        {"name": "has a roof", "options": {"model": "quick"}},
    ]})
    plan = asyncio.run(resolve_references(req, store)).plan
    assert list(plan["criteria"]) == ["has a roof"]

    only_fast = AssessRequest.model_validate({"documents": [TXT], "references": "auto",
                                              "model": "quick", "criteria": [
        {"name": "has a house", "options": {"mode": "fast"}}]})
    pool, _ = asyncio.run(auto_pool(only_fast, store))
    assert pool == []


def test_identity_drops_model_and_mode(registry):
    from api.criterion_options import identity, without_model
    from api.references import _same_question
    from references.resolve import _same

    unset = CriterionInput.model_validate({"name": "x", "options": {"hint": "presence"}})
    for other in ({"mode": "reasoned"}, {"mode": "fast", "model": "quick"}, {"model": "slow"}):
        c = CriterionInput.model_validate({"name": "x", "options": {"hint": "presence", **other}})
        assert identity(c.resolved_options()) == identity(unset.resolved_options())
        assert without_model(c.resolved_options()) == identity(c.resolved_options())
        assert _same(c, unset) and _same_question(c, unset)
    # The hint IS the question.
    other = CriterionInput.model_validate({"name": "x", "options": {"hint": "quality"}})
    assert not _same(other, unset)


def test_from_job_strips_the_mode_of_an_inherited_criterion(registry):
    from api.references import _as_reference_criterion

    c = _as_reference_criterion(_c(model="quick", mode="fast"))
    assert c.options.mode is None and c.options.model == "quick"
    assert not c.is_fast() and c.guided_by_llm()
    plain = _c(model="quick")
    assert _as_reference_criterion(plain) is plain


# ---------------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------------


def test_get_models_reports_fast(checked_in):
    from api.introspection import list_models

    payload = json.loads(list_models().body.decode("utf-8"))
    by_id = {m["id"]: m for m in payload["models"]}
    assert by_id["qwen3.8-solo"]["supports"]["fast"] is True
    assert by_id["qwen3.8-solo"]["fast"] == {"top_logprobs": 64, "min_coverage": 0.5,
                                             "max_unseen_mass": 0.02, "calibrated": []}
    assert by_id["qwen3.8-solo"]["vision"] is True
    assert by_id["qwen3.8-solo"]["max_images_per_prompt"] == 3
    assert by_id["muse-glimmer"]["supports"]["fast"] is False and by_id["muse-glimmer"]["fast"] is None
    # Never the body extras or the markers.
    text = json.dumps(payload)
    assert "enable_thinking" not in text and "think_on" not in text


def test_get_models_reports_a_fitted_calibration(registry):
    from api.introspection import list_models

    payload = json.loads(list_models().body.decode("utf-8"))
    by_id = {m["id"]: m for m in payload["models"]}
    assert by_id["calibrated"]["fast"]["calibrated"] == ["rating"]
    assert by_id["quick"]["fast"]["calibrated"] == []


def test_get_criterion_types_has_the_mode_block(checked_in):
    from api.introspection import list_criterion_types

    payload = json.loads(list_criterion_types().body.decode("utf-8"))
    block = payload["mode"]
    assert block["values"] == ["reasoned", "fast"] and block["default"] == "reasoned"
    assert block["request_level"] is False
    assert block["fast_models"] == ["qwen3.5-4b", "qwen3.8-solo"]
    llm = payload["types"]["llm"]
    assert "mode" in llm["options_schema"]["properties"]
    assert "mode" not in llm["defaults"]                        # unset resolves as before
    assert "mode" in llm["result"]["fields"]


# ---------------------------------------------------------------------------
# (Postgres) through the API: the 400s, and from_job against a fast job
# ---------------------------------------------------------------------------

HOUSE = "has a house"


@pytest.fixture(scope="module")
def client(need_postgres):
    from fastapi.testclient import TestClient

    import main

    with TestClient(main.app) as c:
        yield c


def _wait(client, path: str, done: tuple[str, ...], key: str, timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(path).json()
        if body.get(key) in done:
            return body
        time.sleep(0.05)
    raise AssertionError(f"{path} did not finish: {body}")


def test_the_api_names_a_request_level_mode(client):
    r = client.post("/assess", json={"document": TXT, "mode": "fast",
                                     "criteria": [{"name": "x"}]})
    assert r.status_code == 400 and "mode is per criterion" in r.json()["detail"]
    r = client.post("/assess", data={"text": "hi", "mode": "fast"})
    assert r.status_code == 400 and "mode is per criterion" in r.json()["detail"]
    r = client.post("/references", json={"from_job": "0123456789ab", "mode": "fast"})
    assert r.status_code == 400 and "mode is per criterion" in r.json()["detail"]


def test_from_job_never_copies_a_fast_answer(client, monkeypatch):
    from llm import boxes as llm_boxes

    monkeypatch.setattr(llm_boxes, "LLM_BBOX_REFINE", False)
    monkeypatch.setattr(llm_boxes, "LLM_BBOX_GRIDLINES", False)
    model = Model()
    monkeypatch.setattr(llm_client, "_send", model)
    doc = {"type": "base64", "data": base64.b64encode(_png()).decode("ascii"),
           "filename": "house.png"}
    r = client.post("/assess", json={"document": doc, "criteria": [
        {"name": HOUSE, "options": {"hint": "presence", "boxes": True,
                                    "model": "qwen3.8-solo", "mode": "fast"}}]})
    assert r.status_code == 202, r.text
    job = _wait(client, f"/jobs/{r.json()['job_id']}", ("completed", "failed"), "phase")
    assert job["phase"] == "completed", job
    unit = job["result"]["assessment"]["per_criterion_scores"][HOUSE]
    assert unit["detail"]["mode"] == "fast" and unit["reason"].startswith("Fast readout")

    model.calls.clear()
    r = client.post("/references", json={"from_job": job["job_id"], "description": "x"})
    assert r.status_code == 202, r.text
    ref = _wait(client, f"/references/{r.json()['reference_id']}", ("ready", "failed"), "status")
    assert ref["status"] == "ready", ref
    house = ref["record"]["criteria"][HOUSE]
    # The mode was dropped; the answer is the creation job's REASONED one.
    assert house["input"]["options"].get("mode") is None
    assert house["expected"]["reason"] == "the model reasoned"
    assert "Fast readout" not in json.dumps(ref["record"])
    assert any("fast readout" in w for w in ref["record"]["warnings"])
    assert model.calls and not model.readouts()          # scored reasoned, no readout

    # Explicit fast criteria are refused at submit.
    r = client.post("/references", json={"document": doc, "criteria": [
        {"name": HOUSE, "options": {"model": "qwen3.8-solo", "mode": "fast"}}]})
    assert r.status_code == 400 and "a reference records reasoned 1-10 answers" in r.json()["detail"]


def test_from_job_never_copies_an_errored_fast_units_observation(client, monkeypatch):
    """A capped readout errors its unit with ``detail: {}`` — nothing on the
    unit says it was fast. The job's own criterion does: its observation is
    not copied and the warning is there, even when the caller's breakdown and
    regions mean the criterion is not re-run (the copy would then show)."""
    from llm import boxes as llm_boxes

    monkeypatch.setattr(llm_boxes, "LLM_BBOX_REFINE", False)
    monkeypatch.setattr(llm_boxes, "LLM_BBOX_GRIDLINES", False)
    model = Model(SPREAD)
    monkeypatch.setattr(llm_client, "_send", model)
    doc = {"type": "base64", "data": base64.b64encode(_png()).decode("ascii"),
           "filename": "house.png"}
    r = client.post("/assess", json={"document": doc, "criteria": [
        {"name": HOUSE, "options": {"hint": "presence", "boxes": True,
                                    "model": "qwen3.8-solo", "mode": "fast"}}]})
    assert r.status_code == 202, r.text
    job = _wait(client, f"/jobs/{r.json()['job_id']}", ("completed", "failed"), "phase")
    assert job["phase"] == "completed", job
    unit = job["result"]["assessment"]["per_criterion_scores"][HOUSE]
    assert unit["status"] == "error" and "probability cap reached" in unit["error"]
    assert not (unit.get("detail") or {}).get("mode")      # the unit cannot say it

    model.calls.clear()
    r = client.post("/references", json={
        "from_job": job["job_id"], "description": "x",
        "breakdown": {HOUSE: {"score": 9, "verdict": "PASS", "reason": "mine"}},
        "regions": {HOUSE: [{"box": [10, 10, 100, 100]}]},
    })
    assert r.status_code == 202, r.text
    ref = _wait(client, f"/references/{r.json()['reference_id']}", ("ready", "failed"), "status")
    assert ref["status"] == "ready", ref
    house = ref["record"]["criteria"][HOUSE]
    assert house["expected"]["reason"] == "mine"
    assert "probability cap" not in json.dumps(ref["record"])
    assert (house.get("observed") or {}).get("job_id") != job["job_id"]
    warnings = ref["record"]["warnings"]
    assert any("fast readout" in w and "your breakdown is the answer" in w for w in warnings)
    assert not model.readouts()
