"""Talking to the vLLM server: one transport, two failure modes, one limit.

    encode_image_to_base64() — BGR numpy array -> base64 JPEG for a data URI.
    LLM_CALLS                — the process-wide limit on model requests in
                               flight (CLASSIFIER_MAX_LLM_CALLS), across every
                               job and every call type.
    _post()                  — ONE HTTP request to VISION_LLM_API (``_send``),
                               holding a LLM_CALLS slot for exactly its
                               duration. Every call below goes through it, so
                               no call path — scoring, box ask, refine,
                               verify — can get around the limit.
    call_vllm()              — the SCORING call. Retries a parse failure up to
                               MAX_LLM_RETRIES; raises ``LLMCallError`` on an
                               HTTP failure or when every attempt was
                               unparseable. The scheduler turns that into
                               ``status: "error"`` for the ONE criterion that
                               asked — a model outage fails that criterion,
                               never the job.
    call_vllm_json()         — the generic JSON call the enforcement loop's
                               prompts use. Returns None on failure instead of
                               raising, because a localisation that could not
                               be obtained must leave the score it was
                               annotating untouched.

The difference in failure mode between the two is the whole reason they are
separate functions.

``llm_calls_total`` and ``llm_latency`` live here rather than in ``metrics``
because both calls produce them and nothing else reads them.

Tests script the model by replacing ``call_vllm`` / ``call_vllm_json`` (the
calls) or ``_send`` (the bare transport under ``_post``, which keeps the limit
in play and measurable).

Process flow position: ``call_vllm`` is the ``llm`` evaluator's scoring call
(``analysis.llm_eval``); ``call_vllm_json`` is every round of ``llm.boxes``.
"""

import base64
import json
import re
import time

import httpx
from prometheus_client import Counter, Histogram

from common.jobs.limits import ConcurrencyLimit

from config import (
    HTTP_CONNECT_TIMEOUT,
    HTTP_TIMEOUT,
    MAX_LLM_CALLS,
    MAX_LLM_RETRIES,
    VISION_LLM_API,
    VISION_LLM_MODEL,
)
from logger import logger

# Shared HTTP timeout applied to every vLLM request
_http_timeout = httpx.Timeout(HTTP_TIMEOUT, connect=HTTP_CONNECT_TIMEOUT)

# Model requests in flight across the whole process. One slot per HTTP
# request (a retry takes a fresh slot), so a slow model backs callers up here
# rather than inside vLLM's own queue.
LLM_CALLS = ConcurrencyLimit(MAX_LLM_CALLS, name="llm-calls")

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


class LLMCallError(RuntimeError):
    """The scoring call could not produce an answer.

    Raised for an HTTP failure (the model is down, a 4xx/5xx), an unexpected
    response shape, or ``MAX_LLM_RETRIES`` unparseable answers. The message
    is caller-facing: it lands in the criterion's ``error`` field.
    """


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


async def _send(prompt: dict) -> dict:
    """The bare HTTP request: POST one chat completion, return the JSON body.

    Never called directly — only through ``_post``, which holds the limit.
    Tests replace THIS function to script the model with the limit in play.

    Raises:
        httpx.HTTPError: Transport failures and non-2xx statuses.
    """
    t0 = time.monotonic()
    async with httpx.AsyncClient(timeout=_http_timeout) as client:
        response = await client.post(VISION_LLM_API, json=prompt)
        response.raise_for_status()
        data = response.json()
    llm_latency.observe(time.monotonic() - t0)
    return data


async def _post(prompt: dict) -> dict:
    """One model request holding a ``LLM_CALLS`` slot for exactly its duration.

    Parsing and retry decisions happen outside the slot, so a slow parse
    never holds a place another job's call could use.
    """
    async with LLM_CALLS:
        return await _send(prompt)


def _parse_content(data: dict) -> dict:
    """The JSON object in a completion's ``content``.

    With a reasoning parser the model's thinking lands in
    ``reasoning_content``; ``content`` is the answer and can be None if the
    budget ran out mid-reasoning — treated as a parse failure so the retry
    fires. The regex fallback survives a model that wraps its JSON in
    markdown fences despite ``json_object`` mode.

    Raises:
        KeyError / IndexError: The response is not a chat completion.
        ValueError: No JSON object in the content.
    """
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
    return parsed


async def call_vllm(prompt: dict, *, label: str = "", validator=None) -> dict:
    """The scoring call: the parsed JSON answer, or ``LLMCallError``.

    Parse failures (including an empty ``content``) retry up to
    MAX_LLM_RETRIES with the same prompt. HTTP errors and a response that is
    not a chat completion are not retried — retrying a dead server is
    pointless.

    Args:
        prompt: The dict produced by ``llm.prompts.build_llm_prompt``.
        label:     What this call is for, for the log line only.
        validator: Optional ``parsed -> answer`` function. A ``ValueError``
                   from it counts as a parse failure and retries, so "valid
                   JSON but no score in it" gets the same budget as "not
                   JSON at all". Its return value is what this returns.

    Returns:
        The model's JSON object, or ``validator``'s result for it.

    Raises:
        LLMCallError: The call failed; the message says why.
    """
    logger.info(
        "call_vllm(%s): posting to %s model=%s (max_retries=%d)",
        label, VISION_LLM_API, VISION_LLM_MODEL, MAX_LLM_RETRIES,
    )
    last_exc: Exception | None = None
    for attempt in range(MAX_LLM_RETRIES):
        try:
            data = await _post(prompt)
            result = _parse_content(data)
            if validator is not None:
                result = validator(result)
            llm_calls_total.labels(status="success").inc()
            return result
        except httpx.HTTPError as exc:
            llm_calls_total.labels(status="failed").inc()
            logger.error("call_vllm(%s): HTTP error: %s", label, exc)
            raise LLMCallError(f"vision model call failed: {exc}") from exc
        except (KeyError, IndexError) as exc:
            llm_calls_total.labels(status="failed").inc()
            logger.error("call_vllm(%s): unexpected response shape: %s", label, exc)
            raise LLMCallError(f"unexpected vision model response: {exc!r}") from exc
        except (json.JSONDecodeError, ValueError) as exc:
            llm_calls_total.labels(status="retry").inc()
            logger.warning(
                "call_vllm(%s): parse failure on attempt %d/%d: %s",
                label, attempt + 1, MAX_LLM_RETRIES, exc,
            )
            last_exc = exc

    llm_calls_total.labels(status="failed").inc()
    logger.error("call_vllm(%s): all %d attempts failed", label, MAX_LLM_RETRIES)
    raise LLMCallError(
        f"the vision model's answer could not be parsed after {MAX_LLM_RETRIES} "
        f"attempts: {last_exc}"
    )


async def call_vllm_json(prompt: dict, *, label: str = "") -> dict | None:
    """POST a prompt and return the parsed JSON object, or None.

    The difference from :func:`call_vllm` is the failure mode, and it is the
    whole reason this exists separately: a broken LOCALISATION call must
    produce nothing at all, because the score it is annotating is already
    correct and must not move. So: ``None``, which the enforcement loop
    records as a failed attempt and carries on from.

    HTTP errors are not retried (a dead server stays dead) and are not raised
    either — the loop has to survive them. Parse failures get the same
    MAX_LLM_RETRIES budget the scoring call has.

    Args:
        prompt: A dict from build_bbox_prompt / build_verify_prompt.
        label:  What this call was for, for the log line only.

    Returns:
        The parsed JSON object, or None when every attempt failed.
    """
    for attempt in range(MAX_LLM_RETRIES):
        try:
            parsed = _parse_content(await _post(prompt))
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
