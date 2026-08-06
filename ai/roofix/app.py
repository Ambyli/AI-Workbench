"""
Roofix Bridge — FastAPI + APScheduler entry point.

Endpoints:
    GET  /health                       healthcheck (for Docker)
    GET  /status                       last-tick summary, decision counts, error counts
    POST /tick                         manual batch trigger; body accepts optional
                                       {raw_emails: [...]} for offline / crafted-event testing
    POST /execute/{message_id}         re-run one specific Gmail message through the
                                       pipeline, bypassing the processed_store dedup.
                                       Same actions as a normal tick (subject to DRY_RUN).
    POST /reset/{message_id}           flip processed_store row to pending, mark the
                                       email unread in Gmail, remove ROOFIX_PROCESSED_LABEL
                                       so the next tick re-fetches + re-analyzes it.
    POST /labels/backfill              one-time repair: apply ROOFIX_PROCESSED_LABEL
                                       to every message_id currently in processed_store
                                       so the labeled-fetch query excludes them.

Scheduler runs a batch every TICK_INTERVAL_SECONDS. Gmail (via direct Google
API) is polled for unread Roofix mail; each email is parsed, the brain decides,
and Phoenix (via direct psycopg2) is written to (or DRY_RUN-logged). Everything
runs single-threaded inside the FastAPI event loop's thread pool — batches are
serialized to avoid double-processing an event mid-flight.
"""

from __future__ import annotations

import asyncio
import faulthandler
import json
import logging
import os
import sys
import threading

# Install faulthandler at import time so any future native crash (SIGSEGV,
# SIGABRT, SIGBUS from a misbehaving C extension) writes a Python traceback
# to stderr before the process dies. Costs effectively nothing at runtime;
# turns "exit code 139, no logs" into "here's the exact call stack."
faulthandler.enable(file=sys.stderr, all_threads=True)
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from common.env import load_env
from common.logging_setup import CsvLogger, setup_logging
from common.processed_store import PostgresProcessedStore

load_env()

from components.constants import ROOFIX_PROCESSED_LABEL
from components.gmail_client import GmailClient
from components.orchestrator import LOG_COLUMNS, process_batch, run as orchestrator_run
from components.phoenix_client import PhoenixClient
from components.roofix_scraper_client import RoofixScraperClient

TICK_INTERVAL_SECONDS = int(os.getenv("TICK_INTERVAL_SECONDS", "300"))
FIELD_MAPPING_PATH = os.getenv("FIELD_MAPPING_PATH", "/app/config/field_mapping.json")
LOG_DIR = os.getenv("LOG_DIR", "/data")
DEBUG_LOGGING = os.getenv("DEBUG_LOGGING", "false").lower() == "true"

# DSN for the ProcessedStore Postgres (compose-managed roofix-db service).
# Container-side connection — host + port refer to the docker network, not
# the operator's laptop. Every field falls back to the same "roofix" dev
# defaults the compose file uses, so bringing the stack up with an empty
# .env still yields a working connection string on the shared network.
ROOFIX_DB_DSN = (
    f"postgresql://{os.environ.get('ROOFIX_DB_USER', 'roofix')}"
    f":{os.environ.get('ROOFIX_DB_PASSWORD', 'roofix')}"
    f"@{os.environ.get('ROOFIX_DB_HOST', 'roofix-db')}"
    f":{os.environ.get('ROOFIX_DB_PORT', '5432')}"
    f"/{os.environ.get('ROOFIX_DB_NAME', 'roofix')}"
)

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


