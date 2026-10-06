"""Staff / demo API — the internal portal's routes (inbox, CRM cases, customer table,
queue simulator, model status).

Imported by backend/main.py ONLY when ENABLE_STAFF_API=true, so a public chatbot
process never loads the inbox, case or customer modules (account separation layer,
tests/account_gap_test.py). These routes expose staff data and must never be
reachable on a public deployment (S-C1).
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException

from . import chat_notifier, crm_store, email_render, llm, lookup, queue_manager
from .config import settings
from .schemas import CaseStatusUpdate, LookupRequest


# ===========================================================================
# STAFF / DEMO API — only registered when ENABLE_STAFF_API=true (S-C1).
# These expose PII (inbox, CRM cases, customer table) and admin actions (queue
# simulator, case status). They must never be reachable on a public deployment.
# ===========================================================================
def build_router(require_staff, rate_limit, queue_state) -> APIRouter:
    staff = APIRouter(dependencies=[Depends(require_staff)])


    @staff.get("/api/queue/status")
    def queue_status_legacy(session_id: str):
        """Legacy GET poll used by the internal demo portal (read-only, same as POST)."""
        if not re.match(r"^[a-zA-Z0-9_-]{1,64}$", session_id):
            raise HTTPException(status_code=422, detail="Invalid session_id format")
        s = queue_state(session_id)
        # Shape kept for the demo portal (frontend/app.js).
        return {"session_id": session_id, "allowed": s["state"] == "active", "status": s["state"],
                "position": s["queue_position"], "queued": s["state"] == "queued"}


    @staff.get("/api/queue/stats")
    def queue_stats():
        """Live concurrency metrics (active users, max capacity, queue depth)."""
        return queue_manager.get_stats()


    @staff.post("/api/queue/simulate")
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
    @staff.get("/api/crm/cases")
    def list_crm_cases():
        """List all CRM cases opened for disposition."""
        return crm_store.get_all_cases()


    @staff.get("/api/crm/cases/{case_id}")
    def get_crm_case(case_id: str):
        """Retrieve complete CRM case record with Chatter note and visual evidence."""
        c = crm_store.get_case(case_id)
        if c is None:
            raise HTTPException(status_code=404, detail="CRM case not found")
        return c


    @staff.post("/api/crm/cases/{case_id}/status")
    def update_crm_case_status(case_id: str, payload: CaseStatusUpdate):
        """Update workflow status of a CRM case (status is an allow-listed enum — S-L5)."""
        c = crm_store.update_status(case_id, payload.status)
        if c is None:
            raise HTTPException(status_code=404, detail="CRM case not found")
        return c


    # ---------------------------------------------------------------------------
    # Inbox (staff-facing generated emails)
    # ---------------------------------------------------------------------------
    @staff.get("/api/emails")
    def list_emails():
        return email_render.list_emails()


    @staff.get("/api/emails/{email_id}")
    def get_email(email_id: str):
        rec = email_render.get_email(email_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="Email not found")
        return rec


    @staff.get("/api/chat-notifications")
    def chat_notifications():
        """Google Chat notifications posted to team spaces (in-app feed by default)."""
        return chat_notifier.list_notifications()


    # ---------------------------------------------------------------------------
    # Database tab: customers + lookup demo
    # ---------------------------------------------------------------------------
    @staff.get("/api/customers")
    def customers():
        return lookup.list_customers()


    @staff.post("/api/lookup")
    def lookup_demo(req: LookupRequest, _: None = Depends(rate_limit)):
        return lookup.lookup_demo(req.name, req.address, req.email)


    @staff.get("/api/llm/status")
    def llm_status():
        """Detailed model backend status (host, model, mock) — internal only (S-L1)."""
        return llm.status()

    return staff
