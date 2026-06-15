"""LLM interaction layer — prompt building, vLLM calls, and response validation.

This module is responsible for everything that touches the language model:

  1. _build_scaffold()         — pre-fills the JSON response template with
                                 criterion names so the model cannot invent keys.
  2. build_llm_prompt()        — assembles the full system + user message,
                                 separating QUALITY and FEATURE criteria with
                                 different scoring rubrics.
  3. call_vllm()               — async POST to the vLLM OpenAI-compatible API
                                 with retry on parse failures.
  4. _normalize_criterion_keys() — fuzzy-matches LLM-returned keys back to the
                                 requested criterion names (handles capitalisation
                                 and minor spelling differences).
  5. validate_and_clamp()      — clamps all LLM scores/confidence values to
                                 valid ranges, recomputes verdicts from scores,
                                 and calculates the weighted overall score.

Process flow position: called by analysis.analyze_bgr() after the image is
ready.  Returns a validated assessment dict that analysis packages into the
final response.
"""

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


def _verdict_from_score(score: int) -> str:
    """Map a 1-10 score to a PASS / MARGINAL / FAIL verdict string.

    Thresholds: 7-10 = PASS, 4-6 = MARGINAL, 1-3 = FAIL.
    Used both to recompute per-criterion verdicts after clamping and to
    derive the overall verdict from the weighted score.
    """
    logger.debug("_verdict_from_score: score=%s", score)
    verdict = "PASS" if score >= 7 else ("MARGINAL" if score >= 4 else "FAIL")
    logger.debug("_verdict_from_score: returning %s", verdict)
    return verdict


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------

