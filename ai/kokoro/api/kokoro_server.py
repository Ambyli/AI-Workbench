"""Stateless FastAPI server for Kokoro TTS.

Proxies requests to the kokoro-app inference container.
Exposes an OpenAI-compatible /v1/audio/speech endpoint for LiteLLM routing,
plus /voices and /generate for direct access.
"""

import os
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

APP_URL = os.environ.get("KOKORO_APP_URL", "http://kokoro-app:8085")

app = FastAPI(title="Kokoro TTS API")

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


class TTSRequest(BaseModel):
    model: str
    input: str
    voice: str = "alloy"
    response_format: Optional[str] = "wav"
    speed: Optional[float] = 1.0


@app.get("/voices")
def list_voices():
    try:
        r = httpx.get(f"{APP_URL}/voices", timeout=http_timeout)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Cannot reach kokoro-app: {exc}")


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
        raise HTTPException(status_code=502, detail=f"Cannot reach kokoro-app: {exc}")


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
        raise HTTPException(status_code=502, detail=f"Cannot reach kokoro-app: {exc}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
