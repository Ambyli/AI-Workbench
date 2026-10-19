"""The process-wide objects the routes share, and the two writes.

``Service`` is built by the app's lifespan and hung on ``app.state.service``:

    store         the ruleset cache (rulesets.store.RulesetStore)
    database      route-policy-db (db.pool.Database), or None in DB-less tests
    rulesets_dir  where refs in a POST /evaluate document resolve

``record_decision`` and ``record_outcome`` turn an engine result / an
/observe body into rows, idempotently, and keep the metrics.

**A decision is returned even when it cannot be stored.** Routing must never
depend on the log: a failed write is counted
(``route_policy_errors_total{kind="decision_write"}``) and logged, and the
caller still gets the decision.

**And it is returned in time.** LiteLLM awaits /decide for every ``auto``
turn within 0.25 s and sends the turn to local-private past it, so a write
never holds the answer for more than ``write_budget_s``
(ROUTE_POLICY_DECISION_WRITE_BUDGET_S, 0.05 s): it runs as its own task,
shielded from the request's cancellation; the request waits for it up to the
budget. In time, everything behaves as if the write were synchronous -- a
duplicate decision_id replays the FIRST stored response. Late (a hung
database, an exhausted pool), the computed response goes out at once and the
write finishes in the background (``route_policy_writes_deferred_total``);
a background write that then fails is counted and logged like any other.
At most ``max_in_flight`` writes run at once (WRITES_MAX_IN_FLIGHT); past
that a write is dropped and counted (``decision_write_dropped`` /
``outcome_write_dropped``), never queued without bound. ``drain()`` (the
app's shutdown) waits for the rest up to WRITES_DRAIN_S, then cancels what is
left. The ``write_budget_s`` / ``max_in_flight`` / ``drain_s`` fields default
to the config values; tests set their own.

/observe gets the same treatment: an outcome stored in time answers
``{"stored": true|false}`` as before, a failure in time is a 503, and a late
one answers ``{"stored": null, "deferred": true}`` (the hook ignores the
body: it fires /observe and forgets it).
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
import time
from dataclasses import dataclass, field
from typing import Any, Coroutine, Dict, Optional, Set, Tuple

from . import config, metrics
from .api.schemas import DecideRequest, ObserveRequest
from .db.pool import Database
from .db.repositories import decisions as decisions_repo
from .db.repositories import outcomes as outcomes_repo
from .db.repositories.evaluations import EvaluationRecord
from .engine.pipeline import Decision
from .rulesets.store import RulesetStore

logger = logging.getLogger("route_policy")

STORED, DUPLICATE, NOT_STORED, DEFERRED, DROPPED = "stored", "duplicate", "not_stored", "deferred", "dropped"

# At most one "too many writes in flight" warning per this many seconds: a
# database that is down for minutes would otherwise log one per request.
_DROP_LOG_INTERVAL_S = 60.0


class OutcomeNotStored(RuntimeError):
    """An outcome could not be stored (no database, the write failed in time,
    or too many writes in flight). Already counted and logged; the route
    answers 503."""


@dataclass
class Service:
    store: RulesetStore
    database: Optional[Database]
    rulesets_dir: pathlib.Path
    write_budget_s: float = field(default_factory=lambda: config.DECISION_WRITE_BUDGET_S)
    max_in_flight: int = field(default_factory=lambda: config.WRITES_MAX_IN_FLIGHT)
    drain_s: float = field(default_factory=lambda: config.WRITES_DRAIN_S)
    _pending: Set["asyncio.Task[Any]"] = field(default_factory=set, init=False, repr=False)
    _last_drop_log: float = field(default=float("-inf"), init=False, repr=False)

    # -- decisions --------------------------------------------------------------

    async def record_decision(self, body: DecideRequest, decision: Decision,
                              response: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        """Store a decision, waiting for the write up to ``write_budget_s``.
        Returns (status, the response to send): the stored FIRST response on
        a duplicate decision_id seen in time, else ``response``. Status is
        stored | duplicate | not_stored | deferred | dropped."""
        if self.database is None:
            self._count(decision)
            return NOT_STORED, response
        record = decisions_repo.DecisionRecord(
            decision_id=body.decision_id,
            chat_id=body.chat_id,
            message_id=body.message_id,
            litellm_call_id=body.litellm_call_id,
            user_id=body.user_id,
            key_alias=body.key_alias,
            requested_model=body.requested_model,
            chosen_route=decision.route,
            applied=decision.apply,
            mode=decision.mode,
            ruleset_id=decision.ruleset.id,
            ruleset_version=decision.ruleset.version,
            reason=decision.reason,
            scores=decision.scores,
            backend_snapshot=({k: v.model_dump() for k, v in body.backends.items()}
                              if body.backends is not None else None),
            message_count=len(body.messages),
            tool_count=len(body.tools) if body.tools is not None else 0,
            latency_ms=decision.latency_ms,
            error="; ".join(decision.errors) or None,
            response=response,
        )
        evaluations = [
            EvaluationRecord(i, v.evaluator, v.type, v.role, v.route, v.weight, v.confidence, v.reason,
                             v.abstained, v.latency_ms, v.error, v.detail)
            for i, v in enumerate(decision.votes)
        ]
        task = self._spawn(self._write_decision(record, evaluations, decision))
        if task is None:
            metrics.errors_total.labels("decision_write_dropped").inc()
            self._log_drop("decision", body.decision_id)
            self._count(decision)
            return DROPPED, response
        try:
            status, stored = await asyncio.wait_for(asyncio.shield(task), self.write_budget_s)
        except asyncio.TimeoutError:
            metrics.writes_deferred_total.labels("decision").inc()
            logger.debug("decide: decision %s answered before its write finished (> %.3fs); "
                         "the write goes on in the background", body.decision_id, self.write_budget_s)
            return DEFERRED, response
        if status == DUPLICATE and isinstance(stored, dict):
            return DUPLICATE, stored
        return status, response

    async def _write_decision(self, record: decisions_repo.DecisionRecord, evaluations: list,
                              decision: Decision) -> Tuple[str, Optional[Dict[str, Any]]]:
        """The write itself, as its own task. Never raises: a failure is
        counted and logged here, whether or not the request still waits."""
        try:
            async with self.database.acquire() as conn:
                if await decisions_repo.insert(conn, record, evaluations):
                    self._count(decision)
                    return STORED, None
                stored = await decisions_repo.get_response(conn, record.decision_id)
        except Exception as exc:  # noqa: BLE001 -- the decision was (or is being) returned anyway
            metrics.errors_total.labels("decision_write").inc()
            logger.error("decide: decision %s computed but not stored: %r", record.decision_id, exc)
            self._count(decision)
            return NOT_STORED, None
        metrics.duplicates_total.labels("decide").inc()
        return DUPLICATE, stored if isinstance(stored, dict) else None

    # -- outcomes ---------------------------------------------------------------

    async def record_outcome(self, body: ObserveRequest) -> Optional[bool]:
        """Store an outcome, waiting up to ``write_budget_s``. True when
        stored, False on a duplicate, None when the write outlasted the budget
        (it goes on in the background). Raises OutcomeNotStored -- already
        counted -- when there is no database, too many writes are in flight,
        or the write failed in time (the route answers 503)."""
        if self.database is None:
            metrics.errors_total.labels("outcome_write").inc()
            logger.error("observe: outcome for %s not stored: no database configured", body.decision_id)
            raise OutcomeNotStored("no database configured")
        record = outcomes_repo.OutcomeRecord(
            decision_id=body.decision_id,
            litellm_call_id=body.litellm_call_id,
            response_id=body.response_id,
            served_model_group=body.served_model_group,
            served_deployment=body.served_deployment,
            overflow_reason=body.overflow_reason,
            finish_reason=body.finish_reason,
            status=body.status,
            error=body.error,
            tool_calls=list(body.tool_calls),
        )
        task = self._spawn(self._write_outcome(record))
        if task is None:
            metrics.errors_total.labels("outcome_write_dropped").inc()
            self._log_drop("outcome", body.decision_id)
            raise OutcomeNotStored("too many writes in flight")
        try:
            return await asyncio.wait_for(asyncio.shield(task), self.write_budget_s)
        except asyncio.TimeoutError:
            metrics.writes_deferred_total.labels("outcome").inc()
            logger.debug("observe: outcome for %s answered before its write finished (> %.3fs)",
                         body.decision_id, self.write_budget_s)
            return None

    async def _write_outcome(self, record: outcomes_repo.OutcomeRecord) -> bool:
        """The write itself, as its own task. A failure is counted and logged
        here, then raised as OutcomeNotStored (seen by the request only when it
        is still waiting; otherwise the task's done-callback swallows it)."""
        try:
            async with self.database.acquire() as conn:
                stored = await outcomes_repo.insert(conn, record)
        except Exception as exc:  # noqa: BLE001
            metrics.errors_total.labels("outcome_write").inc()
            logger.error("observe: outcome for %s not stored: %r", record.decision_id, exc)
            raise OutcomeNotStored(str(exc)) from exc
        if stored:
            # Bounded label: a caller's own status string is stored, not labelled.
            label = record.status if record.status in ("success", "failure") else "other"
            metrics.observations_total.labels(label).inc()
        else:
            metrics.duplicates_total.labels("observe").inc()
        return stored

    # -- background writes --------------------------------------------------------

    @property
    def pending_writes(self) -> int:
        return len(self._pending)

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> "Optional[asyncio.Task[Any]]":
        """Start a write as its own task, or None (and the coroutine closed)
        when ``max_in_flight`` are already running."""
        if len(self._pending) >= self.max_in_flight:
            coro.close()  # never started: no "coroutine was never awaited" warning
            return None
        task = asyncio.get_running_loop().create_task(coro)
        self._pending.add(task)
        metrics.writes_in_flight.set(len(self._pending))
        task.add_done_callback(self._finished)
        return task

    def _finished(self, task: "asyncio.Task[Any]") -> None:
        self._pending.discard(task)
        metrics.writes_in_flight.set(len(self._pending))
        # Retrieve the outcome so a failure nobody awaited (a late outcome
        # write, a cancellation at shutdown) is never "Task exception was never
        # retrieved". The write already counted and logged its own failure.
        if not task.cancelled():
            task.exception()

    def _log_drop(self, what: str, decision_id: Any) -> None:
        now = time.monotonic()
        if now - self._last_drop_log >= _DROP_LOG_INTERVAL_S:
            self._last_drop_log = now
            logger.warning("%s %s not stored: %d writes already in flight (WRITES_MAX_IN_FLIGHT) -- "
                           "route-policy-db is not keeping up; further drops are only counted for %.0fs "
                           "(route_policy_errors_total{kind=\"%s_write_dropped\"})",
                           what, decision_id, self.max_in_flight, _DROP_LOG_INTERVAL_S, what)

    async def drain(self, timeout_s: Optional[float] = None) -> int:
        """Shutdown: wait up to ``timeout_s`` (default ``drain_s``,
        WRITES_DRAIN_S) for the background writes, then cancel what is left and
        wait for the cancellations; cancel the store's background reads too.
        Returns how many writes were cancelled."""
        timeout_s = self.drain_s if timeout_s is None else timeout_s
        await self.store.cancel_background()
        pending = set(self._pending)
        if not pending:
            return 0
        _done, left = await asyncio.wait(pending, timeout=max(0.0, timeout_s))
        for task in left:
            task.cancel()
        if left:
            await asyncio.gather(*left, return_exceptions=True)
            logger.warning("shutdown: %d background write(s) did not finish within %.1fs and were cancelled",
                           len(left), timeout_s)
        return len(left)

    # -- metrics ---------------------------------------------------------------------

    @staticmethod
    def _count(decision: Decision) -> None:
        metrics.decisions_total.labels(decision.route, decision.mode, str(decision.apply).lower()).inc()
        metrics.decide_seconds.observe(decision.latency_ms / 1000.0)
        for v in decision.votes:
            metrics.evaluator_seconds.labels(v.evaluator, v.type).observe(v.latency_ms / 1000.0)
            outcome = "error" if v.error else ("abstain" if v.abstained else "vote")
            metrics.votes_total.labels(v.evaluator, v.type, outcome).inc()
        for invariant in decision.overrides:
            metrics.invariant_overrides_total.labels(invariant).inc()
