"""Talking to the models: one transport, two failure modes, a limit per model.

Every request is addressed to ONE registry entry (``llm.models.ModelSpec``,
from ``models.yaml``): its URL, its headers (the API key, when it has one),
its timeout and its own concurrency limit. A caller passes the spec it built
the prompt with; ``None`` means the registry's default.

    encode_image_to_base64() — BGR numpy array -> base64 JPEG for a data URI.
    aclose()                 — close the shared HTTP client (app shutdown).
    _post()                  — ONE HTTP request (``_send``) to the spec's URL,
                               holding a slot of that MODEL's limit
                               (``spec.limit``, models.yaml ``max_concurrent``)
                               for exactly its duration. Every call below goes
                               through it, so no call path — scoring, box ask,
                               refine, verify, select, describe — can get
                               around the limit, and every request is timed
                               and its token usage counted by call kind and
                               model there — and written as one ``llm_calls``
                               row linked to its job (``llm.usage``), with the
                               registry id and the URL actually posted to.
    call_kind()              — label -> score | ask | refine | verify |
                               select | describe | other (the metric label).
    usage_counts()           — a response's prompt / cached / completion /
                               reasoning token counts, None where absent.
    call_model()              — the SCORING call. Retries a parse failure up to
                               MAX_LLM_RETRIES; raises ``LLMCallError`` on an
                               HTTP failure or when every attempt was
                               unparseable. The scheduler turns that into
                               ``status: "error"`` for the ONE criterion that
                               asked — a model outage fails that criterion,
                               never the job.
    call_model_json()         — the generic JSON call the enforcement loop's
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
already passes (:func:`call_kind`), and by ``model`` — the REGISTRY id, a
fixed set from ``models.yaml`` — so they say where time goes without a
criterion name or a free-form string ever becoming a label value.
``llm_latency`` stays unlabelled on purpose, so an existing query on it keeps
meaning what it meant. ``_post`` also writes one INFO line per model request
with the same numbers (and the job id).

The counters cannot say what one JOB cost, and a log line does not survive
rotation, so ``_post`` also writes one row per request — ok or error — to the
``llm_calls`` table of ``classifier-db`` through ``llm.usage``: the job, the
criterion / item / document it was for (from context vars the queue and the
scheduler set), the model, the seconds, every token count and the raw
``usage`` object. Those rows outlive the job's TTL; ``GET /jobs/{id}/usage``
and ``GET /usage`` read them (``api.usage``).

Tests script the model by replacing ``call_model`` / ``call_model_json`` (the
calls) or ``_send`` (the bare transport under ``_post``, which keeps the limit
in play and measurable). ``_send`` keeps its one-argument signature for that
reason: the kind travels in ``_post``, which tests never replace, and so does
the model — ``_post`` sets ``_target`` (a ContextVar) to the spec for the
duration of the request, and ``_send`` reads the URL, the headers and the
timeout from it.

Process flow position: ``call_model`` is the ``llm`` evaluator's scoring call
(``analysis.llm_eval``); ``call_model_json`` is every round of ``llm.boxes``.
"""

import asyncio
import base64
import json
import re
import time
import weakref
from contextvars import ContextVar
from typing import Optional

import httpx
from prometheus_client import Counter, Histogram

from config import HTTP_CONNECT_TIMEOUT, MAX_LLM_RETRIES
from llm import usage as llm_usage
from llm.models import ModelSpec, get_registry
from logger import logger

# One pooled client per event loop, so a model call reuses a kept-alive
# connection instead of opening a new one. Per loop because an AsyncClient's
# connections belong to the loop that opened them; in the container there is
# exactly one, while the tests run several. Weak keys: a finished loop drops
# its client with it. The timeout is per REQUEST (the model's own
# ``timeout_s``), so the client carries none of its own.
_clients: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncClient]" = (
    weakref.WeakKeyDictionary()
)

# The model the request ``_send`` is about to make is addressed to — set by
# ``_post`` around exactly one ``_send`` call (see the module docstring).
_target: ContextVar[Optional[ModelSpec]] = ContextVar("classifier_llm_target", default=None)


def _client() -> httpx.AsyncClient:
    """This event loop's shared client, created on first use."""
    loop = asyncio.get_running_loop()
    client = _clients.get(loop)
    if client is None or client.is_closed:
        client = httpx.AsyncClient()
        _clients[loop] = client
    return client


async def aclose() -> None:
    """Close this event loop's shared client. Called on app shutdown."""
    client = _clients.pop(asyncio.get_running_loop(), None)
    if client is not None:
        await client.aclose()


def _spec(model: Optional[ModelSpec]) -> ModelSpec:
    """``model``, or the registry default when None."""
    return model if model is not None else get_registry().default

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------

