"""Stateless FastAPI server for Kokoro TTS.

Proxies requests to the kokoro-app inference container.
Exposes an OpenAI-compatible /v1/audio/speech endpoint for LiteLLM routing,
plus /voices and /generate for direct access.
"""

import hashlib
import os
import threading
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastmcp import FastMCP
from pydantic import BaseModel

APP_URL = os.environ.get("KOKORO_APP_URL", "http://kokoro-app:8085")

mcp = FastMCP("Kokoro TTS")
mcp_app = mcp.http_app(path="/")

app = FastAPI(title="Kokoro TTS API", lifespan=mcp_app.lifespan)

http_timeout = httpx.Timeout(120.0, connect=10.0)

# Maps OpenAI voice names to Kokoro equivalents.
# Unrecognised names are passed through so callers can use Kokoro voices directly.
VOICE_MAP = {
    "alloy": "af_heart",
    "echo": "am_adam",
    "fable": "bf_emma",
    "onyx": "am_michael",
    "nova": "af_sarah",
    "shimmer": "af_bella",
}

# Kokoro language codes. Mirror of the catalog in kokoro-app so /languages
# can respond without a round-trip to the inference container.
LANGUAGES: dict[str, str] = {
    "a": "American English",
    "b": "British English",
    "e": "Spanish",
    "f": "French",
    "h": "Hindi",
    "i": "Italian",
    "j": "Japanese",
    "p": "Brazilian Portuguese",
    "z": "Mandarin",
}

AUDIO_DIR = Path(__file__).parent / "audio"
AUDIO_BASE_URL = os.environ.get("AUDIO_BASE_URL", "http://localhost:8000")
_audio_cache: dict[str, str] = {}
_audio_lock = threading.Lock()


def _clean_stale_audio():
    """Remove all audio files on startup (they're ephemeral)."""
    if not AUDIO_DIR.exists():
        return
    for f in AUDIO_DIR.glob("*.wav"):
        f.unlink()


def _ensure_audio_dir():
    AUDIO_DIR.mkdir(exist_ok=True)


def _save_audio(text: str, voice: str, audio_bytes: bytes) -> str:
    """Save audio to disk and return the filename."""
    key = f"{text}|{voice}"
    with _audio_lock:
        if key in _audio_cache:
            return _audio_cache[key]
        filename = hashlib.md5(key.encode()).hexdigest()[:12] + ".wav"
        (AUDIO_DIR / filename).write_bytes(audio_bytes)
        _audio_cache[key] = filename
        return filename


class TTSRequest(BaseModel):
    model: str
    input: str
    voice: str = "alloy"
    response_format: Optional[str] = "wav"
    speed: Optional[float] = 1.0
    language: Optional[str] = None


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
        raise HTTPException(status_code=502, detail=f"Cannot reach kokoro-app: {exc}")


@app.get("/languages")
def list_languages():
    return {"languages": [{"code": code, "name": name} for code, name in LANGUAGES.items()]}


@app.post("/generate")
def generate(text: str, voice: str = "af_heart", language: Optional[str] = None):
    params = {"text": text, "voice": voice}
    if language is not None:
        params["language"] = language
    try:
        r = httpx.post(f"{APP_URL}/generate", params=params, timeout=http_timeout)
        r.raise_for_status()
        data = r.json()
        audio_bytes = bytes.fromhex(data["audio"])
        return Response(content=audio_bytes, media_type="audio/wav")
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Cannot reach kokoro-app: {exc}")


@app.post("/v1/audio/speech")
def openai_speech(req: TTSRequest):
    kokoro_voice = VOICE_MAP.get(req.voice, req.voice)
    params = {"text": req.input, "voice": kokoro_voice}
    if req.language is not None:
        params["language"] = req.language
    try:
        r = httpx.post(f"{APP_URL}/generate", params=params, timeout=http_timeout)
        r.raise_for_status()
        data = r.json()
        audio_bytes = bytes.fromhex(data["audio"])
        return Response(content=audio_bytes, media_type="audio/wav")
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Cannot reach kokoro-app: {exc}")


@mcp.tool()
def text_to_speech(text: str, voice: str = "alloy", language: Optional[str] = None) -> str:
    """Generate speech audio from text using Kokoro TTS.

    Args:
        text: The text to convert to speech.
        voice: Voice name. OpenAI aliases (alloy, echo, fable, onyx, nova, shimmer) map to
            English voices. Any Kokoro voice can also be passed directly (e.g. jf_alpha,
            zf_xiaobei, ff_siwis) — see GET /voices.
        language: Optional Kokoro language code. When omitted it is inferred from the
            voice-name prefix. Codes: a=American English, b=British English, e=Spanish,
            f=French, h=Hindi, i=Italian, j=Japanese, p=Brazilian Portuguese, z=Mandarin.

    Returns:
        A URL to the generated audio file (set AUDIO_BASE_URL env var to override the default host).
    """
    _ensure_audio_dir()
    _clean_stale_audio()
    kokoro_voice = VOICE_MAP.get(voice, voice)
    params = {"text": text, "voice": kokoro_voice}
    if language is not None:
        params["language"] = language
    try:
        r = httpx.post(f"{APP_URL}/generate", params=params, timeout=http_timeout)
        r.raise_for_status()
        data = r.json()
        audio_bytes = bytes.fromhex(data["audio"])
        filename = _save_audio(text, voice, audio_bytes)
        return f"{AUDIO_BASE_URL}/audio/{filename}"
    except httpx.HTTPError as exc:
        return f"Error generating audio: {exc}"


app.mount("/mcp", mcp_app)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
