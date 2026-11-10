"""``detector.schemas.json`` against the code (root ``CLAUDE.md`` § Postman
collections; the file-wide checks -- coverage, valid schemas, every
collection body, the rendered copies -- are ``unit-tests/apidocs``).

(a) **The generated schemas are current**: the JSON spelling of
    ``POST /detect`` (``models.DetectRequest``) and the ``detect_objects``
    tool's arguments (its FastMCP ``inputSchema``). ``API_SCHEMAS_UPDATE=1``
    rewrites them and re-renders the copies::

        API_SCHEMAS_UPDATE=1 UV_LINK_MODE=copy uv run --no-sync --with pytest --package detector \\
            python -m pytest unit-tests/detector/test_api_schemas.py -q -p no:cacheprovider

(b) **Real responses validate**, with no model and no network: ``app`` is
    imported (its lifespan, which would load the weights, never runs) and a
    fake detector family is put in its place, so every request runs the real
    parsing, decode, grouping and ``build_response``:

    * ``/health`` loaded (200) and not loaded (503, with and without a loader
      error); ``/metrics``;
    * ``POST /detect`` -- multipart, JSON base64 and JSON URL (the fetch
      replaced; the SSRF check is not), every collection body, and the
      errors, which must be the generic ``{"detail": "<str>"}``;
    * MCP, in memory through ``fastmcp.Client``: ``tools/list`` as the
      server sends it, and ``detect_objects`` -- its ``structuredContent``
      against the entry and the JSON text inside it against the entry's
      ``contentSchema``, a result and each kind of error-as-data;
    * ``models.build_response`` on ``test_detector_units._Fake``.

    The last test checks that every declared status was produced.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import pathlib
import sys
from typing import Any, Dict

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVICE_DIR = ROOT / "ai" / "detector"
sys.path.insert(0, str(SERVICE_DIR))

from common.apidocs import generate, postman, render, schemas  # noqa: E402
from detectors.base import Detection, OpenVocabularyDetector  # noqa: E402
import models  # noqa: E402

SCHEMAS_PATH = SERVICE_DIR / "detector.schemas.json"
COLLECTION_PATH = SERVICE_DIR / "detector.postman_collection.json"
PREFIX = "/v1/detector"           # the LiteLLM pass-through the collection calls through

GENERIC_ERROR = {"type": "object", "additionalProperties": False, "required": ["detail"],
                 "properties": {"detail": {"type": "string"}}}

SEEN: set = set()
RAN: set = set()
PRODUCERS = {"health", "detect", "mcp"}


@pytest.fixture(scope="module")
def app_module():
    import app

    return app


@pytest.fixture(scope="module")
def schema_file():
    return schemas.load_schemas(SCHEMAS_PATH)


# -- (a) the generated schemas ---------------------------------------------------------------


def generated_schemas(app_module) -> Dict[tuple, Any]:
    out: Dict[tuple, Any] = {("$defs", "detect_request"): generate.from_pydantic(models.DetectRequest)}
    for name, schema in asyncio.run(generate.from_mcp(app_module.mcp, source="app.mcp")).items():
        out[("endpoints", f"MCP {name}", "arguments")] = schema
    return out


def test_generated_schemas_are_current(app_module):
    data = json.loads(postman.read_text(SCHEMAS_PATH))
    changed = generate.apply_generated(data, generated_schemas(app_module))
    if changed and os.environ.get("API_SCHEMAS_UPDATE") == "1":
        schemas.dump_schemas(data, SCHEMAS_PATH)
        render.render_service(SCHEMAS_PATH)
        changed = []
    assert changed == [], (f"{SCHEMAS_PATH.name} is stale at {changed}: rerun this test with "
                           "API_SCHEMAS_UPDATE=1 (it rewrites them and re-renders the copies)")


# -- checking a response --------------------------------------------------------------------------


def check(schema_file, method: str, path: str, response) -> Any:
    """Validate ``response`` against the entry of ``method <PREFIX>path``."""
    key = f"{method} {PREFIX}{path}"
    endpoint = schema_file.endpoints[key]
    status = str(response.status_code)
    spec = endpoint.responses().get(status)
    if spec is None:
        assert response.status_code >= 400, f"{key}: {status} has no entry in {SCHEMAS_PATH.name}"
        errors = schemas.validate(GENERIC_ERROR, response.json())
        assert errors == [], f"{key}: {status} is not the generic error body, so it needs an entry: {errors}"
        return response.json()
    SEEN.add((key, status))
    media = response.headers.get("content-type", "").split(";")[0].strip().lower()
    assert media == spec.content_type, (key, status, media)
    if spec.schema is None:
        return None
    body = response.json()
    errors = schemas.validate(endpoint.standalone(spec.schema), body)
    assert errors == [], f"{key} {status}: {errors}"
    return body


def check_mcp(schema_file, key: str, result: Dict[str, Any]) -> Any:
    """``result`` (a JSON-RPC result as sent) against an MCP entry: a
    ``tools/call``'s ``structuredContent``, and the JSON text inside it
    against the ``contentSchema``. Returns the decoded content (or the
    result)."""
    endpoint = schema_file.endpoints[key]
    schema = endpoint.standalone(endpoint.entry["result"])
    if "arguments" not in endpoint.entry:
        assert schemas.validate(schema, result) == [], key
        SEEN.add((key, "result"))
        return result
    assert result.get("isError") is not True, result
    structured = result["structuredContent"]
    assert schemas.validate(schema, structured) == [], key
    assert result["content"][0]["text"] == structured["result"]
    inner = {"$defs": schema.get("$defs", {}), **schema["properties"]["result"]["contentSchema"]}
    content = json.loads(structured["result"])
    assert schemas.validate(inner, content) == [], (key, content)
    SEEN.add((key, "result"))
    return content


# -- a fake detector family ------------------------------------------------------------------------


class Boxes(OpenVocabularyDetector):
    """Boxes per label (in prepared pixels), whatever the image shows."""

    family = "fake"

    def __init__(self, per_label):
        super().__init__("fake/model", "cpu", max_image_side=1024)
        self.per_label = per_label
        self._loaded = True

    def _detect_prepared(self, image, labels, threshold):
        return [Detection(label=label, score=score, box=box)
                for label in labels for box, score in self.per_label.get(label, []) if score >= threshold]


BOXES = {"tree": [((10, 10, 50, 40), 0.9), ((300, 20, 420, 210), 0.41)],
         "house": [((60, 5, 200, 190), 0.65)],
         "roof vent": [((-5, -5, 30, 30), 0.27)]}


def png(width: int = 400, height: int = 200) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def loaded(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "_detector", Boxes(BOXES))
    monkeypatch.setattr(app_module, "_load_error", None)
    monkeypatch.setattr(app_module, "_device", "cpu")

    async def fetch(url: str) -> bytes:          # no network: a public URL "answers" a 640x480 PNG
        return png(640, 480)

    return app_module, fetch


def client_of(app_module):
    from fastapi.testclient import TestClient

    return TestClient(app_module.app)            # no `with`: the lifespan (the weights) never runs


# -- (b) producers ------------------------------------------------------------------------------------


def test_health_and_metrics(schema_file, app_module, monkeypatch):
    client = client_of(app_module)
    monkeypatch.setattr(app_module, "_device", "cpu")
    monkeypatch.setattr(app_module, "_detector", Boxes({}))
    monkeypatch.setattr(app_module, "_load_error", None)
    assert check(schema_file, "GET", "/health", client.get("/health"))["loaded"] is True
    monkeypatch.setattr(app_module, "_detector", None)
    monkeypatch.setattr(app_module, "_load_error", "NotImplementedError: Grounding DINO is not implemented")
    assert check(schema_file, "GET", "/health", client.get("/health"))["status"] == "degraded"
    monkeypatch.setattr(app_module, "_load_error", None)
    assert check(schema_file, "GET", "/health", client.get("/health"))["error"] is None
    assert client.get("/health").status_code == 503
    check(schema_file, "GET", "/metrics", client.get("/metrics"))
    RAN.add("health")


def test_detect_responses(schema_file, loaded, monkeypatch):
    app_module, fetch = loaded
    monkeypatch.setattr(app_module, "_fetch_url", fetch)
    client = client_of(app_module)
    image = png()
    multipart = client.post("/detect", files={"file": ("page.png", image, "image/png")},
                            data={"labels": '["tree", "house", "car", "tree"]', "threshold": "0.2",
                                  "max_per_label": "1"})
    body = check(schema_file, "POST", "/detect", multipart)
    assert body["labels"] == ["tree", "house", "car"] and body["by_label"]["car"] == []
    assert body["counts"] == {"tree": 1, "house": 1, "car": 0}
    as_json = {"image": {"data": base64.b64encode(image).decode(), "type": "base64"},
               "labels": "tree, roof vent", "threshold": 0.25}
    body = check(schema_file, "POST", "/detect", client.post("/detect", json=as_json))
    assert body["by_label"]["roof vent"][0]["box"][:2] == [0.0, 0.0]          # clipped
    data_url = {**as_json, "image": {"data": "data:image/png;base64," + as_json["image"]["data"], "type": "base64"}}
    check(schema_file, "POST", "/detect", client.post("/detect", json=data_url))
    url = {"image": {"data": "https://images.example.com/roof.jpg", "type": "url"}, "labels": ["house"]}
    assert check(schema_file, "POST", "/detect", client.post("/detect", json=url))["image"] == {"width": 640,
                                                                                               "height": 480}
    # Every collection body (the 1x1 PNG placeholder is a 400 by design).
    collection = postman.load(COLLECTION_PATH)
    values = postman.variables(collection)
    statuses = []
    for req in postman.requests(collection):
        if postman.endpoint_key(req) != f"POST {PREFIX}/detect":
            continue
        sent = postman.body(req, values)
        if sent.content_type == postman.MULTIPART:
            response = client.post("/detect", files={"file": ("f.png", image, "image/png")},
                                   data={k: v for k, v in sent.value.items() if k != "file"})
        else:
            response = client.post("/detect", json=sent.value)
        check(schema_file, "POST", "/detect", response)
        statuses.append(response.status_code)
    assert statuses == [200, 400, 200]
    # The errors: generic bodies.
    for response in (
        client.post("/detect", json={**as_json, "labels": []}),
        client.post("/detect", json={**as_json, "threshold": 2}),
        client.post("/detect", json={"labels": ["tree"]}),
        client.post("/detect", json={**as_json, "image": {"data": base64.b64encode(png(20, 20)).decode(),
                                                          "type": "base64"}}),
        client.post("/detect", files={"labels": (None, "tree")}),
        client.post("/detect", content=b"labels=tree", headers={"content-type": "text/plain"}),
        client.post("/detect", content=b"{not json", headers={"content-type": "application/json"}),
    ):
        assert response.status_code == 400, response.text
        check(schema_file, "POST", "/detect", response)
    monkeypatch.setattr(app_module, "_detector", None)
    response = client.post("/detect", json=as_json)
    assert response.status_code == 503
    check(schema_file, "POST", "/detect", response)
    RAN.add("detect")


def test_detect_body_from_the_unit_tests_fake(schema_file):
    from test_detector_units import _Fake

    detector = _Fake([((10, 10, 50, 25), 0.9), ((0, 0, 400, 200), 0.31)], max_image_side=100)
    from PIL import Image

    labels = ["tree", "house"]
    detections = detector.detect(Image.new("RGB", (400, 200), "white"), labels, 0.1)
    body = models.build_response(model="fake/model", device="cpu", family=detector.family, elapsed_ms=12.345,
                                 threshold=0.1, labels=labels, width=400, height=200,
                                 grouped=models.apply_max_per_label(detections, labels, None))
    endpoint = schema_file.endpoints[f"POST {PREFIX}/detect"]
    assert schemas.validate(endpoint.standalone(endpoint.responses()["200"].schema), body) == []


# -- MCP, in memory ----------------------------------------------------------------------------------


def _wire(model) -> Dict[str, Any]:
    return model.model_dump(mode="json", by_alias=True, exclude_none=True)


def test_mcp_tools(schema_file, loaded, monkeypatch):
    from fastmcp import Client

    app_module, fetch = loaded
    image = base64.b64encode(png()).decode()

    async def go():
        async with Client(app_module.mcp) as client:
            listed = _wire(await client.list_tools_mcp())
            check_mcp(schema_file, "MCP tools/list", listed)
            ok = check_mcp(schema_file, "MCP detect_objects", _wire(await client.call_tool_mcp(
                "detect_objects", {"image": image, "labels": ["tree", "house", "car"], "max_per_label": 1})))
            assert "detections" not in ok and ok["by_label"]["car"] == []
            bad_labels = check_mcp(schema_file, "MCP detect_objects", _wire(await client.call_tool_mcp(
                "detect_objects", {"image": image, "labels": [" "]})))
            assert set(bad_labels) == {"error"}
            blocked = check_mcp(schema_file, "MCP detect_objects", _wire(await client.call_tool_mcp(
                "detect_objects", {"image": "http://127.0.0.1/roof.jpg", "labels": ["tree"]})))
            assert blocked["status"] == 400
            tiny = check_mcp(schema_file, "MCP detect_objects", _wire(await client.call_tool_mcp(
                "detect_objects", {"image": base64.b64encode(png(8, 8)).decode(), "labels": ["tree"]})))
            assert tiny["status"] == 400
            monkeypatch.setattr(app_module, "_detector", None)
            unloaded = check_mcp(schema_file, "MCP detect_objects", _wire(await client.call_tool_mcp(
                "detect_objects", {"image": image, "labels": ["tree"]})))
            assert unloaded["status"] == 503

    asyncio.run(go())
    RAN.add("mcp")


# -- last: every declared status was produced --------------------------------------------------------


def test_every_declared_response_was_produced(schema_file):
    if RAN != PRODUCERS:
        pytest.skip(f"needs the whole module (ran: {sorted(RAN)})")
    declared = {(key, status) for key, e in schema_file.endpoints.items() for status in e.responses()}
    declared |= {(key, "result") for key, e in schema_file.endpoints.items() if e.is_mcp}
    assert declared - SEEN == set(), "declared but never produced by a real response"