llm_calls_total = Counter(
    "classifier_llm_calls_total",
    "Total LLM API calls by status and model (registry id)",
    ["status", "model"],  # status: success | retry | failed
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
    "Model HTTP request duration in seconds, by call kind, outcome and model "
    "(ok = a 2xx response, error = transport failure, timeout or non-2xx). "
    "One observation per request, so a parse retry is a second observation.",
    ["kind", "outcome", "model"],
    buckets=_CALL_SECONDS_BUCKETS,
)
llm_prompt_tokens = Counter(
    "classifier_llm_prompt_tokens_total",
    "Prompt tokens reported in model responses (usage.prompt_tokens), by call kind and model",
    ["kind", "model"],
)
llm_cached_prompt_tokens = Counter(
    "classifier_llm_cached_prompt_tokens_total",
    "Prompt tokens served from the server's prefix cache "
    "(usage.prompt_tokens_details.cached_tokens; needs --enable-prompt-tokens-details), "
    "by call kind and model",
    ["kind", "model"],
)
llm_completion_tokens = Counter(
    "classifier_llm_completion_tokens_total",
    "Completion tokens reported in model responses (usage.completion_tokens, "
    "reasoning included), by call kind and model",
    ["kind", "model"],
)
llm_reasoning_tokens = Counter(
    "classifier_llm_reasoning_tokens_total",
    "Reasoning tokens reported in model responses "
    "(usage.completion_tokens_details.reasoning_tokens; a subset of completion tokens), "
    "by call kind and model",
    ["kind", "model"],
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

    An OpenAI-compatible server reports ``prompt_tokens`` and
    ``completion_tokens`` always; vLLM adds
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


def _record_usage(kind: str, model: str, counts: dict[str, int | None]) -> None:
    """Add one response's token counts to the per-kind, per-model counters."""
    for key, counter in (
        ("prompt", llm_prompt_tokens),
        ("cached", llm_cached_prompt_tokens),
        ("completion", llm_completion_tokens),
        ("reasoning", llm_reasoning_tokens),
    ):
        if counts[key] is not None:
            counter.labels(kind=kind, model=model).inc(counts[key])


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

    Never called directly — only through ``_post``, which holds the model's
    limit and sets ``_target`` to the model the request is for: its URL, its
    headers (the API key, if any) and its per-request timeout come from
    there (the registry default if nothing set it). Tests replace THIS
    function to script the model with the limit in play.

    Raises:
        httpx.HTTPError: Transport failures and non-2xx statuses.
    """
    spec = _spec(_target.get())
    t0 = time.monotonic()
    response = await _client().post(
        spec.url,
        json=prompt,
        headers=spec.headers(),
        timeout=httpx.Timeout(spec.timeout_s, connect=HTTP_CONNECT_TIMEOUT),
    )
    response.raise_for_status()
    data = response.json()
    llm_latency.observe(time.monotonic() - t0)
    return data


async def _post(
    prompt: dict, *, label: str = "", attempt: int = 1, model: Optional[ModelSpec] = None,
) -> dict:
    """One model request holding a slot of ITS model's limit for exactly its
    duration.

    Parsing and retry decisions happen outside the slot, so a slow parse
    never holds a place another job's call could use. The slot is the
    model's own (``spec.limit``, models.yaml ``max_concurrent``): a slow CPU
    model backs its own callers up, never the GPU model's.

    Every request — each retry included — is timed into
    ``classifier_llm_call_seconds{kind, outcome, model}``, its ``usage`` is
    added to the per-kind, per-model token counters, and one INFO line
    records the job, the model, the kind, the label, the seconds and the four
    token counts. The timer runs inside the slot, so it measures the model,
    not the wait for a slot.

    Every request — ok or error — is also written as one ``llm_calls`` row
    (``llm.usage.record_call``), attributed to its job and unit from the
    context vars ``llm.usage`` holds, with the registry id (``model_id``)
    and the URL actually posted to (``api_url``). The write happens after the
    slot is released, so the database insert never holds a model slot, and it
    never raises: a failed accounting write cannot fail the call. A failed
    request is recorded (``http_status`` for a non-2xx, the exception as
    ``error``) and then re-raised unchanged.

    Args:
        prompt:  The chat-completion request body (built for ``model``).
        label:   The caller's label; its kind (:func:`call_kind`) labels the
                 metrics, the label itself only reaches the log line.
        attempt: 1-based attempt number, for the log line.
        model:   The registry entry to send it to; None = the default.
    """
    spec = _spec(model)
    kind = call_kind(label)
    failure: Exception | None = None
    data = None
    async with spec.limit:
        started_at = llm_usage.now_iso()
        t0 = time.monotonic()
        token = _target.set(spec)
        try:
            data = await _send(prompt)
        except Exception as exc:
            # Kept, not raised here: the usage row is written below, AFTER
            # the slot is released, and then the failure is re-raised. A
            # cancellation (BaseException) is not caught and records nothing.
            failure = exc
        finally:
            _target.reset(token)
        elapsed = time.monotonic() - t0
    job_id = llm_usage.job_id_var.get() or "-"
    if failure is not None:
        llm_call_seconds.labels(kind=kind, outcome="error", model=spec.id).observe(elapsed)
        logger.info(
            "llm call job=%s model=%s kind=%s label=%s attempt=%d %.2fs outcome=error",
            job_id, spec.id, kind, label, attempt, elapsed,
        )
        await llm_usage.record_call(
            label=label, kind=kind, attempt=attempt, prompt=prompt, data=None,
            error=failure, seconds=elapsed, started_at=started_at,
            counts=usage_counts(None), model_id=spec.id, api_url=spec.url,
        )
        raise failure
    llm_call_seconds.labels(kind=kind, outcome="ok", model=spec.id).observe(elapsed)
    counts = usage_counts(data)
    _record_usage(kind, spec.id, counts)
    logger.info(
        "llm call job=%s model=%s kind=%s label=%s attempt=%d %.2fs prompt=%s cached=%s "
        "completion=%s reasoning=%s",
        job_id, spec.id, kind, label, attempt, elapsed,
        counts["prompt"], counts["cached"], counts["completion"], counts["reasoning"],
    )
    await llm_usage.record_call(
        label=label, kind=kind, attempt=attempt, prompt=prompt, data=data,
        error=None, seconds=elapsed, started_at=started_at, counts=counts,
        model_id=spec.id, api_url=spec.url,
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


async def call_model(
    prompt: dict, *, label: str = "", validator=None, model: Optional[ModelSpec] = None,
) -> dict:
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
        model:     The registry entry the prompt was built for (None = the
                   default) — where it is sent, and under whose limit.

    Returns:
        The model's JSON object, or ``validator``'s result for it.

    Raises:
        LLMCallError: The call failed; the message says why.
    """
    spec = _spec(model)
    logger.debug(
        "call_model(%s): posting to %s model=%s (max_retries=%d)",
        label, spec.id, spec.served_name, MAX_LLM_RETRIES,
    )
    last_exc: Exception | None = None
    for attempt in range(MAX_LLM_RETRIES):
        try:
            data = await _post(prompt, label=label, attempt=attempt + 1, model=spec)
            result = _parse_content(data)
            if validator is not None:
                result = validator(result)
            llm_calls_total.labels(status="success", model=spec.id).inc()
            return result
        except httpx.HTTPError as exc:
            llm_calls_total.labels(status="failed", model=spec.id).inc()
            logger.error("call_model(%s): HTTP error: %s", label, exc)
            raise LLMCallError(f"model {spec.id!r} call failed: {exc}") from exc
        except (KeyError, IndexError) as exc:
            llm_calls_total.labels(status="failed", model=spec.id).inc()
            logger.error("call_model(%s): unexpected response shape: %s", label, exc)
            raise LLMCallError(f"unexpected response from model {spec.id!r}: {exc!r}") from exc
        except (json.JSONDecodeError, ValueError) as exc:
            llm_calls_total.labels(status="retry", model=spec.id).inc()
            logger.warning(
                "call_model(%s): parse failure on attempt %d/%d: %s",
                label, attempt + 1, MAX_LLM_RETRIES, exc,
            )
            last_exc = exc

    llm_calls_total.labels(status="failed", model=spec.id).inc()
    logger.error("call_model(%s): all %d attempts failed", label, MAX_LLM_RETRIES)
    raise LLMCallError(
        f"model {spec.id!r}'s answer could not be parsed after {MAX_LLM_RETRIES} "
        f"attempts: {last_exc}"
    )


async def call_model_json(
    prompt: dict, *, label: str = "", model: Optional[ModelSpec] = None,
) -> dict | None:
    """POST a prompt and return the parsed JSON object, or None.

    The difference from :func:`call_model` is the failure mode, and it is the
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
        model:  The registry entry the prompt was built for (None = default).

    Returns:
        The parsed JSON object, or None when every attempt failed.
    """
    spec = _spec(model)
    for attempt in range(MAX_LLM_RETRIES):
        try:
            parsed = _parse_content(
                await _post(prompt, label=label, attempt=attempt + 1, model=spec)
            )
            llm_calls_total.labels(status="success", model=spec.id).inc()
            return parsed
        except httpx.HTTPError as exc:
            llm_calls_total.labels(status="failed", model=spec.id).inc()
            logger.warning("call_model_json(%s): HTTP error: %s", label, exc)
            return None
        except (KeyError, IndexError) as exc:
            llm_calls_total.labels(status="failed", model=spec.id).inc()
            logger.warning(
                "call_model_json(%s): unexpected response shape: %s", label, exc
            )
            return None
        except (json.JSONDecodeError, ValueError) as exc:
            llm_calls_total.labels(status="retry", model=spec.id).inc()
            logger.warning(
                "call_model_json(%s): parse failure on attempt %d/%d: %s",
                label, attempt + 1, MAX_LLM_RETRIES, exc,
            )

    llm_calls_total.labels(status="failed", model=spec.id).inc()
    logger.error("call_model_json(%s): all %d attempts failed", label, MAX_LLM_RETRIES)
    return None
