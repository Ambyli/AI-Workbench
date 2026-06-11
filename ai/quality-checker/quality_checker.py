"""Document Quality Assessment Service.

Accepts multipart image uploads, runs OpenCV pre-checks, then delegates
to Qwen2.5-VL-7B for criterion-based scoring via the vLLM OpenAI-compatible API.
"""

import base64
import io
import json
import os
import re
from typing import Optional

import httpx
import numpy as np
from fastapi import FastAPI, HTTPException, UploadFile, Form
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

VLLM_QWEN_VL_API = os.environ.get(
    "VLLM_QWEN_VL_API", "http://vllm-qwen-vl:8000/v1/chat/completions"
)

# OpenCV blur detection thresholds (Laplacian variance)
BLUR_THRESHOLD = 100.0
EXPOSURE_LOW = 30.0
EXPOSURE_HIGH = 220.0

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Document Quality Checker")

http_timeout = httpx.Timeout(120.0, connect=10.0)


# ---------------------------------------------------------------------------
# CV Pre-checks
# ---------------------------------------------------------------------------

def check_blur(image) -> dict:
    """Detect blur using Laplacian variance."""
    import cv2

    if len(image.shape) == 3:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    else:
        gray = image

    variance = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    passed = variance >= BLUR_THRESHOLD

    return {
        "criterion": "sharpness",
        "score": min(10, int(10 * min(variance / (BLUR_THRESHOLD * 3), 1.0))),
        "verdict": "PASS" if passed else "FAIL",
        "detail": f"Laplacian variance: {variance:.1f} (threshold: {BLUR_THRESHOLD})",
    }


def check_exposure(image) -> dict:
    """Check overall exposure via mean pixel intensity."""
    import cv2

    if len(image.shape) == 3:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    else:
        gray = image

    mean_intensity = float(np.mean(gray))
    if mean_intensity < EXPOSURE_LOW:
        verdict = "FAIL"
        detail = f"Underexposed (mean: {mean_intensity:.1f}, min: {EXPOSURE_LOW})"
        score = 2
    elif mean_intensity > EXPOSURE_HIGH:
        verdict = "FAIL"
        detail = f"Overexposed (mean: {mean_intensity:.1f}, max: {EXPOSURE_HIGH})"
        score = 2
    else:
        verdict = "PASS"
        score = int(1 + 9 * (mean_intensity - EXPOSURE_LOW) / (EXPOSURE_HIGH - EXPOSURE_LOW))
        detail = f"Normal exposure (mean: {mean_intensity:.1f})"

    return {
        "criterion": "exposure",
        "score": score,
        "verdict": verdict,
        "detail": detail,
    }


# ---------------------------------------------------------------------------
# LLM scoring via Qwen2.5-VL-7B
# ---------------------------------------------------------------------------

def encode_image_to_base64(image) -> str:
    """Encode a numpy image array to base64 JPEG string."""
    import cv2

    _, buf = cv2.imencode(".jpg", image)
    return base64.b64encode(buf).decode("utf-8")


def build_llm_prompt(image_b64: str, criteria: list[str]) -> dict:
    """Build the OpenAI-compatible chat completion request for Qwen2.5-VL."""
    criteria_text = "\n".join(f"- {c}" for c in criteria)

    system_prompt = (
        "You are a document quality assessment expert. "
        "Analyze the provided image and score it against each criterion. "
        "Return ONLY a valid JSON object with this exact structure:\n"
        '{"assessment": {'
        '"overall_verdict": "PASS" | "FAIL" | "MARGINAL",'
        '"overall_score": <1-10>,'
        '"per_criterion_scores": {'
        '"<criterion_name>": {"score": <1-10>, "verdict": "PASS" | "FAIL", "reason": "<string>"},'
        '...}}}\n'
        "Scoring rubric: 1-3 = FAIL, 4-6 = MARGINAL, 7-10 = PASS."
        "Be specific in your reasoning."
    )

    user_content = [
        {
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
        },
        {
            "type": "text",
            "text": (
                f"Assess this document against the following criteria:\n"
                f"{criteria_text}\n\n"
                f"Return your assessment as JSON."
            ),
        },
    ]

    return {
        "model": "Qwen/Qwen2.5-VL-7B-Instruct",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "max_tokens": 2048,
        "temperature": 0.1,
    }


