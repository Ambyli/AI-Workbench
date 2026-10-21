"""HTTP: POST /signals, GET /models, GET /health -- and error shapes that never echo text.

``install(app)`` registers the routes and a guard that refuses an oversized
body (413) before it is read. POST /signals reads the body itself and parses
it with the wire's strict parser (schemas.parse): FastAPI's own validation
would answer a bad field with a 422 that quotes the offending value -- a
whole text, when a text is what failed. Here:

* 422 ``{"detail": "<field>: <rule>"}`` -- the ``WireError`` message, which
  never carries a value from the input;
* 413 ``{"detail": "<what> has <n> ...; the cap is <m>"}`` -- ids and sizes only;
* 500 ``{"detail": "internal error (<ExceptionType>)"}`` -- logged the same
  way, no message and no traceback: a tokenizer or onnxruntime exception
  can quote its input.

Every answered /signals request logs ONE line at INFO: the request id (only
when it looks like an id), the number of texts and characters, the deadline,
each task's status and ms, the latency. Never text.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse

from common.pii import ENTITY_TYPES, SENSITIVE_TYPES
from common.signals import (INJECTION_WINDOWS, MAX_EXAMPLE_CHARS, MAX_EXAMPLES, MAX_K, MAX_REQUEST_CHARS,
                            MAX_TEXT_CHARS, MAX_TEXTS, MAX_TOKENS_CAP, STATUSES, TASK_MODELS, WireError)
from common.signals.wire import MAX_DEADLINE_MS

from . import config, metrics, schemas
from .tasks.pii import LABEL_TYPES, LOCATION_LABEL

logger = logging.getLogger("signals")
router = APIRouter()

_SAFE_ID = re.compile(r"[A-Za-z0-9._:-]{1,64}")

# For the OpenAPI page only: the body is parsed by hand (module docstring).
_BODY_DOC = {
    "requestBody": {
        "required": True,
        "content": {"application/json": {"example": {
            "request_id": "6f0e8c1e-8f7e-4c55-9a39-0f3f7ab5d001",
            "deadline_ms": 80,
            "texts": [{"id": "u2", "text": "Send the quote to 77 N 400 E, Provo."}],
            "tasks": [{"task": "pii", "texts": ["u2"]},
                      {"task": "injection", "texts": ["u2"], "max_tokens": 256, "windows": "head_tail"},
                      {"task": "language", "texts": ["u2"]}],
        }}},
    }
}


def _log_id(value: Any) -> str:
    return value if isinstance(value, str) and _SAFE_ID.fullmatch(value) else "-"


def _too_large(length: int) -> JSONResponse:
    metrics.requests_total.labels("rejected").inc()
    return JSONResponse({"detail": f"body of {length} bytes; the cap is {config.MAX_BODY_BYTES}"},
                        status_code=413)


def install(app: FastAPI) -> None:
    @app.middleware("http")
    async def _body_cap(request: Request, call_next):
        # Refuse an oversized body before it is read.
        if request.url.path == "/signals":
            try:
                length = int(request.headers.get("content-length") or 0)
            except ValueError:
                length = 0
            if length > config.MAX_BODY_BYTES:
                return _too_large(length)
        return await call_next(request)

    app.include_router(router)


@router.post("/signals", openapi_extra=_BODY_DOC)
async def post_signals(request: Request) -> JSONResponse:
    """Every task of one request, within ``deadline_ms`` (shapes: schemas.py)."""
    state = request.app.state
    clock = state.clock
    started = clock()
    body = await request.body()
    if len(body) > config.MAX_BODY_BYTES:      # a chunked body sent no Content-Length
        return _too_large(len(body))
    try:
        parsed = schemas.parse(body)
    except WireError as exc:
        metrics.requests_total.labels("rejected").inc()
        return JSONResponse({"detail": str(exc)}, status_code=422)
    refused = parsed.cap_problem()
    if refused is not None:
        metrics.requests_total.labels("rejected").inc()
        logger.info("[signals] request=%s refused (413): %s", _log_id(parsed.request_id), refused)
        return JSONResponse({"detail": refused}, status_code=413)
    texts = schemas.texts_by_id(parsed)
    try:
        results = await state.engine.run(texts, parsed.tasks, parsed.deadline_ms, start=started)
        latency_ms = (clock() - started) * 1000.0
        answer = schemas.answer(parsed, latency_ms, state.models.ids(), results)
    except Exception as exc:  # noqa: BLE001 -- reduced to its type: a message may quote a text
        metrics.requests_total.labels("error").inc()
        logger.error("[signals] request=%s failed: %s", _log_id(parsed.request_id), type(exc).__name__)
        return JSONResponse({"detail": f"internal error ({type(exc).__name__})"}, status_code=500)
    metrics.requests_total.labels("ok").inc()
    metrics.request_seconds.observe(latency_ms / 1000.0)
    logger.info("[signals] request=%s texts=%d chars=%d deadline=%s %s latency=%.1f ms",
                _log_id(parsed.request_id), len(texts), sum(len(t) for t in texts.values()),
                "none" if parsed.deadline_ms is None else f"{parsed.deadline_ms:g} ms",
                " ".join(f"{task}={r.status}" + (f"/{r.ms:.1f}" if r.ms is not None else "")
                         for task, r in results.items()) or "no-tasks", latency_ms)
    return JSONResponse(answer)


@router.get("/models")
async def get_models(request: Request) -> Dict[str, Any]:
    """The pinned models (ids, revisions, sha256, licences, token caps),
    which model serves each task, the PII vocabulary and the request caps.
    No thresholds: those live in the rulesets that read these signals."""
    models = request.app.state.models
    state = models.snapshot()
    return {
        "models": {lane: {**spec.public(), "state": state["state"].get(lane)} for lane, spec in models.specs.items()},
        "tasks": dict(TASK_MODELS),
        "statuses": list(STATUSES),
        "pii": {
            "vocabulary": list(ENTITY_TYPES),
            "sensitive": list(SENSITIVE_TYPES),
            "labels": {**LABEL_TYPES, LOCATION_LABEL: "street_address (with a digit) | location"},
            "other_labels": "other",
        },
        "injection": {"windows": list(INJECTION_WINDOWS)},
        "caps": {
            "max_text_chars": MAX_TEXT_CHARS,
            "max_request_chars": MAX_REQUEST_CHARS,
            "max_texts": MAX_TEXTS,
            "max_examples": MAX_EXAMPLES,
            "max_example_chars": MAX_EXAMPLE_CHARS,
            "max_tokens": MAX_TOKENS_CAP,
            "max_k": MAX_K,
            "max_deadline_ms": MAX_DEADLINE_MS,
            "max_body_bytes": config.MAX_BODY_BYTES,
        },
    }


@router.get("/health")
async def get_health(request: Request) -> JSONResponse:
    """200 once every model is loaded and warmed; 503 before (and for good
    when one failed to load -- the reason is its exception type)."""
    engine = request.app.state.engine
    models = request.app.state.models
    state = models.snapshot()
    ready = models.ready()
    failed = any(s == "failed" for s in state["state"].values())
    body = {
        "status": "ok" if ready else ("failed" if failed else "loading"),
        "models": {lane: {"id": models.model_id(lane), "state": s, "error": state["errors"].get(lane),
                          "load_s": state["load_seconds"].get(lane)} for lane, s in state["state"].items()},
        "cache_entries": len(engine.cache),
        "example_sets": engine.sets.states(),
        "queues": {lane: dict(zip(("foreground", "background"), l.depth())) for lane, l in engine.lanes.items()},
    }
    return JSONResponse(body, status_code=200 if ready else 503)
