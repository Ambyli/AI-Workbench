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

### Main stack

```bash
make up              # Start all services
make down            # Stop all services
make clean           # Stop and remove containers + volumes
make very-clean      # Stop, remove containers, volumes, and images
make logs            # Follow logs
make build           # Build images
```

### LiteLLM

```bash
make up-litellm      # Start LiteLLM proxy
make down-litellm    # Stop LiteLLM
make clean-litellm   # Stop and remove LiteLLM container
make logs-litellm    # Follow LiteLLM logs
make build-litellm   # Build LiteLLM image
```

### Unsloth

```bash
make up-unsloth      # Start Unsloth
make down-unsloth    # Stop Unsloth
make clean-unsloth   # Stop and remove Unsloth container
make logs-unsloth    # Follow Unsloth logs
make build-unsloth   # Build Unsloth image
```

### vLLM

```bash
make up-vllm         # Start vLLM (Qwen + Llama)
make down-vllm       # Stop vLLM
make clean-vllm      # Stop and remove vLLM containers
make logs-vllm       # Follow vLLM logs
make build-vllm      # Build vLLM images
```
