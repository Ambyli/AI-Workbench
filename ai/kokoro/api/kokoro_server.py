"""Stateless FastAPI server for Kokoro TTS.

Proxies requests to the kokoro-app inference container.
Exposes an OpenAI-compatible /v1/audio/speech endpoint for LiteLLM routing,
plus /voices and /generate for direct access.
"""

import hashlib
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastmcp import FastMCP
from pydantic import BaseModel

APP_URL = os.environ.get("KOKORO_APP_URL", "http://kokoro-app:8085")

mcp = FastMCP("Kokoro TTS")
mcp_app = mcp.http_app(path="/")

http_timeout = httpx.Timeout(120.0, connect=10.0)

# Maps OpenAI voice names to Kokoro equivalents. All aliases resolve to English
# voices — Kokoro voices are language-specific, so callers who want another
# language must pass a Kokoro voice name directly (see GET /voices or /languages).
# Unrecognised names are passed through as-is.
VOICE_MAP = {
    "alloy": "af_heart",
    "echo": "am_adam",
    "fable": "bf_emma",
    "onyx": "am_michael",
    "nova": "af_sarah",
    "shimmer": "af_bella",
}

AUDIO_DIR = Path(__file__).parent / "audio"
AUDIO_BASE_URL = os.environ.get("AUDIO_BASE_URL", "http://localhost:8000")
# How long a `text_to_speech` link keeps working: a WAV older than this is
# deleted (with its cache entry) on the next save, which keeps the directory
# bounded. Reusing a cached WAV resets its age. Files never outlive the process
# either: startup clears the directory.
AUDIO_MAX_AGE_SECONDS = 24 * 60 * 60
_audio_cache: dict[str, str] = {}
_audio_lock = threading.Lock()


def _clean_stale_audio():
    """Remove all audio files on startup (they're ephemeral)."""
    if not AUDIO_DIR.exists():
        return
    for f in AUDIO_DIR.glob("*.wav"):
        f.unlink(missing_ok=True)


def _ensure_audio_dir():
    AUDIO_DIR.mkdir(exist_ok=True)


def _prune_old_audio(now: float) -> None:
    """Delete WAVs older than AUDIO_MAX_AGE_SECONDS and their cache entries.

    Caller holds ``_audio_lock``.
    """
    cutoff = now - AUDIO_MAX_AGE_SECONDS
    removed = set()
    for f in AUDIO_DIR.glob("*.wav"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                removed.add(f.name)
        except FileNotFoundError:
            removed.add(f.name)
    if removed:
        for key in [k for k, name in _audio_cache.items() if name in removed]:
            del _audio_cache[key]


def _save_audio(text: str, voice: str, audio_bytes: bytes) -> str:
    """Save audio to disk and return the filename.

    A cached filename is returned only while its file still exists (its age is
    then reset, so the link lives another AUDIO_MAX_AGE_SECONDS); otherwise the
    bytes are written again. Every save first prunes expired files.
    """
    key = f"{text}|{voice}"
    with _audio_lock:
        _prune_old_audio(time.time())
        cached = _audio_cache.get(key)
        if cached is not None:
            try:
                os.utime(AUDIO_DIR / cached)
                return cached
            except FileNotFoundError:
                del _audio_cache[key]
        filename = hashlib.md5(key.encode()).hexdigest()[:12] + ".wav"
        (AUDIO_DIR / filename).write_bytes(audio_bytes)
        _audio_cache[key] = filename
        return filename


def _app_error(exc: httpx.HTTPError) -> tuple[int, Any]:
    """``(status, detail)`` for a failed kokoro-app call.

    An upstream 4xx is the caller's mistake (an unknown voice is kokoro-app's
    400): keep its status and its ``detail``. Anything else -- unreachable,
    timed out, an upstream 5xx -- is a 502.
    """
    if isinstance(exc, httpx.HTTPStatusError) and 400 <= exc.response.status_code < 500:
        response = exc.response
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict) and "detail" in body:
            return response.status_code, body["detail"]
        return response.status_code, response.text or response.reason_phrase
    return 502, f"Cannot reach kokoro-app: {exc}"


