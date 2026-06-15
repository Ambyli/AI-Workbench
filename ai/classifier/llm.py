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
                                 valid ranges and recomputes verdicts from scores.
                                 Weighted score calculation is handled separately
                                 by analysis.compute_weighted_score().

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
from utils import verdict_from_score as _verdict_from_score

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



# ---------------------------------------------------------------------------
# Hint rubric definitions
# ---------------------------------------------------------------------------

# Each entry defines the heading, scoring rubric, and any extra instruction
# the LLM receives for criteria with that hint value.  build_llm_prompt()
# groups criteria by hint and emits one section per group using these strings.
# Edit here to change how any hint type is explained to the LLM — no need to
# touch the prompt-building logic itself.

HINT_RUBRICS: dict[str, dict[str, str]] = {
    "quality": {
        "heading": "QUALITY criteria — score image quality on a 1-10 scale",
        "rubric":  "1-3 = FAIL (poor quality)  |  4-6 = MARGINAL  |  7-10 = PASS (good quality)",
        "extra":   "",
    },
    "presence": {
        "heading": "PRESENCE criteria — detect whether each feature is present in the image",
        "rubric":  (
            "10 = clearly present (PASS)  |  "
            "5 = uncertain or partially present (MARGINAL)  |  "
            "1 = clearly absent (FAIL)"
        ),
        "extra":   (
            "For each PRESENCE criterion your 'reason' MUST follow this structure:\n"
            "  'I observe [specific visual evidence]. "
            "Therefore [feature] is [present / absent / uncertain].'"
        ),
    },
    "auto": {
        "heading": "INFERRED criteria — determine the appropriate rubric from the criterion name",
        "rubric":  (
            "Quality/clarity criteria (e.g. 'image sharpness'): score quality 1-10.\n"
            "  Presence/absence criteria (e.g. 'has X'): "
            "10=present, 5=uncertain, 1=absent."
        ),
        "extra":   "",
    },
}

# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------


def _build_scaffold(criteria: list[CriterionInput]) -> str:
    """Build a pre-filled JSON response template with criterion names as keys.

    Pre-defining the keys prevents the LLM from grouping or renaming criteria.
    Only the criteria passed in are included — CV-resolved criteria are handled
    before this is called and are excluded from the LLM prompt.

    Args:
        criteria: The LLM-bound criteria for this request.

    Returns:
        A JSON string with 0-valued placeholders for the model to fill in.
    """
    per_criterion = {
        c.name: {"score": 0, "verdict": "...", "confidence": 0, "reason": "..."}
        for c in criteria
    }
    return json.dumps(
        {
            "assessment": {
                "overall_verdict": "...",
                "overall_score": 0,
                "per_criterion_scores": per_criterion,
            }
        },
        indent=2,
    )


def build_llm_prompt(image_b64: str, criteria: list[CriterionInput]) -> dict:
    """Assemble the full vLLM chat completion request for a set of criteria.

    All criteria passed here are LLM-bound (type="llm", or type="cv" with no
    matching detector).  A unified rubric is used — the LLM infers from the
    criterion name whether to score quality or detect presence:
      - Quality criteria (e.g. "image sharpness"): score 1-10 for quality level.
      - Presence criteria (e.g. "has solar panels"): 10=present, 5=uncertain, 1=absent.

    The prompt applies four reliability improvements:
      1. Pre-filled scaffold    — criterion keys defined in advance.
      2. Explicit key list      — reinforces expected keys.
      3. "Do not group" rule    — system prompt forbids merging criteria.
      4. Verification step      — model self-checks before responding.

    Args:
        image_b64: Base64-encoded JPEG of the (resized) image.
        criteria:  LLM-bound CriterionInput objects.

    Returns:
        A dict ready to POST to the vLLM /v1/chat/completions endpoint.
    """
    logger.debug(
        "build_llm_prompt: image_b64[%d chars] criteria=%s hints=%s",
        len(image_b64),
        [c.name for c in criteria],
        {c.name: c.hint for c in criteria},
    )

    # --- Group criteria by hint and emit one rubric section per group ---
    # Ordering: quality → presence → auto, so explicit hints come first.
    hint_order = ["quality", "presence", "auto"]
    sections = []
    for hint_val in hint_order:
        group = [c for c in criteria if c.hint == hint_val]
        if not group:
            continue
        rubric_def = HINT_RUBRICS[hint_val]
        names = "\n".join(f"  - {c.name}" for c in group)
        section = f"{rubric_def['heading']}:\n  {rubric_def['rubric']}"
        if rubric_def["extra"]:
            section += f"\n  {rubric_def['extra']}"
        section += f"\n{names}"
        sections.append(section)
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
        # Improvement 4: self-verification step
        f"Before returning, verify your JSON contains exactly those {n} keys in "
        "per_criterion_scores — no more, no fewer, with names spelled exactly as shown. "
        "If any key is missing or renamed, revise before responding."
    )

    # --- System prompt (improvement 3: do-not-group rule) ---
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
            {
                "role": "user",
                "content": [
                    # Embed the image as a data URI so vLLM can process it
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
                    },
                    {"type": "text", "text": user_text},
                ],
            },
        ],
        "max_tokens": 2048,
        "temperature": 0.1,  # low temperature → more deterministic scoring
        "response_format": {"type": "json_object"},  # forces valid JSON output
    }
    logger.debug(
        "build_llm_prompt: returning prompt — %d llm criteria, scaffold keys=%s",
        len(criteria),
        [c.name for c in criteria],
    )
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

    logger.info(
        "call_vllm: posting to %s (max_retries=%d)", VLLM_QWEN_VL_API, MAX_LLM_RETRIES
    )

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
    logger.debug(
        "_normalize_criterion_keys: returned=%s requested=%s",
        list(per_criterion.keys()),
        requested_names,
    )
    normalized: dict = {}

    for returned_key, value in per_criterion.items():
        # 1. Exact match — most common case after prompt improvements
        if returned_key in requested_names:
            normalized[returned_key] = value
            continue

        # 2. Case-insensitive match
        lower = returned_key.lower().strip()
        exact_ci = next(
            (n for n in requested_names if n.lower().strip() == lower), None
        )
        if exact_ci:
            if exact_ci != returned_key:
                logger.debug(
                    "_normalize_criterion_keys: case match '%s' -> '%s'",
                    returned_key,
                    exact_ci,
                )
            normalized[exact_ci] = value
            continue

        # 3. Fuzzy match — handles minor wording differences
        close = difflib.get_close_matches(
            returned_key, requested_names, n=1, cutoff=0.6
        )
        if close:
            logger.debug(
                "_normalize_criterion_keys: fuzzy match '%s' -> '%s'",
                returned_key,
                close[0],
            )
            normalized[close[0]] = value
        else:
            # 4. No match — preserve the original key but warn
            logger.warning(
                "_normalize_criterion_keys: no match for '%s', keeping as-is",
                returned_key,
            )
            normalized[returned_key] = value

    logger.debug(
        "_normalize_criterion_keys: returning keys=%s", list(normalized.keys())
    )
    return normalized


