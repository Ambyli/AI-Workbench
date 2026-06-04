"""Stateless FastAPI server for Kokoro TTS.

Proxies requests to the internal kokoro-app container on port 8080.
Exposes HTTP endpoints on port 8000 for external clients.
"""

import os

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response

APP_URL = os.environ.get("KOKORO_APP_URL", "http://kokoro-app:8080")

app = FastAPI(title="Kokoro TTS API")

http_timeout = httpx.Timeout(120.0, connect=10.0)


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
            json={"text": text, "voice": voice},
            timeout=http_timeout,
        )
        r.raise_for_status()
        data = r.json()
        audio_bytes = bytes.fromhex(data["audio"])
        return Response(content=audio_bytes, media_type="audio/wav")
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Cannot reach kokoro-app: {exc}")
