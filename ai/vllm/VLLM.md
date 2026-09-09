# vLLM — Multi-Model Serving

> **Windows / WSL2 compatibility note:** vLLM v0.22.0+ (the current `latest` image) uses a V1 engine that requires CUDA Unified Virtual Addressing (UVA), which is unavailable in WSL2's paravirtualized GPU driver. Running the latest image on Windows Docker with WSL2 will fail at startup with `RuntimeError: UVA is not available`. Pin the image to `vllm/vllm-openai:v0.6.6` until upstream adds a WSL2-compatible code path.

Run multiple models simultaneously, each in its own container on a different port. Hit any model at the standard OpenAI-compatible endpoint (`/v1/chat/completions`) — pick which model by specifying `"model": "qwen"` or `"model": "llama"` in your request body.

### Quick start

```bash
docker compose -f ai/vllm/docker-compose.vllm.yml up -d
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

Each service in `ai/vllm/docker-compose.vllm.yml` is a standalone vLLM instance. The `--model` flag on the command line tells vLLM which HuggingFace model to load. Each container gets its own GPU memory allocation and listens on a different host port, so they run in parallel without conflict.

For strategies on dividing GPU resources between containers (time-slicing, MIG, `--gpu-memory-utilization` tuning), see [GPU_SHARING_GUIDE.md](../GPU_SHARING_GUIDE.md).

The HuggingFace token (`HF_TOKEN`) is read from `.env` so gated models can be downloaded.

### HuggingFace Token Setup

Gated models like Llama require a HuggingFace access token:

1. **Create a token**: Go to Settings → Access Tokens in your HuggingFace profile and create a new "Read" token.
2. **Accept model licenses**: Some models (e.g. Llama) require you to accept their license on the model page first. Click "Agree and Access" on the model's HuggingFace page before the token will work.
3. **Add to `.env`**: Set `HF_TOKEN=<your-token>` in your project's `.env` file.

The token only needs **Read** permissions — model access is granted per-model via license acceptance, not token scopes.

### Adding more models

To add a third model, add a new service block to `ai/vllm/docker-compose.vllm.yml`:

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

### Tensor parallelism (splitting one model across multiple GPUs)

To run a single model across multiple GPUs for lower latency and larger KV cache, add `--tensor-parallel-size N` to the `command` and **set `shm_size`** on the service:

```yaml
  qwen3.8:
    image: vllm/vllm-openai:latest
    container_name: qwen3.8
    restart: unless-stopped
    shm_size: '8gb'          # required for TP > 1
    # ...
    command: >
      cyankiwi/Qwen3.8-27B-AWQ-INT4
      --tensor-parallel-size 2
      # ...
```

**Why `shm_size` is required:** vLLM's tensor-parallel workers coordinate through POSIX shared memory (`/dev/shm`). Docker's default is 64 MiB, which is too small — startup fails with `RuntimeError: Insufficient space in /dev/shm`. `shm_size: '8gb'` gives the container an isolated tmpfs (lazy-allocated, so you only pay for what's actually used).

**Why not `ipc: host`:** it works but shares the host's entire IPC namespace, breaking container isolation and coupling every TP container to the host's shm. `shm_size` is the isolated, security-scan-friendly equivalent — same performance, no coupling.

**Picking TP size:** must divide **both** the model's `num_attention_heads` and `num_key_value_heads`. GQA models often have only 4–8 KV heads, so TP=3 typically fails even when TP=2 and TP=4 work. Check the model's `config.json` before choosing. A6000s support NVLink only in 2-way pairs — TP=2 across an NVLinked pair scales close to linearly; higher TP over PCIe scales sub-linearly.

**One `shm_size` per replica:** if you run multiple containers (data parallel), each needs its own `shm_size` line — they don't share.

### Custom chat template — qwen3.8

The `qwen3.8` service loads a patched chat template from [`ai/jinja/qwen_fixed_chat_template.jinja`](../jinja/qwen_fixed_chat_template.jinja), sourced from [froggeric/Qwen-Fixed-Chat-Templates](https://huggingface.co/froggeric/Qwen-Fixed-Chat-Templates). The same file is also mounted by the llama.cpp `qwen3.8-flash` service — see [`ai/jinja/README.md`](../jinja/README.md) for the shared-config convention. It fixes known bugs in the stock Qwen 3.5/3.6/3.8 templates, most notably:

- **Duplicate blank `<think>` blocks** in conversation history that caused the model to lose reasoning state and re-think from scratch until it hit the token limit.
- **`enable_thinking=false` crash** on Qwen 3.8.
- **Runaway token budget** from the `reasoning_effort=xhigh` default (fixed template defaults to `medium`; callers can override per-request).
- **KV-cache invalidation** on multi-turn reasoning conversations.

Applied via a bind mount and the `--chat-template` flag:

```yaml
  qwen3.8:
    # ...
    volumes:
      - vllm_data:/root/.cache/huggingface
      - ../jinja/qwen_fixed_chat_template.jinja:/config/qwen_fixed_chat_template.jinja:ro
    command: >
      # ...
      --chat-template /config/qwen_fixed_chat_template.jinja
