"""LLM interaction layer — prompt building, vLLM calls, and response validation.

This module is responsible for everything that touches the language model:

  1. _build_scaffold()         — pre-fills the JSON response template with
                                 criterion names so the model cannot invent keys.
  2. build_llm_prompt()        — assembles the full system + user message,
                                 separating QUALITY and FEATURE criteria with
                                 different scoring rubrics, and attaching the
                                 document's extracted text (and at most ONE
                                 page image — see the note below).
  3. call_vllm()               — async POST to the vLLM OpenAI-compatible API
                                 with retry on parse failures.
  4. _normalize_criterion_keys() — fuzzy-matches LLM-returned keys back to the
                                 requested criterion names (handles capitalisation
                                 and minor spelling differences).
  5. validate_and_clamp()      — clamps all LLM scores/confidence values to
                                 valid ranges and recomputes verdicts from scores.
                                 Weighted score calculation is handled separately
                                 by analysis.compute_weighted_score().

ONE IMAGE PER PROMPT. The vision model (muse-glimmer) is served by vLLM
WITHOUT ``--limit-mm-per-prompt``, which means a request may contain at most
one image — a second one fails the whole call. build_llm_prompt therefore
takes a single ``image_b64`` (or None, for a text-only document) no matter how
many pages the document has; analysis.py decides which page that is. Phase 2:
set ``--limit-mm-per-prompt image=N`` on the vLLM container, then this
function can take a list.

Process flow position: called by analysis.analyze_document() after the
document is loaded, OCR'd, and its pages resized.  Returns a validated
assessment dict that analysis packages into the final response.
"""

import base64
import difflib
import json
import re

import httpx
from fastapi import HTTPException
from prometheus_client import Counter, Histogram

from config import (
    DOCUMENT_TEXT_HEADING,
    HINT_RUBRICS,
    VISION_LLM_API,
    VISION_LLM_MODEL,
    VISION_LLM_REASONING_STRENGTH,
    VISION_LLM_MAX_TOKENS,
    MAX_LLM_RETRIES,
    HTTP_TIMEOUT,
    HTTP_CONNECT_TIMEOUT,
)
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
# Prompt building
# ---------------------------------------------------------------------------
# The rubric strings (HINT_RUBRICS) and the extracted-text block heading
# (DOCUMENT_TEXT_HEADING) live in config.py § LLM prompt text, so prompt
# wording can be tuned without touching the assembly logic below.


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


