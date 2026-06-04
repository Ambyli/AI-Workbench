<p align="center">
  <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 160" width="400" height="160">
    <!-- Background -->
    <rect width="400" height="160" rx="16" fill="#1a1a2e"/>
    <!-- Circuit lines -->
    <path d="M60 80 L120 80 L140 50 L200 50" stroke="#4a4e8a" stroke-width="2" fill="none"/>
    <path d="M60 80 L120 80 L140 110 L200 110" stroke="#4a4e8a" stroke-width="2" fill="none"/>
    <path d="M260 50 L320 50 L340 80 L340 80" stroke="#4a4e8a" stroke-width="2" fill="none"/>
    <path d="M260 110 L320 110 L340 80 L340 80" stroke="#4a4e8a" stroke-width="2" fill="none"/>
    <!-- Left node — monitoring -->
    <circle cx="60" cy="80" r="18" fill="#16213e" stroke="#7b68ee" stroke-width="2"/>
    <text x="60" y="86" text-anchor="middle" fill="#7b68ee" font-size="18" font-family="system-ui">◉</text>
    <!-- Middle-left node — docker -->
    <circle cx="140" cy="50" r="14" fill="#16213e" stroke="#00b4d8" stroke-width="2"/>
    <text x="140" y="55" text-anchor="middle" fill="#00b4d8" font-size="14" font-family="system-ui">⬡</text>
    <circle cx="140" cy="110" r="14" fill="#16213e" stroke="#00b4d8" stroke-width="2"/>
    <text x="140" y="115" text-anchor="middle" fill="#00b4d8" font-size="14" font-family="system-ui">⬡</text>
    <!-- Center node — brain/AI -->
    <circle cx="200" cy="80" r="28" fill="#16213e" stroke="#e06c75" stroke-width="2"/>
    <text x="200" y="88" text-anchor="middle" fill="#e06c75" font-size="24" font-family="system-ui">🧠</text>
    <!-- Middle-right node — serving -->
    <circle cx="260" cy="50" r="14" fill="#16213e" stroke="#50fa7b" stroke-width="2"/>
    <text x="260" y="55" text-anchor="middle" fill="#50fa7b" font-size="14" font-family="system-ui">⚙</text>
    <circle cx="260" cy="110" r="14" fill="#16213e" stroke="#50fa7b" stroke-width="2"/>
    <text x="260" y="115" text-anchor="middle" fill="#50fa7b" font-size="14" font-family="system-ui">⚙</text>
    <!-- Right node — output -->
    <circle cx="340" cy="80" r="18" fill="#16213e" stroke="#f1fa8c" stroke-width="2"/>
    <text x="340" y="86" text-anchor="middle" fill="#f1fa8c" font-size="18" font-family="system-ui">→</text>
    <!-- Title -->
    <text x="200" y="148" text-anchor="middle" fill="#cdd6f4" font-size="16" font-weight="bold" font-family="system-ui">AI Workbench</text>
  </svg>
</p>

# AI Workbench

A local AI development workbench — real-time token usage monitoring for claude-code, multi-model serving, and MCP tool calling, all on one machine.

## Components

| Component | Description | Docs |
|---|---|---|
| **Usage Widget** | Python tray app — daily/weekly token totals, per-project breakdown, rolling averages, claude.ai account stats via CDP, local LLM toggle | [widget/USAGE_WIDGET.md](widget/USAGE_WIDGET.md) |
| **AI Infrastructure** | Main compose (LiteLLM + Unsloth + vLLM), multi-model serving, GPU configuration | [ai/AI_INFRA.md](ai/AI_INFRA.md) |

## Configuration

All settings live in `config.json` (project root) and `.env`. The widget reads `config.json` at startup and applies changes immediately via the Settings window (tray right-click → **Settings…**).

