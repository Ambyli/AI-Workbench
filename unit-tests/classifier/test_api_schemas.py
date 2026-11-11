"""``classifier.schemas.json`` against the code (root ``CLAUDE.md`` § Postman
collections; the file-wide checks -- coverage, valid schemas, every
collection body, the rendered copies -- are ``unit-tests/apidocs``).

(a) **The generated schemas are current**: the JSON request models
    (``api.schemas.AssessRequest``, ``api.reference_schemas.ReferenceRequest``
    / ``ReferencePatch``), ``CriterionInput`` (the result's
    ``request.criteria``), the jobs models (``common.jobs.model``), each
    criterion type's ``detail`` from its ``ResultSpec`` -- the full shape and
    the stable-keys shape a ``mean`` aggregate keeps -- and each OpenCV
    detector's ``measurements`` from its ``DetectorSpec`` (with
    ``cv_measurements``, which checks a cv ``detail``'s measurements against
    its own ``detector``). ``API_SCHEMAS_UPDATE=1`` rewrites them and
    re-renders the copies::

        API_SCHEMAS_UPDATE=1 UV_LINK_MODE=copy uv run --no-sync --with pytest --package classifier \\
            python -m pytest unit-tests/classifier/test_api_schemas.py -q -p no:cacheprovider

(b) **Real output validates**, with no Postgres, no model and no detector:

    * the introspection routes (``main.app``, its lifespan -- the database --
      never run): /health, /models, /criterion-types, /hints, /cv-detectors,
      /document-kinds;
    * ``analysis.analyze_document`` with a job id (so the artifact
      directory is written, as every API job's is) and a scripted model,
      against ``assess_result`` -- every type, scored / unscored, an
      aggregate under every rule, ``mean`` across documents, document-scope
      text, dependencies, the box loop (one and several items), a guided
      (references) run, measured confidence, a failed model, a cv criterion
      on a text document;
    * the four artifact routes (``api.artifacts.build_artifacts_router`` over
      a registry stub that returns the job): the manifest, scoped and not,
      every file kind, the zip, the DELETE and its 404 / 410 afterwards.

    A status with no entry must be the generic ``{"detail": "<str>"}``. The
    routes behind Postgres (``/assess``, ``/jobs``, ``/references``,
    ``/usage``, progress) are hand-written from the code and checked by
    ``unit-tests/apidocs`` only; the last test checks that everything else
    declared was produced here.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import pathlib
import types
import uuid
from dataclasses import replace
from typing import Any, Dict

import cv2
import numpy as np
import pytest

from common.apidocs import generate, postman, render, schemas

from result_shape import ENTRY_KEYS

ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVICE_DIR = ROOT / "ai" / "classifier"
SCHEMAS_PATH = SERVICE_DIR / "classifier.schemas.json"
DOCS = pathlib.Path(__file__).resolve().parent / "documents"
PREFIX = "/v1/classifier"           # the LiteLLM pass-through the collection calls through

GENERIC_ERROR = {"type": "object", "additionalProperties": False, "required": ["detail"],
                 "properties": {"detail": {"type": "string"}}}

SEEN: set = set()
RAN: set = set()
PRODUCERS = {"introspection", "results", "artifacts"}
# Declared but only answerable with the database behind them (checked by
# unit-tests/apidocs against the collection, not by a real response here).
NEEDS_POSTGRES = {
    ("POST /v1/classifier/assess", "202"),
    ("GET /v1/classifier/jobs", "200"),
    ("GET /v1/classifier/jobs/:jobId", "200"),
    ("GET /v1/classifier/jobs/:jobId/progress", "200"),
    ("POST /v1/classifier/jobs/:jobId/cancel", "200"),
    ("DELETE /v1/classifier/jobs/:jobId", "204"),
    ("POST /v1/classifier/references", "202"),
    ("GET /v1/classifier/references", "200"),
    ("GET /v1/classifier/references/:referenceId", "200"),
    ("GET /v1/classifier/references/:referenceId/files/:referenceFile", "200"),
    ("PATCH /v1/classifier/references/:referenceId", "200"),
    ("DELETE /v1/classifier/references/:referenceId", "204"),
    ("GET /v1/classifier/jobs/:jobId/usage", "200"),
    ("GET /v1/classifier/usage", "200"),
}


@pytest.fixture(scope="module")
def schema_file():
    return schemas.load_schemas(SCHEMAS_PATH)


# -- (a) the generated schemas ---------------------------------------------------------------


def generated_schemas() -> Dict[tuple, Any]:
    from analysis.result_specs import AGGREGATE_FIELD, specs
    from api.reference_schemas import ReferencePatch, ReferenceRequest
    from api.schemas import AssessRequest, CriterionInput
    from common.jobs.model import JobBase, JobsListResponse
    from cv import REGISTRY
    from cv.result import spec_of

    out: Dict[tuple, Any] = {
        # The JSON spelling of the two endpoints that also take multipart (their
        # request is a list; the JSON item references these).
        ("$defs", "assess_request"): generate.from_pydantic(AssessRequest),
        ("$defs", "reference_request"): generate.from_pydantic(ReferenceRequest),
        ("endpoints", f"PATCH {PREFIX}/references/:referenceId", "request", "schema"):
            generate.from_pydantic(ReferencePatch),
        ("$defs", "criterion_input"): generate.from_pydantic(CriterionInput, mode="serialization"),
        ("$defs", "job_base"): generate.from_pydantic(JobBase, mode="serialization"),
        ("$defs", "jobs_list_response"): generate.from_pydantic(JobsListResponse, mode="serialization"),
    }
    for type_, spec in sorted(specs().items()):
        source = f"analysis.result_specs.specs()[{type_!r}]"
        out[("$defs", f"detail_{type_}")] = generate.from_result_spec(spec, source=source)
        # What a `mean` aggregate keeps: the stable keys, metric, value and the
        # aggregate block (then always there) -- analysis.result_specs.aggregated_detail.
        kept = {key: (replace(f, when=None) if f is AGGREGATE_FIELD else f)
                for key, f in spec.fields.items()
                if f.stable or key in ("metric", "value", "aggregate")}
        mean = generate.from_result_spec(types.SimpleNamespace(type=spec.type, fields=kept),
                                         source=f"{source}, the stable keys a `mean` keeps")
        mean["description"] = (f"`detail` of a `{type_}` criterion aggregated by `mean`: only the stable "
                               "keys, `metric`, `value` (the members' mean) and `aggregate`")
        out[("$defs", f"detail_{type_}_mean")] = mean
    functions = {fn.__name__: fn for fn in REGISTRY.values()}
    cases = {}
    for name, fn in sorted(functions.items()):
        spec = spec_of(fn)
        if spec is None:
            continue
        out[("$defs", f"measurements_{name}")] = generate.from_detector_spec(
            spec, source=f"{fn.__module__}.{name}")
        cases[name] = f"#/$defs/measurements_{name}"
    out[("$defs", "cv_measurements")] = generate.by_discriminator(
        "detector", "measurements", cases, source="cv.REGISTRY",
        description="A cv `detail`'s `measurements` are its own detector's (GET /cv-detectors).")
    return out


def test_generated_schemas_are_current():
    data = json.loads(postman.read_text(SCHEMAS_PATH))
    changed = generate.apply_generated(data, generated_schemas())
    if changed and os.environ.get("API_SCHEMAS_UPDATE") == "1":
        schemas.dump_schemas(data, SCHEMAS_PATH)
        render.render_service(SCHEMAS_PATH)
        changed = []
    assert changed == [], (f"{SCHEMAS_PATH.name} is stale at {changed}: rerun this test with "
                           "API_SCHEMAS_UPDATE=1 (it rewrites them and re-renders the copies)")


def test_the_entry_schema_declares_exactly_the_entry_keys(schema_file):
    entry = schema_file.defs["criterion_entry"]
    assert set(entry["properties"]) == ENTRY_KEYS
    assert set(entry["required"]) == ENTRY_KEYS


# -- checking -------------------------------------------------------------------------------------


def check(schema_file, method: str, path: str, response) -> Any:
    key = f"{method} {PREFIX}{path}"
    endpoint = schema_file.endpoints[key]
    status = str(response.status_code)
    spec = endpoint.responses().get(status)
    if spec is None:
        assert response.status_code >= 400, f"{key}: {status} has no entry in {SCHEMAS_PATH.name}"
        errors = schemas.validate(GENERIC_ERROR, response.json())
        assert errors == [], f"{key}: {status} is not the generic error body: {errors}"
        return response.json()
    SEEN.add((key, status))
    if spec.content_type is None:
        assert response.content == b"", key
        return None
    media = response.headers.get("content-type", "").split(";")[0].strip().lower()
    if spec.content_type != "*/*":
        assert media == spec.content_type, (key, status, media)
    if spec.schema is None:
        return response.content
    body = response.json()
    errors = schemas.validate(endpoint.standalone(spec.schema), body)
    assert errors == [], f"{key} {status}: {errors}"
    return body


def check_result(schema_file, result: dict) -> dict:
    schema = schemas.standalone({"$ref": "#/$defs/assess_result"}, schema_file.defs)
    errors = schemas.validate(schema, result)
    assert errors == [], errors
    return result


# -- (b) introspection ----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def app_client():
    from fastapi.testclient import TestClient

    import main

    return TestClient(main.app)          # no `with`: the lifespan opens the database


def test_introspection(schema_file, app_client, monkeypatch):
    from detector import client as detector_client

    check(schema_file, "GET", "/health", app_client.get("/health"))
    models = check(schema_file, "GET", "/models", app_client.get("/models"))
    assert models["default"] in {m["id"] for m in models["models"]}
    types_ = check(schema_file, "GET", "/criterion-types", app_client.get("/criterion-types"))
    assert types_["types"]["llm"]["result"]["fields"]["value"]["nullable"] is True
    check(schema_file, "GET", "/hints", app_client.get("/hints"))
    check(schema_file, "GET", "/cv-detectors", app_client.get("/cv-detectors"))
    check(schema_file, "GET", "/document-kinds", app_client.get("/document-kinds"))
    monkeypatch.setattr(detector_client, "DETECTOR_URL", "http://detector:8000")   # the configured answers
    check(schema_file, "GET", "/criterion-types", app_client.get("/criterion-types"))
    kinds = check(schema_file, "GET", "/document-kinds", app_client.get("/document-kinds"))
    assert kinds["regions"]["detector"]["url_host"] == "detector:8000"
    RAN.add("introspection")


# -- (b) the assessment ---------------------------------------------------------------------------


def _png(value=128, w=400, h=300) -> bytes:
    return cv2.imencode(".png", np.full((h, w, 3), value, dtype=np.uint8))[1].tobytes()


def _doc(raw: bytes, name: str):
    import analysis

    return analysis.load_document_bytes(raw, name, None, keep_source=True)


def _job_id() -> str:
    return uuid.uuid4().hex[:12]


def _run(docs, criteria, **kw) -> dict:
    import analysis

    return asyncio.run(analysis.analyze_document(docs, criteria, job_id=kw.pop("job_id", None) or _job_id(), **kw))


def _entries(result) -> dict:
    return result["assessment"]["per_criterion_scores"]


@pytest.fixture
def model(monkeypatch):
    from llm import client as llm_client

    async def send(prompt):
        return {"choices": [{"message": {"content": json.dumps(
            {"score": 8, "verdict": "PASS", "confidence": 80, "reason": "I see it."})}}]}

    monkeypatch.setattr(llm_client, "_send", send)


@pytest.fixture
def detector(monkeypatch):
    from common.vision import Region
    from detector import client as detector_client

    monkeypatch.setattr(detector_client, "DETECTOR_URL", "http://stub")

    async def detect_page(image, labels, geometry, *, min_score, stats):
        stats.calls += 1
        return {label: ([Region(page=geometry.page if geometry else 0, kind="box", points=[(1, 1), (50, 50)],
                                label=label, score=0.8, source="detector")] if label == "has bicycle" else [])
                for label in labels}

    monkeypatch.setattr(detector_client, "detect_page", detect_page)


class Scripted:
    """``call_model_json`` answering from a list (the box loop's ask / verify)."""

    def __init__(self, answers):
        self.answers = list(answers)

    async def __call__(self, prompt, *, label="", model=None):
        return self.answers.pop(0) if self.answers else None


def _boxes(monkeypatch, answers):
    from llm import boxes as llm_boxes
    from llm import client as llm_client

    async def scoring(prompt, *, label="", validator=None, model=None):
        raw = {"score": 10, "verdict": "PASS", "confidence": 80, "reason": "I observe panels."}
        return validator(raw) if validator else raw

    monkeypatch.setattr(llm_client, "call_model", scoring)
    monkeypatch.setattr(llm_client, "call_model_json", Scripted(answers))
    monkeypatch.setattr(llm_boxes, "LLM_BBOX_REFINE", False)
    monkeypatch.setattr(llm_boxes, "LLM_BBOX_GRIDLINES", False)


GOOD_BOX = [100, 200, 300, 400]


def test_assess_results(schema_file, model, detector, monkeypatch):
    from api.schemas import CriterionInput as C
    from llm import client as llm_client

    pdf = (DOCS / "invoice_two_page.pdf").read_bytes()
    two_texts = [_doc(b"Net 30 here\n", "a.txt"), _doc(b"no terms\n", "b.txt")]
    results = {
        "every type": _run([_doc(_png(), "p.png")], [
            C(name="has a meter", type="llm"),
            C(name="the meter", type="llm", score=False, options={"hint": "presence", "boxes": True}),
            C(name="has bicycle", type="detector"), C(name="has kite", type="detector"),
            C(name="sharpness", type="cv"), C(name="exposure", type="cv"),
            C(name="Net 30", type="text", options={"ocr": "never"}),
            C(name="has a house", type="cv"),            # no OpenCV detector: the detector fallback
        ]),
        "text": _run([_doc(b"Payment Terms: Net 30\nNotice to Owner\n", "t.txt")], [
            C(name="Net 30", type="text"), C(name="Lien waiver", type="text"),
            C(name="fuzzy", type="text", options={"match": "fuzzy", "pattern": "Net 3O"}),
            C(name="regex", type="text", options={"match": "regex", "pattern": r"Net \d+"}),
            C(name="exposure", type="cv"), C(name="mentions terms", type="llm", options={"hint": "presence"}),
        ]),
        "document scope": _run([_doc(pdf, "inv.pdf")], [
            C(name="Net 30", type="text", options={"scope": "document"}),
            C(name="per page", type="text", options={"pattern": "Net 30"}),
        ]),
        "mixed documents": _run([_doc(pdf, "inv.pdf"), _doc(_png(), "p.png"), _doc(b"Net 30\n", "c.txt")], [
            C(name="Net 30", type="text"), C(name="has logo", type="llm", options={"hint": "presence"}),
            C(name="sharpness", type="cv"),
        ]),
        "cv mean": _run([_doc(_png(60), "d.png"), _doc(_png(180), "l.png")],
                        [C(name="exposure", type="cv", options={"aggregate": "mean"})]),
        "llm across documents": _run([_doc(_png(), "a.png"), _doc(_png(), "b.png")], [
            C(name="has a meter", type="llm", options={"hint": "presence"}),
            C(name="mean quality", type="llm", options={"aggregate": "mean"})]),
        "dependencies": _run([_doc(_png(0), "black.png")], [
            C(name="exposure", type="cv"),
            C(name="has a meter", type="llm", depends_on="exposure"),
            C(name="weightless", type="llm", weight=2.5, depends_on="has a meter")]),
    }
    for rule in ("any", "worst", "all", "mean", "sum"):
        results[f"text {rule}"] = _run(two_texts, [C(name="Net 30", type="text", options={"aggregate": rule})])
    results["detector mean"] = _run([_doc(_png(), "a.png"), _doc(_png(), "b.png")],
                                    [C(name="has bicycle", type="detector", options={"aggregate": "mean"})])
    for name, result in results.items():
        check_result(schema_file, result)
    assert {e["method"] for e in _entries(results["every type"]).values()} == {"llm", "detector", "cv", "text"}
    assert _entries(results["dependencies"])["has a meter"]["status"] == "skipped"
    assert _entries(results["cv mean"])["exposure"]["detail"]["aggregate"]["rule"] == "mean"
    assert results["mixed documents"]["artifacts"]["files"]

    # A failed model: the criterion errors alone, the assessment is incomplete.
    async def failing(prompt):
        raise RuntimeError("model down")

    monkeypatch.setattr(llm_client, "_send", failing)
    failed = check_result(schema_file, _run([_doc(_png(), "p.png")],
                                            [C(name="has a meter", type="llm"), C(name="exposure", type="cv")]))
    assert failed["assessment"]["complete"] is False and _entries(failed)["has a meter"]["status"] == "error"
    # cv on a text document: skipped, not applicable.
    skipped = check_result(schema_file, _run([_doc(b"hello\n", "a.txt")], [C(name="sharpness", type="cv")]))
    assert _entries(skipped)["sharpness"]["status"] == "skipped"
    RAN.add("results")


def test_the_box_loop(schema_file, monkeypatch):
    from api.schemas import CriterionInput as C

    criterion = C(name="has solar panels", type="llm", options={"hint": "presence", "boxes": True})
    _boxes(monkeypatch, [{"bbox": [0, 0, 1000, 1000]}, {"bbox": GOOD_BOX, "confidence": 90, "reason": "x"},
                         {"score": 9, "reason": "panels"}])
    one = check_result(schema_file, _run([_doc(_png(), "page.png")], [criterion]))
    loc = _entries(one)["has solar panels"]["localization"]
    assert loc["accepted_attempt"] == 2 and _entries(one)["has solar panels"]["artifacts"]["items"][0]["attempts"]
    _boxes(monkeypatch, [{"bbox": GOOD_BOX}, {"score": 9, "reason": "a"}, {"bbox": GOOD_BOX}, {"score": 2, "reason": "b"},
                         {"bbox": [400, 200, 600, 400]}, {"score": 9, "reason": "c"}])
    pdf = (DOCS / "invoice_two_page.pdf").read_bytes()
    several = check_result(schema_file, _run([_doc(pdf, "inv.pdf")], [criterion]))
    assert "accepted" in _entries(several)["has solar panels"]["localization"]
    # Several single-page DOCUMENTS merge like the pages of one: every attempt
    # tagged with its item, `accepted` naming each accepted box (the schema's
    # merged branch requires both, so the old untagged / empty shape fails).
    _boxes(monkeypatch, [{"bbox": GOOD_BOX}, {"score": 9, "reason": "a"},
                         {"bbox": [400, 200, 600, 400]}, {"score": 9, "reason": "b"}])
    documents = check_result(schema_file, _run([_doc(_png(), "a.png"), _doc(_png(90), "b.png")], [criterion]))
    loc = _entries(documents)["has solar panels"]["localization"]
    assert sorted({a["item"] for a in loc["attempts"]}) == [0, 1], loc["attempts"]
    assert loc["accepted"] == [{"item": 0, "attempt": 1}, {"item": 1, "attempt": 1}], loc["accepted"]
    assert loc["accepted_attempt"] is None
    old_shape = {**loc, "attempts": [{k: v for k, v in a.items() if k != "item"} for a in loc["attempts"]]}
    assert schemas.validate(schemas.standalone({"$ref": "#/$defs/localization"}, schema_file.defs), old_shape), \
        "an untagged merged record must fail the schema"


def _guided_plan(name: str) -> dict:
    """A one-example plan whose composite is in the reference store (as
    test_result_specs builds it)."""
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
            "use": True, "criterion": name, "position": "off", "min_iou": 0.3, "combine": "any"}}},
    }


