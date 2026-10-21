"""common.signals.wire — the POST /signals request and response shapes.

The sidecar (ai/signals) builds its answers with these classes and
route-policy parses them, so the JSON both sides see is pinned here: the
exact keys, what a request may not carry, what a malformed answer turns
into, and that no error message ever quotes its input (a sentinel string is
planted in every value that could leak).

    UV_LINK_MODE=copy uv run --no-sync pytest shared/common/tests/test_signals_wire.py -q
"""

from __future__ import annotations

import copy
import json

import pytest

from common.pii import ENTITY_TYPES
from common.signals import (
    FEATURES,
    MAX_EXAMPLE_CHARS,
    MAX_EXAMPLES,
    MAX_REQUEST_CHARS,
    MAX_TEXT_CHARS,
    MAX_TEXTS,
    STATUSES,
    TASK_MODELS,
    TASKS,
    USABLE_STATUSES,
    EntityStat,
    Example,
    ExampleSet,
    InjectionFeatures,
    LanguageFeatures,
    NearestFeatures,
    Neighbour,
    PiiFeatures,
    RouteShare,
    SignalsRequest,
    SignalsResponse,
    TaskRequest,
    TaskResult,
    TextItem,
    WireError,
    fingerprint_examples,
)

SENTINEL = "ZZ-SENTINEL-4471-ZZ"
MODEL = "intfloat/multilingual-e5-small"
EXAMPLES = ExampleSet.build([
    Example("code-001", "local-code", "my script throws an error when I run it"),
    Example("gen-001", "local-general", "what is net metering"),
    Example("rsn-001", "local-reasoning", "prove that the square root of 2 is irrational"),
], model=MODEL)


def full_request() -> SignalsRequest:
    return SignalsRequest(
        (TextItem("u7", f"{SENTINEL} my SSN is 523-45-6789"), TextItem("u6", "hello"),
         TextItem("t2", "1 row: Laura Kim")),
        (TaskRequest("pii", ("u7", "t2", "u6")),
         TaskRequest("nearest", ("u7",), max_tokens=256, k=5, examples=EXAMPLES),
         TaskRequest("injection", ("u7",), max_tokens=256, windows="head_tail"),
         TaskRequest("language", ("u7",))),
        request_id="5f0c2d4e-0000-4000-8000-000000000001", deadline_ms=90)


# The plan's § 3.1 answer, verbatim in shape.
PLAN_RESPONSE = {
    "request_id": "5f0c2d4e-0000-4000-8000-000000000001", "latency_ms": 41.8,
    "models": {"pii": "gravitee-io/bert-small-pii-detection@abc123", "embed": f"{MODEL}@def456",
               "injection": "NeuralTrust/prompt-guard-oss-small@0a1b2c", "language": "fasttext/lid.176@0123456789ab"},
    "results": {
        "pii": {"status": "ok", "ms": 8.1, "texts": {"u7": {
            "entities": {"person": {"count": 1, "max_score": 0.97}, "street_address": {"count": 1, "max_score": 0.95}},
            "complete": True, "cached": False}}},
        "nearest": {"status": "ok", "ms": 15.2, "texts": {"u7": {
            "neighbours": [{"id": "code-001", "route": "local-code", "similarity": 0.871}],
            "routes": {"local-code": {"share": 0.8, "best": 0.871, "count": 4}}, "cached": False}}},
        "injection": {"status": "deadline", "ms": None, "texts": {}},
        "language": {"status": "ok", "ms": 0.1, "texts": {"u7": {"language": "en", "confidence": 0.98}}},
    },
}


# ── vocabulary ────────────────────────────────────────────────────────────────
def test_vocabularies():
    assert TASKS == ("pii", "nearest", "injection", "language")
    assert STATUSES == ("ok", "partial", "deadline", "skipped", "warming", "error", "unavailable")
    assert USABLE_STATUSES == ("ok", "partial")
    assert dict(TASK_MODELS) == {"pii": "pii", "nearest": "embed", "injection": "injection", "language": "language"}
    assert set(FEATURES) == set(TASKS)
    assert (MAX_TEXT_CHARS, MAX_REQUEST_CHARS, MAX_TEXTS) == (20_000, 200_000, 64)


