"""Central configuration for the Document Classifier service.

All tuneable values live here — environment-driven settings AND the
module-level constants the pipeline used to keep next to the code that used
them (content-type allowlist, working image size, fuzzy-credit floor, the LLM
hint rubrics and prompt headings, the SSRF blocklist). Anything a deployment
or a prompt engineer might want to change without reading the pipeline is in
this file; the other modules import from here and hold no constants of their
own beyond function registries.

This file is deliberately NOT split along the package boundaries below it. A
constant read by three packages has one home, and the section comments name
the module that reads each group.

Environment-driven values can be overridden per container so that the same
Docker image can be reconfigured without a rebuild. The rest are code
constants — edit them here and rebuild.

Process flow position: loaded first by every other module at import time.
"""

import ipaddress
import os
import re

from common.net import DEFAULT_BLOCKED_NETWORKS

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
# Page images smaller than this on EITHER axis are rejected before any
# processing (HTTP 400, "Image too small"). A thumbnail-catcher, nothing
# more: OCR upscales small pages itself, the vision model takes any size,
# and the working-image step only ever shrinks — so the floor is set where
# an image stops being a document at all, not where it gets hard. It was
# 100 × 100, which refused a legitimate input: a crop of one text line
# submitted as its own document (the utility-bill pipeline's stage 3) is
# often under 100 px tall and perfectly readable.
MIN_IMAGE_WIDTH: int = max(1, int(os.environ.get("CLASSIFIER_MIN_IMAGE_WIDTH", "32")))
MIN_IMAGE_HEIGHT: int = max(1, int(os.environ.get("CLASSIFIER_MIN_IMAGE_HEIGHT", "32")))

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
# Document analysis constants (analysis/loading.py, analysis/text_eval.py)
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
# LLM prompt text (llm/prompts.py)
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
# container restarts.  JOB_TTL_HOURS is the retention window the artifact
# sweeper enforces (see § Region layers below): past it, a terminal job's
# artifact directory AND its row are both deleted. A job still pending or
# processing is never swept, however old.
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
# Region layers and the artifact directory (regions/, api/artifacts.py)
# ---------------------------------------------------------------------------
# Regions answer "where" — a criterion result's list of boxes/polygons in
# original page pixels, plus the rendered overlays a caller can look at. All
# of it is OFF unless the request asks (`regions` on /assess and /compare;
# implied on /locate), because regions cost detector work and layers cost
# disk.
#
# ARTIFACT_DIR is one directory per job on the same /data volume as the DB and
# the payload store. ARTIFACT_SWEEP_INTERVAL_S is how often the background
# sweeper runs; the TTL it enforces is JOB_TTL_HOURS above, which the sweeper
# makes real for the first time — for both directories AND job rows.
#
# ARTIFACT_MAX_BYTES is the per-job cap. When a render would exceed it the PNG
# layers are dropped first, then the previews (and the base images kept so a
# FILTERED preview can be re-rendered); regions.json, manifest.json and the
# SVGs are never dropped, and the manifest records what went.
#
# INLINE_REGIONS_MAX is how many regions per criterion are copied into the job
# result itself. Past the cap the inline list is cut and `regions_truncated`
# is set — the complete list is always in regions.json.
ARTIFACT_DIR: str = os.environ.get(
    "CLASSIFIER_ARTIFACT_DIR", os.path.join(os.path.dirname(DB_PATH) or ".", "artifacts")
)
ARTIFACT_SWEEP_INTERVAL_S: float = max(
    30.0, float(os.environ.get("CLASSIFIER_ARTIFACT_SWEEP_INTERVAL_S", "600"))
)
ARTIFACT_MAX_BYTES: int = max(
    0, int(os.environ.get("CLASSIFIER_ARTIFACT_MAX_BYTES", "50000000"))
)
INLINE_REGIONS_MAX: int = max(
    0, int(os.environ.get("CLASSIFIER_INLINE_REGIONS_MAX", "50"))
)

# Rendered layer formats a request may ask for. "svg" is the default when the
# shorthand `regions=true` is used; an empty layer list still writes
# regions.json + manifest.json.
REGION_LAYER_FORMATS: frozenset[str] = frozenset({"svg", "png", "preview"})