async def _run_one_batch(
    raw_emails: Optional[list] = None,
    skip_dedup: bool = False,
) -> dict:
    """Run one processing batch. If raw_emails is None, pull from Gmail.

    Serialized by ``_tick_lock`` — a second call arriving while a batch is
    in flight will await the current one, guaranteeing no overlap between
    the scheduler tick and manual ``/tick`` requests.

    ``skip_dedup=True`` bypasses the ``processed_store.is_processed`` filter
    so an already-handled email can be re-run. Used by ``/execute/{id}``;
    normal scheduled ticks leave it False so the dedup guarantee holds.

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
            PostgresProcessedStore(ROOFIX_DB_DSN) as processed_store,
        ):
            async with RoofixScraperClient() as scraper_client:
                if raw_emails is None:
                    raw_emails = await asyncio.to_thread(gmail.fetch)
                    _audit_log.log("listener", "fetch", True,
                                   f"{len(raw_emails)} email(s)")

                # Filter out already-processed emails. is_processed hits sqlite;
                # cheap, but wrap in to_thread so many misses don't block the loop.
                # Skipped entirely for manual /execute runs — the caller has
                # explicitly asked to re-process this id.
                if skip_dedup:
                    unprocessed = list(raw_emails)
                    _audit_log.log(
                        "listener", "filtered", True,
                        f"{len(unprocessed)} email(s) (dedup skipped)",
                        event_type="", project_ref="",
                    )
                else:
                    unprocessed = [
                        e for e in raw_emails
                        if not processed_store.is_processed(e.get("message_id"))
                    ]
                    _audit_log.log(
                        "listener", "filtered", True,
                        f"{len(unprocessed)} email(s) after filtering processed",
                        event_type="", project_ref="",
                    )

                records = await orchestrator_run(
                    listener=lambda: unprocessed,
                    phoenix=phoenix,
                    log=_audit_log,
                    milestone_map=milestone_map,
                    scraper_client=scraper_client,
                    processed_store=processed_store,
                    gmail=gmail,
                    skip_dedup=skip_dedup,
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
                # Resolve the processed-label id ONCE per batch. Cached
                # in-process by GmailClient after first lookup, so this is
                # a no-op on every tick but the first-after-boot.
                try:
                    processed_label_id = await asyncio.to_thread(
                        gmail.get_or_create_label, ROOFIX_PROCESSED_LABEL
                    )
                except Exception as e:
                    processed_label_id = None
                    _audit_log.log(
                        "gmail",
                        "label_lookup_failed",
                        False,
                        f"{ROOFIX_PROCESSED_LABEL!r}: {e}",
                        event_type="",
                        project_ref="",
                    )

                # Per-message try/except so one transient Gmail hiccup (429,
                # 5xx, 404 on a message that was deleted between fetch and
                # mark) can't cascade — pre-fix, a single failure here
                # abandoned every remaining mark_read for the batch, which
                # left rows marked `ok` in processed_store but still unread
                # in Gmail. That drift is what filled the 25-message fetch
                # window with stuck-unread emails on subsequent ticks.
                #
                # Order: label FIRST (unconditional — every evaluated email
                # gets tagged so the LISTENER_QUERY's `-label:...` excludes
                # it next tick), then mark_read only for the classes that
                # should also disappear from the operator's inbox view.
                # Failures on either step are audit-logged and skipped;
                # nothing else changes.
                processed_ids = set()
                for item in records:
                    d = item["decision"]
                    mid = d.get("message_id")
                    if not mid:
                        continue
                    etype = item.get("event", {}).get("event_type", "")

                    # ── Step 1: apply the "we've seen this" label ────────
                    # Unconditional. Even escalate-not-forwarded and AI-
                    # ignore rows get labeled — they still need to leave the
                    # bridge's fetch queue, they just stay unread visually.
                    if processed_label_id is not None:
                        try:
                            await asyncio.to_thread(
                                gmail.apply_label, mid, processed_label_id
                            )
                        except Exception as e:
                            _audit_log.log(
                                "gmail",
                                "apply_label_failed",
                                False,
                                f"{mid}: {e}",
                                event_type=etype,
                                project_ref="",
                            )

                    # ── Step 2: mark_read for the "clear from inbox" classes ─
                    action = d.get("action")
                    if action == "escalate" and not d.get("_forwarded"):
                        continue
                    if action == "ignore" and d.get("source") != "rule":
                        continue
                    try:
                        await asyncio.to_thread(gmail.mark_read, mid)
                        processed_ids.add(mid)
                    except Exception as e:
                        _audit_log.log(
                            "gmail",
                            "mark_read_failed",
                            False,
                            f"{mid}: {e}",
                            event_type=etype,
                            project_ref="",
                        )

        _record_tick(records, error=None)
        return {"records": records, "count": len(records)}


def _record_tick(records: list, error: Optional[str]) -> None:
    """``records`` is the list of {"event", "decision"} objects returned by
    ``orchestrator_run``. We only tally decision-side fields here.
    """
    with _STATE_LOCK:
        _STATE["last_tick_at"] = datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        )
        _STATE["last_tick_ok"] = error is None
        _STATE["last_tick_error"] = error
        _STATE["tick_count"] += 1
        _STATE["decisions_total"] += len(records)
        for item in records:
            d = item["decision"]
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
        return {"error": repr(e), "records": [], "count": 0}


@app.post("/execute/{message_id}")
async def execute(message_id: str) -> dict:
    """Manually run one Gmail message through the full pipeline.

    Fetches ``message_id`` from Gmail regardless of read/unread state,
    skips the ``processed_store`` dedup, and otherwise runs the same
    orchestrator path a scheduled tick would — Phoenix writes,
    ``mark_read``, escalation forwarding all happen normally (subject
    to ``DRY_RUN``). Serialized with the scheduler via the tick lock.

    Returns 404 if Gmail can't find the id under this token, 200 with
    the standard ``{records, count}`` shape on success, or 200 with
    ``{error, ...}`` if the pipeline itself raised.
    """
    with GmailClient() as gmail:
        msg = await asyncio.to_thread(gmail.fetch_one, message_id)
    if msg is None:
        raise HTTPException(
            status_code=404,
            detail=f"message_id {message_id!r} not found in Gmail",
        )
    try:
        return await _run_one_batch(raw_emails=[msg], skip_dedup=True)
    except Exception as e:
        _record_tick([], error=repr(e))
        return {"error": repr(e), "records": [], "count": 0}


@app.post("/reset/{message_id}")
async def reset(message_id: str) -> dict:
    """Reset a single email so the next tick re-fetches and re-analyzes it.

    Undoes the three gates the bridge sets after a normal run:
      1. ``processed_store`` — the row's status is flipped from ``ok``/``error``
         to ``pending``. ``is_processed()`` returns False for ``pending``,
         so the bridge-side dedup filter no longer skips this email.
      2. Gmail label — ``ROOFIX_PROCESSED_LABEL`` is removed so the
         ``-label:`` clause in ``LISTENER_QUERY`` no longer excludes it.
      3. Read/unread — the ``UNREAD`` label is re-added so ``is:unread``
         in ``LISTENER_QUERY`` matches it again.

    All three Gmail modifies are idempotent; safe to call repeatedly. The
    processed_store transition uses ``mark_pending`` (not delete) so the row's
    history is preserved.

    Returns ``{message_id, store, gmail: {unread, label_removed}, label}``.
    404 if the message id isn't visible to the OAuth token (Gmail 404). If
    processed_store has no row for this id, ``store`` is ``"unchanged"`` and
    the Gmail side still runs (useful if the row was manually deleted and
    the label is still stuck on the message).

    Prefer ``POST /execute/{message_id}`` for a one-shot immediate re-run —
    it needs no cleanup and returns the resulting decision. Use ``/reset``
    when you want the email to flow through the normal listener path on the
    next scheduled tick (e.g. as part of a batch, or to sanity-check the
    LISTENER_QUERY end-to-end).
    """
    from googleapiclient.errors import HttpError

    result = {
        "message_id": message_id,
        "store": "unchanged",
        "gmail": {"unread": False, "label_removed": False},
        "label": ROOFIX_PROCESSED_LABEL,
    }

    with (
        GmailClient() as gmail,
        PostgresProcessedStore(ROOFIX_DB_DSN) as processed_store,
    ):
        # Gmail first: a 404 aborts before we touch the store, so we don't
        # leave a half-reset row pointing at a message that no longer exists.
        try:
            await asyncio.to_thread(gmail.mark_unread, message_id)
            result["gmail"]["unread"] = True
        except HttpError as e:
            if e.resp.status == 404:
                raise HTTPException(
                    status_code=404,
                    detail=f"message_id {message_id!r} not found in Gmail",
                )
            raise

        try:
            label_id = await asyncio.to_thread(
                gmail.get_or_create_label, ROOFIX_PROCESSED_LABEL
            )
            await asyncio.to_thread(gmail.remove_label, message_id, label_id)
            result["gmail"]["label_removed"] = True
        except Exception as e:
            _audit_log.log(
                "gmail",
                "remove_label_failed",
                False,
                f"reset {message_id}: {e}",
                event_type="",
                project_ref="",
            )

        existing = await asyncio.to_thread(processed_store.get, message_id)
        if existing is not None:
            await asyncio.to_thread(
                processed_store.mark_pending,
                message_id,
                {"action": "reset", "prev_status": existing.status},
            )
            result["store"] = "reset"

    _audit_log.log(
        "orchestrator",
        "reset",
        True,
        f"reset {message_id}: store={result['store']}, "
        f"unread={result['gmail']['unread']}, "
        f"label_removed={result['gmail']['label_removed']}",
        event_type="",
        project_ref="",
    )
    return result


@app.post("/labels/backfill")
async def backfill_labels() -> dict:
    """One-time repair: apply the ``ROOFIX_PROCESSED_LABEL`` Gmail label to
    every message id currently marked ``ok`` or ``error`` in processed_store.

    Run this once after upgrading to the labeled-fetch model to sweep the
    existing backlog of stuck-unread rows out of the ``LISTENER_QUERY``
    window. Safe to re-run — ``apply_label`` is idempotent, so re-labeling
    already-labeled messages is a no-op on Gmail's side.

    Returns per-status counts: ``labeled`` (Gmail accepted the modify),
    ``skipped`` (row had no message_id — shouldn't happen for real rows),
    and ``failed`` (Gmail returned an error, e.g. message deleted). The
    failed count is not surfaced per-mid in the response body to keep it
    small; per-mid failures are logged to the audit CSV as
    ``gmail / apply_label_failed``.
    """
    counts = {"labeled": 0, "skipped": 0, "failed": 0}
    with (
        GmailClient() as gmail,
        PostgresProcessedStore(ROOFIX_DB_DSN) as processed_store,
    ):
        try:
            label_id = await asyncio.to_thread(
                gmail.get_or_create_label, ROOFIX_PROCESSED_LABEL
            )
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"could not resolve label {ROOFIX_PROCESSED_LABEL!r}: {e}",
            )

        # Walk every ok + error row. Skip pending (still in-flight).
        for status_name in ("ok", "error"):
            for rec in processed_store.list_by_status(status_name):
                mid = rec.key
                if not mid:
                    counts["skipped"] += 1
                    continue
                try:
                    await asyncio.to_thread(gmail.apply_label, mid, label_id)
                    counts["labeled"] += 1
                except Exception as e:
                    counts["failed"] += 1
                    _audit_log.log(
                        "gmail",
                        "apply_label_failed",
                        False,
                        f"backfill {mid}: {e}",
                        event_type="",
                        project_ref="",
                    )
    return {"label": ROOFIX_PROCESSED_LABEL, "counts": counts}


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
