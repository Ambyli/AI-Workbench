"""Every criterion type's ``detail`` matches its declaration.

``analysis.result_specs`` declares what each type's ``detail`` carries, and
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
from analysis.result_specs import AGGREGATE_BLOCK, SPECS
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
    spec = SPECS[method]
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


def test_criterion_types_serves_each_result_shape():
    import main

    with TestClient(main.app) as client:
        body = client.get("/criterion-types").json()
    for type_, spec in SPECS.items():
        result = body["types"][type_]["result"]
        assert result["metric"] == spec.metric
        assert set(result["fields"]) == set(spec.fields)
        for f in result["fields"].values():
            assert {"kind", "description"} <= set(f)
    assert body["types"]["text"]["result"]["fields"]["pattern"]["stable"] is True
    assert body["types"]["text"]["result"]["fields"]["scope"]["when"]
    assert set(body["aggregate_detail"]) == {"rule", "level", "from", "values"}
