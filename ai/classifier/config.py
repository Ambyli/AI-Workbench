"""Central configuration for the Document Classifier service.

All tuneable values live here. Override any setting via environment variable
so that the same Docker image can be reconfigured without a rebuild.

Process flow position: loaded first by every other module at import time.
"""

import os

# ---------------------------------------------------------------------------
# Upstream LLM — vLLM OpenAI-compatible endpoint
# ---------------------------------------------------------------------------
# Points at the vllm-qwen-vl container on the shared Docker network.
# Change this if you swap the vision model or run vLLM on a different host.
VLLM_QWEN_VL_API: str = os.environ.get(
    "VLLM_QWEN_VL_API", "http://vllm-qwen-vl:8000/v1/chat/completions"
)

# ---------------------------------------------------------------------------
# OpenCV pre-check thresholds
# ---------------------------------------------------------------------------
# These determine PASS/FAIL for the deterministic CV checks that run before
# the LLM call.  Raise BLUR_THRESHOLD to be stricter about sharpness;
# widen EXPOSURE_LOW/HIGH to accept a broader range of lighting conditions.
BLUR_THRESHOLD: float = 100.0   # Laplacian variance below this → blurry → FAIL
EXPOSURE_LOW: float = 30.0      # Mean pixel intensity below this → underexposed → FAIL
EXPOSURE_HIGH: float = 220.0    # Mean pixel intensity above this → overexposed → FAIL

# ---------------------------------------------------------------------------
# Input image validation
# ---------------------------------------------------------------------------
# Images smaller than this are rejected before any processing.
# Too-small images produce unreliable LLM scores and wasted API calls.
MIN_IMAGE_WIDTH: int = 100   # pixels
MIN_IMAGE_HEIGHT: int = 100  # pixels

# ---------------------------------------------------------------------------
# LLM call behaviour
# ---------------------------------------------------------------------------
# MAX_LLM_RETRIES: how many times to retry if the LLM returns unparseable JSON.
# HTTP_TIMEOUT: seconds to wait for the vLLM server to respond.
# HTTP_CONNECT_TIMEOUT: seconds to wait while establishing the TCP connection.
MAX_LLM_RETRIES: int = 3
HTTP_TIMEOUT: float = 120.0
HTTP_CONNECT_TIMEOUT: float = 10.0

# ---------------------------------------------------------------------------
# Async job store (SQLite)
# ---------------------------------------------------------------------------
# DB_PATH is mounted from a named Docker volume (/data) so jobs survive
# container restarts.  JOB_TTL_HOURS is informational for now; TTL-based
# cleanup can be added as a background task later.
DB_PATH: str = os.environ.get("DB_PATH", "/data/classifier.db")
JOB_TTL_HOURS: int = int(os.environ.get("JOB_TTL_HOURS", "24"))

# ---------------------------------------------------------------------------
# Job queue + workers
# ---------------------------------------------------------------------------
# The jobs table in DB_PATH is the queue (see common.jobs.sqlite.claim_next).
# CLASSIFIER_MAX_CONCURRENT worker tasks each claim one pending job at a
# time, so at most that many jobs run simultaneously. Size it to what the
# vision model behind VLLM_QWEN_VL_API can absorb — remember a /assess/compare
# job with N live examples fans out into N+1 LLM calls of its own.
#
# PAYLOAD_DIR holds one JSON file per queued job (image bytes + criteria, or
# the full CompareRequest) so a job survives a container restart. Files are
# deleted the moment the job reaches a terminal phase. Defaults to a
# sibling of DB_PATH so it lands on the same /data volume.
#
# WORKER_POLL_INTERVAL_S is the fallback wake-up for idle workers. New jobs
# posted to this process wake a worker instantly; the poll only matters for
# rows written by another process (or left behind by a crash).
MAX_CONCURRENT: int = max(1, int(os.environ.get("CLASSIFIER_MAX_CONCURRENT", "2")))
PAYLOAD_DIR: str = os.environ.get(
    "PAYLOAD_DIR", os.path.join(os.path.dirname(DB_PATH) or ".", "payloads")
)
WORKER_POLL_INTERVAL_S: float = float(os.environ.get("WORKER_POLL_INTERVAL_S", "1.0"))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
# Set LOG_LEVEL=DEBUG in docker-compose.classifier.yml to see per-step debug
# output across all modules.  INFO (default) shows the key decision points.
LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO").upper()

# ---------------------------------------------------------------------------
# Default criteria (used when the caller omits the criteria field)
# ---------------------------------------------------------------------------
DEFAULT_CRITERIA: list[dict] = [
    {"name": "document legibility", "type": "quality"},
    {"name": "image sharpness",     "type": "quality"},
    {"name": "proper exposure",     "type": "quality"},
    {"name": "absence of artifacts","type": "quality"},
]