```

**Updating the template:** re-download the raw file over the existing [`ai/jinja/qwen_fixed_chat_template.jinja`](../jinja/qwen_fixed_chat_template.jinja) and restart the container. No compose changes needed. Both vLLM `qwen3.8` and llama.cpp `qwen3.8-flash` bind-mount the same file, so a single update fixes both — recreate the containers you care about (`docker compose -f ai/vllm/docker-compose.vllm.yml up -d --force-recreate qwen3.8` and/or `docker compose -f ai/llama/docker-compose.llama.yml up -d --force-recreate qwen3.8-flash`).

**Reasoning effort:** the fixed template's default is `medium`. To bump it globally, add `--default-chat-template-kwargs '{"reasoning_effort":"xhigh"}'` to the command; to bump per-request, pass `{"reasoning_effort": "xhigh"}` in the client JSON body.

**qwen3.6 uses the stock template.** If it exhibits the same thinking-loop symptoms, mount and apply the same file.

### Muse Glimmer 30B — tensor parallel + DFlash speculative decoding

The `muse-glimmer` service serves [`meta-models/Muse-Glimmer-30B`](https://huggingface.co/meta-models/Muse-Glimmer-30B) — Meta's dense 29.6B vision-language model (52-layer text decoder + ~1.8B ViT-G/14 encoder, 131072-token context, Apache 2.0, not gated) — in BF16 across two A6000s with [DFlash](https://huggingface.co/meta-models/Muse-Glimmer-30B-assistant) block-diffusion speculative decoding.

```yaml
  muse-glimmer:
    image: vllm/vllm-openai:latest      # needs >= v0.28.0
    shm_size: '8gb'                     # TP > 1
    # ...
    command: >
      meta-models/Muse-Glimmer-30B
      --served-model-name muse-glimmer
      --tensor-parallel-size 2
      --max-model-len 131072
      --max-num-seqs 16
      --gpu-memory-utilization 0.90
      --generation-config auto
      --reasoning-parser muse_glimmer
      --enable-auto-tool-choice
      --tool-call-parser muse_glimmer
      --speculative-config '{"method":"dflash","model":"meta-models/Muse-Glimmer-30B-assistant","num_speculative_tokens":15}'
