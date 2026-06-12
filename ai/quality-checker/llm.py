import base64
import difflib
import json
import re

import httpx
from fastapi import HTTPException
from prometheus_client import Counter, Histogram

from config import VLLM_QWEN_VL_API, MAX_LLM_RETRIES, HTTP_TIMEOUT, HTTP_CONNECT_TIMEOUT
from logger import logger
from models import CriterionInput

_http_timeout = httpx.Timeout(HTTP_TIMEOUT, connect=HTTP_CONNECT_TIMEOUT)

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------

llm_calls_total = Counter(
    "quality_checker_llm_calls_total",
    "Total LLM API calls by status",
    ["status"],  # success | retry | failed
)
llm_latency = Histogram(
    "quality_checker_llm_latency_seconds",
    "LLM API call latency in seconds",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def encode_image_to_base64(image) -> str:
    import cv2
    logger.debug("encode_image_to_base64: image shape=%s", image.shape)
    _, buf = cv2.imencode(".jpg", image)
    result = base64.b64encode(buf).decode("utf-8")
    logger.debug("encode_image_to_base64: returning base64[%d chars]", len(result))
    return result


def _verdict_from_score(score: int) -> str:
    logger.debug("_verdict_from_score: score=%s", score)
    verdict = "PASS" if score >= 7 else ("MARGINAL" if score >= 4 else "FAIL")
    logger.debug("_verdict_from_score: returning %s", verdict)
    return verdict


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------

def _build_scaffold(criteria: list[CriterionInput]) -> str:
    """Build a pre-filled JSON scaffold with all criterion names already as keys.

    The model fills in values rather than inventing keys, preventing it from
    merging or renaming criteria.
    """
    per_criterion = {
        c.name: {"score": 0, "verdict": "...", "confidence": 0, "reason": "..."}
        for c in criteria
    }
    return json.dumps(
        {"assessment": {"overall_verdict": "...", "overall_score": 0,
                        "per_criterion_scores": per_criterion}},
        indent=2,
    )


def build_llm_prompt(image_b64: str, criteria: list[CriterionInput]) -> dict:
    logger.debug("build_llm_prompt: image_b64[%d chars] criteria=%s",
                 len(image_b64), [c.name for c in criteria])

    quality_criteria = [c for c in criteria if c.type == "quality"]
    feature_criteria = [c for c in criteria if c.type == "feature"]

    # --- Criteria descriptions ---
    sections = []
    if quality_criteria:
        names = "\n".join(f"  - {c.name}" for c in quality_criteria)
        sections.append(
            "QUALITY criteria — score image quality on a 1-10 scale:\n"
            "  Rubric: 1-3 = FAIL, 4-6 = MARGINAL, 7-10 = PASS\n"
            f"{names}"
        )
    if feature_criteria:
        names = "\n".join(f"  - {c.name}" for c in feature_criteria)
        sections.append(
            "FEATURE criteria — detect whether each feature is present:\n"
            "  Rubric: 10 = clearly present (PASS), 5 = uncertain or partially present (MARGINAL), "
            "1 = clearly absent (FAIL)\n"
            "  For each feature criterion, your 'reason' MUST follow this structure:\n"
            "    'I observe [specific visual evidence]. Therefore, [feature] is [present/absent/uncertain].'\n"
            f"{names}"
        )
    criteria_text = "\n\n".join(sections)

    # --- Pre-filled scaffold (improvement 1) ---
    scaffold = _build_scaffold(criteria)

    # --- Explicit key list (improvement 2) ---
    key_list = ", ".join(f'"{c.name}"' for c in criteria)
    n = len(criteria)

    # --- User message combining all four improvements ---
    user_text = (
        f"{criteria_text}\n\n"
        "Fill in the following JSON structure. "
        "The keys in per_criterion_scores are already defined — "
        "do NOT change, rename, merge, or add any keys:\n\n"
        f"{scaffold}\n\n"
        f"Required keys in per_criterion_scores ({n} total): {key_list}\n\n"
        # Improvement 4: verification step
        f"Before returning, verify your JSON contains exactly those {n} keys in "
        "per_criterion_scores — no more, no fewer, with names spelled exactly as shown. "
        "If any key is missing or renamed, revise before responding."
    )

    # --- System prompt with "do not group" instruction (improvement 3) ---
    system_prompt = (
        "You are an image assessment expert. "
        "Analyze the provided image and score it against each criterion listed below. "
        "Score each criterion independently — do NOT group multiple criteria under a "
        "single key or summarise them together. "
        "Set confidence to a number 0-100: 0 = completely uncertain, 100 = completely certain. "
        "Return ONLY a valid JSON object."
    )

    prompt = {
        "model": "Qwen/Qwen2.5-VL-7B-Instruct",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                {"type": "text", "text": user_text},
            ]},
        ],
        "max_tokens": 2048,
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    logger.debug("build_llm_prompt: returning prompt — %d quality + %d feature criteria, scaffold keys=%s",
                 len(quality_criteria), len(feature_criteria), [c.name for c in criteria])
    return prompt


# ---------------------------------------------------------------------------
# vLLM call with retry
# ---------------------------------------------------------------------------

