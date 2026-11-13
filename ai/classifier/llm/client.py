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
    call_kind()              — label -> fast | escalate | score | ask |
                               refine | verify | select | describe | other
                               (the metric label; ``escalate`` is an adaptive
                               criterion's scoring call on the escalation
                               model, measured like ``score``).
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
    call_readout()            — the ONE-TOKEN readout (``options.mode:
                               "fast"``, a ``ModelSpec.fast_body`` prompt):
                               posted ONCE (never the parse-retry loop — the
                               same prompt gets the same logits), its answer
                               read from the first generated position's
                               probabilities (``common.logprobs
                               .option_distribution``). Raises
                               ``ReadoutIncomplete`` (coverage under
                               ``fast.min_coverage``; not a ``ValueError``)
                               or its subclass ``ReadoutCapped`` (labels
                               missing from the top-k could hold
                               ``fast.max_unseen_mass``), counted in
                               ``classifier_readout_incomplete_total{model,
                               kind, reason}``; a server's 4xx about
                               logprobs becomes the named ``LLMCallError``
                               "the server refused top_logprobs K".

The difference in failure mode between the first two is the whole reason
they are separate functions.

**Measured confidence** (``models.yaml`` ``confidence.method``, score calls
only). ``logprobs``: the score call's body asked for token probabilities
(``ModelSpec.body``); after parsing, ``common.logprobs`` reads the score's
distribution from them and the payload is dropped — it is never stored
(``llm_calls`` keeps ``usage`` and ``finish_reason`` only). ``sampling``: via
``n_param`` ONE request asks for n completions and holds n slots of the
model's limit (``ConcurrencyLimit.hold``; the registry refuses n above
``max_concurrent``); via ``repeat`` n requests each hold one. The reported
answer is the first parseable sample. Either way the answer dict gains the
private keys ``llm.confidence.assess`` reads; a missing or unreadable
measurement is a note and the fallback, never a failed call. A test that
replaces ``call_model`` returns plain answers and gets the fallback.

``llm_calls_total`` and ``llm_latency`` live here rather than in ``metrics``
because both calls produce them and nothing else reads them — and so do the
per-request ``classifier_llm_call_seconds`` histogram and the four
``classifier_llm_*_tokens_total`` counters ``_post`` records from each
response's ``usage``. Those are labelled by call KIND — fast, escalate, score,
ask, refine, verify, select, describe, other — derived from the ``label`` every caller
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
(``analysis.llm_eval``), ``call_readout`` its fast one; ``call_model_json`` is
every round of ``llm.boxes``.
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

from common.logprobs import (
    OptionReadout,
    from_samples,
    int_field_distribution,
    option_distribution,
    resolve_path,
)

from config import HTTP_CONNECT_TIMEOUT, MAX_LLM_RETRIES, SCORE_HI, SCORE_LO
from llm import confidence as llm_confidence
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
# A one-token readout (call_readout) that answered but could not be believed:
# reason `coverage` — under fast.min_coverage of the answer position's
# probability fell on the labels (or the response carried no logprobs);
# `cap` — labels missing from the top_logprobs list could hold
# fast.max_unseen_mass or more (ReadoutCapped). Each one fails its unit.
readout_incomplete = Counter(
    "classifier_readout_incomplete_total",
    "One-token readouts refused after the model answered, by model, call kind and "
    "reason (coverage | cap)",
    ["model", "kind", "reason"],
)

# label prefix -> call kind. The callers' labels are "score/<name>",
# "score/<name>/ref<k>", "fast/<name>" (a one-token readout, call_readout),
# "escalate/<name>" (an adaptive criterion's reasoned scoring call on the
# escalation model), "bbox/<name>#<n>", "refine/<name>#<n>",
# "verify/<name>#<n>", "reference/select#<item>" and "reference/describe"; a
# label that matches none of them is "other". A new call site picks one of
# these prefixes (or adds a row here, AHEAD of any more generic prefix it
# shares) so its tokens are attributed.
_KIND_PREFIXES: tuple[tuple[str, str], ...] = (
    ("fast/", "fast"),
    ("escalate/", "escalate"),
    ("score/", "score"),
    ("bbox/", "ask"),
    ("refine/", "refine"),
    ("verify/", "verify"),
    ("reference/select", "select"),
    ("reference/describe", "describe"),
)
CALL_KINDS: tuple[str, ...] = tuple(kind for _, kind in _KIND_PREFIXES) + ("other",)
# The kinds that are SCORING calls (a ModelSpec.body(..., "score") body), so
# call_model measures their confidence per the entry's confidence.method: the
# plain / guided scoring call and an adaptive criterion's escalation.
_SCORING_KINDS: frozenset[str] = frozenset({"score", "escalate"})


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


