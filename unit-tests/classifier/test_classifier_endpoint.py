"""POST /assess end to end, through FastAPI's TestClient, with a scripted model.

No network and no live model: the app runs in-process with its real worker
pool, SQLite queue and artifact store (all on the session temp directory set
up by conftest.py), and the vision model is replaced at the transport
(``llm.client._send``) so the process-wide call limit stays in play.

What is pinned here:

  * the one request, two ways — a JSON body and a multipart form produce the
    SAME result for the same document and criteria; inline ``text`` works
    both ways;
  * the result shape — ``schema_version: 3``, one key set for every
    criterion whatever its type or status, ``page_geometry`` a list with one
    entry per item;
  * the refusals at submit — the removed routes (404), and the validation
    400s that need the HTTP layer (the model-level rules are in
    test_criterion_options.py; the item cap and the multi-document shapes
    are in test_multi_items.py);
  * the artifacts every job writes — regions.json, the base image, one
    ``text.p{n}.<key>.json`` per item and distinct text layer (linked from
    each criterion, never inlined), the lazily rendered layers, and what the
    byte cap and DELETE do to them;
  * a model outage fails one criterion, not the job.

Run with::

    UV_LINK_MODE=copy uv run --no-sync --with pytest --package classifier \\
        python -m pytest unit-tests/classifier/test_classifier_endpoint.py -q -p no:cacheprovider
"""

from __future__ import annotations

import base64
import json
import pathlib
import time

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from common.documents import OCRResult

HERE = pathlib.Path(__file__).resolve().parent
DOCS = HERE / "documents"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client(need_postgres):
    import main

    with TestClient(main.app) as c:
        yield c


def _answer(score=9, reason="I observe it. Therefore it is present."):
    return {"choices": [{"message": {"content": json.dumps(
        {"score": score, "verdict": "PASS", "confidence": 80, "reason": reason}
    )}}]}


@pytest.fixture
def model(monkeypatch):
    """Script the vision model at the transport. Records every prompt."""
    from llm import client as llm_client

    calls: list[dict] = []

    async def fake_post(prompt):
        calls.append(prompt)
        return _answer()

    monkeypatch.setattr(llm_client, "_send", fake_post)
    return calls


class FakeOCR:
    """An OCREngine with one known line and box; counts its calls."""

    def __init__(self):
        self.calls = 0

    def recognize(self, image_bgr):
        self.calls += 1
        return OCRResult(
            text="NOTICE TO OWNER\nTotal Due $4,850.00",
            confidence=0.9,
            lines=[
                {"text": "NOTICE TO OWNER", "confidence": 0.95,
                 "box": [[40, 100], [360, 100], [360, 140], [40, 140]]},
                {"text": "Total Due $4,850.00", "confidence": 0.85,
                 "box": [[40, 200], [520, 200], [520, 240], [40, 240]]},
            ],
        )


@pytest.fixture
def fake_ocr(monkeypatch):
    from analysis import ocr

    engine = FakeOCR()
    monkeypatch.setattr(ocr, "_ocr_engine", engine)
    return engine


