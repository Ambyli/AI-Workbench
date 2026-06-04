# Docker Infrastructure

All services are orchestrated through a single `docker-compose.yml` that includes three sub-composes. Each can also be run independently.

## Shared Docker network

All containers are attached to a shared `ai_shared` network so they can resolve each other by container name (e.g. `kokoro-api:8000`, `vllm-llama:8000`). Create it once:

```bash
docker network create ai_shared
```

Or via make:

```bash
make network
```

The `make setup` target creates the network automatically.

## Main compose

```bash
docker compose up -d
```

`docker-compose.yml` includes:

| Sub-compose | Service(s) | What it runs |
|---|---|---|
| [LiteLLM](LITELLM.md) | `litellm`, `db`, `prometheus` | LiteLLM proxy + PostgreSQL + metrics |
| [LiteLLM + MCP](LITELLM_MCP.md) | Tool-calling workflow — send messages, route tool results, obtain API tokens |
| [Unsloth](UNSLOTH.md) | `unsloth` | Unsloth environment with CUDA-compiled llama.cpp |
| [vLLM](VLLM.md) | `vllm-qwen`, `vllm-llama` | Multi-model vLLM serving |
| [Kokoro](KOKORO.md) | `kokoro-app`, `kokoro-api` | Text-to-speech (Kokoro-82M) |
