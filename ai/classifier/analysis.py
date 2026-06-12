"""Core image analysis pipeline.

This module orchestrates the full assessment flow for a single image:

  parse_criteria()          — deserialise the criteria JSON string from a
                              multipart form field into CriterionInput objects.

  _validate_image_dimensions() — reject images that are too small to assess.
  _validate_magic_bytes()   — verify the file is actually a JPEG or PNG.
  _bytes_to_bgr()           — decode raw bytes → BGR numpy array, applying
                              EXIF orientation correction via PIL.
  _load_bgr_from_input()    — load from base64 or URL (SSRF-checked).

  analyze_bgr()             — the central pipeline:
                                1. Validate dimensions
                                2. Resize to ≤1000px
                                3. Run CV pre-checks (blur + exposure)
                                4. Encode to base64 for LLM
                                5. Call LLM and validate/clamp response
                                6. Build final result dict

  analyze_upload()          — thin wrapper for multipart UploadFile inputs.
  analyze_input()           — thin wrapper for ImageInput (JSON body) inputs.
  resolve_example()         — return a pre-generated analysis or analyse live;
                              used in /assess/compare to avoid redundant calls.

Process flow position: called by workers.py (_run_assess, _run_compare) after
the job is dequeued.
"""

import base64
import io
import json

import httpx
import numpy as np
from fastapi import HTTPException, UploadFile
from PIL import Image, ImageOps

from config import MIN_IMAGE_WIDTH, MIN_IMAGE_HEIGHT, HTTP_TIMEOUT, HTTP_CONNECT_TIMEOUT
from logger import logger
from models import CriterionInput, ImageInput, ExampleInput
from cv import check_blur, check_exposure
from llm import encode_image_to_base64, build_llm_prompt, call_vllm, validate_and_clamp
from ssrf import validate_url

_http_timeout = httpx.Timeout(HTTP_TIMEOUT, connect=HTTP_CONNECT_TIMEOUT)

# Known JPEG and PNG file signatures (magic bytes at the start of the file)
_MAGIC_BYTES = {
    b'\xff\xd8\xff': "JPEG",
    b'\x89PNG\r\n\x1a\n': "PNG",
}


# ---------------------------------------------------------------------------
# Criteria parsing
# ---------------------------------------------------------------------------

def parse_criteria(raw: str) -> list[CriterionInput]:
    """Parse and validate the criteria JSON string from a multipart form field.

    The /assess endpoint receives criteria as a JSON string (multipart forms
    cannot carry structured objects natively).  This function converts it into
    a typed list of CriterionInput objects.

    Args:
        raw: JSON string, e.g. '[{"name":"sharpness","type":"quality","weight":1.0}]'

    Returns:
        List of validated CriterionInput objects.

    Raises:
        HTTPException(400): If the string is not valid JSON, not a list, or
            contains items that fail CriterionInput validation.
    """
    logger.info("parse_criteria: raw=%s", raw[:200])
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            raise ValueError("criteria must be a JSON array")
        result = [CriterionInput(**item) for item in parsed]
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.error("parse_criteria: failed to parse criteria: %s", exc)
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid criteria JSON: {exc}. "
                "Expected a JSON array of objects, e.g. "
                '[{"name": "image sharpness", "type": "quality"}]'
            ),
        )
    logger.info("parse_criteria: returning %d criteria: %s", len(result), [c.name for c in result])
    return result


# ---------------------------------------------------------------------------
# Image loading helpers
# ---------------------------------------------------------------------------

def _validate_image_dimensions(w: int, h: int) -> None:
    """Reject images that are too small to produce meaningful assessments.

    Images below MIN_IMAGE_WIDTH × MIN_IMAGE_HEIGHT pixels cannot provide
    enough detail for reliable LLM scoring and are refused early.

    Args:
        w, h: Image width and height in pixels.

    Raises:
        HTTPException(400): If either dimension is below the configured minimum.
    """
    logger.debug("_validate_image_dimensions: w=%d h=%d (min %dx%d)",
                 w, h, MIN_IMAGE_WIDTH, MIN_IMAGE_HEIGHT)
    if w < MIN_IMAGE_WIDTH or h < MIN_IMAGE_HEIGHT:
        logger.warning("_validate_image_dimensions: image too small (%dx%d)", w, h)
        raise HTTPException(
            status_code=400,
            detail=f"Image too small ({w}×{h} px). Minimum is {MIN_IMAGE_WIDTH}×{MIN_IMAGE_HEIGHT} px.",
        )
    logger.debug("_validate_image_dimensions: dimensions valid")


