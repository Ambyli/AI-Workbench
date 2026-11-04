"""The box loop's per-criterion options (ZT-527).

An ``llm`` criterion with ``options.boxes: true`` may set, beside ``boxes``
and ``max_attempts``:

  * ``refine``: ``"grid"`` | ``"marks"`` (service default
    CLASSIFIER_LLM_BBOX_REFINE_MODE),
  * ``verify_partial``: bool (CLASSIFIER_LLM_BBOX_VERIFY_PARTIAL),
  * ``verify_min_span``: 0-1 (CLASSIFIER_LLM_BBOX_VERIFY_MIN_SPAN),

and under ``options.reference``:

  * ``boxes``: ``{"ask", "refine", "verify"}`` bools
    (CLASSIFIER_REFERENCE_BOX_ASK / _REFINE / _VERIFY),
  * ``text``: bool (CLASSIFIER_REFERENCE_BOX_TEXT).

What is pinned here, model-level and through the pipeline with a scripted
model (no app, no database):

  * validation — each key accepted with ``boxes: true``; each refusal and
    its message (no ``boxes``, a span outside 0-1, wrong types, unknown
    keys, ``text`` / ``detector`` / ``cv`` criteria, ``options.reference``
    without ``references``);
  * precedence — a value on the criterion beats the service default; unset
    takes the default, with the env patched on and off (patched where the
    service reads it, ``api.criterion_options``);
  * ``options_used`` shows the values that ran, and ``GET
    /criterion-types`` lists the keys and this container's defaults;
  * plumbing — ``analysis.llm_eval`` hands ``llm.boxes.locate_criterion``
    the criterion's resolved values (two criteria on one page run the loop
    two ways), and a criterion's own ``refine: "marks"`` runs the marks
    refine with the service default ``grid``;
  * compatibility — a payload, a stored reference's ``input`` and a
    resolved-options dict from before these keys run exactly as today, and
    the reference-inheritance comparison (``references.resolve._same``).

The reference-guided half through the app (``reference.boxes`` showing the
examples per criterion, ``reference.text`` reaching the captions) is in
``test_references_assess.py``; the loop's behaviour with each setting on is
``test_llm_box_marks.py``'s.

Run with::

    UV_LINK_MODE=copy uv run --no-sync --with pytest --package classifier \\
        python -m pytest unit-tests/classifier/test_box_loop_options.py -q -p no:cacheprovider
"""

from __future__ import annotations

import asyncio
import base64
import copy

import pytest
from pydantic import ValidationError

from api import criterion_options
from api.criterion_options import box_loop_settings, criterion_types
from api.schemas import AssessRequest, CriterionInput, validation_message
from config import LLM_BBOX_MARKS_MAX
from llm import boxes as llm_boxes
from llm import client as llm_client
from test_llm_boxes import ScriptedModel, _analyze, _entry, _png_bytes, _scoring_call

DOC = {"type": "text", "data": "hello"}
NAME = "has solar panels"
REF = "r000000000000"
LOOP_KEYS = ("refine", "verify_partial", "verify_min_span")
# The service defaults, as the service reads them (api.criterion_options).
DEFAULTS = ("LLM_BBOX_REFINE_MODE", "LLM_BBOX_VERIFY_PARTIAL", "LLM_BBOX_VERIFY_MIN_SPAN",
            "REFERENCE_BOX_ASK", "REFERENCE_BOX_REFINE", "REFERENCE_BOX_VERIFY",
            "REFERENCE_BOX_TEXT")


def _criterion(**options) -> dict:
    return {"name": NAME, "type": "llm", "options": {"hint": "presence", **options}}


def _request(*criteria, references=None) -> AssessRequest:
    body = {"document": DOC, "criteria": list(criteria)}
    if references is not None:
        body["references"] = references
    return AssessRequest.model_validate(body)


def _error(*criteria, references=None) -> str:
    with pytest.raises(ValidationError) as exc:
        _request(*criteria, references=references)
    return validation_message(exc.value)