```

**vLLM version.** Model support, the `muse_glimmer` reasoning + tool-call parsers, and DFlash2 all landed in **vLLM v0.28.0** (2026-08-26; [#51655](https://github.com/vllm-project/vllm/pull/51655), [#52816](https://github.com/vllm-project/vllm/pull/52816)). The `latest` tag satisfies this; if you ever pin, pin to `v0.28.0` or newer. Meta's own recipe is at [recipes.vllm.ai](https://recipes.vllm.ai/meta-models/Muse-Glimmer-30B).

**Why TP=2 works.** The text stack has 32 attention heads and **2 KV heads**, so the only legal TP sizes are 1 and 2 (see *Picking TP size* above). Two A6000s (2 × 48 GB) comfortably clear Meta's 72 GB minimum:

| Item | Total | Per GPU at TP=2 |
|---|---|---|
| Target weights, BF16 (29.6B text + 1.8B vision) | ~63 GB | ~31.5 GB |
| DFlash drafter, BF16 (~3B, 5 layers, 32q/8kv heads) | ~6 GB | ~3 GB |
| Budget at `--gpu-memory-utilization 0.90` | — | 43.2 GB |
| Left for KV cache + activations + CUDA graphs | — | ~8.7 GB |

KV cache is cheap on this model: only 13 of the 52 layers are full-attention (the other 39 use a 2048-token sliding window), and with 2 KV heads × 128 head_dim a full 131072-token sequence costs roughly 1.7 GB total across both GPUs. So `--max-model-len 131072` with `--max-num-seqs 16` fits with headroom. If startup OOMs anyway (e.g. after adding `--limit-mm-per-prompt` for multi-image prompts), drop utilisation to `0.85` before touching context length.

**DFlash speculative decoding.** `meta-models/Muse-Glimmer-30B-assistant` is the official drafter — a 5-layer block-diffusion network that reads the target's residual stream at layers {1, 13, 25, 37, 49} and proposes a 16-token block in one forward pass; the target verifies the block in parallel, so output is bit-identical to plain decoding. Configuration is `--speculative-config '{"method":"dflash","model":"<drafter>","num_speculative_tokens":15}'` — the drafter's block size is 16, so **`num_speculative_tokens` is fixed at 15** (block minus the anchor token); it is not a tuning knob here. The drafter is distilled for this exact target checkpoint and must not be paired with a quantised or fine-tuned variant. Meta reports ~3× single-stream speedup on consumer GPUs; expect the gain to shrink as concurrency rises, which is why the recipe halves `--max-num-seqs` when DFlash is on. To disable speculation for A/B testing, remove the `--speculative-config` line and recreate the container. The drafter downloads into the shared `vllm_data` volume alongside the target on first start.

**Parsers and sampling.** `--reasoning-parser muse_glimmer` and `--tool-call-parser muse_glimmer` must run **together** — both key off the model's channel-scoped message framing (reasoning rides special markers, tool calls are emitted as `<atem:function_calls>` blocks, not OpenAI JSON). The reasoning parser forces `skip_special_tokens=False` itself. `--generation-config auto` picks up Meta's published sampling defaults (temperature 1.0, top_p 0.95, top_k 64) from the repo's `generation_config.json`; the model card explicitly warns against greedy decoding. Reasoning depth is set by a system-prompt line `Reasoning strength: low|medium|high|xhigh`, not by a chat-template kwarg. Stock chat template — no jinja mount needed.

**Multimodal.** Text + image input, up to 4096 visual tokens per image. vLLM's default is one image per prompt; add `--limit-mm-per-prompt '{"image": 4}'` to the command if callers need more, and re-check the VRAM table above. LiteLLM registers the alias with `supports_vision: true`.

**GPU scheduling — mutually exclusive with `qwen3.8`.** Both services are TP=2 at 0.90 utilisation on `count: all`, so both land on the first two visible GPUs and cannot co-run. Bring up one or the other:

```bash
make stop vllm qwen3.8 && make up vllm muse-glimmer
```

To pin the NVLinked pair explicitly instead of relying on enumeration order, replace `count: all` with `device_ids: ['0', '1']` under `deploy.resources.reservations.devices`.

**Cold start.** First launch pulls ~69 GB (target + drafter) from HuggingFace and then shards it; the healthcheck's `start_period` is 600s for that reason. Subsequent starts load from `vllm_data` in a few minutes.

### Removing a model

Delete the corresponding service block from `ai/vllm/docker-compose.vllm.yml`, then:

```bash
docker compose -f ai/vllm/docker-compose.vllm.yml down
docker compose -f ai/vllm/docker-compose.vllm.yml up -d
```
