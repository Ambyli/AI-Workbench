"""The process-wide objects the routes share, and the two writes.

``Service`` is built by the app's lifespan and hung on ``app.state.service``:

    store           the ruleset cache (rulesets.store.RulesetStore)
    database        route-policy-db (db.pool.Database), or None in DB-less tests
    rulesets_dir    where refs in a POST /evaluate document resolve
    analysis_queue  the llm_classifier's background classifications
                    (analysis.queue.AnalysisQueue), or None when
                    ROUTE_POLICY_CLASSIFIER_URL is empty
    signals_client  the signals sidecar's client (common.signals.SignalsClient),
                    or None when ROUTE_POLICY_SIGNALS_URL is empty

``record_decision`` and ``record_outcome`` turn an engine result / an
/observe body into rows, idempotently, and keep the metrics.
``load_context`` puts what the pipeline reads besides the request onto the
request view before it runs -- the verdicts a ruleset's evaluators named
(``load_analyses``), for a ``pin: turn`` ruleset the turn's first decision
(``load_turn_pin``), and the signals its evaluators asked the sidecar for
(``load_signals``), all three concurrently; ``after_decide`` does what
/decide leaves until after its answer (below).

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

**Verdict lookups are bounded too.** ``load_analyses`` reads every key in ONE
query, within ``read_budget_s`` (config.DB_READ_BUDGET_S) on /decide; past
it, or with the database failing or absent, the context records why
(``analysis_lookup_error``) and an ``llm_classifier`` abstains ("lookup timed
out"). A ruleset without such evaluators names no keys and makes no query --
the live ``default`` ruleset is one.

**So is the turn's first decision.** For a ``pin: turn`` ruleset and a call
carrying both a chat id and a message id (and not a background task),
``load_turn_pin`` reads the EARLIEST stored decision of the same ruleset id
for that (chat, message) -- among the primaries for the primary, among that
ruleset's own shadow rows for a shadow -- in ONE query within the same
budget. Past it, on a failure or with no database, there is no pin and the
call is decided afresh as if it were the turn's first
(``route_policy_db_reads_cut_total{read="turn_pin"}`` /
``route_policy_errors_total{kind="turn_pin"}``). The pipeline applies the pin
(``engine.pipeline``); a call that found one enqueues no analysis requests.

**And so are the signals.** ``load_signals`` sends the ONE ``POST /signals``
the ruleset's evaluators asked for (``engine.pipeline.signal_requests``; no
request, no call -- the live ``default`` asks for none) within
``signals_budget_s`` (ROUTE_POLICY_SIGNALS_BUDGET_S, 0.1 s), telling the
sidecar ``deadline_ms`` = the budget minus SIGNALS_DEADLINE_MARGIN_S so it
answers with whatever finished first. Past the budget, on a failure, with no
client (an empty ROUTE_POLICY_SIGNALS_URL) or a sidecar that is not local,
``ctx.signals_error`` says why and the signal evaluators abstain (``pii``
votes from its patterns). Counted in ``route_policy_signals_total{outcome}``,
timed in ``route_policy_signals_seconds{kind}``, each answered task in
``route_policy_signal_tasks_total{task,status}``. POST /evaluate waits up to
SIGNALS_EVALUATE_TIMEOUT_S with no deadline cut. At startup,
``warm_signals`` sends each bound ruleset's warm-up (its example sets), so
the sidecar embeds them before the first turn needs them.

**After the answer** (``after_decide``), ONE background task per /decide,
counted with the writes against ``max_in_flight`` (past it the work is
dropped: ``shadow_dropped``):

1. the primary decision's analysis requests go to the queue;
2. each SHADOW ruleset bound beside the primary (``bindings.yaml``
   ``shadow:``) runs on the same request -- its own verdict lookup, turn pin
   and signals call (with the same budgets, so it measures what enforcing it
   would do; texts the sidecar saw for the primary are cache hits), the
   pipeline -- and is stored as a decisions row with id ``uuid5(primary id,
   "<ruleset>@<version>")`` and ``shadow_of`` = the primary id, with its own
   evaluations; then its analysis requests go to the queue.

The primary's response is never touched by any of it, and a shadow that
fails to load or to run is skipped and counted -- each shadow on its own,
so one that raises never costs the next (``shadow_error``). A repeated
/decide derives the same shadow ids, so nothing is written twice; the queue
deduplicates requests by key, and a worker skips a key that already has a
verdict.

**Exceptions are logged by type only**, here and in the engine: an
evaluator, a write or a shadow run all handle conversation-derived data,
and an exception's message can quote it.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import pathlib
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Coroutine, Dict, List, Optional, Set, Tuple

from . import config, metrics
from .analysis.queue import AnalysisQueue
from .signals import wire
from .api.schemas import DecideRequest, ObserveRequest
from .db.pool import Database
from .db.repositories import analyses as analyses_repo
from .db.repositories import decisions as decisions_repo
from .db.repositories import outcomes as outcomes_repo
from .db.repositories.evaluations import EvaluationRecord
from .engine import pipeline
from .engine.context import RequestContext, TurnPin
from .engine.pipeline import Decision
from .rulesets.model import CompiledRuleset
from .rulesets.store import Resolved, RulesetStore

logger = logging.getLogger("route_policy")

STORED, DUPLICATE, NOT_STORED, DEFERRED, DROPPED = "stored", "duplicate", "not_stored", "deferred", "dropped"

# At most one "too many writes in flight" warning per this many seconds: a
# database that is down for minutes would otherwise log one per request.
_DROP_LOG_INTERVAL_S = 60.0


def shadow_decision_id(primary_id: uuid.UUID, ruleset: CompiledRuleset) -> uuid.UUID:
    """The id of ``ruleset``'s shadow decision beside ``primary_id``: derived,
    so a repeated /decide writes the same rows (the first write wins)."""
    return uuid.uuid5(primary_id, f"{ruleset.id}@{ruleset.version}")


class OutcomeNotStored(RuntimeError):
    """An outcome could not be stored (no database, the write failed in time,
    or too many writes in flight). Already counted and logged; the route
    answers 503."""


@dataclass
class Service:
    store: RulesetStore
    database: Optional[Database]
    rulesets_dir: pathlib.Path
    analysis_queue: Optional[AnalysisQueue] = None
    write_budget_s: float = field(default_factory=lambda: config.DECISION_WRITE_BUDGET_S)
    max_in_flight: int = field(default_factory=lambda: config.WRITES_MAX_IN_FLIGHT)
    drain_s: float = field(default_factory=lambda: config.WRITES_DRAIN_S)
    read_budget_s: Optional[float] = field(default_factory=lambda: config.DB_READ_BUDGET_S)
    signals_client: Optional[Any] = None
    signals_budget_s: float = field(default_factory=lambda: config.SIGNALS_BUDGET_S)
    _pending: Set["asyncio.Task[Any]"] = field(default_factory=set, init=False, repr=False)
    _last_drop_log: float = field(default=float("-inf"), init=False, repr=False)

    # -- what the pipeline reads besides the request -----------------------------------

    async def load_context(self, ruleset: CompiledRuleset, ctx: RequestContext, decision_id: uuid.UUID, *,
                           shadow: bool, budget_s: Optional[float]) -> None:
        """Put the stored verdicts, the turn's first decision and the
        signals on ``ctx`` for ``ruleset``, the three concurrently: the reads
        each within ``budget_s``, the signals call within
        ``signals_budget_s``. ``decision_id`` is the decision being made (a
        shadow's derived id for a shadow); ``shadow`` picks the rows its pin
        is read from. Never raises: a read or call that does not happen is
        recorded or counted, and the decision goes on without it."""
        await asyncio.gather(
            self.load_analyses(ruleset, ctx, budget_s=budget_s),
            self.load_turn_pin(ruleset, ctx, decision_id, shadow=shadow, budget_s=budget_s),
            self.load_signals(ruleset, ctx, decision_id, kind="shadow" if shadow else "primary",
                              budget_s=self.signals_budget_s),
        )

    async def load_signals(self, ruleset: CompiledRuleset, ctx: RequestContext, decision_id: Optional[uuid.UUID],
                           *, kind: str, budget_s: float, deadline: bool = True) -> None:
        """Send the ONE signals request ``ruleset``'s evaluators asked for and
        put the answer on ``ctx`` (``ctx.signals`` / ``signals_error`` /
        ``signals_ms``), within ``budget_s``; with ``deadline`` the sidecar is
        told to answer ``SIGNALS_DEADLINE_MARGIN_S`` before it. No request,
        no call. Never raises: anything unexpected (a bug -- the client itself
        never raises) leaves ``signals_error = "failed"``, so the signal
        evaluators abstain and ``pii`` votes from its patterns, and is counted
        (``route_policy_errors_total{kind="signals"}``) and logged by type."""
        try:
            await self._load_signals(ruleset, ctx, decision_id, kind=kind, budget_s=budget_s, deadline=deadline)
        except Exception as exc:  # noqa: BLE001 -- the decision goes on without signals
            ctx.signals_error = "failed"   # what a failed call says (signals.wire); evaluators read it first
            metrics.errors_total.labels("signals").inc()
            logger.warning("decide: signals for %s@%d (%s) failed: %s", ruleset.id, ruleset.version, kind,
                           type(exc).__name__)

    async def _load_signals(self, ruleset: CompiledRuleset, ctx: RequestContext, decision_id: Optional[uuid.UUID],
                            *, kind: str, budget_s: float, deadline: bool) -> None:
        batch = pipeline.signal_requests(ruleset, ctx)
        if batch is None:
            return
        if self.signals_client is None or not batch.tasks:
            wire.apply(ctx, batch, None if batch.tasks else wire.NOTHING_SENT)
            if batch.tasks:
                metrics.signals_total.labels("unavailable").inc()
            return
        deadline_ms = (max(1, int(round((budget_s - config.SIGNALS_DEADLINE_MARGIN_S) * 1000)))
                       if deadline else None)
        started = time.perf_counter()
        try:
            request = wire.build_request(batch, request_id=str(decision_id) if decision_id else None,
                                         deadline_ms=deadline_ms)
            # The client keeps its own budget and never raises; this is the
            # belt to its braces, so /decide's time never depends on it.
            outcome = await asyncio.wait_for(self.signals_client.fetch(request, budget_s=budget_s),
                                             budget_s + config.SIGNALS_DEADLINE_MARGIN_S)
        except asyncio.TimeoutError:
            outcome = wire.TIMED_OUT
        except Exception as exc:  # noqa: BLE001 -- the client should never raise; the evaluators abstain
            logger.warning("decide: signals call for %s@%d raised %s", ruleset.id, ruleset.version,
                           type(exc).__name__)
            outcome = wire.FAILED
        metrics.signals_seconds.labels(kind).observe(time.perf_counter() - started)
        label = wire.apply(ctx, batch, outcome)
        metrics.signals_total.labels(label).inc()
        for task in ctx.signals.values():
            metrics.signal_tasks_total.labels(*wire.task_labels(task)).inc()
        logger.debug("decide: signals for %s@%d (%s): %s in %s ms, tasks %s", ruleset.id, ruleset.version, kind,
                     label, ctx.signals_ms, ", ".join(f"{t.task} {t.status}" for t in ctx.signals.values()))

    async def warm_signals(self, timeout_s: Optional[float] = None) -> int:
        """Startup: send every bound ruleset's (primary and shadow) signal
        warm-ups -- the example sets the sidecar should embed before the
        first turn needs them. Returns how many were answered; never raises
        and never stops startup."""
        if self.signals_client is None:
            return 0
        timeout_s = config.SIGNALS_WARM_TIMEOUT_S if timeout_s is None else timeout_s
        rulesets: Dict[Tuple[str, int], CompiledRuleset] = {}
        for binding in self.store.bindings:
            for ruleset_id, version in ((binding.ruleset_id, binding.version), *binding.shadows):
                compiled = await self.store.get(ruleset_id, version)
                if compiled is not None:
                    rulesets.setdefault((compiled.id, compiled.version), compiled)
        answered = 0
        for batch in pipeline.signal_warmups(list(rulesets.values())):
            try:
                request = wire.build_request(batch, request_id="warmup", deadline_ms=int(timeout_s * 1000))
                outcome = await asyncio.wait_for(self.signals_client.fetch(request, budget_s=timeout_s),
                                                 timeout_s + 1.0)
            except Exception as exc:  # noqa: BLE001 -- the set is embedded on its first real use instead
                logger.warning("startup: signals warm-up (%s) failed: %s", ", ".join(t.task for t in batch.tasks),
                               type(exc).__name__)
                continue
            ctx = RequestContext(requested_model="warmup")
            label = wire.apply(ctx, batch, outcome)
            if label == "ok":
                answered += 1
            logger.info("startup: signals warm-up %s: %s (%s)", ", ".join(t.task for t in batch.tasks), label,
                        ", ".join(f"{t.task} {t.status}" for t in ctx.signals.values()) or ctx.signals_error)
        return answered

    async def load_turn_pin(self, ruleset: CompiledRuleset, ctx: RequestContext, decision_id: uuid.UUID, *,
                            shadow: bool, budget_s: Optional[float]) -> None:
        """For a ``pin: turn`` ruleset: put the turn's first decision on
        ``ctx`` (``ctx.turn_pin``), in one query within ``budget_s``. Nothing
        to read (no pin, an id missing, a background task) or no database:
        no query. A timeout or a failure is counted and leaves no pin."""
        if ruleset.pin != "turn" or not ctx.chat_id or not ctx.message_id or ctx.background_task:
            return
        if self.database is None:
            return
        try:
            found = await asyncio.wait_for(
                self._read_turn_pin(ctx.chat_id, ctx.message_id, ruleset.id, shadow, decision_id), budget_s)
        except asyncio.TimeoutError:
            metrics.db_reads_cut_total.labels("turn_pin").inc()
            logger.debug("decide: the turn pin of %s for %s outlasted %s s; decided afresh",
                         ruleset.id, decision_id, budget_s)
            return
        except Exception as exc:  # noqa: BLE001 -- no pin; the decision goes on
            metrics.errors_total.labels("turn_pin").inc()
            logger.warning("decide: turn pin lookup for %s@%d failed: %s", ruleset.id, ruleset.version,
                           type(exc).__name__)
            return
        if found is not None:
            ctx.turn_pin = TurnPin(*found)

    async def _read_turn_pin(self, chat_id: str, message_id: str, ruleset_id: str, shadow: bool,
                             decision_id: uuid.UUID):
        async with self.database.acquire() as conn:
            return await decisions_repo.first_of_turn(conn, chat_id=chat_id, message_id=message_id,
                                                      ruleset_id=ruleset_id, shadow=shadow, exclude=decision_id)

    async def load_analyses(self, ruleset: CompiledRuleset, ctx: RequestContext, *,
                            budget_s: Optional[float]) -> None:
        """Put the verdicts ``ruleset``'s evaluators asked for on ``ctx``
        (``ctx.analyses``), in one query within ``budget_s`` (None = wait as
        long as it takes). On a timeout, a failure or no database, record why
        in ``ctx.analysis_lookup_error`` instead. No keys, no query."""
        keys = pipeline.analysis_lookups(ruleset, ctx)
        if not keys:
            return
        if self.database is None:
            ctx.analysis_lookup_error = "unavailable (no database)"
            return
        try:
            ctx.analyses = await asyncio.wait_for(self._read_analyses(keys), budget_s)
        except asyncio.TimeoutError:
            metrics.db_reads_cut_total.labels("analysis_lookup").inc()
            ctx.analysis_lookup_error = "timed out"
        except Exception as exc:  # noqa: BLE001 -- the llm_classifier abstains; the decision goes on
            metrics.errors_total.labels("analysis_lookup").inc()
            logger.warning("decide: verdict lookup for %s@%d failed: %s", ruleset.id, ruleset.version,
                           type(exc).__name__)
            ctx.analysis_lookup_error = "failed"

    async def _read_analyses(self, keys: List[str]):
        async with self.database.acquire() as conn:
            return await analyses_repo.get_many(conn, keys)

    # -- after the answer --------------------------------------------------------------

    def after_decide(self, body: DecideRequest, decision: Decision, resolved: Resolved,
                     ctx: RequestContext) -> bool:
        """Start /decide's after-the-answer work as one background task: the
        primary's analysis requests, then every shadow ruleset. Returns False
        when there was nothing to do or the task was dropped."""
        if not resolved.shadows and self.analysis_queue is None:
            return False
        task = self._spawn(self._after_decide(body, decision, resolved, ctx))
        if task is None:
            metrics.errors_total.labels("shadow_dropped").inc()
            self._log_drop("shadow decisions for", body.decision_id, "shadow_dropped")
            return False
        return True

    async def _after_decide(self, body: DecideRequest, decision: Decision, resolved: Resolved,
                            ctx: RequestContext) -> None:
        # Let the request finish sending its answer before any of this work
        # (rendering transcripts, evaluating shadows) takes the event loop.
        await asyncio.sleep(0)
        try:
            self._enqueue(pipeline.analysis_requests(resolved.primary, ctx), decision.ruleset,
                          body.decision_id)
            shadows = await self.store.shadow_rulesets(resolved)
        except Exception as exc:  # noqa: BLE001 -- never reaches the request; counted and logged
            metrics.errors_total.labels("shadow_error").inc()
            logger.error("decide: after-the-answer work for %s failed: %s", body.decision_id, type(exc).__name__)
            return
        # Each shadow on its own: one that raises is counted and skipped, and
        # the next still runs (default-signals must not depend on default-llm).
        for shadow in shadows:
            try:
                await self._run_shadow(body, decision, shadow)
            except Exception as exc:  # noqa: BLE001 -- never reaches the request; counted and logged
                metrics.errors_total.labels("shadow_error").inc()
                logger.error("decide: shadow %s@%d beside %s failed: %s", shadow.id, shadow.version,
                             body.decision_id, type(exc).__name__)

    async def _run_shadow(self, body: DecideRequest, primary: Decision, ruleset: CompiledRuleset) -> None:
        """Decide, store and count one shadow ruleset on the request."""
        ctx = body.context()
        shadow_id = shadow_decision_id(body.decision_id, ruleset)
        await self.load_context(ruleset, ctx, shadow_id, shadow=True, budget_s=self.read_budget_s)
        decision = await pipeline.run(ruleset, ctx)
        stored = await self._store_shadow(body, decision, shadow_id)
        if stored:
            self._count(decision)
            metrics.shadow_decisions_total.labels(f"{ruleset.id}@{ruleset.version}",
                                                  str(decision.route == primary.route).lower()).inc()
        logger.info("[shadow] %s %s@%d -> %s (%s) beside %s -> %s%s", shadow_id, ruleset.id, ruleset.version,
                    decision.route, decision.reason, body.decision_id, primary.route,
                    "" if stored else ", not stored")
        self._enqueue(pipeline.analysis_requests(ruleset, ctx), ruleset, shadow_id)

    async def _store_shadow(self, body: DecideRequest, decision: Decision, shadow_id: uuid.UUID) -> bool:
        """True when the shadow decision was stored now; False when it already
        was (a repeated /decide) or could not be (counted)."""
        if self.database is None:
            return True
        record, evaluations = self._records(body, decision, shadow_id, decision.response(str(shadow_id)),
                                            shadow_of=body.decision_id)
        try:
            async with self.database.acquire() as conn:
                return await decisions_repo.insert(conn, record, evaluations)
        except Exception as exc:  # noqa: BLE001
            metrics.errors_total.labels("decision_write").inc()
            logger.error("decide: shadow decision %s computed but not stored: %s", shadow_id, type(exc).__name__)
            return False

    def _enqueue(self, requests: List[Any], ruleset: CompiledRuleset, decision_id: uuid.UUID) -> None:
        if self.analysis_queue is None:
            return
        for request in requests:
            self.analysis_queue.submit(dataclasses.replace(
                request, ruleset_id=ruleset.id, ruleset_version=ruleset.version, decision_id=decision_id))

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
        record, evaluations = self._records(body, decision, body.decision_id, response)
        task = self._spawn(self._write_decision(record, evaluations, decision))
        if task is None:
            metrics.errors_total.labels("decision_write_dropped").inc()
            self._log_drop("decision", body.decision_id, "decision_write_dropped")
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

    @staticmethod
    def _records(body: DecideRequest, decision: Decision, decision_id: uuid.UUID, response: Dict[str, Any],
                 shadow_of: Optional[uuid.UUID] = None) -> Tuple[decisions_repo.DecisionRecord, list]:
        """The decisions row and its evaluations for one decided ruleset."""
        record = decisions_repo.DecisionRecord(
            decision_id=decision_id,
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
            shadow_of=shadow_of,
            pinned_from=decision.pinned_from,
            background_task=body.background_task,
        )
        evaluations = [
            EvaluationRecord(i, v.evaluator, v.type, v.role, v.route, v.weight, v.confidence, v.reason,
                             v.abstained, v.latency_ms, v.error, v.detail)
            for i, v in enumerate(decision.votes)
        ]
        return record, evaluations

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
            logger.error("decide: decision %s computed but not stored: %s", record.decision_id, type(exc).__name__)
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
            self._log_drop("outcome", body.decision_id, "outcome_write_dropped")
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
            logger.error("observe: outcome for %s not stored: %s", record.decision_id, type(exc).__name__)
            raise OutcomeNotStored(type(exc).__name__) from exc
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

    def _log_drop(self, what: str, decision_id: Any, kind: str) -> None:
        now = time.monotonic()
        if now - self._last_drop_log >= _DROP_LOG_INTERVAL_S:
            self._last_drop_log = now
            logger.warning("%s %s not stored: %d background tasks already in flight (WRITES_MAX_IN_FLIGHT) -- "
                           "route-policy-db is not keeping up; further drops are only counted for %.0fs "
                           "(route_policy_errors_total{kind=\"%s\"})",
                           what, decision_id, self.max_in_flight, _DROP_LOG_INTERVAL_S, kind)

    async def drain(self, timeout_s: Optional[float] = None) -> int:
        """Shutdown: wait up to ``timeout_s`` (default ``drain_s``,
        WRITES_DRAIN_S) for the background writes and after-the-answer tasks,
        then cancel what is left and wait for the cancellations; cancel the
        store's background reads too; then stop the analysis queue (its
        running classifications get their own ``drain_s``) and close the
        signals client. Returns how many background tasks were cancelled."""
        timeout_s = self.drain_s if timeout_s is None else timeout_s
        await self.store.cancel_background()
        pending = set(self._pending)
        left: Set["asyncio.Task[Any]"] = set()
        if pending:
            _done, left = await asyncio.wait(pending, timeout=max(0.0, timeout_s))
            for task in left:
                task.cancel()
            if left:
                await asyncio.gather(*left, return_exceptions=True)
                logger.warning("shutdown: %d background task(s) did not finish within %.1fs and were cancelled",
                               len(left), timeout_s)
        if self.analysis_queue is not None:
            await self.analysis_queue.stop()
        if self.signals_client is not None and hasattr(self.signals_client, "aclose"):
            try:
                await self.signals_client.aclose()
            except Exception as exc:  # noqa: BLE001 -- shutting down anyway
                logger.warning("shutdown: closing the signals client failed: %s", type(exc).__name__)
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
        if decision.pin_outcome is not None:
            metrics.pinned_total.labels(f"{decision.ruleset.id}@{decision.ruleset.version}",
                                        decision.pin_outcome).inc()