def _validate_magic_bytes(raw: bytes) -> None:
    """Confirm the file starts with a known JPEG or PNG magic signature.

    Trusting Content-Type headers alone is insufficient — any file can be
    uploaded with a spoofed header.  Checking the first bytes confirms the
    actual format before passing data to OpenCV or PIL.

    Args:
        raw: Raw file bytes.

    Raises:
        HTTPException(400): If no known magic signature is found.
    """
    logger.debug("_validate_magic_bytes: checking %d bytes", len(raw))
    for magic in _MAGIC_BYTES:
        if raw[:len(magic)] == magic:
            logger.debug("_validate_magic_bytes: valid %s signature", _MAGIC_BYTES[magic])
            return
    logger.warning("_validate_magic_bytes: unrecognised file signature: %s", raw[:8].hex())
    raise HTTPException(
        status_code=400,
        detail="File does not appear to be a valid JPEG or PNG image.",
    )


async def _bytes_to_bgr(raw: bytes):
    """Decode raw image bytes to a BGR numpy array suitable for OpenCV.

    Steps:
      1. Validate magic bytes (JPEG/PNG check).
      2. Open with PIL and apply EXIF orientation correction.
         Phone cameras embed orientation metadata; without this step a portrait
         photo may load sideways, producing wrong CV scores.
      3. Convert PIL RGB array to OpenCV BGR format.
      4. Fall back to direct cv2.imdecode() if PIL fails for any reason.

    Args:
        raw: Raw JPEG or PNG file bytes.

    Returns:
        BGR numpy array (H×W×3).

    Raises:
        HTTPException(400): If magic bytes are invalid or decoding fails entirely.
    """
    import cv2
    logger.debug("_bytes_to_bgr: decoding %d bytes", len(raw))

    # Step 1 — reject obviously wrong file formats early
    _validate_magic_bytes(raw)

    try:
        # Step 2 — PIL handles EXIF orientation (cv2 does not)
        pil_img = Image.open(io.BytesIO(raw))
        pil_img = ImageOps.exif_transpose(pil_img)  # rotate to match camera orientation
        # Step 3 — convert to BGR for all downstream OpenCV operations
        rgb = np.array(pil_img.convert("RGB"))
        bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        logger.debug("_bytes_to_bgr: PIL decode + EXIF correction succeeded shape=%s", bgr.shape)
    except Exception as exc:
        # Step 4 — PIL failed; fall back to cv2 (no EXIF correction)
        logger.warning("_bytes_to_bgr: PIL EXIF correction failed (%s), falling back to cv2", exc)
        nparr = np.frombuffer(raw, np.uint8)
        bgr = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

    if bgr is None:
        logger.error("_bytes_to_bgr: failed to decode image from %d bytes", len(raw))
        raise HTTPException(status_code=400, detail="Failed to decode image.")

    logger.debug("_bytes_to_bgr: returning image shape=%s", bgr.shape)
    return bgr


async def _load_bgr_from_input(data: str, type_: str):
    """Load a BGR numpy array from a base64 string or a remote URL.

    For URL inputs: performs an SSRF check before fetching (ssrf.validate_url)
    and sets a descriptive User-Agent to avoid 403 responses from servers that
    block default request libraries.

    Args:
        data:  Base64 string or URL string.
        type_: "base64" or "url".

    Returns:
        BGR numpy array.

    Raises:
        HTTPException(400): Invalid base64 data or SSRF-blocked URL.
        HTTPException(502): HTTP error while fetching the URL.
    """
    data_repr = data[:80] if type_ == "url" else f"base64[{len(data)} chars]"
    logger.debug("_load_bgr_from_input: type=%s data=%s", type_, data_repr)

    if type_ == "base64":
        # Decode the base64 payload directly — no network call needed
        try:
            raw = base64.b64decode(data)
        except Exception as exc:
            logger.error("_load_bgr_from_input: invalid base64 data: %s", exc)
            raise HTTPException(status_code=400, detail=f"Invalid base64 data: {exc}")
    else:
        # SSRF check must pass before we fetch anything
        validate_url(data)
        try:
            async with httpx.AsyncClient(timeout=_http_timeout) as client:
                r = await client.get(data, headers={"User-Agent": "Classifier/1.0"})
                r.raise_for_status()
                raw = r.content
            logger.debug("_load_bgr_from_input: fetched %d bytes from URL", len(raw))
        except httpx.HTTPError as exc:
            logger.error("_load_bgr_from_input: failed to fetch URL '%s': %s", data[:80], exc)
            raise HTTPException(status_code=502, detail=f"Failed to fetch image URL: {exc}")

    bgr = await _bytes_to_bgr(raw)
    logger.debug("_load_bgr_from_input: returning image shape=%s", bgr.shape)
    return bgr