class AnswerUnparsed(LLMCallError):
    """The ``MAX_LLM_RETRIES`` unparseable answers case of
    :class:`LLMCallError`. Its message quotes the start of the model's last
    answer — which can carry a verdict and a confidence — so a caller that
    must not report a judgement (an adaptive ``score: false`` unit's
    ``detail.escalation.error``) tells it apart by type."""


class ReadoutIncomplete(RuntimeError):
    """A one-token readout answered, but too little of its answer position's
    probability fell on the labels (``fast.min_coverage``) — or the response
    carried no token probabilities to read.

    NOT a ``ValueError`` on purpose: nothing may mistake it for a parse
    failure and retry (a retry gets the same logits). ``reason`` is
    ``coverage`` here, ``cap`` on :class:`ReadoutCapped`; ``readout`` is the
    ``common.logprobs.OptionReadout`` (None when there was nothing to read).
    ``summary`` is the message without its remedy, for a caller that words
    its own.
    """

    reason = "coverage"

    def __init__(self, message: str, *, summary: Optional[str] = None, readout=None):
        super().__init__(message)
        self.summary = summary or message
        self.readout = readout


class ReadoutCapped(ReadoutIncomplete):
    """The probability cap: labels missing from the returned top-k could hold
    ``fast.max_unseen_mass`` or more of the probability (the readout's
    ``unseen_bound`` estimate), so its answer is not known well enough to
    report."""

    reason = "cap"


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
    slots: int = 1,
) -> dict:
    """One model request holding a slot of ITS model's limit for exactly its
    duration (``slots`` of them for a request the server runs as several
    sequences — a sampling score call with ``n``; ``ConcurrencyLimit.hold``
    takes them all at once, so two such calls never deadlock on half each).

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
    async with spec.limit.hold(slots):
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


def _parse_content(data: dict, index: int = 0) -> dict:
    """The JSON object in a completion's ``content`` (choice ``index``; a
    sampling call with ``n`` has several).

    With a reasoning parser the model's thinking lands in
    ``reasoning_content``; ``content`` is the answer and can be None if the
    budget ran out mid-reasoning — treated as a parse failure so the retry
    fires. The regex fallback survives a model that wraps its JSON in
    markdown fences despite ``json_object`` mode.

    Raises:
        KeyError / IndexError: The response is not a chat completion.
        ValueError: No JSON object in the content.
    """
    content = data["choices"][index]["message"].get("content") or ""
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
                   Start it with ``score/``, ``escalate/`` (an adaptive
                   criterion's escalation) or ``reference/...`` (see
                   ``_KIND_PREFIXES``).
        validator: Optional ``parsed -> answer`` function. A ``ValueError``
                   from it counts as a parse failure and retries, so "valid
                   JSON but no score in it" gets the same budget as "not
                   JSON at all". Its return value is what this returns.
        model:     The registry entry the prompt was built for (None = the
                   default) — where it is sent, and under whose limit.

    Returns:
        The model's JSON object, or ``validator``'s result for it. For a
        ``score/`` or ``escalate/`` call to an entry whose
        ``confidence.method`` measures something, a dict result also carries
        the private keys
        ``llm.confidence`` reads (``_score_dist``, ``_samples``,
        ``_confidence_note``) — see :func:`_measure`.

    Raises:
        LLMCallError: The call failed; the message says why.
    """
    spec = _spec(model)
    conf = spec.confidence
    # Only a SCORING call is measured (kind score, or escalate — the same
    # score body, ModelSpec.body's rule for the confidence request keys); box
    # / select / describe calls are sent and read exactly as before.
    measure = (
        conf.method if conf.method != "stated" and call_kind(label) in _SCORING_KINDS else None
    )
    repeat = measure == "sampling" and conf.sampling.via == "repeat"
    slots = conf.slots if measure == "sampling" else 1
    logger.debug(
        "call_model(%s): posting to %s model=%s (max_retries=%d, confidence=%s)",
        label, spec.id, spec.served_name, MAX_LLM_RETRIES, measure or "stated",
    )
    last_exc: Exception | None = None
    for attempt in range(MAX_LLM_RETRIES):
        try:
            if repeat:
                responses = await _post_repeat(
                    prompt, label=label, attempt=attempt + 1, model=spec, n=conf.sampling.n,
                )
            else:
                responses = [
                    await _post(prompt, label=label, attempt=attempt + 1, model=spec, slots=slots)
                ]
            result = _measure(responses, validator, measure, spec)
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
    raise AnswerUnparsed(
        f"model {spec.id!r}'s answer could not be parsed after {MAX_LLM_RETRIES} "
        f"attempts: {last_exc}"
    )


