# Open WebUI

Browser-based chat interface for the LiteLLM proxy. Open WebUI sees LiteLLM as a single OpenAI-compatible provider, so every model defined in `litellm_config.yaml` automatically appears in the model picker.

### Quick start

```bash
docker compose -f ai/docker-compose.openwebui.yml --env-file .env up -d
```

Or via make:

```bash
make up-openwebui
```

Then open `http://localhost:8007`. The first account created becomes the admin. Sign-up is disabled afterwards (see `ENABLE_SIGNUP` below to change this).

| Container | Port | Purpose |
|---|---|---|
| `openwebui` | `localhost:8007` | Chat UI — talks to LiteLLM over the `ai_shared` Docker network |

### How it connects to LiteLLM

Both containers are attached to the `ai_shared` network, so Open WebUI reaches the proxy via the Docker service name — `http://litellm:4000/v1` — not via the host port. LiteLLM does **not** need to be exposed on the host for this to work; it is exposed at `localhost:4001` only for direct API use.

The connection settings are passed in once at first launch:

| Env var | Value | Source |
|---|---|---|
| `OPENAI_API_BASE_URL` | `http://litellm:4000/v1` | hardcoded in `docker-compose.openwebui.yml` |
| `OPENAI_API_KEY` | LiteLLM master key | `${DEFAULT_LITELLM_MASTER_KEY}` from `.env` |
| `ENABLE_OLLAMA_API` | `false` | disables the Ollama discovery probe |
| `WEBUI_SECRET_KEY` | random hex string | `${OPENWEBUI_SECRET_KEY}` from `.env` |
| `ENABLE_SIGNUP` | `false` | only the first sign-up is allowed |
| `DEFAULT_USER_ROLE` | `admin` | first user gets the admin role |

> **Important:** Open WebUI only reads these env vars on the **first launch**. Once the SQLite store under `/app/backend/data` is initialized, further changes must be made through **Admin Settings → Connections** in the UI, or by deleting the `openwebui_data` volume and starting fresh.

### `WEBUI_SECRET_KEY`

Sessions are signed with this key. If it changes between container restarts, every user is logged out. Generate one with:

```bash
openssl rand -hex 32
```

Paste the output into `OPENWEBUI_SECRET_KEY` in `.env`. The default placeholder (`change-me-run-openssl-rand-hex-32`) is fine for a first boot but should be rotated before any real use.

### Health check

Polls `http://localhost:8080/health` inside the container every 30s with a 30s startup grace period.

### Stopping

```bash
docker compose -f ai/docker-compose.openwebui.yml down
# or
make down-openwebui
```

User data, chat history, and uploaded files are stored in the named volume `openwebui_data` and survive restarts. To wipe everything (and force re-reading the env vars on next launch):

```bash
docker compose -f ai/docker-compose.openwebui.yml down -v
```

### Adding more LiteLLM models

Models are configured in `litellm_config.yaml`, not in Open WebUI. After editing that file and restarting LiteLLM, the new model appears in Open WebUI's model picker automatically — Open WebUI calls `GET /v1/models` against LiteLLM to populate the list.

### Notes

- The image (`ghcr.io/open-webui/open-webui:main`) tracks the rolling `main` tag. Pin to a specific version (e.g. `:v0.4.8`) before relying on this in production.
- The container does **not** request GPU access — it does no inference of its own, only proxies requests to LiteLLM.
- If LiteLLM is not running, Open WebUI loads but the model list is empty. Start LiteLLM first (`make up-litellm`).