# JPEG quality for `p{n}.preview.jpg` and for the `p{n}.base.jpg` copies kept
# alongside it. The base is what a filtered preview is re-rendered from — the
# burned-in preview cannot be un-burned.
PREVIEW_JPEG_QUALITY: int = 85

# Layer file naming (regions/artifacts.py, api/artifacts.py). Every per-page
# layer is `p{n}.<suffix>`; LAYER_FILE_SUFFIXES maps a requested format to the
# suffix it is written under, and it is the one table both the writer and the
# per-criterion `artifacts` URLs are built from. LAYER_STEM_PREFIX_RE matches
# what can sit in FRONT of the page number — a compare job's diff layers
# (`diff-e0-p0.svg`) and, with `regions.examples`, an example's own layers
# (`e0.p0.svg`) — so the file endpoint can strip it and read the page number
# the same way for all three families.
LAYER_FILE_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("svg", "svg"), ("png", "layer.png"), ("preview", "preview.jpg")
)
LAYER_STEM_PREFIX_RE: re.Pattern[str] = re.compile(r"^(?:diff-e\d+-|e\d+\.)")

# ── CV detectors (cv/) ─────────────────────────────────────────────────────
# Two kinds of number live in a detector. The MEASUREMENT parameters — which
# hues count as green, how big a blue blob must be, the Haar cascade's search
# settings, the text block size — are here, because they are what an operator
# retunes for a new site or camera. The SCORING CURVES (how a coverage ratio
# maps onto 1-10 and PASS/MARGINAL/FAIL) stay inside each detector next to the
# docstring that explains them; they are the detector's definition, not its
# configuration.
#
# detect_vegetation — HSV range for "green" (H 35-85 covers grass through
# conifer) and the open/close kernel that removes speckle from the mask.
CV_VEGETATION_HSV_LOWER: tuple[int, int, int] = (35, 40, 40)
CV_VEGETATION_HSV_UPPER: tuple[int, int, int] = (85, 255, 255)
CV_VEGETATION_MORPH_KERNEL: int = 5

# detect_sky — only the top CV_SKY_TOP_FRACTION of the page is examined; clear
# sky is the blue range, overcast sky is low-saturation bright grey.
CV_SKY_TOP_FRACTION: float = 0.35
CV_SKY_BLUE_HSV_LOWER: tuple[int, int, int] = (100, 30, 100)
CV_SKY_BLUE_HSV_UPPER: tuple[int, int, int] = (130, 200, 255)
CV_SKY_GREY_HSV_LOWER: tuple[int, int, int] = (0, 0, 150)
CV_SKY_GREY_HSV_UPPER: tuple[int, int, int] = (179, 60, 255)

# detect_faces — Haar cascade search settings. The strict pass
# (MIN_NEIGHBORS_HIGH) decides PASS; the loose pass (MIN_NEIGHBORS_LOW) only
# runs when the strict one found nothing and can at best reach MARGINAL.
CV_FACE_SCALE_FACTOR: float = 1.05
CV_FACE_MIN_NEIGHBORS_HIGH: int = 5
CV_FACE_MIN_NEIGHBORS_LOW: int = 3
CV_FACE_MIN_SIZE: tuple[int, int] = (30, 30)

# detect_water — the blue/teal range, the contour area (in working-image
# pixels) below which a blue blob is ignored, and the Laplacian variance a
# blob must stay UNDER to count as flat water rather than a blue car.
CV_WATER_HSV_LOWER: tuple[int, int, int] = (90, 40, 40)
CV_WATER_HSV_UPPER: tuple[int, int, int] = (130, 255, 255)
CV_WATER_MIN_CONTOUR_AREA_PX: int = 500
CV_WATER_MAX_TEXTURE_VARIANCE: float = 200.0

