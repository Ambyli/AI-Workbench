# LiteLLM Docker

Run a LiteLLM proxy with PostgreSQL for model management and Prometheus for metrics.

### Quick start

```bash
docker compose -f ai/docker-compose.litellm.yml up -d
```

This launches three services:

| Container | Port | Purpose |
|---|---|---|
| `litellm` | `localhost:4001` | LiteLLM proxy — OpenAI-compatible API |
| `litellm_db` | `localhost:5432` | PostgreSQL — stores model configs in DB |
| `prometheus` | `localhost:9090` | Metrics scraping and storage |

The proxy is reachable at `http://localhost:4001`. Configuration is loaded from `litellm_config.yaml` (mounted into the container).

### Dependencies

- `HF_TOKEN` from `.env` — HuggingFace token for gated model downloads
- `LITELLM_DATABASE_URL` from `.env` — PostgreSQL connection string
- `litellm_config.yaml` — proxy config with model definitions and routing rules

### Health checks

The LiteLLM service runs a liveliness probe against `/health/liveliness`. Prometheus scrapes metrics from the proxy on its default endpoint.

### Stopping

```bash
docker compose -f ai/docker-compose.litellm.yml down
```

Data in PostgreSQL is persisted in the `litellm_postgres_data` named volume and survives container restarts.
