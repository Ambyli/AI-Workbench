"""Kokoro-82M TTS inference server.

Runs a local FastAPI service on port 8080 that loads the Kokoro model
and exposes /generate and /voices endpoints for internal consumption
by the external API container.
"""

import io
import logging
from contextlib import asynccontextmanager

import soundfile as sf
import torch
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Lazy-loaded — initialized on first request to avoid blocking startup.
_pipeline = None
_voices = None


def _get_pipeline():
    global _pipeline  # noqa: PLW0603
    if _pipeline is None:
        from kokoro import KPipeline
        logger.info("Loading Kokoro model (first request)...")
        _pipeline = KPipeline(lang_code="a")
        logger.info("Kokoro model loaded.")
    return _pipeline


def _get_voices():
    global _voices  # noqa: PLW0603
    if _voices is None:
        from misaki.kokoro import VOICES
        _voices = list(VOICES)
        logger.info("Discovered %d voices.", len(_voices))
    return _voices


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Kokoro inference server starting on :8080")
    yield
    logger.info("Kokoro inference server shutting down")


app = FastAPI(title="Kokoro TTS", lifespan=lifespan)


@app.get("/voices")
def list_voices():
    return {"voices": _get_voices()}


@app.post("/generate")
def generate(text: str, voice: str = "af_heart"):
    voices = _get_voices()
    if voice not in voices:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown voice '{voice}'. Available: {voices}",
        )

    pipeline = _get_pipeline()
    gen = pipeline(text, voice=voice)

    try:
        _, _, audio = next(gen)
    except StopIteration:
        raise HTTPException(status_code=400, detail="No audio generated — check your input text.")

    buf = io.BytesIO()
    sf.write(buf, audio, 24000, format="wav")
    buf.seek(0)

    return JSONResponse(content={"audio": buf.read().hex()}, media_type="application/json")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
