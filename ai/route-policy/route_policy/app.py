"""Route-policy service -- FastAPI application.

``create_app()`` builds the app; ``app`` (module level) is what uvicorn runs
(``uvicorn route_policy.app:app``, Dockerfile).

**Startup** (the lifespan), in order -- any failure stops the container with
the reason in its log, which is what a swap deploy wants (the old copy keeps
serving):

1. open the Postgres pool on route-policy-db (``db.pool.Database``);
2. apply pending migrations (``db.migrations.migrate``);
3. read and validate the baked-in rulesets and seed them, idempotently, with
   their bindings and shadow slots (``rulesets.store.seed_and_load``) -- a
   changed ruleset that kept its version number, a shadow ruleset bound as a
   primary or an enforce one in a shadow slot, fails here;
4. start the analysis queue (``analysis.queue.AnalysisQueue``: the
   llm_classifier's background classifications through the document
   classifier, ROUTE_POLICY_CLASSIFIER_URL) -- unless that URL is empty;
5. hang the ``Service`` (ruleset cache + database + queue) on ``app.state``.

Shutdown drains the background tasks (``Service.drain``: decision / outcome
writes that outlasted ROUTE_POLICY_DECISION_WRITE_BUDGET_S and /decide's
after-the-answer work get up to WRITES_DRAIN_S, then are cancelled), stops
the analysis queue (waiting classifications are dropped, running ones get
WRITES_DRAIN_S and then are cancelled -- each still deletes its classifier
job), then closes the pool.

A test can pass its own ``service`` -- e.g. a ruleset cache filled from the
files with no database -- and the lifespan then touches nothing; or its own
``analysis_client`` (a ``ClassifierClient`` on a fake transport) for the
queue the lifespan starts.
"""

from __future__ import annotations

import logging
import pathlib
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator

from . import config
from .analysis.classifier_client import ClassifierClient
from .analysis.queue import AnalysisQueue
from .api import chats, decide, evaluate, evaluator_types, health, observe, rulesets
from .db.migrations import migrate
from .db.pool import Database
from .rulesets.store import RulesetStore, seed_and_load
from .service import Service

logger = logging.getLogger("route_policy")


def configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("route_policy").setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))


def build_analysis_queue(database: Database, client: Optional[ClassifierClient]) -> Optional[AnalysisQueue]:
    """The analysis queue on ``client`` (default: one for
    ROUTE_POLICY_CLASSIFIER_URL), or None when there is no client and the URL
    is empty. Not started."""
    if client is None:
        if not config.CLASSIFIER_URL:
            logger.warning("startup: ROUTE_POLICY_CLASSIFIER_URL is empty -- no analysis queue; every "
                           "llm_classifier evaluator will abstain")
            return None
        client = ClassifierClient(config.CLASSIFIER_URL, timeout_s=config.CLASSIFY_TIMEOUT_S,
                                  poll_s=config.CLASSIFY_POLL_S, models_ttl_s=config.CLASSIFIER_MODELS_TTL_S)
    return AnalysisQueue(database, client, maxsize=config.ANALYSIS_QUEUE_MAX,
                         concurrency=config.ANALYSIS_CONCURRENCY, drain_s=config.WRITES_DRAIN_S)


async def start_service(database: Database, rulesets_dir: pathlib.Path,
                        analysis_client: Optional[ClassifierClient] = None) -> Service:
    """Steps 1-4 of startup; returns the ready Service."""
    await database.init()
    async with database.acquire() as conn:
        await migrate(conn)
    store = RulesetStore(database, refresh_s=config.BINDING_REFRESH_S, read_budget_s=config.DB_READ_BUDGET_S)
    seed = await seed_and_load(store, database, rulesets_dir)
    logger.info("startup: %d ruleset version(s) loaded (%s); bindings: %s",
                len(seed.compiled), ", ".join(f"{r.id}@{r.version} {r.mode}" for r in seed.compiled),
                ", ".join(f"{b.scope_model}{'/' + b.scope_key_alias if b.scope_key_alias else ''}"
                          f" -> {b.ruleset_id}@{b.version}"
                          + (f" (shadow: {', '.join(f'{rid}@{v}' for rid, v in b.shadows)})" if b.shadows else "")
                          for b in store.bindings) or "none")
    queue = build_analysis_queue(database, analysis_client)
    if queue is not None:
        queue.start()
    return Service(store=store, database=database, rulesets_dir=rulesets_dir, analysis_queue=queue)


def create_app(*, service: Optional[Service] = None, database: Optional[Database] = None,
               rulesets_dir: Optional[pathlib.Path] = None, instrument: bool = True,
               analysis_client: Optional[ClassifierClient] = None) -> FastAPI:
    """Build the app.

    Args:
        service:         a ready Service -- the lifespan then does nothing
                         but drain it at shutdown.
        database:        the database to start against (default: config.DB_DSN).
        rulesets_dir:    the rulesets to seed (default: the baked-in directory).
        instrument:      mount prometheus-fastapi-instrumentator's HTTP metrics + /metrics.
        analysis_client: the ClassifierClient the analysis queue uses
                         (default: one for ROUTE_POLICY_CLASSIFIER_URL).
    """
    directory = rulesets_dir or config.RULESETS_DIR

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if service is not None:
            app.state.service = service
            try:
                yield
            finally:
                await service.drain()
            return
        db = database or Database(config.DB_DSN, max_size=config.DB_POOL_MAX)
        try:
            app.state.service = await start_service(db, directory, analysis_client)
        except BaseException:
            await db.close()
            raise
        logger.info("lifespan: startup complete")
        try:
            yield
        finally:
            try:
                await app.state.service.drain()
            finally:
                await db.close()
            logger.info("lifespan: shutdown complete")

    app = FastAPI(
        title="Route policy",
        description="Rulesets of evaluators pick the LiteLLM chain alias for each `auto` turn; "
                    "every decision is logged to route-policy-db. See ai/route-policy/ROUTE_POLICY.md.",
        lifespan=lifespan,
    )
    if instrument:
        Instrumentator(
            should_group_status_codes=True,
            excluded_handlers=["/metrics", "/health", "/ready"],
        ).instrument(app).expose(app)
    for module in (decide, observe, evaluate, rulesets, chats, evaluator_types, health):
        app.include_router(module.router)
    return app


configure_logging()
app = create_app()