async def call_vllm(prompt: dict) -> dict:
    import time
    logger.info("call_vllm: posting to %s (max_retries=%d)", VLLM_QWEN_VL_API, MAX_LLM_RETRIES)

    last_exc: Exception | None = None

    for attempt in range(MAX_LLM_RETRIES):
        logger.info("call_vllm: attempt %d/%d", attempt + 1, MAX_LLM_RETRIES)
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=_http_timeout) as client:
                response = await client.post(VLLM_QWEN_VL_API, json=prompt)
                response.raise_for_status()
                data = response.json()

            elapsed = time.monotonic() - t0
            llm_latency.observe(elapsed)
            content = data["choices"][0]["message"]["content"]
            logger.debug("call_vllm: response[%d chars] in %.2fs", len(content), elapsed)

            try:
                result = json.loads(content)
            except json.JSONDecodeError:
                json_match = re.search(r"\{[\s\S]*\}", content)
                if not json_match:
                    raise ValueError(f"No JSON found in response: {content[:200]}")
                result = json.loads(json_match.group())

            if "assessment" not in result:
                raise ValueError(f"Response missing 'assessment' key: {content[:200]}")

            llm_calls_total.labels(status="success").inc()
            verdict = result.get("assessment", {}).get("overall_verdict", "unknown")
            score = result.get("assessment", {}).get("overall_score", "unknown")
            logger.info("call_vllm: returning overall_verdict=%s overall_score=%s", verdict, score)
            return result

        except httpx.HTTPError as exc:
            llm_calls_total.labels(status="failed").inc()
            logger.error("call_vllm: HTTP error on attempt %d: %s", attempt + 1, exc)
            raise HTTPException(status_code=502, detail=f"vLLM call failed: {exc}")
        except (KeyError, IndexError) as exc:
            llm_calls_total.labels(status="failed").inc()
            logger.error("call_vllm: unexpected response format on attempt %d: %s", attempt + 1, exc)
            raise HTTPException(status_code=502, detail=f"Unexpected vLLM response format: {exc}")
        except (json.JSONDecodeError, ValueError) as exc:
            llm_calls_total.labels(status="retry").inc()
            logger.warning("call_vllm: parse failure on attempt %d: %s", attempt + 1, exc)
            last_exc = exc

    llm_calls_total.labels(status="failed").inc()
    logger.error("call_vllm: all %d attempts failed", MAX_LLM_RETRIES)
    return {
        "assessment": {
            "overall_verdict": "FAIL",
            "overall_score": 1,
            "per_criterion_scores": {
                "_llm_error": {
                    "score": 1, "verdict": "FAIL", "confidence": 0,
                    "reason": f"LLM parsing failed after {MAX_LLM_RETRIES} attempts: {last_exc}",
                }
            },
        }
    }


# ---------------------------------------------------------------------------
# Response validation & normalisation
# ---------------------------------------------------------------------------

def _normalize_criterion_keys(
    per_criterion: dict, criteria: list[CriterionInput]
) -> dict:
    requested_names = [c.name for c in criteria]
    logger.debug("_normalize_criterion_keys: returned=%s requested=%s",
                 list(per_criterion.keys()), requested_names)
    normalized: dict = {}

    for returned_key, value in per_criterion.items():
        if returned_key in requested_names:
            normalized[returned_key] = value
            continue
        lower = returned_key.lower().strip()
        exact_ci = next((n for n in requested_names if n.lower().strip() == lower), None)
        if exact_ci:
            if exact_ci != returned_key:
                logger.debug("_normalize_criterion_keys: case match '%s' -> '%s'", returned_key, exact_ci)
            normalized[exact_ci] = value
            continue
        close = difflib.get_close_matches(returned_key, requested_names, n=1, cutoff=0.6)
        if close:
            logger.debug("_normalize_criterion_keys: fuzzy match '%s' -> '%s'", returned_key, close[0])
            normalized[close[0]] = value
        else:
            logger.warning("_normalize_criterion_keys: no match for '%s', keeping as-is", returned_key)
            normalized[returned_key] = value

    logger.debug("_normalize_criterion_keys: returning keys=%s", list(normalized.keys()))
    return normalized


def validate_and_clamp(assessment: dict, criteria: list[CriterionInput]) -> dict:
    logger.info("validate_and_clamp: raw overall_score=%s overall_verdict=%s criteria=%s",
                assessment.get("overall_score"), assessment.get("overall_verdict"),
                [c.name for c in criteria])

    raw_score = assessment.get("overall_score", 5)
    try:
        overall_score = max(1, min(10, int(raw_score)))
    except (TypeError, ValueError):
        logger.warning("validate_and_clamp: invalid overall_score=%r, defaulting to 5", raw_score)
        overall_score = 5

    assessment["overall_score"] = overall_score
    assessment["overall_verdict"] = _verdict_from_score(overall_score)

    per_criterion = _normalize_criterion_keys(
        assessment.get("per_criterion_scores", {}), criteria
    )
    for key, val in per_criterion.items():
        if not isinstance(val, dict):
            continue
        try:
            score = max(1, min(10, int(val.get("score", 5))))
        except (TypeError, ValueError):
            logger.warning("validate_and_clamp: invalid score for '%s', defaulting to 5", key)
            score = 5
        try:
            confidence = max(0, min(100, int(val.get("confidence", 50))))
        except (TypeError, ValueError):
            logger.warning("validate_and_clamp: invalid confidence for '%s', defaulting to 50", key)
            confidence = 50
        val["score"] = score
        val["confidence"] = confidence
        val["verdict"] = _verdict_from_score(score)

    assessment["per_criterion_scores"] = per_criterion
    logger.info("validate_and_clamp: returning overall_score=%s overall_verdict=%s keys=%s",
                assessment["overall_score"], assessment["overall_verdict"],
                list(per_criterion.keys()))
    return assessment
