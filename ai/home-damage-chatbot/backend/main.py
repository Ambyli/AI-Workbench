"""FastAPI app: chat orchestration API + static frontend serving.

Run (Windows dev, no Ollama needed):
    USE_MOCK_LLM=1 uvicorn backend.main:app --reload
Run (Mac mini, real model):
    ollama serve  &&  uvicorn backend.main:app
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

import re

from . import chat_notifier, crm_store, email_render, llm, lookup, queue_manager, ratelimit, seed, state_machine
from .config import settings
from .safety import SAFETY_MESSAGE, is_safety_concern
from .schemas import ChatRequest, LookupRequest
from .upload import validate_and_save_upload

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("chatbot")

app = FastAPI(title="Zeo Energy Service Chatbot (prototype)")

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


# --- Generic error handler: never leak stack traces to clients. ---
@app.exception_handler(Exception)
async def _unhandled_exception(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error."})


def _rate_limit(request: Request) -> None:
    ratelimit.check(request)


@app.on_event("startup")
def _on_startup() -> None:
    logger.info("LLM status: %s", llm.status())
    logger.info(
        "Config: CRM_BACKEND=%s EMAIL_SEND_ENABLED=%s CORS=%s",
        settings.CRM_BACKEND, settings.EMAIL_SEND_ENABLED, settings.CORS_ALLOW_ORIGINS,
    )
    seed.seed_if_empty()


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------
@app.post("/api/chat")
def chat(req: ChatRequest, _: None = Depends(_rate_limit)):
    # 1) Deterministic, always-on safety check on real user input.
    if req.message and is_safety_concern(req.message):
        queue_manager.release_active(req.session_id)
        return {
            "session_id": req.session_id,
            "messages": [{"text": SAFETY_MESSAGE, "kind": "safety"}],
            "quick_replies": [],
            "allow_upload": False,
            "state": "safety",
            "done": False,
            "email_id": None,
            "queued": False,
            "queue_position": 0,
        }

    # 2) Queue check: verify active user capacity before starting/processing turn.
    allowed, position, status_code = queue_manager.acquire_or_enqueue(req.session_id)
    if not allowed:
        if status_code == "full":
            return {
                "session_id": req.session_id,
                "messages": [
                    {
                        "text": "We are currently experiencing extraordinarily high service request volume and the queue is full. Please try again in a few minutes or call our Customer Care team directly at 727-375-9375.",
                        "kind": "system",
                    }
                ],
                "quick_replies": [],
                "allow_upload": False,
                "state": "queue_full",
                "done": True,
                "queued": False,
                "queue_position": -1,
            }
        # status_code == "queued"
        return {
            "session_id": req.session_id,
            "messages": [
                {
                    "text": f"You are in line to chat with our service assistant (Position #{position}). Please hold on while an active assistant slot opens up...",
                    "kind": "system",
                }
            ],
            "quick_replies": [],
            "allow_upload": False,
            "state": "queued",
            "done": False,
            "queued": True,
            "queue_position": position,
        }

    # 3) Empty message = (re)start the session and return the greeting.
    if not req.message and not req.attachments:
        res = state_machine.start(req.session_id, req.mode)
        if isinstance(res, dict):
            res["queued"] = False
            res["queue_position"] = 0
            if res.get("done"):
                queue_manager.release_active(req.session_id)
        return res

    # 4) Normal turn.
    res = state_machine.handle(req.session_id, req.mode, req.message, req.attachments)
    if isinstance(res, dict):
        res["queued"] = False
        res["queue_position"] = 0
        if res.get("done"):
            queue_manager.release_active(req.session_id)
    return res


# ---------------------------------------------------------------------------
# Queueing & Concurrency Endpoints
# ---------------------------------------------------------------------------
@app.get("/api/queue/status")
def queue_status(session_id: str, _: None = Depends(_rate_limit)):
    """Check current queue or active status for a given session."""
    if not re.match(r"^[a-zA-Z0-9_-]{1,64}$", session_id):
        raise HTTPException(status_code=422, detail="Invalid session_id format")
    allowed, position, status_code = queue_manager.acquire_or_enqueue(session_id)
    return {
        "session_id": session_id,
        "allowed": allowed,
        "status": status_code,
        "position": position,
        "queued": not allowed and status_code == "queued",
    }


@app.get("/api/queue/stats")
def queue_stats():
    """Live concurrency metrics (active users, max capacity, queue depth)."""
    return queue_manager.get_stats()


@app.post("/api/queue/simulate")
def queue_simulate(req: dict = None):
    """Testing endpoint to simulate queue load, set capacity, or advance slots."""
    req = req or {}
    action = req.get("action", "fill")
    if action == "fill":
        # Fill active slots with simulated users so next user goes to queue
        for i in range(settings.MAX_ACTIVE_USERS):
            queue_manager.acquire_or_enqueue(f"simulated_active_{i}")
        return {"ok": True, "message": f"Filled {settings.MAX_ACTIVE_USERS} active slots.", "stats": queue_manager.get_stats()}
    elif action == "free":
        # Free one simulated active slot to promote next queued user
        stats = queue_manager.get_stats()
        for i in range(settings.MAX_ACTIVE_USERS):
            sid = f"simulated_active_{i}"
            if sid in queue_manager._ACTIVE_USERS:
                queue_manager.release_active(sid)
                break
        return {"ok": True, "message": "Released an active slot.", "stats": queue_manager.get_stats()}
    elif action == "reset":
        queue_manager.reset()
        return {"ok": True, "message": "Queue state reset.", "stats": queue_manager.get_stats()}
    return {"ok": False, "error": f"Unknown action: {action}"}


# ---------------------------------------------------------------------------
# Mock CRM Case Management & Chatter Feed
# ---------------------------------------------------------------------------
@app.get("/api/crm/cases")
def list_crm_cases():
    """List all CRM cases opened for disposition."""
    return crm_store.get_all_cases()


@app.get("/api/crm/cases/{case_id}")
def get_crm_case(case_id: str):
    """Retrieve complete CRM case record with Chatter note and visual evidence."""
    c = crm_store.get_case(case_id)
    if c is None:
        raise HTTPException(status_code=404, detail="CRM case not found")
    return c


@app.post("/api/crm/cases/{case_id}/status")
def update_crm_case_status(case_id: str, payload: dict):
    """Update workflow status of a CRM case."""
    new_status = payload.get("status", "in_review")
    c = crm_store.update_status(case_id, new_status)
    if c is None:
        raise HTTPException(status_code=404, detail="CRM case not found")
    return c


# ---------------------------------------------------------------------------
# Inbox (staff-facing generated emails)
# ---------------------------------------------------------------------------
@app.get("/api/emails")
def list_emails():
    return email_render.list_emails()


@app.get("/api/emails/{email_id}")
def get_email(email_id: str):
    rec = email_render.get_email(email_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="Email not found")
    return rec


@app.get("/api/chat-notifications")
def chat_notifications():
    """Google Chat notifications posted to team spaces (in-app feed by default)."""
    return chat_notifier.list_notifications()


# ---------------------------------------------------------------------------
# Database tab: customers + lookup demo
# ---------------------------------------------------------------------------
@app.get("/api/customers")
def customers():
    return lookup.list_customers()


@app.post("/api/lookup")
def lookup_demo(req: LookupRequest, _: None = Depends(_rate_limit)):
    return lookup.lookup_demo(req.name, req.address, req.email)


@app.get("/api/health")
def health():
    return {"ok": True, "llm": llm.status()}


@app.post("/api/upload")
def upload_file(file: UploadFile = File(...), _: None = Depends(_rate_limit)):
    filename = validate_and_save_upload(file)
    return {"filename": filename}


# ---------------------------------------------------------------------------
# Static frontend (mounted last so /api/* wins)
# ---------------------------------------------------------------------------
app.mount("/uploads", StaticFiles(directory=str(Path(__file__).parent / "data" / "uploads")), name="uploads")
app.mount("/", StaticFiles(directory=str(_FRONTEND_DIR), html=True), name="frontend")
