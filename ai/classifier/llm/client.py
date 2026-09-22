"""Talking to the vLLM server: the two calls, and what they cost.

    encode_image_to_base64() — BGR numpy array -> base64 JPEG for a data URI.
    call_vllm()              — the SCORING call. Retries a parse failure up to
                               MAX_LLM_RETRIES and, when every attempt fails,
                               returns an error SENTINEL rather than raising,
                               so a broken call still produces a scored result.
    call_vllm_json()         — the generic JSON call the enforcement loop's two
                               prompts use. Returns None on failure instead of
                               a sentinel, because a localisation that could
                               not be obtained must leave the score it was
                               annotating untouched.

The difference in failure mode between the two is the whole reason they are
separate functions.

``llm_calls_total`` and ``llm_latency`` live here rather than in ``metrics``
because both calls produce them and nothing else reads them.

Process flow position: ``call_vllm`` is step 6 of
``analysis.pipeline.analyze_document``; ``call_vllm_json`` is every round of
``llm.boxes``.
"""

import base64
import json
import re

import httpx
from fastapi import HTTPException
from prometheus_client import Counter, Histogram

from config import (
    HTTP_CONNECT_TIMEOUT,
    HTTP_TIMEOUT,
    MAX_LLM_RETRIES,
    VISION_LLM_API,
    VISION_LLM_MODEL,
)
from logger import logger

# Shared HTTP timeout applied to every vLLM request
_http_timeout = httpx.Timeout(HTTP_TIMEOUT, connect=HTTP_CONNECT_TIMEOUT)

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------

llm_calls_total = Counter(
    "classifier_llm_calls_total",
    "Total LLM API calls by status",
    ["status"],  # success | retry | failed
)
llm_latency = Histogram(
    "classifier_llm_latency_seconds",
    "LLM API call latency in seconds",
)


def encode_image_to_base64(image) -> str:
    """JPEG-encode a BGR numpy array and return a base64 string.

    The image is re-encoded as JPEG (lossy but compact) before being embedded
    in the LLM prompt.  This keeps the prompt size manageable for large images.

    Args:
        image: BGR numpy array, already resized to ≤1000px on the long side.

    Returns:
        Base64-encoded JPEG string suitable for a data URI.
    """
    import cv2

    logger.debug("encode_image_to_base64: image shape=%s", image.shape)
    _, buf = cv2.imencode(".jpg", image)
    result = base64.b64encode(buf).decode("utf-8")
    logger.debug("encode_image_to_base64: returning base64[%d chars]", len(result))
    return result


async def call_vllm(prompt: dict) -> dict:
    """POST a prompt to the vLLM API and return the parsed assessment dict.

    Retry logic: JSON parse or structure errors trigger up to MAX_LLM_RETRIES
    retries with the same prompt.  HTTP errors (model down, network issue) are
    raised immediately — retrying a dead server is pointless.

    On exhausting all retries, returns an error sentinel dict rather than
    raising so the caller can still produce a FAIL response with an error
    reason in per_criterion_scores.

    Args:
        prompt: The dict produced by build_llm_prompt().

    Returns:
        Parsed assessment dict  {"assessment": {...}}  or error sentinel.

    Raises:
        HTTPException(502): On HTTP-level failures from vLLM.
    """
    import time

    logger.info(
        "call_vllm: posting to %s model=%s (max_retries=%d)",
        VISION_LLM_API,
        VISION_LLM_MODEL,
        MAX_LLM_RETRIES,
    )

    last_exc: Exception | None = None

    for attempt in range(MAX_LLM_RETRIES):
        logger.info("call_vllm: attempt %d/%d", attempt + 1, MAX_LLM_RETRIES)
        t0 = time.monotonic()
        try:
            # Step 1 — send the request and check HTTP status
            async with httpx.AsyncClient(timeout=_http_timeout) as client:
                response = await client.post(VISION_LLM_API, json=prompt)
                response.raise_for_status()
                data = response.json()

            elapsed = time.monotonic() - t0
            llm_latency.observe(elapsed)
            # With a reasoning parser the model's thinking lands in
            # `reasoning_content`; `content` is the answer and can be None if
            # the budget ran out mid-reasoning. Treat that as a parse failure
            # so the retry loop fires instead of a 502.
            content = data["choices"][0]["message"].get("content") or ""
            logger.debug(
                "call_vllm: response[%d chars] in %.2fs", len(content), elapsed
            )

            # Step 2 — parse the JSON response
            # json_object mode should guarantee valid JSON, but keep regex
            # as a fallback in case the model wraps it in markdown fences
            try:
                result = json.loads(content)
            except json.JSONDecodeError:
                json_match = re.search(r"\{[\s\S]*\}", content)
                if not json_match:
                    raise ValueError(f"No JSON found in response: {content[:200]}")
                result = json.loads(json_match.group())

            # Step 3 — verify the expected top-level key is present
            if "assessment" not in result:
                raise ValueError(f"Response missing 'assessment' key: {content[:200]}")

            llm_calls_total.labels(status="success").inc()
            verdict = result.get("assessment", {}).get("overall_verdict", "unknown")
            score = result.get("assessment", {}).get("overall_score", "unknown")
            logger.info(
                "call_vllm: returning overall_verdict=%s overall_score=%s",
                verdict,
                score,
            )
            return result

        except httpx.HTTPError as exc:
            # HTTP errors (4xx/5xx from vLLM) are not retried — log and raise
            llm_calls_total.labels(status="failed").inc()
            logger.error("call_vllm: HTTP error on attempt %d: %s", attempt + 1, exc)
            raise HTTPException(status_code=502, detail=f"vLLM call failed: {exc}")
        except (KeyError, IndexError) as exc:
            # Unexpected response shape — not retried
            llm_calls_total.labels(status="failed").inc()
            logger.error(
                "call_vllm: unexpected response format on attempt %d: %s",
                attempt + 1,
                exc,
            )
            raise HTTPException(
                status_code=502, detail=f"Unexpected vLLM response format: {exc}"
            )
        except (json.JSONDecodeError, ValueError) as exc:
            # Parse / structure errors — retry up to MAX_LLM_RETRIES
            llm_calls_total.labels(status="retry").inc()
            logger.warning(
                "call_vllm: parse failure on attempt %d: %s", attempt + 1, exc
            )
            last_exc = exc

    # All retries exhausted — return an error sentinel instead of raising
    llm_calls_total.labels(status="failed").inc()
    logger.error("call_vllm: all %d attempts failed", MAX_LLM_RETRIES)
    return {
        "assessment": {
            "overall_verdict": "FAIL",
            "overall_score": 1,
            "per_criterion_scores": {
                "_llm_error": {
                    "score": 1,
                    "verdict": "FAIL",
                    "confidence": 0,
                    "reason": f"LLM parsing failed after {MAX_LLM_RETRIES} attempts: {last_exc}",
                }
            },
        }
    }