# ── the request ───────────────────────────────────────────────────────────────
def test_request_json_shape():
    body = full_request().to_json()
    assert list(body) == ["request_id", "deadline_ms", "texts", "tasks"]
    assert body["texts"][0] == {"id": "u7", "text": f"{SENTINEL} my SSN is 523-45-6789"}
    assert body["tasks"][0] == {"task": "pii", "texts": ["u7", "t2", "u6"]}
    assert body["tasks"][1]["examples"]["fingerprint"] == EXAMPLES.fingerprint
    assert body["tasks"][1]["examples"]["items"][0] == {"id": "code-001", "route": "local-code",
                                                        "text": "my script throws an error when I run it"}
    assert {k for k in body["tasks"][1]} == {"task", "texts", "max_tokens", "k", "examples"}
    assert body["tasks"][2] == {"task": "injection", "texts": ["u7"], "max_tokens": 256, "windows": "head_tail"}
    # Unset optionals are omitted, not null.
    bare = SignalsRequest((), (TaskRequest("language"),)).to_json()
    assert bare == {"texts": [], "tasks": [{"task": "language", "texts": []}]}


def test_request_round_trips_through_json():
    request = full_request()
    assert SignalsRequest.from_json(json.loads(json.dumps(request.to_json()))) == request


def test_lists_become_tuples():
    request = SignalsRequest([TextItem("u1", "x")], [TaskRequest("pii", ["u1"])])
    assert request.texts == (TextItem("u1", "x"),) and request.tasks[0].texts == ("u1",)
    assert request.task("pii") is request.tasks[0] and request.task("nearest") is None


@pytest.mark.parametrize("build", [
    lambda: TaskRequest("summarise", ()),
    lambda: TaskRequest("pii", ("u1", "u1")),
    lambda: TaskRequest("pii", "u1"),
    lambda: TaskRequest("pii", ("u1",), k=5),
    lambda: TaskRequest("language", ("u1",), max_tokens=10),
    lambda: TaskRequest("injection", ("u1",), examples=EXAMPLES),
    lambda: TaskRequest("nearest", ("u1",)),
    lambda: TaskRequest("nearest", ("u1",), examples={"fingerprint": "f", "items": []}),
    lambda: TaskRequest("nearest", ("u1",), examples=EXAMPLES, k=0),
    lambda: TaskRequest("nearest", ("u1",), examples=EXAMPLES, k=51),
    lambda: TaskRequest("injection", ("u1",), max_tokens=513),
    lambda: TaskRequest("injection", ("u1",), max_tokens=True),
    lambda: TaskRequest("injection", ("u1",), windows="all"),
    lambda: SignalsRequest((TextItem("u1", "a"), TextItem("u1", "b")), ()),
    lambda: SignalsRequest((TextItem("u1", "a"),), (TaskRequest("pii", ("u2",)),)),
    lambda: SignalsRequest((), (TaskRequest("pii"), TaskRequest("pii"))),
    lambda: SignalsRequest((), (), deadline_ms=0),
    lambda: SignalsRequest((), (), deadline_ms=float("nan")),
    lambda: SignalsRequest((), (), request_id=""),
    lambda: SignalsRequest(({"id": "u1", "text": "x"},), ()),
    lambda: TextItem("", "x"),
    lambda: TextItem("u1", None),
    lambda: Example("e", "local-code", ""),
    lambda: ExampleSet("f", ()),
    lambda: ExampleSet("f", (Example("e", "r", "a"), Example("e", "r", "b"))),
])
def test_requests_that_cannot_be_built(build):
    with pytest.raises(WireError):
        build()


