"""
ORCHESTRATOR — the loop that turns the parts into a running agent.

Flow per batch of raw emails:
    parse each  ->  group by project identity  ->  collapse superseded events
    ->  resolve the Phoenix project (context)  ->  brain decides
    ->  [DRY_RUN: log intended action] or [execute via Phoenix client]
    ->  log every step; escalations to Jonathan are logged (notify wired Phase 1)

DRY_RUN (env): true -> decide + log what WOULD happen, write nothing.

The LISTENER is injected (a callable returning raw emails) so the orchestrator
can be tested with sample emails now and wired to the Gmail MCP on the server.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Optional

from common.logging_setup import CsvLogger

from components.parser import parse_email
from components.brain import decide
from components.proposal_extractor import extract_proposal

DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

# CSV audit schema — positional-arg order matches every call site's usage.
# `timestamp` is auto-prepended by CsvLogger; don't include it here.
LOG_COLUMNS = ["stage", "action", "ok", "detail", "event_type", "project_ref"]


def _default_log() -> CsvLogger:
    """Fallback CsvLogger for callers (e.g. tests) that don't inject one.

    Called by: `process_batch` and `run` when no log is passed in.
    """
    log_dir = os.getenv("LOG_DIR", "/data")
    return CsvLogger(path=Path(log_dir) / "agent_log.csv", columns=LOG_COLUMNS)


def _identity_key(ev: dict) -> str:
    """Build a grouping key so emails from the same real-world project are batched together.

    Priority: use the explicit `project_id` if present (most reliable).
    Otherwise fall back to a composite of customer_name + address.

    Called by: `process_batch` (inside the grouping loop).
    """
    if ev.get("project_id"):
        return f"id:{ev['project_id']}"
    return f"na:{(ev.get('customer_name') or '').lower()}|{(ev.get('address') or '').lower()}"


def _resolve_context(ev: dict, phoenix) -> dict:
    """Look up the Phoenix project that an event belongs to.

    Strategy:
      1. If the event carries a `project_id`, search by that Roofix external ID.
      2. Otherwise search by customer identity (name + optional address).

    Returns a dict with:
      - ``found`` (bool): did we find a matching project?
      - ``ambiguous`` (bool): were there multiple candidates?
      - ``phoenix_project_id`` (int, optional): the single-match Phoenix project id.
      - ``candidate_count`` (int, optional): how many candidates matched.
      - ``offline`` (bool, optional): True when phoenix client is unavailable.

    Called by: `process_batch` (once per event, inside the per-group loop).
    """
    if phoenix is None:
        return {"found": False, "offline": True}
    if ev.get("project_id"):
        r = phoenix.find_project_by_roofix_id(ev["project_id"])
        if r.ok:
            matches = r.data.get("matches", [])
            if len(matches) == 1:
                return {"found": True, "ambiguous": False,
                        "phoenix_project_id": matches[0]["id"]}
            if len(matches) > 1:
                return {"found": True, "ambiguous": True,
                        "candidate_count": len(matches)}
    if ev.get("customer_name"):
        r = phoenix.find_project_by_identity(ev["customer_name"], ev.get("address"))
        if r.ok:
            matches = r.data.get("matches", [])
            if len(matches) == 1:
                return {"found": True, "ambiguous": False,
                        "phoenix_project_id": matches[0]["id"]}
            if len(matches) > 1:
                return {"found": True, "ambiguous": True,
                        "candidate_count": len(matches)}
    return {"found": False, "ambiguous": False}


def _execute(decision: dict, ev: dict, phoenix, log: CsvLogger,
             milestone_map: Optional[dict],
             processed_store=None) -> None:
    """Carry out (or log) the brain's decision for a single event.

    Branches on ``decision["action"]``:
      - ``"ignore"``        → log the ignore, do nothing else.
      - ``"escalate"`` / needs_human → log for human review, skip Phoenix write.
      - ``"update_chatter"`` → call ``phoenix.update_chatter(project_id, note)``.
      - ``"update_milestone"`` → look up the milestone mapping, call
        ``phoenix.update_milestone(project_id, block_name, status_id)``.
      - anything else      → log as unsupported (Phase 0 gate).

    When ``phoenix is None`` or ``DRY_RUN=True``, the Phoenix write is skipped
    and a dry-run note is logged instead.

    Called by: `process_batch` (once per event, after ``decide()`` returns).
    """
    action = decision["action"]
    etype = ev.get("event_type", "")
    pref = decision.get("target") or ""

    match action:
        # ── Ignore ────────────────────────────────────────────────────
        # Log and stop. No Phoenix write, no human review.
        case "ignore":
            log.log("orchestrator", "ignore", True, decision["reasoning"],
                    event_type=etype, project_ref=pref)

        # ── Escalate / Needs human ────────────────────────────────────
        # Log for human review, skip Phoenix write.
        case "escalate" | _ if decision.get("needs_human"):
            log.log("escalate", action, True, "NEEDS HUMAN: " + decision["reasoning"],
                    event_type=etype, project_ref=pref)

        # ── Offline dry-run ───────────────────────────────────────────
        # If phoenix client is None, log and stop. No Phoenix write.
        case _ if phoenix is None:
            log.log("orchestrator", action, True,
                    "offline dry-run: " + decision["reasoning"],
                    event_type=etype, project_ref=pref)

        # ── Update chatter ────────────────────────────────────────────
        # Append a note to the Phoenix project's chatter.
        case "update_chatter":
            res = phoenix.update_chatter(int(pref), decision["payload"]["note_text"])
            log.log("phoenix", action, res.ok,
                    (("DRY_RUN " if res.dry_run else "") + res.detail),
                    event_type=etype, project_ref=pref)

        # ── Update milestone ──────────────────────────────────────────
        # Look up the milestone mapping, then advance the project's milestone.
        case "update_milestone":
            roofix_event = decision["payload"].get("roofix_event", etype)
            mapping = (milestone_map or {}).get(roofix_event)
            if not mapping:
                log.log("phoenix", action, False,
                        f"no milestone mapping for '{roofix_event}' (needs Michael)",
                        event_type=etype, project_ref=pref)
            else:
                res = phoenix.update_milestone(int(pref), mapping["block_name"],
                                               mapping["status_id"])
                log.log("phoenix", action, res.ok,
                        (("DRY_RUN " if res.dry_run else "") + res.detail),
                        event_type=etype, project_ref=pref)

        # ── Create project ────────────────────────────────────────────
        # Use the already-scraped data from the parser.
        case "create_project":
            # The parser already scraped the URL and populated the event.
            # Check if we have the required data.
            roofix_project_id = ev.get("project_id")
            if not roofix_project_id:
                log.log("orchestrator", action, False,
                        "create_project missing project_id (scraping failed?)",
                        event_type=etype, project_ref=pref)
                return

            # Check if the proposal was accepted
            is_accepted = ev.get("is_accepted", False)
            if not is_accepted:
                log.log("orchestrator", "not_accepted", True,
                        "proposal not accepted, skipping create",
                        event_type=etype, project_ref=pref)
                if processed_store:
                    processed_store.mark_ok(ev.get("message_id"), metadata={
                        "roofix_project_id": roofix_project_id,
                        "accepted": False,
                    })
                return

            # Call ensure_entity_and_project with the scraped data.
            if not phoenix:
                log.log("orchestrator", action, False,
                        "phoenix client required for create_project",
                        event_type=etype, project_ref=pref)
                return

            # Build the extracted proposal dict from the event data.
            extracted_data = {
                "roofix_project_id": roofix_project_id,
                "customer_name": ev.get("customer_name"),
                "address": ev.get("address"),
                # Add other fields as needed from the event/scraped data
            }

            res = phoenix.ensure_entity_and_project(extracted_data)

            log.log("phoenix", action, res.ok,
                    (("DRY_RUN " if res.dry_run else "") + res.detail),
                    event_type=etype, project_ref=pref)

            if res.ok:
                if processed_store:
                    processed_store.mark_ok(ev.get("message_id"), metadata={
                        "roofix_project_id": roofix_project_id,
                        "phoenix_entity_id": res.data.get("entity_id"),
                        "phoenix_project_id": res.data.get("phoenix_project_id"),
                        "accepted": True,
                    })
            else:
                if processed_store:
                    processed_store.mark_error(ev.get("message_id"), metadata={
                        "error": res.detail,
                    })

        # ── Unsupported action ────────────────────────────────────────
        # Log and stop. No Phoenix write.
        case _:
            log.log("orchestrator", action, False,
                    f"action '{action}' not enabled in Phase 0",
                    event_type=etype, project_ref=pref)


def process_batch(raw_emails: list, phoenix=None, log: Optional[CsvLogger] = None,
                  milestone_map: Optional[dict] = None,
                  scraper_client=None, processed_store=None) -> list:
    """Process a batch of raw emails through the full pipeline.

    Pipeline per event:
      1. **Parse** — convert raw email dict into a structured event via ``parse_email()``.
      2. **Group** — bucket events by project identity (``_identity_key``).
      3. **Sort** — within each group, order by ``email_timestamp`` (oldest first).
      4. **Resolve context** — look up the Phoenix project for the event.
      5. **Decide** — ask the brain what to do (``decide()``).
      6. **Execute** — carry out or log the decision (_execute).

    Every step is logged to the CsvLogger.

    Called by: `run` (the production entry point) and tests (direct invocation).

    Args:
        raw_emails: List of raw email dicts from the listener.
        phoenix: Phoenix client instance (None for offline / dry-run mode).
        log: CsvLogger for audit trail (falls back to default if omitted).
        milestone_map: Mapping from roofix event names to Phoenix block/status ids.
        scraper_client: RoofixScraperClient instance for scraping proposals.
        processed_store: ProcessedStore instance for tracking processed emails.

    Returns:
        List of decision dicts (one per event, after ``decide().as_dict()``).
    """
    log = log or _default_log()
    decisions = []

    # ── Step 1: Parse all raw emails into structured events ────────────────
    parsed = [parse_email(e, scraper_client=scraper_client).as_dict() for e in raw_emails]

    # ── Step 2: Group events by project identity ──────────────────────────
    # Each group contains all events belonging to the same Roofix project.
    groups: dict[str, list] = {}
    for ev in parsed:
        groups.setdefault(_identity_key(ev), []).append(ev)

    # ── Step 3: Process each project group ────────────────────────────────
    for key, evs in groups.items():
        # Sort events within each group by timestamp (oldest first)
        evs.sort(key=lambda e: e.get("email_timestamp") or "")

        # Process each event in the group
        for ev in evs:
            # Log the parsed event
            log.log("parser", "parsed", ev.get("parse_complete", False),
                    "; ".join(ev.get("notes", [])) or "ok",
                    event_type=ev.get("event_type", ""),
                    project_ref=key)

            # Resolve the project context from Phoenix
            ctx = _resolve_context(ev, phoenix)

            # Decide what to do based on the event and context
            d = decide(ev, ctx).as_dict()

            # Log the decision
            log.log("brain", d["action"], not d["needs_human"],
                    f"[{d['source']}] {d['reasoning']}",
                    event_type=ev.get("event_type", ""), project_ref=key)

            # Execute the decision
            _execute(d, ev, phoenix, log, milestone_map,
                     scraper_client=scraper_client,
                     processed_store=processed_store)

            # Add the decision to the results
            decisions.append(d)

    return decisions


def run(listener: Callable[[], list], phoenix=None, milestone_map=None,
        log: Optional[CsvLogger] = None,
        scraper_client=None, processed_store=None) -> list:
    """Production entry point: fetch one batch of emails and process it.

    This is the function called by the scheduler (APS) each tick. It pulls raw
    emails from the injected ``listener`` callable, logs how many were fetched,
    then delegates to ``process_batch()`` for the full parse → group → decide →
    execute pipeline.

    Called by: the APScheduler job in the bridge's main loop (``fetcher.py`` or
    the scheduler that invokes ``components.orchestrator.run`` every
    ``TICK_INTERVAL_SECONDS``).

    Args:
        listener: Callable that returns a list of raw email dicts (e.g. the
            Gmail client wrapper).
        phoenix: Phoenix client instance (None for dry-run mode).
        milestone_map: Mapping from roofix event names to Phoenix block/status ids.
        log: CsvLogger for audit trail.
        scraper_client: RoofixScraperClient instance for scraping proposals.
        processed_store: ProcessedStore instance for tracking processed emails.

    Returns:
        List of decision dicts (same as ``process_batch``).
    """
    log = log or _default_log()
    raw = listener()
    log.log("listener", "fetch", True, f"{len(raw)} email(s)")

    # Filter out already-processed emails.
    if processed_store:
        raw = [e for e in raw if not processed_store.is_processed(e.get("message_id"))]
        log.log("listener", "filtered", True,
                f"{len(raw)} email(s) after filtering processed",
                event_type="", project_ref="")

    return process_batch(
        raw, phoenix=phoenix, log=log, milestone_map=milestone_map,
        scraper_client=scraper_client, processed_store=processed_store)
