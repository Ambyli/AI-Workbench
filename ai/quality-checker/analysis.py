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

_MAGIC_BYTES = {
    b'\xff\xd8\xff': "JPEG",
    b'\x89PNG\r\n\x1a\n': "PNG",
}


# ---------------------------------------------------------------------------
# Criteria parsing
# ---------------------------------------------------------------------------

def parse_criteria(raw: str) -> list[CriterionInput]:
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
# Image loading
# ---------------------------------------------------------------------------

def _validate_image_dimensions(w: int, h: int) -> None:
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
    """Reject data that doesn't start with a known image magic signature."""
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
    """Decode image bytes to a BGR numpy array.

    Validates magic bytes, corrects EXIF orientation via PIL, then converts
    to BGR for OpenCV. Falls back to direct cv2 decode if PIL fails.
    """
    import cv2
    logger.debug("_bytes_to_bgr: decoding %d bytes", len(raw))

    _validate_magic_bytes(raw)

    try:
        pil_img = Image.open(io.BytesIO(raw))
        pil_img = ImageOps.exif_transpose(pil_img)  # correct camera orientation
        rgb = np.array(pil_img.convert("RGB"))
        bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        logger.debug("_bytes_to_bgr: PIL decode + EXIF correction succeeded shape=%s", bgr.shape)
    except Exception as exc:
        logger.warning("_bytes_to_bgr: PIL EXIF correction failed (%s), falling back to cv2", exc)
        nparr = np.frombuffer(raw, np.uint8)
        bgr = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

    if bgr is None:
        logger.error("_bytes_to_bgr: failed to decode image from %d bytes", len(raw))
        raise HTTPException(status_code=400, detail="Failed to decode image.")

    logger.debug("_bytes_to_bgr: returning image shape=%s", bgr.shape)
    return bgr


async def _load_bgr_from_input(data: str, type_: str):
    data_repr = data[:80] if type_ == "url" else f"base64[{len(data)} chars]"
    logger.debug("_load_bgr_from_input: type=%s data=%s", type_, data_repr)

    if type_ == "base64":
        try:
            raw = base64.b64decode(data)
        except Exception as exc:
            logger.error("_load_bgr_from_input: invalid base64 data: %s", exc)
            raise HTTPException(status_code=400, detail=f"Invalid base64 data: {exc}")
    else:
        validate_url(data)  # SSRF check before fetching
        try:
            async with httpx.AsyncClient(timeout=_http_timeout) as client:
                r = await client.get(data, headers={"User-Agent": "QualityChecker/1.0"})
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
    import cv2
    logger.info("analyze_bgr: image=%dx%d content_type=%s size=%d bytes criteria=%s",
                original_w, original_h, content_type, size_bytes, [c.name for c in criteria])

    _validate_image_dimensions(original_w, original_h)

    max_dim = 1000
    if max(original_h, original_w) > max_dim:
        scale = max_dim / max(original_h, original_w)
        image_bgr = cv2.resize(
            image_bgr, (int(original_w * scale), int(original_h * scale)),
            interpolation=cv2.INTER_AREA,
        )
        logger.debug("analyze_bgr: resized to %s", image_bgr.shape)

    cv_results = {
        "sharpness": check_blur(image_bgr),
        "exposure": check_exposure(image_bgr),
    }
    logger.debug("analyze_bgr: cv sharpness=%s exposure=%s",
                 cv_results["sharpness"]["verdict"], cv_results["exposure"]["verdict"])

    image_b64 = encode_image_to_base64(image_bgr)
    llm_result = await call_vllm(build_llm_prompt(image_b64, criteria))
    assessment = validate_and_clamp(llm_result.get("assessment", {}), criteria)

    per_criterion = assessment.get("per_criterion_scores", {})
    merged = [k for k in cv_results if k not in per_criterion]
    for k in merged:
        per_criterion[k] = cv_results[k]
    if merged:
        logger.debug("analyze_bgr: merged CV results for: %s", merged)

    cv_failures = sum(1 for r in cv_results.values() if r["verdict"] == "FAIL")
    cv_verdict = "FAIL" if cv_failures >= 2 else ("MARGINAL" if cv_failures == 1 else "PASS")

    result = {
        "image_info": {
            "width": original_w, "height": original_h,
            "format": content_type, "size_bytes": size_bytes,
        },
        "cv_pre_checks": cv_results,
        "cv_overall_verdict": cv_verdict,
        "llm_assessment": assessment,
        "combined_verdict": cv_verdict if not assessment.get("overall_verdict") else assessment["overall_verdict"],
    }
    logger.info("analyze_bgr: returning combined_verdict=%s cv=%s llm=%s",
                result["combined_verdict"], cv_verdict, assessment.get("overall_verdict"))
    return result


async def analyze_upload(upload: UploadFile, criteria: list[CriterionInput]) -> dict:
    logger.info("analyze_upload: filename=%s content_type=%s criteria=%s",
                upload.filename, upload.content_type, [c.name for c in criteria])
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
    data_repr = img.data[:80] if img.type == "url" else f"base64[{len(img.data)} chars]"
    logger.info("analyze_input: type=%s data=%s criteria=%s",
                img.type, data_repr, [c.name for c in criteria])
    bgr = await _load_bgr_from_input(img.data, img.type)
    h, w = bgr.shape[:2]
    size = len(base64.b64decode(img.data)) if img.type == "base64" else 0
    result = await analyze_bgr(bgr, w, h, "image/jpeg", size, criteria)
    logger.info("analyze_input: returning combined_verdict=%s", result["combined_verdict"])
    return result


async def resolve_example(example: ExampleInput, criteria: list[CriterionInput]) -> dict:
    pre_generated = example.pre_generated_analysis is not None
    logger.info("resolve_example: type=%s weight=%s pre_generated=%s criteria=%s",
                example.type, example.weight, pre_generated, [c.name for c in criteria])
    if pre_generated:
        logger.info("resolve_example: using pre-generated analysis, skipping LLM call")
        return example.pre_generated_analysis
    result = await analyze_input(ImageInput(data=example.data, type=example.type), criteria)
    logger.info("resolve_example: returning combined_verdict=%s", result["combined_verdict"])
    return result
