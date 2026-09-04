# llama.cpp — Multi-Model Serving

Run large GGUF-quantized models on llama.cpp's OpenAI-compatible server. This is the sibling of [VLLM.md](../vllm/VLLM.md) for models that either don't fit in vLLM's supported precisions or that don't fit in VRAM at all — llama.cpp is the only inference backend in this stack that supports **dynamic sub-2-bit quants** and **MoE expert offload to system RAM**.

Each service in `ai/llama/docker-compose.llama.yml` is a standalone llama-server instance. Hit any model at the standard OpenAI-compatible endpoint (`/v1/chat/completions`) — pick which model by specifying `"model": "glm5.2"` in your request body.

### Quick start

```bash
docker compose -f ai/llama/docker-compose.llama.yml up -d
# or
make up llama
```

This launches two containers by default:

| Container | Port | Model | Quant | Weights |
|---|---|---|---|---|
| `glm5.2` | `localhost:8010` | Z.ai GLM-5.2 (~753B-A40B MoE) | UD-IQ1_S (~176 GB) | `unsloth/GLM-5.2-GGUF` |
| `qwen3.8-flash` | `localhost:8017` | Qwen3.8-Flash-Next | UD-Q4_K_XL | `unsloth/Qwen3.8-Flash-Next-GGUF` |

Test with:

```bash
curl http://localhost:8010/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "glm5.2", "messages": [{"role": "user", "content": "Hello!"}]}'

curl http://localhost:8017/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "qwen3.8-flash", "messages": [{"role": "user", "content": "Hello!"}]}'
```

### `qwen3.8-flash` sampling defaults

