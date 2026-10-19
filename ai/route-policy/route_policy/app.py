"""Route-policy service -- FastAPI application.

``create_app()`` builds the app; ``app`` (module level) is what uvicorn runs
(``uvicorn route_policy.app:app``, Dockerfile).

**Startup** (the lifespan), in order -- any failure stops the container with
the reason in its log, which is what a swap deploy wants (the old copy keeps
serving):

1. open the Postgres pool on route-policy-db (``db.pool.Database``);
2. apply pending migrations (``db.migrations.migrate``);
3. read and validate the baked-in rulesets and seed them, idempotently, with
   their bindings (``rulesets.store.seed_and_load``) -- a changed ruleset
   that kept its version number fails here;
4. hang the ``Service`` (ruleset cache + database) on ``app.state``.

Shutdown closes the pool. There are no background workers in phase 2 (the
llm_classifier's analysis queue is phase 4).

A test can pass its own ``service`` -- e.g. a ruleset cache filled from the
files with no database -- and the lifespan then touches nothing.
"""

from __future__ import annotations

import logging
import pathlib
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator

from . import config
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


async def start_service(database: Database, rulesets_dir: pathlib.Path) -> Service:
    """Steps 1-3 of startup; returns the ready Service."""
    await database.init()
    async with database.acquire() as conn:
        await migrate(conn)
    store = RulesetStore(database, refresh_s=config.BINDING_REFRESH_S)
    seed = await seed_and_load(store, database, rulesets_dir)
    logger.info("startup: %d ruleset version(s) loaded (%s); bindings: %s",
                len(seed.compiled), ", ".join(f"{r.id}@{r.version} {r.mode}" for r in seed.compiled),
                ", ".join(f"{b.scope_model}{'/' + b.scope_key_alias if b.scope_key_alias else ''}"
                          f" -> {b.ruleset_id}@{b.version}" for b in store.bindings) or "none")
    return Service(store=store, database=database, rulesets_dir=rulesets_dir)


def create_app(*, service: Optional[Service] = None, database: Optional[Database] = None,
               rulesets_dir: Optional[pathlib.Path] = None, instrument: bool = True) -> FastAPI:
    """Build the app.

    Args:
        service:      a ready Service -- the lifespan then does nothing.
        database:     the database to start against (default: config.DB_DSN).
        rulesets_dir: the rulesets to seed (default: the baked-in directory).
        instrument:   mount prometheus-fastapi-instrumentator's HTTP metrics + /metrics.
    """
    directory = rulesets_dir or config.RULESETS_DIR

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if service is not None:
            app.state.service = service
            yield
            return
        db = database or Database(config.DB_DSN, max_size=config.DB_POOL_MAX)
        try:
            app.state.service = await start_service(db, directory)
        except BaseException:
            await db.close()
            raise
        logger.info("lifespan: startup complete")
        try:
            yield
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
