# llama.cpp — Multi-Model Serving

Run large GGUF-quantized models on llama.cpp's OpenAI-compatible server. This is the sibling of [VLLM.md](../vllm/VLLM.md) for models that either don't fit in vLLM's supported precisions or that don't fit in VRAM at all — llama.cpp is the only inference backend in this stack that supports **dynamic sub-2-bit quants** and **MoE expert offload to system RAM**.

Each service in `ai/llama/docker-compose.llama.yml` is a standalone llama-server instance. Hit any model at the standard OpenAI-compatible endpoint (`/v1/chat/completions`) — pick which model by specifying `"model": "glm5.2"` in your request body.

### Quick start

```bash
docker compose -f ai/llama/docker-compose.llama.yml up -d
# or
make up llama
```

This launches four containers by default:

| Container | Port | Model | Quant | Weights |
|---|---|---|---|---|
| `glm5.2` | `localhost:8010` | Z.ai GLM-5.2 (~753B-A40B MoE) | UD-IQ1_S (~176 GB) | `unsloth/GLM-5.2-GGUF` |
| `qwen3.8-flash` | `localhost:8017` | Qwen3.8-Flash-Next | UD-Q4_K_XL + MTP `shared-Q8_0` draft head | `unsloth/Qwen3.8-Flash-Next-GGUF` |
| `glm5.3-flash` | `localhost:8020` | Z.ai GLM-5.3-Flash (321B-A18B MoE) — **CPU + RAM only, no GPU** | UD-Q3_K_XL (~148 GB) + embedded MTP head | `unsloth/GLM-5.3-Flash-GGUF` |
| `qwen3.5-4b` | `localhost:8028` | Qwen3.5-4B — **CPU only**, route-policy's classifier model, not in LiteLLM | Q4_K_M (2.74 GB) | `unsloth/Qwen3.5-4B-GGUF` |

`glm5.2` runs the stock `ghcr.io/ggml-org/llama.cpp:server-cuda` image. `qwen3.8-flash` and `glm5.3-flash` build local images from Unsloth's llama.cpp prebuilds ([`Dockerfile.llama-unsloth`](Dockerfile.llama-unsloth)): the first because MTP speculative decoding for Qwen3.8-Flash-Next is not in mainline llama.cpp yet — see [MTP speculative decoding](#qwen38-flash-mtp-speculative-decoding) below — the second because the GLM-5.3-Flash architecture itself is not merged upstream, and it uses the GPU-free `cpu` tarball flavour — see [`glm5.3-flash` — GLM-5.3-Flash on CPU and RAM only](#glm53-flash--glm-53-flash-on-cpu-and-ram-only). `qwen3.5-4b` reuses `glm5.3-flash`'s `cpu` image — see [`qwen3.5-4b` — a small CPU model for route-policy](#qwen35-4b--a-small-cpu-model-for-route-policy). `make up llama` / `docker compose up -d` build both images on first run.

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

The service also loads the **shared Qwen chat template** from [`ai/jinja/qwen_fixed_chat_template.jinja`](../jinja/qwen_fixed_chat_template.jinja) — the same file the vLLM `qwen3.8` service mounts. It's bind-mounted into the container at `/config/qwen_fixed_chat_template.jinja` and passed to llama-server via `--chat-template-file`. `--jinja` stays on so llama.cpp renders the template with its Jinja engine (the built-in template renderer can't handle the full Qwen template). Patching the template once (see [`ai/jinja/README.md`](../jinja/README.md)) fixes both backends — the llama.cpp service picks up the change on the next `--force-recreate`.

The default `UD-Q4_K_XL` quant is the canonical example on the model card; swap the `-hf` tag (e.g. `UD-Q8_0`, `UD-Q2_K_XL`) to change the size/quality tradeoff — full quant list is on the HuggingFace page.

### `qwen3.8-flash` MTP speculative decoding

