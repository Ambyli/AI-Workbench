"""The analysis queue: background classifications for ``llm_classifier``.

``/decide`` answers first; then (``Service.after_decide``) every
``AnalysisRequest`` the decided rulesets produced is handed to
:meth:`AnalysisQueue.submit`, which never blocks and never fails the request:

* **Deduplicated** by ``input_key``: a key already queued or running is not
  queued again -- tool rounds of one user message, and a primary and a
  shadow that share an evaluator, ask for the same prefix.
* **Bounded**: at most ``maxsize`` requests wait (ROUTE_POLICY_ANALYSIS_QUEUE_MAX);
  past it a request is dropped and counted
  (``route_policy_analysis_dropped_total{reason="queue_full"}``). A dropped
  analysis costs one turn's verdict.
* **In memory only.** The request carries the rendered transcript, which must
  never be written anywhere route-policy keeps, so there is no durable queue:
  a restart loses what was waiting (``common.jobs``' Postgres queue would put
  the text in a payload). The classifier holds the text only while its job
  runs; the worker deletes the job as soon as it has read it.

``concurrency`` workers (ROUTE_POLICY_ANALYSIS_CONCURRENCY) each take one
request at a time -- so at most that many classifier jobs are open at once;
the classifier's own per-model ``max_concurrent`` bounds the model calls.
Each worker:

1. skips a key that already has a verdict (another copy of the service may
   have classified it during a swap, or a duplicate /decide);
2. runs the ``/assess`` cycle (``ClassifierClient.assess``: locality guard,
   submit, poll, read, DELETE);
3. turns the scores into a route (``verdict.choose``, criterion text ->
   route) and writes ONE ``analyses`` row, ``ON CONFLICT DO NOTHING``.

A failure at any step is a status in the row or a counted error
(``analysis_write``), never an exception out of the worker.

**Shutdown** (:meth:`stop`): stop accepting, drop what is still waiting
(``reason="shutdown"``), give the running classifications up to ``drain_s``
(WRITES_DRAIN_S), then cancel them -- a cancelled cycle still sends its
DELETE (bounded) before it ends.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, List, Optional, Set

from .. import metrics
from ..db.repositories import analyses as analyses_repo
from .classifier_client import AssessOutcome, ClassifierClient
from .model import USABLE_STATUSES, AnalysisRequest
from .verdict import choose

logger = logging.getLogger("route_policy.analysis")

# At most one "queue full" warning per this many seconds.
_DROP_LOG_INTERVAL_S = 60.0


class AnalysisQueue:
    def __init__(self, database: Any, client: ClassifierClient, *, maxsize: int, concurrency: int,
                 drain_s: float) -> None:
        self.database = database
        self.client = client
        self.maxsize = max(1, int(maxsize))
        self.concurrency = max(1, int(concurrency))
        self.drain_s = float(drain_s)
        self._queue: "Optional[asyncio.Queue[AnalysisRequest]]" = None
        self._keys: Set[str] = set()          # queued or running
        self._workers: "List[asyncio.Task[None]]" = []
        self._busy: "Set[asyncio.Task[None]]" = set()   # workers inside a classification
        self._accepting = False
        self._last_drop_log = float("-inf")

    # -- lifecycle -----------------------------------------------------------------

    def start(self) -> None:
        """Create the queue and the workers on the running loop."""
        loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(maxsize=self.maxsize)
        self._workers = [loop.create_task(self._worker(i), name=f"analysis-worker-{i}")
                         for i in range(self.concurrency)]
        self._accepting = True
        metrics.analysis_queue_depth.set(0)
        logger.info("analysis: queue ready (%d worker(s), at most %d waiting) -> %s",
                    self.concurrency, self.maxsize, self.client.base_url)

    async def stop(self, timeout_s: Optional[float] = None) -> int:
        """Stop accepting, drop what waits, wait up to ``timeout_s`` (default
        ``drain_s``) for the running classifications, cancel the rest, close
        the client. Returns how many running ones were cancelled."""
        timeout_s = self.drain_s if timeout_s is None else timeout_s
        self._accepting = False
        dropped = 0
        if self._queue is not None:
            while not self._queue.empty():
                request = self._queue.get_nowait()
                self._queue.task_done()
                self._keys.discard(request.input_key)
                dropped += 1
        if dropped:
            metrics.analysis_dropped_total.labels("shutdown").inc(dropped)
            logger.warning("shutdown: %d queued analysis request(s) dropped", dropped)
        metrics.analysis_queue_depth.set(0)
        cancelled = 0
        if self._queue is not None and self._busy:
            # Every request taken is task_done()'d when its worker finishes it
            # (the dropped ones were above), so join() returns as soon as the
            # running classifications are over -- not after the whole timeout.
            try:
                await asyncio.wait_for(self._queue.join(), max(0.0, timeout_s))
            except asyncio.TimeoutError:
                cancelled = len(self._busy)
                logger.warning("shutdown: %d running analysis(es) did not finish within %.1fs and were "
                               "cancelled (their classifier jobs are deleted on the way out)",
                               cancelled, timeout_s)
        for task in self._workers:
            task.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers = []
        await self.client.aclose()
        return cancelled

    # -- intake ----------------------------------------------------------------------

    @property
    def depth(self) -> int:
        return self._queue.qsize() if self._queue is not None else 0

    def submit(self, request: AnalysisRequest) -> bool:
        """Queue one request; True when queued. False when its key is already
        queued or running (not counted), when the queue is full or stopped
        (dropped and counted). Never blocks, never raises."""
        if self._queue is None or not self._accepting:
            metrics.analysis_dropped_total.labels("shutdown").inc()
            return False
        if request.input_key in self._keys:
            return False
        try:
            self._queue.put_nowait(request)
        except asyncio.QueueFull:
            metrics.analysis_dropped_total.labels("queue_full").inc()
            now = time.monotonic()
            if now - self._last_drop_log >= _DROP_LOG_INTERVAL_S:
                self._last_drop_log = now
                logger.warning("analysis: queue full (%d waiting, ROUTE_POLICY_ANALYSIS_QUEUE_MAX) -- the "
                               "classifier is not keeping up; further drops are only counted for %.0fs "
                               "(route_policy_analysis_dropped_total)", self.maxsize, _DROP_LOG_INTERVAL_S)
            return False
        self._keys.add(request.input_key)
        metrics.analysis_queue_depth.set(self._queue.qsize())
        return True

    # -- the workers ---------------------------------------------------------------------

    async def _worker(self, index: int) -> None:
        assert self._queue is not None
        task = asyncio.current_task()
        while True:
            request = await self._queue.get()
            metrics.analysis_queue_depth.set(self._queue.qsize())
            self._busy.add(task)
            try:
                await self.process(request)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 -- a bug in one analysis never stops the worker
                logger.exception("analysis: %s failed unexpectedly", request.input_key[:12])
            finally:
                self._busy.discard(task)
                self._keys.discard(request.input_key)
                self._queue.task_done()

    async def process(self, request: AnalysisRequest) -> Optional[bool]:
        """One request, start to finish. True when a row was stored, False
        when the prefix already had one, None when it was skipped or could
        not be stored."""
        if self.database is not None:
            try:
                async with self.database.acquire() as conn:
                    if await analyses_repo.exists(conn, request.input_key):
                        logger.debug("analysis: %s already classified; skipped", request.input_key[:12])
                        return None
            except Exception as exc:  # noqa: BLE001 -- classify anyway; the insert is idempotent
                logger.warning("analysis: could not check %s before classifying: %r", request.input_key[:12], exc)
        criteria = list(request.criteria.values())
        outcome = await self.client.assess(model=request.model, text=request.text, criteria=criteria)
        record = self.record(request, outcome)
        metrics.analyses_total.labels(request.model, outcome.status).inc()
        metrics.analysis_seconds.labels(request.model).observe(outcome.latency_ms / 1000.0)
        logger.info("[analysis] %s %s k=%d -> %s (%s; scores %s; %.1fs, queue %s) chat=%s",
                    request.input_key[:12], request.model, request.exchange_index, record.route or "-",
                    outcome.status, record.scores, outcome.latency_ms / 1000.0,
                    f"{outcome.queue_ms / 1000.0:.1f}s" if outcome.queue_ms is not None else "-",
                    request.chat_id or "-")
        if self.database is None:
            return None
        try:
            async with self.database.acquire() as conn:
                return await analyses_repo.insert(conn, record)
        except Exception as exc:  # noqa: BLE001 -- the verdict is lost; the next message asks again
            metrics.errors_total.labels("analysis_write").inc()
            logger.error("analysis: verdict for %s computed but not stored: %r", request.input_key[:12], exc)
            return None

    @staticmethod
    def record(request: AnalysisRequest, outcome: AssessOutcome) -> analyses_repo.AnalysisRecord:
        """The ``analyses`` row for one finished cycle: scores by ROUTE, the
        route ``verdict.choose`` picks, and the best-scoring criterion's
        reason (the winner's, when one won)."""
        by_route = {}
        reasons = {}
        for route, text in request.criteria.items():
            score = outcome.scores.get(text)
            by_route[route] = score.score if score is not None else None
            reasons[route] = score.reason if score is not None else None
        usable = outcome.status in USABLE_STATUSES
        route: Optional[str] = None
        confidence: Optional[float] = None
        reason: Optional[str] = None
        if usable:
            route, confidence = choose(by_route, order=list(request.criteria), min_score=request.min_score,
                                       otherwise=request.otherwise)
            scored = [r for r in request.criteria if by_route[r] is not None]
            if scored:
                top = route if route in reasons else max(scored, key=lambda r: by_route[r])
                reason = reasons.get(top)
        return analyses_repo.AnalysisRecord(
            input_key=request.input_key,
            fingerprint=request.fingerprint,
            model_id=request.model,
            ruleset_id=request.ruleset_id,
            ruleset_version=request.ruleset_version,
            evaluator_id=request.evaluator_id,
            chat_id=request.chat_id,
            message_id=request.message_id,
            decision_id=request.decision_id,
            exchange_index=request.exchange_index,
            input_chars=request.input_chars,
            route=route,
            confidence=confidence,
            scores=by_route if usable else {},
            reason=reason,
            status=outcome.status,
            error=outcome.error,
            classifier_job_id=outcome.job_id,
            calls=outcome.calls,
            prompt_tokens=outcome.prompt_tokens,
            completion_tokens=outcome.completion_tokens,
            model_seconds=outcome.model_seconds,
            queue_ms=outcome.queue_ms,
            latency_ms=outcome.latency_ms,
        )
