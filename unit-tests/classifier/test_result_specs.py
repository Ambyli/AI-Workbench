"""Every criterion type's ``detail`` matches its declaration.

Each type's ``detail`` shape is declared beside its evaluator
(``@result_spec`` in ``analysis.llm_eval`` / ``text_eval`` / ``detector_eval``
/ ``cv_eval``) and registered by type in ``analysis.result_specs``;
``GET /criterion-types`` serves it as each type's ``result`` block. This file
runs real jobs through ``analysis.analyze_document`` — every type, the edge
paths (no text to search, a detector that finds nothing, ``score: false``),
and multi-document aggregates under every rule — and checks, for the
criterion AND for each of its per-unit ``items`` entries:

  * no key outside the declaration; every always-present key present;
  * ``metric`` / ``value`` follow the declared rule (a detail key for text and
    detector, a measurement for cv, the score for llm — null under
    ``score: false``);
  * an aggregate carries exactly the ``aggregate_detail`` block, and a
    ``mean`` keeps only the type's stable keys.

The model and the detector are scripted; OCR is off (text comes from .txt
documents), so the file runs in a few seconds.
"""

from __future__ import annotations

import asyncio
import json

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

import analysis
from analysis.result_specs import AGGREGATE_BLOCK, specs
from api.schemas import CriterionInput
from llm import client as llm_client


def _png(value=128, w=400, h=300) -> bytes:
    return cv2.imencode(".png", np.full((h, w, 3), value, dtype=np.uint8))[1].tobytes()


def _doc(raw: bytes, name: str):
    return analysis.load_document_bytes(raw, name, None, keep_source=True)


def _run(docs, criteria):
    return asyncio.run(analysis.analyze_document(docs, criteria))


def _entries(result):
    return result["assessment"]["per_criterion_scores"]


@pytest.fixture
def model(monkeypatch):
    async def send(prompt):
        return {"choices": [{"message": {"content": json.dumps(
            {"score": 8, "verdict": "PASS", "confidence": 80, "reason": "I see it."}
        )}}]}

    monkeypatch.setattr(llm_client, "_send", send)


@pytest.fixture
def detector(monkeypatch):
    """A stub detector that finds a box for 'has bicycle' and nothing else."""
    from common.vision import Region
    from detector import client as detector_client

    monkeypatch.setattr(detector_client, "DETECTOR_URL", "http://stub")

    async def detect_page(image, labels, geometry, *, min_score, stats):
        stats.calls += 1
        return {
            label: ([Region(page=geometry.page if geometry else 0, kind="box",
                            points=[(1, 1), (50, 50)], label=label, score=0.8,
                            source="detector")] if label == "has bicycle" else [])
            for label in labels
        }

    monkeypatch.setattr(detector_client, "detect_page", detect_page)


def _check(detail, method: str, *, score=None, scored=True, aggregated=False):
    """Assert one ``detail`` matches its type's declaration."""
    spec = specs()[method]
    assert isinstance(detail, dict), detail
    keys = set(detail)
    assert keys <= set(spec.fields), (method, sorted(keys - set(spec.fields)))
    required = spec.required()
    if aggregated and detail.get("aggregate", {}).get("rule") == "mean":
        # A mean keeps only the stable keys, plus metric / value / aggregate.
        assert keys <= spec.stable() | {"metric", "value", "aggregate"}, (method, keys)
    else:
        assert required <= keys, (method, sorted(required - keys))
    if method != "cv":
        assert detail["metric"] == spec.metric
    if method in ("text", "detector") and "aggregate" not in detail or (
        method in ("text", "detector") and detail["aggregate"]["rule"] in ("any", "worst", "all", "sum")
    ):
        assert detail["value"] == detail[spec.metric]
    if method == "cv" and "measurements" in detail:
        assert detail["value"] == detail["measurements"][detail["metric"]]
    if method == "llm" and not aggregated:
        assert detail["value"] == (score if scored else None)
    if aggregated:
        assert set(detail["aggregate"]) == set(AGGREGATE_BLOCK)
    else:
        assert "aggregate" not in detail