# ---------------------------------------------------------------------------
# Core analysis pipeline
# ---------------------------------------------------------------------------

async def analyze_bgr(
    image_bgr,
    original_w: int,
    original_h: int,
    content_type: str,
    size_bytes: int,
    criteria: list[CriterionInput],
) -> dict:
    """Run the full assessment pipeline on a BGR numpy array.

    This is the central function that all entry points (analyze_upload,
    analyze_input) ultimately call.

    Pipeline steps:
      1. Validate image dimensions (reject too-small images).
      2. Resize to ≤1000px on the long side for LLM efficiency.
      3. Run deterministic CV pre-checks (blur + exposure).
      4. Encode image as base64 JPEG for the LLM prompt.
      5. Call vLLM and validate/clamp the response.
      6. Compute the cv_overall_verdict from CV failures.
      7. Assemble and return the final result dict.

    Args:
        image_bgr:   BGR numpy array of the (already decoded) image.
        original_w:  Original image width before any resizing.
        original_h:  Original image height before any resizing.
        content_type: MIME type string (e.g. "image/jpeg") for the response.
        size_bytes:  Original file size in bytes for the response.
        criteria:    List of CriterionInput objects defining what to assess.

    Returns:
        dict with image_info, cv_pre_checks, cv_overall_verdict,
        llm_assessment, and combined_verdict.
    """
    import cv2
    logger.info("analyze_bgr: image=%dx%d content_type=%s size=%d bytes criteria=%s",
                original_w, original_h, content_type, size_bytes, [c.name for c in criteria])

    # Step 1 — reject images that are too small for reliable assessment
    _validate_image_dimensions(original_w, original_h)

    # Step 2 — resize to ≤1000px so the LLM prompt stays within token limits
    max_dim = 1000
    if max(original_h, original_w) > max_dim:
        scale = max_dim / max(original_h, original_w)
        image_bgr = cv2.resize(
            image_bgr, (int(original_w * scale), int(original_h * scale)),
            interpolation=cv2.INTER_AREA,
        )
        logger.debug("analyze_bgr: resized to %s", image_bgr.shape)

    # Step 3 — fast deterministic CV checks (always run, regardless of criteria)
    cv_results = {
        "sharpness": check_blur(image_bgr),
        "exposure": check_exposure(image_bgr),
    }
    logger.debug("analyze_bgr: cv sharpness=%s exposure=%s",
                 cv_results["sharpness"]["verdict"], cv_results["exposure"]["verdict"])

    # Step 4 — encode resized image for the LLM prompt
    image_b64 = encode_image_to_base64(image_bgr)

    # Step 5 — call LLM and validate/clamp the response
    llm_result = await call_vllm(build_llm_prompt(image_b64, criteria))
    assessment = validate_and_clamp(llm_result.get("assessment", {}), criteria)

    # Step 6 — derive a CV-level overall verdict from the two CV checks
    # (both fail → FAIL, one fails → MARGINAL, neither fails → PASS)
    cv_failures = sum(1 for r in cv_results.values() if r["verdict"] == "FAIL")
    cv_verdict = "FAIL" if cv_failures >= 2 else ("MARGINAL" if cv_failures == 1 else "PASS")

    # Step 7 — assemble the final response dict
    # combined_verdict prefers the LLM's verdict; falls back to cv_verdict if absent
    result = {
        "image_info": {
            "width": original_w, "height": original_h,
            "format": content_type, "size_bytes": size_bytes,
        },
        "cv_pre_checks": cv_results,          # raw CV measurements
        "cv_overall_verdict": cv_verdict,     # PASS/MARGINAL/FAIL from CV only
        "llm_assessment": assessment,         # validated LLM scores per criterion
        "combined_verdict": cv_verdict if not assessment.get("overall_verdict")
                            else assessment["overall_verdict"],
    }
    logger.info("analyze_bgr: returning combined_verdict=%s cv=%s llm=%s",
                result["combined_verdict"], cv_verdict, assessment.get("overall_verdict"))
    return result