# ---------------------------------------------------------------------------
# Measured confidence (llm.confidence reads what these attach)
# ---------------------------------------------------------------------------


async def _post_repeat(
    prompt: dict, *, label: str, attempt: int, model: ModelSpec, n: int,
) -> list[dict]:
    """``sampling`` via ``repeat``: n concurrent requests of the same prompt,
    each its own ``_post`` (its own slot, timer, usage row). The bodies that
    answered, in request order; when none did, the first failure is raised
    (an HTTP error stays an HTTP error, so ``call_model`` does not retry it)."""
    results = await asyncio.gather(
        *(_post(prompt, label=label, attempt=attempt, model=model) for _ in range(n)),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, BaseException) and not isinstance(result, Exception):
            raise result  # a cancellation: the job is being torn down
    answered = [r for r in results if not isinstance(r, BaseException)]
    if not answered:
        raise next(r for r in results if isinstance(r, BaseException))
    return answered


def _logprob_distribution(data: dict, spec: ModelSpec):
    """``(Distribution | None, note | None)`` for the score in one response.

    Never raises: a response without logprobs, or with entries that cannot be
    read, is a note and the fallback — the scoring call itself never fails
    because of its confidence measurement.
    """
    cfg = spec.confidence.logprobs
    try:
        entries = resolve_path(data, cfg.entries_path)
        if not isinstance(entries, list) or not entries:
            return None, (
                f"logprobs: the response has no entries at {cfg.entries_path} — the "
                "server ignored the request, or keeps them elsewhere (models.yaml "
                "confidence.logprobs.entries_path)"
            )
        content = None
        message = data["choices"][0].get("message") if isinstance(data.get("choices"), list) else None
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            content = message["content"]
        dist = int_field_distribution(
            entries, llm_confidence.SCORE_KEY, SCORE_LO, SCORE_HI,
            content=content, top_key=cfg.top_key, number_tokens=cfg.number_tokens,
        )
    except Exception as exc:  # noqa: BLE001 — a measurement never fails the call
        logger.warning("logprobs: could not read model %s's entries: %s", spec.id, exc)
        return None, f"logprobs: the entries could not be read ({type(exc).__name__})"
    if dist is None:
        return None, "logprobs: no score value was found in the answer's token stream"
    return dist, None


def _drop_logprobs(data: dict) -> None:
    """Forget a response's logprob payload (tens of KB with reasoning) once it
    has been read. ``llm.usage.record_call`` already stored only ``usage`` and
    ``finish_reason``; nothing else keeps the body."""
    choices = data.get("choices") if isinstance(data, dict) else None
    for choice in choices if isinstance(choices, list) else []:
        if isinstance(choice, dict):
            choice.pop("logprobs", None)


