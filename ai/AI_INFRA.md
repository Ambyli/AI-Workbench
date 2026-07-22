# Docker Infrastructure

Each product is packaged as its own `docker-compose.*.yml` under `ai/` and can be started independently. Every container is attached to a shared external Docker network (`ai_shared`) so services resolve each other by container name (e.g. `http://litellm:4000`, `http://vllm-qwen-vl:8000`).

## Shared Docker network

Create it once before starting any compose file:

```bash
docker network create ai_shared
```

Or via make:

```bash
make network
```

The `make setup` target creates the network automatically.

## Compose files

Every service in the list below is on the `ai_shared` network unless noted. Ports shown are the **host** ports the container publishes (sourced from `.env` — the values in the table are the documented defaults).

| Compose file | Service(s) | Host port | README |
|---|---|---|---|
| [`docker-compose.yml`](docker-compose.yml) | _(none — just declares the external `ai_shared` network)_ | — | — |
| [`docker-compose.litellm.yml`](docker-compose.litellm.yml) | `litellm`, `litellm_db`, `prometheus` | `4001`, `5432`, `9090` | [LITELLM.md](LITELLM.md) · [LITELLM_MCP.md](LITELLM_MCP.md) |
| [`docker-compose.openwebui.yml`](docker-compose.openwebui.yml) | `openwebui` | `8007` | [OPENWEBUI.md](OPENWEBUI.md) |
| [`docker-compose.oauth2-proxy.yml`](docker-compose.oauth2-proxy.yml) | `oauth2-proxy` | `4180` | [OAUTH2_PROXY.md](OAUTH2_PROXY.md) |
| [`docker-compose.cloudflared.yml`](docker-compose.cloudflared.yml) | `cloudflared` | _(outbound tunnel — no publish)_ | see [OPENWEBUI.md § Cloudflare Tunnel](OPENWEBUI.md#public-hostname-via-cloudflare-tunnel) |
| [`docker-compose.vllm.yml`](docker-compose.vllm.yml) | `vllm-qwen`, `vllm-qwen-vl` | `8002`, `8006` | [VLLM.md](VLLM.md) · [GPU_SHARING_GUIDE.md](GPU_SHARING_GUIDE.md) |
| [`docker-compose.kokoro.yml`](docker-compose.kokoro.yml) | `kokoro-api`, `kokoro-app` (internal) | `8004` | [KOKORO.md](KOKORO.md) |
| [`docker-compose.madlad.yml`](docker-compose.madlad.yml) | `madlad-api`, `madlad-app` (internal) | `8008` | [MADLAD.md](MADLAD.md) |
| [`docker-compose.classifier.yml`](docker-compose.classifier.yml) | `classifier` | `8005` | [classifier/API.md](classifier/API.md) |
| [`docker-compose.unsloth.yml`](docker-compose.unsloth.yml) | `unsloth` | `8000` (model — LiteLLM upstream), `8888` (Jupyter), `22` (SSH) | [UNSLOTH.md](UNSLOTH.md) |
| [`docker-compose.roofix.yml`](docker-compose.roofix.yml) | `roofix-bridge`, `roofix-scraper` | _(internal only)_ | [ROOFIX.md](ROOFIX.md) |

## Flow diagram

Solid arrows are runtime request paths; dotted arrows are auxiliary (metrics scraping, model-weight downloads, OAuth callbacks). Node → README links live in the [Compose files](#compose-files) table above.

```mermaid
flowchart TB
    classDef ext fill:#f5f5f5,stroke:#999,color:#333
    classDef svc fill:#e8f0fe,stroke:#4a86e8,color:#1a1a1a
    classDef store fill:#fff4d6,stroke:#e8a33d,color:#1a1a1a
    classDef standalone fill:#f3e8fd,stroke:#8e63ce,color:#1a1a1a

    Browser["Browser<br/>chat.zeoenergy.com"]:::ext
    Google["Google OAuth<br/>+ Directory API"]:::ext
    HF["HuggingFace Hub<br/>model weights"]:::ext
    CC["Claude Code / API clients<br/>localhost:4001"]:::ext
    Gmail["Gmail<br/>(roofix@zeoenergy.com)"]:::ext
    GmailMCP["gmail-mcp<br/>(external)"]:::ext
    PhoenixMCP["phoenix-mcp.com<br/>(external MCP)"]:::ext
    Roofix["roofix.io<br/>(Bubble app)"]:::ext

    subgraph CFG["docker-compose.cloudflared.yml"]
        CF["cloudflared<br/>tunnel"]:::svc
    end
    subgraph O2PG["docker-compose.oauth2-proxy.yml"]
        O2P["oauth2-proxy<br/>:4180"]:::svc
    end
    subgraph OWUG["docker-compose.openwebui.yml"]
        OWU["openwebui<br/>:8007"]:::svc
    end
    subgraph LLG["docker-compose.litellm.yml"]
        LL["litellm<br/>:4001"]:::svc
        DB[("litellm_db<br/>postgres :5432")]:::store
        PROM["prometheus<br/>:9090"]:::svc
    end
    subgraph VG["docker-compose.vllm.yml"]
        VQ["vllm-qwen<br/>:8002<br/>Qwen3.6-35B-A3B"]:::svc
        VQVL["vllm-qwen-vl<br/>:8006<br/>Qwen2.5-VL-7B"]:::svc
    end
    subgraph KG["docker-compose.kokoro.yml"]
        KAPI["kokoro-api<br/>:8004"]:::svc
        KAPP["kokoro-app<br/>internal"]:::svc
    end
    subgraph MG["docker-compose.madlad.yml"]
        MAPI["madlad-api<br/>:8008"]:::svc
        MAPP["madlad-app<br/>internal"]:::svc
    end
    subgraph CLG["docker-compose.classifier.yml"]
        CLS["classifier<br/>:8005"]:::svc
        CLSDB[("classifier.db<br/>sqlite (job store)")]:::store
    end
    subgraph UG["docker-compose.unsloth.yml"]
        UN["unsloth<br/>model :8000 (llama.cpp)<br/>Jupyter :8888 / SSH :22"]:::svc
    end
    subgraph RXG["docker-compose.roofix.yml"]
        RB["roofix-bridge<br/>internal :8080"]:::svc
        RS["roofix-scraper<br/>internal :8080"]:::svc
    end

    Browser --> CF --> O2P --> OWU
    O2P -. OAuth + group check .-> Google
    OWU  ==>|OpenAI API<br/>via ai_shared| LL
    CC   ==>|OpenAI API| LL

    LL ==>|"model pass-through<br/>qwen3.6-unsloth"| UN
    LL ==> VQ
    LL ==> VQVL
    LL ==>|"/v1/audio/speech"| KAPI
    LL ==>|"/v1/madlad/* + MCP tool"| MAPI
    LL ==>|"/v1/classifier/*"| CLS
    LL --> DB
    PROM -. scrape .-> LL

    KAPI --> KAPP
    MAPI --> MAPP
    CLS  -->|VLLM_QWEN_VL_API| VQVL
    CLS  --> CLSDB

    RB   ==>|OpenAI SDK<br/>brain fallback| LL
    RB   -->|"HTTP<br/>/proposal/{id}"| RS
    RB   -->|"MCP JSON-RPC"| GmailMCP
    RB   -->|"MCP JSON-RPC"| PhoenixMCP
    LL   -.->|MCP registration| GmailMCP
    LL   -.->|MCP registration| PhoenixMCP
    GmailMCP -. IMAP/API .-> Gmail
    RS   -. Playwright .-> Roofix

    KAPP -. model download .-> HF
    MAPP -. model download .-> HF
    VQ   -. model download .-> HF
    VQVL -. model download .-> HF
```

### Reading the diagram

- **Public entry point** — only `cloudflared` receives inbound traffic from outside the LAN. Every request to `chat.zeoenergy.com` transits `cloudflared → oauth2-proxy → openwebui`.
- **Fan-out from LiteLLM** — LiteLLM is the single OpenAI-compatible surface. Chat models are served by vLLM and Unsloth (llama.cpp); TTS by Kokoro; translation by MADLAD; image-quality by the classifier. Open WebUI and any external Claude Code / API client both hit LiteLLM the same way.
- **Two-container app/api pattern** — Kokoro and MADLAD each split into an internal `-app` (model on GPU, blocking) and a `-api` proxy (stateless, non-blocking). Only the `-api` half is published to the host.
- **Classifier ↔ vLLM** — the classifier is a vLLM client, not a peer; it calls `vllm-qwen-vl` internally for LLM scoring. Its own SQLite job store (`classifier.db` on the `classifier_data` volume) persists async `/assess` job state so callers can poll `GET /jobs/{id}` across restarts.
- **Unsloth dual role** — the CUDA-compiled llama.cpp binary serves a chat model at `unsloth:8000` (routed via LiteLLM as the `qwen3.6-unsloth` model entry sourced from `DEFAULT_LITELLM_MODEL_API_BASE`), while Jupyter (`:8888`) and SSH (`:22`) remain available for training / fine-tuning workflows.
- **Roofix bridge + scraper** — bundled in `docker-compose.roofix.yml`. The bridge is an internal worker that does NOT receive inbound traffic. Its APScheduler ticks every `TICK_INTERVAL_SECONDS` (default 300s); each tick fetches unread Roofix mail via the Gmail MCP, decides per-event (rules first, LiteLLM fallback), and writes back via the Phoenix MCP. Ambiguous email events trigger a proposal fetch against the sibling `roofix-scraper` (Playwright), which owns Roofix session cookies. Both containers expose `:8080` internally only — no host port unless explicitly published.
- **Gmail MCP is a passthrough, not a proxied identity** — the `LL -.-> GmailMCP` edge uses LiteLLM's `delegate_auth_to_upstream: true` mode. LiteLLM only advertises the endpoint; the OAuth 2.1 flow runs end-to-end between Open WebUI and `gmailmcp.googleapis.com` per user, and LiteLLM forwards the resulting `Authorization: Bearer` header untouched. Users must enable the Gmail tool per-chat (it cannot be a default-enabled tool on a model, because the OAuth browser redirect cannot happen mid-completion).

## Ports at a glance

Ports are sourced from `.env` (`PORT_*` variables). Defaults shown; change them in `.env` if any conflict on the host.

| Service | Host port |
|---|---|
| oauth2-proxy | `4180` |
| litellm | `4001` |
| litellm_db (postgres) | `5432` |
| prometheus | `9090` |
| openwebui | `8007` |
| vllm-qwen | `8002` |
| vllm-qwen-vl | `8006` |
| kokoro-api | `8004` |
| madlad-api | `8008` |
| classifier | `8005` |
| unsloth (Jupyter / model / SSH) | `8888` / `8000` / `22` |