async def analyze_upload(upload: UploadFile, criteria: list[CriterionInput]) -> dict:
    """Entry point for multipart file uploads (POST /assess via form data).

    Reads the uploaded file, validates the content type, decodes the bytes,
    and delegates to analyze_bgr().

    Args:
        upload:   FastAPI UploadFile from a multipart/form-data request.
        criteria: Parsed list of CriterionInput objects.

    Returns:
        Analysis result dict from analyze_bgr().
    """
    logger.info("analyze_upload: filename=%s content_type=%s criteria=%s",
                upload.filename, upload.content_type, [c.name for c in criteria])

    # Validate content type before reading the entire file into memory
    if not upload.content_type or upload.content_type.split("/")[1] not in ("jpeg", "jpg", "png"):
        raise HTTPException(status_code=400, detail=f"Only JPEG/PNG accepted (got {upload.content_type})")

    contents = await upload.read()
    if not contents:
        raise HTTPException(status_code=400, detail="Empty image file")

    logger.debug("analyze_upload: read %d bytes", len(contents))
    bgr = await _bytes_to_bgr(contents)
    h, w = bgr.shape[:2]
    result = await analyze_bgr(bgr, w, h, upload.content_type, len(contents), criteria)
    logger.info("analyze_upload: returning combined_verdict=%s", result["combined_verdict"])
    return result


async def analyze_input(img: ImageInput, criteria: list[CriterionInput]) -> dict:
    """Entry point for ImageInput objects from a JSON request body.

    Loads the image from a base64 string or URL and delegates to analyze_bgr().

    Args:
        img:      ImageInput with data and type fields.
        criteria: List of CriterionInput objects.

    Returns:
        Analysis result dict from analyze_bgr().
    """
    data_repr = img.data[:80] if img.type == "url" else f"base64[{len(img.data)} chars]"
    logger.info("analyze_input: type=%s data=%s criteria=%s",
                img.type, data_repr, [c.name for c in criteria])
    bgr = await _load_bgr_from_input(img.data, img.type)
    h, w = bgr.shape[:2]
    # Calculate size from base64 length (3 base64 chars ≈ 2 bytes)
    size = len(base64.b64decode(img.data)) if img.type == "base64" else 0
    result = await analyze_bgr(bgr, w, h, "image/jpeg", size, criteria)
    logger.info("analyze_input: returning combined_verdict=%s", result["combined_verdict"])
    return result


async def resolve_example(example: ExampleInput, criteria: list[CriterionInput]) -> dict:
    """Return the analysis for a reference example, live or pre-generated.

    Used in /assess/compare to obtain an analysis for each reference image.
    If pre_generated_analysis is provided, it is returned immediately without
    any LLM call — this is the recommended pattern for stable reference images
    to avoid redundant token usage.

    Args:
        example:  ExampleInput including the image and optional prior analysis.
        criteria: The criteria to apply if a live analysis is needed.

    Returns:
        Analysis result dict (same shape as analyze_bgr output).
    """
    pre_generated = example.pre_generated_analysis is not None
    logger.info("resolve_example: type=%s weight=%s pre_generated=%s criteria=%s",
                example.type, example.weight, pre_generated, [c.name for c in criteria])

    if pre_generated:
        # Skip the LLM entirely — use the cached result
        logger.info("resolve_example: using pre-generated analysis, skipping LLM call")
        return example.pre_generated_analysis

    # Analyse live — same path as a regular /assess call
    result = await analyze_input(ImageInput(data=example.data, type=example.type), criteria)
    logger.info("resolve_example: returning combined_verdict=%s", result["combined_verdict"])
    return result
