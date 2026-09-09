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
    image: vllm/vllm-openai:v0.29.0
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

**Guidelines for the image tag:** copy the pinned `vllm/vllm-openai:vX.Y.Z` tag from the sibling blocks, never `latest` — `up -d` does not re-pull a cached `latest`, so a model that needs a newer vLLM fails on a stale image with an "invalid tool call parser" / "unknown architecture" error that looks like a typo. If the new model needs a newer release than the siblings use, bump the pin on the new service only and note the reason in a comment.

**Guidelines for picking ports:** use consecutive ports (8002, 8003, 8004…) and make sure none are already in use.

**Guidelines for `--max-model-len`:** larger context lengths need more GPU memory. If a container OOMs on startup, reduce it (e.g. `4096` for 6GB GPUs, `16384` for 24GB+ GPUs).

**Guidelines for `--gpu-memory-utilization`:** controls how much of the GPU VRAM vLLM reserves. Lower values leave room for other containers. If you get OOM errors, try `0.7` or `0.8`.

### Tensor parallelism (splitting one model across multiple GPUs)

To run a single model across multiple GPUs for lower latency and larger KV cache, add `--tensor-parallel-size N` to the `command` and **set `shm_size`** on the service:

```yaml
  qwen3.8:
    image: vllm/vllm-openai:v0.29.0
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

### Single-GPU qwen3.8 — pinning a service to one card

`qwen3.8-solo` serves the same `cyankiwi/Qwen3.8-27B-AWQ-INT4` as `qwen3.8`, but on one A6000 so it can run **alongside** `muse-glimmer`, which occupies the first two cards. The block is a copy of `qwen3.8` with four differences:

```yaml
  qwen3.8-solo:
    # no shm_size — TP=1 has no cross-worker shared memory
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              device_ids: ['2']       # instead of count: all
              capabilities: [gpu]
    command: >
      cyankiwi/Qwen3.8-27B-AWQ-INT4
      --served-model-name qwen3.8-solo
      # no --tensor-parallel-size
      --max-num-seqs 3               # was 16 on two cards
      # ...everything else identical
```

**`device_ids` vs `count`.** `count: all` exposes every GPU and lets vLLM take the first N it needs — fine when a service owns the box, wrong when two services must share it. `device_ids: ['2']` sets `NVIDIA_VISIBLE_DEVICES=2`, so the container sees exactly one card and vLLM's `cuda:0` *is* host GPU 2. Indices follow `nvidia-smi` order and are 0-based, so "slot 3" is `'2'`. If a driver update or reseat changes enumeration, pin by UUID instead: `nvidia-smi -L` prints `GPU-xxxxxxxx-…` ids that Docker accepts verbatim in `device_ids`. Compose rejects `count` and `device_ids` on the same entry — use one.

**Why 3 sequences.** Qwen3.8-27B is a hybrid: 16 of its 64 layers are full attention (4 KV heads × 256 head_dim), the rest are linear attention with a small fixed state. In BF16 that is 64 KiB of KV per token, so a full 131072-token sequence costs ~8 GiB. One A6000 at 0.90 utilisation leaves ~21 GiB for KV after ~19 GiB of INT4 weights + MTP head and ~3 GiB of activations — roughly 2½ max-length sequences, which is why the cap is 3 (the same figure `qwen3.6`, the same architecture on one card, has run with). As always the cap is a ceiling: a dozen 8k-token chats fit fine; three 128k ones will queue briefly. `--kv-cache-dtype fp8` would double that if you ever need it.

**What can co-run.** With three A6000s and these services at 0.90 utilisation:

| Running | GPUs used | Can add |
|---|---|---|
| `muse-glimmer` (TP=2, first two cards) | 0, 1 | `qwen3.8-solo` **or** `qwen3.6` (both TP=1) — but not both, and `qwen3.6` uses `count: all`, so it grabs GPU 0 and collides with muse; give it a `device_ids` too if you want that pairing |
| `qwen3.8` (TP=2, first two cards) | 0, 1 | `qwen3.8-solo` on GPU 2 — pointless duplication, but it works |
| `glm5.2` / `qwen3.8-flash` (llama.cpp, `--tensor-split 1,1,1`) | 0, 1, 2 | nothing — they use all three cards |

`qwen3.8` and `qwen3.8-solo` are separate LiteLLM aliases on purpose. Listing both under one `model_name` would make LiteLLM round-robin onto whichever is down and burn a retry + cooldown per request; keep them distinct and point clients at the one that is up.

### Muse Glimmer 30B — tensor parallel + DFlash speculative decoding

The `muse-glimmer` service serves [`meta-models/Muse-Glimmer-30B`](https://huggingface.co/meta-models/Muse-Glimmer-30B) — Meta's dense 29.6B vision-language model (52-layer text decoder + ~1.8B ViT-G/14 encoder, 131072-token trained context served at 262144 — see *Serving beyond the trained context* below, Apache 2.0, not gated) — in BF16 across two A6000s with [DFlash](https://huggingface.co/meta-models/Muse-Glimmer-30B-assistant) block-diffusion speculative decoding.

```yaml
  muse-glimmer:
    image: vllm/vllm-openai:v0.29.0     # pinned — see below
    shm_size: '8gb'                     # TP > 1
    # ...
    command: >
      meta-models/Muse-Glimmer-30B
      --served-model-name muse-glimmer
      --tensor-parallel-size 2
      --max-model-len 262144           # > config.json; needs VLLM_ALLOW_LONG_MAX_MODEL_LEN=1
      --max-num-seqs 4
      --gpu-memory-utilization 0.90
      --generation-config auto
      --reasoning-parser muse_glimmer
      --enable-auto-tool-choice
      --tool-call-parser muse_glimmer
      --speculative-config '{"method":"dflash","model":"meta-models/Muse-Glimmer-30B-assistant","num_speculative_tokens":15}'