def _measure(responses: list[dict], validator, measure: Optional[str], spec: ModelSpec):
    """The answer of a scoring call, with its measured confidence attached.

    ``measure`` None (stated, or not a score call): choice 0 parsed and
    validated — exactly the call's old behaviour. ``logprobs``: the same
    answer, plus the score distribution read from the response's token
    probabilities. ``sampling``: every choice of every response is parsed
    and validated, unparseable ones are dropped (when ALL are, the first
    failure is raised — a parse failure is then the normal retry); the
    REPORTED answer is the first that parsed, so sampling never changes the
    score, and the samples' scores make the distribution.
    """
    if measure != "sampling":
        data = responses[0]
        result = _parse_content(data)
        if validator is not None:
            result = validator(result)
        if measure == "logprobs":
            dist, note = _logprob_distribution(data, spec)
            _drop_logprobs(data)
            if isinstance(result, dict):
                result = {**result, llm_confidence.DIST_KEY: dist,
                          llm_confidence.NOTE_KEY: note}
        return result

    answers: list = []
    first_exc: Exception | None = None
    for data in responses:
        choices = data.get("choices") if isinstance(data, dict) else None
        count = len(choices) if isinstance(choices, list) and choices else 1
        for index in range(count):
            try:
                parsed = _parse_content(data, index)
                answers.append(validator(parsed) if validator is not None else parsed)
            except (json.JSONDecodeError, ValueError, KeyError, IndexError) as exc:
                first_exc = first_exc or exc
    if not answers:
        raise first_exc or ValueError("no choice in the sampling response")
    reported = answers[0]
    if not isinstance(reported, dict):
        return reported
    scored = [a for a in answers if isinstance(a, dict) and "score" in a]
    samples = {
        "asked": spec.confidence.sampling.n,
        "verdicts": [a.get("verdict") for a in scored],
        "stated": [a.get("confidence") for a in scored],
    }
    dist = from_samples([a["score"] for a in scored], SCORE_LO, SCORE_HI) if scored else None
    note = None if dist is not None else "sampling: no sample carried a score"
    return {**reported, llm_confidence.DIST_KEY: dist, llm_confidence.SAMPLES_KEY: samples,
            llm_confidence.NOTE_KEY: note}


# ---------------------------------------------------------------------------
# The one-token readout (options.mode "fast")
# ---------------------------------------------------------------------------


def _beside(value: float, threshold: float, *, below: bool) -> str:
    """``value`` printed with the fewest decimals (2 to 6) that keep it on
    the side of ``threshold`` the check found it on — strictly under it
    (``below``), else over it or equal to it — so a refusal message never
    reads "coverage 0.50 < 0.5" or "0.02 (fast.max_unseen_mass 0.02)" for a
    value a hair across the line."""
    for digits in range(2, 7):
        text = f"{value:.{digits}f}"
        shown = float(text)
        if (shown < threshold) if below else (shown > threshold or value == threshold):
            return text
    return repr(value)


def _refused_top_logprobs(exc: httpx.HTTPError) -> bool:
    """Whether an HTTP error is the server refusing the logprobs request: a
    4xx whose body mentions ``logprobs`` (vLLM's 400 for a ``top_logprobs``
    over its ``--max-logprobs``)."""
    response = getattr(exc, "response", None)
    if response is None or not 400 <= response.status_code < 500:
        return False
    try:
        text = response.text
    except Exception:  # noqa: BLE001 — an unreadable body is not this case
        return False
    return "logprobs" in (text or "").lower()


