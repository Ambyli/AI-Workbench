"""kokoro-api (``ai/kokoro/api/kokoro_server.py``) without kokoro-app.

``httpx`` calls to kokoro-app are replaced by canned responses, and
``AUDIO_DIR`` points at a temp dir, so this needs no model, GPU or network:

* the ``text_to_speech`` audio links: a repeated text/voice keeps working
  (every call used to delete every WAV while the cache kept the names, so
  the cached URL 404ed), files older than ``AUDIO_MAX_AGE_SECONDS`` are
  pruned with their cache entries, and the directory is cleared only at
  startup -- by a lifespan that still runs the MCP app's;
* kokoro-app's 4xx (an unknown voice) is passed through with its status and
  ``detail``; unreachable or a 5xx is a 502.

    UV_LINK_MODE=copy uv run --no-sync python -m pytest unit-tests/kokoro -q -p no:cacheprovider
"""

from __future__ import annotations

import os
import pathlib
import sys
import time

import httpx
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ai" / "kokoro" / "api"))

import kokoro_server as ks  # noqa: E402

WAV = b"RIFF\x24\x00\x00\x00WAVEfmt "  # any bytes: kokoro-api never parses them
UNKNOWN_VOICE = "Unknown voice 'zz_nobody'. Available: ['af_heart', 'am_adam']"


def _response(status: int, method: str = "POST", **kw) -> httpx.Response:
    return httpx.Response(status, request=httpx.Request(method, f"{ks.APP_URL}/x"), **kw)


@pytest.fixture
def audio_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(ks, "AUDIO_DIR", tmp_path)
    monkeypatch.setattr(ks, "_audio_cache", {})
    return tmp_path


@pytest.fixture
def app_ok(monkeypatch):
    """kokoro-app answers every /generate with the same WAV."""
    calls = []

    def post(url, params=None, timeout=None, **kw):
        calls.append(params)
        return _response(200, json={"audio": WAV.hex()})

    monkeypatch.setattr(ks.httpx, "post", post)
    return calls


def _filename(url: str) -> str:
    assert url.startswith(f"{ks.AUDIO_BASE_URL}/audio/"), url
    return url.rsplit("/", 1)[1]


def _client():
    from fastapi.testclient import TestClient

    return TestClient(ks.app)


# -- the audio links -------------------------------------------------------------------------


def test_repeated_text_keeps_its_link(audio_dir, app_ok):
    first = ks.text_to_speech("Hello there", "alloy")
    other = ks.text_to_speech("Something else", "alloy")
    again = ks.text_to_speech("Hello there", "alloy")
    assert again == first and other != first
    client = _client()
    for url in (first, other):
        response = client.get(f"/audio/{_filename(url)}")
        assert response.status_code == 200, response.text
        assert response.content == WAV
        assert response.headers["content-type"] == "audio/wav"


def test_old_files_are_pruned_with_their_cache_entries(audio_dir, app_ok):
    old = _filename(ks.text_to_speech("old text"))
    stale = time.time() - ks.AUDIO_MAX_AGE_SECONDS - 60
    os.utime(audio_dir / old, (stale, stale))
    fresh = _filename(ks.text_to_speech("new text"))
    assert not (audio_dir / old).exists()                 # pruned on the next save
    assert "old text|alloy" not in ks._audio_cache
    assert (audio_dir / fresh).exists()
    # The same text again is written anew, not served from a dead cache entry.
    assert (audio_dir / _filename(ks.text_to_speech("old text"))).exists()


def test_a_reused_file_gets_a_fresh_age(audio_dir, app_ok):
    name = _filename(ks.text_to_speech("keep me"))
    almost = time.time() - ks.AUDIO_MAX_AGE_SECONDS + 60
    os.utime(audio_dir / name, (almost, almost))
    assert _filename(ks.text_to_speech("keep me")) == name
    assert (audio_dir / name).stat().st_mtime > almost + 30   # reset by the reuse
    ks.text_to_speech("another")                              # a later save does not prune it
    assert (audio_dir / name).exists()


def test_a_cached_name_whose_file_is_gone_is_rewritten(audio_dir, app_ok):
    name = _filename(ks.text_to_speech("vanishing"))
    (audio_dir / name).unlink()
    assert _filename(ks.text_to_speech("vanishing")) == name
    assert (audio_dir / name).read_bytes() == WAV


def test_startup_clears_the_directory_and_runs_the_mcp_lifespan(audio_dir):
    (audio_dir / "leftover.wav").write_bytes(WAV)
    with _client() as client:                             # runs the app's lifespan
        assert not (audio_dir / "leftover.wav").exists()
        # The MCP app's session manager is running (without its lifespan the
        # streamable-HTTP transport refuses every request).
        response = client.post(
            "/mcp/",
            headers={"Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                             "clientInfo": {"name": "test", "version": "0"}}},
        )
        assert response.status_code == 200, response.text
        assert "Kokoro TTS" in response.text


# -- kokoro-app errors -------------------------------------------------------------------------


@pytest.fixture
def app_says(monkeypatch):
    """Make kokoro-app answer ``status`` with ``body`` (or raise ``exc``)."""

    def install(status=None, body=None, exc=None):
        def call(url, params=None, timeout=None, **kw):
            if exc is not None:
                raise exc
            return _response(status, json=body)

        monkeypatch.setattr(ks.httpx, "post", call)
        monkeypatch.setattr(ks.httpx, "get", call)

    return install


def test_unknown_voice_is_passed_through_as_400(audio_dir, app_says):
    app_says(400, {"detail": UNKNOWN_VOICE})
    client = _client()
    for response in (
        client.post("/v1/audio/speech", json={"model": "kokoro", "input": "hi", "voice": "zz_nobody"}),
        client.post("/generate", params={"text": "hi", "voice": "zz_nobody"}),
    ):
        assert response.status_code == 400, response.text
        assert response.json() == {"detail": UNKNOWN_VOICE}
    assert ks.text_to_speech("hi", "zz_nobody") == f"Error generating audio: {UNKNOWN_VOICE}"
    assert list(audio_dir.iterdir()) == []


def test_other_upstream_4xx_keep_their_status(app_says):
    app_says(404, {"detail": "Not Found"})
    response = _client().get("/voices")
    assert (response.status_code, response.json()) == (404, {"detail": "Not Found"})


@pytest.mark.parametrize("kind", ["5xx", "unreachable"])
def test_upstream_5xx_or_unreachable_is_502(audio_dir, app_says, kind):
    if kind == "5xx":
        app_says(500, {"detail": "boom"})
    else:
        app_says(exc=httpx.ConnectError("connection refused"))
    client = _client()
    for response in (
        client.post("/v1/audio/speech", json={"model": "kokoro", "input": "hi"}),
        client.post("/generate", params={"text": "hi"}),
        client.get("/voices"),
        client.get("/languages"),
    ):
        assert response.status_code == 502, response.text
        assert response.json()["detail"].startswith("Cannot reach kokoro-app: ")
    message = ks.text_to_speech("hi")
    assert message.startswith("Error generating audio: ") and "Cannot reach" not in message
