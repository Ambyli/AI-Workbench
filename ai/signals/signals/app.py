"""Signals sidecar -- FastAPI application.

``create_app()`` builds the app; ``app`` (module level) is what uvicorn runs
(``uvicorn signals.app:app``, Dockerfile).

**Startup** (the lifespan): the model lanes' worker threads start, and the
models load in a background thread -- every lane built from /models and
warmed with one inference (runtime.load_models). The app serves while they
load: GET /health is 503 until every model is ready, and a /signals task
whose model is not ready yet answers ``unavailable``. A swap deploy waits
for the healthcheck, so a copy takes traffic from the edge only once it is
warm (Docker DNS may still hand it a request earlier; that request gets
``unavailable`` tasks and route-policy's evaluators abstain).

**Shutdown** stops the lanes (each worker finishes its current step --
one ONNX run -- and exits; a PII job still finishing a history after its
response is dropped with the in-memory cache, which is all there is).

A test passes its own ``specs`` and ``loader`` (fake models), a ``clock``,
and ``instrument=False``.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
from contextlib import asynccontextmanager
from typing import Mapping, Optional

from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator

from . import api, config
from .cache import ExampleSets, FeatureCache
from .runtime import Loader, Models, ModelSpec, load_models, load_specs, model_loader
from .scheduler import CLOCK, Clock, Engine

logger = logging.getLogger("signals")


def configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("signals").setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))


def create_app(*, specs: Optional[Mapping[str, ModelSpec]] = None, loader: Optional[Loader] = None,
               clock: Clock = CLOCK, instrument: bool = True) -> FastAPI:
    """Build the app.

    Args:
        specs:      the model specs (default: models.yaml).
        loader:     ``loader(lane, spec) -> model`` (default: the real ONNX /
                    fastText loader over SIGNALS_MODELS_DIR).
        clock:      the monotonic clock deadlines are measured with.
        instrument: mount prometheus-fastapi-instrumentator's HTTP metrics + /metrics.
    """
    models = Models(specs if specs is not None else load_specs())
    engine = Engine(models, FeatureCache(config.CACHE_ENTRIES), ExampleSets(config.EXAMPLE_SETS_MAX), clock=clock)
    load = loader or model_loader()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # The deadline timer must win the GIL from the lane threads promptly
        # (config.GIL_SWITCH_INTERVAL_S).
        sys.setswitchinterval(config.GIL_SWITCH_INTERVAL_S)
        engine.start()
        threading.Thread(target=load_models, args=(models, load), name="signals-load", daemon=True).start()
        logger.info("startup: loading %s (threads %s, cache %d entries)",
                    ", ".join(f"{lane}={spec.model_id}" for lane, spec in models.specs.items()),
                    "/".join(f"{lane}:{n}" for lane, n in config.THREADS.items()), config.CACHE_ENTRIES)
        try:
            yield
        finally:
            await asyncio.to_thread(engine.stop)
            logger.info("lifespan: shutdown complete")

    app = FastAPI(
        title="Signals",
        description="Fast CPU signals (PII NER, nearest examples, prompt injection, language) for "
                    "route-policy's evaluators. See ai/signals/SIGNALS.md.",
        lifespan=lifespan,
    )
    app.state.engine = engine
    app.state.models = models
    app.state.clock = clock
    api.install(app)
    if instrument:
        Instrumentator(should_group_status_codes=True, excluded_handlers=["/metrics", "/health"]).instrument(
            app).expose(app)
    return app


configure_logging()
app = create_app()
