"""Open-vocabulary detector — FastAPI + FastMCP application entry point.

Text-prompted object detection as a service: send an image and a list of
free-text labels, get boxes with scores back in the image's ORIGINAL pixel
coordinates. No fixed class list and no fine-tuning — "swimming pool" and
"roof vent" work exactly as well as "car" does, which is what makes this
usable as the localisation source for the classifier's arbitrary `has X`
criteria (see ai/classifier/API.md § Regions and layers).

Endpoints:

    POST /detect   — image + labels → boxes. Two spellings of the same
                     request: a multipart upload (`file`, `labels`,
                     `threshold`, `max_per_label`) or a JSON body
                     (`{"image": {"data", "type"}, "labels": [...] , ...}`).
                     Content-Type decides; both normalise to the same params.
    GET  /health   — model id, family, device, and whether the weights are
                     actually resident yet.
    GET  /metrics  — Prometheus scrape endpoint (added by Instrumentator).
    POST /mcp      — the MCP surface, one tool: `detect_objects`.

Loading. The weights are built ONCE, in a worker thread, at startup — not
lazily on the first request. A lazy load would make one unlucky caller wait
several seconds and, worse, would let a broken `DETECTOR_MODEL` sit silently
in a "healthy" container until the first job. A failed load is recorded and
reported by `GET /health` and by a 503 on `/detect`, so the container stays
up and says what is wrong rather than crash-looping.

Device. `DETECTOR_DEVICE=cuda` is the default and the container is pinned to
GPU 2, which it shares with `qwen3.8-solo`. When torch reports no CUDA
device the service falls back to CPU with a warning rather than refusing to
start — a detector answering in seconds is more useful than one that is down.

Security posture. The URL input is SSRF-checked with the shared
`common.net.validate_url` before any fetch, because on `ai_shared` an
unchecked URL is a request to proxy into the private network. There is no
auth on the service itself: like the classifier, the LiteLLM pass-through
does the bearer check and direct access on PORT_DETECTOR is unauthenticated.

Process flow position: the whole service. Called by ai/classifier's
`ai/classifier/detector/client.py` over `ai_shared`, by LiteLLM's `/v1/detector`
pass-through, and by models through the `detector.detect_objects` MCP tool.
"""

import asyncio
import base64
import binascii
import io
import json
import logging
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastmcp import FastMCP
from PIL import Image, ImageOps
from prometheus_fastapi_instrumentator import Instrumentator

from common.net import BlockedURLError, validate_url

from config import (
    DEFAULT_THRESHOLD,
    DETECTOR_DEVICE,
    DETECTOR_MODEL,
    FETCH_CONNECT_TIMEOUT,
    FETCH_TIMEOUT,
    LOG_LEVEL,
    MAX_IMAGE_SIDE,
    MAX_LABELS,
    MIN_IMAGE_SIDE,
)
from detectors import build_detector
from logger import logger
from models import (
    DetectRequest,
    apply_max_per_label,
    build_response,
    parse_labels,
    parse_max_per_label,
    parse_threshold,
)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

_http_timeout = httpx.Timeout(FETCH_TIMEOUT, connect=FETCH_CONNECT_TIMEOUT)

# Process-wide model state. `_detector` is built at startup; `_load_error`
# holds the reason when that failed, so /health and /detect can both report
# it instead of the container looking healthy and answering 500s.
_detector = None
_load_error: str | None = None
_device = DETECTOR_DEVICE

# One inference at a time. OWLv2 on a shared card has no batching to gain
# from concurrency — two requests interleaved just double each other's
# latency and double peak VRAM, which is exactly what must not happen beside
# qwen3.8-solo. The lock is what bounds the memory footprint.
_inference_lock = asyncio.Lock()


