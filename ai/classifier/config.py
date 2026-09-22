"""Central configuration for the Document Classifier service.

All tuneable values live here — environment-driven settings AND the
module-level constants the pipeline used to keep next to the code that used
them (content-type allowlist, working image size, fuzzy-credit floor, the LLM
hint rubrics and prompt headings, the SSRF blocklist). Anything a deployment
or a prompt engineer might want to change without reading the pipeline is in
this file; the other modules import from here and hold no constants of their
own beyond function registries.

Environment-driven values can be overridden per container so that the same
Docker image can be reconfigured without a rebuild. The rest are code
constants — edit them here and rebuild.

Process flow position: loaded first by every other module at import time.
"""

import ipaddress
import os

# ---------------------------------------------------------------------------
# Upstream vision LLM — vLLM OpenAI-compatible endpoint
# ---------------------------------------------------------------------------
# Points at the muse-glimmer container (meta-models/Muse-Glimmer-30B, served
# under alias `muse-glimmer`) on the shared Docker network. VISION_LLM_MODEL is
# the `model` field sent with every chat completion and must match the
# server's --served-model-name (or the HF repo id when that flag is unset,
# e.g. `Qwen/Qwen2.5-VL-7B-Instruct` for vllm-qwen-vl). Change both to swap
# the vision model or run vLLM on a different host.
#
# VISION_LLM_REASONING_STRENGTH is a Muse Glimmer-specific knob: the model's
# reasoning depth is set by a `Reasoning strength: low|medium|high|xhigh`
# line in the system prompt (not a chat-template kwarg). Reasoning tokens
# count against max_tokens, so higher settings need a bigger budget. Set it
# to an empty string for models that don't understand the directive.
VISION_LLM_API: str = os.environ.get(
    "VISION_LLM_API", "http://muse-glimmer:8000/v1/chat/completions"
)
VISION_LLM_MODEL: str = os.environ.get("VISION_LLM_MODEL", "muse-glimmer")
VISION_LLM_REASONING_STRENGTH: str = os.environ.get(
    "VISION_LLM_REASONING_STRENGTH", "low"
).strip().lower()
# Completion budget per scoring call. Includes any reasoning the model emits
# before the JSON answer, so it is deliberately larger than the JSON alone.
VISION_LLM_MAX_TOKENS: int = int(os.environ.get("VISION_LLM_MAX_TOKENS", "8192"))

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
# Document loading + OCR
# ---------------------------------------------------------------------------
# The classifier accepts JPEG/PNG, PDF, plain text, and .docx. Everything is
# normalised into a common.documents.Document — pages that may carry an image,
# a text layer, or both — before any criterion runs.
#
# OCR_ENGINE: "rapidocr" loads the bundled RapidOCR (PP-OCRv6 ONNX models,
# baked into the image at build time); "none" disables OCR entirely, which
# makes `text` criteria fail on any scan or photo and leaves the vision LLM
# with no document text to read. There is no third option today.
#
# DOC_MAX_PAGES caps how many PDF pages are loaded and rendered. Pages past
# the cap are reported in document_info.truncated_pages rather than silently
# dropped. Raising it multiplies both OCR time and CV work per job.
#
# PDF_RENDER_DPI is the raster density for PDF page renders. 150 is the lowest
# density at which 8-10pt body text survives OCR; 300 roughly quadruples the
# pixel count (and the OCR time) for little accuracy gain on clean scans.
#
# TEXT_CHAR_BUDGET caps how much extracted text is pasted into the vision
# prompt. Text past the budget is dropped and the prompt says so. ~60k chars
# is roughly 15k tokens — sized so a long contract cannot crowd out the image
# or the response budget (VISION_LLM_MAX_TOKENS).
#
# OCR_MIN_NATIVE_CHARS is the "does this page already have a text layer?"
# threshold used by ocr mode "auto". Below it the page is treated as a scan.
OCR_ENGINE: str = os.environ.get("CLASSIFIER_OCR_ENGINE", "rapidocr").strip().lower()
DOC_MAX_PAGES: int = max(1, int(os.environ.get("CLASSIFIER_DOC_MAX_PAGES", "20")))
PDF_RENDER_DPI: int = max(72, int(os.environ.get("CLASSIFIER_PDF_RENDER_DPI", "150")))
TEXT_CHAR_BUDGET: int = max(0, int(os.environ.get("CLASSIFIER_TEXT_CHAR_BUDGET", "60000")))
OCR_MIN_NATIVE_CHARS: int = max(
    0, int(os.environ.get("CLASSIFIER_OCR_MIN_NATIVE_CHARS", "20"))
)

# ---------------------------------------------------------------------------
# Document analysis constants (analysis.py)
# ---------------------------------------------------------------------------
# ACCEPTED_CONTENT_TYPES: declared upload types accepted on POST /assess. This
# is a cheap early reject only — a caller can declare anything, so
# common.documents.detect_kind re-checks the actual bytes and is the
# authority. application/octet-stream is allowed precisely because many
# clients send it for everything. Legacy Word (application/msword) is NOT
# supported but is allowed through on purpose: detect_kind then sees the OLE2
# magic and returns the specific "convert to .docx" message instead of a
# generic content-type rejection.
ACCEPTED_CONTENT_TYPES: frozenset[str] = frozenset({
    "image/jpeg",
    "image/jpg",
    "image/png",
    "application/pdf",
    "text/plain",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/octet-stream",
    "application/msword",
})

