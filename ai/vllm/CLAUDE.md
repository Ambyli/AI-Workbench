# ai/vllm

vLLM model servers on the box's three RTX A6000s: `qwen3.8-solo` (GPU 2, chain order 1) and `muse-glimmer` (TP=2 on GPUs 0–1, DFlash, vision) carry chat and the classifier; `qwen3.8` (TP=2, mutually exclusive with `muse-glimmer`), `qwen3.6`, `vllm-qwen-vl` are extras. Guide: [`VLLM.md`](VLLM.md). Compose: [`docker-compose.vllm.yml`](docker-compose.vllm.yml). All recreate-class (GPU, host ports).

## Rules

- **Pin explicit release tags, never `latest`.** `docker compose up -d` never re-pulls a cached `latest`; a stale one boots and dies (`invalid tool call parser: muse_glimmer`). `qwen3.8` / `qwen3.8-solo` sit on `v0.27.1`, the rest on `v0.29.0`; bump one service at a time (`VLLM.md § Muse Glimmer 30B — tensor parallel + DFlash speculative decoding`). Verify a flag against the running image's `--help` before suggesting it.
- **`muse-glimmer`**: `--reasoning-parser` and `--tool-call-parser muse_glimmer` run together; `num_speculative_tokens` is fixed at 15 (drafter block 16); `--max-model-len 262144` needs `VLLM_ALLOW_LONG_MAX_MODEL_LEN=1`. Reasoning depth is the `reasoning_strength` template variable — a "reasoning strength" line in a system prompt overrides it, so never add one. If startup OOMs, drop `--gpu-memory-utilization` to 0.85 before touching `--max-model-len` / `--max-num-seqs`.
- `--max-num-seqs` is a ceiling, not a reservation; the KV pool decides what runs.
- `qwen3.8` and `qwen3.8-solo` stay separate LiteLLM aliases (one `model_name` would round-robin onto the one that is down).
- `qwen3.8` (and `qwen3.8-flash` in `ai/llama`) mount `ai/jinja/qwen_fixed_chat_template.jinja` — patch it once, recreate both.

## Cross-folder rules

Also in the root `CLAUDE.md` § Cross-folder rules — read the named entry there for the full picture.

- **Local vLLM slots** — `muse-glimmer` `--max-num-seqs 8`: the classifier takes at most 6 (`max_concurrent` in `ai/classifier/models.yaml`), 2 stay for chat, or the overflow hook spills chat to Claude. `qwen3.8-solo` `--max-num-seqs 3` (classifier entry `max_concurrent: 1`). The edge's per-key limit (8) is sized from 3 + 2. Running `qwen3.8` instead of `muse-glimmer` makes the overflow hook mark muse down. (root `CLAUDE.md` § Cross-folder rules)
- **Context windows** — LiteLLM `model_info.max_input_tokens` = `--max-model-len` − the deployment's `max_tokens` (`qwen3.8-solo` 131072 → 114688; `muse-glimmer` 262144 → 245760, 229376 on `local-code`). Change `ai/litellm/litellm_config.yaml` with any `--max-model-len` edit. A tag bump on a chain backend must keep `POST /tokenize` (root, outside `/v1`) working — the overflow hook's context fit uses it. (root `CLAUDE.md` § Cross-folder rules)
- **GPU sharing and driver floor** — GPU 2 is shared by `qwen3.8-solo` (`--gpu-memory-utilization 0.90`) and the detector (`ai/detector`); don't grow `qwen3.8-solo`'s memory without checking the detector fits. (root `CLAUDE.md` § Cross-folder rules)
- **Image limit per prompt** — `muse-glimmer` `'{"image": 3}'` must equal its `ai/classifier/models.yaml` entry's `max_images_per_prompt: 3` (a reference call sends up to 3 images; `2` = one example per call, `1` = references refused on this model). Change both, `docker restart classifier`. (root `CLAUDE.md` § Cross-folder rules; `VLLM.md § Muse Glimmer 30B — tensor parallel + DFlash speculative decoding`, *Multimodal*)