@pytest.fixture
def service_on(monkeypatch):
    """Every service default ON (the env set on the box), where the service
    reads it."""
    for name, value in zip(DEFAULTS, ("marks", True, 0.08, True, True, True, True)):
        monkeypatch.setattr(criterion_options, name, value)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("options", [
    {"refine": "marks"}, {"refine": "grid"},
    {"verify_partial": True}, {"verify_partial": False},
    {"verify_min_span": 0}, {"verify_min_span": 1}, {"verify_min_span": 0.08},
    {"refine": "marks", "verify_partial": True, "verify_min_span": 0.08},
    {"reference": {"boxes": {"ask": True}}},
    {"reference": {"boxes": {}}},
    {"reference": {"boxes": {"ask": False, "refine": True, "verify": True}}},
    {"reference": {"text": True}},
    {"reference": {"boxes": {"ask": True}, "text": False, "position": "check"}},
    {"refine": "marks", "reference": {"boxes": {"ask": True, "refine": True}, "text": True}},
])
def test_each_loop_option_is_accepted_with_boxes(options):
    request = _request(_criterion(boxes=True, **options), references=[REF])
    resolved = request.criteria[0].resolved_options()
    for key in LOOP_KEYS:
        if key in options:
            assert resolved[key] == options[key]
    sent = options.get("reference", {})
    for step, value in (sent.get("boxes") or {}).items():
        assert resolved["reference"]["boxes"][step] is value
    if "text" in sent:
        assert resolved["reference"]["text"] is sent["text"]


def test_the_refusal_without_boxes_names_every_key_sent():
    assert _error(_criterion(refine="marks")) == (
        "criteria.0: options: refine tunes the bounding-box loop, which runs only with "
        "options.boxes: true; set boxes: true or drop it")
    assert _error(
        _criterion(refine="marks", verify_partial=True, verify_min_span=0.1,
                   reference={"boxes": {"ask": True}, "text": True}),
        references=[REF],
    ) == (
        "criteria.0: options: refine, verify_partial, verify_min_span, reference.boxes, "
        "reference.text tune the bounding-box loop, which runs only with options.boxes: "
        "true; set boxes: true or drop them")
    # Explicitly false is still "set": the loop is not there to tune.
    assert "verify_partial tunes the bounding-box loop" in _error(
        _criterion(boxes=False, verify_partial=False))
    assert "reference.boxes tunes" in _error(
        _criterion(reference={"boxes": {}}), references=[REF])


@pytest.mark.parametrize("criterion, fragment", [
    (_criterion(boxes=True, verify_min_span=1.5),
     "options.verify_min_span: Input should be less than or equal to 1"),
    (_criterion(boxes=True, verify_min_span=-0.1),
     "options.verify_min_span: Input should be greater than or equal to 0"),
    (_criterion(boxes=True, verify_min_span="0.1"), "options.verify_min_span: Input should be a "
                                                    "valid number"),
    (_criterion(boxes=True, refine="Marks"), "options.refine: Input should be 'grid' or 'marks'"),
    (_criterion(boxes=True, refine="zoom"), "options.refine: Input should be 'grid' or 'marks'"),
    (_criterion(boxes=True, verify_partial="yes"),
     "options.verify_partial: Input should be a valid boolean"),
    (_criterion(boxes=True, reference={"boxes": {"ask": 1}}),
     "options.reference.boxes.ask: Input should be a valid boolean"),
    (_criterion(boxes=True, reference={"boxes": {"zoom": True}}),
     "options.reference.boxes.zoom: Extra inputs are not permitted"),
    (_criterion(boxes=True, reference={"boxes": True}), "options.reference.boxes"),
    (_criterion(boxes=True, reference={"text": "yes"}),
     "options.reference.text: Input should be a valid boolean"),
    # Not on the types that never run the loop: an unknown key, as hint is.
    ({"name": "Total", "type": "text", "options": {"refine": "marks"}},
     "options.refine: Extra inputs are not permitted"),
    ({"name": "has bicycle", "type": "detector", "options": {"verify_partial": True}},
     "options.verify_partial: Extra inputs are not permitted"),
    ({"name": "has a house", "type": "cv", "options": {"verify_min_span": 0.1}},
     "options.verify_min_span: Extra inputs are not permitted"),
    # options.reference's loop keys: refused by name on cv (its llm fallback
    # never locates), and on text / detector with the whole of reference.
    ({"name": "has a house", "type": "cv", "options": {"reference": {"boxes": {"ask": True}}}},
     "options: reference.boxes tunes the bounding-box loop, and a cv criterion's llm fallback "
     "runs without boxes; use an llm criterion with options.boxes: true"),
    ({"name": "has a house", "type": "cv", "options": {"fallback": "llm",
                                                       "reference": {"text": True}}},
     "reference.text tunes the bounding-box loop, and a cv criterion's llm fallback"),
    ({"name": "Total", "type": "text", "options": {"reference": {"boxes": {}}}},
     "references guide the vision model"),
])
def test_bad_loop_options_are_refused_with_the_field_named(criterion, fragment):
    assert fragment in _error(criterion, references=[REF])