def validate_and_clamp(assessment: dict, criteria: list[CriterionInput]) -> dict:
    """Clamp scores/confidence to valid ranges, normalise criterion keys, and
    recompute per-criterion verdicts from the clamped scores.

    Does NOT compute the weighted overall score — call compute_weighted_score()
    separately when a weighted breakdown is needed (i.e. for combined_assessment).

    Steps:
      1. Clamp raw overall_score to [1, 10]; set preliminary overall_verdict.
      2. Normalise criterion keys via _normalize_criterion_keys().
      3. Clamp per-criterion score to [1, 10] and confidence to [0, 100].
      4. Recompute per-criterion verdict from the clamped score.

    Args:
        assessment: Raw assessment dict from call_vllm() (may have bad values).
        criteria:   Criteria list used for key normalisation.

    Returns:
        Cleaned assessment dict with valid scores and verdicts.
    """
    logger.info(
        "validate_and_clamp: raw overall_score=%s overall_verdict=%s criteria=%s",
        assessment.get("overall_score"),
        assessment.get("overall_verdict"),
        [c.name for c in criteria],
    )

    # Step 1 — clamp raw overall score and set a preliminary verdict
    raw_score = assessment.get("overall_score", 5)
    try:
        overall_score = max(1, min(10, int(raw_score)))
    except (TypeError, ValueError):
        logger.warning(
            "validate_and_clamp: invalid overall_score=%r, defaulting to 5", raw_score
        )
        overall_score = 5
    assessment["overall_score"] = overall_score
    assessment["overall_verdict"] = _verdict_from_score(overall_score)

    # Step 2 — normalise criterion keys
    per_criterion = _normalize_criterion_keys(
        assessment.get("per_criterion_scores", {}), criteria
    )

    # Step 3+4 — clamp per-criterion scores/confidence and recompute verdicts
    for key, val in per_criterion.items():
        if not isinstance(val, dict):
            continue
        try:
            score = max(1, min(10, int(val.get("score", 5))))
        except (TypeError, ValueError):
            logger.warning(
                "validate_and_clamp: invalid score for '%s', defaulting to 5", key
            )
            score = 5
        try:
            confidence = max(0, min(100, int(val.get("confidence", 50))))
        except (TypeError, ValueError):
            logger.warning(
                "validate_and_clamp: invalid confidence for '%s', defaulting to 50", key
            )
            confidence = 50
        val["score"] = score
        val["confidence"] = confidence
        val["verdict"] = _verdict_from_score(score)

    assessment["per_criterion_scores"] = per_criterion

    logger.info(
        "validate_and_clamp: returning overall_score=%s overall_verdict=%s keys=%s",
        assessment["overall_score"],
        assessment["overall_verdict"],
        list(per_criterion.keys()),
    )
    return assessment


