# vLLM — Multi-Model Serving

> **Windows / WSL2 compatibility note:** vLLM v0.22.0+ (the current `latest` image) uses a V1 engine that requires CUDA Unified Virtual Addressing (UVA), which is unavailable in WSL2's paravirtualized GPU driver. Running the latest image on Windows Docker with WSL2 will fail at startup with `RuntimeError: UVA is not available`. Pin the image to `vllm/vllm-openai:v0.6.6` until upstream adds a WSL2-compatible code path.

Run multiple models simultaneously, each in its own container on a different port. Hit any model at the standard OpenAI-compatible endpoint (`/v1/chat/completions`) — pick which model by specifying `"model": "qwen"` or `"model": "llama"` in your request body.

### Quick start

```bash
docker compose -f ai/docker-compose.vllm.yml up -d
```

This launches two containers by default:

| Container | Port | Model |
|---|---|---|
| `vllm-qwen` | `localhost:8002` | `Qwen/Qwen2.5-3B-Instruct` |
| `vllm-llama` | `localhost:8003` | `meta-llama/Llama-3.2-3B-Instruct` |

Test with:

```bash
# Qwen on port 8002
curl http://localhost:8002/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "qwen", "messages": [{"role": "user", "content": "Hello!"}]}'

# Llama on port 8003
curl http://localhost:8003/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "llama", "messages": [{"role": "user", "content": "Hello!"}]}'
```

### How it works

Each service in `ai/docker-compose.vllm.yml` is a standalone vLLM instance. The `--model` flag on the command line tells vLLM which HuggingFace model to load. Each container gets its own GPU memory allocation and listens on a different host port, so they run in parallel without conflict.

The HuggingFace token (`HF_TOKEN`) is read from `.env` so gated models can be downloaded.

### HuggingFace Token Setup

Gated models like Llama require a HuggingFace access token:

1. **Create a token**: Go to Settings → Access Tokens in your HuggingFace profile and create a new "Read" token.
2. **Accept model licenses**: Some models (e.g. Llama) require you to accept their license on the model page first. Click "Agree and Access" on the model's HuggingFace page before the token will work.
3. **Add to `.env`**: Set `HF_TOKEN=<your-token>` in your project's `.env` file.

The token only needs **Read** permissions — model access is granted per-model via license acceptance, not token scopes.

### Adding more models

To add a third model, add a new service block to `ai/docker-compose.vllm.yml`:

```yaml
  vllm-mistral:
    image: vllm/vllm-openai:latest
    container_name: vllm-mistral
    restart: unless-stopped
    environment:
      - HUGGING_FACE_HUB_TOKEN=${HF_TOKEN}
    ports:
      - "8004:8000"          # pick an unused host port
    volumes:
      - vllm_data:/root/.cache/huggingface
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
    command: >
      --model mistralai/Mistral-7B-Instruct-v0.3
      --dtype float16
      --max-model-len 8192
      --gpu-memory-utilization 0.9
```

Then hit it at `localhost:8004` with `"model": "mistral"` in the request body.

**Guidelines for picking ports:** use consecutive ports (8002, 8003, 8004…) and make sure none are already in use.

**Guidelines for `--max-model-len`:** larger context lengths need more GPU memory. If a container OOMs on startup, reduce it (e.g. `4096` for 6GB GPUs, `16384` for 24GB+ GPUs).

**Guidelines for `--gpu-memory-utilization`:** controls how much of the GPU VRAM vLLM reserves. Lower values leave room for other containers. If you get OOM errors, try `0.7` or `0.8`.

### Removing a model

Delete the corresponding service block from `ai/docker-compose.vllm.yml`, then:

```bash
docker compose -f ai/docker-compose.vllm.yml down
docker compose -f ai/docker-compose.vllm.yml up -d
```