# detect_text — Sobel magnitude threshold for an "edge" pixel, the square
# block size the page is gridded into, and the fraction of edge pixels a block
# needs to count as text. CV_TEXT_MERGE_KERNEL closes one-cell gaps (the space
# between two words) before adjacent hot blocks are merged into one box, and
# a merged block smaller than CV_TEXT_MIN_BLOCKS cells is dropped as a stray
# high-contrast edge.
CV_TEXT_EDGE_THRESHOLD: int = 50
CV_TEXT_BLOCK_SIZE: int = 32
CV_TEXT_BLOCK_DENSITY: float = 0.35
CV_TEXT_MERGE_KERNEL: tuple[int, int] = (2, 3)
CV_TEXT_MIN_BLOCKS: int = 2

# Region extraction shared by the mask-based detectors. Detectors already
# compute masks and contours to produce their scores; these control how much
# of that becomes a region rather than being discarded.
#
# CV_REGION_MIN_AREA_FRAC drops specks: a contour under this fraction of the
# image is noise in the mask, not a finding worth drawing.
# CV_REGION_MAX_PER_DETECTOR bounds a pathological mask (a photo of a hedge
# can produce thousands of contours) so one criterion cannot fill the layer.
# CV_REGION_POLY_EPSILON_FRAC is the approxPolyDP tolerance as a fraction of
# the contour's perimeter — higher means fewer, straighter vertices.
CV_REGION_MIN_AREA_FRAC: float = 0.002
CV_REGION_MAX_PER_DETECTOR: int = 40
CV_REGION_POLY_EPSILON_FRAC: float = 0.01

# How close a `cv` criterion name must be to a registered detector alias for
# `get_detector` to treat it as that detector (difflib ratio, 0-1). 0.8 lets
# typos and small variants through — "has textt" → "has text", "exposed" →
# "is exposed", "has a pool" → "has pool" — while unrelated names fall
# through to the detector service / LLM as they should. The old 0.6 mapped
# "has solar panels" → "has plants", "has meter" → "has water", "has bicycle"
# → "has faces", "has car" → "has water": wrong detector, wrong answer,
# silently. Do not lower it without checking those pairs.
CV_NAME_FUZZY_CUTOFF: float = 0.8

# ── Open-vocabulary detector service (detector/client.py) ──────────────────
# The `ai/detector` container (OWLv2 by default) turns a free-text label into
# boxes, which is what lets an arbitrary "has bicycle" criterion localise
# without a vision LLM. Used only when the request sets `regions.detector`.
#
# DETECTOR_URL empty = the feature is off. A request that asks for
# `regions.detector` then still succeeds and records a note saying the
# service is not configured — an unreachable dependency must never fail a
# job that would otherwise have scored fine.
#
# DETECTOR_MIN_SCORE is the confidence floor sent as the detector's
# `threshold` AND used to decide whether a `cv` criterion passes on the
# detector's evidence alone. It is deliberately the same number: two floors
# would mean boxes that count as regions but not as evidence.
#
# DETECTOR_TIMEOUT_S bounds one /detect call. A page image on the shared GPU
# answers in a few hundred ms; the CPU fallback takes a few seconds, and 30 s
# is generous for either without letting a wedged service hold a worker.
#
# DETECTOR_MAX_LABELS_PER_CALL bounds how many labels go in one request. The
# detector embeds every label as its own query, so its cost is linear and its
# own DETECTOR_MAX_LABELS caps the list; a page needing more labels than this
# is split across several calls rather than rejected.
DETECTOR_URL: str = os.environ.get("DETECTOR_URL", "").strip().rstrip("/")
DETECTOR_MIN_SCORE: float = float(os.environ.get("DETECTOR_MIN_SCORE", "0.25"))
DETECTOR_TIMEOUT_S: float = float(os.environ.get("DETECTOR_TIMEOUT_S", "30"))
DETECTOR_MAX_LABELS_PER_CALL: int = max(
    1, int(os.environ.get("DETECTOR_MAX_LABELS_PER_CALL", "16"))
)

# Detector score at or above which a detector-scored `cv` criterion earns a
# full 10 rather than a 7. Between DETECTOR_MIN_SCORE and this the finding is
# real but not confident, which is what a 7 means everywhere else in this
# service (PASS, but do not build on it).
DETECTOR_STRONG_SCORE: float = 0.5

