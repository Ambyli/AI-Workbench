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

# Kokoro language codes — the first character of a voice name identifies its language.
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

# Lazy-loaded per language — initialized on first request to avoid blocking startup.
_pipelines: dict = {}
_voices = None


def _get_pipeline(lang_code: str):
    if lang_code not in LANGUAGES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown language '{lang_code}'. Supported: {sorted(LANGUAGES)}",
        )
    if lang_code not in _pipelines:
        from kokoro import KPipeline
        logger.info("Loading Kokoro pipeline for lang_code=%s (%s)...", lang_code, LANGUAGES[lang_code])
        _pipelines[lang_code] = KPipeline(lang_code=lang_code)
        logger.info("Kokoro pipeline for lang_code=%s loaded.", lang_code)
    return _pipelines[lang_code]


def _get_voices():
    global _voices  # noqa: PLW0603
    if _voices is None:
        from huggingface_hub import list_repo_files
        _voices = [
            f.removeprefix("voices/").removesuffix(".pt")
            for f in list_repo_files("hexgrad/Kokoro-82M")
            if f.startswith("voices/") and f.endswith(".pt")
        ]
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


@app.get("/languages")
def list_languages():
    """Return every language Kokoro supports, with its voices."""
    grouped: dict[str, list[str]] = {code: [] for code in LANGUAGES}
    for v in _get_voices():
        prefix = v[0]
        if prefix in grouped:
            grouped[prefix].append(v)
    return {
        "languages": [
            {"code": code, "name": name, "voices": sorted(grouped[code])}
            for code, name in LANGUAGES.items()
        ]
    }


@app.post("/generate")
def generate(text: str, voice: str = "af_heart"):
    voices = _get_voices()
    if voice not in voices:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown voice '{voice}'. Available: {voices}",
        )

    pipeline = _get_pipeline(voice[0])
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
    uvicorn.run(app, host="0.0.0.0", port=8085)