async def call_vllm_json(prompt: dict, *, label: str = "") -> dict | None:
    """POST a prompt and return the parsed JSON object, or None.

    The difference from :func:`call_vllm` is the failure mode, and it is the
    whole reason this exists separately. ``call_vllm`` returns a FAIL sentinel
    assessment so a broken scoring call still produces a scored result; a
    broken LOCALISATION call must produce nothing at all, because the score it
    is annotating is already correct and must not move. So: ``None``, which
    the enforcement loop records as a failed attempt and carries on from.

    HTTP errors are not retried (a dead server stays dead) and are not raised
    either — the loop has to survive them. Parse failures get the same
    MAX_LLM_RETRIES budget the scoring call has.

    Args:
        prompt: A dict from build_bbox_prompt / build_verify_prompt.
        label:  What this call was for, for the log line only.

    Returns:
        The parsed JSON object, or None when every attempt failed.
    """
    import time

    for attempt in range(MAX_LLM_RETRIES):
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=_http_timeout) as client:
                response = await client.post(VISION_LLM_API, json=prompt)
                response.raise_for_status()
                data = response.json()
            llm_latency.observe(time.monotonic() - t0)
            # `content` is None when the reasoning parser ate the whole
            # budget. Treated as a parse failure so the retry fires.
            content = data["choices"][0]["message"].get("content") or ""
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError:
                match = re.search(r"\{[\s\S]*\}", content)
                if not match:
                    raise ValueError(f"no JSON in response: {content[:200]}")
                parsed = json.loads(match.group())
            if not isinstance(parsed, dict):
                raise ValueError(f"expected a JSON object, got {type(parsed).__name__}")
            llm_calls_total.labels(status="success").inc()
            return parsed
        except httpx.HTTPError as exc:
            llm_calls_total.labels(status="failed").inc()
            logger.warning("call_vllm_json(%s): HTTP error: %s", label, exc)
            return None
        except (KeyError, IndexError) as exc:
            llm_calls_total.labels(status="failed").inc()
            logger.warning(
                "call_vllm_json(%s): unexpected response shape: %s", label, exc
            )
            return None
        except (json.JSONDecodeError, ValueError) as exc:
            llm_calls_total.labels(status="retry").inc()
            logger.warning(
                "call_vllm_json(%s): parse failure on attempt %d/%d: %s",
                label, attempt + 1, MAX_LLM_RETRIES, exc,
            )

    llm_calls_total.labels(status="failed").inc()
    logger.error("call_vllm_json(%s): all %d attempts failed", label, MAX_LLM_RETRIES)
    return None