def test_guided_and_measured_confidence(schema_file, monkeypatch):
    from api.schemas import CriterionInput as C
    from llm import client as llm_client

    def e(token, top=None):
        top = top or [(token, 1.0)]
        return {"token": token, "logprob": math.log(dict(top).get(token, 1.0)),
                "top_logprobs": [{"token": t, "logprob": math.log(p)} for t, p in top]}

    async def send(prompt):
        tokens = [e('{"'), e("score"), e('":'), e(" "), e("8", [("8", 0.7), ("7", 0.2), (" eight", 0.1)]), e(","),
                  e(' "verdict":'), e(' "PASS",'), e(' "confidence":'), e(" 90,"), e(' "reason":'), e(' "ok"}')]
        return {"choices": [{"message": {"content": "".join(t["token"] for t in tokens)},
                             "logprobs": {"content": tokens}}]}

    monkeypatch.setattr(llm_client, "_send", send)
    guided = check_result(schema_file, _run([_doc(_png(), "p.png")],
                                            [C(name="has a meter", type="llm", options={"hint": "presence"})],
                                            references_plan=_guided_plan("has a meter")))
    entry = _entries(guided)["has a meter"]
    assert entry["detail"]["reference"]["applied"] is True and guided["references"]["mode"] == "explicit"
    assert entry["detail"]["confidence"]["source"] == "logprobs"


