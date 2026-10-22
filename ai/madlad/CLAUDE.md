# ai/madlad

MADLAD-400 translation: `madlad-app` (CTranslate2 model, GPU) behind `madlad-api` (stateless proxy: `/translate`, `/languages`, the `translate` MCP tool). Guide: [`MADLAD.md`](MADLAD.md). Compose: `docker-compose.madlad.yml`. API: `madlad.postman_collection.json`.

## Rules

- **`madlad-api` is swap-class, `madlad-app` recreate-class (GPU).** `madlad-api` has no `container_name` / `ports:`; `PORT_MADLAD_API` (8008) is published by `ai/edge`. Use `make up madlad`, and `make exec|logs madlad madlad-api` for the API copy.
- **Tool-only.** MADLAD is not in LiteLLM's `model_list` and cannot be called via `/v1/chat/completions`; a chat model invokes the `translate` MCP tool (`madlad_translate` in `litellm_config.yaml`), or callers use the `/v1/madlad` pass-through.
- **`MADLAD_MODEL` must be a pre-converted CTranslate2 checkpoint** — there is no conversion step and no torch / transformers in the image.
- The model lazy-loads on the first request (~1–3 min, cached in `madlad_data`); the healthcheck allows 10 minutes for that first download.
- **License is the reason for this model** (Apache-2.0, commercial use). Don't swap in Aya Expanse or NLLB (CC-BY-NC).

## Cross-folder rules

Also in the root `CLAUDE.md` § Cross-folder rules — read the named entry there for the full picture.

- **Swap-class services publish through the edge** — `madlad-api`'s port lives in `ai/edge` (`listen madlad-api` + edge `ports:`), never in this compose file. (root `CLAUDE.md` § Cross-folder rules; the rules themselves are in § Deploys (`make up`))
- **LiteLLM pass-throughs carry the auth** — `madlad-api` checks no token; LiteLLM's `/v1/madlad` pass-through has `auth: true`, and any new route to it needs the same (`LITELLM.md § Pass-through auth`). (root `CLAUDE.md` § Cross-folder rules)

## Env vars

One line each; full detail in `.env.example` and `MADLAD.md § Dependencies`.

- `MADLAD_APP_URL` (`http://madlad-app:8085`) — where the proxy reaches the model container.
- `MADLAD_MODEL` (`SoybeanMilk/madlad400-3b-mt-ct2-int8_float16`) — HF repo of a pre-converted CT2 checkpoint; alternatives: the 10b (more VRAM) or `Nextcloud-AI/madlad400-3b-mt-ct2-int8` (CPU).
- `PORT_MADLAD_API` (`8008`) — host port, published by the edge.