def _check_criterion(entry, *, scored=True, aggregated=False):
    assert entry["status"] == "ok", entry
    _check(entry["detail"], entry["method"], score=entry["score"], scored=scored,
           aggregated=aggregated)
    for unit in entry.get("items") or []:
        if unit["status"] == "ok":
            _check(unit["detail"], entry["method"], score=unit["score"], scored=scored)


# ---------------------------------------------------------------------------
# One document, every type
# ---------------------------------------------------------------------------


def test_llm_scored(model):
    r = _run([_doc(_png(), "p.png")], [CriterionInput(name="has a meter", type="llm")])
    _check_criterion(_entries(r)["has a meter"])
    assert _entries(r)["has a meter"]["detail"]["value"] == 8


def test_llm_score_false_reports_no_value(model):
    c = CriterionInput(name="the meter", type="llm", score=False,
                       options={"hint": "presence", "boxes": True})
    entry = _entries(_run([_doc(_png(), "p.png")], [c]))["the meter"]
    _check_criterion(entry, scored=False)
    assert entry["score"] is None and entry["detail"]["value"] is None


def test_text_hit_and_miss():
    doc = _doc(b"Payment Terms: Net 30\nNotice to Owner\n", "t.txt")
    r = _run([doc], [CriterionInput(name="Net 30", type="text"),
                     CriterionInput(name="Lien waiver", type="text")])
    _check_criterion(_entries(r)["Net 30"])
    _check_criterion(_entries(r)["Lien waiver"])
    assert _entries(r)["Net 30"]["detail"]["value"] == 1
    assert _entries(r)["Lien waiver"]["detail"]["value"] == 0


def test_text_with_no_text_available_has_the_same_shape():
    c = CriterionInput(name="Net 30", type="text", options={"ocr": "never"})
    entry = _entries(_run([_doc(_png(), "p.png")], [c]))["Net 30"]
    _check_criterion(entry)
    assert entry["detail"]["found"] is False and entry["detail"]["searched_chars"] == 0


def test_text_document_scope():
    from pathlib import Path

    pdf = (Path(__file__).parent / "documents" / "invoice_two_page.pdf").read_bytes()
    c = CriterionInput(name="Net 30", type="text", options={"scope": "document"})
    entry = _entries(_run([_doc(pdf, "inv.pdf")], [c]))["Net 30"]
    _check_criterion(entry)
    assert entry["detail"]["scope"] == "document"


def test_detector_hit_and_none(detector):
    r = _run([_doc(_png(), "p.png")], [CriterionInput(name="has bicycle", type="detector"),
                                       CriterionInput(name="has kite", type="detector")])
    _check_criterion(_entries(r)["has bicycle"])
    _check_criterion(_entries(r)["has kite"])
    assert _entries(r)["has bicycle"]["detail"]["value"] == 0.8
    assert _entries(r)["has kite"]["detail"]["value"] == 0.0


def test_cv():
    r = _run([_doc(_png(), "p.png")], [CriterionInput(name="sharpness", type="cv"),
                                       CriterionInput(name="exposure", type="cv")])
    _check_criterion(_entries(r)["sharpness"])
    _check_criterion(_entries(r)["exposure"])


# ---------------------------------------------------------------------------
# Aggregates: the block, and what each rule keeps
# ---------------------------------------------------------------------------


def _two_texts():
    return [_doc(b"Net 30 here\n", "a.txt"), _doc(b"no terms\n", "b.txt")]