# ── LLM bounding-box enforcement loop (llm/boxes.py) ───────────────────────
# A vision model asked "where is X" answers with a box that is often wrong and
# occasionally a non-answer (the whole frame). The loop therefore never trusts
# one: it ASKS for a box on a 0-1000 grid, VALIDATES the numbers, VERIFIES by
# cropping that box out of the ORIGINAL page and asking whether the feature is
# visible in the crop alone, and RETRIES with the failure as feedback. Every
# attempt is returned, accepted or not — a rejected box is evidence about the
# model, and the `?attempt=n` artifact filter renders it on its own.
#
# The loop runs only when the request sets `regions.llm_boxes`, only for
# `llm` criteria with hint presence/auto, and only when the model already
# scored the criterion at or above LLM_BBOX_PRESENCE_MIN — there is nothing to
# locate about a feature the model just said is absent. It never changes a
# score or a verdict: it runs AFTER the scoring call and only adds keys.
#
# LLM_BBOX_MAX_ATTEMPTS bounds the cost. Each attempt is one ask plus (when
# the box validates) one verify call, so 3 attempts is at most 6 small calls
# per criterion on top of the one scoring call for the whole job.
#
# LLM_BBOX_VERIFY_PASS is the 1-10 score the crop has to earn. 7 is the same
# line PASS means everywhere else in this service.
#
# LLM_BBOX_MIN_AREA / _MAX_AREA are the box's area as a fraction of the page.
# Under the floor it is a speck the crop cannot confirm; over the ceiling it
# is the whole frame, which is a refusal dressed up as an answer.
#
# LLM_BBOX_MAX_TOKENS is the completion budget for ONE ask or verify call.
# Muse Glimmer's reasoning tokens count against it before the JSON answer
# (the `Reasoning strength` system line still applies), so it is deliberately
# larger than the ~60 tokens of JSON it has to produce.
#
# LLM_BBOX_CROP_PAD widens the crop by this fraction of the box on each side
# before the verify call, clamped to the page. A box that clips the feature is
# common and a padded crop still answers the question that was asked; a padded
# crop is NOT what gets stored as the region. 0.25 rather than 0.10 because
# the refine pass draws TIGHT boxes: on a bill header it boxed "before $193.33"
# — three units off the line, two-thirds of its width — and a 10% crop showed
# the verifier a line with no "Amount due" on it, which it rightly failed.
LLM_BBOX_MAX_ATTEMPTS: int = max(
    1, int(os.environ.get("CLASSIFIER_LLM_BBOX_MAX_ATTEMPTS", "3"))
)
LLM_BBOX_VERIFY_PASS: int = max(
    1, min(10, int(os.environ.get("CLASSIFIER_LLM_BBOX_VERIFY_PASS", "7")))
)
LLM_BBOX_MIN_AREA: float = float(
    os.environ.get("CLASSIFIER_LLM_BBOX_MIN_AREA", "0.002")
)
LLM_BBOX_MAX_AREA: float = float(
    os.environ.get("CLASSIFIER_LLM_BBOX_MAX_AREA", "0.95")
)
LLM_BBOX_MAX_TOKENS: int = max(
    64, int(os.environ.get("CLASSIFIER_LLM_BBOX_MAX_TOKENS", "1024"))
)
LLM_BBOX_CROP_PAD: float = max(
    0.0, float(os.environ.get("CLASSIFIER_LLM_BBOX_CROP_PAD", "0.25"))
)