def _build_scaffold(criteria: list[CriterionInput]) -> str:
    """Build a pre-filled JSON response template with criterion names as keys.

    Why: Without a scaffold, the LLM sometimes groups multiple criteria under
    one key (e.g. merging 'image sharpness' and 'proper exposure' into a single
    'image_quality' entry).  Pre-defining the keys forces the model to fill in
    values rather than invent structure.

    Args:
        criteria: The full list of criteria for this request.

    Returns:
        A JSON string with 0-valued placeholders for the model to fill in.
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
    """Assemble the full vLLM chat completion request for a set of criteria.

    The prompt applies four reliability improvements:
      1. Pre-filled scaffold    — keys defined in advance (see _build_scaffold).
      2. Explicit key list      — repeated after the scaffold to reinforce.
      3. "Do not group" rule    — system prompt forbids merging criteria.
      4. Verification step      — user message asks the model to self-check
                                  before responding.

    QUALITY and FEATURE criteria get different scoring rubrics:
      - QUALITY: 1-10 scale measuring how good the image is.
      - FEATURE: 1/5/10 scale measuring presence vs absence.

    Args:
        image_b64: Base64-encoded JPEG of the (resized) image.
        criteria:  List of CriterionInput objects for this request.

    Returns:
        A dict ready to POST to the vLLM /v1/chat/completions endpoint.
    """
    logger.debug("build_llm_prompt: image_b64[%d chars] criteria=%s",
                 len(image_b64), [c.name for c in criteria])

    # Partition criteria by type so each gets the right rubric
    quality_criteria = [c for c in criteria if c.type == "quality"]
    feature_criteria = [c for c in criteria if c.type == "feature"]

    # --- Build criteria description sections ---
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
            # Chain-of-thought instruction: forces the model to ground its
            # verdict in observed visual evidence before scoring
            "  For each feature criterion, your 'reason' MUST follow this structure:\n"
            "    'I observe [specific visual evidence]. Therefore, [feature] is [present/absent/uncertain].'\n"
            f"{names}"
        )
    criteria_text = "\n\n".join(sections)

    # --- Improvement 1: pre-filled scaffold ---
    scaffold = _build_scaffold(criteria)

    # --- Improvement 2: explicit key list ---
    key_list = ", ".join(f'"{c.name}"' for c in criteria)
    n = len(criteria)

    # --- Full user message (improvements 1, 2, and 4) ---
    user_text = (
        f"{criteria_text}\n\n"
        "Fill in the following JSON structure. "
        "The keys in per_criterion_scores are already defined — "
        "do NOT change, rename, merge, or add any keys:\n\n"
        f"{scaffold}\n\n"
        f"Required keys in per_criterion_scores ({n} total): {key_list}\n\n"
        # Improvement 4: self-verification step reduces key mismatch errors
        f"Before returning, verify your JSON contains exactly those {n} keys in "
        "per_criterion_scores — no more, no fewer, with names spelled exactly as shown. "
        "If any key is missing or renamed, revise before responding."
    )

    # --- System prompt (improvement 3: do-not-group rule) ---
    system_prompt = (
        "You are an image assessment expert. "
        "Analyze the provided image and score it against each criterion listed below. "
        # Explicitly forbid the grouping behaviour we observed in earlier runs
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
                # Embed the image as a data URI so vLLM can process it
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                {"type": "text", "text": user_text},
            ]},
        ],
        "max_tokens": 2048,
        "temperature": 0.1,           # low temperature → more deterministic scoring
        "response_format": {"type": "json_object"},  # forces valid JSON output
    }
    logger.debug("build_llm_prompt: returning prompt — %d quality + %d feature criteria, scaffold keys=%s",
                 len(quality_criteria), len(feature_criteria), [c.name for c in criteria])
    return prompt


# ---------------------------------------------------------------------------
# vLLM call with retry
# ---------------------------------------------------------------------------

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
    logger.info("call_vllm: posting to %s (max_retries=%d)", VLLM_QWEN_VL_API, MAX_LLM_RETRIES)

    last_exc: Exception | None = None

    for attempt in range(MAX_LLM_RETRIES):
        logger.info("call_vllm: attempt %d/%d", attempt + 1, MAX_LLM_RETRIES)
        t0 = time.monotonic()
        try:
            # Step 1 — send the request and check HTTP status
            async with httpx.AsyncClient(timeout=_http_timeout) as client:
                response = await client.post(VLLM_QWEN_VL_API, json=prompt)
                response.raise_for_status()
                data = response.json()

            elapsed = time.monotonic() - t0
            llm_latency.observe(elapsed)
            content = data["choices"][0]["message"]["content"]
            logger.debug("call_vllm: response[%d chars] in %.2fs", len(content), elapsed)

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
            logger.info("call_vllm: returning overall_verdict=%s overall_score=%s", verdict, score)
            return result

        except httpx.HTTPError as exc:
            # HTTP errors (4xx/5xx from vLLM) are not retried — log and raise
            llm_calls_total.labels(status="failed").inc()
            logger.error("call_vllm: HTTP error on attempt %d: %s", attempt + 1, exc)
            raise HTTPException(status_code=502, detail=f"vLLM call failed: {exc}")
        except (KeyError, IndexError) as exc:
            # Unexpected response shape — not retried
            llm_calls_total.labels(status="failed").inc()
            logger.error("call_vllm: unexpected response format on attempt %d: %s", attempt + 1, exc)
            raise HTTPException(status_code=502, detail=f"Unexpected vLLM response format: {exc}")
        except (json.JSONDecodeError, ValueError) as exc:
            # Parse / structure errors — retry up to MAX_LLM_RETRIES
            llm_calls_total.labels(status="retry").inc()
            logger.warning("call_vllm: parse failure on attempt %d: %s", attempt + 1, exc)
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
    """Remap LLM-returned criterion keys to the exact names that were requested.

    The LLM sometimes returns keys that differ from the requested names:
      - capitalisation: "Image Sharpness" instead of "image sharpness"
      - minor wording: "solar_panels" instead of "has solar panels"

    Resolution order (first match wins):
      1. Exact match — no change needed.
      2. Case-insensitive match — strip and lower both sides.
      3. Fuzzy match via difflib (cutoff 0.6) — catches minor spelling diffs.
      4. No match — keep the key as-is (logged as a warning).

    Args:
        per_criterion: The dict of criterion scores returned by the LLM.
        criteria:      The original list of CriterionInput objects.

    Returns:
        A new dict with keys remapped to the canonical criterion names.
    """
    requested_names = [c.name for c in criteria]
    logger.debug("_normalize_criterion_keys: returned=%s requested=%s",
                 list(per_criterion.keys()), requested_names)
    normalized: dict = {}

    for returned_key, value in per_criterion.items():
        # 1. Exact match — most common case after prompt improvements
        if returned_key in requested_names:
            normalized[returned_key] = value
            continue

        # 2. Case-insensitive match
        lower = returned_key.lower().strip()
        exact_ci = next((n for n in requested_names if n.lower().strip() == lower), None)
        if exact_ci:
            if exact_ci != returned_key:
                logger.debug("_normalize_criterion_keys: case match '%s' -> '%s'",
                             returned_key, exact_ci)
            normalized[exact_ci] = value
            continue

        # 3. Fuzzy match — handles minor wording differences
        close = difflib.get_close_matches(returned_key, requested_names, n=1, cutoff=0.6)
        if close:
            logger.debug("_normalize_criterion_keys: fuzzy match '%s' -> '%s'",
                         returned_key, close[0])
            normalized[close[0]] = value
        else:
            # 4. No match — preserve the original key but warn
            logger.warning("_normalize_criterion_keys: no match for '%s', keeping as-is",
                           returned_key)
            normalized[returned_key] = value

    logger.debug("_normalize_criterion_keys: returning keys=%s", list(normalized.keys()))
    return normalized


def validate_and_clamp(assessment: dict, criteria: list[CriterionInput]) -> dict:
    """Validate, clamp, and recompute the LLM assessment before it leaves this module.

    The LLM can return values outside the expected ranges (e.g. score=12,
    confidence=-5).  This function corrects those silently rather than failing
    the request.

    Steps:
      1. Clamp and normalise per-criterion scores/confidence values.
      2. Recompute per-criterion verdicts from clamped scores (ignores LLM verdict).
      3. Normalise criterion keys via _normalize_criterion_keys().
      4. Compute the weighted overall score from per-criterion scores and weights.
      5. Derive the overall verdict from the weighted score.

    Args:
        assessment: Raw assessment dict from call_vllm() (may have bad values).
        criteria:   The original criteria list, used for key normalisation and
                    weight lookup.

    Returns:
        Cleaned assessment dict with valid scores, verdicts, and a weighted
        overall_score that reflects criterion weights.
    """
    logger.info("validate_and_clamp: raw overall_score=%s overall_verdict=%s criteria=%s",
                assessment.get("overall_score"), assessment.get("overall_verdict"),
                [c.name for c in criteria])

    # Step 1a — clamp the raw overall score (will be overwritten in step 4)
    raw_score = assessment.get("overall_score", 5)
    try:
        overall_score = max(1, min(10, int(raw_score)))
    except (TypeError, ValueError):
        logger.warning("validate_and_clamp: invalid overall_score=%r, defaulting to 5", raw_score)
        overall_score = 5
    assessment["overall_score"] = overall_score
    assessment["overall_verdict"] = _verdict_from_score(overall_score)

    # Step 2+3 — normalise keys and clamp per-criterion values
    per_criterion = _normalize_criterion_keys(
        assessment.get("per_criterion_scores", {}), criteria
    )
    for key, val in per_criterion.items():
        if not isinstance(val, dict):
            continue
        # Clamp score to [1, 10]
        try:
            score = max(1, min(10, int(val.get("score", 5))))
        except (TypeError, ValueError):
            logger.warning("validate_and_clamp: invalid score for '%s', defaulting to 5", key)
            score = 5
        # Clamp confidence to [0, 100]
        try:
            confidence = max(0, min(100, int(val.get("confidence", 50))))
        except (TypeError, ValueError):
            logger.warning("validate_and_clamp: invalid confidence for '%s', defaulting to 50", key)
            confidence = 50
        val["score"] = score
        val["confidence"] = confidence
        # Recompute verdict from clamped score — don't trust the LLM's verdict string
        val["verdict"] = _verdict_from_score(score)

    assessment["per_criterion_scores"] = per_criterion

    # Step 4 — weighted overall score + transparency breakdown
    # Find all criteria whose names appear in the normalised per_criterion dict
    matched = [
        (c, per_criterion[c.name]) for c in criteria
        if c.name in per_criterion and isinstance(per_criterion[c.name], dict)
    ]
    if matched:
        total_weight = sum(c.weight for c, _ in matched)
        if total_weight > 0:
            # Weighted average of clamped per-criterion scores
            weighted_sum = sum(val["score"] * c.weight for c, val in matched)
            unrounded = weighted_sum / total_weight
            weighted_score = max(1, min(10, round(unrounded)))

            # Step 5 — overwrite the preliminary overall score with the weighted result
            assessment["overall_score"] = weighted_score
            assessment["overall_verdict"] = _verdict_from_score(weighted_score)

            # Attach a full breakdown so callers can audit exactly how the
            # final score was derived from each criterion's score and weight
            assessment["weighted_score_breakdown"] = {
                "formula": "sum(score × weight) / total_weight",
                "total_weight": round(total_weight, 4),
                "weighted_sum": round(weighted_sum, 4),
                "unrounded_average": round(unrounded, 4),
                "final_score": weighted_score,
                "per_criterion": {
                    c.name: {
                        "score": val["score"],
                        "weight": c.weight,
                        "contribution": round(val["score"] * c.weight, 4),
                    }
                    for c, val in matched
                },
            }
            logger.debug("validate_and_clamp: weighted score=%s weights=%s",
                         weighted_score, {c.name: c.weight for c, _ in matched})

    logger.info("validate_and_clamp: returning overall_score=%s overall_verdict=%s keys=%s",
                assessment["overall_score"], assessment["overall_verdict"],
                list(per_criterion.keys()))
    return assessment
