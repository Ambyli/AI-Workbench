"""Central configuration & secrets.

All tunables and secrets live here, sourced from environment variables (and an
optional `.env` file). Nothing secret is hard-coded; the app reads everything
through the single `settings` object below.

We prefer `pydantic-settings` when it is installed, but fall back to a tiny
hand-rolled loader so the prototype still imports on a machine that hasn't run
`pip install -r requirements.txt` yet. Either way the public surface is the same:
`from .config import settings` then `settings.SOME_KEY`.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List

# ZEO_ENV_FILE lets each process load its own file: the chatbot loads .env (model key,
# no CRM/mail secrets); the account service loads .env.account (CRM/mail secrets,
# no model key). See backend/account_service.py.
_ENV_FILE = Path(os.environ.get("ZEO_ENV_FILE") or (Path(__file__).parent.parent / ".env"))


# ---------------------------------------------------------------------------
# Minimal .env loader (only used for the fallback path; pydantic-settings does
# its own parsing). Deliberately conservative: KEY=VALUE lines, # comments,
# optional surrounding quotes. Never overrides an already-set real env var.
# ---------------------------------------------------------------------------
def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val
    except OSError:
        pass


def _split_csv(value: str) -> List[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


# Defaults — safe for local/prototype use. Production overrides via env/.env.
_DEFAULTS = {
    # --- LLM Provider (vllm | ollama | mock) ---
    "LLM_BACKEND": "vllm",           # vllm (default for production) | ollama | mock
    "LLM_TIMEOUT_SECONDS": "12",     # per model call; a turn makes up to 2 calls (fits a 30s client budget)
    # true -> model is mandatory: health reports 503 and turns fail closed instead of
    # silently degrading to the regex mock (which fails the Stage 2 quality gate).
    "LLM_REQUIRED": "false",
    "LLM_PROBE_TTL_SECONDS": "30",   # re-probe model reachability at most this often (never cached forever)
    # json_schema -> constrained decoding (vLLM/OpenAI json_schema); json_object -> legacy JSON mode
    "LLM_STRUCTURED_OUTPUT": "json_schema",
    # vLLM (OpenAI-compatible server endpoint)
    "VLLM_BASE_URL": "http://localhost:8000/v1",
    "VLLM_MODEL": "qwen3.8",        # model id as served by the LiteLLM gateway (/v1/models)
    "VLLM_API_KEY": "",              # optional bearer token for vLLM
    # Ollama (legacy / local dev alternative)
    "OLLAMA_HOST": "http://localhost:11434",
    "OLLAMA_MODEL": "qwen3.8:27b",
    # --- HTTP / CORS ---
    # Comma-separated allow-list. Locked down by default to local dev origins.
    "CORS_ALLOW_ORIGINS": "http://localhost:8000,http://127.0.0.1:8000",
    # --- Rate limiting (fixed window, per client IP) ---
    "RATE_LIMIT_REQUESTS": "60",      # requests allowed...
    "RATE_LIMIT_WINDOW_SECONDS": "60",  # ...per this many seconds
    # --- User Concurrency & Queueing ---
    "MAX_ACTIVE_USERS": "25",         # max concurrent active chat users
    "MAX_QUEUE_SIZE": "100",          # max waiting queue capacity
    "ACTIVE_SESSION_TIMEOUT_SECONDS": "150",  # idle timeout before an active slot is released; the page heartbeats every 45s, so only closed tabs time out
    "QUEUE_POLL_TIMEOUT_SECONDS": "60",       # 60s timeout for queued client heartbeats
    # --- Session hygiene ---
    "SESSION_TTL_SECONDS": "3600",    # idle session expiry (1h)
    "MAX_SESSIONS": "5000",           # hard cap on concurrent in-memory sessions
    "MAX_SESSIONS_PER_CLIENT": "5",   # live server-issued sessions per client IP (anti-squatting)
    "MAX_UPLOADS_PER_SESSION": "10",  # files one conversation may upload
    # --- Public vs staff API surface ---
    # The staff/demo portal (inbox, CRM, customers, lookup, queue simulator, /docs,
    # /uploads) is OFF unless explicitly enabled. Never enable on a public deployment.
    "ENABLE_STAFF_API": "false",
    # --- Runtime data (S-M6) ---
    # Where runtime-written data lives: handoff inbox, Chat feed, CRM cases, uploads.
    # Empty -> backend/data (dev default). Docker mounts a volume here; tests use a temp dir.
    "DATA_DIR": "",
    "UPLOAD_DIR": "",                 # default DATA_DIR/uploads; the account service points this
                                      # at the chatbot's upload folder (read-only use) to attach photos
    # --- Account separation layer (backend/account_gateway.py, backend/account_service.py) ---
    # inprocess: dev/tests/internal demo (no gap). service: the chatbot calls the separate
    # account service and only ever learns found / not found plus an opaque reference.
    "ACCOUNT_GATEWAY": "inprocess",
    "ACCOUNT_SERVICE_URL": "http://127.0.0.1:8100",
    "ACCOUNT_SERVICE_KEY": "",        # shared secret between the chatbot and the account service
    "ACCOUNT_SERVICE_TIMEOUT_SECONDS": "10",
    "REQUIRE_ACCOUNT_GAP": "false",   # true (mock/production): refuse to start the chatbot unless
                                      # the gap holds (service gateway, no CRM/mail secrets, staff off)
    "ACCOUNT_REF_TTL_SECONDS": "7200",
    # Customers who stop answering: after this long idle, the department gets an
    # "incomplete request" alert with what was collected (once per conversation).
    "ABANDON_AFTER_SECONDS": "1200",
    "RETENTION_DAYS": "30",           # purge stored cases/emails/uploads older than this (0 = keep)
    # --- Geocoding (address -> lat/lng for the damage-pointer map) ---
    # nominatim = public OpenStreetMap service (dev only: 1 req/s usage policy, third-party
    #             data sharing); none = skip (map falls back to address search in the UI).
    # A contracted provider is a production decision (see DELIVERY_PLAN findings).
    "GEOCODER": "nominatim",
    "STAFF_API_KEY": "",              # if set, staff routes also require header X-Staff-Key
    # --- Answer-validation gate ---
    "ANSWER_CONFIDENCE_THRESHOLD": "0.7",  # min LLM responsiveness confidence to accept
    # --- CRM ---
    "CRM_BACKEND": "stub",            # stub | phoenix | mcp | inhouse
    # Account match gates (backend/account_match.py). Scores are the 0-100 fuzzy_score
    # of the phoenix queries. Calibrate against real data before launch.
    "CRM_ADDRESS_MIN_SCORE": "55",    # address query score floor (exact house number also required)
    "CRM_NAME_MIN_SCORE": "65",       # name query score floor
    "CRM_STREET_MIN_SIMILARITY": "0.3",  # street-name similarity floor (blocks same number, other street)
    "CRM_MATCH_MARGIN": "5",          # two different homes within this many points -> ambiguous, no match
    "CRM_DB_URL": "",                 # secret — read-only phoenix connection (CRM_BACKEND=phoenix)
    "CRM_QUERY_TIMEOUT_SECONDS": "3",
    "CRM_MCP_URL": "",                # filled in once the in-house MCP is available
    "CRM_API_BASE_URL": "",           # filled in once the in-house CRM API is documented
    "CRM_API_KEY": "",                # secret — set via env/.env only
    # --- Email (Google Workspace) ---
    "EMAIL_SEND_ENABLED": "false",    # false -> deliver to in-memory Inbox (demo)
    "EMAIL_FROM": "service-bot@zeoenergy.com",
    # Google Workspace service-account + domain-wide delegation
    "GOOGLE_SA_CREDENTIALS_FILE": "",  # path to service-account JSON (secret)
    "GOOGLE_DELEGATED_USER": "",       # user the service account impersonates
    "GMAIL_CREATE_DRAFT": "true",      # true -> create a draft then send it; false -> send directly
    # --- Routing mailboxes (where handoff emails go) ---
    "MAILBOX_DISPOSITION": "service-team@zeoenergy.com",     # matched accounts
    "MAILBOX_UNVERIFIED": "intake-review@zeoenergy.com",     # no CRM match
    "MAILBOX_SOLAR": "performance-team@zeoenergy.com",       # solar production
    # --- Google Chat notifications (per-team space incoming webhooks) ---
    "CHAT_SEND_ENABLED": "false",   # false -> log to in-memory feed (demo); true -> real backend
    "CHAT_BACKEND": "stub",         # stub | webhook | api
    "CHAT_WEBHOOK_DISPOSITION": "",  # incoming-webhook URL for the service/disposition team space
    "CHAT_WEBHOOK_UNVERIFIED": "",   # incoming-webhook URL for the intake-review team space
    "CHAT_WEBHOOK_SOLAR": "",        # incoming-webhook URL for the performance team space
}


def _get(key: str) -> str:
    return os.environ.get(key, _DEFAULTS[key])


class Settings:
    """Typed accessors over the resolved environment.

    Implemented as plain properties (not pydantic) so the module has zero hard
    dependencies; values are read live so tests can monkeypatch os.environ.
    """

    # LLM Provider
    @property
    def LLM_BACKEND(self) -> str:
        return _get("LLM_BACKEND").strip().lower() or "vllm"

    @property
    def LLM_TIMEOUT_SECONDS(self) -> float:
        return _float(_get("LLM_TIMEOUT_SECONDS"), 60.0)

    @property
    def VLLM_BASE_URL(self) -> str:
        return _get("VLLM_BASE_URL").rstrip("/")

    @property
    def VLLM_MODEL(self) -> str:
        return _get("VLLM_MODEL")

    @property
    def VLLM_API_KEY(self) -> str:
        return _get("VLLM_API_KEY")

    @property
    def OLLAMA_HOST(self) -> str:
        return _get("OLLAMA_HOST").rstrip("/")

    @property
    def OLLAMA_MODEL(self) -> str:
        return _get("OLLAMA_MODEL")

    # HTTP / CORS
    @property
    def CORS_ALLOW_ORIGINS(self) -> List[str]:
        return _split_csv(_get("CORS_ALLOW_ORIGINS"))

    # Rate limiting
    @property
    def RATE_LIMIT_REQUESTS(self) -> int:
        return _int(_get("RATE_LIMIT_REQUESTS"), 60)

    @property
    def RATE_LIMIT_WINDOW_SECONDS(self) -> int:
        return _int(_get("RATE_LIMIT_WINDOW_SECONDS"), 60)

    # User Concurrency & Queueing
    @property
    def MAX_ACTIVE_USERS(self) -> int:
        return _int(_get("MAX_ACTIVE_USERS"), 25)

    @property
    def MAX_QUEUE_SIZE(self) -> int:
        return _int(_get("MAX_QUEUE_SIZE"), 100)

    @property
    def ACTIVE_SESSION_TIMEOUT_SECONDS(self) -> int:
        return _int(_get("ACTIVE_SESSION_TIMEOUT_SECONDS"), 150)

    @property
    def QUEUE_POLL_TIMEOUT_SECONDS(self) -> int:
        return _int(_get("QUEUE_POLL_TIMEOUT_SECONDS"), 60)

    # Sessions
    @property
    def SESSION_TTL_SECONDS(self) -> int:
        return _int(_get("SESSION_TTL_SECONDS"), 3600)

    @property
    def MAX_SESSIONS(self) -> int:
        return _int(_get("MAX_SESSIONS"), 5000)

    # Validation
    @property
    def ANSWER_CONFIDENCE_THRESHOLD(self) -> float:
        return _float(_get("ANSWER_CONFIDENCE_THRESHOLD"), 0.7)

    # CRM
    @property
    def CRM_BACKEND(self) -> str:
        return _get("CRM_BACKEND").strip().lower() or "stub"

    @property
    def CRM_ADDRESS_MIN_SCORE(self) -> float:
        return _float(_get("CRM_ADDRESS_MIN_SCORE"), 55.0)

    @property
    def CRM_NAME_MIN_SCORE(self) -> float:
        return _float(_get("CRM_NAME_MIN_SCORE"), 65.0)

    @property
    def CRM_STREET_MIN_SIMILARITY(self) -> float:
        return _float(_get("CRM_STREET_MIN_SIMILARITY"), 0.3)

    @property
    def CRM_MATCH_MARGIN(self) -> float:
        return _float(_get("CRM_MATCH_MARGIN"), 5.0)

    @property
    def CRM_DB_URL(self) -> str:
        return _get("CRM_DB_URL")

    @property
    def CRM_QUERY_TIMEOUT_SECONDS(self) -> float:
        return _float(_get("CRM_QUERY_TIMEOUT_SECONDS"), 3.0)

    @property
    def CRM_MCP_URL(self) -> str:
        return _get("CRM_MCP_URL")

    @property
    def CRM_API_BASE_URL(self) -> str:
        return _get("CRM_API_BASE_URL")

    @property
    def CRM_API_KEY(self) -> str:
        return _get("CRM_API_KEY")

    # Email
    @property
    def EMAIL_SEND_ENABLED(self) -> bool:
        return _bool(_get("EMAIL_SEND_ENABLED"))

    @property
    def EMAIL_FROM(self) -> str:
        return _get("EMAIL_FROM")

    @property
    def GOOGLE_SA_CREDENTIALS_FILE(self) -> str:
        return _get("GOOGLE_SA_CREDENTIALS_FILE")

    @property
    def GOOGLE_DELEGATED_USER(self) -> str:
        return _get("GOOGLE_DELEGATED_USER")

    @property
    def GMAIL_CREATE_DRAFT(self) -> bool:
        return _bool(_get("GMAIL_CREATE_DRAFT"))

    # Routing mailboxes
    @property
    def MAILBOX_DISPOSITION(self) -> str:
        return _get("MAILBOX_DISPOSITION")

    @property
    def MAILBOX_UNVERIFIED(self) -> str:
        return _get("MAILBOX_UNVERIFIED")

    @property
    def MAILBOX_SOLAR(self) -> str:
        return _get("MAILBOX_SOLAR")

    # Google Chat notifications
    @property
    def CHAT_SEND_ENABLED(self) -> bool:
        return _bool(_get("CHAT_SEND_ENABLED"))

    @property
    def CHAT_BACKEND(self) -> str:
        return _get("CHAT_BACKEND").strip().lower() or "stub"

    @property
    def CHAT_WEBHOOK_DISPOSITION(self) -> str:
        return _get("CHAT_WEBHOOK_DISPOSITION")

    @property
    def CHAT_WEBHOOK_UNVERIFIED(self) -> str:
        return _get("CHAT_WEBHOOK_UNVERIFIED")

    @property
    def CHAT_WEBHOOK_SOLAR(self) -> str:
        return _get("CHAT_WEBHOOK_SOLAR")

    # Hardening (Stage 1R)
    @property
    def LLM_REQUIRED(self) -> bool:
        return _bool(_get("LLM_REQUIRED"))

    @property
    def LLM_PROBE_TTL_SECONDS(self) -> float:
        return _float(_get("LLM_PROBE_TTL_SECONDS"), 30.0)

    @property
    def LLM_STRUCTURED_OUTPUT(self) -> str:
        v = _get("LLM_STRUCTURED_OUTPUT").strip().lower()
        return v if v in {"json_schema", "json_object"} else "json_schema"

    @property
    def MAX_SESSIONS_PER_CLIENT(self) -> int:
        return _int(_get("MAX_SESSIONS_PER_CLIENT"), 5)

    @property
    def MAX_UPLOADS_PER_SESSION(self) -> int:
        return _int(_get("MAX_UPLOADS_PER_SESSION"), 10)

    @property
    def DATA_DIR(self) -> Path:
        raw = _get("DATA_DIR").strip()
        return Path(raw) if raw else Path(__file__).parent / "data"

    @property
    def UPLOAD_DIR(self) -> Path:
        raw = _get("UPLOAD_DIR").strip()
        return Path(raw) if raw else self.DATA_DIR / "uploads"

    @property
    def ACCOUNT_GATEWAY(self) -> str:
        return _get("ACCOUNT_GATEWAY").strip().lower() or "inprocess"

    @property
    def ACCOUNT_SERVICE_URL(self) -> str:
        return _get("ACCOUNT_SERVICE_URL").rstrip("/")

    @property
    def ACCOUNT_SERVICE_KEY(self) -> str:
        return _get("ACCOUNT_SERVICE_KEY")

    @property
    def ACCOUNT_SERVICE_TIMEOUT_SECONDS(self) -> float:
        return _float(_get("ACCOUNT_SERVICE_TIMEOUT_SECONDS"), 10.0)

    @property
    def REQUIRE_ACCOUNT_GAP(self) -> bool:
        return _bool(_get("REQUIRE_ACCOUNT_GAP"))

    @property
    def ABANDON_AFTER_SECONDS(self) -> float:
        return _float(_get("ABANDON_AFTER_SECONDS"), 1200.0)

    @property
    def ACCOUNT_REF_TTL_SECONDS(self) -> float:
        return _float(_get("ACCOUNT_REF_TTL_SECONDS"), 7200.0)

    @property
    def RETENTION_DAYS(self) -> int:
        return _int(_get("RETENTION_DAYS"), 30)

    @property
    def GEOCODER(self) -> str:
        v = _get("GEOCODER").strip().lower()
        return v if v in {"nominatim", "none"} else "nominatim"

    @property
    def ENABLE_STAFF_API(self) -> bool:
        return _bool(_get("ENABLE_STAFF_API"))

    @property
    def STAFF_API_KEY(self) -> str:
        return _get("STAFF_API_KEY")


def _int(v: str, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _float(v: str, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _bool(v: str) -> bool:
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


# Load .env once at import (no-op if absent), then expose the singleton.
_load_dotenv(_ENV_FILE)
settings = Settings()