# LLM_BBOX_GRIDLINES draws a labelled 0-1000 coordinate grid — lines every
# LLM_BBOX_GRID_STEP units, numbered along every edge — on the copy of the
# page the ASK call sees, so the model reads a position off a ruler instead
# of estimating a fraction of the frame. Measured on photographed utility
# bills (unit-tests/classifier/documents/utility_bill*.jpeg): the bare image
# put a text line's box 25-100 grid units off on one axis; with the grid the
# mean error halved. The scoring call and the verify crop never see the grid
# and it is never stored.
#
# LLM_BBOX_REFINE re-asks after a coarse box validates, on a crop of the
# ORIGINAL page around that box — LLM_BBOX_REFINE_ZOOM × the box on each
# axis, never less than LLM_BBOX_REFINE_MIN_SPAN of the page — with its own
# grid, and maps the answer back into the page frame. Same measurement: hits
# on text lines went from 0/8 to 7/8 and the mean y error from 48 units to
# 2.4. Costs one extra call per attempt whose coarse box validated; the
# coarse box is kept when the second answer is unusable, and both are
# recorded on the attempt (`coarse_bbox_grid`, `refined`).
def _env_flag(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


LLM_BBOX_GRIDLINES: bool = _env_flag("CLASSIFIER_LLM_BBOX_GRIDLINES", "true")
LLM_BBOX_GRID_STEP: int = max(
    10, int(os.environ.get("CLASSIFIER_LLM_BBOX_GRID_STEP", "100"))
)
LLM_BBOX_REFINE: bool = _env_flag("CLASSIFIER_LLM_BBOX_REFINE", "true")
LLM_BBOX_REFINE_ZOOM: float = max(
    1.0, float(os.environ.get("CLASSIFIER_LLM_BBOX_REFINE_ZOOM", "2.5"))
)
LLM_BBOX_REFINE_MIN_SPAN: float = min(
    1.0, max(0.05, float(os.environ.get("CLASSIFIER_LLM_BBOX_REFINE_MIN_SPAN", "0.2")))
)

# Presence score at or above which a criterion is worth locating. Not an env
# knob: it is the service-wide PASS line (utils.verdict_from_score), and a
# deployment that moved it here alone would locate features the same result
# calls FAIL.
LLM_BBOX_PRESENCE_MIN: int = 7

# The normalised square the model is asked to answer on. 0-1000 rather than
# 0-1 because models emit integers far more reliably than decimals; shared
# with common.vision.geometry.DEFAULT_GRID, which does the conversion.
LLM_BBOX_GRID: float = 1000.0

# ── Change detection (compare/diff.py) ─────────────────────────────────────
# `/assess/compare` with `regions.diff` aligns each live example onto the
# subject and reports what changed. Classical CV, no model: ORB features +
# a RANSAC homography, then an absolute difference on blurred grayscale.
#
# DIFF_MIN_INLIERS is the honesty threshold. Under it the two photos were not
# taken from close enough to the same place for a pixel difference to mean
# anything, and the result says `aligned: false` with NO regions rather than
# drawing boxes around the parallax.
#
# DIFF_MIN_AREA drops specks: a change blob under this fraction of the image
# is compression noise or a moved leaf, not a finding.
#
# DIFF_BLUR is the Gaussian kernel (odd, in pixels) applied before the
# difference. It is what stops JPEG blocking and a one-pixel alignment error
# from lighting up every edge in the scene.
#
# DIFF_MAX_REGIONS bounds a pathological pair (a re-shot photo at a different
# time of day differs everywhere) so one example cannot fill the layer.
DIFF_MIN_INLIERS: int = max(
    4, int(os.environ.get("CLASSIFIER_DIFF_MIN_INLIERS", "30"))
)
DIFF_MIN_AREA: float = float(os.environ.get("CLASSIFIER_DIFF_MIN_AREA", "0.001"))
DIFF_BLUR: int = max(1, int(os.environ.get("CLASSIFIER_DIFF_BLUR", "5")) | 1)
DIFF_MAX_REGIONS: int = max(1, int(os.environ.get("CLASSIFIER_DIFF_MAX_REGIONS", "40")))

# The three change classes a diff region is labelled with (`attrs.change`),
# in the order a reader thinks about them.
DIFF_CHANGE_ADDED: str = "added"
DIFF_CHANGE_REMOVED: str = "removed"
DIFF_CHANGE_CHANGED: str = "changed"

# How much more textured one side has to be than the other before a blob is
# called added or removed rather than merely changed. 1.6 is deliberately not
# 1.0: two renderings of the same object at different exposures differ in
# edge energy by a few per cent, and calling that "added" would be a lie with
# a box around it.
DIFF_EDGE_RATIO: float = 1.6

# Alignment. DIFF_ORB_FEATURES is the keypoint budget — 2000 is plenty for a
# photograph and cheap; more mostly buys matches on JPEG noise.
# DIFF_LOWE_RATIO is Lowe's ratio for the kNN match filter (the standard
# 0.75). DIFF_RANSAC_REPROJ_PX is findHomography's inlier tolerance in pixels.
DIFF_ORB_FEATURES: int = 2000
DIFF_LOWE_RATIO: float = 0.75
DIFF_RANSAC_REPROJ_PX: float = 5.0

# Where a difference is NOT measured. Two photos taken from slightly different
# places do not overlap at the frame edge, and the warp leaves a black border
# where the reference had no pixels — a strip of "change" hugging the frame is
# the commonest false positive in the whole method. The valid mask is eroded
# by DIFF_VALID_ERODE_KERNEL and a frame margin of DIFF_FRAME_MARGIN_FRAC of
# the short side (at least DIFF_FRAME_MARGIN_MIN_PX) is zeroed.
DIFF_FRAME_MARGIN_FRAC: float = 0.01
DIFF_FRAME_MARGIN_MIN_PX: int = 4
DIFF_VALID_ERODE_KERNEL: int = 9

# Mask clean-up after Otsu: close (kernel, iterations) joins the fragments of
# one change, then open (kernel) removes what is left of the speckle.
# DIFF_POLY_EPSILON_FRAC is the approxPolyDP tolerance for the resulting
# contours, as a fraction of each contour's perimeter.
DIFF_CLOSE_KERNEL: int = 7
DIFF_CLOSE_ITERATIONS: int = 2
DIFF_OPEN_KERNEL: int = 5
DIFF_POLY_EPSILON_FRAC: float = 0.01

# Prefix for the synthetic criterion name a diff's regions are filed under —
# `_diff:e0` for the first example. It is not a criterion: it never reaches
# compute_weighted_score or the similarity comparison, it only needs a key in
# regions.json and a slug for the layer file name.
DIFF_CRITERION_PREFIX: str = "_diff:e"

# ── Text-hit regions (analysis/text_eval.py) ───────────────────────────────
# Cap on regions derived from one text criterion's matches, per page. A regex
# like `\d` on a dense scan would otherwise localise every digit.
TEXT_REGION_MAX_HITS: int = 200

# ── Grounding experiment (bin/grounding_experiment.py) ─────────────────────
# Defaults for the operator script that measures how well the vision model
# boxes things (plan § 3.3, step 0). GROUNDING_IMAGE_SUFFIXES is what counts
# as an input image when a directory is scanned; GROUNDING_DEFAULT_CRITERIA
# names the repo fixtures and criteria worth asking about each — defaults
# rather than requirements, since the point of the experiment is the
# operator's own documents.
GROUNDING_IMAGE_SUFFIXES: frozenset[str] = frozenset({".jpg", ".jpeg", ".png"})
GROUNDING_DEFAULT_CRITERIA: dict[str, list[str]] = {
    "Neighborhood.jpeg": [
        "a house", "a tree", "a parked car", "a roof", "the sky",
        "solar panels", "a swimming pool",
    ],
    "scene_before.png": ["a house", "a shed", "a fence", "the sky"],
    "scene_after.png": ["a house", "a red car", "a fence", "the sky"],
    "greenery_and_sky.png": ["a swimming pool", "the sky", "vegetation"],
    "text_blocks.png": ["a heading", "a dollar amount", "an email address"],
    "photo_of_letter.png": ["a heading", "a signature", "a printed paragraph"],
}

# ---------------------------------------------------------------------------
# SSRF blocklist (analysis/loading.py)
# ---------------------------------------------------------------------------
# Private/internal IP ranges a caller-supplied document URL must never resolve
# to. analysis.loading.validate_url() resolves the hostname and rejects the
# fetch if any resolved address falls in one of these. The list is
# common.net.DEFAULT_BLOCKED_NETWORKS — the same one the detector service
# uses, so the two cannot drift. Append a network here to fence off more of
# this deployment's infrastructure; never remove the RFC1918 or loopback
# entries — on ai_shared that would let a URL reach litellm, the databases, or
# the vLLM containers.
BLOCKED_NETWORKS: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = list(
    DEFAULT_BLOCKED_NETWORKS
)

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