```

**vLLM version — pinned, not `latest`.** Model support, the `muse_glimmer` reasoning + tool-call parsers, and DFlash2 all landed in **vLLM v0.28.0** (2026-08-26; [#51655](https://github.com/vllm-project/vllm/pull/51655), [#52816](https://github.com/vllm-project/vllm/pull/52816)); **v0.29.0** (2026-09-09) adds a DFlash draft RoPE-layout fix and a Muse Glimmer LoRA fix, and is what every service in this compose file now pins except `qwen3.8`, which is held at `v0.27.1` (the build it was already running) until it has been tried on v0.29.0. Meta's recipe names `v0.28.0` as the minimum. The whole file moved off `latest` because `docker compose up -d` never re-pulls a `latest` tag that is already cached on the host — a stale pre-0.28 `latest` boots the container and then dies with:

```
KeyError: 'invalid tool call parser: muse_glimmer (chose from { apertus, ..., qwen3_coder, qwen3_xml, ... })'
```

If you see that, the running image is too old — it is not a typo in the parser name. Bumping a pin is a deliberate edit + `make up vllm <service>` (compose pulls a tag it doesn't have locally); bump one service at a time so a regression is attributable. Meta's own recipe is at [recipes.vllm.ai](https://recipes.vllm.ai/meta-models/Muse-Glimmer-30B).

**Why TP=2 works.** The text stack has 32 attention heads and **2 KV heads**, so the only legal TP sizes are 1 and 2 (see *Picking TP size* above). Two A6000s (2 × 48 GB) comfortably clear Meta's 72 GB minimum:

| Item | Total | Per GPU at TP=2 |
|---|---|---|
| Target weights, BF16 (29.6B text + 1.8B vision) | ~63 GB | ~31.5 GB |
| DFlash drafter, BF16 (~3B, 5 layers, 32q/8kv heads) | ~6 GB | ~3 GB |
| Budget at `--gpu-memory-utilization 0.90` | — | 43.2 GB |
| Left for KV cache + activations + CUDA graphs | — | ~8.7 GB |

KV cache is cheap on this model: only 13 of the 52 layers are full-attention (the other 39 use a 2048-token sliding window), and with 2 KV heads × 128 head_dim a sequence costs about 1 KiB per token per layer — roughly 1.7 GiB per 131072 tokens across both GPUs, so a full 262144-token sequence is ~3.3 GiB. With the ~14–16 GiB KV pool the pair leaves after weights, that is **4 sequences at max length**, which is why `--max-num-seqs` is 4: every admitted request can run to the full context without preemption. `--max-num-seqs` is a ceiling, not a reservation — if you would rather trade guaranteed full-length slots for more concurrency at typical lengths, raise it to 8 and let vLLM queue when KV runs out. At startup vLLM logs `Maximum concurrency for 262144 tokens per request: N`; that N is the real number. If startup OOMs (e.g. after adding `--limit-mm-per-prompt` for multi-image prompts), drop utilisation to `0.85` before touching context length.

**Serving beyond the trained context.** `config.json` says `max_position_embeddings: 131072`, and vLLM refuses a longer `--max-model-len` unless the container sets `VLLM_ALLOW_LONG_MAX_MODEL_LEN=1` (it does). No RoPE scaling / YaRN / `--hf-overrides` is involved, and none is needed: the 13 global layers are **NoPE** (`layer_rope_theta` is `0` for every `full_attention` layer — they carry no positional embedding at all), and the 39 RoPE layers attend only within a 2048-token sliding window, so the largest relative offset any rotary embedding ever encodes is 2048 regardless of sequence length. The DFlash drafter is built the same way (sliding window 2048 on all 5 layers). The model was *trained* to 128K, so quality past that is extrapolation Meta has not benchmarked — treat 128K–256K as usable-but-unvalidated, and if long-prompt recall degrades, drop `--max-model-len` back to `131072`, remove the env var, raise `--max-num-seqs` to 16, and set LiteLLM `max_input_tokens` back to `114688`.

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
