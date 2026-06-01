# Docker Infrastructure

All services are orchestrated through a single `docker-compose.yml` that includes three sub-composes. Each can also be run independently.

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