def _png(width=400, height=300, value=180) -> bytes:
    import cv2

    return cv2.imencode(".png", np.full((height, width, 3), value, dtype=np.uint8))[1].tobytes()


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _wait(client, job_id: str, timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/jobs/{job_id}").json()
        if job["phase"] in ("completed", "failed"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish: {job}")


def _run_json(client, document: dict, criteria: list) -> dict:
    r = client.post("/assess", json={"document": document, "criteria": criteria})
    assert r.status_code == 202, r.text
    job = _wait(client, r.json()["job_id"])
    assert job["phase"] == "completed", job
    return job


def _run_form(client, criteria: list, *, files=None, data=None) -> dict:
    form = {"criteria": json.dumps(criteria), **(data or {})}
    r = client.post("/assess", files=files, data=form)
    assert r.status_code == 202, r.text
    job = _wait(client, r.json()["job_id"])
    assert job["phase"] == "completed", job
    return job


def _entries(job) -> dict:
    return job["result"]["assessment"]["per_criterion_scores"]


def _strip_ids(value, job_id: str):
    """The result with this job's id removed, so two jobs can be compared."""
    return json.loads(json.dumps(value).replace(job_id, "<job>"))


ENTRY_KEYS = {
    "status", "type", "method", "scored", "score", "verdict", "confidence",
    "reason", "detail", "regions", "regions_truncated", "artifacts",
    "localization", "options_used", "error", "complete", "aggregate_used", "items",
}

TEXT_CRITERIA = [
    {"name": "Limited Warranty", "type": "text"},
    {"name": "mentions a warranty", "type": "llm", "options": {"hint": "presence"}},
]


# ---------------------------------------------------------------------------
# One request, two encodings
# ---------------------------------------------------------------------------


def test_json_and_multipart_produce_the_same_result(client, model):
    raw = (DOCS / "contract.txt").read_bytes()
    as_json = _run_json(
        client, {"type": "base64", "data": _b64(raw), "filename": "contract.txt"}, TEXT_CRITERIA
    )
    as_form = _run_form(
        client, TEXT_CRITERIA, files={"file": ("contract.txt", raw, "text/plain")}
    )
    a = _strip_ids(as_json["result"], as_json["job_id"])
    b = _strip_ids(as_form["result"], as_form["job_id"])
    for result in (a, b):
        result["artifacts"].pop("expires_at")
        result["artifacts"].pop("dir")
        for f in result["artifacts"]["files"]:
            f.pop("bytes", None)
    assert a["assessment"] == b["assessment"]
    assert a["documents"] == b["documents"]
    assert a["items"] == b["items"]
    assert a["verdict"] == b["verdict"] == "PASS"


def test_legacy_image_part_is_still_accepted(client, model):
    job = _run_form(
        client, [{"name": "sharpness", "type": "cv"}],
        files={"image": ("page.png", _png(), "image/png")},
    )
    assert _entries(job)["sharpness"]["method"] == "cv"


def test_inline_text_both_ways(client, model):
    criteria = [{"name": "Notice to Owner", "type": "text"}]
    via_json = _run_json(client, {"type": "text", "data": "A NOTICE TO OWNER is here."}, criteria)
    via_form = _run_form(client, criteria, data={"text": "A NOTICE TO OWNER is here."})
    for job in (via_json, via_form):
        (info,) = job["result"]["documents"]
        assert info["kind"] == "txt" and info["filename"] == "inline.txt"
        assert _entries(job)["Notice to Owner"]["verdict"] == "PASS"


def test_default_criteria_apply_when_omitted(client, model):
    r = client.post("/assess", files={"file": ("page.png", _png(), "image/png")})
    assert r.status_code == 202, r.text
    job = _wait(client, r.json()["job_id"])
    names = list(_entries(job))
    assert names == [
        "document legibility", "image sharpness", "proper exposure", "absence of artifacts"
    ]
    assert all(e["options_used"]["hint"] == "quality" for e in _entries(job).values())


# ---------------------------------------------------------------------------
# The result shape
# ---------------------------------------------------------------------------


def test_every_criterion_has_the_same_keys(client, model, monkeypatch):
    from llm import client as llm_client

    async def flaky_post(prompt):
        text = json.dumps(prompt)
        if "will fail" in text:
            raise httpx.ConnectError("model down")
        return _answer()

    monkeypatch.setattr(llm_client, "_send", flaky_post)
    job = _run_form(
        client,
        [
            {"name": "sharpness", "type": "cv"},
            {"name": "has sky", "type": "cv"},
            {"name": "SOMETHING", "type": "text"},
            {"name": "has a roof", "type": "llm", "options": {"hint": "presence"}},
            {"name": "will fail", "type": "llm"},
            {"name": "after", "type": "text", "depends_on": "SOMETHING"},
        ],
        files={"file": ("page.png", _png(), "image/png")},
    )
    result = job["result"]
    assert result["schema_version"] == 3
    (geometry,) = result["page_geometry"]
    assert geometry["item"] == 0 and geometry["page"] == 0 and geometry["width"] == 400
    assert result["documents"] == [{
        "index": 0, "filename": "page.png", "kind": "image", "pages": 1, "items": [0],
        "warnings": [],
        "document_info": result["documents"][0]["document_info"],
    }]
    assert result["items"] == [{
        "item": 0, "document": 0, "page": 0, "filename": "page.png",
        "overall_score": None, "overall_verdict": None, "complete": False,
    }]
    entries = _entries(job)
    for name, entry in entries.items():
        assert set(entry) == ENTRY_KEYS, name
        (unit,) = entry["items"]  # one item → one unit
        assert (unit["item"], unit["document"], unit["page"]) == (0, 0, 0)
        assert unit["status"] == entry["status"] and unit["score"] == entry["score"]
    assert entries["will fail"]["status"] == "error"
    assert "model down" in entries["will fail"]["error"]
    assert entries["after"]["status"] == "skipped"
    assert entries["has a roof"]["localization"] == {
        "attempts": [], "accepted_attempt": None, "calls": 0
    }
    assert entries["sharpness"]["localization"] is None
    assert "image_info" not in result and "features" not in result


def test_a_model_outage_fails_one_criterion_and_marks_the_job_incomplete(client, monkeypatch):
    from llm import client as llm_client

    async def down(prompt):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(llm_client, "_send", down)
    job = _run_form(
        client,
        [
            {"name": "sharpness", "type": "cv"},
            {"name": "has a roof", "type": "llm", "options": {"hint": "presence"}},
        ],
        files={"file": ("page.png", _png(), "image/png")},
    )
    assessment = job["result"]["assessment"]
    assert job["phase"] == "completed"
    assert assessment["complete"] is False
    assert assessment["overall_verdict"] is None and assessment["overall_score"] is None
    assert job["result"]["verdict"] is None
    breakdown = assessment["weighted_score_breakdown"]
    assert breakdown["partial"] is True
    assert list(breakdown["per_criterion"]) == ["sharpness"]
    assert breakdown["excluded"] == {"has a roof": "error"}


# ---------------------------------------------------------------------------
# Refusals at submit
# ---------------------------------------------------------------------------


def test_a_two_page_pdf_is_two_items_not_a_refusal(client, model):
    raw = (DOCS / "invoice_two_page.pdf").read_bytes()
    job = _run_form(client, [{"name": "Net 30", "type": "text"}],
                    files={"file": ("two.pdf", raw, "application/pdf")})
    result = job["result"]
    assert [(i["item"], i["page"]) for i in result["items"]] == [(0, 0), (1, 1)]
    assert result["documents"][0]["pages"] == 2
    assert [g["item"] for g in result["page_geometry"]] == [0, 1]


@pytest.mark.parametrize("path", ["/locate", "/assess/compare"])
def test_removed_routes_are_404(client, path):
    assert client.post(path, json={}).status_code == 404


def _submit(client, criteria, *, document=None):
    return client.post(
        "/assess",
        json={"document": document or {"type": "text", "data": "hello world"}, "criteria": criteria},
    )


@pytest.mark.parametrize(
    "criteria, fragment",
    [
        ([{"name": "x", "type": "llm", "options": {"colour": "red"}}], "options.colour: Extra inputs"),
        ([{"name": "x", "type": "llm", "options": {"boxes": "yes"}}], "options.boxes"),
        ([{"name": "x", "type": "llm", "options": {"max_attempts": 99}}], "exceeds this server's cap"),
        ([{"name": "x", "type": "llm", "hint": "presence"}], "moved into 'options'"),
        ([{"name": "x", "type": "text", "options": {"match": "regex", "pattern": "(["}}],
         "options.pattern: Invalid regular expression"),
        ([{"name": "a"}, {"name": "a"}], "duplicate criterion name"),
        ([{"name": "a", "depends_on": "nope"}], "not a criterion in this request"),
        ([{"name": "a", "depends_on": "b"}, {"name": "b", "depends_on": "a"}], "dependency cycle"),
        ([{"name": "a", "type": "llm", "score": False}], "cannot produce any geometry"),
        ([], "at least 1 item"),
    ],
)
def test_validation_400s(client, criteria, fragment):
    r = _submit(client, criteria)
    assert r.status_code == 400, r.text
    assert fragment in r.json()["detail"]


def test_score_false_on_a_text_only_document_is_refused(client):
    r = _submit(client, [{"name": "a", "type": "text", "score": False}])
    assert r.status_code == 400
    assert "need a page image" in r.json()["detail"]


def test_detector_criteria_need_a_configured_detector(client):
    r = _submit(client, [{"name": "has bicycle", "type": "detector"}],
                document={"type": "base64", "data": _b64(_png())})
    assert r.status_code == 400 and "DETECTOR_URL is empty" in r.json()["detail"]


def test_form_level_refusals(client):
    png = ("page.png", _png(), "image/png")
    r = client.post("/assess", files={"file": png}, data={"ocr": "always"})
    assert r.status_code == 400 and "options.ocr" in r.json()["detail"]
    r = client.post("/assess", data={"criteria": "[]"})
    assert r.status_code == 400 and "No document" in r.json()["detail"]
    r = client.post("/assess", files={"file": png}, data={"criteria": "not json"})
    assert r.status_code == 400 and "not valid JSON" in r.json()["detail"]
    legacy = (DOCS / "unsupported_legacy.doc").read_bytes()
    r = client.post("/assess", files={"file": ("old.doc", legacy, "application/msword")})
    assert r.status_code == 400 and ".docx" in r.json()["detail"]


def test_unsupported_content_type_is_415(client):
    r = client.post("/assess", content=b"x", headers={"content-type": "application/xml"})
    assert r.status_code == 415


def test_criterion_types_describes_every_type(client):
    body = client.get("/criterion-types").json()
    assert set(body["types"]) == {"llm", "text", "cv", "detector"}
    llm = body["types"]["llm"]
    assert llm["defaults"] == {"hint": "auto", "boxes": False, "max_attempts": 3, "ocr": "auto",
                               "aggregate": {"pages": "worst", "documents": "worst"}}
    assert llm["caps"] == {"max_attempts": 3}
    assert "boxes" in llm["options_schema"]["properties"]
    assert body["types"]["cv"]["defaults"]["fallback"] == "llm"  # no DETECTOR_URL here
    assert "aggregate" in body and "rules" in body["aggregate"]


# ---------------------------------------------------------------------------
# The text a criterion searched
# ---------------------------------------------------------------------------


def _artifact_names(client, job) -> list[str]:
    return [f["name"] for f in client.get(f"/jobs/{job['job_id']}/artifacts").json()["files"]]


def test_text_layer_is_written_linked_and_shared(client, model):
    raw = (DOCS / "contract.txt").read_bytes()
    job = _run_form(
        client,
        [
            {"name": "Limited Warranty", "type": "text"},
            {"name": "2026-03-14", "type": "text"},
            {"name": "mentions a warranty", "type": "llm"},
        ],
        files={"file": ("contract.txt", raw, "text/plain")},
    )
    names = _artifact_names(client, job)
    assert [n for n in names if n.startswith("text.")] == ["text.p0.auto.json"]  # ONE shared file

    entries = _entries(job)
    for name in ("Limited Warranty", "2026-03-14", "mentions a warranty"):
        (link,) = entries[name]["artifacts"]["text"]
        assert link == {
            "item": 0,
            "key": "auto",
            "url": f"/jobs/{job['job_id']}/artifacts/text.p0.auto.json",
            "source": "native",
            "chars": len(raw.decode("utf-8")),
        }
        # The unit's entry links the same file.
        assert entries[name]["items"][0]["text_layer"] == {k: v for k, v in link.items() if k != "item"}
        assert entries[name]["options_used"]["ocr"] == link["key"]
        assert "text" not in (entries[name]["detail"] or {})  # never inlined

    stored = client.get(link["url"])
    assert stored.status_code == 200
    assert stored.headers["content-type"].startswith("application/json")
    payload = stored.json()
    # Byte-for-byte the string the matcher searched: the document itself.
    assert payload["text"] == raw.decode("utf-8")
    assert payload["source"] == "native" and payload["engine"] is None
    assert payload["settings"]["mode"] == "auto" and payload["lines"] == []

    plain = client.get(f"/jobs/{job['job_id']}/artifacts/text.p0.auto.txt")
    assert plain.status_code == 200
    assert plain.headers["content-type"] == "text/plain; charset=utf-8"
    assert plain.text == raw.decode("utf-8")

    manifest = client.get(f"/jobs/{job['job_id']}/artifacts").json()
    assert manifest["criteria"]["Limited Warranty"]["text_layers"] == ["text.p0.auto.json"]
    assert manifest["items"] == {"0": {"document": 0, "page": 0, "filename": "contract.txt"}}


def test_different_settings_write_different_layers(client, fake_ocr):
    raw = (DOCS / "invoice_scanned.pdf").read_bytes()
    job = _run_form(
        client,
        [
            {"name": "NOTICE TO OWNER", "type": "text", "options": {"ocr": "never"}},
            {"name": "notice again", "type": "text",
             "options": {"pattern": "NOTICE TO OWNER", "ocr": "always"}},
            {"name": "and again", "type": "text",
             "options": {"pattern": "Total Due", "ocr": "always"}},
        ],
        files={"file": ("scan.pdf", raw, "application/pdf")},
    )
    names = sorted(n for n in _artifact_names(client, job) if n.startswith("text."))
    assert names == ["text.p0.always.json", "text.p0.never.json"]
    assert fake_ocr.calls == 1  # the two "always" criteria shared ONE pass

    entries = _entries(job)
    assert entries["NOTICE TO OWNER"]["artifacts"]["text"][0]["source"] == "none"
    assert entries["NOTICE TO OWNER"]["verdict"] == "FAIL"
    assert entries["notice again"]["artifacts"]["text"][0]["source"] == "ocr"
    assert entries["notice again"]["verdict"] == "PASS"
    # The OCR line polygons became regions on the page.
    assert entries["notice again"]["regions"][0]["source"] == "ocr"

    ocr_layer = client.get(f"/jobs/{job['job_id']}/artifacts/text.p0.always.json").json()
    assert ocr_layer["source"] == "ocr" and ocr_layer["engine"] == "FakeOCR"
    assert ocr_layer["text"] == "NOTICE TO OWNER\nTotal Due $4,850.00"
    assert ocr_layer["lines"][0]["polygon"] == [[40, 100], [360, 100], [360, 140], [40, 140]]
    never = client.get(f"/jobs/{job['job_id']}/artifacts/text.p0.never.json").json()
    assert never["source"] == "none" and never["text"] == ""


@pytest.mark.parametrize("fixture", ["invoice_native.pdf", "proposal.docx", "contract.txt"])
def test_native_documents_store_a_native_layer(client, fixture):
    raw = (DOCS / fixture).read_bytes()
    job = _run_form(
        client, [{"name": "Tampa", "type": "text"}], files={"file": (fixture, raw, None)}
    )
    layer = client.get(f"/jobs/{job['job_id']}/artifacts/text.p0.auto.json").json()
    assert layer["source"] == "native"
    assert "Tampa" in layer["text"]
    assert _entries(job)["Tampa"]["verdict"] == "PASS"


def test_text_layers_survive_the_byte_cap_and_go_with_the_job(client, monkeypatch):
    from regions.store import store

    monkeypatch.setattr(store, "max_bytes", 1)  # everything droppable goes
    raw = (DOCS / "invoice_native.pdf").read_bytes()
    job = _run_form(
        client, [{"name": "Net 30", "type": "text"}],
        files={"file": ("invoice.pdf", raw, "application/pdf")},
    )
    names = _artifact_names(client, job)
    assert "text.p0.auto.json" in names and "regions.json" in names
    assert "p0.base.jpg" not in names  # dropped by the cap
    assert "p0.base.jpg" in job["result"]["artifacts"]["dropped"]

    job_id = job["job_id"]
    assert client.delete(f"/jobs/{job_id}").status_code in (200, 204)
    assert not store.exists(job_id)
    assert client.get(f"/jobs/{job_id}/artifacts/text.p0.auto.json").status_code == 404


# ---------------------------------------------------------------------------
# Layers render on first fetch
# ---------------------------------------------------------------------------


def test_layers_render_on_first_fetch_and_are_cached(client, model):
    raw = (DOCS / "invoice_native.pdf").read_bytes()
    job = _run_form(
        client, [{"name": "Total Due", "type": "text"}],
        files={"file": ("invoice.pdf", raw, "application/pdf")},
    )
    job_id = job["job_id"]
    entry = _entries(job)["Total Due"]
    assert entry["regions"] and entry["regions"][0]["source"] == "pdf-text"
    (page_layers,) = job["result"]["artifacts"]["items"]
    assert page_layers["item"] == 0 and set(page_layers["layers"]) == {"svg", "png", "preview"}

    before = _artifact_names(client, job)
    assert "p0.svg" not in before and "p0.base.jpg" in before

    svg = client.get(f"/jobs/{job_id}/artifacts/p0.svg")
    assert svg.status_code == 200 and svg.headers["content-type"] == "image/svg+xml"
    assert 'data-region-count="' in svg.text
    png = client.get(f"/jobs/{job_id}/artifacts/p0.layer.png")
    assert png.status_code == 200 and png.headers["content-type"] == "image/png"
    preview = client.get(f"/jobs/{job_id}/artifacts/p0.preview.jpg")
    assert preview.status_code == 200 and preview.headers["content-type"] == "image/jpeg"

    after = _artifact_names(client, job)
    assert {"p0.svg", "p0.layer.png", "p0.preview.jpg"} <= set(after)

    # A per-criterion view renders and caches under the criterion's name.
    slug = entry["artifacts"]["slug"]
    one = client.get(entry["artifacts"]["items"][0]["layers"]["svg"])
    assert one.status_code == 200
    by_name = client.get(f"/jobs/{job_id}/artifacts/p0.{slug}.layer.png")
    assert by_name.status_code == 200
    assert f"p0.{slug}.layer.png" in _artifact_names(client, job)


def test_text_only_documents_have_no_layers(client, model):
    job = _run_json(client, {"type": "text", "data": "hello"}, [{"name": "hello", "type": "text"}])
    assert job["result"]["page_geometry"] == [
        {"item": 0, "page": 0, "width": None, "height": None,
         "working_scale": None, "pdf_points": None}
    ]
    assert job["result"]["artifacts"]["items"] == []
    r = client.get(f"/jobs/{job['job_id']}/artifacts/p0.svg")
    assert r.status_code == 404 and "no page image" in r.json()["detail"]