# -- (b) the artifact routes ---------------------------------------------------------------------


class _Registry:
    """``registry.get`` for the artifact routes: the job, its result as stored."""

    def __init__(self):
        self.jobs: Dict[str, Any] = {}

    async def get(self, job_id):
        return self.jobs.get(job_id)


def test_artifact_routes(schema_file, model):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.artifacts import build_artifacts_router
    from api.schemas import CriterionInput as C
    from common.jobs.model import JobBase

    job_id = _job_id()
    result = check_result(schema_file, _run([_doc((DOCS / "invoice_two_page.pdf").read_bytes(), "inv.pdf")], [
        C(name="Net 30", type="text"), C(name="has logo", type="llm", options={"hint": "presence"})], job_id=job_id))
    registry = _Registry()
    registry.jobs[job_id] = JobBase(job_id=job_id, phase="completed", created_at="x", updated_at="x",
                                    elapsed_seconds=1.0, metadata={"type": "assess"}, result=result)
    app = FastAPI()
    app.include_router(build_artifacts_router(registry))
    client = TestClient(app)
    base = f"/jobs/{job_id}/artifacts"
    manifest = check(schema_file, "GET", "/jobs/:jobId/artifacts", client.get(base))
    slug = _entries(result)["Net 30"]["artifacts"]["slug"]
    scoped = check(schema_file, "GET", "/jobs/:jobId/artifacts", client.get(base, params={"criterion": slug}))
    assert scoped["filter"]["criterion"] == [slug]
    name_path = "/jobs/:jobId/artifacts/:artifactName"
    kinds = {}
    for name in ("regions.json", "manifest.json", "p0.svg", "p0.layer.png", "p0.preview.jpg",
                 f"p1.{slug}.svg", "text.p0.auto.json", "text.p0.auto.txt"):
        response = client.get(f"{base}/{name}")
        assert response.status_code == 200, (name, response.text[:300])
        check(schema_file, "GET", name_path, response)
        kinds[name] = response.headers["content-type"].split(";")[0]
    assert kinds["regions.json"] == "application/json" and kinds["p0.svg"] == "image/svg+xml"
    filtered = check(schema_file, "GET", name_path, client.get(f"{base}/regions.json", params={"criterion": slug}))
    assert filtered is not None
    for response in (client.get(f"{base}/nope.json"), client.get(f"{base}/regions.json", params={"criterion": "x"}),
                     client.get(f"{base}/manifest.json", params={"source": "llm"})):
        assert response.status_code in (400, 404), response.text
        check(schema_file, "GET", name_path, response)
    zipped = client.get(f"/jobs/{job_id}/artifacts.zip")
    check(schema_file, "GET", "/jobs/:jobId/artifacts.zip", zipped)
    assert zipped.content[:2] == b"PK"
    check(schema_file, "GET", "/jobs/:jobId/artifacts.zip", client.get(f"/jobs/{job_id}/artifacts.zip",
                                                                       params={"criterion": slug}))
    check(schema_file, "DELETE", "/jobs/:jobId/artifacts", client.delete(base))
    for method, path, response in (
        ("GET", "/jobs/:jobId/artifacts", client.get(base)),                  # 410: swept
        ("DELETE", "/jobs/:jobId/artifacts", client.delete(base)),            # 404: no directory
        ("GET", "/jobs/:jobId/artifacts", client.get("/jobs/000000000000/artifacts")),
        ("GET", "/jobs/:jobId/artifacts.zip", client.get("/jobs/000000000000/artifacts.zip")),
    ):
        assert response.status_code in (404, 410), (path, response.status_code)
        check(schema_file, method, path, response)
    RAN.add("artifacts")


# -- last: everything declared that can be produced here was ------------------------------------


def test_every_declared_response_was_produced(schema_file):
    if RAN != PRODUCERS:
        pytest.skip(f"needs the whole module (ran: {sorted(RAN)})")
    declared = {(key, status) for key, e in schema_file.endpoints.items() for status in e.responses()}
    assert NEEDS_POSTGRES <= declared, sorted(NEEDS_POSTGRES - declared)
    assert declared - NEEDS_POSTGRES - SEEN == set(), "declared but never produced by a real response"