def _raise_app_error(exc: httpx.HTTPError):
    status, detail = _app_error(exc)
    raise HTTPException(status_code=status, detail=detail)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start with an empty audio directory, then run the MCP app's lifespan
    (its session manager must run, so ours wraps it rather than replacing it)."""
    _ensure_audio_dir()
    _clean_stale_audio()
    async with mcp_app.lifespan(app):
        yield


app = FastAPI(title="Kokoro TTS API", lifespan=lifespan)


class TTSRequest(BaseModel):
    model: str
    input: str
    voice: str = "alloy"
    response_format: Optional[str] = "wav"
    speed: Optional[float] = 1.0


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/audio/{filename}")
def serve_audio(filename: str):
    # Prevent path traversal
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    filepath = AUDIO_DIR / filename
    if not filepath.is_file():
        raise HTTPException(status_code=404, detail="Audio file not found")
    return FileResponse(filepath, media_type="audio/wav")


@app.get("/voices")
def list_voices():
    try:
        r = httpx.get(f"{APP_URL}/voices", timeout=http_timeout)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as exc:
        _raise_app_error(exc)


@app.get("/languages")
def list_languages():
    try:
        r = httpx.get(f"{APP_URL}/languages", timeout=http_timeout)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as exc:
        _raise_app_error(exc)


@app.post("/generate")
def generate(text: str, voice: str = "af_heart"):
    try:
        r = httpx.post(
            f"{APP_URL}/generate",
            params={"text": text, "voice": voice},
            timeout=http_timeout,
        )
        r.raise_for_status()
        data = r.json()
        audio_bytes = bytes.fromhex(data["audio"])
        return Response(content=audio_bytes, media_type="audio/wav")
    except httpx.HTTPError as exc:
        _raise_app_error(exc)


@app.post("/v1/audio/speech")
def openai_speech(req: TTSRequest):
    kokoro_voice = VOICE_MAP.get(req.voice, req.voice)
    try:
        r = httpx.post(
            f"{APP_URL}/generate",
            params={"text": req.input, "voice": kokoro_voice},
            timeout=http_timeout,
        )
        r.raise_for_status()
        data = r.json()
        audio_bytes = bytes.fromhex(data["audio"])
        return Response(content=audio_bytes, media_type="audio/wav")
    except httpx.HTTPError as exc:
        _raise_app_error(exc)


@mcp.tool()
def text_to_speech(text: str, voice: str = "alloy") -> str:
    """Generate speech audio from text using Kokoro TTS.

    Args:
        text: The text to convert to speech.
        voice: Voice name. OpenAI aliases (alloy, echo, fable, onyx, nova, shimmer)
            all map to English voices. To speak another language, pass a Kokoro
            voice name whose prefix identifies the language, e.g.
            jf_alpha (Japanese), zf_xiaobei (Mandarin), ff_siwis (French).
            Call GET /languages to see voices grouped by language, or /voices for
            the flat list.

    Returns:
        A URL to the generated audio file (set AUDIO_BASE_URL env var to override the default host).
        The link stays valid for at least a day (AUDIO_MAX_AGE_SECONDS) unless kokoro-api restarts.
    """
    _ensure_audio_dir()
    kokoro_voice = VOICE_MAP.get(voice, voice)
    try:
        r = httpx.post(
            f"{APP_URL}/generate",
            params={"text": text, "voice": kokoro_voice},
            timeout=http_timeout,
        )
        r.raise_for_status()
        data = r.json()
        audio_bytes = bytes.fromhex(data["audio"])
        filename = _save_audio(text, voice, audio_bytes)
        return f"{AUDIO_BASE_URL}/audio/{filename}"
    except httpx.HTTPError as exc:
        # A 4xx from kokoro-app (an unknown voice) says what to fix: relay its
        # detail; unreachable / 5xx keeps httpx's own message.
        status, detail = _app_error(exc)
        return f"Error generating audio: {detail if status != 502 else exc}"


app.mount("/mcp", mcp_app)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