def call_vllm(prompt: dict) -> dict:
    """Call the vLLM OpenAI-compatible API and parse the response."""
    try:
        response = httpx.post(
            VLLM_QWEN_VL_API,
            json=prompt,
            timeout=http_timeout,
        )
        response.raise_for_status()
        data = response.json()

        content = data["choices"][0]["message"]["content"]

        # Strip markdown fences if present
        json_match = re.search(r"\{[\s\S]*\}", content)
        if json_match:
            return json.loads(json_match.group())
        else:
            raise ValueError(f"No JSON found in LLM response: {content[:200]}")

    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"vLLM call failed: {exc}")
    except (KeyError, IndexError) as exc:
        raise HTTPException(status_code=502, detail=f"Unexpected vLLM response format: {exc}")
    except (json.JSONDecodeError, ValueError) as exc:
        return {
            "assessment": {
                "overall_verdict": "FAIL",
                "overall_score": 1,
                "per_criterion_scores": {
                    "_llm_error": {
                        "score": 1,
                        "verdict": "FAIL",
                        "reason": f"LLM parsing failed: {exc}",
                    }
                },
            }
        }


# ---------------------------------------------------------------------------
# API Endpoint
# ---------------------------------------------------------------------------

@app.post("/assess")
async def assess_document(
    image: UploadFile,
    criteria: str = Form(
        default="document legibility, image sharpness, proper exposure, absence of artifacts"
    ),
):
    """Assess document quality via CV pre-checks + LLM scoring.

    Accepts a multipart form with:
      - image: An image file (JPEG/PNG)
      - criteria: Comma-separated list of quality criteria

    Returns structured JSON with per-criterion scores and verdicts.
    """
    import cv2

    # Validate content type
    if not image.content_type or image.content_type.split("/")[1] not in ("jpeg", "jpg", "png"):
        raise HTTPException(status_code=400, detail="Only JPEG and PNG images are accepted")

    # Read image bytes
    contents = await image.read()
    if len(contents) == 0:
        raise HTTPException(status_code=400, detail="Empty image file")

    # Decode with OpenCV
    nparr = np.frombuffer(contents, np.uint8)
    image_bgr = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if image_bgr is None:
        raise HTTPException(status_code=400, detail="Failed to decode image")

    original_h, original_w = image_bgr.shape[:2]

    # Scale down for LLM (vision models handle ~1000px tiles well)
    max_dim = 1000
    if max(original_h, original_w) > max_dim:
        scale = max_dim / max(original_h, original_w)
        new_w, new_h = int(original_w * scale), int(original_h * scale)
        image_bgr = cv2.resize(image_bgr, (new_w, new_h), interpolation=cv2.INTER_AREA)

    # Parse criteria
    criterion_list = [c.strip() for c in criteria.split(",") if c.strip()]
    if not criterion_list:
        raise HTTPException(status_code=400, detail="At least one criterion is required")

    # Run CV pre-checks
    cv_results = {
        "sharpness": check_blur(image_bgr),
        "exposure": check_exposure(image_bgr),
    }

    # Encode for LLM
    image_b64 = encode_image_to_base64(image_bgr)

    # Call LLM
    llm_prompt = build_llm_prompt(image_b64, criterion_list)
    llm_result = call_vllm(llm_prompt)

    # Merge CV results into LLM per_criterion_scores
    assessment = llm_result.get("assessment", {})
    per_criterion = assessment.get("per_criterion_scores", {})

    for cv_name, cv_result in cv_results.items():
        if cv_name not in per_criterion:
            per_criterion[cv_name] = cv_result

    # Compute overall verdict from CV results
    cv_failures = sum(1 for r in cv_results.values() if r["verdict"] == "FAIL")
    if cv_failures >= 2:
        cv_verdict = "FAIL"
    elif cv_failures == 1:
        cv_verdict = "MARGINAL"
    else:
        cv_verdict = "PASS"

    # Build final response
    response = {
        "status": "ok",
        "image_info": {
            "width": original_w,
            "height": original_h,
            "format": image.content_type,
            "size_bytes": len(contents),
        },
        "cv_pre_checks": cv_results,
        "cv_overall_verdict": cv_verdict,
        "llm_assessment": assessment,
        "combined_verdict": cv_verdict
        if not assessment.get("overall_verdict")
        else assessment["overall_verdict"],
    }

    return JSONResponse(content=response)


@app.get("/health")
def health():
    return {"status": "ok"}