# Long side every page image is resized to before CV detectors and the LLM
# prompt. Keeps prompt size (and CV thresholds) consistent with the
# single-image behaviour the detectors were tuned against.
MAX_WORKING_DIMENSION: int = 1000

# Similarity below which a fuzzy text criterion earns no partial credit at
# all. difflib scores two unrelated phrases around 0.3-0.5, so a "near miss"
# only means something above this line. Between the floor and the criterion's
# fuzzy_threshold the score scales 1-6; at or above the threshold it is 10.
FUZZY_CREDIT_FLOOR: float = 0.5

# ---------------------------------------------------------------------------
# LLM prompt text (llm.py)
# ---------------------------------------------------------------------------
# HINT_RUBRICS: each entry defines the heading, scoring rubric, and any extra
# instruction the LLM receives for criteria with that hint value.
# build_llm_prompt() groups criteria by hint and emits one section per group
# using these strings; GET /hints returns the table verbatim. Edit here to
# change how any hint type is explained to the LLM — no need to touch the
# prompt-building logic itself.
HINT_RUBRICS: dict[str, dict[str, str]] = {
    "quality": {
        "heading": "QUALITY criteria — score image quality on a 1-10 scale",
        "rubric":  "1-3 = FAIL (poor quality)  |  4-6 = MARGINAL  |  7-10 = PASS (good quality)",
        "extra":   "",
    },
    "presence": {
        "heading": "PRESENCE criteria — detect whether each feature is present in the document",
        "rubric":  (
            "10 = clearly present (PASS)  |  "
            "5 = uncertain or partially present (MARGINAL)  |  "
            "1 = clearly absent (FAIL)"
        ),
        "extra":   (
            "For each PRESENCE criterion your 'reason' MUST follow this structure:\n"
            "  'I observe [specific visual evidence]. "
            "Therefore [feature] is [present / absent / uncertain].'\n"
            "  When DOCUMENT TEXT is provided below, a quoted phrase from that text "
            "counts as evidence just as a visual observation does — e.g. "
            "'I observe the line \"Notice to Owner\" in the document text. "
            "Therefore notice to owner is present.' Say which source you used."
        ),
    },
    "auto": {
        "heading": "INFERRED criteria — determine the appropriate rubric from the criterion name",
        "rubric":  (
            "Quality/clarity criteria (e.g. 'image sharpness'): score quality 1-10.\n"
            "  Presence/absence criteria (e.g. 'has X'): "
            "10=present, 5=uncertain, 1=absent."
        ),
        "extra":   (
            "A criterion about content ('mentions X', 'includes a total') can be judged "
            "from the DOCUMENT TEXT block when one is provided; a criterion about the "
            "image itself (sharpness, lighting, framing) must be judged from the image."
        ),
    },
}

# Header for the extracted-text block in the user message. The rubric text
# above and API.md both refer to the block by this name, so change all three
# together.
DOCUMENT_TEXT_HEADING: str = "DOCUMENT TEXT (extracted, may contain OCR errors)"

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
# vision model behind VISION_LLM_API can absorb — remember a /assess/compare
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
# SSRF blocklist (ssrf.py)
# ---------------------------------------------------------------------------
# Private/internal IP ranges a caller-supplied document URL must never resolve
# to. validate_url() resolves the hostname and rejects the fetch if the address
# falls in any of these. Add a network here to fence off more of the
# infrastructure; never remove the RFC1918 or loopback entries — on ai_shared
# that would let a URL reach litellm, the databases, or the vLLM containers.
BLOCKED_NETWORKS: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
    ipaddress.ip_network("10.0.0.0/8"),       # RFC1918 private
    ipaddress.ip_network("172.16.0.0/12"),     # RFC1918 private
    ipaddress.ip_network("192.168.0.0/16"),    # RFC1918 private
    ipaddress.ip_network("127.0.0.0/8"),       # loopback
    ipaddress.ip_network("169.254.0.0/16"),    # link-local
    ipaddress.ip_network("0.0.0.0/8"),         # "this" network
    ipaddress.ip_network("100.64.0.0/10"),     # shared address space (RFC6598)
    ipaddress.ip_network("::1/128"),           # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),          # IPv6 unique local
    ipaddress.ip_network("fe80::/10"),         # IPv6 link-local
]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
# Set LOG_LEVEL=DEBUG in docker-compose.classifier.yml to see per-step debug
# output across all modules.  INFO (default) shows the key decision points.
LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO").upper()

# ---------------------------------------------------------------------------
# Default criteria (used when the caller omits the criteria field)
# ---------------------------------------------------------------------------
# `type` is the evaluation path (llm | cv | text); `hint` is the rubric the
# LLM applies. The two used to be conflated ("type": "quality"), which no
# longer validates against CriterionInput — hence the explicit pairing here.
DEFAULT_CRITERIA: list[dict] = [
    {"name": "document legibility", "type": "llm", "hint": "quality"},
    {"name": "image sharpness",     "type": "llm", "hint": "quality"},
    {"name": "proper exposure",     "type": "llm", "hint": "quality"},
    {"name": "absence of artifacts","type": "llm", "hint": "quality"},
]
