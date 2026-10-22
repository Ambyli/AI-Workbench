"""One classification through the document classifier's ``POST /assess``.

``ClassifierClient.assess(model=..., text=..., criteria=[...])`` runs the
whole cycle and returns an :class:`AssessOutcome` -- it never raises for a
remote failure; every one becomes a ``status``:

1. **Locality guard** (before anything is sent). ``GET /models`` (cached
   ``models_ttl_s``) must list ``model`` with ``authenticated: false`` and
   ``available: true``, and EVERY address its ``endpoint.host`` resolves to
   -- resolved here, from inside route-policy, which sits on ``ai_shared``
   like the classifier -- must be inside ``common.net.DEFAULT_BLOCKED_NETWORKS``
   (private, loopback, link-local). The classifier reads conversations, so
   only a model on this box's own networks may answer: an entry that sends a
   key, a host that resolves to any public address, or one that does not
   resolve at all is ``not_local`` and nothing is submitted. (Docker DNS
   answers only for RUNNING containers, so a stopped model service shows up
   as ``not_local`` too.) The host's verdict is cached per host for
   ``models_ttl_s`` -- an unresolved one for ``unresolved_ttl_s`` only, so a
   restarted model is classified again within seconds. An unknown or
   unavailable model is ``refused``.
2. ``POST /assess`` -- JSON: the transcript as ONE ``text`` document, one
   ``llm`` criterion per question with ``options: {hint: presence, ocr:
   never}``, and ``model``. ``202 {job_id}``.
3. ``GET /jobs/{job_id}`` every ``poll_s`` until ``completed`` / ``failed``,
   or until ``timeout_s`` (counted from the start of the cycle) has passed.
4. Read ``result.assessment.per_criterion_scores`` (each criterion's
   ``score`` and ``reason``, capped at 200 characters) and ``result.usage``.
5. **Always** ``DELETE /jobs/{job_id}`` -- on success, on failure, on a
   timeout (after ``POST /jobs/{job_id}/cancel``, which the classifier does
   not mount today; the DELETE is what counts), and when the task itself is
   cancelled at shutdown. ``/assess`` keeps the text as a payload until the
   job ends and as a text-layer artifact until ``JOB_TTL_HOURS``; the DELETE
   removes the row, the artifacts, the progress history and the job's
   ``llm_calls`` rows, so the usage numbers are copied into the outcome
   first. A job deleted while it still runs may write its artifact directory
   afterwards; the classifier's sweeper removes a directory whose job row is
   gone (``CLASSIFIER_ARTIFACT_SWEEP_INTERVAL_S``).

Statuses: ``ok`` (every criterion scored), ``incomplete`` (some did -- the
partial scores are returned), ``failed`` (the job failed, its result is
unreadable, or no criterion was scored), ``refused`` (a 4xx from ``/assess``,
or an unknown / unavailable model), ``unavailable`` (the classifier could not
be reached, or answered 5xx), ``timeout``, ``not_local``.

**The submitted text never leaves this object** other than in the
``/assess`` body: it is not logged, and an ``error`` that would quote it
(an echoing error page) is withheld.

Used by the analysis queue (``analysis.queue``) and by the benchmark
(``ai/route-policy/bench``), which therefore measures the production path.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import httpx

from common.net import DEFAULT_BLOCKED_NETWORKS

from .. import metrics

logger = logging.getLogger("route_policy.analysis")

REASON_MAX_CHARS = 200
ERROR_MAX_CHARS = 300
# One HTTP request (the cycle as a whole is bounded by ``timeout_s``).
CALL_TIMEOUT_S = 10.0
# Each cleanup request (cancel, DELETE): they run even past the deadline.
CLEANUP_TIMEOUT_S = 5.0
# An error that quotes this many consecutive characters of the submitted text
# is withheld.
_QUOTE_WINDOW = 24

_WAITING_PHASES = ("staging", "pending")
_TERMINAL_PHASES = ("completed", "failed")

# host -> every address it resolves to. Runs in a worker thread.
Resolver = Callable[[str], Iterable[str]]


def resolve_host(host: str) -> List[str]:
    """Every address ``host`` resolves to (``socket.getaddrinfo``, so IPv6
    too, and every address of a multi-homed name)."""
    return sorted({info[4][0] for info in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)})


@dataclass(frozen=True)
class CriterionScore:
    score: Optional[int]      # 1..10; None when that criterion errored
    reason: Optional[str]     # the classifier's reason for it, capped at 200 characters


@dataclass(frozen=True)
class AssessOutcome:
    status: str               # ok | incomplete | unavailable | refused | failed | timeout | not_local
    scores: Mapping[str, CriterionScore]   # keyed by criterion TEXT as sent; empty unless ok / incomplete
    job_id: Optional[str]
    calls: Optional[int]
    prompt_tokens: Optional[int]
    completion_tokens: Optional[int]
    model_seconds: Optional[float]
    queue_ms: Optional[float]  # submit -> first poll that saw the job running (± poll_s)
    latency_ms: float          # the cycle, up to the result (the DELETE not included)
    error: Optional[str]       # short, <= 300 chars, never containing the submitted text


class _Stop(Exception):
    """Ends a cycle early with a status and an error message."""

    def __init__(self, status: str, error: str) -> None:
        super().__init__(error)
        self.status = status
        self.error = error


@dataclass
class _Cycle:
    """What one assess() has learned so far."""

    started: float
    job_id: Optional[str] = None
    submitted_at: Optional[float] = None
    running_at: Optional[float] = None
    phase: Optional[str] = None
    timed_out: bool = False
    scores: Dict[str, CriterionScore] = field(default_factory=dict)
    usage: Dict[str, Any] = field(default_factory=dict)


def _int(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _number(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _cap(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _quotes(message: str, text: str) -> bool:
    """Does ``message`` quote the submitted ``text``?"""
    if not text:
        return False
    if len(text) < _QUOTE_WINDOW:
        return text in message
    return any(message[i:i + _QUOTE_WINDOW] in text for i in range(len(message) - _QUOTE_WINDOW + 1))


class ClassifierClient:
    """The ``/assess`` cycle against one classifier (``base_url``, e.g.
    ``http://classifier:8000``). One ``httpx.AsyncClient`` per instance;
    :meth:`aclose` at shutdown."""

    def __init__(self, base_url: str, *, timeout_s: float, poll_s: float,
                 models_ttl_s: float = 60.0,
                 unresolved_ttl_s: float = 2.0,
                 transport: Optional[httpx.AsyncBaseTransport] = None,
                 resolver: Optional[Resolver] = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = float(timeout_s)
        self.poll_s = float(poll_s)
        self.models_ttl_s = float(models_ttl_s)
        self.unresolved_ttl_s = float(unresolved_ttl_s)
        self._resolver = resolver or resolve_host
        self._clock = clock
        self._http = httpx.AsyncClient(transport=transport, timeout=CALL_TIMEOUT_S)
        self._models: Optional[Dict[str, Dict[str, Any]]] = None
        self._models_at = float("-inf")
        self._hosts: Dict[str, Tuple[float, Optional[str], float]] = {}

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- the cycle -----------------------------------------------------------------

    async def assess(self, *, model: str, text: str, criteria: Sequence[str]) -> AssessOutcome:
        criteria = list(criteria)
        cycle = _Cycle(started=self._clock())
        deadline = cycle.started + self.timeout_s
        try:
            await self._check_model(model, deadline)
            await self._submit(cycle, model, text, criteria, deadline)
            job = await self._wait(cycle, deadline)
            status, error = self._read(cycle, job, criteria)
        except _Stop as stop:
            status, error = stop.status, stop.error
        finally:
            ended = self._clock()
            if cycle.job_id is not None:
                # Also when this task is being cancelled (shutdown): the
                # cleanup requests are bounded, and the cancellation goes on
                # once they return.
                await self._discard(cycle.job_id, cancel=cycle.timed_out or cycle.phase not in _TERMINAL_PHASES)
        if error is not None:
            error = _cap(error, ERROR_MAX_CHARS)
            if _quotes(error, text):
                error = f"{status}: the classifier's message quoted the submitted text and was withheld"
        usable = status in ("ok", "incomplete")
        return AssessOutcome(
            status=status,
            scores=dict(cycle.scores) if usable else {},
            job_id=cycle.job_id,
            calls=_int(cycle.usage.get("calls")),
            prompt_tokens=_int(cycle.usage.get("prompt_tokens")),
            completion_tokens=_int(cycle.usage.get("completion_tokens")),
            model_seconds=_number(cycle.usage.get("seconds")),
            queue_ms=(round((cycle.running_at - cycle.submitted_at) * 1000.0, 1)
                      if cycle.running_at is not None and cycle.submitted_at is not None else None),
            latency_ms=round((ended - cycle.started) * 1000.0, 1),
            error=error,
        )

    def _remaining(self, deadline: float) -> float:
        return deadline - self._clock()

    def _call_timeout(self, deadline: float) -> float:
        return max(0.1, min(CALL_TIMEOUT_S, self._remaining(deadline)))

    # -- 1. the locality guard ----------------------------------------------------

    async def _registry(self, deadline: float) -> Dict[str, Dict[str, Any]]:
        now = self._clock()
        if self._models is not None and now - self._models_at < self.models_ttl_s:
            return self._models
        try:
            r = await self._http.get(f"{self.base_url}/models", timeout=self._call_timeout(deadline))
        except httpx.HTTPError as exc:
            raise _Stop("unavailable", f"GET /models: {type(exc).__name__}") from None
        if r.status_code != 200:
            raise _Stop("unavailable", f"GET /models answered {r.status_code}")
        try:
            listed = r.json().get("models")
            models = {m["id"]: m for m in listed if isinstance(m, dict) and isinstance(m.get("id"), str)}
        except (ValueError, AttributeError, TypeError):
            raise _Stop("unavailable", "GET /models answered something that is not the model registry") from None
        self._models, self._models_at = models, now
        return models

    async def _check_model(self, model: str, deadline: float) -> None:
        entry = (await self._registry(deadline)).get(model)
        if entry is None:
            raise _Stop("refused", f"model {model!r} is not in the classifier's registry (GET /models)")
        if entry.get("authenticated") is not False:
            raise _Stop("not_local", f"model {model!r} is called with an API key; only a local model "
                                     "may read conversations")
        if entry.get("available") is not True:
            raise _Stop("refused", f"model {model!r} is not available on the classifier: "
                                   f"{entry.get('unavailable_reason') or 'no reason given'}")
        endpoint = entry.get("endpoint")
        host = endpoint.get("host") if isinstance(endpoint, dict) else None
        if not isinstance(host, str) or not host:
            raise _Stop("not_local", f"model {model!r} has no endpoint host in GET /models")
        problem = await self._host_problem(host)
        if problem is not None:
            raise _Stop("not_local", f"model {model!r}: {problem}")

    async def _host_problem(self, host: str) -> Optional[str]:
        """None when every address ``host`` resolves to is private /
        loopback / link-local; else why not. Cached per host: a resolved
        verdict for ``models_ttl_s``, a host that did not resolve for
        ``unresolved_ttl_s`` only (Docker DNS answers only for running
        containers, so a restarting model is back in seconds)."""
        now = self._clock()
        cached = self._hosts.get(host)
        if cached is not None and now - cached[0] < cached[2]:
            return cached[1]
        problem: Optional[str] = None
        ttl = self.models_ttl_s
        try:
            addresses = list(await asyncio.to_thread(self._resolver, host))
        except Exception as exc:  # noqa: BLE001 -- gaierror, or anything a resolver raises
            addresses = []
            problem = f"endpoint host {host!r} could not be resolved ({type(exc).__name__})"
            ttl = self.unresolved_ttl_s
        if problem is None and not addresses:
            problem = f"endpoint host {host!r} resolved to no address"
            ttl = self.unresolved_ttl_s
        for raw in addresses:
            try:
                ip = ipaddress.ip_address(str(raw).split("%", 1)[0])
            except ValueError:
                problem = f"endpoint host {host!r} resolved to an unusable address"
                break
            if not any(ip.version == net.version and ip in net for net in DEFAULT_BLOCKED_NETWORKS):
                problem = f"endpoint host {host!r} resolves to a public address"
                break
        self._hosts[host] = (now, problem, ttl)
        return problem

    # -- 2. submit ---------------------------------------------------------------------

    async def _submit(self, cycle: _Cycle, model: str, text: str, criteria: List[str], deadline: float) -> None:
        body = {
            "documents": [{"type": "text", "data": text}],
            "criteria": [{"name": c, "type": "llm", "options": {"hint": "presence", "ocr": "never"}}
                         for c in criteria],
            "model": model,
        }
        try:
            r = await self._http.post(f"{self.base_url}/assess", json=body, timeout=self._call_timeout(deadline))
        except httpx.HTTPError as exc:
            # The type only: this request carries the transcript (analyses.error stores the reason).
            raise _Stop("unavailable", f"POST /assess: {type(exc).__name__}") from None
        if 400 <= r.status_code < 500:
            raise _Stop("refused", f"POST /assess answered {r.status_code}: {self._detail(r)}")
        if r.status_code not in (200, 202):
            raise _Stop("unavailable", f"POST /assess answered {r.status_code}")
        try:
            job_id = r.json()["job_id"]
        except (ValueError, KeyError, TypeError):
            job_id = None
        if not isinstance(job_id, str) or not job_id:
            raise _Stop("failed", "POST /assess answered without a job id")
        cycle.job_id = job_id
        cycle.submitted_at = self._clock()

    @staticmethod
    def _detail(r: httpx.Response) -> str:
        try:
            detail = r.json().get("detail")
        except (ValueError, AttributeError):
            detail = None
        if detail is None:
            return "no detail"
        return detail if isinstance(detail, str) else str(detail)

    # -- 3. poll -----------------------------------------------------------------------

    async def _wait(self, cycle: _Cycle, deadline: float) -> Dict[str, Any]:
        while True:
            remaining = self._remaining(deadline)
            if remaining <= 0:
                cycle.timed_out = True
                raise _Stop("timeout", f"no result within {self.timeout_s:g}s "
                                       f"(job {cycle.job_id}, last phase {cycle.phase or 'unknown'})")
            await asyncio.sleep(min(self.poll_s, remaining))
            try:
                r = await self._http.get(f"{self.base_url}/jobs/{cycle.job_id}",
                                         timeout=self._call_timeout(deadline))
            except httpx.HTTPError:
                continue  # transient; the deadline bounds the retries
            if r.status_code == 404:
                raise _Stop("failed", f"job {cycle.job_id} disappeared from the classifier")
            if r.status_code != 200:
                continue
            try:
                job = r.json()
                phase = job.get("phase")
            except (ValueError, AttributeError):
                continue
            cycle.phase = phase if isinstance(phase, str) else None
            if cycle.running_at is None and cycle.phase is not None and cycle.phase not in _WAITING_PHASES:
                cycle.running_at = self._clock()
            if cycle.phase in _TERMINAL_PHASES:
                return job

    # -- 4. read -------------------------------------------------------------------------

    def _read(self, cycle: _Cycle, job: Dict[str, Any], criteria: List[str]) -> Tuple[str, Optional[str]]:
        if cycle.phase == "failed":
            error = job.get("error")
            return "failed", f"the job failed: {error}" if isinstance(error, str) and error else "the job failed"
        result = job.get("result")
        assessment = result.get("assessment") if isinstance(result, dict) else None
        per = assessment.get("per_criterion_scores") if isinstance(assessment, dict) else None
        if not isinstance(per, dict):
            return "failed", "the job completed without a readable assessment"
        usage = result.get("usage")
        cycle.usage = usage if isinstance(usage, dict) else {}
        errors: List[str] = []
        for name in criteria:
            entry = per.get(name)
            if not isinstance(entry, dict):
                cycle.scores[name] = CriterionScore(None, None)
                errors.append("a criterion is missing from the result")
                continue
            score = _int(entry.get("score")) if entry.get("status") == "ok" else None
            reason = entry.get("reason")
            cycle.scores[name] = CriterionScore(score, _cap(reason, REASON_MAX_CHARS)
                                                if isinstance(reason, str) and reason else None)
            if score is None:
                err = entry.get("error")
                errors.append(err if isinstance(err, str) and err else f"status {entry.get('status')!r}")
        scored = sum(1 for s in cycle.scores.values() if s.score is not None)
        if scored == 0:
            return "failed", f"no criterion was scored: {errors[0]}" if errors else "no criterion was scored"
        if scored < len(criteria) or assessment.get("complete") is not True:
            return "incomplete", (f"{len(errors)} of {len(criteria)} criteria not scored: {errors[0]}"
                                  if errors else "the classifier marked the assessment incomplete")
        return "ok", None

    # -- 5. cleanup ------------------------------------------------------------------------

    async def _discard(self, job_id: str, *, cancel: bool) -> None:
        """Cancel (when the job may still run) and delete the job. Never
        raises; a DELETE that fails is logged and counted -- the job then
        expires with the classifier's JOB_TTL_HOURS."""
        if cancel:
            try:
                await self._http.post(f"{self.base_url}/jobs/{job_id}/cancel", timeout=CLEANUP_TIMEOUT_S)
            except httpx.HTTPError:
                pass  # best effort; not mounted by the classifier today
        try:
            r = await self._http.delete(f"{self.base_url}/jobs/{job_id}", timeout=CLEANUP_TIMEOUT_S)
        except httpx.HTTPError as exc:
            problem = f"{type(exc).__name__}"
        else:
            if r.status_code in (200, 202, 204, 404):
                return
            problem = f"answered {r.status_code}"
        metrics.errors_total.labels("classifier_delete").inc()
        logger.warning("analysis: DELETE /jobs/%s on the classifier failed (%s); the job (and its text) "
                       "stays until the classifier's JOB_TTL_HOURS", job_id, problem)