def build_llm_prompt(
    image_b64: str | None,
    criteria: list[CriterionInput],
    document_text: str = "",
    *,
    document_kind: str = "image",
    page_index: int | None = None,
    page_count: int = 1,
    text_truncated: bool = False,
) -> dict:
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

    Document handling:
      * ``document_text`` (already truncated to CLASSIFIER_TEXT_CHAR_BUDGET by
        the caller) is appended as a clearly-labelled block, and the system
        prompt tells the model it may use image and text together.
      * At most ONE image is attached — vLLM rejects multi-image requests
        while muse-glimmer runs without ``--limit-mm-per-prompt``. Pass None
        for a text-only document (.txt / .docx); the content array then holds
        text only and the JSON response format is unchanged.

    Args:
        image_b64:      Base64-encoded JPEG of one (resized) page image, or
                        None when the document has no images.
        criteria:       LLM-bound CriterionInput objects.
        document_text:  Extracted text for the whole document ("" if none).
        document_kind:  "image" | "pdf" | "txt" | "docx", for context.
        page_index:     Which page the attached image came from (0-based).
        page_count:     How many pages the document has in total.
        text_truncated: True when document_text was cut at the char budget.

    Returns:
        A dict ready to POST to the vLLM /v1/chat/completions endpoint.
    """
    logger.debug(
        "build_llm_prompt: image_b64[%s] kind=%s page=%s/%s text=%d chars criteria=%s hints=%s",
        f"{len(image_b64)} chars" if image_b64 else "none",
        document_kind,
        page_index,
        page_count,
        len(document_text),
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

    # --- Document context: what the model is actually looking at ---
    # A one-line header so the model knows whether the attached image is the
    # whole document or one page of many (and that the rest is in the text).
    if document_kind == "image":
        context_line = "You are assessing a single image."
    elif image_b64 and page_count > 1:
        context_line = (
            f"You are assessing a {page_count}-page {document_kind} document. "
            f"The attached image is page {(page_index or 0) + 1} of {page_count}; "
            "the extracted text below covers every page."
        )
    elif image_b64:
        context_line = f"You are assessing a 1-page {document_kind} document."
    else:
        context_line = (
            f"You are assessing a {document_kind} document that has no page images. "
            "Judge every criterion from the extracted text below."
        )

    # --- Extracted text block (truncation already applied by the caller) ---
    text_block = ""
    if document_text.strip():
        truncation_note = (
            "\n[text truncated at the configured character budget — later pages "
            "may be missing]"
            if text_truncated
            else ""
        )
        text_block = (
            f"\n\n{DOCUMENT_TEXT_HEADING}:\n"
            "---\n"
            f"{document_text}{truncation_note}\n"
            "---\n"
        )

    # --- Full user message (improvements 1, 2, and 4) ---
    user_text = (
        f"{context_line}\n\n"
        f"{criteria_text}"
        f"{text_block}\n\n"
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
    if document_text.strip():
        # Both modalities are available: say so explicitly, and warn that the
        # text may be OCR output so the model treats near-misses sensibly.
        source_sentence = (
            "You are given a document as an image and as extracted text. Use BOTH: "
            "the image for anything visual (legibility, lighting, framing, stamps, "
            "signatures) and the text for anything about content (wording, amounts, "
            "dates, clauses). The text may be OCR output and can contain recognition "
            "errors — judge meaning, not exact spelling. "
            if image_b64
            else
            "You are given a document as extracted text only — it has no page images. "
            "Judge every criterion from that text. It may be OCR output and can "
            "contain recognition errors — judge meaning, not exact spelling. "
        )
    else:
        source_sentence = "Analyze the provided image and score it against each criterion listed below. "

    system_prompt = (
        "You are a document assessment expert. "
        f"{source_sentence}"
        "Score each criterion independently — do NOT group multiple criteria under a "
        "single key or summarise them together. "
        "Set confidence to a number 0-100: 0 = completely uncertain, 100 = completely certain. "
        "Return ONLY a valid JSON object."
    )
    if VISION_LLM_REASONING_STRENGTH:
        # Muse Glimmer reads its reasoning depth from this system-prompt line
        # (see ai/vllm/VLLM.md "Parsers and sampling"). Other models ignore it.
        system_prompt += f"\nReasoning strength: {VISION_LLM_REASONING_STRENGTH}"

    # ONE image maximum — see the module docstring. A text-only document
    # (.txt / .docx, or an image-less request) sends a text-only content array,
    # which vLLM accepts from a multimodal model without complaint.
    user_content: list[dict] = []
    if image_b64:
        user_content.append(
            # Embed the image as a data URI so vLLM can process it
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
            }
        )
    user_content.append({"type": "text", "text": user_text})

    prompt = {
        "model": VISION_LLM_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        # Budget covers reasoning + the JSON answer; reasoning is stripped
        # server-side by --reasoning-parser so `content` is JSON only.
        "max_tokens": VISION_LLM_MAX_TOKENS,
        # No temperature override: the server's --generation-config auto applies
        # Meta's published sampling for Muse Glimmer (temperature 1.0, top_p
        # 0.95, top_k 64). The model card warns against greedy / near-greedy
        # decoding, so the old 0.1 is deliberately gone. Consistency comes from
        # the JSON schema constraint below plus validate_and_clamp().
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
        if val.get("verdict") == "SKIPPED":
            # Not applicable, not scored: a SKIPPED entry carries score=None on
            # purpose (apply_dependencies, or a cv criterion on a document with
            # no page images). Clamping would turn it into a middling 5 and
            # drag it back into the weighted average.
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


