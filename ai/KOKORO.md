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

- `HF_TOKEN` from `.env` — HuggingFace token for downloading the Kokoro model weights on first run

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
cd ai/kokoro/app && uv sync
cd ai/kokoro/api && uv sync
```

### Adding voices

Voices come from the `misaki` package bundled with Kokoro. Run `/voices` to see all available names, then pass the name as the `voice` query parameter to `/generate`.
