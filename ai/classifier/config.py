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
from typing import Optional
from urllib.parse import quote

from common.net import DEFAULT_BLOCKED_NETWORKS


def _env_flag(name: str, default: str) -> bool:
    """A boolean environment knob: 1 / true / yes / on (any case) is True."""
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


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
# The sharpness score rises linearly to 10 at this multiple of BLUR_THRESHOLD
# (reported as detail.thresholds.full_score_at).
BLUR_FULL_SCORE_MULTIPLE: float = 3.0
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
# Criterion input caps
# ---------------------------------------------------------------------------
# Server-side ceilings a request cannot exceed; a value past either is a 422.
#
# CRITERION_NAME_MAX_CHARS bounds a criterion's `name`. For `llm` criteria the
# name IS the prompt text, and it is also slugified into artifact file names,
# so this is what keeps both bounded. /criterion-types reports the live value.
#
# TEXT_MIN_COUNT_CAP bounds a `text` criterion's `options.min_count` (how many
# matches the text layer must contain). It mirrors the matcher's own guards in
# common.documents.textmatch; the pattern-length cap (MAX_PATTERN_CHARS) stays
# there because the matcher enforces it for every caller, not just this one.
CRITERION_NAME_MAX_CHARS: int = max(
    1, int(os.environ.get("CLASSIFIER_CRITERION_NAME_MAX_CHARS", "200"))
)
TEXT_MIN_COUNT_CAP: int = max(1, int(os.environ.get("CLASSIFIER_TEXT_MIN_COUNT_CAP", "1000")))

# ---------------------------------------------------------------------------
# Document loading + OCR
# ---------------------------------------------------------------------------
# The classifier accepts JPEG/PNG, PDF, SVG, plain text, and .docx. Everything
# is normalised into a common.documents.Document — pages that may carry an
# image, a text layer, or both — before any criterion runs. An SVG is one page
# rendered by MuPDF like a one-page PDF (its <text> is native text), at
# PDF_RENDER_DPI up to common.documents.DEFAULT_MAX_RENDER_PIXELS; MuPDF never
# fetches what an SVG links to, and each external reference it did not draw is
# reported in the result's documents[].warnings (see SVG_FETCH_* below for the
# opt-in that fetches <image> links instead).
#
# OCR_ENGINE: "rapidocr" loads the bundled RapidOCR (PP-OCRv6 ONNX models,
# baked into the image at build time); "none" disables OCR entirely, which
# makes `text` criteria fail on any scan or photo and leaves the vision LLM
# with no document text to read. There is no third option today.
#
# A request carries a LIST of documents, and every page of every document is
# one ITEM: a photo, an SVG, a .txt and a .docx count 1 each, a PDF counts
# its pages (read from the bytes at submit, without rendering). MAX_ITEMS caps
# the total, INCLUSIVELY — 20 items are accepted, 21 are a 400 at submit whose
# message gives the per-document breakdown. Each item costs a render (PDF/SVG),
# possibly an OCR pass, and one unit of work per criterion, so this is the
# knob that bounds one job's size.
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
MAX_ITEMS: int = max(1, int(os.environ.get("CLASSIFIER_MAX_ITEMS", "20")))
PDF_RENDER_DPI: int = max(72, int(os.environ.get("CLASSIFIER_PDF_RENDER_DPI", "150")))
TEXT_CHAR_BUDGET: int = max(0, int(os.environ.get("CLASSIFIER_TEXT_CHAR_BUDGET", "60000")))
OCR_MIN_NATIVE_CHARS: int = max(
    0, int(os.environ.get("CLASSIFIER_OCR_MIN_NATIVE_CHARS", "20"))
)

