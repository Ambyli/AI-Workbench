# MADLAD-400 Translation

Run a two-container translation stack built on [MADLAD-400](https://huggingface.co/jbochi/madlad400-3b-mt) (Google, Apache 2.0 — commercial use permitted). One container runs the model via [CTranslate2](https://github.com/OpenNMT/CTranslate2); a second acts as a stateless API proxy so clients never block the inference process directly.

### Quick start

```bash
docker compose -f ai/docker-compose.madlad.yml up -d
```

This launches two containers:

| Container | Port | Purpose |
|---|---|---|
| `madlad-app` | internal only | Loads MADLAD via CTranslate2 and exposes `/translate` and `/languages` |
| `madlad-api` | `localhost:8008` | Stateless proxy — the only public-facing endpoint |

All client traffic goes through `madlad-api` on port 8008. `madlad-app` is not exposed to the host.

### Usage

**List available target languages:**

```bash
curl http://localhost:8008/languages
```

Returns 400+ ISO 639-1 language codes.

**Translate text:**

```bash
curl -X POST "http://localhost:8008/translate?text=Hello+world&target_lang=es"
# {"translated": "Hola mundo"}
```

Source language is auto-detected — no need to specify it.

### Dependencies

| Variable | Default | Purpose |
|---|---|---|
| `HF_TOKEN` | — | HuggingFace token for downloading model weights on first run |
| `MADLAD_APP_URL` | `http://madlad-app:8085` | URL the API proxy uses to reach the inference container. Resolves via Docker service name in Compose. For local dev, override inline: `MADLAD_APP_URL=http://localhost:8085 uv run madlad_server.py` |
| `MADLAD_MODEL` | `jbochi/madlad400-3b-mt` | HuggingFace repo ID for the MADLAD checkpoint. Try `jbochi/madlad400-7b-mt` or `jbochi/madlad400-10b-mt` for higher quality (larger VRAM footprint). |

### Model loading

The model is **lazy-loaded** on the first `/translate` (or `/languages`) request, not at startup.

**First request is slow (~5-10 minutes):**
1. Downloads the raw PyTorch checkpoint from HuggingFace (~5 GB for 3B).
2. Converts to CTranslate2 `int8_float16` format.
3. Loads the converted model onto the GPU.

Subsequent starts skip both download and conversion — the CT2 output is cached in the `madlad_data` named volume alongside the HF cache. Restarts of `madlad-app` are fast after the first run.

Because of the long first-run window, the Dockerfile healthcheck has a **10-minute** start-up grace period.

### Health check

`madlad-app` exposes a `/languages` endpoint that the Docker healthcheck polls every 30 seconds with a 600-second start-up grace period. `madlad-api` exposes a `/health` endpoint that its own healthcheck polls once ready. `madlad-api` only becomes reachable once `madlad-app` responds.

### Stopping

```bash
docker compose -f ai/docker-compose.madlad.yml down
```

Model weights and CT2 conversion output in the `madlad_data` volume are preserved. To also delete the cache (forcing full re-download and re-conversion on next start):

```bash
docker compose -f ai/docker-compose.madlad.yml down -v
```

### Project structure

```
ai/
  docker-compose.madlad.yml
  Dockerfile.madlad-app      ← builds the inference container
  Dockerfile.madlad-api      ← builds the proxy container
  madlad/
    app/
      pyproject.toml         ← app deps (ctranslate2, sentencepiece, transformers, torch, fastapi, uvicorn)
      app.py                 ← FastAPI inference server on :8085
    api/
      pyproject.toml         ← api deps (fastapi, uvicorn, httpx, fastmcp)
      madlad_server.py       ← FastAPI proxy server on :8000
```

Each subfolder is an independent `uv` project. For local development:

```bash
# Install deps
cd ai/madlad/app && uv sync
cd ai/madlad/api && uv sync

# Run the inference server (terminal 1)
cd ai/madlad/app && uv run python app.py

# Run the API proxy pointing at localhost (terminal 2)
cd ai/madlad/api && MADLAD_APP_URL=http://localhost:8085 uv run madlad_server.py
```

### LiteLLM integration

`madlad-api` is exposed through LiteLLM in two ways.

**1. Pass-through HTTP endpoint** (simplest — direct translation without a chat model in the loop):

Anything under `/v1/madlad/*` on LiteLLM is forwarded to `madlad-api` on the internal `ai_shared` network. Headers (including `Authorization`) are forwarded unchanged.

```bash
# Translate
curl -X POST "http://localhost:4001/v1/madlad/translate?text=Good+morning&target_lang=ja" \
  -H "Authorization: Bearer $DEFAULT_LITELLM_MASTER_KEY"
# {"translated": "おはよう"}

# List supported languages
curl "http://localhost:4001/v1/madlad/languages" \
  -H "Authorization: Bearer $DEFAULT_LITELLM_MASTER_KEY"
```

The `/v1/madlad` prefix is stripped when forwarding — `/v1/madlad/translate` on LiteLLM hits `/translate` on madlad-api.

**2. MCP tool** (for when you want a chat model to decide whether to translate as part of a larger task):

`madlad-api` also exposes an MCP tool at `/mcp` that any LiteLLM-routed model can call. Registered in `litellm_config.yaml` as the `madlad_translate` MCP server.

**Tool signature:**

```python
translate(text: str, target_lang: str) -> str
```

- `text` — source text (any language, auto-detected).
- `target_lang` — ISO 639-1 code (e.g. `"es"`, `"fr"`, `"ja"`, `"zh"`).

Unlike a chat completions model, MADLAD is **tool-only** — it does not appear in `model_list` and cannot be called via `/v1/chat/completions` directly. Use it by asking a chat model to invoke the `translate` tool.

**Example flow via LiteLLM proxy** (see CLAUDE.md's LiteLLM section for the two-step tool call loop):

```bash
curl http://localhost:4001/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3.6",
    "messages": [{"role": "user", "content": "Translate \"Good morning\" into Japanese using the translate tool."}]
  }'
```

The response will include a `tool_calls` entry naming `translate` with arguments `{"text": "Good morning", "target_lang": "ja"}`. Call the MCP tool directly and feed the result back in a second request.

### Supported languages

MADLAD-400 supports 400+ languages, drawn from the CommonCrawl-based MADLAD-400 corpus. To see the full list from your running container:

```bash
curl http://localhost:8008/languages | jq '.languages | length'   # count
curl http://localhost:8008/languages | jq '.languages'            # full list
```

Language codes follow ISO 639-1 where available (e.g. `en`, `es`, `fr`, `de`, `ja`, `zh`, `ko`, `ar`, `hi`, `pt`). Some low-resource languages use ISO 639-3 (three-letter) codes.

### License note

MADLAD-400 checkpoints on HuggingFace are released by Google under the **Apache 2.0** license — commercial use is permitted with no restrictions on output. This is the reason MADLAD was chosen over Aya Expanse (CC-BY-NC, non-commercial) and NLLB (CC-BY-NC).
