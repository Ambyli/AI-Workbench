"""Centralized constants for the sandbox runner.

Single source of truth for every configuration knob and compile-time
limit consumed by ``app.py``, ``models.py``, ``operations.py``,
``tool_server.py``, and ``spawner.py``. Do not redefine any of these
elsewhere in the runner.

## Import contract

This module is a LEAF: it imports only stdlib (``os``, ``re``).
Anything else would risk a cycle since ``models`` and ``operations``
import from here. If you need a new constant that depends on another
runner module's type, define the raw value here and derive the typed
form at the call site.

## Env-var reads happen at import time

``os.environ.get(...)`` in the env-derived section below runs ONCE
when this module is first imported. ``common.env.load_env()`` MUST
run before that — the entry point (``app.py``) calls it as its very
first executable statement and only THEN imports the modules that
pull constants in. Any script importing ``models`` / ``operations``
in isolation without loading ``.env`` first will get the defaults
below, which is the same behavior the runner had before this
centralization (each module read ``os.environ`` inline). No
regression.

## Sections

- Env-derived — SANDBOX_*, LOG_DIR, DEBUG_LOGGING. Overridable in ``.env``.
- Payload limits — file/payload caps used by the pydantic validators.
- Validation regexes / reserved sets — SESSION_ID_RE, RESERVED_ENV_KEYS.
- Read-file limits — GET_FILES_*.
- Exec limits — EXEC_*.
- Timing — HEALTH_PROBE_TIMEOUT_S, UPDATE_SETTLE_S.
- Container hardening — MEMORY_LIMIT, NANO_CPUS_PER_CPU, PIDS_LIMIT.
  These are the security invariants documented in SANDBOX.md § Security
  model; do NOT change without updating that checklist.
"""

from __future__ import annotations

import os
import re


# ── Env-derived (runtime-tunable via .env) ────────────────────────────────

MAX_CONCURRENT = int(os.environ.get("SANDBOX_MAX_CONCURRENT", "8"))
DEFAULT_TTL_S = int(os.environ.get("SANDBOX_DEFAULT_TTL_SECONDS", "900"))
HARD_TTL_S = int(os.environ.get("SANDBOX_HARD_TTL_SECONDS", "3600"))
# Idle TTL bounds "container is up but nobody's touched the session
# recently" — the reaper tears it down even though hard TTL hasn't hit.
# Defaults to the same value as DEFAULT_TTL_S so operators only have to
# think about one knob unless they want a distinct idle policy.
IDLE_TTL_S = int(
    os.environ.get(
        "SANDBOX_IDLE_TTL_SECONDS",
        os.environ.get("SANDBOX_DEFAULT_TTL_SECONDS", "900"),
    )
)
PROXY_URL = os.environ.get("SANDBOX_PROXY_URL", "http://sandbox-proxy")
LOG_DIR = os.environ.get("LOG_DIR", "/data")
DEBUG_LOGGING = os.environ.get("DEBUG_LOGGING", "false").lower() in (
    "1", "true", "yes", "on",
)

# Reaper sweep cadence — how often the TTL/idle sweeper scans running
# sandboxes. Not env-tunable; the value trades reaper wake-ups for how
# long an expired sandbox can linger past its deadline.
SWEEP_INTERVAL_S = 60

# Docker network + egress the spawner attaches every sandbox to. Kept
# env-tunable so operators can rename the compose networks without
# touching code, but the defaults match what docker-compose.sandbox.yml
# actually creates — changing either here without also updating the
# compose file will break new container spawns.
NET_NAME = os.environ.get("SANDBOX_NET_NAME", "sandbox_net")
EGRESS_URL = os.environ.get("SANDBOX_EGRESS_URL", "http://sandbox-egress:8888")


# ── Payload limits ────────────────────────────────────────────────────────
# Enforced at pydantic-validation time so a hostile payload never touches
# the tarball builder. Base64 files count their DECODED length so a
# client can't smuggle a huge blob past the cap by encoding it.

MAX_FILE_BYTES = int(os.environ.get("SANDBOX_MAX_FILE_BYTES", "1000000"))
MAX_PAYLOAD_BYTES = int(os.environ.get("SANDBOX_MAX_PAYLOAD_BYTES", "10000000"))


# ── Validation regexes / reserved sets ────────────────────────────────────

# Sessions are the durable identity of a preview across turns. Regex is
# both a validation surface (reject anything that could path-inject when
# a caller later uses the id in a URL or filesystem context) and a hint
# to the model that the id is a short opaque string, not free text.
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Env vars we refuse to accept from the caller — these are
# runner-controlled invariants (egress proxy, log buffering) and must
# not be overridable. The spawner reapplies them on top anyway;
# refusing at the request layer gives the caller a clear error instead
# of silently discarding their value.
RESERVED_ENV_KEYS = frozenset({
    "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
    "PYTHONUNBUFFERED", "NPM_CONFIG_LOGLEVEL", "FORCE_COLOR", "TERM",
})


# ── get_files limits ──────────────────────────────────────────────────────

GET_FILES_DEFAULT_BYTES = 8 * 1024
GET_FILES_HARD_CAP_BYTES = 64 * 1024


# ── exec limits ───────────────────────────────────────────────────────────

EXEC_DEFAULT_TIMEOUT_S = 30
EXEC_HARD_TIMEOUT_S = 120
EXEC_MAX_OUTPUT_BYTES = 8 * 1024


# ── Timing ────────────────────────────────────────────────────────────────

# Post-update HTTP health probe deadline. Single-shot GET against the
# runtime's readiness path — long enough for a slow first render, short
# enough to keep update_files responsive.
HEALTH_PROBE_TIMEOUT_S = 3.0

# Gap between writing files into a live container and reading the log
# tail / probing health. Long enough that Streamlit's mtime-based
# reloader and Vite's HMR notice the change; short enough not to add
# perceptible latency to the tool response.
UPDATE_SETTLE_S = 0.5


# ── Container hardening ───────────────────────────────────────────────────
# These are security invariants — see SANDBOX.md § Security model. Do
# NOT change without re-running the security-invariant checklist.

MEMORY_LIMIT = "512m"
NANO_CPUS_PER_CPU = 1_000_000_000
PIDS_LIMIT = 256


# ── Browser log ingest ───────────────────────────────────────────────────
# Rolling-window cap on the number of browser-side events a single
# sandbox may forward to the runner. Applied per sandbox_id. Once the
# window has BROWSER_LOG_RATE_LIMIT_PER_MIN entries in the last 60s,
# further entries in the same POST are dropped and a single synthetic
# `[browser rate-limited: N events dropped in the last minute]` line
# is written in their place. Protects the runner + the container's
# /tmp/sandbox.log from a debug-loop firehose.

BROWSER_LOG_RATE_LIMIT_PER_MIN = 100

# Sanity cap on the size of a single POST body from the shim. Anything
# above this and we assume abuse / a runaway loop and reject with 413.
BROWSER_LOG_MAX_BODY_BYTES = 64 * 1024
