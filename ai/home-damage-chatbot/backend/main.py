"""FastAPI app: chat orchestration API + static frontend serving.

Run (Windows dev, no Ollama needed):
    USE_MOCK_LLM=1 uvicorn backend.main:app --reload
Run (Mac mini, real model):
    ollama serve  &&  uvicorn backend.main:app
"""
from __future__ import annotations

import logging
from pathlib import Path

import hmac

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

import re

from . import account_gateway, llm, queue_manager, ratelimit, retention, sessions, state_machine
from .config import settings
from .safety import SAFETY_MESSAGE, is_safety_concern
from .schemas import ChatRequest, QueueStatusRequest
from .upload import validate_and_save_upload

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("chatbot")

# The PUBLIC surface is exactly these routes (docs/API_CONTRACT.md). Anything else —
# the staff/demo portal, inbox, CRM, customer table, queue simulator, interactive API
# docs, and the /uploads mount — only exists when ENABLE_STAFF_API=true.
PUBLIC_ROUTES = {
    ("POST", "/api/chat"),
    ("POST", "/api/queue/status"),
    ("GET", "/api/health"),
    ("POST", "/api/upload"),
}

_STAFF = settings.ENABLE_STAFF_API

app = FastAPI(
    title="Zeo Energy Service Chatbot",
    docs_url="/docs" if _STAFF else None,
    redoc_url="/redoc" if _STAFF else None,
    openapi_url="/openapi.json" if _STAFF else None,
)

_FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

# --- CORS: locked to a configured allow-list (never "*"). ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ALLOW_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


# --- Security headers on every response. ---
@app.middleware("http")
async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    # Conservative CSP: app is same-origin vanilla JS/CSS with inline styles.
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data: https://server.arcgisonline.com; style-src 'self' 'unsafe-inline'; "
        "object-src 'none'; base-uri 'self'; frame-ancestors 'none'",
    )
    return response


# --- Model required but unavailable -> 503 (contract v2: client retries, then offers the form). ---
@app.exception_handler(llm.LLMUnavailable)
async def _llm_unavailable(request: Request, exc: llm.LLMUnavailable):
    return JSONResponse(status_code=503, content={"detail": "assistant_unavailable"})