def test_reference_loop_keys_without_references_are_refused():
    message = _error(_criterion(boxes=True, reference={"boxes": {"ask": True}, "text": True}))
    assert message == (
        f"options.reference on ['{NAME}'] but the request lists no `references`")


# ---------------------------------------------------------------------------
# Precedence: the criterion's value, else the service default
# ---------------------------------------------------------------------------


def _resolved(**options) -> dict:
    return CriterionInput.model_validate(_criterion(boxes=True, **options)).resolved_options()


def test_unset_takes_the_service_defaults_off():
    resolved = _resolved(reference={})
    assert (resolved["refine"], resolved["verify_partial"], resolved["verify_min_span"]) == (
        "grid", False, 0.0)
    assert resolved["reference"]["boxes"] == {"ask": False, "refine": False, "verify": False}
    assert resolved["reference"]["text"] is False


def test_unset_takes_the_service_defaults_on(service_on):
    resolved = _resolved(reference={})
    assert (resolved["refine"], resolved["verify_partial"], resolved["verify_min_span"]) == (
        "marks", True, 0.08)
    assert resolved["reference"]["boxes"] == {"ask": True, "refine": True, "verify": True}
    assert resolved["reference"]["text"] is True


def test_a_criterion_value_beats_the_service_default_on(service_on):
    resolved = _resolved(refine="grid", verify_partial=False, verify_min_span=0,
                         reference={"boxes": {"ask": False, "refine": False, "verify": False},
                                    "text": False})
    assert (resolved["refine"], resolved["verify_partial"], resolved["verify_min_span"]) == (
        "grid", False, 0.0)
    assert resolved["reference"]["boxes"] == {"ask": False, "refine": False, "verify": False}
    assert resolved["reference"]["text"] is False


def test_a_criterion_value_beats_the_service_default_off():
    resolved = _resolved(refine="marks", verify_partial=True, verify_min_span=0.1,
                         reference={"boxes": {"ask": True, "verify": True}, "text": True})
    assert (resolved["refine"], resolved["verify_partial"], resolved["verify_min_span"]) == (
        "marks", True, 0.1)
    # A step left out of reference.boxes is still the service default.
    assert resolved["reference"]["boxes"] == {"ask": True, "refine": False, "verify": True}
    assert resolved["reference"]["text"] is True


def test_a_partial_reference_boxes_mixes_with_the_defaults(monkeypatch):
    monkeypatch.setattr(criterion_options, "REFERENCE_BOX_REFINE", True)
    resolved = _resolved(reference={"boxes": {"ask": True, "verify": False}})
    assert resolved["reference"]["boxes"] == {"ask": True, "refine": True, "verify": False}


@pytest.mark.parametrize("env, shown", [("marks", "marks"), ("grid", "grid"),
                                        ("grid-ish", "grid"), ("", "grid")])
def test_the_refine_default_reads_anything_but_marks_as_grid(monkeypatch, env, shown):
    """As llm.boxes reads the env var — options_used never shows a mode the
    loop would not run."""
    monkeypatch.setattr(criterion_options, "LLM_BBOX_REFINE_MODE", env)
    assert _resolved()["refine"] == shown


def test_box_loop_settings_follow_the_resolved_options(service_on):
    """The keywords llm_eval passes: the criterion's, else the defaults —
    including for a criterion that sent no options.reference at all."""
    assert box_loop_settings(_resolved(refine="grid", reference={"boxes": {"ask": False}})) == {
        "refine_mode": "grid", "verify_partial": True, "verify_min_span": 0.08,
        "guide_ask": False, "guide_refine": True, "guide_verify": True, "reference_text": True,
    }
    assert box_loop_settings(_resolved()) == box_loop_settings({}) == {
        "refine_mode": "marks", "verify_partial": True, "verify_min_span": 0.08,
        "guide_ask": True, "guide_refine": True, "guide_verify": True, "reference_text": True,
    }


# ---------------------------------------------------------------------------
# What options_used and GET /criterion-types show
# ---------------------------------------------------------------------------


