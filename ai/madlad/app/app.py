"""MADLAD-400 translation inference server.

Runs a local FastAPI service on port 8085 that loads the MADLAD-400
translation model via CTranslate2 and exposes /translate and /languages
endpoints for internal consumption by the external API container.
"""

import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MODEL_NAME = os.environ.get("MADLAD_MODEL", "jbochi/madlad400-3b-mt")
CACHE_DIR = "/root/.cache/huggingface"
CT2_DIR_SUFFIX = "-ct2-int8"

# Lazy-loaded — initialized on first request to avoid blocking startup.
_translator = None
_tokenizer = None
_languages = None


def _get_ct2_dir() -> str:
    """Download the HF model if needed and convert to CTranslate2 format.

    Cached in the HF cache volume so subsequent starts skip both steps.
    """
    from huggingface_hub import snapshot_download

    hf_dir = snapshot_download(MODEL_NAME, cache_dir=CACHE_DIR)
    ct2_dir = Path(hf_dir).parent / (Path(hf_dir).name + CT2_DIR_SUFFIX)

    if (ct2_dir / "model.bin").exists():
        logger.info("Reusing existing CT2 conversion at %s", ct2_dir)
        return str(ct2_dir)

    logger.info("Converting %s to CTranslate2 int8_float16 format (one-time, ~5-10 min)...", MODEL_NAME)
    from ctranslate2.converters import TransformersConverter

    converter = TransformersConverter(hf_dir, copy_files=["spiece.model", "tokenizer.json", "special_tokens_map.json", "tokenizer_config.json"])
    converter.convert(str(ct2_dir), quantization="int8_float16", force=False)
    logger.info("Conversion complete. CT2 model saved to %s", ct2_dir)
    return str(ct2_dir)


def _load():
    global _translator, _tokenizer, _languages  # noqa: PLW0603
    if _translator is not None:
        return

    import ctranslate2
    import sentencepiece as spm

    ct2_dir = _get_ct2_dir()

    logger.info("Loading CT2 translator on CUDA...")
    _translator = ctranslate2.Translator(ct2_dir, device="cuda", compute_type="int8_float16")

    spm_path = Path(ct2_dir) / "spiece.model"
    _tokenizer = spm.SentencePieceProcessor(model_file=str(spm_path))

    lang_pattern = re.compile(r"^<2([a-z]{2,3}(?:_[A-Za-z]+)?)>$")
    _languages = sorted(
        {
            m.group(1)
            for i in range(_tokenizer.get_piece_size())
            if (m := lang_pattern.match(_tokenizer.id_to_piece(i)))
        }
    )
    logger.info("MADLAD ready. %d target languages available.", len(_languages))


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("MADLAD inference server starting on :8085 (model: %s)", MODEL_NAME)
    yield
    logger.info("MADLAD inference server shutting down")


app = FastAPI(title="MADLAD Translation", lifespan=lifespan)


@app.get("/languages")
def list_languages():
    _load()
    return {"languages": _languages}


@app.post("/translate")
def translate(text: str, target_lang: str):
    _load()
    if target_lang not in _languages:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown target_lang '{target_lang}'. Use /languages to list supported codes.",
        )

    prompt = f"<2{target_lang}> {text}"
    input_tokens = _tokenizer.encode(prompt, out_type=str)

    results = _translator.translate_batch(
        [input_tokens],
        max_decoding_length=1024,
        beam_size=1,
    )
    if not results or not results[0].hypotheses:
        raise HTTPException(status_code=500, detail="No translation produced.")

    output_tokens = results[0].hypotheses[0]
    translated = _tokenizer.decode(output_tokens)
    return {"translated": translated}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8085)