def _resolve_device() -> str:
    """The device to actually use, which may not be the one configured.

    `cuda` with no CUDA device is a misconfiguration the operator should hear
    about, not one the service should die of — the CPU path is the documented
    fallback for exactly this (see DETECTOR.md § CPU fallback).
    """
    if DETECTOR_DEVICE == "cpu":
        return "cpu"
    try:
        import torch
    except ImportError:  # pragma: no cover - torch is a hard dependency
        logger.error("_resolve_device: torch is not installed — using cpu")
        return "cpu"
    if not torch.cuda.is_available():
        logger.warning(
            "_resolve_device: DETECTOR_DEVICE=%s but torch reports no CUDA "
            "device — falling back to cpu. Check the container's `deploy."
            "resources.reservations.devices` block and the host's nvidia "
            "container toolkit.",
            DETECTOR_DEVICE,
        )
        return "cpu"
    logger.info(
        "_resolve_device: cuda available — %d device(s), using %s",
        torch.cuda.device_count(),
        torch.cuda.get_device_name(0),
    )
    return "cuda"


def _load_model() -> None:
    """Build and load the detector. Blocking; run in a worker thread."""
    global _detector, _load_error, _device

    _device = _resolve_device()
    try:
        detector = build_detector(
            DETECTOR_MODEL, _device, max_image_side=MAX_IMAGE_SIDE
        )
        detector.load()
    except Exception as exc:
        _load_error = f"{type(exc).__name__}: {exc}"
        logger.error(
            "_load_model: DETECTOR_MODEL=%s failed to load on %s — %s",
            DETECTOR_MODEL,
            _device,
            _load_error,
        )
        return
    _detector = detector
    _load_error = None


mcp = FastMCP("Open-Vocabulary Detector")
mcp_app = mcp.http_app(path="/")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load the weights once, at startup, off the event loop.

    The MCP app has its own lifespan (a session manager it must run), so ours
    wraps it rather than replacing it — same pattern as ai/kokoro's api
    container, which passes `mcp_app.lifespan` straight through.
    """
    logger.info(
        "detector starting — model=%s device=%s max_image_side=%d "
        "default_threshold=%.2f max_labels=%d",
        DETECTOR_MODEL,
        DETECTOR_DEVICE,
        MAX_IMAGE_SIDE,
        DEFAULT_THRESHOLD,
        MAX_LABELS,
    )
    await asyncio.to_thread(_load_model)
    async with mcp_app.lifespan(app):
        yield
    logger.info("detector shutting down")


app = FastAPI(
    title="Open-Vocabulary Detector",
    description=(
        "Text-prompted object detection. POST an image and a list of labels, "
        "get boxes in original image pixels. See ai/detector/DETECTOR.md."
    ),
    lifespan=lifespan,
)

Instrumentator().instrument(app).expose(app)


# ---------------------------------------------------------------------------
# Image loading
# ---------------------------------------------------------------------------


async def _fetch_url(url: str) -> bytes:
    """Fetch a caller-supplied image URL, after the SSRF check.

    Raises:
        HTTPException(400): Blocked or unresolvable URL.
        HTTPException(502): The remote host failed the fetch.
    """
    try:
        validate_url(url)
    except BlockedURLError as exc:
        logger.warning("_fetch_url: refused %s: %s", url[:120], exc)
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        async with httpx.AsyncClient(timeout=_http_timeout) as client:
            response = await client.get(url, headers={"User-Agent": "Detector/1.0"})
            response.raise_for_status()
            return response.content
    except httpx.HTTPError as exc:
        logger.error("_fetch_url: failed to fetch %s: %s", url[:120], exc)
        raise HTTPException(status_code=502, detail=f"Failed to fetch image URL: {exc}")


def _decode_image(raw: bytes) -> Image.Image:
    """Raw image bytes → an RGB PIL image in its true orientation.

    EXIF transpose matters here more than anywhere: a phone photo stored
    sideways would otherwise produce boxes that are correct for the stored
    pixels and wrong for the picture — and the classifier draws them over the
    page image, which IS transposed (analysis._decode_image_bgr does the
    same thing). The two have to agree about what "original pixels" means.

    Raises:
        HTTPException(400): Undecodable bytes, or an image too small to be
            worth running.
    """
    try:
        image = Image.open(io.BytesIO(raw))
        image = ImageOps.exif_transpose(image)
        image = image.convert("RGB")
    except Exception as exc:
        logger.error("_decode_image: failed to decode %d bytes: %s", len(raw), exc)
        raise HTTPException(status_code=400, detail=f"Failed to decode image: {exc}")

    width, height = image.size
    if min(width, height) < MIN_IMAGE_SIDE:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Image too small ({width}x{height} px). Minimum side is "
                f"{MIN_IMAGE_SIDE} px — below that the model's answers are noise."
            ),
        )
    return image


def _decode_base64(data: str) -> bytes:
    """Base64 string → bytes, tolerating a data: URL prefix.

    Raises:
        HTTPException(400): Not valid base64.
    """
    payload = data.split(",", 1)[1] if data.startswith("data:") else data
    try:
        return base64.b64decode(payload, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid base64 image: {exc}")


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def _require_detector():
    """The loaded detector, or a 503 that says why there isn't one."""
    if _detector is None:
        raise HTTPException(
            status_code=503,
            detail=(
                f"Detector model '{DETECTOR_MODEL}' is not loaded: "
                f"{_load_error or 'still loading'}. Check GET /health."
            ),
        )
    return _detector


