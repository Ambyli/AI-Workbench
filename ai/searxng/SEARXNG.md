# SearXNG

Self-hosted metasearch engine that fronts Google/Bing/DuckDuckGo/etc. so Open WebUI (and any other tool on `ai_shared`) can pull live web results into model responses without paying for an API. It replaces the "no search backend configured" state Open WebUI ships with.

### Quick start

```bash
docker compose -f ai/docker-compose.searxng.yml --env-file .env up -d
```

Or via make:

```bash
make up searxng
```

Then browse `http://localhost:8009` — you should see the SearXNG search page. Try a query in the browser to confirm outbound search is working before wiring up Open WebUI.

| Container | Port | Purpose |
|---|---|---|
| `searxng` | `localhost:8009` | Metasearch UI + JSON API — Open WebUI hits it as `http://searxng:8080` over `ai_shared` |

### Config file

`ai/searxng/settings.yml` is bind-mounted into the container at `/etc/searxng/settings.yml`. It sets:

- `use_default_settings: true` — inherits upstream defaults, only overrides what we need.
- `server.limiter: false` — the built-in rate limiter drops server-to-server calls with the wrong headers; disable so Open WebUI can reach `/search` unmolested.
- `search.formats: [html, json]` — JSON output is off by default upstream; Open WebUI needs it.
- `server.secret_key: "${SEARXNG_SECRET}"` — literal placeholder. The `entrypoint` wrapper in `ai/docker-compose.searxng.yml` reads `$SEARXNG_SECRET` from the container env at startup, sed-substitutes it into a copy of `settings.yml` at `/tmp/searxng-settings.yml`, and points `SEARXNG_SETTINGS_PATH` at that copy — so the bind-mounted host file stays free of the real secret and can be committed to git. If `SEARXNG_SECRET` is unset, the wrapper refuses to boot rather than starting with an unresolved placeholder.

Edits to `settings.yml` take effect on `make down searxng && make up searxng` — no rebuild needed because it's a bind-mount.

### `SEARXNG_SECRET`

Signs internal SearXNG session state. Generate one:

```bash
openssl rand -hex 32
```

Paste into `.env`:

```
SEARXNG_SECRET=<hex>
```

Blank is fine for a first boot but should be rotated before real use.

### Wiring into Open WebUI

Open WebUI already gets these env vars from `.env` (declared in `ai/docker-compose.openwebui.yml`):

| Open WebUI env var | `.env` key | Default | Notes |
|---|---|---|---|
| `ENABLE_WEB_SEARCH` | `OPENWEBUI_ENABLE_WEB_SEARCH` | `true` | Master switch |
| `WEB_SEARCH_ENGINE` | `OPENWEBUI_WEB_SEARCH_ENGINE` | `searxng` | Which backend to use |
| `SEARXNG_QUERY_URL` | `OPENWEBUI_SEARXNG_QUERY_URL` | `http://searxng:8080/search?q=<query>&format=json` | Must use the Docker service name, not `localhost` |
| `WEB_SEARCH_RESULT_COUNT` | `OPENWEBUI_WEB_SEARCH_RESULT_COUNT` | `3` | Results per query fed to the model |
| `WEB_SEARCH_CONCURRENT_REQUESTS` | `OPENWEBUI_WEB_SEARCH_CONCURRENT_REQUESTS` | `10` | Parallel fetch cap when Open WebUI enriches results |

> **Important — first-boot-only env vars.** Open WebUI reads these on the *very first* boot of the `openwebui_data` volume. On an existing install, changing them here does **nothing** — set them via **Admin Panel → Settings → Web Search** in the running container instead, then click Save. Wiping the volume (`docker compose -f ai/docker-compose.openwebui.yml down -v`) re-arms the env-var path but destroys all users, chats, and uploads.

### Using it in a chat

1. Open a chat in Open WebUI.
2. Toggle **Web search** on in the chat composer (icon/label may shift with UI releases — in v0.11.0 it lives in the tools tray).
3. Ask a time-sensitive question ("what's the AUD/USD rate today?"). The response should cite source URLs pulled via SearXNG.

If the model responds without citations, the toggle isn't wired — recheck the Admin-Panel Web Search settings.

### Health check

Polls `http://localhost:8080/healthz` inside the container every 30 s with a 30 s grace period. That endpoint returns `OK` when the internal Flask app is up.

### Stopping

```bash
make down searxng
```

There is no persistent volume beyond the mounted config directory — nothing on-disk state to worry about. `docker compose … down -v` is a no-op here.

### Verifying end-to-end

From the host (proves port publish + JSON output):

```bash
curl -s "http://localhost:8009/search?q=hello&format=json" | python -c "import sys, json; print(len(json.load(sys.stdin).get('results', [])))"
```

Expect a small integer (typically 5–10). If it prints `0`, upstream engines all failed — check `docker logs searxng`. If it returns HTML, the `search.formats` override isn't loading — inspect the bind mount.

From inside `ai_shared` (proves the exact URL Open WebUI uses):

```bash
docker run --rm --network ai_shared curlimages/curl \
  -s "http://searxng:8080/search?q=hello&format=json" | head -c 200
```

### Updating the image

The image tag is pinned in `ai/docker-compose.searxng.yml`:

```yaml
image: docker.io/searxng/searxng:latest
```

To bump to a specific release, pick a tag from <https://hub.docker.com/r/searxng/searxng/tags>, edit that line, then:

```bash
make build searxng
make down searxng && make up searxng
```

### Notes

- SearXNG doesn't need a GPU or any secrets besides `SEARXNG_SECRET`. It's stateless — every query fans out to upstream engines fresh.
- It's not on the LiteLLM fan-out. Models never call SearXNG directly; Open WebUI enriches the prompt with search results server-side before dispatching to LiteLLM.
- If public search engines start rate-limiting your egress IP, individual engines can be disabled in `settings.yml` under `engines:` — see the [upstream engine list](https://docs.searxng.org/user/configured_engines.html).
- Wider infrastructure map: [`AI_INFRA.md`](AI_INFRA.md).