@pytest.mark.parametrize("rule", ["any", "worst", "all", "mean", "sum"])
def test_text_aggregates_keep_the_shape(rule):
    c = CriterionInput(name="Net 30", type="text", options={"aggregate": rule})
    entry = _entries(_run(_two_texts(), [c]))["Net 30"]
    _check_criterion(entry, aggregated=True)
    block = entry["detail"]["aggregate"]
    assert block["rule"] == rule and block["level"] == "documents"
    assert block["values"] == {"document 0": 1, "document 1": 0}
    if rule == "any":
        assert block["from"] == "document 0" and entry["detail"]["value"] == 1
    elif rule in ("worst", "all"):
        assert block["from"] == "document 1" and entry["detail"]["value"] == 0
    elif rule == "mean":
        assert block["from"] is None and entry["detail"]["value"] == 0.5
        assert "snippets" not in entry["detail"]  # per-member: see items[]
    else:  # sum
        assert entry["detail"]["value"] == 1 and entry["detail"]["count"] == 1


def test_cv_mean_is_the_mean_measurement():
    docs = [_doc(_png(60), "dark.png"), _doc(_png(180), "light.png")]
    c = CriterionInput(name="exposure", type="cv", options={"aggregate": "mean"})
    entry = _entries(_run(docs, [c]))["exposure"]
    _check_criterion(entry, aggregated=True)
    assert entry["detail"]["metric"] == "mean_intensity"
    assert entry["detail"]["value"] == pytest.approx(120.0)
    assert entry["detail"]["aggregate"]["values"] == {"document 0": 60.0, "document 1": 180.0}
    assert "measurements" not in entry["detail"] and "thresholds" in entry["detail"]


def test_llm_any_across_documents(model):
    docs = [_doc(_png(), "a.png"), _doc(_png(), "b.png")]
    entry = _entries(_run(docs, [CriterionInput(name="has a meter", type="llm",
                                                options={"hint": "presence"})]))["has a meter"]
    _check_criterion(entry, aggregated=True)
    assert entry["detail"]["aggregate"]["rule"] == "all"   # presence default: documents all
    assert entry["detail"]["value"] == 8


# ---------------------------------------------------------------------------
# GET /criterion-types serves the declarations
# ---------------------------------------------------------------------------


@pytest.mark.postgres  # the app's lifespan opens the classifier's Postgres pool
def test_criterion_types_serves_each_result_shape():
    import main

    with TestClient(main.app) as client:
        body = client.get("/criterion-types").json()
    for type_, spec in specs().items():
        result = body["types"][type_]["result"]
        assert result["metric"] == spec.metric
        assert set(result["fields"]) == set(spec.fields)
        for f in result["fields"].values():
            assert {"kind", "description"} <= set(f)
    assert body["types"]["text"]["result"]["fields"]["pattern"]["stable"] is True
    assert body["types"]["text"]["result"]["fields"]["scope"]["when"]
    assert set(body["aggregate_detail"]) == {"rule", "level", "from", "values"}


# ---------------------------------------------------------------------------
# The standard: declared beside the producer, registered by type
# ---------------------------------------------------------------------------


def test_each_type_is_declared_by_its_own_evaluator():
    """Every scheduler type has a spec, and that spec is the one its own
    evaluator module declares — not a copy defined somewhere else."""
    from analysis import cv_eval, detector_eval, llm_eval, text_eval
    from analysis.scheduler import EVALUATORS

    declared = specs()
    assert set(declared) == set(EVALUATORS)
    for type_, module in {"llm": llm_eval, "text": text_eval,
                          "detector": detector_eval, "cv": cv_eval}.items():
        assert module.evaluate.result_spec is declared[type_]
    # Every public entry point that produces a type's detail carries its spec.
    assert llm_eval.evaluate_with.result_spec is declared["llm"]
    assert text_eval.evaluate_document.result_spec is declared["text"]
    assert detector_eval.evaluate_label.result_spec is declared["detector"]


