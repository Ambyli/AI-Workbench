"""Per-type criterion options: validation, defaults, caps, and the request rules.

Everything here is model-level (``api.schemas`` / ``api.criterion_options``),
so it runs without the app. The HTTP layer's own refusals — the item cap,
the removed form fields — are in test_classifier_endpoint.py.

Run with::

    UV_LINK_MODE=copy uv run --no-sync --with pytest --package classifier \\
        python -m pytest unit-tests/classifier/test_criterion_options.py -q -p no:cacheprovider
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from api import criterion_options
from api.criterion_options import (
    CVOptions,
    DetectorOptions,
    LLMOptions,
    TextOptions,
    criterion_types,
)
from api.schemas import AssessRequest, CriterionInput, validation_message
from config import (
    CRITERION_NAME_MAX_CHARS,
    DETECTOR_MIN_SCORE,
    LLM_BBOX_MAX_ATTEMPTS,
    TEXT_MIN_COUNT_CAP,
)

DOC = {"type": "text", "data": "hello"}


def _request(*criteria):
    return AssessRequest.model_validate({"document": DOC, "criteria": list(criteria)})


def _error(*criteria) -> str:
    with pytest.raises(ValidationError) as exc:
        _request(*criteria)
    return validation_message(exc.value)


# ── Typed options per type ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "type_, model",
    [("llm", LLMOptions), ("text", TextOptions), ("cv", CVOptions), ("detector", DetectorOptions)],
)
def test_options_become_the_model_for_the_type(type_, model):
    c = CriterionInput(name="x", type=type_)
    assert isinstance(c.options, model)


def test_type_defaults_to_llm_and_options_resolve():
    c = CriterionInput(name="has a roof")
    assert c.type == "llm"
    assert c.resolved_options() == {
        "hint": "auto", "boxes": False, "max_attempts": LLM_BBOX_MAX_ATTEMPTS, "ocr": "auto",
        "aggregate": {"pages": "worst", "documents": "worst"},
    }


def test_text_pattern_defaults_to_the_name():
    c = CriterionInput(name="Notice to Owner", type="text")
    assert c.resolved_options()["pattern"] == "Notice to Owner"
    c = CriterionInput(name="n", type="text", options={"pattern": "NTO", "match": "fuzzy"})
    assert c.resolved_options()["pattern"] == "NTO"
    assert c.resolved_options()["fuzzy_threshold"] == 0.85


def test_detector_threshold_defaults_to_the_env_floor():
    assert CriterionInput(name="x", type="detector").resolved_options() == {
        "threshold": DETECTOR_MIN_SCORE, "aggregate": {"pages": "any", "documents": "all"},
    }
    assert CriterionInput(
        name="x", type="detector", options={"threshold": 0.5}
    ).resolved_options()["threshold"] == 0.5


def test_cv_fallback_default_follows_the_detector_configuration(monkeypatch):
    from detector import client as detector_client

    def fallback(**kw):
        return CriterionInput(name="has bicycle", type="cv", **kw).resolved_options()["fallback"]

    assert fallback() == "llm"
    monkeypatch.setattr(detector_client, "DETECTOR_URL", "http://detector:8000")
    assert fallback() == "detector"
    assert fallback(options={"fallback": "llm"}) == "llm"


def test_options_round_trip_through_a_payload():
    c = CriterionInput(name="x", type="text", options={"match": "regex", "pattern": r"\d+"})
    again = CriterionInput.model_validate(c.model_dump())
    assert again.resolved_options() == c.resolved_options()


# ── Refusals, per field ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "criterion, fragment",
    [
        ({"name": "x", "options": {"boxes": 1}}, "options.boxes: Input should be a valid boolean"),
        ({"name": "x", "options": {"hint": "vibes"}}, "options.hint"),
        ({"name": "x", "options": {"unknown": 1}}, "options.unknown: Extra inputs are not permitted"),
        ({"name": "x", "options": {"max_attempts": 0}}, "options.max_attempts"),
        ({"name": "x", "options": {"max_attempts": LLM_BBOX_MAX_ATTEMPTS + 1}},
         "exceeds this server's cap (CLASSIFIER_LLM_BBOX_MAX_ATTEMPTS="),
        ({"name": "x", "options": {"max_attempts": "2"}}, "valid integer"),
        ({"name": "x", "options": "boxes"}, "options must be an object"),
        ({"name": "x", "type": "text", "options": {"fuzzy_threshold": 1.5}}, "options.fuzzy_threshold"),
        ({"name": "x", "type": "text", "options": {"min_count": 0}}, "options.min_count"),
        ({"name": "x", "type": "text", "options": {"min_count": 5000}}, "options.min_count"),
        ({"name": "x", "type": "text", "options": {"pattern": "a" * 501}}, "options.pattern"),
        ({"name": "x", "type": "text", "options": {"match": "regex", "pattern": "(unclosed"}},
         "Invalid regular expression"),
        ({"name": "x", "type": "text", "options": {"hint": "presence"}}, "options.hint: Extra inputs"),
        ({"name": "x", "type": "cv", "options": {"fallback": "ocr"}}, "options.fallback"),
        ({"name": "x", "type": "detector", "options": {"threshold": -0.1}}, "options.threshold"),
        ({"name": "x", "type": "nope"}, "type"),
        ({"name": "x", "weight": 0}, "weight"),
        ({"name": ""}, "name"),
        ({"name": "x", "hint": "presence"}, "hint (options.hint on a llm criterion)"),
        ({"name": "x", "pattern": "y"}, "pattern (options.pattern on a text criterion)"),
        ({"name": "x", "regions": True}, "Extra inputs are not permitted"),
    ],
)
def test_bad_options_are_refused_with_the_field_named(criterion, fragment):
    assert fragment in _error(criterion)


# ── Cross-criterion rules ──────────────────────────────────────────────────


def test_duplicate_names_are_refused():
    assert "duplicate criterion name 'a'" in _error({"name": "a"}, {"name": "a", "type": "text"})


def test_unknown_dependency_is_refused():
    assert "depends_on 'ghost', which is not a criterion" in _error({"name": "a", "depends_on": "ghost"})


def test_self_dependency_is_refused():
    assert "depends on itself" in _error({"name": "a", "depends_on": "a"})


def test_cycles_are_refused():
    message = _error(
        {"name": "a", "depends_on": "c"},
        {"name": "b", "depends_on": "a"},
        {"name": "c", "depends_on": "b"},
    )
    assert "dependency cycle: a -> c -> b -> a" in message


def test_chains_are_fine():
    request = _request(
        {"name": "a", "type": "text"},
        {"name": "b", "type": "text", "depends_on": "a"},
        {"name": "c", "type": "text", "depends_on": "b"},
    )
    assert [c.depends_on for c in request.criteria] == [None, "a", "b"]


def test_depending_on_an_unscored_criterion_is_refused():
    message = _error(
        {"name": "where", "type": "text", "score": False},
        {"name": "b", "depends_on": "where"},
    )
    assert "has score: false" in message


@pytest.mark.parametrize(
    "criterion",
    [
        {"name": "has a roof", "type": "llm", "score": False},  # boxes off
        {"name": "has a roof", "type": "llm", "score": False,
         "options": {"hint": "quality", "boxes": True}},       # quality is not a place
        {"name": "sharpness", "type": "cv", "score": False},   # whole-page measurement
        {"name": "has bicycle", "type": "cv", "score": False,
         "options": {"fallback": "llm"}},                       # llm fallback, no boxes
    ],
)
def test_score_false_without_geometry_is_refused(criterion):
    assert "cannot produce any geometry" in _error(criterion)


@pytest.mark.parametrize(
    "criterion",
    [
        {"name": "has a roof", "type": "llm", "score": False, "options": {"boxes": True}},
        {"name": "Total", "type": "text", "score": False},
        {"name": "has sky", "type": "cv", "score": False},
        {"name": "has bicycle", "type": "cv", "score": False, "options": {"fallback": "detector"}},
        {"name": "has bicycle", "type": "detector", "score": False},
    ],
)
def test_score_false_with_geometry_is_accepted(criterion):
    assert _request(criterion).criteria[0].score is False


def test_empty_criteria_are_refused_and_omitted_ones_default():
    assert "at least 1 item" in _error()
    request = AssessRequest.model_validate({"document": DOC})
    assert [c.name for c in request.criteria][0] == "document legibility"
    assert request.criteria[0].options.hint == "quality"


def test_document_input_shape():
    with pytest.raises(ValidationError):
        AssessRequest.model_validate({"document": {"type": "file", "data": "x"}})
    with pytest.raises(ValidationError):
        AssessRequest.model_validate({"document": {"type": "text", "data": ""}})
    with pytest.raises(ValidationError):
        AssessRequest.model_validate({"document": DOC, "regions": True})


def test_a_single_document_is_a_one_item_list():
    request = AssessRequest.model_validate({"document": DOC})
    assert request.document is None
    assert [d.data for d in request.documents] == ["hello"]
    request = AssessRequest.model_validate({"documents": [DOC, {"type": "text", "data": "b"}]})
    assert [d.data for d in request.documents] == ["hello", "b"]


@pytest.mark.parametrize(
    "body, fragment",
    [
        ({"document": DOC, "documents": [DOC]}, "not both"),
        ({}, "no document"),
        ({"documents": []}, "at least 1 item"),
        ({"documents": [{"type": "text", "data": ""}]}, "documents.0.data"),
    ],
)
def test_document_list_refusals(body, fragment):
    with pytest.raises(ValidationError) as exc:
        AssessRequest.model_validate(body)
    assert fragment in validation_message(exc.value)


# ── options.aggregate and options.scope ────────────────────────────────────


@pytest.mark.parametrize(
    "criterion, expected",
    [
        ({"name": "x", "type": "llm", "options": {"hint": "presence"}}, ("any", "all")),
        ({"name": "x", "type": "llm", "options": {"hint": "quality"}}, ("worst", "worst")),
        ({"name": "x", "type": "llm"}, ("worst", "worst")),  # hint auto
        ({"name": "sharpness", "type": "cv"}, ("worst", "worst")),
        ({"name": "x", "type": "detector"}, ("any", "all")),
        ({"name": "x", "type": "text"}, ("sum", "sum")),
    ],
)
def test_aggregate_defaults_follow_the_table(criterion, expected):
    rules = CriterionInput.model_validate(criterion).aggregate_rules()
    assert (rules["pages"], rules["documents"]) == expected


def test_aggregate_a_string_sets_both_levels_and_an_object_sets_either():
    c = CriterionInput(name="x", options={"aggregate": "mean"})
    assert c.aggregate_rules() == {"pages": "mean", "documents": "mean"}
    c = CriterionInput(name="x", options={"hint": "presence", "aggregate": {"documents": "any"}})
    assert c.aggregate_rules() == {"pages": "any", "documents": "any"}
    c = CriterionInput(name="x", options={"aggregate": {"pages": "all"}})
    assert c.aggregate_rules() == {"pages": "all", "documents": "worst"}  # all is kept as sent
    assert c.resolved_options()["aggregate"] == c.aggregate_rules()


@pytest.mark.parametrize("type_", ["llm", "cv", "detector"])
def test_sum_is_refused_off_text(type_):
    for agg in ("sum", {"pages": "sum"}, {"documents": "sum"}):
        message = _error({"name": "has sky", "type": type_, "options": {"aggregate": agg}})
        assert "aggregate 'sum' adds text hit counts and is only for text criteria" in message


def test_sum_is_fine_on_text():
    c = CriterionInput(
        name="x", type="text", options={"aggregate": {"documents": "sum", "pages": "any"}}
    )
    assert c.aggregate_rules() == {"pages": "any", "documents": "sum"}


@pytest.mark.parametrize(
    "agg, fragment",
    [
        ("median", "aggregate must be one of any, worst, all, mean, sum"),
        ({"page": "any"}, "unknown level(s) ['page']"),
        ({"pages": "best"}, "got pages: 'best'"),
        (3, "got int"),
    ],
)
def test_bad_aggregates_are_refused(agg, fragment):
    assert fragment in _error({"name": "x", "options": {"aggregate": agg}})


def test_text_scope():
    assert CriterionInput(name="x", type="text").scope() == "page"
    c = CriterionInput(name="x", type="text", options={"scope": "document"})
    assert c.scope() == "document" and c.resolved_options()["scope"] == "document"
    assert CriterionInput(name="x", type="llm").scope() == "page"
    assert "options.scope" in _error({"name": "x", "type": "text", "options": {"scope": "book"}})
    assert "options.scope: Extra inputs" in _error({"name": "x", "options": {"scope": "document"}})


# ── GET /criterion-types' payload ──────────────────────────────────────────


def test_criterion_types_payload():
    payload = criterion_types()
    assert set(payload["types"]) == set(criterion_options.OPTIONS_MODELS)
    for type_, entry in payload["types"].items():
        assert entry["options_schema"]["additionalProperties"] is False, type_
        assert set(entry["defaults"]) <= set(entry["options_schema"]["properties"]), type_
    assert payload["types"]["text"]["defaults"]["pattern"] == "<name>"
    assert payload["types"]["text"]["caps"] == {
        "pattern_max_chars": 500, "min_count": TEXT_MIN_COUNT_CAP
    }
    assert payload["types"]["llm"]["caps"] == {"max_attempts": LLM_BBOX_MAX_ATTEMPTS}
    assert payload["types"]["text"]["defaults"]["aggregate"] == {"pages": "sum", "documents": "sum"}
    assert payload["types"]["text"]["defaults"]["scope"] == "page"
    assert payload["aggregate"]["rules"]["all"] == "alias of worst"
    assert payload["aggregate"]["defaults"]["llm (hint presence)"] == {
        "pages": "any", "documents": "all"
    }


# ── The two input caps are config knobs ────────────────────────────────────


def test_criterion_name_cap_is_the_config_knob():
    CriterionInput(name="x" * CRITERION_NAME_MAX_CHARS, type="llm")
    with pytest.raises(ValidationError):
        CriterionInput(name="x" * (CRITERION_NAME_MAX_CHARS + 1), type="llm")
    assert f"1-{CRITERION_NAME_MAX_CHARS} characters" in criterion_types()["shared_fields"]["name"]


def test_text_min_count_cap_is_the_config_knob():
    ok = {"min_count": TEXT_MIN_COUNT_CAP}
    CriterionInput(name="total", type="text", options=ok)
    with pytest.raises(ValidationError):
        CriterionInput(name="total", type="text", options={"min_count": TEXT_MIN_COUNT_CAP + 1})


# ── options.reference ──────────────────────────────────────────────────────


def test_reference_resolves_only_when_sent():
    from config import REFERENCE_POSITION_MIN_IOU

    plain = CriterionInput(name="has a roof").resolved_options()
    assert "reference" not in plain
    guided = CriterionInput(name="has a roof", options={"reference": {}}).resolved_options()
    assert guided["reference"] == {
        "use": True, "criterion": "has a roof", "position": "off",
        "min_iou": REFERENCE_POSITION_MIN_IOU, "combine": "any",
    }
    assert {k: v for k, v in guided.items() if k != "reference"} == plain
    cv = CriterionInput(name="has a house", type="cv",
                        options={"reference": {"criterion": "a house", "combine": "mean"}})
    assert cv.resolved_options()["reference"]["criterion"] == "a house"


@pytest.mark.parametrize("criterion, fragment", [
    ({"name": "x", "type": "text", "options": {"reference": {}}}, "references guide the vision model"),
    ({"name": "x", "type": "detector", "options": {"reference": {}}}, "references guide the vision model"),
    ({"name": "sharpness", "type": "cv", "options": {"reference": {}}}, "OpenCV detector"),
    ({"name": "has a house", "type": "cv", "options": {"fallback": "detector", "reference": {}}},
     "the detector service"),
    ({"name": "x", "options": {"reference": {"position": "check"}}}, "needs options.boxes: true"),
    ({"name": "x", "options": {"hint": "quality", "boxes": True,
                               "reference": {"position": "check"}}}, "needs options.boxes: true"),
    ({"name": "x", "type": "cv", "options": {"reference": {"position": "check"}}},
     "llm fallback runs without boxes"),
    ({"name": "x", "options": {"reference": {"combine": "sum"}}}, "combine"),
    ({"name": "x", "options": {"reference": {"min_iou": 2}}}, "min_iou"),
    ({"name": "x", "options": {"reference": {"guide": True}}}, "guide"),
])
def test_bad_references_are_refused(criterion, fragment):
    with pytest.raises(ValidationError) as exc:
        CriterionInput.model_validate(criterion)
    assert fragment in validation_message(exc.value)


def test_position_check_with_boxes_is_accepted():
    c = CriterionInput(name="x", options={"hint": "presence", "boxes": True,
                                          "reference": {"position": "check", "min_iou": 0.5}})
    assert c.reference_options()["position"] == "check"
    assert c.reference_options()["min_iou"] == 0.5


def test_criterion_types_has_a_reference_block():
    from config import REFERENCE_MAX_PER_CRITERION, VISION_LLM_MAX_IMAGES_PER_PROMPT

    block = criterion_types()["reference"]
    assert block["options_schema"]["additionalProperties"] is False
    assert set(block["defaults"]) == {"use", "criterion", "position", "min_iou", "combine"}
    assert block["caps"]["max_per_criterion"] == REFERENCE_MAX_PER_CRITERION
    assert block["caps"]["images_per_llm_prompt"] == VISION_LLM_MAX_IMAGES_PER_PROMPT
    assert "reference" in criterion_types()["types"]["llm"]["options_schema"]["properties"]
    assert "reference" not in criterion_types()["types"]["text"]["options_schema"]["properties"]
