# Kokoro TTS

Run a two-container text-to-speech stack built on [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M). One container runs the model; a second acts as a stateless API proxy so clients never block the inference process directly.

### Quick start

```bash
docker compose -f ai/docker-compose.kokoro.yml up -d
```

This launches two containers:

| Container | Port | Purpose |
|---|---|---|
| `kokoro-app` | internal only | Loads the Kokoro model and exposes `/generate` and `/voices` |
| `kokoro-api` | `localhost:8004` | Stateless proxy — the only public-facing endpoint |

All client traffic goes through `kokoro-api` on port 8004. `kokoro-app` is not exposed to the host.

### Usage

**List available voices:**

```bash
curl http://localhost:8004/voices
```

**Generate speech:**

```bash
curl -X POST "http://localhost:8004/generate?text=Hello+world&voice=af_heart" \
  --output audio.wav
```

The response is a WAV file. The default voice is `af_heart` if `voice` is omitted.

### Dependencies

| Variable | Default | Purpose |
|---|---|---|
| `HF_TOKEN` | — | HuggingFace token for downloading model weights on first run |
| `KOKORO_APP_URL` | `http://kokoro-app:8085` | URL the API proxy uses to reach the inference container. Resolves via Docker service name in Compose. For local dev, override inline: `KOKORO_APP_URL=http://localhost:8085 uv run kokoro_server.py` |
| `AUDIO_BASE_URL` | `http://localhost:8000` | Base URL returned by the `text_to_speech` MCP tool. Used to build the audio playback link. Override if the server runs on a different host or port. |

### Model loading

The model is **lazy-loaded** on the first `/generate` request, not at startup. The first request will be slow (model download + load); subsequent requests are fast. Model weights are cached in the `kokoro_data` named volume so they survive container restarts.

### Health check

`kokoro-app` exposes a `/voices` endpoint that the Docker health check polls every 30 seconds with a 120-second start-up grace period (to allow model download on first launch). `kokoro-api` only becomes reachable once `kokoro-app` is healthy.

### Stopping

```bash
docker compose -f ai/docker-compose.kokoro.yml down
```

Model weights in the `kokoro_data` volume are preserved. To also delete the cache:

```bash
docker compose -f ai/docker-compose.kokoro.yml down -v
```

### Project structure

```
ai/
  docker-compose.kokoro.yml
  Dockerfile.kokoro-app      ← builds the inference container
  Dockerfile.kokoro-api      ← builds the proxy container
  kokoro/
    app/
      pyproject.toml         ← app deps (kokoro, soundfile, fastapi, uvicorn, misaki[en])
      app.py                 ← FastAPI inference server on :8080
    api/
      pyproject.toml         ← api deps (fastapi, uvicorn, httpx)
      kokoro_server.py       ← FastAPI proxy server on :8000
```

Each subfolder is an independent `uv` project. For local development:

```bash
# Install deps
cd ai/kokoro/app && uv sync
cd ai/kokoro/api && uv sync

# Run the inference server (terminal 1)
cd ai/kokoro/app && uv run python app.py

# Run the API proxy pointing at localhost (terminal 2)
cd ai/kokoro/api && KOKORO_APP_URL=http://localhost:8085 uv run kokoro_server.py
```

### LiteLLM integration

`kokoro-api` exposes a `POST /v1/audio/speech` endpoint that matches the OpenAI TTS API. LiteLLM routes requests with `"model": "kokoro"` to it via the `litellm_config.yaml` entry.

**Example via LiteLLM proxy:**

```bash
curl http://localhost:4001/v1/audio/speech \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "kokoro", "input": "Hello world", "voice": "alloy"}' \
  --output audio.wav
```

**OpenAI → Kokoro voice mapping:**

| OpenAI voice | Kokoro voice |
|---|---|
| `alloy` | `af_heart` |
| `echo` | `am_adam` |
| `fable` | `bf_emma` |
| `onyx` | `am_michael` |
| `nova` | `af_sarah` |
| `shimmer` | `af_bella` |

Kokoro voice names (e.g. `af_heart`) can also be passed directly and will be used as-is.

> **Note:** Kokoro only produces WAV output. The `response_format` field is accepted but ignored — the response is always `audio/wav`.

### Adding voices

Voices come from the Kokoro model on HuggingFace. Run `/voices` to see all available names, then pass the name as the `voice` query parameter to `/generate` or as the `voice` field in `/v1/audio/speech`.

### Language support

Kokoro supports nine languages. Each voice is trained for exactly one language — the first character of the voice name is the language code — so the language is derived from the voice you pick. There is no separate `language` parameter on any endpoint. Each language uses its own `KPipeline`; the app caches one per language and instantiates them lazily on first use.

| Code | Language | Voice prefix |
|---|---|---|
| `a` | American English | `af_*`, `am_*` |
| `b` | British English | `bf_*`, `bm_*` |
| `e` | Spanish | `ef_*`, `em_*` |
| `f` | French | `ff_*`, `fm_*` |
| `h` | Hindi | `hf_*`, `hm_*` |
| `i` | Italian | `if_*`, `im_*` |
| `j` | Japanese | `jf_*`, `jm_*` |
| `p` | Brazilian Portuguese | `pf_*`, `pm_*` |
| `z` | Mandarin | `zf_*`, `zm_*` |

**Discover — voices grouped by language:**

```bash
curl http://localhost:8004/languages
```

Returns something like:

```json
{
  "languages": [
    {"code": "a", "name": "American English", "voices": ["af_heart", "am_adam", ...]},
    {"code": "j", "name": "Japanese",         "voices": ["jf_alpha", "jm_kumo", ...]},
    ...
  ]
}
```

**Generate:**

```bash
curl -X POST "http://localhost:8004/generate?text=Bonjour&voice=ff_siwis" --output fr.wav
curl -X POST "http://localhost:8004/generate?text=%E3%83%8F%E3%83%AD%E3%83%BC&voice=jf_alpha" --output ja.wav
```

Note that OpenAI voice aliases (`alloy`, `echo`, `fable`, `onyx`, `nova`, `shimmer`) always map to English voices. To speak another language via `/v1/audio/speech`, pass a Kokoro voice name in the `voice` field.

Japanese and Mandarin use extra G2P dependencies (`misaki[ja]`, `misaki[zh]`), which are installed as part of `kokoro-app`.
