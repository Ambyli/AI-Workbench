"""madlad-api (``ai/madlad/api/madlad_server.py``) without madlad-app.

``httpx`` calls to madlad-app are replaced by canned responses (no model, no
GPU, no network): madlad-app's 4xx (an unknown ``target_lang``) is passed
through with its status and ``detail`` -- on ``/translate`` and in the
``translate`` MCP tool's text -- while unreachable or a 5xx is a 502; a
malformed body is FastAPI's own 422.

    UV_LINK_MODE=copy uv run --no-sync python -m pytest unit-tests/madlad -q -p no:cacheprovider
"""

from __future__ import annotations

import pathlib
import sys

import httpx
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ai" / "madlad" / "api"))

import madlad_server as ms  # noqa: E402

UNKNOWN = "Unknown target_lang 'xx'. Use /languages to list supported codes."


def _client():
    from fastapi.testclient import TestClient

    return TestClient(ms.app)


@pytest.fixture
def app_says(monkeypatch):
    """Make madlad-app answer ``status`` with ``body`` (or raise ``exc``)."""

    def install(status=None, body=None, exc=None):
        def call(url, json=None, timeout=None, **kw):
            if exc is not None:
                raise exc
            return httpx.Response(status, json=body, request=httpx.Request("POST", url))

        monkeypatch.setattr(ms.httpx, "post", call)
        monkeypatch.setattr(ms.httpx, "get", call)

    return install


def test_translation_passes_through(app_says):
    app_says(200, {"translated": "Hola mundo"})
    response = _client().post("/translate", json={"text": "Hello world", "target_lang": "es"})
    assert (response.status_code, response.json()) == (200, {"translated": "Hola mundo"})
    assert ms.translate("Hello world", "es") == "Hola mundo"


def test_unknown_target_lang_is_passed_through_as_400(app_says):
    app_says(400, {"detail": UNKNOWN})
    response = _client().post("/translate", json={"text": "hi", "target_lang": "xx"})
    assert (response.status_code, response.json()) == (400, {"detail": UNKNOWN})
    assert ms.translate("hi", "xx") == f"Error translating: {UNKNOWN}"


@pytest.mark.parametrize("kind", ["5xx", "unreachable"])
def test_upstream_5xx_or_unreachable_is_502(app_says, kind):
    if kind == "5xx":
        app_says(500, {"detail": "No translation produced."})
    else:
        app_says(exc=httpx.ConnectError("connection refused"))
    client = _client()
    for response in (
        client.post("/translate", json={"text": "hi", "target_lang": "es"}),
        client.get("/languages"),
    ):
        assert response.status_code == 502, response.text
        assert response.json()["detail"].startswith("Cannot reach madlad-app: ")
    message = ms.translate("hi", "es")
    assert message.startswith("Error translating: ") and "Cannot reach" not in message


def test_malformed_json_is_fastapis_422(app_says):
    app_says(200, {"translated": "never reached"})
    response = _client().post("/translate", content=b"{not json",
                              headers={"content-type": "application/json"})
    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "json_invalid"
