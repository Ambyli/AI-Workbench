# ai/llama

llama.cpp `llama-server` instances, one per service: `glm5.2` (GPU + RAM expert offload, stock `server-cuda` image), `qwen3.8-flash` (GPU, Unsloth CUDA build for MTP), `glm5.3-flash` (CPU + RAM only, Unsloth CPU build, off today) and `qwen3.5-4b` (CPU, route-policy's classifier model). Guide: [`LLAMA.md`](LLAMA.md). Compose: [`docker-compose.llama.yml`](docker-compose.llama.yml). All recreate-class (model load, host ports). No tests — verify per `LLAMA.md § No tests`.

## Rules

- **Two Unsloth pins, deliberately separate.** `LLAMA_UNSLOTH_TAG` (CUDA, `qwen3.8-flash`) and `LLAMA_UNSLOTH_CPU_TAG` (CPU image shared by `glm5.3-flash` and `qwen3.5-4b`) are part of the local image names, so a bump needs `--build` (`make build llama` / `up -d --build --force-recreate <svc>`); a CPU bump rebuilds both CPU services. Mainline llama.cpp lacks MTP for `qwen4exp` and the `glm5next` arch — never move those services to the stock image.
- **`LLAMA_UNSLOTH_VARIANT` and `LLAMA_CUDA_RUNTIME_IMAGE` must agree on the CUDA major**; the Dockerfile fails the build via `ldd` if they don't.
- **K-quants only on CPU.** `IQ*` quants have no fast CPU matmul path and run ~2x slower than a K-quant of the same size.
- **`glm5.3-flash` mlocks the whole GGUF** (`--load-mode mmap+mlock`, `ulimits.memlock: -1` — keep it if you copy the block); `UD-Q3_K_XL` 148 GB + ~10–15 GB on a ~200 GB box. It does not coexist with `glm5.2` (whose expert offload takes another ~70 GB). Its `reasoning_effort` accepts only `low` / `high`; anything else becomes `max`.
- **`glm5.3-flash` takes every core by default** — give it a `--cpu-range` outside 0–15 and 24–31 (`LLAMA_GLM53_FLASH_EXTRA_ARGS`) before turning it on.
- **`qwen3.5-4b` is not in LiteLLM** — no alias, chain or fallback; only the classifier registry (`ai/classifier/models.yaml` entry `qwen3.5-4b`) calls it, for route-policy's `llm_classifier`. Never add a LiteLLM alias (it would be one `fallbacks` line from chat). Thinking off by server default (`enable_thinking: false`); no mlock; `--no-mmproj`; sampling = the card's instruct preset without its presence penalty.
- `glm5.3-flash` `--n-predict`, LiteLLM's `max_tokens` / `max_output_tokens` and `max_input_tokens` change together (recreate both); see `LLAMA.md § glm5.3-flash`.
- `qwen3.8-flash` shares `ai/jinja/qwen_fixed_chat_template.jinja` with vLLM `qwen3.8` — patch the template once, recreate both.
- Editing a `command:` block needs a recreate (`make up llama <svc>`); `llama_data` must stay mounted at `/root/.cache/huggingface`. A new service needs a `PORT_*` in the PORT REGISTRY and a line in the Makefile's `llama` stack.

## Cross-folder rules

Also in the root `CLAUDE.md` § Cross-folder rules — read the named entry there for the full picture.

- **`qwen3.5-4b` sizing** — `-c 32768 --parallel 2 --n-predict 512` here = the classifier entry's `max_concurrent: 2` and `max_tokens.score: 512`; route-policy's `history.max_chars` ≤ the entry's `text_char_budget` (12000). Change together, then `make up llama qwen3.5-4b` and `docker restart classifier`. (root `CLAUDE.md` § Cross-folder rules)
- **CPU core split** — `qwen3.5-4b` on 0–15 (`LLAMA_QWEN35_4B_THREADS=16` + `LLAMA_QWEN35_4B_EXTRA_ARGS="--cpu-range 0-15"`, set together), `signals` on 24–31, the rest 16–23; a range on one side only still lets the other's threads onto those cores. (root `CLAUDE.md` § Cross-folder rules)
- **GPU sharing and driver floor** — `cuda13-portable` needs an R580+ host driver (the detector's torch wheel shares that floor); `cuda12-portable` + a `12.x` runtime image is the R525+ path. (root `CLAUDE.md` § Cross-folder rules)

## Env vars

One line each; full detail in `.env.example` and `LLAMA.md` (§ `qwen3.8-flash` MTP speculative decoding, § `glm5.3-flash` — GLM-5.3-Flash on CPU and RAM only, § `qwen3.5-4b` — a small CPU model for route-policy).

- `LLAMA_UNSLOTH_TAG` (`b10796-mix-659e406`) — Unsloth release for `qwen3.8-flash`'s CUDA image; in the image name, bump with `--build`.
- `LLAMA_UNSLOTH_VARIANT` (`cuda13-portable`) — tarball flavour; `cuda13` needs R580+, `cuda12-portable` R525+; must match the runtime image's CUDA major.
- `LLAMA_CUDA_RUNTIME_IMAGE` (`nvidia/cuda:13.3.1-runtime-ubuntu22.04`) — base image providing `libcudart` / `libcublas` of that major.
- `LLAMA_UNSLOTH_CPU_TAG` (`b10840-mix-d5c17a0`) — Unsloth CPU release for `glm5.3-flash` AND `qwen3.5-4b`; bump rebuilds both.
- `LLAMA_GLM53_FLASH_QUANT` (`UD-Q3_K_XL`) — mlocked in RAM: Q2 109 GB, Q3 148 GB (fits 200 GB), Q4 200 GB (needs 256 GB+); K-quants only.
- `LLAMA_GLM53_FLASH_REASONING_EFFORT` (`low`) — `low` | `high`, else `max`; `--reasoning-budget 8192` caps it.
- `LLAMA_GLM53_FLASH_EXTRA_ARGS` (empty) — appended verbatim (`--numa distribute -t 48`, `--cpu-range …`, `--flash-attn off`).
- `PORT_LLAMA_QWEN35_4B` (`8028`) — `qwen3.5-4b` host port; in-network callers use `http://qwen3.5-4b:8000`.
- `LLAMA_QWEN35_4B_QUANT` (`Q4_K_M`) — tag on `unsloth/Qwen3.5-4B-GGUF`; K-quants only; a bigger quant is slower, re-check the classifier entry's `timeout_s`.
- `LLAMA_QWEN35_4B_THREADS` (`16` in `.env.example`; compose fallback `8` on purpose) — `-t`; only with the cpu range below.
- `LLAMA_QWEN35_4B_EXTRA_ARGS` (`"--cpu-range 0-15"`; compose fallback empty) — appended verbatim; quoted because of the space.