async def _run_detection(
    image: Image.Image,
    labels: list[str],
    threshold: float,
    max_per_label: int | None,
) -> dict:
    """Run the model and shape the response.

    Steps:
      1. Take the inference lock — one image at a time, which is what bounds
         VRAM on a card shared with qwen3.8-solo.
      2. Run the (blocking) forward pass in a worker thread so the event loop
         keeps serving /health and /metrics while a long CPU-mode call runs.
      3. Group by label, apply `max_per_label`, and build the body.
    """
    detector = _require_detector()
    width, height = image.size

    started = time.perf_counter()
    async with _inference_lock:
        detections = await asyncio.to_thread(
            detector.detect, image, labels, threshold
        )
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    grouped = apply_max_per_label(detections, labels, max_per_label)
    logger.info(
        "detect: %dx%d, %d label(s), threshold=%.2f, device=%s -> %s in %.0f ms",
        width,
        height,
        len(labels),
        threshold,
        _device,
        {label: len(b) for label, b in grouped.items()},
        elapsed_ms,
    )
    return build_response(
        model=DETECTOR_MODEL,
        device=_device,
        family=detector.family,
        elapsed_ms=elapsed_ms,
        threshold=threshold,
        labels=labels,
        width=width,
        height=height,
        grouped=grouped,
    )


@app.post(
    "/detect",
    summary="Detect free-text labels in an image",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["file", "labels"],
                        "properties": {
                            "file": {"type": "string", "format": "binary"},
                            "labels": {
                                "type": "string",
                                "example": '["tree", "swimming pool"]',
                                "description": "JSON array or a comma list.",
                            },
                            "threshold": {"type": "number"},
                            "max_per_label": {"type": "integer"},
                        },
                    }
                },
                "application/json": {
                    "schema": DetectRequest.model_json_schema(
                        ref_template="#/components/schemas/{model}"
                    )
                },
            },
        }
    },
)
async def detect(request: Request):
    """Find every requested label in one image.

    Two spellings of the same request, chosen by Content-Type:

      * ``multipart/form-data`` — ``file`` (the image) plus ``labels``
        (a JSON array or a comma list), ``threshold``, ``max_per_label``.
      * ``application/json`` — ``{"image": {"data", "type"}, "labels": [...],
        "threshold": …, "max_per_label": …}`` where ``type`` is ``base64`` or
        ``url``. URLs are SSRF-checked before the fetch.

    Returns:
        ``model``, ``family``, ``device``, ``elapsed_ms``, the ``threshold``
        actually applied, ``image`` (the ORIGINAL size every box is in),
        ``counts``, ``by_label`` (every requested label, empty list when it
        found nothing), and ``detections`` (the same boxes flat, score order).

    Raises:
        HTTPException(400): Bad labels/threshold, undecodable image, blocked
            URL, or a body that is neither multipart nor JSON.
        HTTPException(502): An image URL that could not be fetched.
        HTTPException(503): The model is not loaded — see GET /health.
    """
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip()

    if content_type == "multipart/form-data":
        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            raise HTTPException(
                status_code=400,
                detail="multipart/form-data requires a 'file' part with the image.",
            )
        raw = await upload.read()
        if not raw:
            raise HTTPException(status_code=400, detail="Uploaded image was empty.")
        try:
            labels = parse_labels(form.get("labels"))
            threshold = parse_threshold(form.get("threshold"))
            max_per_label = parse_max_per_label(form.get("max_per_label"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    elif content_type == "application/json":
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=400, detail=f"Invalid JSON body: {exc}")
        try:
            parsed = DetectRequest.model_validate(body)
            labels = parse_labels(parsed.labels)
            threshold = parse_threshold(parsed.threshold)
            max_per_label = parse_max_per_label(parsed.max_per_label)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        raw = (
            _decode_base64(parsed.image.data)
            if parsed.image.type == "base64"
            else await _fetch_url(parsed.image.data)
        )
        if not raw:
            raise HTTPException(status_code=400, detail="Image input was empty.")

    else:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unsupported Content-Type '{content_type or 'none'}'. Send "
                "multipart/form-data with a 'file' part, or application/json "
                'with {"image": {"data", "type"}, "labels": [...]}.'
            ),
        )

    image = _decode_image(raw)
    return await _run_detection(image, labels, threshold, max_per_label)