# --- Generic error handler: never leak stack traces to clients. ---
@app.exception_handler(Exception)
async def _unhandled_exception(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error."})


def _rate_limit(request: Request) -> None:
    ratelimit.check(request)


def _require_staff(x_staff_key: str = Header(default="")) -> None:
    """Staff routes only exist when ENABLE_STAFF_API=true; if STAFF_API_KEY is set they
    additionally require a matching X-Staff-Key header (constant-time compare)."""
    key = settings.STAFF_API_KEY
    if key and not hmac.compare_digest(x_staff_key, key):
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.on_event("startup")
def _on_startup() -> None:
    logger.info("LLM status: %s", llm.status())
    logger.info(
        "Config: CRM_BACKEND=%s EMAIL_SEND_ENABLED=%s STAFF_API=%s LLM_REQUIRED=%s CORS=%s",
        settings.CRM_BACKEND, settings.EMAIL_SEND_ENABLED, _STAFF, settings.LLM_REQUIRED,
        settings.CORS_ALLOW_ORIGINS,
    )
    if settings.REQUIRE_ACCOUNT_GAP:
        problems = account_gateway.gap_violations()
        if problems:
            # Refuse to serve customers if the chatbot could reach CRM or delivery secrets.
            raise RuntimeError("Account gap not satisfied: " + "; ".join(problems))
        logger.info("Account gap enforced: CRM and delivery live in the account service.")
    if _STAFF:
        logger.warning("ENABLE_STAFF_API=true — staff/demo routes are exposed. Never enable publicly.")
        from . import seed  # noqa: PLC0415 - staff/demo only
        # Demo sample data only in staff/demo mode — never seed fake cases into a real inbox.
        seed.seed_if_empty()
    # In the gapped deployment the chatbot only holds uploads and the outbox; the
    # account service purges the staff records it owns.
    gapped = settings.ACCOUNT_GATEWAY == "service"
    retention.start_background_purge(scope="chatbot" if gapped else "all")
    if gapped:
        account_gateway.flush_outbox()
        account_gateway.start_outbox_retry()
    _start_abandon_sweep()


def _start_abandon_sweep(interval: float = 60.0) -> None:
    """Alert departments about requests customers stopped answering (once each)."""
    import threading  # noqa: PLC0415
    import time as _time  # noqa: PLC0415

    def loop() -> None:
        while True:
            _time.sleep(interval)
            try:
                state_machine.sweep_abandoned()
            except Exception as exc:  # noqa: BLE001 - never kill the thread
                logger.warning("Abandoned-request sweep failed (%s).", type(exc).__name__)

    threading.Thread(target=loop, name="abandon-sweep", daemon=True).start()


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------
_UPLOAD_NAME_RE = re.compile(r"^[0-9a-f]{32}\.(jpg|png|gif|webp)$")
_QUEUE_FULL_TEXT = ("We are currently experiencing extraordinarily high service request volume and the "
                    "queue is full. Please try again in a few minutes, or call 727-349-4057 for solar "
                    "production, microinverter, or Enphase issues, or 727-382-0075 for roofing, electrical, "
                    "battery, or other home damage.")


def _envelope(sid, messages, *, state, done=False, queued=False, position=0, outcome=None) -> dict:
    """Contract v2 response shape for turns that don't come from the state machine."""
    return {
        "session_id": sid, "messages": messages, "quick_replies": [], "allow_upload": False,
        "await_step": None, "state": state, "done": done, "queued": queued,
        "queue_position": position, "location": None, "summary": None,
        "outcome": (outcome or "ended") if done else None,
    }


def _expired() -> HTTPException:
    return HTTPException(status_code=404, detail="session_expired")


@app.post("/api/chat")
def chat(req: ChatRequest, request: Request, _: None = Depends(_rate_limit)):
    # 1) Deterministic, always-on 911 check. Life-safety guidance is NEVER blocked by
    #    session, queue, or model state.
    if req.message and is_safety_concern(req.message):
        sid = req.session_id if sessions.touch(req.session_id) else None
        if sid:
            queue_manager.release_active(sid)
            state_machine.note_safety(sid)  # staff see it on any later alert or request
        return _envelope(sid, [{"text": SAFETY_MESSAGE, "kind": "safety"}], state="safety")

    # 2) Sessions are server-issued (S-M2): issue on start, otherwise must be ours.
    if req.session_id is None:
        try:
            sid = sessions.issue(ratelimit.client_key(request))
        except sessions.TooManySessions:
            raise HTTPException(status_code=429, detail="too_many_sessions")
        is_start = True
    else:
        sid = req.session_id
        if not sessions.touch(sid):
            raise _expired()
        is_start = False

    # 3) Files may only be referenced by the session that uploaded them (S-H3):
    #    explicit attachments, and the pointer image sent as the damage_pointer answer.
    for fname in req.attachments:
        if not sessions.owns_upload(sid, fname):
            raise HTTPException(status_code=422, detail="unknown_attachment")
    msg = req.message.strip()
    if _UPLOAD_NAME_RE.match(msg) and not sessions.owns_upload(sid, msg):
        raise HTTPException(status_code=422, detail="unknown_attachment")

    # 4) Capacity: POST /api/chat is the ONLY way to take an active slot.
    allowed, position, status_code = queue_manager.acquire_or_enqueue(sid)
    if not allowed:
        if status_code == "full":
            sessions.end(sid)  # nothing to resume; frees the client's session quota
            return _envelope(None, [{"text": _QUEUE_FULL_TEXT, "kind": "system"}],
                             state="queue_full", done=True, position=-1)
        place = "You're first in line" if position <= 1 else f"You're number {position} in line"
        return _envelope(sid, [{
            "text": (f"{place} to chat with our service assistant. We'll connect you automatically. "
                     "Don't want to wait? Call 727-349-4057 for solar production, microinverter, or "
                     "Enphase issues, or 727-382-0075 for roofing, electrical, battery, or other home damage."),
            "kind": "system"}], state="queued", queued=True, position=position)

    # 5) Start (greeting) or a normal turn.
    if is_start or (not req.message and not req.attachments):
        res = state_machine.start(sid, req.mode)
    else:
        pointer = (req.pointer.lat, req.pointer.lng) if req.pointer else None
        res = state_machine.handle(sid, req.mode, req.message, req.attachments, pointer)

    res["queued"] = False
    res["queue_position"] = 0
    if res.get("done"):
        # Submitted (or handed to a human): free the slot and drop the in-memory PII.
        queue_manager.release_active(sid)
        sessions.end(sid)
        state_machine.forget(sid)
    if not _STAFF:
        res.pop("email_id", None)  # internal inbox id; only meaningful to the staff API
    return res


# ---------------------------------------------------------------------------
# Queueing & Concurrency Endpoints
# ---------------------------------------------------------------------------
def _queue_state(session_id: str) -> dict:
    # READ-ONLY by design (S-H1): check_status never grants a slot or creates a queue
    # entry — it only refreshes the heartbeat of a session that is already waiting.
    # Slots are acquired exclusively through POST /api/chat.
    _allowed, position, status_code = queue_manager.check_status(session_id)
    state = {"active": "active", "queued": "queued"}.get(status_code, "untracked")
    return {"state": state, "queue_position": position if state == "queued" else 0}


@app.post("/api/queue/status")
def queue_status(req: QueueStatusRequest, _: None = Depends(_rate_limit)):
    """Poll queue position for a waiting session (contract v2)."""
    if not sessions.touch(req.session_id):
        raise _expired()
    return _queue_state(req.session_id)


# ===========================================================================
# PUBLIC: health + upload
# ===========================================================================
@app.get("/api/health")
def health():
    """Public health (contract v2). Deliberately minimal: no host/model details.

    Uses the cached readiness probe, so polling this never hammers the model server.
    When LLM_REQUIRED=true and the model is unreachable, report 503 so the page
    routes customers to the classic form instead of a degraded assistant.
    """
    available = llm.assistant_available()
    if not available:
        return JSONResponse(status_code=503, content={"ok": False, "assistant_available": False})
    return {"ok": True, "assistant_available": True}


@app.post("/api/upload")
def upload_file(file: UploadFile = File(...), session_id: str = Form(""),
                _: None = Depends(_rate_limit)):
    """Session-bound image upload (contract v2, S-H3 / S-M3)."""
    if not re.match(r"^[a-zA-Z0-9_-]{1,64}$", session_id or "") or not sessions.touch(session_id):
        raise _expired()
    if sessions.upload_slots_left(session_id) <= 0:  # check before writing anything to disk
        raise HTTPException(status_code=429, detail="upload_limit")
    filename = validate_and_save_upload(file)
    try:
        sessions.register_upload(session_id, filename)
    except (KeyError, sessions.UploadLimitReached):
        raise HTTPException(status_code=429, detail="upload_limit")
    return {"filename": filename}


# ---------------------------------------------------------------------------
# Staff/demo surface: router, uploads mount, and the internal portal UI.
# Mounted last so /api/* wins. Absent entirely unless ENABLE_STAFF_API=true.
# ---------------------------------------------------------------------------
if _STAFF:
    from . import staff_api  # noqa: PLC0415 - never imported on a public deployment

    app.include_router(staff_api.build_router(_require_staff, _rate_limit, _queue_state))
    app.mount("/uploads", StaticFiles(directory=str(settings.UPLOAD_DIR)), name="uploads")
    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIR), html=True), name="frontend")
