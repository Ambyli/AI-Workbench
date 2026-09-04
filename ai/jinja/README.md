# Shared Jinja templates

Chat templates and other Jinja assets consumed by more than one inference backend. Anything in this directory is expected to be bind-mounted read-only into a container by a service defined in a sibling `ai/<service>/docker-compose.*.yml`.

The point of this directory: **one file, one canonical location, N consumers**. When a template needs a patch, edit it here — every service that mounts it picks up the change on its next `--force-recreate`.

## Current files

| File | Consumers | Purpose |
|---|---|---|
| [`qwen_fixed_chat_template.jinja`](qwen_fixed_chat_template.jinja) | vLLM `qwen3.8` ([ai/vllm/docker-compose.vllm.yml](../vllm/docker-compose.vllm.yml)), llama.cpp `qwen3.8-flash` ([ai/llama/docker-compose.llama.yml](../llama/docker-compose.llama.yml)) | Patched Qwen 3.x chat template from [froggeric/Qwen-Fixed-Chat-Templates](https://huggingface.co/froggeric/Qwen-Fixed-Chat-Templates). Fixes duplicate `<think>` blocks, `enable_thinking=false` crash, runaway `reasoning_effort` default, and multi-turn KV-cache invalidation. See [`ai/vllm/VLLM.md § Custom chat template — qwen3.8`](../vllm/VLLM.md#custom-chat-template--qwen38) for the full list of bugs it fixes. |

## Adding a template

1. Drop the file into `ai/jinja/`.
2. Bind-mount it read-only into each consuming service, using a path relative to the compose file:
   - From `ai/vllm/docker-compose.vllm.yml`: `../jinja/<file>:/config/<file>:ro`
   - From `ai/llama/docker-compose.llama.yml`: `../jinja/<file>:/config/<file>:ro`
3. Add a row to the **Current files** table above with the consumer list, so the next person editing the file knows what will break if they change it.

## Maintenance rule

If you delete or rename a file here, `grep -rn '<filename>' ai/` and update every consumer's compose file, docs, and Postman collection in the same change. The compose bind-mounts fail loudly at startup (file-not-found) when a mount source is missing, but downstream docs and collections drift silently.