def test_a_second_different_spec_for_a_type_is_refused():
    from analysis.result_specs import (AGGREGATE_FIELD, METRIC_FIELD, VALUE_FIELD,
                                       ResultSpec, register)

    impostor = ResultSpec(type="text", metric="value", metric_from="detail",
                          fields={"metric": METRIC_FIELD, "value": VALUE_FIELD,
                                  "aggregate": AGGREGATE_FIELD})
    with pytest.raises(ValueError, match="already registered"):
        register(impostor)
    register(specs()["text"])  # the same object again is fine


def test_a_spec_missing_the_common_fields_is_refused():
    from analysis.result_specs import FieldSpec, ResultSpec

    with pytest.raises(ValueError, match="every type declares"):
        ResultSpec(type="x", metric="n", metric_from="detail",
                   fields={"n": FieldSpec("n", "integer")})


# ---------------------------------------------------------------------------
# detail.reference — declared once (REFERENCE_FIELD), on the llm type only
# ---------------------------------------------------------------------------


def _guided_plan(name: str) -> dict:
    """A one-example plan whose composite exists in the reference store —
    built by hand, so this file needs no reference endpoint."""
    from references.model import composite_name
    from references.store import reference_files

    rid = "rfeedfacecafe"
    reference_files.write(rid, composite_name(name), _png(90))
    example = {
        "reference_id": rid, "title": None, "criterion": name,
        "expected": {"score": 10, "verdict": "PASS", "reason": "it", "source": "caller"},
        "polarity": "pass", "region_source": "whole_page", "regions": [],
        "composite": composite_name(name), "geometry": None,
    }
    return {
        "mode": "explicit", "requested": [rid], "pool": None, "pool_truncated": False,
        "resolved": [rid], "inherited": False,
        "criteria": {name: {"matched": name, "examples": [example], "options": {
            "use": True, "criterion": name, "position": "off", "min_iou": 0.3,
            "combine": "any"}}},
    }


def test_reference_is_declared_on_llm_only():
    from analysis.result_specs import REFERENCE_FIELD

    declared = specs()
    assert declared["llm"].fields["reference"] is REFERENCE_FIELD
    assert not REFERENCE_FIELD.stable and REFERENCE_FIELD.when
    assert all("reference" not in s.fields for t, s in declared.items() if t != "llm")


def test_llm_guided_detail_matches_its_declaration(model):
    from analysis.result_specs import REFERENCE_DETAIL_KEYS

    c = CriterionInput(name="has a meter", type="llm", options={"hint": "presence"})
    result = asyncio.run(analysis.analyze_document(
        [_doc(_png(), "p.png")], [c], references_plan=_guided_plan("has a meter")))
    entry = _entries(result)["has a meter"]
    _check_criterion(entry)
    assert tuple(entry["detail"]["reference"]) == REFERENCE_DETAIL_KEYS
    assert entry["detail"]["reference"]["applied"] is True
    assert result["references"]["mode"] == "explicit"


def test_cv_llm_fallback_guided_has_the_llm_shape(model):
    # "has a meter" would fuzzy-match an OpenCV detector; this name matches none.
    c = CriterionInput(name="has a house", type="cv")
    result = asyncio.run(analysis.analyze_document(
        [_doc(_png(), "p.png")], [c], references_plan=_guided_plan("has a house")))
    entry = _entries(result)["has a house"]
    assert entry["method"] == "llm"
    _check_criterion(entry)
    assert entry["detail"]["reference"]["applied"] is True


def test_reference_is_dropped_by_mean_and_kept_in_items(model):
    c = CriterionInput(name="has a meter", type="llm",
                       options={"hint": "presence", "aggregate": "mean"})
    docs = [_doc(_png(), "a.png"), _doc(_png(), "b.png")]
    result = asyncio.run(analysis.analyze_document(
        docs, [c], references_plan=_guided_plan("has a meter")))
    entry = _entries(result)["has a meter"]
    _check_criterion(entry, aggregated=True)
    assert "reference" not in entry["detail"]
    assert all(u["detail"]["reference"]["applied"] for u in entry["items"])