def test_options_used_shows_the_values_that_ran(monkeypatch):
    result, _ = _analyze(monkeypatch, CriterionInput.model_validate(
        _criterion(boxes=True, refine="marks", verify_min_span=0.1)), answers=[])
    used = _entry(result)["options_used"]
    assert list(used) == ["hint", "boxes", "max_attempts", "refine", "verify_partial",
                          "verify_min_span", "ocr", "aggregate"]
    assert (used["refine"], used["verify_partial"], used["verify_min_span"]) == (
        "marks", False, 0.1)
    # The request block keeps what was SENT (unset is null), options_used
    # what ran.
    sent = result["request"]["criteria"][0]["options"]
    assert (sent["refine"], sent["verify_partial"], sent["verify_min_span"]) == (
        "marks", None, 0.1)


def test_criterion_types_lists_the_keys_and_this_containers_defaults(service_on):
    payload = criterion_types()
    llm = payload["types"]["llm"]
    for key in LOOP_KEYS:
        assert key in llm["options_schema"]["properties"]
        assert key in llm["defaults"]
    assert (llm["defaults"]["refine"], llm["defaults"]["verify_partial"],
            llm["defaults"]["verify_min_span"]) == ("marks", True, 0.08)
    for type_ in ("text", "cv", "detector"):
        assert not set(LOOP_KEYS) & set(payload["types"][type_]["options_schema"]["properties"])
    reference = payload["reference"]
    assert {"boxes", "text"} <= set(reference["options_schema"]["properties"])
    assert reference["defaults"]["boxes"] == {"ask": True, "refine": True, "verify": True}
    loop = payload["box_loop"]
    assert set(loop["options"]) == {*LOOP_KEYS, "reference.boxes", "reference.text"}
    assert loop["service_defaults"] == {
        "refine": "marks", "verify_partial": True, "verify_min_span": 0.08,
        "reference.boxes": {"ask": True, "refine": True, "verify": True},
        "reference.text": True,
    }
    assert loop["marks_max"] == LLM_BBOX_MARKS_MAX


# ---------------------------------------------------------------------------
# Plumbing: llm_eval hands the loop the criterion's own values
# ---------------------------------------------------------------------------


@pytest.fixture
def loop_calls(monkeypatch):
    """Record the keywords every ``locate_criterion`` call receives, then run
    the real loop."""
    calls: dict[str, dict] = {}
    real = llm_boxes.locate_criterion

    async def recording(name, **kwargs):
        calls[name] = kwargs
        return await real(name, **kwargs)

    monkeypatch.setattr(llm_boxes, "locate_criterion", recording)
    return calls


def _loop_keywords(kwargs: dict) -> dict:
    return {key: kwargs[key] for key in (
        "refine_mode", "verify_partial", "verify_min_span",
        "guide_ask", "guide_refine", "guide_verify")}


@pytest.mark.parametrize("on", [False, True], ids=["service-off", "service-on"])
def test_llm_eval_passes_the_service_defaults_for_unset_options(monkeypatch, loop_calls, on):
    if on:
        for name, value in zip(DEFAULTS, ("marks", True, 0.08, True, True, True, True)):
            monkeypatch.setattr(criterion_options, name, value)
    _analyze(monkeypatch, CriterionInput.model_validate(_criterion(boxes=True)),
             answers=[{"bbox": [100, 200, 300, 400]}, {"score": 9}])
    assert _loop_keywords(loop_calls[NAME]) == (
        {"refine_mode": "marks", "verify_partial": True, "verify_min_span": 0.08,
         "guide_ask": True, "guide_refine": True, "guide_verify": True} if on else
        {"refine_mode": "grid", "verify_partial": False, "verify_min_span": 0.0,
         "guide_ask": False, "guide_refine": False, "guide_verify": False})


def test_llm_eval_passes_the_criterions_values_over_the_defaults(
    monkeypatch, loop_calls, service_on
):
    _analyze(monkeypatch, CriterionInput.model_validate(_criterion(
        boxes=True, refine="grid", verify_partial=False, verify_min_span=0.2)),
        answers=[{"bbox": [100, 200, 300, 400]}, {"score": 9}])
    assert _loop_keywords(loop_calls[NAME]) == {
        "refine_mode": "grid", "verify_partial": False, "verify_min_span": 0.2,
        # No options.reference sent: the reference steps are the defaults.
        "guide_ask": True, "guide_refine": True, "guide_verify": True,
    }


