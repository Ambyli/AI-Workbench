# ai/kokoro

Kokoro-82M text-to-speech: `kokoro-app` (the model, GPU) behind `kokoro-api` (a stateless proxy with an OpenAI-compatible `POST /v1/audio/speech` and the `text_to_speech` MCP tool). Guide: [`KOKORO.md`](KOKORO.md). Compose: `docker-compose.kokoro.yml`. API: `kokoro.postman_collection.json`.

## Rules

- **`kokoro-api` is swap-class, `kokoro-app` recreate-class (GPU).** `kokoro-api` has no `container_name` / `ports:`; `PORT_KOKORO_API` (8004) is published by `ai/edge`. Use `make up kokoro`, and `make exec|logs kokoro kokoro-api` for the API copy.
- **WAV only** — `response_format` is ignored; Open WebUI transcodes, any other OpenAI-TTS client must too.
- **First chunk only** — `app.py`'s `/generate` takes one item from the `KPipeline` generator, so long text is cut; Open WebUI's `AUDIO_TTS_SPLIT_ON=punctuation` sidesteps it. The real fix is concatenating every chunk.
- **Open WebUI calls `kokoro-api` directly over `ai_shared`, not via LiteLLM** (its virtual key is scoped to chat models; `kokoro` stays off the chat picker). Its `AUDIO_TTS_*` settings are first-boot-only PersistentConfig (`OPENWEBUI.md § Voice (TTS via Kokoro)`).
- The model lazy-loads on the first request (slow), cached in the `kokoro_data` volume.

## Cross-folder rules

Also in the root `CLAUDE.md` § Cross-folder rules — read the named entry there for the full picture.

- **Swap-class services publish through the edge** — `kokoro-api`'s port lives in `ai/edge` (`listen kokoro-api` + edge `ports:`), never in this compose file. (root `CLAUDE.md` § Cross-folder rules; the rules themselves are in § Deploys (`make up`))

## Env vars

One line each; full detail in `.env.example` and `KOKORO.md § Dependencies`.

- `KOKORO_APP_URL` (`http://kokoro-app:8085`) — where the proxy reaches the model container.
- `AUDIO_BASE_URL` (`http://localhost:8004`) — base of the playback link the `text_to_speech` MCP tool returns; its port must match `PORT_KOKORO_API`.
- `PORT_KOKORO_API` (`8004`) — host port, published by the edge.