Qwen3.8-Flash-Next ships **MTP (multi-token prediction) draft heads** in the `MTP/` folder of `unsloth/Qwen3.8-Flash-Next-GGUF`. llama-server runs the small head ahead to propose tokens and the main model verifies them in one forward pass. Verification is exact, so output is unchanged — Unsloth measures **1.3–1.7× tok/s at concurrency 1 with greedy sampling** ([MTP/README.md](https://huggingface.co/unsloth/Qwen3.8-Flash-Next-GGUF/blob/main/MTP/README.md)).

**Why a custom image.** Mainline llama.cpp has no MTP graph for the `qwen4exp` architecture and no cross-model tensor borrowing — [ggml-org/llama.cpp#28243](https://github.com/ggml-org/llama.cpp/pull/28243) and [#27836](https://github.com/ggml-org/llama.cpp/pull/27836) are still open. On the stock `server-cuda` image the head fails to load (`exactly one out of metadata, path_model, and file must be defined` / `failed to measure the memory of the extra model`) or `--spec-type draft-mtp` silently does nothing. Unsloth's prebuilt **"mix" releases** ([unslothai/llama.cpp/releases](https://github.com/unslothai/llama.cpp/releases)) are upstream plus a curated set of open PRs, including [unslothai/llama.cpp#144](https://github.com/unslothai/llama.cpp/pull/144) (MTP for Qwen3.8-Flash-Next). [`Dockerfile.llama-unsloth`](Dockerfile.llama-unsloth) drops the `linux-x64-cuda13-portable` tarball onto an `nvidia/cuda:13.3.1-runtime-ubuntu22.04` base (built on GitHub's `ubuntu-22.04` runner with the CUDA 13.3 toolkit; `sm 75–120` covers the A6000s; the host driver is R580 = CUDA 13.0, which the 13.3 runtime accepts via minor-version compatibility). Three `.env` keys drive it:

| Key | Default | Notes |
|---|---|---|
| `LLAMA_UNSLOTH_TAG` | `b10796-mix-659e406` | Release tag. Also part of the local image tag (`llama-unsloth:<tag>-<variant>`), so a bump forces a rebuild. |
| `LLAMA_UNSLOTH_VARIANT` | `cuda13-portable` | Tarball flavour. `cuda13-*` needs an R580+ host driver; `cuda12-*` works on R525+. `-newer` / `-older` are narrower arch lists with no functional difference. |
| `LLAMA_CUDA_RUNTIME_IMAGE` | `nvidia/cuda:13.3.1-runtime-ubuntu22.04` | Base image. Must match the variant's CUDA major — `libggml-cuda.so` links `libcudart.so.13` / `libcublas.so.13`. For `cuda12-portable` use `nvidia/cuda:12.8.1-runtime-ubuntu22.04`. The Dockerfile runs `ldd` on the tarball and fails the build on a mismatch. |

Upgrade with:

```bash
docker compose -f ai/llama/docker-compose.llama.yml up -d --build --force-recreate qwen3.8-flash
```

**How both models get pulled.** The relevant flags in the `command:` block:

| Flag | Value | Purpose |
|---|---|---|
| `-hf` | `unsloth/Qwen3.8-Flash-Next-GGUF:UD-Q4_K_XL` | Main model. The part after `:` is a **quant tag**, never a file path. |
| `-hfd` | `unsloth/Qwen3.8-Flash-Next-GGUF` | Draft repo — the same repo, **no tag**. |
| `-md` | `MTP/mtp-Qwen3.8-Flash-Next-shared-Q8_0.gguf` | Exact repo-relative path of the head. When `-hfd` is set, `-md` doubles as the HF file selector, so the head auto-downloads into `llama_data` next to the main weights. |
| `--spec-type` | `draft-mtp` | Explicit. Sidecar auto-discovery only searches the main model's own folder, never `MTP/`, so without this + `-md` you get base speed and no error. |
| `--spec-draft-n-max` | `2` | README default (the Unsloth docs page shows `5`). Higher drafts more per verify pass but each guess is accepted less often. Measured on the 3× A6000 box at temp 1.0: `2` → ~50 tok/s, `3` → ~45 tok/s, so 2 stays. |
| `-ngld` | `999` | Draft head fully on GPU. |
| `-devd` | `CUDA2` | Pin the draft head to the GPU that holds the main model's output layer (the last device under `--tensor-split 1,1,1`). The `shared-*` head borrows that tensor, so co-locating avoids a cross-GPU copy per draft step. |

Things that look like they should work but don't: `-hfd repo:MTP/file.gguf` (the tag is matched as a quant name — that was the `exactly one out of metadata, path_model, and file must be defined` error), and `--spec-type draft-mtp` alone (auto-discovery never finds the head).

**Which head.** `shared-Q8_0` is the README's recommendation: it borrows the token embedding and output projection from the main model (≈1.3 GB smaller than the self-contained `Q8_0`) and drafts identically. The trade-off is one **expected** error pair at startup — the automatic `--fit` memory probe loads the draft on its own before the main model exists, so there is nothing to borrow from:

```
E llama_model_load: error loading model: borrow_shared_tensor: this model is a draft head ...
W operator(): failed to measure the memory of the extra model, fitting without it
```

Speculation still runs. `-c`, `-ngl` and `--tensor-split` are all set explicitly, so the unmeasured draft only matters if VRAM is already at the edge; switch to the self-contained `MTP/mtp-Qwen3.8-Flash-Next-Q8_0.gguf` if you want a clean fit measurement.

**Confirm it is on.** After the first request, the log should show acceptance stats:

```bash
make logs llama qwen3.8-flash | grep "draft acceptance"
# draft acceptance = 0.66139 (325 accepted / 491 generated), mean len = 2.76
```

If that line never appears, speculation is off — almost always a build without MTP support (i.e. the stock image).

**When it hurts.** Unsloth measured MTP as a **net loss (~0.81–0.87×) at concurrency 8**: a busy model has no idle capacity for a draft to exploit. The service runs `--parallel 2` for that reason — two slots is where MTP still pays off, and it leaves each slot 524k of the 1M context. If the box regularly has several simultaneous streams, drop `--spec-type` / `-md` / `-hfd` from the command rather than raising `--parallel`. Higher sampling temperature also lowers acceptance — the 1.67× headline is a greedy number.

### `qwen3.8-flash` reasoning budget

The service caps thinking with two llama-server flags:

| Flag | Value |
|---|---|
| `--reasoning-budget` | `8192` |
| `--reasoning-budget-message` | `"Time to stop thinking. Give the final answer or make the tool call now."` |

`--reasoning-budget N` is the token budget for the think block: `-1` unrestricted (llama-server default), `0` ends thinking immediately, `N>0` caps it. When the cap is reached, llama-server injects the `--reasoning-budget-message` text into the thought stream and emits the end-of-thinking tag, so generation continues straight into the final answer or tool call instead of looping. The budget counts only reasoning tokens — it does not shrink the 65,536-token `--n-predict` output cap in the next section, so a long final answer after a short think is still fine.

Why 8192: with the fixed chat template's default `reasoning_effort=medium`, most agentic turns think for well under 8k tokens; the cap exists to stop the occasional runaway think that otherwise burns the whole output budget and stalls tool loops. Raise it (or set `-1`) for hard-reasoning workloads, then `make up llama qwen3.8-flash` to recreate. Both flags are present in the pinned Unsloth build (`LLAMA_UNSLOTH_TAG=b10796-mix-659e406`); if a future tag drops them, llama-server refuses to start with an unknown-argument error, which the healthcheck surfaces as a restart loop.

### `qwen3.8-flash` context and output budget

`-c 1048576` is split evenly across `--parallel` slots, so each request gets `1048576 / 2 = 524288` tokens. `--n-predict 65536` is the hard per-request output cap — llama-server clamps any larger client `max_tokens` to it. The LiteLLM entry mirrors the split:

| Setting | Where | Value |
|---|---|---|
| Context per slot | llama-server (`-c` / `--parallel`) | 524,288 |
| `--n-predict` | llama-server | 65,536 |
| `max_tokens`, `max_output_tokens` | `ai/litellm/litellm_config.yaml` → `qwen3.8-flash` | 65,536 |
| `max_input_tokens` | same entry | 458,752 |

Why 64k output: Qwen3.8 runs in thinking mode with reasoning preserved, and Unsloth's guidance for Qwen thinking models is a 32k output budget for most prompts and ~80k for hard reasoning. 64k covers nearly everything without starving input. Change `--n-predict`, `max_tokens` / `max_output_tokens`, and `max_input_tokens` **together** — input + output must equal the slot size, and `max_input_tokens` is what `context_window_fallbacks` uses to decide when a request is too big and gets routed to `claude-sonnet-5` (the paid model). Recreate both `qwen3.8-flash` and `litellm` after editing.

### `glm5.3-flash` — GLM-5.3-Flash on CPU and RAM only

`glm5.3-flash` is the one service in this stack with **no GPU at all**: no `runtime: nvidia`, no device reservation, and a llama.cpp build that has no CUDA backend. Weights, KV cache and the MTP draft head live in system RAM and every matmul runs on the CPU cores. It exists so a frontier-class coding model can sit on the box without taking a single byte of VRAM from the vLLM / GPU llama services.

**Why Flash, not full GLM-5.3.** GLM-5.3 and GLM-5.3-Flash are different models, not two quants of one: GLM-5.3 is 754B total / 40B active (same base as GLM-5.2), GLM-5.3-Flash is 321B total / 18B active (288 experts, 8 routed per token, hybrid KDA linear attention + sparse MLA, 1M context). For a CPU box both numbers point the same way:

- **It has to fit in RAM.** The smallest GLM-5.3 quant (`UD-IQ1_S`) is 217 GB — larger than a 200 GB box before the KV cache exists. GLM-5.3-Flash `UD-Q3_K_XL` is 148 GB.
- **CPU decode is memory-bandwidth bound.** Each generated token streams every *active* weight through the memory bus, so tok/s scales with active bytes, not total size. 18B active at ~3.5 bpw is ~8 GB/token; 40B active at 1 bpw is ~5 GB/token but at 1-bit quality, and at any usable quant it is 2x+ the bytes. Flash decodes at roughly twice the speed of the full model at a comparable quality quant.
- Z.ai's own numbers have Flash beating GLM-5.2 on their benchmarks, so nothing is given up relative to the `glm5.2` service.

If more RAM is added later, spend it on a higher Flash quant (`UD-Q4_K_XL`, 200 GB, ~93% accuracy retained on Unsloth's KL-divergence tests) rather than on a 1-bit full GLM-5.3 — same RAM, better quality, twice the tok/s.

**Quant and RAM budget.** The service loads with `--load-mode mmap+mlock`, so the *entire* GGUF is pinned in RAM plus ~10–15 GB for KV, compute buffers and the draft head. Everything else on the box (vLLM pinned buffers, the other llama services, Postgres, …) has to fit in what is left. Only K-quants are sensible on CPU — the `IQ*` families use codebook lookups without a fast CPU matmul path and run at roughly half the speed of a K-quant of the same size.

| `LLAMA_GLM53_FLASH_QUANT` | Weights | Accuracy retained (Unsloth) | RAM you need | Notes |
|---|---|---|---|---|
| `UD-Q2_K_XL` | 109 GB | ~78% | ~130 GB | Fallback if the default doesn't fit next to the rest of the box. |
| **`UD-Q3_K_XL`** _(default)_ | 148 GB | ~85% | ~165 GB | Fits a 200 GB box with ~35 GB for everything else. |
| `UD-Q4_K_XL` | 200 GB | ~93% | ~220 GB | Needs a 256 GB+ RAM upgrade. Best quality/speed point once you have it. |

> **`glm5.2` and `glm5.3-flash` do not coexist in 200 GB.** `glm5.2` keeps the experts of blocks 0–38 in system RAM via `-ot` (~70 GB at UD-IQ1_S). With `glm5.3-flash` pinning another 148 GB, mlock fails or the kernel OOM-kills one of them. Stop `glm5.2` before starting `glm5.3-flash` (or move to 256 GB+). A `failed to mlock` warning in the log means the pin fell back to plain mmap — the model still runs, but page-outs turn into multi-second stalls.

**Threads and NUMA.** `-t` is left at llama.cpp's default, which is the number of physical cores the container can see — normally the right answer. On a dual-socket host, `--numa distribute` is the single biggest CPU knob (llama.cpp's own help text says to drop the page cache first when switching it on). Both go through `LLAMA_GLM53_FLASH_EXTRA_ARGS`, which is appended verbatim to the command:

```bash
# .env
LLAMA_GLM53_FLASH_EXTRA_ARGS=--numa distribute -t 48
```

**MTP speculative decoding.** GLM-5.3-Flash ships one NextN block (`blk.45.nextn.*`, after the 45 trunk layers) *inside* the main GGUF, so unlike `qwen3.8-flash` there is no `-hfd` / `-md` pair: `--spec-type draft-mtp` alone makes llama-server load the extra layer and run it as the drafter. Verification is exact, so output is unchanged. Upstream's MTP PR for this architecture ([ggml-org/llama.cpp#27917](https://github.com/ggml-org/llama.cpp/pull/27917)) measures acceptance 0.74 / mean draft length 4.1 and a 15–30% decode speedup on a heavily CPU-bound setup at `--spec-draft-n-max 3`; Unsloth measures up to 3.3x at long context on GPU. Because CPU decode is bandwidth bound, verifying three drafted tokens costs about the same weight traffic as one, which is why the service uses `n_max 3` rather than the GPU-tuned 2. Confirm it is active the same way as for `qwen3.8-flash`:

```bash
make logs llama glm5.3-flash | grep "draft acceptance"
```

If tok/s is *worse* with it on (many concurrent streams, or a high sampling temperature dragging acceptance down), delete the two `--spec-*` lines from the `command:` block.

**Thinking budget.** The GGUF's chat template reads `reasoning_effort` from `--chat-template-kwargs` and accepts exactly `low` and `high`; any other value — including `medium` — falls through to `max`. At CPU speeds a `max` think block can run 20k+ tokens, i.e. tens of minutes per turn, so the default is `low` (`LLAMA_GLM53_FLASH_REASONING_EFFORT`) with `--reasoning-budget 8192` as a hard cap (same mechanism as `qwen3.8-flash`: when the cap hits, the `--reasoning-budget-message` text is injected and the think block is closed). Raise to `high` for hard problems you are willing to wait for; set the budget to `-1` in the compose to remove the cap.

**Context and output.** `-c 262144 --parallel 1` gives one 256k slot. KV is cheap for this architecture (MLA/DSA layers plus a fixed-size recurrent KDA state), so RAM is not what limits context — CPU prefill is. Expect low hundreds of tok/s for prompt processing, i.e. minutes for a fresh 100k-token prompt; llama-server's slot prompt cache means follow-up turns only re-process the new suffix. `--n-predict 32768` caps output per request, mirrored in the LiteLLM entry:

| Setting | Where | Value |
|---|---|---|
| Context (one slot) | llama-server (`-c` / `--parallel`) | 262,144 |
| `--n-predict` | llama-server | 32,768 |
| `max_tokens`, `max_output_tokens` | `ai/litellm/litellm_config.yaml` → `glm5.3-flash` | 32,768 |
| `max_input_tokens` | same entry | 229,376 |
| `stream_timeout` | same entry | 1800 s — per-read budget (`httpx.Timeout`), which on a cold prompt means time-to-first-chunk; a long uncached prompt on CPU legitimately takes minutes before the first token. A caller through Open WebUI is cut first by its 900 s **total** (`OPENWEBUI_AIOHTTP_CLIENT_TIMEOUT`, see [LITELLM.md § Chain timeouts](../litellm/LITELLM.md#chain-timeouts)), so 1800 only matters to direct API callers |

Change `--n-predict`, `max_tokens` / `max_output_tokens`, and `max_input_tokens` together; recreate both `glm5.3-flash` and `litellm` after editing.

**Build pin.** The `glm5next` architecture is **not in mainline llama.cpp** — [ggml-org/llama.cpp#27754](https://github.com/ggml-org/llama.cpp/pull/27754) is still open, and the stock `ghcr.io/ggml-org/llama.cpp:server` image fails with `unknown model architecture: 'glm5next'`. Unsloth's mix releases carry #27754, whose head branch (`unslothai/llama.cpp` `glm5next/upstream`) also contains the NextN/MTP draft graph for the arch. The service builds [`Dockerfile.llama-unsloth`](Dockerfile.llama-unsloth) with the `linux-x64-cpu` tarball on a plain `ubuntu:22.04` base, pinned by its **own** key so it never forces a rebuild or re-validation of `qwen3.8-flash`:

| Key | Default | Notes |
|---|---|---|
| `LLAMA_UNSLOTH_CPU_TAG` | `b10840-mix-d5c17a0` | Unsloth release tag ([releases](https://github.com/unslothai/llama.cpp/releases)). Part of the image name (`llama-unsloth:<tag>-cpu`), so a bump forces a rebuild. Verified for this tag: the `cpu` tarball's `libllama.so` contains the `glm5next` arch and its MTP graph, and every flag in the `command:` block is present in the build. |
| `LLAMA_GLM53_FLASH_QUANT` | `UD-Q3_K_XL` | Quant tag on `unsloth/GLM-5.3-Flash-GGUF` — see the RAM table above. |
| `LLAMA_GLM53_FLASH_REASONING_EFFORT` | `low` | `low` \| `high` \| `max`. |
| `LLAMA_GLM53_FLASH_EXTRA_ARGS` | _(empty)_ | Extra llama-server args appended verbatim (`--numa distribute`, `-t N`, `--flash-attn off`, …). |

```bash
docker compose -f ai/llama/docker-compose.llama.yml up -d --build --force-recreate glm5.3-flash
```

Deliberately **not** set: `-ngl` (the build has no GPU backend), `--cache-type-k/-v` (KV is small and f16 keeps the DSA indexer path exact), `--flash-attn` (auto). The NVIDIA-only workarounds discussed on #27754 (`NVIDIA_TF32_OVERRIDE=0`, `-fa off`) don't apply on CPU.

**What to expect.** Decode speed is set almost entirely by memory bandwidth: a dual-socket server with 8–12 DDR4/DDR5 channels lands in the high single digits to low teens tok/s before MTP; a desktop with two or four channels is 2–4 tok/s. The `--reasoning-budget` cap and `low` effort are what keep that usable for agentic loops.

### `qwen3.5-4b` — a small CPU model for route-policy

`qwen3.5-4b` serves Qwen3.5-4B on the CPU for one job: route-policy's `llm_classifier` evaluator ([ROUTE_POLICY.md](../route-policy/ROUTE_POLICY.md)), which asks, per `auto` conversation, whether the latest request is about code, needs reasoning, or carries personal data. Route-policy never calls this server itself. It renders the conversation (`common.conversation.render_transcript`) and submits it to the document classifier's `POST /assess` as one `text` document with one `llm` presence criterion per route and `model: qwen3.5-4b`; the classifier's registry entry ([`ai/classifier/models.yaml`](../classifier/models.yaml), [API.md § Models](../classifier/API.md#models)) is what calls `http://qwen3.5-4b:8000`. The service is **not in LiteLLM** — no alias, no chain, no fallback — so no chat ever lands on it.

**Why a small CPU model.** The verdict is computed in the background and only counts from the next turn, so latency matters less than not taking a GPU sequence from chat: the GPU models' sequences are spoken for — `qwen3.8-solo`'s 3 serve chat, and of `muse-glimmer`'s 8 the classifier's document jobs take 6 and chat the other 2. Qwen3.5-4B is a hybrid model (24 Gated DeltaNet layers with a fixed-size recurrent state, 8 gated-attention layers), so its KV cache is small and a CPU handles it at seconds per call. Whether it is good enough is what the benchmark in `ai/route-policy/bench/` measures, against `muse-glimmer` and `qwen3.8-solo` (both also registry entries).

**Image and RAM.** Same `build:` block and image as `glm5.3-flash` (`llama-unsloth:${LLAMA_UNSLOTH_CPU_TAG}-cpu`): the Qwen3.5 architecture is in mainline llama.cpp, and the Unsloth `cpu` build is mainline plus extras, already running on the box. One image for both CPU services, so a `LLAMA_UNSLOTH_CPU_TAG` bump rebuilds and recreates both. The Q4_K_M weights are 2.74 GB; with two 16k slots of KV the process stays at a few GB (`docker stats qwen3.5-4b` is the real number). There is **no mlock**: a 2.7 GB file read on every token stays hot in the page cache, and an eviction while idle costs a second or two of re-reading on the next call, not the per-token stalls `glm5.3-flash`'s 148 GB would see — so neither `--load-mode mmap+mlock` nor the memlock ulimit. The repo also ships a vision projector (`mmproj-*.gguf`, ~670 MB) that `-hf` would download and load; `--no-mmproj` skips it, since the classifier entry is text-only.

**Threads and cores.** `-t 16` on cores 0–15 (`LLAMA_QWEN35_4B_THREADS=16`, `LLAMA_QWEN35_4B_EXTRA_ARGS="--cpu-range 0-15"` in `.env.example`). The box is a Xen VM with 32 vCPUs (one thread per core, one NUMA node, AVX2 only — the hypervisor hides AVX-512 / VNNI), split three ways:

| Cores | Who | How |
|---|---|---|
| 0–15 | `qwen3.5-4b` | `--cpu-range 0-15` (`-Cr`) + `-t 16` |
| 16–23 | everything else (classifier, route-policy, Postgres, …) | unpinned |
| 24–31 | the `signals` sidecar (route-policy's PII / topic / injection models, ~100 ms per call) | its compose `cpuset` (`SIGNALS_CPUSET`, [SIGNALS.md § CPU pinning](../signals/SIGNALS.md#cpu-pinning)) |

At `-t 8` unpinned a verdict took ~60–100 s (prompt 25–50 tok/s, decode 5–12 tok/s, measured 2026-10-20), and `glm5.3-flash` — which takes every physical core by default — has been off for months, so doubling the threads on otherwise idle cores is the cheapest speed-up. The two values go together: the compose file still falls back to `-t 8` with no range when `.env` lacks them, because 16 unpinned threads would also land on the sidecar's cores. On a host with fewer than 32 CPUs, lower both (and leave `SIGNALS_CPUSET` empty). Before turning `glm5.3-flash` on, give it a range of its own — on BOTH sides, since a range on one only still lets the other's threads run there. Pick the ranges from `lscpu -e`:

```bash
# .env
LLAMA_QWEN35_4B_THREADS=16
LLAMA_QWEN35_4B_EXTRA_ARGS="--cpu-range 0-15"
LLAMA_GLM53_FLASH_EXTRA_ARGS="--cpu-range 16-23 -t 8"   # only while it runs; it needs cores taken from the others to be fast
```

**Thinking off.** Qwen3.5 thinks by default; its chat template reads `enable_thinking`. A classifier call wants the JSON answer, not minutes of CPU reasoning, so the server default is `--chat-template-kwargs '{"enable_thinking":false}'`, and the registry entry also sends `chat_template_kwargs: {enable_thinking: false}` in its `extra_body` (the classifier's `reasoning.value` is a string, so a boolean switch rides there). A request that sends `enable_thinking: true` still turns it on.

**Sampling.** The classifier sends no sampling of its own — the server applies the model's published sampling, as for every registry model — so these server defaults are what every scoring call gets: the model card's **instruct (non-thinking)** preset, without its presence penalty.

| Flag | Value | Why |
|---|---|---|
| `--temp` | `0.7` | Model card, instruct mode |
| `--top-p` | `0.8` | Model card, instruct mode |
| `--top-k` | `20` | Model card |
| `--min-p` | `0.0` | Model card |
| `--presence-penalty` | `0.0` | The card says `1.5`. A presence penalty lowers every token already in the answer, and a ~100-token JSON answer is mostly repetition by design: the scaffold's quotes, colons and keys, and the digits of `score` again in `confidence`. It guards long free text against loops, which `--n-predict 512` caps here anyway; the card's own precise-coding preset sets it to 0 too. |

If the benchmark shows the same transcript scoring differently run to run, lower `--temp` here rather than adding sampling to the registry entry.

**Context and output.** `-c 32768 --parallel 2` = two 16k slots, one per classifier request in flight. A scoring prompt is the transcript plus ~600 tokens of rubric, and `--n-predict 512` caps the answer. The classifier entry mirrors it — change them together, then `make up llama qwen3.5-4b` and `docker restart classifier`:

| Setting | Where | Value |
|---|---|---|
| Context per slot | llama-server (`-c` / `--parallel`) | 16,384 |
| `--n-predict` | llama-server | 512 |
| `max_tokens.score` | `ai/classifier/models.yaml` → `qwen3.5-4b` | 512 |
| `max_concurrent` | same entry | 2 (= `--parallel`) |
| `text_char_budget` | same entry | 12,000 characters (~3–4k tokens); route-policy's `history.max_chars` must not exceed it |
| `timeout_s` | same entry | 90 |

The classifier sends every criterion of a job with the same transcript first in the prompt (`ai/classifier/llm/prompts.py` orders it for prefix caching), so with `llama-server`'s slot prompt cache the second and third criterion should only process the rubric. Qwen3.5's recurrent layers make that reuse depend on llama.cpp's context checkpoints; check on the box whether follow-up criteria show a short prompt time (`prompt eval time` in the log, or `timings.prompt_n` in a response).

**Verify on the box.**

```bash
make up llama qwen3.5-4b
make logs llama qwen3.5-4b                 # the 2.7 GB download, then "server is listening"
curl -s http://localhost:8028/health       # {"status":"ok"}
curl -s http://localhost:8028/v1/chat/completions -H 'content-type: application/json' -d '{
  "model": "qwen3.5-4b",
  "messages": [{"role": "user", "content": "Is this request about code? \"my python script crashes with KeyError\". Answer as JSON: {\"score\": 1-10, \"reason\": \"...\"}"}],
  "response_format": {"type": "json_object"},
  "chat_template_kwargs": {"enable_thinking": false},
  "max_tokens": 128}' | jq '.choices[0].message, .usage, .timings'
```

`message.content` must be the JSON alone, with no `<think>` block and no `reasoning_content`, in a few dozen completion tokens. Watch `docker stats qwen3.5-4b` during a request for its CPU use. Then the classifier side — `docker restart classifier`, `GET /models` must list `qwen3.5-4b` with `available: true`, and a hand-made job must complete with a score:

```bash
curl -s http://localhost:8005/models | jq '.models[] | select(.id == "qwen3.5-4b") | {available, endpoint}'
curl -s http://localhost:8005/assess -H 'content-type: application/json' -d '{
  "documents": [{"type": "text", "data": "User: my python script crashes with KeyError, can you fix it?", "filename": "transcript.txt"}],
  "model": "qwen3.5-4b",
  "criteria": [{"name": "The latest user request is about writing, running, debugging or changing code.",
                "type": "llm", "options": {"hint": "presence", "ocr": "never"}}]}'
# -> {"job_id": "..."}: poll GET /jobs/<id> until it is done, read the score, then DELETE /jobs/<id>
```

**Quant swap.** `LLAMA_QWEN35_4B_QUANT` is the tag after the colon on `unsloth/Qwen3.5-4B-GGUF`. K-quants only on CPU (the `IQ*` files are slower there, as for `glm5.3-flash`): `Q4_K_M` 2.74 GB _(default)_, `Q5_K_M` 3.14 GB, `Q6_K` 3.53 GB, `Q8_0` 4.48 GB. Edit `.env`, then `make up llama qwen3.5-4b`; the new file downloads into `llama_data` beside the old one. A bigger quant is slower per token, so re-check the entry's `timeout_s` against `classifier_llm_call_seconds{model="qwen3.5-4b"}`.

| Key | Default | Notes |
|---|---|---|
| `PORT_LLAMA_QWEN35_4B` | `8028` | Host port (container 8000), in the PORT REGISTRY. In-network callers use `http://qwen3.5-4b:8000`. |
| `LLAMA_QWEN35_4B_QUANT` | `Q4_K_M` | Quant tag — see above. |
| `LLAMA_QWEN35_4B_THREADS` | `16` (compose fallback `8`) | `-t`; set together with the range below (Threads and cores, above). |
| `LLAMA_QWEN35_4B_EXTRA_ARGS` | `"--cpu-range 0-15"` (compose fallback empty) | Extra llama-server args appended verbatim. Cores 0–15; 24–31 are the `signals` sidecar's. |

### First-run download

All services use llama-server's `-hf` flag (and, for `qwen3.8-flash`, `-hfd` + `-md` for the MTP head) to download GGUFs from HuggingFace on first start. The weights land in the `llama_data` named volume, mounted at `/root/.cache/huggingface` — llama-server keeps a HuggingFace-hub-style cache at `~/.cache/huggingface/hub` (override with `LLAMA_CACHE` or `HF_HUB_CACHE`) — so subsequent starts are instant. Expect the first launch to take a while — UD-IQ1_S is ~176 GB across split GGUF files. Follow progress with:

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

All services share the `llama_data` volume, so downloads for one don't affect the others.

### LiteLLM integration

`glm5.2`, `qwen3.8-flash` and `glm5.3-flash` are pre-registered in [`ai/litellm/litellm_config.yaml`](../litellm/litellm_config.yaml) under `model_list`, and all three have `fallbacks` and `context_window_fallbacks` entries to `claude-sonnet-5`. Container-name DNS works because LiteLLM and every llama-server sit on `ai_shared`, so the `api_base` values (`http://glm5.2:8000/v1`, `http://qwen3.8-flash:8000/v1`, `http://glm5.3-flash:8000/v1`) resolve inside the network.

`qwen3.5-4b` is deliberately **not** registered: its only caller is the document classifier's model registry ([`qwen3.5-4b` — a small CPU model for route-policy](#qwen35-4b--a-small-cpu-model-for-route-policy)), and a LiteLLM alias would put it one `fallbacks` line away from a chat path it was never sized for.

Clients hit LiteLLM the same way they hit any other model:

```bash
curl http://localhost:4001/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "qwen3.8-flash", "messages": [{"role": "user", "content": "Hello"}]}'
```

**Adding a new llama-server service to LiteLLM** — copy the closest existing entry (`glm5.2` for large-context / long-thinking models, `qwen3.8-flash` for Qwen-family thinking-mode defaults, `glm5.3-flash` for a slow CPU backend that needs a long `stream_timeout`) and change `model_name`, `model:`, and `api_base:` to match your service's `-a <alias>` and `container_name`. Register the fallback in `litellm_settings.context_window_fallbacks` in the same edit.

### State files (outside the repo)

| Path | Purpose |
|---|---|
| `llama_data` (named volume) | HuggingFace-cached GGUF weights — 176 GB and up. Persists across container restarts. Delete with `docker volume rm ai-llama_llama_data` (compose project prefix `ai-llama` is set in the Makefile). |

### Non-obvious constraints

- **`ghcr.io/ggml-org/llama.cpp:server-cuda` is a moving tag.** Upstream ships fast and occasionally changes CLI flag names (`--n-cpu-moe` is relatively new — added mid-2026). If you upgrade and see `unrecognized argument`, pin to a specific dated tag from https://github.com/ggml-org/llama.cpp/pkgs/container/llama.cpp.
- **`qwen3.8-flash`, `glm5.3-flash` and `qwen3.5-4b` are pinned, `glm5.2` is not.** The Unsloth prebuilds are pinned by `LLAMA_UNSLOTH_TAG` (`b10796`, CUDA) and `LLAMA_UNSLOTH_CPU_TAG` (`b10840`, CPU — shared by the two CPU services, which run one image) respectively; each is a full llama.cpp (upstream + patches), so every flag the stock image accepts works there too. The three images can drift apart in flag names independently — check the pinned release when a flag rename appears upstream. The two pins are deliberately separate so bumping one never rebuilds the other; a CPU bump rebuilds and recreates both CPU services.
- **`glm5.3-flash` needs an unlimited `memlock` ulimit.** `--load-mode mmap+mlock` pins ~150 GB; Docker's default memlock limit is 64 KB, so the compose service sets `ulimits.memlock` to `-1`. If you copy the block for another CPU model, keep it — without it the log shows `failed to mlock` and the weights silently become evictable.
- **`IQ*` quants are a CPU trap.** They are the smallest files, but the codebook dequant has no fast CPU matmul path — on the CPU-only service a `UD-IQ3_XXS` (120 GB) decodes slower than `UD-Q3_K_XL` (148 GB). Stick to `Q*_K_*` there.
- **`llama_data` must stay mounted at `/root/.cache/huggingface`.** llama-server writes `-hf` / `-hfd` downloads to `~/.cache/huggingface/hub/models--<owner>--<repo>/` (same layout as the `huggingface_hub` Python library). Mounting the volume anywhere else means a full re-download on every `--force-recreate`.
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
| Running `qwen3.8-flash` on the stock `ghcr.io/ggml-org/llama.cpp:server-cuda` image | MTP head cannot load (`exactly one out of metadata, path_model, and file must be defined`) or speculation silently stays off. Keep the `build:` block / `Dockerfile.llama-unsloth`. |
| Passing the MTP head as an `-hfd` tag (`repo:MTP/file.gguf`) | Tag is matched as a quant name → nothing resolves. Use `-hfd repo` + `-md MTP/file.gguf`. |
| Bumping `LLAMA_UNSLOTH_TAG` without `--build` | Compose reuses the old local image (the tag is in the image name, so `up -d --build` is required). |
| Moving the `llama_data` mount off `/root/.cache/huggingface` | Every `--force-recreate` re-downloads all weights. |

### No tests

There is no automated test suite. Verify by hitting `/v1/chat/completions` after `make up llama glm5.2` and confirming a coherent response. `curl http://localhost:8010/health` should return `{"status":"ok"}` once the model has finished loading. For `glm5.3-flash` use port 8020 and expect the first `/health` OK to take a good while after the download — the mlock pass touches every page of the file before the server starts answering. For `qwen3.5-4b` use port 8028 and follow [its own checklist](#qwen35-4b--a-small-cpu-model-for-route-policy): thinking must be off, and the classifier must list it as available.
