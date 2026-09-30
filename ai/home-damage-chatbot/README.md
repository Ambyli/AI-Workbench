# Zeo Energy — Service Chatbot (vLLM & Local LLM Edition)

A secure, high-concurrency conversational service request platform for Zeo Energy. A deterministic state machine drives the customer intake; an LLM connected via **vLLM** (OpenAI-compatible) or Ollama (defaulting to **Qwen 3.8 27B** / `qwen3.8:27b`) extracts structured fields and validates user responsiveness. The staff **handoff email is template-rendered and auto-escaped, never free-written by the LLM**.

Includes a complete Docker container service with lifecycle management scripts and a comprehensive security & model test suite.

---

## Key Capabilities & Architecture

```
frontend/ (vanilla JS, zero-build)  ->  POST /api/chat
backend/ (FastAPI + Docker)
  config.py         central settings/secrets from env/.env (no hard-coded secrets)
  state_machine.py  owns the intake flow + security boundary (slots, branching, limits)
  validation.py     answer-validation gate — "was this actually answered?"
  safety.py         deterministic 911 classifier (never an LLM judgment)
  llm.py            vLLM OpenAI-compatible & Ollama structured schema extraction | mock
  lookup.py         mock DB, fuzzy confidence scoring, minimal return
  crm.py            CRM adapter (stub | mcp | inhouse) — match + open case
  mailer.py         email adapter (Google Workspace / Gmail API) — send / route
  pipeline.py       disposition: match -> open case | route to review -> send
  email_render.py   Jinja2 (autoescaped) template -> handoff email + store
  ratelimit.py      per-IP fixed-window rate limiter
  queue_manager.py  active user concurrency limiter & FIFO waiting queue
  upload.py         secure image uploads with signature checks & EXIF stripping
```

### 1. User Concurrency Limiting & FIFO Queue
- **Active Capacity Control**: Limits concurrent active chat users (`MAX_ACTIVE_USERS`) to prevent system/LLM server congestion.
- **Real-Time FIFO Queue**: When capacity is reached, surplus users enter a waiting queue with live position tracking (`Position #1 in line...`) and automatic promotion when active slots open up or idle sessions time out (`ACTIVE_SESSION_TIMEOUT_SECONDS`).
- **Endpoints**: `/api/queue/status` (status and heartbeats) and `/api/queue/stats` (concurrency metrics).

### 2. vLLM & Qwen 3.8 27B Support
- **vLLM Integration**: Native client connecting to OpenAI-compatible `/v1/chat/completions` with JSON schema structured output decoding.
- **Model**: Defaulting to `qwen3.8:27b` / `Qwen/Qwen2.5-27B-Instruct`, with temperature `0.0`, token budgeting, and anti-hallucination XML containment.
- **Fallback Resilience**: Automatic fallback to deterministic extraction if the remote LLM server is unreachable or experiences an error.

### 3. Complete Docker Containerization
- **Production Dockerfile**: Lightweight multi-stage Python container with non-root security (`appuser`), healthcheck endpoint verification (`/api/health`), and volume persistence.
- **Docker Compose**: Orchestrates `chatbot` service and optional `vllm` GPU server profile.
- **Lifecycle Control**: Turn the service on/off easily with provided scripts.

### 4. Comprehensive Security Hardening
- **Prompt Injection Defense**: Untrusted customer inputs are sanitized and wrapped in XML `<customer_message>` tags; prompt jailbreaks and delimiter breakouts are stripped.
- **Deterministic 911 Safety Check**: Every message is evaluated by an instant safety classifier (gas leaks, sparking wires, smoke, fire) without delegating to an LLM.
- **File Upload Protection**: Header magic byte (signature) verification, 5MB ceiling, UUID filename sanitization, and EXIF/GPS metadata stripping via Pillow.
- **Edge Security**: Per-IP rate limiting (HTTP 429), CORS origin allow-list, secure HTTP headers (CSP, X-Frame-Options: DENY, X-Content-Type-Options: nosniff, COOP), and generic 500 error interception without stack trace leakage.
- **Anti-Enumeration & Privacy**: Customer account lookup requires multi-factor authentication; phone numbers are excluded from table listings.

---

## Quick Start with Docker (Easy On / Off)

### Start the Chatbot Service
```bash
# Linux / macOS
./scripts/docker-start.sh

# Windows PowerShell
.\scripts\docker-start.ps1

# Or via Docker Compose directly:
docker compose up -d --build chatbot
```
Access the application at **`http://localhost:8000`**.

### Check Status & Health
```bash
# Linux / macOS
./scripts/docker-status.sh

# Windows PowerShell
.\scripts\docker-status.ps1
```

### Stop the Chatbot Service
```bash
# Linux / macOS
./scripts/docker-stop.sh

# Windows PowerShell
.\scripts\docker-stop.ps1

# Or via Docker Compose directly:
docker compose down
```

---

## Configuration (`.env`)

Copy `.env.example` to `.env` to configure your environment:

```ini
# LLM Provider: vllm (default for production/docker) | ollama | mock
LLM_BACKEND=vllm
LLM_TIMEOUT_SECONDS=60
USE_MOCK_LLM=0

# vLLM Server Configuration (OpenAI-compatible /v1 endpoint)
VLLM_BASE_URL=http://localhost:8000/v1
VLLM_MODEL=qwen3.8:27b
VLLM_API_KEY=

# Ollama Server Configuration (Alternative / local dev)
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=qwen3.8:27b

# HTTP / CORS Allowlist
CORS_ALLOW_ORIGINS=http://localhost:8000,http://127.0.0.1:8000

# Rate Limiting
RATE_LIMIT_REQUESTS=60
RATE_LIMIT_WINDOW_SECONDS=60
```

---

## Local Development (Without Docker)

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Run with vLLM (or Ollama / Mock)
```bash
# Run with vLLM / configured LLM
uvicorn backend.main:app --reload

# Or run in offline mock mode (no LLM server needed):
# Linux / macOS
USE_MOCK_LLM=1 uvicorn backend.main:app --reload

# Windows PowerShell
$env:USE_MOCK_LLM=1; uvicorn backend.main:app --reload
```

---

## Comprehensive Test Suite

Run the full series of 7 test suites covering security measures, vLLM integration, Qwen model compatibility, and end-to-end flows:

```bash
# Run all test suites at once with summary report
python tests/run_all_tests.py
```

### Individual Test Suites
```bash
# 1. vLLM backend integration & OpenAI schema testing
python tests/test_vllm_integration.py

# 2. Qwen 3.8 27B model compatibility & domain extraction
python tests/test_qwen_model_compatibility.py

# 3. Comprehensive security (injection, XSS, headers, rate-limiting, upload, 911 safety)
python tests/test_comprehensive_security.py

# 4. End-to-end smoke tests (Roof, Electrical, Solar, Misc)
python tests/smoke_test.py

# 5. Pipeline & CRM disposition tests
python tests/pipeline_test.py

# 6. Characterization tests (behavior lockdown)
python tests/characterization_test.py

# 7. Gmail & Google Chat integration tests
python tests/integration_gmail_chat_test.py
```

---

## Verification & Status

All 7 test suites pass cleanly:
- ✅ **vLLM Integration**: OpenAI-compatible chat completions with structured JSON schema extraction.
- ✅ **Qwen 3.8 27B Compatibility**: Zero-temperature decoding, XML prompt boundary isolation, and domain field extraction.
- ✅ **Security Hardening**: Sanitization, XML delimiter protection, rate limiting, secure uploads with EXIF removal, anti-enumeration, and deterministic safety checks.
- ✅ **Docker Ready**: Production containerization with seamless turn on/off control.