@pytest.mark.parametrize("mutate", [
    lambda b: b.update(extra=SENTINEL),
    lambda b: b["tasks"][0].update(k=SENTINEL),
    lambda b: b["tasks"][2].update(windows=SENTINEL),
    lambda b: b["tasks"].append({"task": SENTINEL, "texts": []}),
    lambda b: b["texts"].append({"id": SENTINEL, "text": "x", "extra": 1}),
    lambda b: b["texts"][0].update(id=12),
    lambda b: b["tasks"][1]["examples"].update(items=SENTINEL),
    lambda b: b.update(texts=SENTINEL),
    lambda b: b["tasks"][0].update(texts=[SENTINEL]),
])
def test_malformed_requests_are_refused_without_quoting_them(mutate):
    body = copy.deepcopy(full_request().to_json())
    mutate(body)
    with pytest.raises(WireError) as caught:
        SignalsRequest.from_json(body)
    assert SENTINEL not in str(caught.value)
    with pytest.raises(WireError):
        SignalsRequest.from_json(SENTINEL)


def test_cap_problems_name_sizes_never_text():
    assert full_request().cap_problem() is None
    many = SignalsRequest(tuple(TextItem(f"u{i}", "x") for i in range(MAX_TEXTS + 1)), ())
    assert many.cap_problem() == f"{MAX_TEXTS + 1} texts, more than {MAX_TEXTS}"
    long_text = SENTINEL + "x" * MAX_TEXT_CHARS
    problem = SignalsRequest((TextItem("u1", long_text),), ()).cap_problem()
    assert problem.startswith("text 'u1' has ") and SENTINEL not in problem
    total = SignalsRequest(tuple(TextItem(f"u{i}", "x" * MAX_TEXT_CHARS) for i in range(11)), ())
    assert total.cap_problem() == f"the texts hold {11 * MAX_TEXT_CHARS} characters, more than {MAX_REQUEST_CHARS}"
    big_set = ExampleSet.build([Example(f"e{i}", "local-code", "x") for i in range(MAX_EXAMPLES + 1)], model=MODEL)
    assert SignalsRequest((), (TaskRequest("nearest", examples=big_set),)).cap_problem() == \
        f"{MAX_EXAMPLES + 1} examples, more than {MAX_EXAMPLES}"
    long_example = ExampleSet.build([Example("e1", "local-code", "x" * (MAX_EXAMPLE_CHARS + 1))], model=MODEL)
    assert SignalsRequest((), (TaskRequest("nearest", examples=long_example),)).cap_problem() == \
        f"example 'e1' has {MAX_EXAMPLE_CHARS + 1} characters, more than {MAX_EXAMPLE_CHARS}"
    # 20,000 characters exactly is allowed.
    assert SignalsRequest((TextItem("u1", "x" * MAX_TEXT_CHARS),), ()).cap_problem() is None


def test_the_example_fingerprint():
    items = list(EXAMPLES.items)
    assert EXAMPLES.fingerprint == fingerprint_examples(items, MODEL)
    assert len(EXAMPLES.fingerprint) == 64 and int(EXAMPLES.fingerprint, 16) >= 0
    assert fingerprint_examples(reversed(items), MODEL) == EXAMPLES.fingerprint       # order-independent
    assert fingerprint_examples(items, MODEL + "@v2") != EXAMPLES.fingerprint         # another model
    edited = [Example(items[0].id, items[0].route, items[0].text + "!")] + items[1:]
    assert fingerprint_examples(edited, MODEL) != EXAMPLES.fingerprint                 # an edited example
    moved = [Example(items[0].id, "local-general", items[0].text)] + items[1:]
    assert fingerprint_examples(moved, MODEL) != EXAMPLES.fingerprint                  # a moved example


# ── features ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("build", [
    lambda: PiiFeatures({"ssn": EntityStat(1)}),
    lambda: PiiFeatures({"email": {"count": 1}}),
    lambda: PiiFeatures(complete="yes"),
    lambda: EntityStat(-1),
    lambda: EntityStat(True),
    lambda: EntityStat(1, 1.5),
    lambda: Neighbour("e", "r", 2.0),
    lambda: RouteShare(1.2, 0.5, 1),
    lambda: NearestFeatures((Neighbour("e", "r", 0.5),) * 51),
    lambda: InjectionFeatures(float("inf")),
    lambda: InjectionFeatures(0.5, windows=0),
    lambda: LanguageFeatures(SENTINEL + " this is text", 0.9),
    lambda: LanguageFeatures("en", -0.1),
    lambda: TaskResult("done"),
    lambda: TaskResult("ok", ms=-1),
    lambda: SignalsResponse({"summarise": TaskResult("ok")}),
    lambda: SignalsResponse({"pii": TaskResult("ok", texts={"u1": LanguageFeatures("en", 0.9)})}),
    lambda: SignalsResponse({}, models={"pii": 3}),
])
def test_responses_that_cannot_be_built(build):
    with pytest.raises(WireError) as caught:
        build()
    assert SENTINEL not in str(caught.value)