See the [Usage Widget docs](widget/USAGE_WIDGET.md#configuration) for the full key reference.

## Quick start

```bash
make setup
```

## Docker Compose Commands

Run `make help` to list all available targets, including any service stacks currently registered in the Makefile.

### Network

```bash
make network         # Create shared Docker network (ai_shared)
```

All containers share a `ai_shared` Docker network so they can resolve each other by container name. `make setup` creates it automatically.

### Main stack

```bash
make up              # Start all services
make down            # Stop all services
make clean           # Stop and remove containers + volumes
make very-clean      # Stop, remove containers, volumes, and images
make logs            # Follow logs
make build           # Build images
```

## Adding or Removing a Service

The Makefile uses a macro to generate `up-*`, `down-*`, `clean-*`, `very-clean-*`, `logs-*`, and `build-*` targets for each service stack. Adding or removing a service is a two-step change.

### Add a service

1. Create `ai/docker-compose.<name>.yml` with the service definition.
2. Add one line to the `Makefile` with the stack name and the space-separated list of Docker Compose service names to target:

```makefile
$(eval $(call service,<name>,<service1> <service2> ...))
```

Example — adding a Whisper stack that runs two containers:

```makefile
$(eval $(call service,whisper,whisper-api whisper-worker))
```

This immediately makes `make up-whisper`, `make down-whisper`, `make clean-whisper`, `make very-clean-whisper`, `make logs-whisper`, and `make build-whisper` available. The compose file must be named `ai/docker-compose.whisper.yml`.

### Remove a service

Delete the corresponding `$(eval $(call service,...))` line from the `Makefile`. That's it — all five generated targets disappear with it.

### Naming rules

| Constraint | Detail |
|---|---|
| Stack name | Must match the suffix of the compose filename: `service,foo,...` → `ai/docker-compose.foo.yml` |
| Service list | Space-separated Docker Compose service names (second argument). These are passed directly to `docker compose up/stop/rm/build`. |
| Multiple services | All listed services are started/stopped together as a group (e.g. `vllm-qwen vllm-llama`). |

## GPU Compatibility

All inference services (Unsloth, vLLM, Kokoro) require a CUDA-capable NVIDIA GPU. The full list of supported GPUs is at https://developer.nvidia.com/cuda/gpus. The Unsloth build defaults to compute capability `89` (Ada Lovelace — RTX 4080/4090); adjust `Dockerfile.unsloth` for other architectures.

## Environment Variables

Copy `.env.example` to `.env` and fill in the values.

| Variable | Default | Description |
|---|---|---|
| `DEBUG_LOGGING` | `false` | Write DEBUG-level logs to the log file |
| `INCLUDE_PATHS` | _(empty)_ | Comma-separated path prefixes to filter project sessions. Leave blank to include all |
| `EXCLUDE_WEEKDAYS` | `5,6` | Days excluded from rolling averages (0=Monday, 6=Sunday) |
| `CONSOLE_FETCHER_ENABLED` | `false` | Scrape console.anthropic.com for account-level usage |
| `CONSOLE_REFRESH_MINUTES` | `30` | Minutes between console scraping refreshes |
| `CONSOLE_HEADLESS` | `true` | Run Chrome headless for scraping (`false` keeps window visible) |
| `CHROME_PATHS_VAR` | _(empty)_ | Alternative Chrome executable path. Leave empty to use defaults |
| `LLM_LOG_MAX_LINES` | `200` | Max lines kept in the LLM Backend server log |
| `LLM_URL` | `http://localhost:8001` | Base URL for the local LLM server |
| `LLM_API_KEY` | _(empty)_ | API key sent to the local LLM server |
| `LLM_MODEL` | _(empty)_ | Model alias passed to Claude Code |
| `LLAMA_SERVER_CMD` | _(empty)_ | Full shell command to launch llama-server |
| `BROWSER_DEBUG_PORT` | `9222` | Chrome remote-debugging port for CDP |
| `KEEP_LLM_ACTIVE` | `true` | Keep the local LLM server running when switching away |
| `HF_TOKEN` | _(empty)_ | HuggingFace token for gated model downloads (vLLM, LiteLLM) |
| `DEFAULT_LITELLM_MASTER_KEY` | _(empty)_ | Master key for the LiteLLM proxy |
| `DEFAULT_LITELLM_MODEL_NAME` | _(empty)_ | Model alias used in LiteLLM requests |
| `DEFAULT_LITELLM_MODEL` | _(empty)_ | Full model spec (e.g. `openai/unsloth/Qwen3.6-35B-A3B-GGUF`) |
| `DEFAULT_LITELLM_MODEL_API_KEY` | _(empty)_ | API key forwarded to the upstream LLM |
| `DEFAULT_LITELLM_MODEL_API_BASE` | _(empty)_ | Upstream API base URL (must include `/v1` for OpenAI-compatible endpoints) |
| `DEFAULT_LITELLM_DATABASE_URL` | _(empty)_ | PostgreSQL connection string for LiteLLM |
| `DEFAULT_LITELLM_MCP_PHOENIX_URL` | _(empty)_ | URL for the Phoenix MCP server |
| `DEFAULT_LITELLM_MCP_PHOENIX_AUTH_VALUE` | _(empty)_ | Bearer token for Phoenix MCP authentication |
| `KOKORO_APP_URL` | `http://kokoro-app:8085` | URL the Kokoro API proxy uses to reach the inference container. Set to `http://localhost:8080` for local dev |