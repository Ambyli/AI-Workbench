"""MADLAD-400 translation inference server.

Runs a local FastAPI service on port 8085 that loads a pre-converted
CTranslate2 MADLAD-400 checkpoint and exposes /translate and /languages
endpoints for internal consumption by the external API container.
"""

import logging
import os
import re
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MODEL_NAME = os.environ.get("MADLAD_MODEL") or "SoybeanMilk/madlad400-3b-mt-ct2-int8_float16"
CACHE_DIR = "/root/.cache/huggingface"

# Lazy-loaded — initialized on first request to avoid blocking startup.
_translator = None
_tokenizer = None
_languages: list[dict] | None = None
_language_codes: set[str] | None = None


def _describe_lang(code: str) -> str:
    """Human-readable name for a MADLAD language code.

    Handles ISO 639-1, 639-3, and regional variants (e.g. 'zh_CN'). Falls back
    to the raw code for anything langcodes doesn't recognise.
    """
    import langcodes

    try:
        name = langcodes.Language.get(code.replace("_", "-")).display_name()
        return name if name and name != code else code
    except Exception:
        return code


def _load():
    global _translator, _tokenizer, _languages, _language_codes  # noqa: PLW0603
    if _translator is not None:
        return

    import ctranslate2
    import sentencepiece as spm
    from huggingface_hub import snapshot_download

    logger.info("Downloading %s ...", MODEL_NAME)
    model_dir = snapshot_download(MODEL_NAME, cache_dir=CACHE_DIR)

    logger.info("Loading CT2 translator on CUDA from %s", model_dir)
    _translator = ctranslate2.Translator(model_dir, device="cuda", compute_type="int8_float16")

    _tokenizer = spm.SentencePieceProcessor(model_file=f"{model_dir}/spiece.model")

    lang_pattern = re.compile(r"^<2([a-z]{2,3}(?:_[A-Za-z]+)?)>$")
    codes = sorted(
        {
            m.group(1)
            for i in range(_tokenizer.get_piece_size())
            if (m := lang_pattern.match(_tokenizer.id_to_piece(i)))
        }
    )
    _language_codes = set(codes)
    _languages = [{"code": c, "name": _describe_lang(c)} for c in codes]
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


class TranslateRequest(BaseModel):
    text: str
    target_lang: str


@app.post("/translate")
def translate(req: TranslateRequest):
    _load()
    if req.target_lang not in _language_codes:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown target_lang '{req.target_lang}'. Use /languages to list supported codes.",
        )

    prompt = f"<2{req.target_lang}> {req.text}"
    input_tokens = _tokenizer.encode(prompt, out_type=str)

    results = _translator.translate_batch(
        [input_tokens],
        max_decoding_length=1024,
        beam_size=4,
        repetition_penalty=1.1,
        no_repeat_ngram_size=3,
    )
    if not results or not results[0].hypotheses:
        raise HTTPException(status_code=500, detail="No translation produced.")

    output_tokens = results[0].hypotheses[0]
    translated = _tokenizer.decode(output_tokens)
    return {"translated": translated}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8085)