def test_every_entity_type_is_accepted():
    features = PiiFeatures({t: EntityStat(1, 0.5) for t in ENTITY_TYPES})
    assert set(features.entities) == set(ENTITY_TYPES)
    assert LanguageFeatures("zh-Hans", 0.9).language == "zh-Hans"
    assert LanguageFeatures("ceb", 0.4).language == "ceb"


# ── the response ──────────────────────────────────────────────────────────────
def test_the_plans_response_parses():
    response = SignalsResponse.from_json(PLAN_RESPONSE)
    pii = response.result("pii")
    assert pii.status == "ok" and pii.ms == 8.1 and pii.usable
    assert pii.texts["u7"] == PiiFeatures({"person": EntityStat(1, 0.97), "street_address": EntityStat(1, 0.95)})
    nearest = response.result("nearest").texts["u7"]
    assert nearest.neighbours == (Neighbour("code-001", "local-code", 0.871),)
    assert nearest.routes == {"local-code": RouteShare(0.8, 0.871, 4)}
    assert response.result("injection") == TaskResult("deadline")
    assert not response.result("injection").usable
    assert response.result("language").texts["u7"] == LanguageFeatures("en", 0.98)
    assert response.model_for("nearest") == f"{MODEL}@def456"
    assert response.model_for("pii").startswith("gravitee-io/") and response.model_for("summarise") is None
    assert response.latency_ms == 41.8 and response.request_id == PLAN_RESPONSE["request_id"]


def test_response_round_trips_through_json():
    response = SignalsResponse.from_json(PLAN_RESPONSE)
    assert SignalsResponse.from_json(json.loads(json.dumps(response.to_json()))) == response
    body = response.to_json()
    assert body["results"]["injection"] == {"status": "deadline", "ms": None, "texts": {}}
    assert body["results"]["pii"]["texts"]["u7"]["entities"]["person"] == {"count": 1, "max_score": 0.97}
    assert TaskResult("error", error="OrtRuntimeError").to_json() == \
        {"status": "error", "ms": None, "texts": {}, "error": "OrtRuntimeError"}


def test_a_newer_sidecar_is_read_leniently():
    body = copy.deepcopy(PLAN_RESPONSE)
    body["results"]["summarise"] = {"status": "ok", "texts": {"u7": {"summary": SENTINEL}}}
    body["future"] = SENTINEL
    body["results"]["pii"]["texts"]["u7"]["spans_never"] = SENTINEL
    response = SignalsResponse.from_json(body)
    assert set(response.results) == set(TASKS)
    assert SENTINEL not in repr(response)


@pytest.mark.parametrize("task,result", [
    ("pii", {"status": SENTINEL}),
    ("pii", {"status": "ok", "texts": {"u7": {"entities": {SENTINEL: {"count": 1}}}}}),
    ("pii", {"status": "ok", "texts": {"u7": {"entities": {"email": {"count": "1"}}}}}),
    ("pii", {"status": "ok", "texts": SENTINEL}),
    ("nearest", {"status": "ok", "texts": {"u7": {"neighbours": [{"id": "e", "route": "r"}]}}}),
    ("injection", {"status": "ok", "texts": {"u7": {"score": SENTINEL}}}),
    ("language", {"status": "ok", "texts": {"u7": {"language": SENTINEL + " text", "confidence": 0.9}}}),
    ("language", {"status": "ok", "ms": "fast", "texts": {}}),
    ("language", SENTINEL),
])
def test_a_malformed_task_becomes_an_error_result_and_the_rest_survive(task, result):
    body = copy.deepcopy(PLAN_RESPONSE)
    body["results"][task] = result
    response = SignalsResponse.from_json(body)
    assert response.result(task) == TaskResult("error", error=f"malformed {task} result")
    assert SENTINEL not in repr(response)
    others = [t for t in ("pii", "language") if t != task]
    assert response.result(others[0]).status == "ok"


