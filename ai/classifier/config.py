import os

VLLM_QWEN_VL_API: str = os.environ.get(
    "VLLM_QWEN_VL_API", "http://vllm-qwen-vl:8000/v1/chat/completions"
)

# OpenCV thresholds
BLUR_THRESHOLD: float = 100.0
EXPOSURE_LOW: float = 30.0
EXPOSURE_HIGH: float = 220.0

# Image validation
MIN_IMAGE_WIDTH: int = 100
MIN_IMAGE_HEIGHT: int = 100

# LLM
MAX_LLM_RETRIES: int = 3
HTTP_TIMEOUT: float = 120.0
HTTP_CONNECT_TIMEOUT: float = 10.0

# Jobs DB — mounted at /data in the container via a Docker volume
DB_PATH: str = os.environ.get("DB_PATH", "/data/classifier.db")
JOB_TTL_HOURS: int = int(os.environ.get("JOB_TTL_HOURS", "24"))

# Logging — set LOG_LEVEL=DEBUG in the container environment to enable debug output
LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO").upper()

DEFAULT_CRITERIA: list[dict] = [
    {"name": "document legibility", "type": "quality"},
    {"name": "image sharpness",     "type": "quality"},
    {"name": "proper exposure",     "type": "quality"},
    {"name": "absence of artifacts","type": "quality"},
]