def test_two_criteria_on_one_page_run_the_loop_two_ways(monkeypatch, loop_calls):
    """An amount due by marks with a taller verify crop; a chart on the grid,
    both on one page, one job."""
    import analysis

    monkeypatch.setattr(llm_client, "call_model", _scoring_call(10))
    monkeypatch.setattr(llm_client, "call_model_json", ScriptedModel(
        [{"bbox": [100, 200, 300, 400]}, {"score": 9}] * 2))
    monkeypatch.setattr(llm_boxes, "LLM_BBOX_REFINE", False)
    monkeypatch.setattr(llm_boxes, "LLM_BBOX_GRIDLINES", False)
    criteria = [
        CriterionInput.model_validate({"name": "the amount due", "options": {
            "hint": "presence", "boxes": True, "refine": "marks", "verify_partial": True,
            "verify_min_span": 0.08}}),
        CriterionInput.model_validate({"name": "the monthly usage chart", "options": {
            "hint": "presence", "boxes": True}}),
    ]
    doc = analysis.load_document_bytes(_png_bytes(), "page.png", "image/png", keep_source=True)
    result = asyncio.run(analysis.analyze_document(doc, criteria))
    assert _loop_keywords(loop_calls["the amount due"]) == {
        "refine_mode": "marks", "verify_partial": True, "verify_min_span": 0.08,
        "guide_ask": False, "guide_refine": False, "guide_verify": False}
    assert _loop_keywords(loop_calls["the monthly usage chart"]) == {
        "refine_mode": "grid", "verify_partial": False, "verify_min_span": 0.0,
        "guide_ask": False, "guide_refine": False, "guide_verify": False}
    entries = result["assessment"]["per_criterion_scores"]
    assert entries["the amount due"]["options_used"]["refine"] == "marks"
    assert entries["the monthly usage chart"]["options_used"]["refine"] == "grid"


def test_a_criterions_own_marks_refine_runs_with_the_service_on_grid(monkeypatch):
    """The service default stays grid; the criterion's options.refine: marks
    numbers its own text layer's lines — the same path test_llm_box_marks.py
    drives through the env."""
    import analysis
    from analysis import context

    from test_llm_box_marks import FakeEngine

    assert criterion_options.LLM_BBOX_REFINE_MODE == "grid"
    engine = FakeEngine([("Amount due", (40, 100, 200, 130)), ("$193.33", (40, 140, 160, 170))])
    monkeypatch.setattr(context, "get_ocr_engine", lambda: engine)
    monkeypatch.setattr(llm_client, "call_model", _scoring_call(10))
    model = ScriptedModel([{"bbox": [50, 100, 450, 350]}, {"marks": [1, 2]}, {"score": 9}])
    monkeypatch.setattr(llm_client, "call_model_json", model)
    monkeypatch.setattr(llm_boxes, "LLM_BBOX_REFINE", True)
    monkeypatch.setattr(llm_boxes, "LLM_BBOX_GRIDLINES", False)
    criterion = CriterionInput.model_validate(_criterion(boxes=True, ocr="always",
                                                         refine="marks"))
    doc = analysis.load_document_bytes(_png_bytes(), "page.png", "image/png", keep_source=True)
    result = asyncio.run(analysis.analyze_document(doc, [criterion]))
    assert model.labels() == [f"bbox/{NAME}#1", f"refine/marks/{NAME}#1", f"verify/{NAME}#1"]
    attempt = _entry(result)["localization"]["attempts"][0]
    assert attempt["refine_mode"] == "marks" and attempt["marks"] == [1, 2]
    assert attempt["bbox_px"] == [40.0, 100.0, 200.0, 170.0]


def test_a_criterions_verify_partial_reaches_the_verify_prompt(monkeypatch):
    result, model = _analyze(
        monkeypatch, CriterionInput.model_validate(_criterion(boxes=True, verify_partial=True)),
        answers=[{"bbox": [100, 200, 300, 400]}, {"score": 9, "partial": False}])
    assert '"partial": false' in model.last_text(1)
    assert _entry(result)["localization"]["attempts"][0]["verify_partial"] is False


# ---------------------------------------------------------------------------
# Compatibility: before the keys existed
# ---------------------------------------------------------------------------


def _strip_loop_keys(criterion: dict) -> dict:
    """A criterion dump as the previous container wrote it: no loop keys,
    no reference.boxes / .text."""
    old = copy.deepcopy(criterion)
    options = old.get("options") or {}
    for key in LOOP_KEYS:
        options.pop(key, None)
    if isinstance(options.get("reference"), dict):
        options["reference"].pop("boxes", None)
        options["reference"].pop("text", None)
    return old