def test_a_sidecar_error_is_kept_only_as_an_exception_name():
    body = copy.deepcopy(PLAN_RESPONSE)
    body["results"]["injection"] = {"status": "error", "error": "OrtRuntimeError"}
    assert SignalsResponse.from_json(body).result("injection").error == "OrtRuntimeError"
    body["results"]["injection"] = {"status": "error", "error": f"failed on: {SENTINEL} my SSN"}
    assert SignalsResponse.from_json(body).result("injection").error is None


def test_a_partial_result_carries_its_failure_and_stays_usable():
    # One text failed, the other was scanned: the sidecar answers partial with
    # the failure's type, and a consumer still reads the scanned text.
    body = copy.deepcopy(PLAN_RESPONSE)
    body["results"]["pii"] = {"status": "partial", "ms": 4.0, "error": "OrtRuntimeError",
                              "texts": {"u7": {"entities": {"phone": {"count": 1}}, "complete": True}}}
    result = SignalsResponse.from_json(body).result("pii")
    assert (result.status, result.error, set(result.texts)) == ("partial", "OrtRuntimeError", {"u7"})
    assert result.usable
    assert TaskResult.from_json("pii", result.to_json()) == result


@pytest.mark.parametrize("body", [
    SENTINEL, [], None, {"results": SENTINEL}, {},
    {"results": {}, "models": {"pii": 3}}, {"results": {}, "models": SENTINEL},
    {"results": {}, "latency_ms": "slow"}, {"results": {}, "request_id": 7},
])
def test_a_response_that_is_not_the_shape(body):
    with pytest.raises(WireError) as caught:
        SignalsResponse.from_json(body)
    assert SENTINEL not in str(caught.value)


def test_for_request_keeps_only_what_was_asked():
    request = SignalsRequest((TextItem("u7", "x"),), (TaskRequest("pii", ("u7",)),
                                                      TaskRequest("nearest", ("u7",), examples=EXAMPLES),
                                                      TaskRequest("injection", ("u7",))),
                             request_id="ours")
    body = copy.deepcopy(PLAN_RESPONSE)
    body["request_id"] = "theirs"
    body["results"]["pii"]["texts"]["u9"] = {"entities": {}, "complete": True}   # never asked for
    del body["results"]["injection"]
    cut = SignalsResponse.from_json(body).for_request(request)
    assert set(cut.results) == {"pii", "nearest", "injection"}                    # language was not asked
    assert set(cut.result("pii").texts) == {"u7"}
    assert cut.result("injection") == TaskResult("error", error="missing from the response")
    assert cut.result("nearest").status == "ok"
    assert cut.request_id == "ours" and cut.models == SignalsResponse.from_json(body).models


@pytest.mark.parametrize("neighbour,routes", [
    ({"id": "code-999", "route": "local-code", "similarity": 0.9}, {}),
    ({"id": "code-001", "route": "local-general", "similarity": 0.9}, {}),
    ({"id": "code-001", "route": "local-code", "similarity": 0.9}, {SENTINEL: {"share": 1, "best": 0.9, "count": 1}}),
])
def test_for_request_refuses_neighbours_outside_the_example_set(neighbour, routes):
    request = SignalsRequest((TextItem("u7", "x"),), (TaskRequest("nearest", ("u7",), examples=EXAMPLES),))
    body = copy.deepcopy(PLAN_RESPONSE)
    body["results"]["nearest"]["texts"]["u7"] = {"neighbours": [neighbour], "routes": routes}
    cut = SignalsResponse.from_json(body).for_request(request)
    assert cut.result("nearest") == TaskResult("error", 15.2, {}, "neighbour not in the example set")
    assert SENTINEL not in repr(cut)
