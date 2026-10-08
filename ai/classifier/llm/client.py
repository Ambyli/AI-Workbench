"""Talking to the vLLM server: one transport, two failure modes, one limit.

    encode_image_to_base64() — BGR numpy array -> base64 JPEG for a data URI.
    LLM_CALLS                — the process-wide limit on model requests in
                               flight (CLASSIFIER_MAX_LLM_CALLS), across every
                               job and every call type.
    aclose()                 — close the shared HTTP client (app shutdown).
    _post()                  — ONE HTTP request to VISION_LLM_API (``_send``),
                               holding a LLM_CALLS slot for exactly its
                               duration. Every call below goes through it, so
                               no call path — scoring, box ask, refine,
                               verify — can get around the limit, and every
                               request is timed and its token usage counted
                               by call kind there — and written as one
                               ``llm_calls`` row linked to its job
                               (``llm.usage``).
    call_kind()              — label -> score | ask | refine | verify |
                               select | describe | other (the metric label).
    usage_counts()           — a response's prompt / cached / completion /
                               reasoning token counts, None where absent.
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
because both calls produce them and nothing else reads them — and so do the
per-request ``classifier_llm_call_seconds`` histogram and the four
``classifier_llm_*_tokens_total`` counters ``_post`` records from each
response's ``usage``. Those are labelled by call KIND — score, ask, refine,
verify, select, describe, other — derived from the ``label`` every caller
already passes (:func:`call_kind`), so they say whether time goes to
prefill, reasoning, or retries without a criterion name ever becoming a
label value. ``_post`` also writes one INFO line per model request with the
same numbers (and the job id).

The counters cannot say what one JOB cost, and a log line does not survive
rotation, so ``_post`` also writes one row per request — ok or error — to the
``llm_calls`` table of ``classifier-db`` through ``llm.usage``: the job, the
criterion / item / document it was for (from context vars the queue and the
scheduler set), the model, the seconds, every token count and the raw
``usage`` object. Those rows outlive the job's TTL; ``GET /jobs/{id}/usage``
and ``GET /usage`` read them (``api.usage``).

Tests script the model by replacing ``call_vllm`` / ``call_vllm_json`` (the
calls) or ``_send`` (the bare transport under ``_post``, which keeps the limit
in play and measurable). ``_send`` keeps its one-argument signature for that
reason: the kind travels in ``_post``, which tests never replace.

Process flow position: ``call_vllm`` is the ``llm`` evaluator's scoring call
(``analysis.llm_eval``); ``call_vllm_json`` is every round of ``llm.boxes``.
"""

import asyncio
import base64
import json
import re
import time
import weakref

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
from llm import usage as llm_usage
from logger import logger

# Shared HTTP timeout applied to every vLLM request
_http_timeout = httpx.Timeout(HTTP_TIMEOUT, connect=HTTP_CONNECT_TIMEOUT)

# One pooled client per event loop, so a model call reuses a kept-alive
# connection instead of opening a new one. Per loop because an AsyncClient's
# connections belong to the loop that opened them; in the container there is
# exactly one, while the tests run several. Weak keys: a finished loop drops
# its client with it.
_clients: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncClient]" = (
    weakref.WeakKeyDictionary()
)


def _client() -> httpx.AsyncClient:
    """This event loop's shared client, created on first use."""
    loop = asyncio.get_running_loop()
    client = _clients.get(loop)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(timeout=_http_timeout)
        _clients[loop] = client
    return client


async def aclose() -> None:
    """Close this event loop's shared client. Called on app shutdown."""
    client = _clients.pop(asyncio.get_running_loop(), None)
    if client is not None:
        await client.aclose()

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

