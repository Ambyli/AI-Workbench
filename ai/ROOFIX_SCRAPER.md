# Roofix Scraper

A Playwright-based HTTP service that scrapes proposal data from Roofix (a Bubble.io app with no public API). The [Roofix Bridge](ROOFIX_BRIDGE.md) uses it to hydrate `Estimate` / `Estimate Complete` events that arrive too thin in email to act on directly. Splitting this off from the bridge keeps a ~500 MB Chromium image out of the bridge container and makes the scraper reusable.

Roofix data arrives in the browser via `roofix.io/elasticsearch/mget` responses (multiple responses, project doc in one, customer/contact in another). The scraper drives a headless Chromium, captures those responses, and merges the `docs` arrays into a single JSON blob.

### Quick start

```bash
docker compose -f ai/docker-compose.roofix-scraper.yml up -d --build
```

### Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /health` | Container healthcheck. |
| `GET /session` | Status of the persisted Roofix session file (present? size?). |
| `POST /session/refresh` | Accept a Playwright `storage_state` JSON body and persist it to the volume. |
| `GET /proposal/{project_id}?tracking_url=...` | Scrape a proposal by Bubble project id. Optional `tracking_url` (from the notification email) is used instead of building `roofix.io/project/{id}` — useful when only the tokenized email link is available. |

The service is internal-only — reach it from other containers on `ai_shared`:

```bash
docker exec -it roofix-bridge curl http://roofix-scraper:8080/health
docker exec -it roofix-bridge curl "http://roofix-scraper:8080/proposal/1782246308331x9098"
```

### Session cookies

Roofix logins can't happen inside a headless container — no browser UI to complete the flow. The refresh path is a two-step operator action:

1. **On your laptop**, run the [`save_roofix_session.py`](../../roofix-phoenix-bridge/save_roofix_session.py) script from the source repo. A visible Chromium opens, you log into Roofix (including 2FA), then press Enter. It writes `roofix_session.json`.
2. **POST it to the scraper**:

   ```bash
   curl -X POST http://<host>:<published-port>/session/refresh \
     -H "Content-Type: application/json" \
     -d @roofix_session.json
   ```

   The file is persisted to the `roofix_scraper_data` volume so subsequent restarts reuse it.

Alternatively you can bind-mount `roofix_session.json` directly at `/data/roofix_session.json` on container start.

Tracking URLs from Roofix notification emails redirect to the proposal without login, so many `/proposal/...` calls succeed without a session — but a session is preferred and required for the direct `roofix.io/project/{id}` path.

### Configuration

| Variable | Default | Purpose |
|---|---|---|
| `ROOFIX_SESSION_PATH` | `/data/roofix_session.json` | Where the Playwright storage_state lives. |

### Response shape

```json
{
  "url": "https://roofix.io/project/1782246308331x9098",
  "docs": [{"_type": "project", "_source": {...}}, {"_type": "customer", "_source": {...}}, ...],
  "doc_types": {"project": 1, "customer": 1, "contact": 2, ...},
  "response_count": 4
}
```

`docs` is the flat, merged list of every `docs[]` entry from every captured `elasticsearch/mget` response. `doc_types` is a Counter breakdown for quick sanity-checking.

### Project structure

```
ai/
  Dockerfile.roofix-scraper
  docker-compose.roofix-scraper.yml
  roofix-scraper/
    pyproject.toml
    app.py                FastAPI endpoints
    scraper.py            Playwright fetch + response merging
    session.py            storage_state load / save
```

### Base image

`mcr.microsoft.com/playwright/python:v1.47.0-jammy` — comes with Chromium and every native library Playwright needs already installed. Container image lands around 1.5–2 GB but the alternative (installing Chromium + dependencies on `python:3.11-slim`) is bigger and more fragile.

### Known limitations

- **Session refresh is manual.** The container cannot present a login UI. Refresh is an operator flow.
- **Field mapping to Phoenix lives in the bridge**, not here. This service returns raw Roofix data; the bridge translates.
- **No rate limiting.** Roofix has no published limits, but treat this as a per-project scrape, not a bulk crawler.