The `qwen3.8-flash` service bakes the **thinking-mode** sampling parameters recommended on the [model card](https://huggingface.co/unsloth/Qwen3.8-Flash-Next-GGUF) into the launch command:

| Flag | Value |
|---|---|
| `--temp` | `1.0` |
| `--top-p` | `0.95` |
| `--top-k` | `20` |
| `--min-p` | `0.0` |
| `--presence-penalty` | `0.0` |

These are **server defaults** — clients that pass their own `temperature`, `top_p`, etc. in the request body still override them per-call. If you want to switch this service to the non-thinking-mode defaults from the model card (`temperature=0.7, top_p=0.80, presence_penalty=1.5`), edit the `command:` block in `ai/llama/docker-compose.llama.yml` and `docker compose ... up -d --force-recreate qwen3.8-flash`.

The default `UD-Q4_K_XL` quant is the canonical example on the model card; swap the `-hf` tag (e.g. `UD-Q8_0`, `UD-Q2_K_XL`) to change the size/quality tradeoff — full quant list is on the HuggingFace page.

### First-run download

The `glm5.2` service uses llama-server's `-hf` flag to download the GGUF from HuggingFace on first start. The weights land in the `llama_data` named volume (`/root/.cache/llama.cpp`) so subsequent starts are instant. Expect the first launch to take a while — UD-IQ1_S is ~176 GB across split GGUF files. Follow progress with:

```bash
make logs llama glm5.2
```

The healthcheck has a **900-second `start_period`** to accommodate the download; the container will not be marked unhealthy while the model is still fetching or loading.

### Hardware sizing — read this before changing quants

GLM-5.2 is a 753B / 40B-active MoE. Even at 1-bit dynamic quant it does **not** fit in 3× A6000 VRAM (144 GB) alone. The service is configured to offload MoE expert layers to system RAM via llama.cpp's `--n-cpu-moe` flag.

Reference numbers for the default box (3× RTX A6000 = 144 GB VRAM, ~400 GB RAM):

| Quant | Weights | VRAM used | RAM used (via `--n-cpu-moe`) | Notes |
|---|---|---|---|---|
| **UD-IQ1_S** _(default)_ | ~176 GB | ~140 GB | ~36 GB | Fits with headroom. Expect ~5–15 tok/s on MoE routing. |
| UD-IQ1_M | ~217 GB | ~140 GB | ~77 GB | Slightly better quality, more experts on CPU → slower. |
| UD-IQ2_XXS | ~230 GB | ~140 GB | ~90 GB | Recommended quality/size tradeoff over IQ1 if you can tolerate the throughput hit. |
| UD-Q4_K_M | ~460 GB | ~140 GB | ~320 GB | Full RAM budget consumed. Sweep-tune `--n-cpu-moe` carefully. |

**`--n-cpu-moe 20` is a starting guess, not a measurement.** GLM-5.2 has ~93 layers; 20 CPU-offloaded expert blocks is a conservative default that should not OOM. Increase if you see `CUDA out of memory` at model load; decrease if `nvidia-smi` shows VRAM well under the limit and you want more speed. See [Tuning `--n-cpu-moe`](#tuning---n-cpu-moe) below.

### Configuration knobs

The full launch command lives in `ai/llama/docker-compose.llama.yml`. Every flag is set explicitly so behavior can be reviewed at a glance:

| Flag | Value | Purpose |
|---|---|---|
| `-hf unsloth/GLM-5.2-GGUF:UD-IQ1_S` | — | Model repo + quant tag (llama-server auto-fetches split GGUF files) |
| `-a glm5.2` | — | Alias returned by `/v1/models`. Must match `"model"` field in requests and the LiteLLM route. |
| `-ngl 999` | — | Offload all remaining layers to GPU (llama.cpp caps at the actual layer count). |
| `--tensor-split 1,1,1` | — | Even VRAM split across the 3 A6000s. Adjust ratios if GPUs have unequal memory. |
| `--n-cpu-moe 20` | — | Number of MoE expert blocks kept on CPU. Higher = less VRAM, slower. See tuning below. |
| `-c 32768` | — | Context window. 1M is theoretically supported but the KV cache would swamp VRAM — start low, raise if you need it. |
| `--cache-type-k q4_0` / `--cache-type-v q4_0` | — | Quantized KV cache saves ~75% of KV VRAM. Small quality cost. |
| `--flash-attn on` | — | Required for quantized KV cache; also faster generally. |
| `--jinja` | — | Enables GLM's chat template (embedded in the GGUF metadata). Without this, chat requests get raw prompt formatting. |
| `--mlock --no-mmap` | — | Pin weights in RAM so the kernel doesn't page them out — important for a 176 GB model that won't fit in the page cache. |

### Tuning `--n-cpu-moe`

Run the container and watch VRAM after model load stabilizes:

```bash
watch -n 1 nvidia-smi --query-gpu=memory.used,memory.total --format=csv
```

- **If any GPU is near 100%**: increase `--n-cpu-moe` by 4 and restart the container.
- **If all GPUs are well under 90%** (say, sitting at 32/48 GB used): decrease `--n-cpu-moe` by 4 to pull experts back onto GPU for higher throughput.

Restart the container after each change:

```bash
docker compose -f ai/llama/docker-compose.llama.yml up -d --force-recreate glm5.2
```

There is no rule-of-thumb formula because expert size varies with quant. Two or three iterations should converge you on a value that leaves ~2 GB headroom on the tightest GPU.

### Switching quants

To swap UD-IQ1_S for a different quant, edit the `-hf` line in `ai/llama/docker-compose.llama.yml`:

```yaml
command: >
  -hf unsloth/GLM-5.2-GGUF:UD-IQ2_XXS   # was UD-IQ1_S
```

Then recreate:

```bash
docker compose -f ai/llama/docker-compose.llama.yml up -d --force-recreate glm5.2
```

The new weights download into the same `llama_data` volume; the old ones stay cached unless you `docker volume rm`. This lets you A/B quants without paying the download twice.

### Adding more models

Add a new service block to `ai/llama/docker-compose.llama.yml`. A commented template lives at the top of the file. Requirements:

1. Pick a new host port and add it to the **PORT REGISTRY** block in `.env` (see the vertical alignment of the existing `PORT_*` lines — match the format).
2. Give the service its own `container_name` and `-a <alias>` so LiteLLM and clients can address it distinctly.
3. Add the service name to the `llama` line in the root `Makefile` so `make up llama` / `make logs llama` cover it:
   ```
   $(eval $(call service,llama,glm5.2 my-new-model))
   ```

Both services share the `llama_data` volume, so downloads for one don't affect the other.

### LiteLLM integration

Register `glm5.2` in your LiteLLM config so clients hit it through the unified proxy:

```yaml
# litellm_config.yaml
model_list:
  - model_name: glm5.2
    litellm_params:
      model: openai/glm5.2
      api_base: http://glm5.2:8000/v1
      api_key: sk-no-key-required
```

Both LiteLLM and `glm5.2` sit on `ai_shared`, so container-name DNS resolution works. Once registered, clients hit LiteLLM the same way they hit any other model:

```bash
curl http://localhost:4001/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "glm5.2", "messages": [{"role": "user", "content": "Hello"}]}'
```

### State files (outside the repo)

| Path | Purpose |
|---|---|
| `llama_data` (named volume) | HuggingFace-cached GGUF weights — 176 GB and up. Persists across container restarts. Delete with `docker volume rm ai-llama_llama_data` (compose project prefix `ai-llama` is set in the Makefile). |

### Non-obvious constraints

- **`ghcr.io/ggml-org/llama.cpp:server-cuda` is a moving tag.** Upstream ships fast and occasionally changes CLI flag names (`--n-cpu-moe` is relatively new — added mid-2026). If you upgrade and see `unrecognized argument`, pin to a specific dated tag from https://github.com/ggml-org/llama.cpp/pkgs/container/llama.cpp.
- **Split GGUF files auto-resolve via `-hf`.** UD-IQ1_S ships as `*-00001-of-000NN.gguf` chunks. llama-server fetches and stitches them; you don't need to pass any of the individual filenames.
- **`--mlock` requires the container to have the `IPC_LOCK` capability on kernels that enforce RLIMIT_MEMLOCK.** If model load fails with a mlock error, add `cap_add: [IPC_LOCK]` to the service. Not needed on typical Docker Desktop / systemd setups.
- **First request after boot is slow even after model load** — llama.cpp does a warmup pass on the first inference call.
- **Editing the compose file's `command:` block requires `--force-recreate`.** Plain `up -d` will not pick up flag changes on a running container.

### Common pitfalls

| Pitfall | Effect |
|---|---|
| Setting `-c 1048576` (1M context) on this quant/hardware | KV cache alone eats 60+ GB → CUDA OOM at load. Stay ≤ 128k unless you know why. |
| Removing `--jinja` | Chat template not applied → model responds as if in raw completion mode, ignores roles. |
| Forgetting to raise `PORT_LLAMA_*` when adding a second service | Both services try to bind the same host port → second one fails silently in `docker compose up -d`. |
| Deleting `llama_data` to "free space" | Full re-download of 176 GB on next start. |
| Running `-hf` behind a proxy without `HF_TOKEN` for gated repos | `unsloth/GLM-5.2-GGUF` is currently public, but any private/gated repo download will 401. |
| Editing `docker-compose.llama.yml` without `--force-recreate` | Old container keeps running with old flags. |

### No tests

There is no automated test suite. Verify by hitting `/v1/chat/completions` after `make up llama glm5.2` and confirming a coherent response. `curl http://localhost:8010/health` should return `{"status":"ok"}` once the model has finished loading.