# Per-request breakdown by call kind. Kept beside the unlabelled histogram
# above rather than relabelling it, so an existing query on
# classifier_llm_latency_seconds keeps meaning what it meant. The label values
# are the fixed set in CALL_KINDS — never a criterion name.
_CALL_SECONDS_BUCKETS = (0.5, 1, 2, 5, 10, 20, 30, 45, 60, 90, 120, 180, 240, 300, 600)
llm_call_seconds = Histogram(
    "classifier_llm_call_seconds",
    "Vision-model HTTP request duration in seconds, by call kind and outcome "
    "(ok = a 2xx response, error = transport failure, timeout or non-2xx). "
    "One observation per request, so a parse retry is a second observation.",
    ["kind", "outcome"],
    buckets=_CALL_SECONDS_BUCKETS,
)
llm_prompt_tokens = Counter(
    "classifier_llm_prompt_tokens_total",
    "Prompt tokens reported in vision-model responses (usage.prompt_tokens), by call kind",
    ["kind"],
)
llm_cached_prompt_tokens = Counter(
    "classifier_llm_cached_prompt_tokens_total",
    "Prompt tokens served from vLLM's prefix cache "
    "(usage.prompt_tokens_details.cached_tokens; needs --enable-prompt-tokens-details), "
    "by call kind",
    ["kind"],
)
llm_completion_tokens = Counter(
    "classifier_llm_completion_tokens_total",
    "Completion tokens reported in vision-model responses (usage.completion_tokens, "
    "reasoning included), by call kind",
    ["kind"],
)
llm_reasoning_tokens = Counter(
    "classifier_llm_reasoning_tokens_total",
    "Reasoning tokens reported in vision-model responses "
    "(usage.completion_tokens_details.reasoning_tokens; a subset of completion tokens), "
    "by call kind",
    ["kind"],
)

# label prefix -> call kind. The callers' labels are "score/<name>",
# "score/<name>/ref<k>", "bbox/<name>#<n>", "refine/<name>#<n>",
# "verify/<name>#<n>", "reference/select#<item>" and "reference/describe";
# a label that matches none of them is "other". A new call site picks one of
# these prefixes (or adds a row here) so its tokens are attributed.
_KIND_PREFIXES: tuple[tuple[str, str], ...] = (
    ("score/", "score"),
    ("bbox/", "ask"),
    ("refine/", "refine"),
    ("verify/", "verify"),
    ("reference/select", "select"),
    ("reference/describe", "describe"),
)
CALL_KINDS: tuple[str, ...] = tuple(kind for _, kind in _KIND_PREFIXES) + ("other",)


def call_kind(label: str) -> str:
    """The low-cardinality call kind for a caller's ``label``."""
    for prefix, kind in _KIND_PREFIXES:
        if (label or "").startswith(prefix):
            return kind
    return "other"


def _count(value) -> int | None:
    """A non-negative int token count, or None for anything else."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if value >= 0 else None


def usage_counts(data) -> dict[str, int | None]:
    """Token counts from a completion's ``usage``; None for each one missing.

    vLLM reports ``prompt_tokens`` and ``completion_tokens`` always,
    ``completion_tokens_details.reasoning_tokens`` when a reasoning parser is
    on, and ``prompt_tokens_details.cached_tokens`` only with
    ``--enable-prompt-tokens-details``. Never raises: a response with no or
    odd ``usage`` simply reports nothing.
    """
    usage = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        usage = {}
    prompt_details = usage.get("prompt_tokens_details")
    completion_details = usage.get("completion_tokens_details")
    return {
        "prompt": _count(usage.get("prompt_tokens")),
        "cached": _count(
            prompt_details.get("cached_tokens") if isinstance(prompt_details, dict) else None
        ),
        "completion": _count(usage.get("completion_tokens")),
        "reasoning": _count(
            completion_details.get("reasoning_tokens")
            if isinstance(completion_details, dict) else None
        ),
    }


def _record_usage(kind: str, counts: dict[str, int | None]) -> None:
    """Add one response's token counts to the per-kind counters."""
    for key, counter in (
        ("prompt", llm_prompt_tokens),
        ("cached", llm_cached_prompt_tokens),
        ("completion", llm_completion_tokens),
        ("reasoning", llm_reasoning_tokens),
    ):
        if counts[key] is not None:
            counter.labels(kind=kind).inc(counts[key])


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
    response = await _client().post(VISION_LLM_API, json=prompt)
    response.raise_for_status()
    data = response.json()
    llm_latency.observe(time.monotonic() - t0)
    return data


