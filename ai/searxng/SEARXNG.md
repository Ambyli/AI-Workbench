# SearXNG

Self-hosted metasearch engine that fronts Google/Bing/DuckDuckGo/etc. so Open WebUI (and any other tool on `ai_shared`) can pull live web results into model responses without paying for an API. It replaces the "no search backend configured" state Open WebUI ships with.

### Quick start

```bash
docker compose -f ai/searxng/docker-compose.searxng.yml --env-file .env up -d
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
- `outgoing.request_timeout` / `max_request_timeout` — explicit bound on every per-engine outgoing request, instead of relying on whatever the base image's own baked-in defaults happen to be.
- `engines: [{name: duckduckgo, disabled: true}]` — DDG CAPTCHAs datacenter egress IPs almost unconditionally; see § Troubleshooting below.
- `server.secret_key: "${SEARXNG_SECRET}"` — literal placeholder. The `entrypoint` wrapper in `ai/searxng/docker-compose.searxng.yml` reads `$SEARXNG_SECRET` from the container env at startup, sed-substitutes it into a copy of `settings.yml` at `/tmp/searxng-settings.yml`, and points `SEARXNG_SETTINGS_PATH` at that copy — so the bind-mounted host file stays free of the real secret and can be committed to git. If `SEARXNG_SECRET` is unset, the wrapper refuses to boot rather than starting with an unresolved placeholder.

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

Open WebUI already gets these env vars from `.env` (declared in `ai/openwebui/docker-compose.openwebui.yml`):

| Open WebUI env var | `.env` key | Default | Notes |
|---|---|---|---|
| `ENABLE_WEB_SEARCH` | `OPENWEBUI_ENABLE_WEB_SEARCH` | `true` | Master switch |
| `WEB_SEARCH_ENGINE` | `OPENWEBUI_WEB_SEARCH_ENGINE` | `searxng` | Which backend to use |
| `SEARXNG_QUERY_URL` | `OPENWEBUI_SEARXNG_QUERY_URL` | `http://searxng:8080/search?q=<query>&format=json` | Must use the Docker service name, not `localhost` |
| `WEB_SEARCH_RESULT_COUNT` | `OPENWEBUI_WEB_SEARCH_RESULT_COUNT` | `3` | Results per query fed to the model |
| `WEB_SEARCH_CONCURRENT_REQUESTS` | `OPENWEBUI_WEB_SEARCH_CONCURRENT_REQUESTS` | `10` | Parallel fetch cap when Open WebUI enriches results |
| `AIOHTTP_CLIENT_TIMEOUT` | `OPENWEBUI_AIOHTTP_CLIENT_TIMEOUT` | `300` | Ceiling (seconds) on Open WebUI's outbound HTTP client — covers `search_web`'s call to searxng and `fetch_url`'s direct page fetch. Read on every boot, not first-boot-only. Matches the upstream default — the idle-stream cap below is what actually protects against a hang. |
| `AIOHTTP_CLIENT_STREAM_IDLE_TIMEOUT` | `OPENWEBUI_AIOHTTP_CLIENT_STREAM_IDLE_TIMEOUT` | `60` | Max seconds allowed between chunks on a streaming fetch before it's aborted. Upstream default is unset (no idle cap) — this is the real fix for a connection that goes completely silent mid-response. |

> **Important — first-boot-only env vars.** Open WebUI reads these on the *very first* boot of the `openwebui_data` volume. On an existing install, changing them here does **nothing** — set them via **Admin Panel → Settings → Web Search** in the running container instead, then click Save. Wiping the volume (`docker compose -f ai/openwebui/docker-compose.openwebui.yml down -v`) re-arms the env-var path but destroys all users, chats, and uploads.

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

The image tag is pinned in `ai/searxng/docker-compose.searxng.yml`:

```yaml
image: docker.io/searxng/searxng:latest
```

To bump to a specific release, pick a tag from <https://hub.docker.com/r/searxng/searxng/tags>, edit that line, then:

```bash
make build searxng
make down searxng && make up searxng
```

### Troubleshooting: fetch_url / search hangs

Symptom: `searxng` logs a per-engine `WARNING` (e.g. `SearxEngineCaptchaException`, `CAPTCHA (wt-wt) ...` from the `duckduckgo` engine) and, around the same time, a chat's `search_web` or `fetch_url` native tool call sits "pending" in Open WebUI indefinitely with no error ever shown to the model.

These are two separate failures that happen to correlate:

1. **The searxng-side CAPTCHA warning is not what causes the hang.** `outgoing.request_timeout` / `max_request_timeout` (in `settings.yml`) bound every per-engine request, so a CAPTCHA'd or unresponsive engine gets excluded from the response (see `unresponsive_engines` in the JSON body) and `/search` still returns `200` promptly with whatever other engines succeeded. `duckduckgo` is disabled outright in `settings.yml` — datacenter/server egress IPs get CAPTCHA'd by it almost unconditionally, so it was pure log noise and wasted fan-out latency with no chance of ever succeeding here.
2. **The actual hang is on Open WebUI's side.** `search_web` calls searxng, but `fetch_url` fetches the *target page itself* directly (via the configured Web Loader) — never through searxng. Open WebUI's outbound aiohttp client defaults to a 300s timeout with **no idle-stream cap at all**, so `fetch_url` against a site that accepts the connection but then goes completely silent (or serves an anti-bot interstitial that never resolves) can block the tool call indefinitely with nothing surfaced back to the model — indistinguishable from "forever" in a chat. `OPENWEBUI_AIOHTTP_CLIENT_TIMEOUT` / `OPENWEBUI_AIOHTTP_CLIENT_STREAM_IDLE_TIMEOUT` (wired in `ai/openwebui/docker-compose.openwebui.yml`, unlike the web-search block these are read on every boot) are set to `300`/`60`: the overall ceiling is left at upstream's own default so slow-but-progressing pages still get their full 5 minutes, while the idle-stream cap is what actually closes the hang — a connection that goes silent for 60s straight gets aborted and the model sees an error instead of waiting forever.

### Notes

- SearXNG doesn't need a GPU or any secrets besides `SEARXNG_SECRET`. It's stateless — every query fans out to upstream engines fresh.
- It's not on the LiteLLM fan-out. Models never call SearXNG directly; Open WebUI enriches the prompt with search results server-side before dispatching to LiteLLM.
- If public search engines start rate-limiting your egress IP, individual engines can be disabled in `settings.yml` under `engines:` — see the [upstream engine list](https://docs.searxng.org/user/configured_engines.html). `duckduckgo` already is, per the troubleshooting note above.
- Wider infrastructure map: [`AI_INFRA.md`](AI_INFRA.md).
