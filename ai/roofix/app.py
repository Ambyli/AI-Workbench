"""
Roofix Bridge — FastAPI + APScheduler entry point.

Endpoints:
    GET  /health        healthcheck (for Docker)
    GET  /status        last-tick summary, decision counts, error counts
    POST /tick          manual batch trigger; body accepts optional {raw_emails: [...]}
                        for offline / crafted-event testing

Scheduler runs a batch every TICK_INTERVAL_SECONDS. Gmail (via direct Google
API) is polled for unread Roofix mail; each email is parsed, the brain decides,
and Phoenix (via direct psycopg2) is written to (or DRY_RUN-logged). Everything
runs single-threaded inside the FastAPI event loop's thread pool — batches are
serialized to avoid double-processing an event mid-flight.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from pydantic import BaseModel

from common.env import load_env
from common.logging_setup import CsvLogger, setup_logging
from common.processed_store import ProcessedStore

load_env()

from components.gmail_client import GmailClient
from components.orchestrator import LOG_COLUMNS, process_batch, run as orchestrator_run
from components.phoenix_client import PhoenixClient
from components.roofix_scraper_client import RoofixScraperClient

TICK_INTERVAL_SECONDS = int(os.getenv("TICK_INTERVAL_SECONDS", "300"))
FIELD_MAPPING_PATH = os.getenv("FIELD_MAPPING_PATH", "/app/config/field_mapping.json")
LOG_DIR = os.getenv("LOG_DIR", "/data")
DEBUG_LOGGING = os.getenv("DEBUG_LOGGING", "false").lower() == "true"

# Stdlib logging is what httpx / openai / apscheduler noise flows through,
# plus the compact one-line echo CsvLogger emits per audit row.
setup_logging("roofix", log_dir=LOG_DIR, debug=DEBUG_LOGGING)
_stdlib_logger = logging.getLogger("roofix")

# Single CSV audit logger reused across ticks. Schema declared by orchestrator
# (it's the one writing to it); we pass the same list back in so app + module
# stay in agreement.
_audit_log = CsvLogger(
    path=Path(LOG_DIR) / "agent_log.csv",
    columns=LOG_COLUMNS,
    logger=_stdlib_logger,
)

# Threading lock still guards _STATE because /status is a sync handler that
# may read while _record_tick (called from the async batch path) writes.
# Coroutine contention on _STATE is impossible in a single event loop, but
# FastAPI can run sync handlers in a threadpool — the lock covers that case.
_STATE_LOCK = threading.Lock()

# Ensures scheduled tick and manual /tick calls never overlap. Belt: the
# scheduler job has max_instances=1. Suspenders: this lock catches the case
# where a manual /tick lands mid-schedule. Both entry points acquire it.
# Lazy-constructed on first use (asyncio.Lock() binds to the running loop).
_tick_lock: Optional[asyncio.Lock] = None


def _get_tick_lock() -> asyncio.Lock:
    global _tick_lock
    if _tick_lock is None:
        _tick_lock = asyncio.Lock()
    return _tick_lock


_STATE: dict = {
    "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    "last_tick_at": None,
    "last_tick_ok": None,
    "last_tick_error": None,
    "tick_count": 0,
    "decisions_total": 0,
    "decisions_by_action": Counter(),
    "decisions_by_source": Counter(),
    "escalations_total": 0,
    "phoenix_write_failures": 0,
}


def _load_milestone_map() -> dict:
    """Read Michael's Roofix-event -> Phoenix (block_name, status_id) mapping.

    The file lives on disk (mounted config) so it can be updated without a
    container rebuild. Returns {} if the file is missing / malformed — brain
    will log a "no milestone mapping for ..." warning and skip.
    """
    try:
        with open(FIELD_MAPPING_PATH, "r") as f:
            data = json.load(f)
        return data.get("milestones", {}) or {}
    except Exception:
        return {}


async def _run_one_batch(raw_emails: Optional[list] = None) -> dict:
    """Run one processing batch. If raw_emails is None, pull from Gmail.

    Serialized by ``_tick_lock`` — a second call arriving while a batch is
    in flight will await the current one, guaranteeing no overlap between
    the scheduler tick and manual ``/tick`` requests.

    Sync client context managers (PhoenixClient, GmailClient, ProcessedStore)
    remain sync-with — they open cheap resources (a psycopg2 connection, a
    Gmail service builder, a sqlite handle). ``RoofixScraperClient`` is
    async-with (owns an ``httpx.AsyncClient``).
    """
    async with _get_tick_lock():
        milestone_map = _load_milestone_map()

        with (
            PhoenixClient() as phoenix,
            GmailClient() as gmail,
            ProcessedStore(Path(LOG_DIR) / "processed.db") as processed_store,
        ):
            async with RoofixScraperClient() as scraper_client:
                if raw_emails is None:
                    raw_emails = await asyncio.to_thread(gmail.fetch)
                    _audit_log.log("listener", "fetch", True,
                                   f"{len(raw_emails)} email(s)")

                # Filter out already-processed emails. is_processed hits sqlite;
                # cheap, but wrap in to_thread so many misses don't block the loop.
                unprocessed = [
                    e for e in raw_emails
                    if not processed_store.is_processed(e.get("message_id"))
                ]
                _audit_log.log(
                    "listener", "filtered", True,
                    f"{len(unprocessed)} email(s) after filtering processed",
                    event_type="", project_ref="",
                )

                decisions = await orchestrator_run(
                    listener=lambda: unprocessed,
                    phoenix=phoenix,
                    log=_audit_log,
                    milestone_map=milestone_map,
                    scraper_client=scraper_client,
                    processed_store=processed_store,
                    gmail=gmail,
                )

                # Mark successfully processed emails as read. The orchestrator
                # stamps each Decision with the source email's Gmail message id,
                # so we can look up the target directly — no fragile subject
                # matching.
                #
                # Skip rules (leave unread):
                #   - escalate whose forward FAILED (or no ESCALATION_RECIPIENTS
                #     set) → operator needs to see it in the Roofix inbox.
                #     The orchestrator stamps ``_forwarded`` on the decision.
                #   - AI ignore → the model made a fuzzy call; leave unread so
                #     the operator can review. Rule-based ignores are
                #     deterministic and safe to silence.
                # The orchestrator has already marked ALL of these ok in
                # processed_store, so nothing gets re-processed next tick —
                # this gate only controls Gmail visibility.
                processed_ids = set()
                for d in decisions:
                    action = d.get("action")
                    if action == "escalate" and not d.get("_forwarded"):
                        continue
                    if action == "ignore" and d.get("source") != "rule":
                        continue
                    mid = d.get("message_id")
                    if mid:
                        await asyncio.to_thread(gmail.mark_read, mid)
                        processed_ids.add(mid)

        _record_tick(decisions, error=None)
        return {"decisions": decisions, "count": len(decisions)}


def _record_tick(decisions: list, error: Optional[str]) -> None:
    with _STATE_LOCK:
        _STATE["last_tick_at"] = datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        )
        _STATE["last_tick_ok"] = error is None
        _STATE["last_tick_error"] = error
        _STATE["tick_count"] += 1
        _STATE["decisions_total"] += len(decisions)
        for d in decisions:
            _STATE["decisions_by_action"][d.get("action", "")] += 1
            _STATE["decisions_by_source"][d.get("source", "")] += 1
            if d.get("needs_human") or d.get("action") == "escalate":
                _STATE["escalations_total"] += 1


async def _scheduled_tick() -> None:
    """APScheduler job wrapper.

    ``AsyncIOScheduler`` runs coroutine jobs directly on the FastAPI event
    loop, so this is just a thin exception boundary around ``_run_one_batch``.
    """
    try:
        await _run_one_batch()
    except Exception as e:
        _record_tick([], error=repr(e))


scheduler = AsyncIOScheduler(timezone="UTC")
scheduler.add_job(
    _scheduled_tick,
    "interval",
    seconds=TICK_INTERVAL_SECONDS,
    id="roofix_tick",
    max_instances=1,  # belt: scheduler-level protection against overlap
    coalesce=True,
)

app = FastAPI(title="Roofix Bridge")


@app.on_event("startup")
def _startup() -> None:
    scheduler.start()


@app.on_event("shutdown")
def _shutdown() -> None:
    scheduler.shutdown(wait=False)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/status")
def status() -> dict:
    with _STATE_LOCK:
        snap = dict(_STATE)
        snap["decisions_by_action"] = dict(_STATE["decisions_by_action"])
        snap["decisions_by_source"] = dict(_STATE["decisions_by_source"])
        snap["dry_run"] = os.getenv("DRY_RUN", "true").lower() == "true"
        snap["agent_phase"] = os.getenv("AGENT_PHASE", "0")
        snap["tick_interval_seconds"] = TICK_INTERVAL_SECONDS
        return snap


class TickRequest(BaseModel):
    raw_emails: Optional[list] = None


@app.post("/tick")
async def tick(req: Optional[TickRequest] = None) -> dict:
    raw = req.raw_emails if req else None
    try:
        return await _run_one_batch(raw_emails=raw)
    except Exception as e:
        _record_tick([], error=repr(e))
        return {"error": repr(e), "decisions": [], "count": 0}


if __name__ == "__main__":
    import signal
    import sys
    import time
    import uvicorn

    def _shutdown(signum, frame):
        """Handle Ctrl+C / SIGTERM: stop scheduler, wait for children, exit clean."""
        _stdlib_logger.info(
            "Shutdown signal received (%s). Stopping scheduler...", signum
        )
        scheduler.shutdown(wait=True)
        _stdlib_logger.info("Scheduler stopped. Waiting for child processes...")

        # Give child processes a moment to terminate gracefully
        for _ in range(10):  # Wait up to 10 seconds
            import os

            try:
                # Try to terminate all child processes (Windows)
                os.system("taskkill /F /T /PID " + str(os.getpid()))
            except Exception:
                pass
            time.sleep(1)

        _stdlib_logger.info("Exiting.")
        sys.exit(128 + signum)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    uvicorn.run(app, host="0.0.0.0", port=8010)
# tick()