async def _post(prompt: dict, *, label: str = "", attempt: int = 1) -> dict:
    """One model request holding a ``LLM_CALLS`` slot for exactly its duration.

    Parsing and retry decisions happen outside the slot, so a slow parse
    never holds a place another job's call could use.

    Every request — each retry included — is timed into
    ``classifier_llm_call_seconds{kind, outcome}``, its ``usage`` is added to
    the per-kind token counters, and one INFO line records the job, the kind,
    the label, the seconds and the four token counts. The timer runs inside
    the slot, so it measures the model, not the wait for a slot.

    Every request — ok or error — is also written as one ``llm_calls`` row
    (``llm.usage.record_call``), attributed to its job and unit from the
    context vars ``llm.usage`` holds. The write happens after the slot is
    released, so the database insert never holds a model slot, and it never
    raises: a failed accounting write cannot fail the call. A failed request
    is recorded (``http_status`` for a non-2xx, the exception as ``error``)
    and then re-raised unchanged.

    Args:
        prompt:  The chat-completion request body.
        label:   The caller's label; its kind (:func:`call_kind`) labels the
                 metrics, the label itself only reaches the log line.
        attempt: 1-based attempt number, for the log line.
    """
    kind = call_kind(label)
    failure: Exception | None = None
    data = None
    async with LLM_CALLS:
        started_at = llm_usage.now_iso()
        t0 = time.monotonic()
        try:
            data = await _send(prompt)
        except Exception as exc:
            # Kept, not raised here: the usage row is written below, AFTER
            # the slot is released, and then the failure is re-raised. A
            # cancellation (BaseException) is not caught and records nothing.
            failure = exc
        elapsed = time.monotonic() - t0
    job_id = llm_usage.job_id_var.get() or "-"
    if failure is not None:
        llm_call_seconds.labels(kind=kind, outcome="error").observe(elapsed)
        logger.info(
            "llm call job=%s kind=%s label=%s attempt=%d %.2fs outcome=error",
            job_id, kind, label, attempt, elapsed,
        )
        await llm_usage.record_call(
            label=label, kind=kind, attempt=attempt, prompt=prompt, data=None,
            error=failure, seconds=elapsed, started_at=started_at,
            counts=usage_counts(None),
        )
        raise failure
    llm_call_seconds.labels(kind=kind, outcome="ok").observe(elapsed)
    counts = usage_counts(data)
    _record_usage(kind, counts)
    logger.info(
        "llm call job=%s kind=%s label=%s attempt=%d %.2fs prompt=%s cached=%s "
        "completion=%s reasoning=%s",
        job_id, kind, label, attempt, elapsed,
        counts["prompt"], counts["cached"], counts["completion"], counts["reasoning"],
    )
    await llm_usage.record_call(
        label=label, kind=kind, attempt=attempt, prompt=prompt, data=data,
        error=None, seconds=elapsed, started_at=started_at, counts=counts,
    )
    return data


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
        label:     What this call is for: the log lines, and — through
                   :func:`call_kind` — the kind its metrics are labelled by.
                   Start it with ``score/`` or ``reference/...`` (see
                   ``_KIND_PREFIXES``).
        validator: Optional ``parsed -> answer`` function. A ``ValueError``
                   from it counts as a parse failure and retries, so "valid
                   JSON but no score in it" gets the same budget as "not
                   JSON at all". Its return value is what this returns.

    Returns:
        The model's JSON object, or ``validator``'s result for it.

    Raises:
        LLMCallError: The call failed; the message says why.
    """
    logger.debug(
        "call_vllm(%s): posting to %s model=%s (max_retries=%d)",
        label, VISION_LLM_API, VISION_LLM_MODEL, MAX_LLM_RETRIES,
    )
    last_exc: Exception | None = None
    for attempt in range(MAX_LLM_RETRIES):
        try:
            data = await _post(prompt, label=label, attempt=attempt + 1)
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
        label:  What this call was for: the log lines, and the metric kind
                (``bbox/`` -> ask, ``refine/``, ``verify/``).

    Returns:
        The parsed JSON object, or None when every attempt failed.
    """
    for attempt in range(MAX_LLM_RETRIES):
        try:
            parsed = _parse_content(await _post(prompt, label=label, attempt=attempt + 1))
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