def _run_payload(monkeypatch, payload: dict) -> tuple[dict, ScriptedModel]:
    from jobs.runners import run_assess

    monkeypatch.setattr(llm_client, "call_model", _scoring_call(10))
    model = ScriptedModel([{"bbox": [0, 0, 1000, 1000]}, {"bbox": [100, 200, 300, 400]},
                           {"score": 3, "reason": "tiles"}, {"bbox": [400, 200, 600, 400]},
                           {"score": 9, "reason": "panels"}])
    monkeypatch.setattr(llm_client, "call_model_json", model)
    monkeypatch.setattr(llm_boxes, "LLM_BBOX_REFINE", False)
    return asyncio.run(run_assess(payload)), model


def test_an_old_payload_runs_exactly_as_a_new_one_with_nothing_set(monkeypatch):
    from jobs.payloads import SubmittedDocument, build_assess_payload

    request = AssessRequest.model_validate({
        "documents": [{"type": "base64", "data": base64.b64encode(_png_bytes()).decode()}],
        "criteria": [_criterion(boxes=True)],
    })
    document = SubmittedDocument(raw=_png_bytes(), filename="page.png",
                                 content_type="image/png", kind="image", pages=1)
    new = build_assess_payload(request, [document])
    # The new dump carries the keys, unset (null); the old one has none. No
    # schema bump: both are schema 3.
    assert all(new["criteria"][0]["options"][key] is None for key in LOOP_KEYS)
    old = {**new, "criteria": [_strip_loop_keys(c) for c in new["criteria"]]}
    assert not set(LOOP_KEYS) & set(old["criteria"][0]["options"])
    assert old["schema"] == new["schema"] == 3

    old_result, old_model = _run_payload(monkeypatch, old)
    new_result, new_model = _run_payload(monkeypatch, new)
    assert old_model.calls == new_model.calls                 # every prompt, in order
    old_entry, new_entry = _entry(old_result), _entry(new_result)
    assert old_entry["localization"] == new_entry["localization"]
    assert old_entry["localization"]["accepted_attempt"] == 3
    assert old_entry["options_used"] == new_entry["options_used"]
    assert (old_entry["score"], old_entry["verdict"]) == (new_entry["score"],
                                                          new_entry["verdict"])
    # Today's attempt record: none of the opt-in keys.
    keys = {k for a in new_entry["localization"]["attempts"] for k in a}
    assert not keys & {"refine_mode", "marks", "marks_shown", "marks_fallback",
                       "verify_partial"}


def test_resolved_options_from_before_the_keys_take_the_service_defaults(service_on):
    """A dict without the loop's keys — options resolved by an older build,
    and a reference block without boxes / text (an old plan's) — runs on the
    service defaults, the same as an unset key."""
    old = {"hint": "presence", "boxes": True, "max_attempts": 3, "ocr": "auto",
           "reference": {"use": True, "criterion": NAME, "position": "off", "min_iou": 0.3,
                         "combine": "any"}}
    assert box_loop_settings(old) == box_loop_settings({}) == box_loop_settings(
        _resolved(reference={}))


def test_a_stored_reference_input_without_the_keys_is_the_same_criterion(monkeypatch):
    """``references.resolve._same`` compares RESOLVED options: an input from
    before the keys, one that leaves them unset (null) and one set to the
    service default are one criterion; one that would run the loop
    differently is a conflict — until the default changes to match."""
    from references.resolve import _same

    sent = _criterion(boxes=True, reference={"combine": "any"})
    new = CriterionInput.model_validate(sent)
    stored = new.model_dump(mode="json")                  # what a new record's input holds
    assert stored["options"]["refine"] is None
    assert stored["options"]["reference"]["boxes"] is None
    old = CriterionInput.model_validate(_strip_loop_keys(stored))
    assert old.resolved_options() == new.resolved_options()
    assert _same(old, new)

    default = CriterionInput.model_validate(_criterion(
        boxes=True, refine="grid", verify_partial=False,
        reference={"combine": "any", "boxes": {"ask": False}, "text": False}))
    marks = CriterionInput.model_validate(_criterion(
        boxes=True, refine="marks", reference={"combine": "any"}))
    assert _same(old, default)
    assert not _same(old, marks)
    monkeypatch.setattr(criterion_options, "LLM_BBOX_REFINE_MODE", "marks")
    assert _same(old, marks) and not _same(old, default)