async def call_readout(
    prompt: dict, *, labels: tuple[str, ...], label: str = "", model: Optional[ModelSpec] = None,
) -> OptionReadout:
    """A ONE-TOKEN readout: post ``prompt`` (built by ``ModelSpec.fast_body``)
    ONCE and read the answer position's distribution over ``labels``.

    Posted exactly once: it never enters :func:`call_model`'s parse-retry
    loop — the same prompt gets the same logits, so a retry could only cost
    a slot. It goes through ``_post`` like every call, so it holds a slot of
    the model's ``max_concurrent``, is timed and token-counted, and is
    written as one ``llm_calls`` row; start ``label`` with ``fast/`` (call
    kind ``fast``). The logprob payload is dropped once read.

    The response is read at the entry's ``confidence.logprobs.entries_path``
    / ``top_key`` (where this provider keeps the entries) with
    ``common.logprobs.option_distribution``, then believed only when:

      * ``coverage`` (the probability on the labels) is at least
        ``fast.min_coverage`` — else :class:`ReadoutIncomplete`;
      * ``unseen_bound`` (an estimate of what labels missing from the top-k
        hold — one spelling each under the smallest listed probability, a
        heuristic rather than a guarantee: ``common.logprobs.options``) is
        under ``fast.max_unseen_mass`` — else :class:`ReadoutCapped`.

    Either refusal is counted in ``classifier_readout_incomplete_total{model,
    kind, reason}``; its message prints the measured value with enough
    decimals to show it on the threshold's far side (:func:`_beside`).

    Raises:
        ReadoutIncomplete / ReadoutCapped: As above.
        LLMCallError: An HTTP failure — a 4xx about logprobs is named ("the
            server refused top_logprobs K — its --max-logprobs is lower") —
            or a response that is not a chat completion (a body that is not
            JSON, no choices); or ``model`` has no ``fast:`` block. Each is
            counted ``llm_calls_total{status="failed"}``, and none is
            retried.
    """
    spec = _spec(model)
    if spec.fast is None:
        raise LLMCallError(f"model {spec.id!r} has no fast block in models.yaml")
    fast = spec.fast
    kind = call_kind(label)
    cfg = spec.confidence.logprobs
    try:
        data = await _post(prompt, label=label, attempt=1, model=spec)
    except httpx.HTTPError as exc:
        llm_calls_total.labels(status="failed", model=spec.id).inc()
        logger.error("call_readout(%s): HTTP error: %s", label, exc)
        if _refused_top_logprobs(exc):
            raise LLMCallError(
                f"the server refused top_logprobs {fast.top_logprobs} — its --max-logprobs is "
                "lower: raise it (ai/vllm) or lower models.yaml fast.top_logprobs "
                f"(model {spec.id!r})"
            ) from exc
        raise LLMCallError(f"model {spec.id!r} call failed: {exc}") from exc
    except (ValueError, KeyError) as exc:
        # A 2xx whose body is not JSON (a proxy's HTML page, a truncated
        # body: `_send`'s response.json() raises JSONDecodeError, a
        # ValueError). Named here rather than left a bare parse error, and
        # still not retried. ReadoutIncomplete is not a ValueError, and
        # `_post` never raises it.
        llm_calls_total.labels(status="failed", model=spec.id).inc()
        logger.error("call_readout(%s): unreadable response: %s", label, exc)
        what = "not JSON" if isinstance(exc, json.JSONDecodeError) else "unreadable"
        raise LLMCallError(f"unexpected response from model {spec.id!r}: {what} ({exc})") from exc
    if not isinstance(data, dict) or not isinstance(data.get("choices"), list) or not data["choices"]:
        llm_calls_total.labels(status="failed", model=spec.id).inc()
        raise LLMCallError(f"unexpected response from model {spec.id!r}: no choices")
    try:
        entries = resolve_path(data, cfg.entries_path)
        readout = option_distribution(entries, labels, top_key=cfg.top_key)
    except Exception as exc:  # noqa: BLE001 — a garbled stream is an incomplete readout
        logger.warning("call_readout(%s): could not read the entries: %s", label, exc)
        readout = None
    finally:
        _drop_logprobs(data)
    llm_calls_total.labels(status="success", model=spec.id).inc()

    if readout is None:
        readout_incomplete.labels(model=spec.id, kind=kind, reason="coverage").inc()
        summary = (
            f"readout incomplete: the response carried no token probabilities at "
            f"{cfg.entries_path} (models.yaml confidence.logprobs.entries_path)"
        )
        raise ReadoutIncomplete(summary, summary=summary)
    if readout.coverage < fast.min_coverage:
        readout_incomplete.labels(model=spec.id, kind=kind, reason="coverage").inc()
        summary = (
            f"readout incomplete: coverage {_beside(readout.coverage, fast.min_coverage, below=True)}"
            f" < {fast.min_coverage} "
            f"(models.yaml fast.min_coverage; the model wrote {readout.first_token!r})"
        )
        raise ReadoutIncomplete(summary, summary=summary, readout=readout)
    if readout.unseen_bound >= fast.max_unseen_mass:
        readout_incomplete.labels(model=spec.id, kind=kind, reason="cap").inc()
        bound = _beside(readout.unseen_bound, fast.max_unseen_mass, below=False)
        summary = (
            f"probability cap reached: {readout.n_unseen} of {len(readout.labels)} labels were "
            f"not among the top {fast.top_logprobs} alternatives and could hold up to "
            f"{bound} (fast.max_unseen_mass {fast.max_unseen_mass})"
        )
        knobs = (
            "raise models.yaml fast.top_logprobs together with the server's --max-logprobs"
        )
        raise ReadoutCapped(
            f"{summary} — {knobs}, or use mode 'reasoned'",
            summary=f"{summary}; {knobs}", readout=readout,
        )
    logger.debug(
        "call_readout(%s): %s coverage=%.3f unseen_bound=%.4f", label, readout.best(),
        readout.coverage, readout.unseen_bound,
    )
    return readout


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