@app.get("/health")
def health():
    """Liveness plus the one thing that actually matters: is the model resident?

    ``status`` is ``ok`` only when the weights are loaded. A container that is
    up with a failed load reports ``degraded`` and carries the loader's error,
    so the Docker healthcheck fails it rather than routing traffic to a
    service that can only answer 503.
    """
    loaded = _detector is not None
    body = {
        "status": "ok" if loaded else "degraded",
        "model": DETECTOR_MODEL,
        "family": _detector.family if loaded else None,
        "device": _device,
        "configured_device": DETECTOR_DEVICE,
        "loaded": loaded,
        "error": _load_error,
        "default_threshold": DEFAULT_THRESHOLD,
        "max_image_side": MAX_IMAGE_SIDE,
        "max_labels": MAX_LABELS,
    }
    return JSONResponse(content=body, status_code=200 if loaded else 503)


# ---------------------------------------------------------------------------
# MCP surface
# ---------------------------------------------------------------------------


@mcp.tool()
async def detect_objects(
    image: str,
    labels: list[str],
    threshold: float | None = None,
    max_per_label: int | None = None,
) -> str:
    """Find named things in an image and return where they are.

    Open-vocabulary: the labels are free text, not a fixed class list, so
    "swimming pool", "solar panel", or "roof vent" work as well as "car".
    Boxes come back in the image's own pixel coordinates, top-left origin.

    Args:
        image: An http(s) URL to the image, or its base64-encoded bytes.
            Prefer a URL — base64 of a photo is a very large tool argument.
        labels: What to look for, e.g. ["tree", "swimming pool"]. Cost is
            linear in the number of labels.
        threshold: Confidence floor 0-1. Omit for the service default (0.25).
            Raise it when you get spurious boxes, lower it when you get none.
        max_per_label: Keep at most this many boxes per label, best first.

    Returns:
        JSON: per-label boxes with scores, plus the image size the boxes are
        expressed in. An empty list for a label means "looked, found none".
    """
    try:
        parsed_labels = parse_labels(labels)
        parsed_threshold = parse_threshold(threshold)
        parsed_max = parse_max_per_label(max_per_label)
    except ValueError as exc:
        return json.dumps({"error": str(exc)})

    try:
        text = image.strip()
        raw = (
            await _fetch_url(text)
            if text.lower().startswith(("http://", "https://"))
            else _decode_base64(text)
        )
        pil = _decode_image(raw)
        result = await _run_detection(pil, parsed_labels, parsed_threshold, parsed_max)
    except HTTPException as exc:
        return json.dumps({"error": exc.detail, "status": exc.status_code})

    # The flat list is redundant with by_label and is the bulky half; a model
    # reading this wants "what, where, how sure" once.
    result.pop("detections", None)
    return json.dumps(result)


app.mount("/mcp", mcp_app)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