# ---------------------------------------------------------------------------
# SVG external images (analysis/loading.py resolve_svg_images, api/assess.py,
# api/references.py)
# ---------------------------------------------------------------------------
# MuPDF draws an SVG's data: images and NOTHING it would have to fetch, so an
# <image href="https://cdn.example/logo.png"> is a blank region plus a
# "not rendered" warning. SVG_FETCH_IMAGES opts in to fetching those links
# AT SUBMIT — never in the worker (jobs/payloads.py's invariant: the worker
# never touches the network for its input) — and inlining each one as a
# data: URI before the bytes are queued:
#
#   * every URL goes through common.net.fetch_url: the SSRF blocklist below
#     (BLOCKED_NETWORKS), no redirects, at most SVG_FETCH_MAX_BYTES per image,
#     at most SVG_FETCH_TIMEOUT_S per image (every image of every SVG in the
#     request is fetched concurrently, so that is also roughly the added
#     submit latency);
#   * at most SVG_FETCH_MAX_IMAGES distinct URLs per document; the rest are
#     not fetched;
#   * the body must be a PNG or JPEG by its bytes (no nested SVG, no HTML
#     error page);
#   * any failure blanks that href and becomes a documents[].warnings line
#     naming the reason — it never fails the request.
#
# OFF by default because every fetch is a beacon: it tells whoever wrote the
# SVG when, and from where, the document was processed. On customer
# documents that is a privacy cost the deployment should choose to pay.
SVG_FETCH_IMAGES: bool = _env_flag("CLASSIFIER_SVG_FETCH_IMAGES", "false")
SVG_FETCH_MAX_IMAGES: int = max(0, int(os.environ.get("CLASSIFIER_SVG_FETCH_MAX_IMAGES", "10")))
SVG_FETCH_MAX_BYTES: int = max(1, int(os.environ.get("CLASSIFIER_SVG_FETCH_MAX_BYTES", "5000000")))
SVG_FETCH_TIMEOUT_S: float = max(
    0.1, float(os.environ.get("CLASSIFIER_SVG_FETCH_TIMEOUT_S", "10"))
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
    "image/svg+xml",
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
            "  When DOCUMENT TEXT is provided above, a quoted phrase from that text "
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
# together. The block comes BEFORE the rubric and the criterion (prefix
# caching — see llm/prompts.py), which is why the presence rubric says
# "provided above".
DOCUMENT_TEXT_HEADING: str = "DOCUMENT TEXT (extracted, may contain OCR errors)"

# ---------------------------------------------------------------------------
# LLM call behaviour
# ---------------------------------------------------------------------------
# MAX_LLM_RETRIES: how many times to retry if the LLM returns unparseable JSON.
# HTTP_TIMEOUT (CLASSIFIER_HTTP_TIMEOUT_S): seconds one vLLM request may take.
#   It is httpx's per-read timeout, but responses are NOT streamed — vLLM
#   sends nothing until the answer is complete — so in practice it bounds the
#   WHOLE generation: any wait in vLLM's own queue, prefill, every reasoning
#   token, and the JSON answer. A scoring call that spends most of
#   VISION_LLM_MAX_TOKENS (8192) reasoning while the model is batching
#   MAX_LLM_CALLS-many others (plus chat) can run past two minutes, and a
#   timeout is an HTTP error: NOT retried, it fails that criterion
#   (call_vllm) or that localisation attempt (call_vllm_json). The wait for
#   an LLM_CALLS slot is outside it (the slot is taken first). Was a
#   hard-coded 120; 240 leaves room for the 6-wide batch.
# HTTP_CONNECT_TIMEOUT: seconds to wait while establishing the TCP connection.
# FETCH_TIMEOUT: seconds to fetch a `type: "url"` document
#   (analysis/loading.py). Separate from HTTP_TIMEOUT on purpose — a slow
#   remote file server is not a reason to hold a submit request for four
#   minutes.
MAX_LLM_RETRIES: int = 3
HTTP_TIMEOUT: float = max(1.0, float(os.environ.get("CLASSIFIER_HTTP_TIMEOUT_S", "240")))
HTTP_CONNECT_TIMEOUT: float = 10.0
FETCH_TIMEOUT: float = 120.0

# ---------------------------------------------------------------------------
# State: the classifier-db Postgres, and the files beside it on /data
# ---------------------------------------------------------------------------
# Every row the classifier keeps — the job queue (`jobs`), saved references
# (`reference_examples`) and one row per vision-model request (`llm_calls`) —
# lives in ONE Postgres database, the compose-managed `classifier-db`
# container. It used to be a SQLite file (/data/classifier.db); a file has no
# network listener and Trino has no SQLite connector, so none of it could be
# queried from Trino or Superset. Now it is federated as the
# `postgres_classifier` catalog. `db.py` owns the one connection pool all
# three stores share.
#
# CLASSIFIER_DB_HOST is REQUIRED — there is no default and no SQLite
# fallback: `db.database.init()` (main's lifespan) refuses to start with a
# message naming the variables when it is empty. The compose file sets it to
# `classifier-db`; the unit tests point it at a throwaway database on
# TEST_POSTGRES_DSN. User / password / name default to `classifier`, the
# same dev-default convention as ROOFIX_DB_*.
#
# The DSN is built with urllib.parse.quote on the user and the password, so
# a password containing `@`, `:` or `/` cannot break it.
#
# DATA_DIR is the volume root for everything that stays on FILES: the
# payloads of queued jobs, the per-job artifact directories, and the
# reference files. LEGACY_SQLITE_PATH is where the old database lived — read
# only by bin/migrate_sqlite_to_postgres.py and by the startup guard.
# LEGACY_SQLITE_MARKER is the file that script writes when a real (not
# --dry-run) migration finishes. While the old file exists WITHOUT the marker,
# startup skips the orphan sweeps (queued payloads and artifact directories
# whose job row is missing): every row is still in the old file, so "missing"
# would mean "not migrated yet", and the sweeps would delete data the script
# is about to give a row.
#
# JOB_TTL_HOURS is the retention window the artifact sweeper enforces (see
# § Region layers below): past it, a terminal job's artifact directory AND
# its row are both deleted. A job still pending or processing is never
# swept, however old.
DATA_DIR: str = os.environ.get("CLASSIFIER_DATA_DIR", "/data")
DB_HOST: str = os.environ.get("CLASSIFIER_DB_HOST", "").strip()
DB_PORT: int = int(os.environ.get("CLASSIFIER_DB_PORT", "5432") or "5432")
DB_USER: str = os.environ.get("CLASSIFIER_DB_USER", "classifier")
DB_PASSWORD: str = os.environ.get("CLASSIFIER_DB_PASSWORD", "classifier")
DB_NAME: str = os.environ.get("CLASSIFIER_DB_NAME", "classifier")


def build_dsn(host: str, port: int, user: str, password: str, name: str) -> str:
    """``postgresql://user:password@host:port/name`` with the user, password
    and database name percent-quoted (``safe=""``), so no character in a
    credential can be read as DSN punctuation."""
    auth = quote(user, safe="")
    if password:
        auth += ":" + quote(password, safe="")
    return f"postgresql://{auth}@{host}:{int(port)}/{quote(name, safe='')}"


DB_DSN: Optional[str] = (
    build_dsn(DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME) if DB_HOST else None
)
LEGACY_SQLITE_PATH: str = os.path.join(DATA_DIR, "classifier.db")
LEGACY_SQLITE_MARKER: str = LEGACY_SQLITE_PATH + ".migrated"
JOB_TTL_HOURS: int = int(os.environ.get("JOB_TTL_HOURS", "24"))

# ---------------------------------------------------------------------------
# Job queue + workers
# ---------------------------------------------------------------------------
# The jobs table in classifier-db is the queue (see common.jobs.postgres.claim_next,
# FOR UPDATE SKIP LOCKED).
# CLASSIFIER_MAX_CONCURRENT worker tasks each claim one pending job at a
# time, so at most that many jobs run simultaneously.
#
# Inside a job, the unit of work is (criterion, item): one criterion on one
# page of one document (a `text` criterion with options.scope "document" is
# one unit per DOCUMENT instead). Three more limits bound what those units
# may do at once; the last two are process-wide (every worker runs in this
# one process, so an asyncio.Semaphore is the whole mechanism):
#
#   MAX_UNITS_PER_JOB     units ONE job evaluates at once. A job with ten
#                         criteria on twenty pages still holds only this many
#                         in flight. A unit waiting on its depends_on holds no
#                         slot. Defaults to MAX_LLM_CALLS' 6, so a lone job
#                         can fill every model slot the classifier has (it
#                         was 2, then 4). Lower it to keep one big job from
#                         crowding out the others.
#   MAX_LLM_CALLS         model calls in flight across ALL jobs, every call
#                         type — scoring, box ask, refine, verify. Acquired in
#                         llm/client.py around the HTTP request itself, so no
#                         call path can go around it. Size it to the vision
#                         model: muse-glimmer schedules 8 (--max-num-seqs 8)
#                         and the classifier takes 6, leaving 2 for the
#                         LiteLLM chat chains muse-glimmer also serves — their
#                         overflow hook spills chat to the next order (and in
#                         the end to Claude) once vLLM's waiting queue stays
#                         non-empty, so filling all 8 here would push chat
#                         off the local model.
#   OCR_WORKERS           OCR passes at once. Each is one ONNX inference in a
#                         worker thread; the engine is shared.
#
# PAYLOAD_DIR holds one JSON file per queued job (document bytes + the
# validated request) so a job survives a container restart. Files are
# deleted the moment the job reaches a terminal phase. Defaults to a
# directory under DATA_DIR, on the classifier_data volume.
#
# WORKER_POLL_INTERVAL_S is the fallback wake-up for idle workers. New jobs
# posted to this process wake a worker instantly; the poll only matters for
# rows written by another process (or left behind by a crash).
MAX_CONCURRENT: int = max(1, int(os.environ.get("CLASSIFIER_MAX_CONCURRENT", "4")))
MAX_UNITS_PER_JOB: int = max(
    1, int(os.environ.get("CLASSIFIER_MAX_UNITS_PER_JOB", "6"))
)
MAX_LLM_CALLS: int = max(1, int(os.environ.get("CLASSIFIER_MAX_LLM_CALLS", "6")))
OCR_WORKERS: int = max(1, int(os.environ.get("CLASSIFIER_OCR_WORKERS", "4")))
PAYLOAD_DIR: str = os.environ.get(
    "PAYLOAD_DIR", os.path.join(DATA_DIR, "payloads")
)
WORKER_POLL_INTERVAL_S: float = float(os.environ.get("WORKER_POLL_INTERVAL_S", "1.0"))

# The one asyncpg pool's ceiling (db.py). Every worker can hold a connection
# for a claim or a write, every model call writes one llm_calls row after its
# slot is released, and the HTTP routes (polls, /references, /usage) need a
# few more on top — so the workers, plus the model slots, plus headroom. A
# pool that is too small never errors; a caller just waits for a connection.
DB_POOL_MAX: int = MAX_CONCURRENT + MAX_LLM_CALLS + 8

# ---------------------------------------------------------------------------
# Region layers and the artifact directory (regions/, api/artifacts.py)
# ---------------------------------------------------------------------------
# Regions answer "where" — a criterion result's list of boxes/polygons in
# original page pixels, plus the rendered overlays a caller can look at.
# Every job ALWAYS writes regions.json, manifest.json and the page's
# un-annotated base image; the SVG / PNG / preview layers are rendered on
# FIRST FETCH by the artifact endpoint and cached into the job directory.
#
# ARTIFACT_DIR is one directory per job on the classifier_data volume
# (DATA_DIR), beside the payload store. ARTIFACT_SWEEP_INTERVAL_S is how often
# the background sweeper runs; the TTL it enforces is JOB_TTL_HOURS above, which the sweeper
# makes real for the first time — for both directories AND job rows.
#
# ARTIFACT_MAX_BYTES is the cap PER ITEM: a job's directory may hold
# ARTIFACT_MAX_BYTES × its item count (a 20-page PDF gets 20× a photo's
# allowance, since it has 20 base images and 20 sets of layers). When a render
# would exceed it the PNG
# layers are dropped first, then the previews (and the base images kept so a
# FILTERED preview can be re-rendered); regions.json, manifest.json and the
# SVGs are never dropped, and the manifest records what went.
#
# INLINE_REGIONS_MAX is how many regions per criterion are copied into the job
# result itself. Past the cap the inline list is cut and `regions_truncated`
# is set — the complete list is always in regions.json.
ARTIFACT_DIR: str = os.environ.get(
    "CLASSIFIER_ARTIFACT_DIR", os.path.join(DATA_DIR, "artifacts")
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

# Rendered layer formats the artifact endpoint can produce (lazily, on first
# fetch).
REGION_LAYER_FORMATS: frozenset[str] = frozenset({"svg", "png", "preview"})

# JPEG quality for `p{n}.preview.jpg` and for the `p{n}.base.jpg` copies kept
# alongside it. The base is what a filtered preview is re-rendered from — the
# burned-in preview cannot be un-burned.
PREVIEW_JPEG_QUALITY: int = 85

# Layer file naming (regions/artifacts.py, api/artifacts.py). Every layer is
# `p{n}.<suffix>`, n being the job's global ITEM index (every page of every
# document, in order — the manifest's `items` map says which document and
# page n is), or `p{n}.<slug>.<suffix>` for one criterion. LAYER_FILE_SUFFIXES maps a format to
# the suffix it is written under, and it is the one table both the lazy
# renderer and the per-criterion `artifacts` URLs are built from.
LAYER_FILE_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("svg", "svg"), ("png", "layer.png"), ("preview", "preview.jpg")
)

# ── CV detectors (cv/) ─────────────────────────────────────────────────────
# Three kinds of number live in a detector. The MEASUREMENT parameters — which
# hues count as green, how big a blue blob must be, the Haar cascade's search
# settings, the text block size — are here, because they are what an operator
# retunes for a new site or camera. The VERDICT CUT POINTS — the ratio above
# which a detector PASSes and from which it is MARGINAL — are here too, because
# every result REPORTS them (detail.thresholds): the scoring branch and the
# report read the same constant, so a result can never state a threshold the
# detector does not use. The CURVE SHAPE between the cut points (the slopes,
# and the confidence formulas) stays inside each detector next to the
# docstring that explains it; it is never reported, and it is the detector's
# definition, not its configuration.
#
# Coverage cut points: PASS when the ratio is above *_PASS_ABOVE, MARGINAL
# from *_MARGINAL_FROM up to it, FAIL below. The ratio is the detector's
# headline metric (green_ratio, sky_ratio of the top band, water_ratio,
# dense_block_ratio).
CV_VEGETATION_PASS_ABOVE: float = 0.15
CV_VEGETATION_MARGINAL_FROM: float = 0.05
CV_SKY_PASS_ABOVE: float = 0.60
CV_SKY_MARGINAL_FROM: float = 0.30
CV_WATER_PASS_ABOVE: float = 0.15
CV_WATER_MARGINAL_FROM: float = 0.05
CV_TEXT_PASS_ABOVE: float = 0.10
CV_TEXT_MARGINAL_FROM: float = 0.03
# detect_faces PASSes (strict pass) or is MARGINAL (loose pass) at this many
# faces or more.
CV_FACE_PASS_MIN_COUNT: int = 1
#
# Decimal places every float in a structured result is rounded to — the cv
# measurements (cv/result.py) and aggregated values (analysis/result_specs.py,
# analysis/aggregate.py). Enough to compare two pages; not so many that a
# job result is noise.
DETAIL_FLOAT_DECIMALS: int = 4
#
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
# without a vision LLM. Used by `detector` criteria, and by `cv` criteria with
# no OpenCV detector when their `fallback` resolves to "detector".
#
# DETECTOR_URL empty = the feature is off. A `detector` criterion (or a `cv`
# criterion whose `fallback` is explicitly "detector") is then refused at
# submit with a 400, and a `cv` criterion's default fallback resolves to "llm"
# instead. A detector that IS configured but fails at run time fails only the
# criterion that asked it (status "error"), never the job.
#
# DETECTOR_MIN_SCORE is the default confidence floor (a `detector`
# criterion's `options.threshold` overrides it) — sent as the detector's
# `threshold` AND used to decide whether the criterion passes on the
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
# The loop runs only for `llm` criteria with `options.boxes: true` and hint
# presence/auto, and only when the model already
# scored the criterion at or above LLM_BBOX_PRESENCE_MIN — there is nothing to
# locate about a feature the model just said is absent. It never changes a
# score or a verdict: it runs AFTER the scoring call and only adds keys.
#
# LLM_BBOX_MAX_ATTEMPTS bounds the cost, and is also the server cap on a
# criterion's `options.max_attempts` (which may lower it, never raise it).
# Each attempt is one ask plus (when the box validates) one refine and one
# verify call, so 3 attempts is at most 9 small calls per criterion on top of
# that criterion's one scoring call.
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

# ── References (references/, api/references.py, analysis/references.py) ────
# A REFERENCE is a stored, reviewed example: one page, the criteria asked of
# it, the answer each one should get (score / verdict / reason) and where on
# the page the feature is. It is shown to the vision model beside a
# candidate as a worked example (a FAIL reference as a counter-example), so
# the model knows what a criterion means HERE. References are for the llm
# only; see API.md § References.
#
# REFERENCE_DIR is one directory per reference (page.jpg, working.jpg, the
# per-criterion composites c.<slug>.jpg, regions.json, record.json). It is
# deliberately NOT under ARTIFACT_DIR: the artifact sweeper deletes every
# directory whose job row is gone, and a reference must outlive the job that
# created it. Nothing sweeps this root — a reference is kept until DELETE.
#
# REFERENCE_MAX_COUNT caps how many references may exist at once (pending,
# ready and failed alike); POST /references past it is a 409. With no TTL the
# count cap is the only thing bounding the disk this root can take.
#
# VISION_LLM_MAX_IMAGES_PER_PROMPT is what the vision model admits per
# request (muse-glimmer's --limit-mm-per-prompt). 3 lets one call carry a
# PASS example, a FAIL example and the candidate; 2 sends each example in its
# own call; 1 means the model takes one image, and an /assess that asks for
# references is refused at submit.
#
# REFERENCE_MAX_PER_REQUEST bounds the explicit ids one /assess may list;
# REFERENCE_MAX_PER_CRITERION bounds how many examples (and so how many
# scoring calls) one criterion may use.
#
# REFERENCE_AUTO_POOL_MAX bounds the candidates `references: "auto"` ranks
# (newest first; `pool_truncated` says when it cut). The selection call puts
# a text catalogue of the pool beside the candidate page and asks which
# apply; REFERENCE_AUTO_MIN_CONFIDENCE (0-100) is the line a match must clear
# to be used, and REFERENCE_SELECT_MAX_TOKENS its completion budget.
#
# REFERENCE_POSITION_* drive the opt-in position check (`options.reference.
# position: "check"` on an llm criterion with boxes): the candidate's located
# box and the example's, each as a fraction of its own page, HIT when their
# IoU reaches POSITION_MIN_IOU (a criterion's `min_iou` overrides it) or
# their centres are within POSITION_MAX_OFFSET (0-1, centre distance / √2).
# A MISS caps the score at POSITION_CAP; the cap never raises a score.
#
# REFERENCE_DESCRIBE: when a reference is created without a `description`,
# one single-image call describes the page (for the `auto` catalogue), cut
# to REFERENCE_DESCRIPTION_MAX_CHARS — which is also the longest description
# a caller may send.
REFERENCE_DIR: str = os.environ.get(
    "CLASSIFIER_REFERENCE_DIR", os.path.join(DATA_DIR, "references")
)
REFERENCE_MAX_COUNT: int = max(
    1, int(os.environ.get("CLASSIFIER_REFERENCE_MAX_COUNT", "500"))
)
VISION_LLM_MAX_IMAGES_PER_PROMPT: int = max(
    1, int(os.environ.get("VISION_LLM_MAX_IMAGES_PER_PROMPT", "3"))
)
REFERENCE_MAX_PER_REQUEST: int = max(
    1, int(os.environ.get("CLASSIFIER_REFERENCE_MAX_PER_REQUEST", "10"))
)
REFERENCE_MAX_PER_CRITERION: int = max(
    1, int(os.environ.get("CLASSIFIER_REFERENCE_MAX_PER_CRITERION", "3"))
)
REFERENCE_AUTO_POOL_MAX: int = max(
    1, int(os.environ.get("CLASSIFIER_REFERENCE_AUTO_POOL_MAX", "20"))
)
REFERENCE_AUTO_MIN_CONFIDENCE: int = max(
    0, min(100, int(os.environ.get("CLASSIFIER_REFERENCE_AUTO_MIN_CONFIDENCE", "60")))
)
REFERENCE_SELECT_MAX_TOKENS: int = max(
    64, int(os.environ.get("CLASSIFIER_REFERENCE_SELECT_MAX_TOKENS", "2048"))
)
REFERENCE_POSITION_MIN_IOU: float = max(
    0.0, min(1.0, float(os.environ.get("CLASSIFIER_REFERENCE_POSITION_MIN_IOU", "0.3")))
)
REFERENCE_POSITION_MAX_OFFSET: float = max(
    0.0, min(1.0, float(os.environ.get("CLASSIFIER_REFERENCE_POSITION_MAX_OFFSET", "0.15")))
)
REFERENCE_POSITION_CAP: int = max(
    1, min(10, int(os.environ.get("CLASSIFIER_REFERENCE_POSITION_CAP", "5")))
)
REFERENCE_DESCRIBE: bool = _env_flag("CLASSIFIER_REFERENCE_DESCRIBE", "true")
REFERENCE_DESCRIPTION_MAX_CHARS: int = max(
    1, int(os.environ.get("CLASSIFIER_REFERENCE_DESCRIPTION_MAX_CHARS", "500"))
)

# Code constants for references (not env knobs). The describe call's
# completion budget: the answer is ~one paragraph of JSON, the rest is the
# model's reasoning. Title / tag bounds keep a catalogue line short.
# REFERENCE_JPEG_QUALITY is for page.jpg / working.jpg / the composites: a
# composite is what the model will SEE as the example, so it is kept higher
# than a preview.
REFERENCE_DESCRIBE_MAX_TOKENS: int = 1024
REFERENCE_TITLE_MAX_CHARS: int = 120
REFERENCE_MAX_TAGS: int = 20
REFERENCE_TAG_MAX_CHARS: int = 40
REFERENCE_JPEG_QUALITY: int = 90

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
# `type` is the evaluation path (llm | text | cv | detector); everything
# type-specific — here the rubric the LLM applies — lives in `options`.
DEFAULT_CRITERIA: list[dict] = [
    {"name": "document legibility",  "type": "llm", "options": {"hint": "quality"}},
    {"name": "image sharpness",      "type": "llm", "options": {"hint": "quality"}},
    {"name": "proper exposure",      "type": "llm", "options": {"hint": "quality"}},
    {"name": "absence of artifacts", "type": "llm", "options": {"hint": "quality"}},
]
