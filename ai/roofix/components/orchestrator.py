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

import asyncio
import os
from pathlib import Path
from typing import Callable, Optional

from common.logging_setup import CsvLogger

from components.parser import parse_email
from components.brain import decide
from components.proposal_extractor import extract_proposal

# All module-level constants moved to components/constants.py — re-imported
# here so `from components.orchestrator import DRY_RUN` etc. still works and
# so we don't scatter `os.getenv` calls across the codebase.
from components.constants import (
    DRY_RUN,
    LOG_COLUMNS,
    IGNORE_EVENTS,
    EXTRACTED_PAYLOAD_FIELDS as _EXTRACTED_PAYLOAD_FIELDS,
    ESCALATION_RECIPIENTS,
    INTERCEPTOR_MAX_CONCURRENT,
    SCRAPE_TIMEOUT_SECONDS,
)

# Bridge-side rate limit on concurrent scrape calls. Constructed fresh at the
# top of each ``process_batch`` (so it binds to the currently-running event
# loop) and passed to every coroutine that reaches ``_scrape_and_extract``.
# MUST match (or be smaller than) interceptor's own INTERCEPTOR_MAX_CONCURRENT
# so we queue locally rather than pounding the server and eating 409s.
# asyncio.Semaphore serves waiters FIFO → first-come-first-serve automatic.


def _default_log() -> CsvLogger:
    """Fallback CsvLogger for callers (e.g. tests) that don't inject one.

    Called by: `process_batch` and `run` when no log is passed in.
    """
    log_dir = os.getenv("LOG_DIR", "/data")
    return CsvLogger(path=Path(log_dir) / "agent_log.csv", columns=LOG_COLUMNS)


def _identity_key(ev: dict) -> str:
    """Build a grouping key so emails from the same real-world project are batched together.

    Priority: use the explicit `roofix_id` if present (most reliable).
    Otherwise fall back to a composite of customer_name + address.

    Called by: `process_batch` (inside the grouping loop).
    """
    if ev.get("roofix_id"):
        return f"id:{ev['roofix_id']}"
    return f"na:{(ev.get('customer_name') or '').lower()}|{(ev.get('address') or '').lower()}"


async def _resolve_context(ev: dict, phoenix) -> dict:
    """Look up the Phoenix project that an event belongs to.

    Strategy:
      1. If the event carries a `roofix_id`, search by that Roofix external ID.
      2. Otherwise search by customer identity (name + optional address).

    Returns a dict with:
      - ``found`` (bool): did we find a matching project?
      - ``ambiguous`` (bool): were there multiple candidates?
      - ``project_id`` (int, optional): the single-match Phoenix project id.
      - ``candidate_count`` (int, optional): how many candidates matched.
      - ``offline`` (bool, optional): True when phoenix client is unavailable.

    Phoenix's psycopg2 client is sync — we wrap its calls in
    ``asyncio.to_thread`` so a slow DB round-trip doesn't stall the event loop
    while other event groups continue processing.

    Called by: `process_batch` (once per event, inside the per-group loop).
    """
    if phoenix is None:
        return {"found": False, "offline": True}
    if ev.get("roofix_id"):
        r = await asyncio.to_thread(phoenix.find_project_by_roofix_id, ev["roofix_id"])
        if r.ok:
            matches = r.data.get("matches", [])
            if len(matches) == 1:
                return {
                    "found": True,
                    "ambiguous": False,
                    "project_id": matches[0]["id"],
                }
            if len(matches) > 1:
                return {
                    "found": True,
                    "ambiguous": True,
                    "candidate_count": len(matches),
                }
    if ev.get("customer_name"):
        r = await asyncio.to_thread(
            phoenix.find_project_by_identity, ev["customer_name"], ev.get("address")
        )
        if r.ok:
            matches = r.data.get("matches", [])
            if len(matches) == 1:
                return {
                    "found": True,
                    "ambiguous": False,
                    "project_id": matches[0]["id"],
                }
            if len(matches) > 1:
                return {
                    "found": True,
                    "ambiguous": True,
                    "candidate_count": len(matches),
                }
    return {"found": False, "ambiguous": False}


def _extracted_to_payload(extracted) -> dict:
    """Convert an ExtractedProposal (or test fake) into a plain dict.

    Uses ``getattr(..., None)`` so it works with the real dataclass, subclass
    fakes, and MagicMocks. Only the fields ensure_entity_and_project reads (or
    that a future consumer might read) are copied — that keeps the payload
    inspectable in audit / debug output.
    """
    return {f: getattr(extracted, f, None) for f in _EXTRACTED_PAYLOAD_FIELDS}


async def _scrape_and_extract(
    ev: dict,
    scraper_client,
    log: "CsvLogger",
    key: str,
    processed_store,
    scrape_sem: asyncio.Semaphore,
) -> bool:
    """Fetch + extract the proposal behind ``ev["tracking_url"]``.

    Preconditions the caller must verify before invoking:
      * ``ev["tracking_url"]`` is present
      * a scraper_client instance is available
      * either the Phoenix resolve missed or returned an ambiguous match —
        a single-match resolve leaves nothing to refine, so the scrape
        would be waste
      * event type is NOT in ``IGNORE_EVENTS`` — those short-circuit before
        the gate ever runs since the brain ignores them regardless

    Concurrency: ``scrape_sem`` (created per-batch by process_batch) rate-limits
    outbound scrapes to ``INTERCEPTOR_MAX_CONCURRENT``. On a 25-email burst all
    coroutines call this at once but only 8 hit interceptor at a time —
    the rest queue FIFO inside the semaphore. ``SCRAPE_TIMEOUT_SECONDS``
    (default 600s) is a safety net; ``asyncio.wait_for`` unblocks the whole
    coroutine if interceptor hangs.

    Emits audit rows under ``scraper`` (fetch, no_docs, error, timeout) and
    ``extractor`` (extracted) stages as it goes. On the ``no_docs`` /
    ``timeout`` paths the event is marked as an error in ``processed_store``
    so it doesn't get re-attempted every tick (until the operator clears the
    error).

    On extractor success, mutates ``ev``:
      customer_name / address filled in from the scraped proposal,
      roofix_id stamped from ``custom.order1``'s top-level id,
      is_accepted / parse_complete stamped, an audit note appended,
      ``_extracted_payload`` stashed for ``_execute`` to pass onward to
      ``phoenix.ensure_entity_and_project``.

    Returns True iff the extraction produced usable data (caller should
    re-resolve Phoenix context so the brain sees the sharper identity).

    Called by: ``process_batch`` (per event, only on the Phoenix-miss path).
    """
    etype = ev.get("event_type", "")
    # Instrumentation: log the semaphore state around every scrape acquisition
    # so we can prove parallelism (or catch serialization). ``_value`` is a
    # public-ish attribute on asyncio.Semaphore giving the count of remaining
    # slots BEFORE this coroutine grabs one — subtract from cap for in-flight.
    _cap = INTERCEPTOR_MAX_CONCURRENT
    _slots_free_before = getattr(scrape_sem, "_value", -1)
    _in_flight_before = _cap - _slots_free_before if _slots_free_before >= 0 else -1
    log.log(
        "scraper",
        "sem_wait",
        True,
        f"waiting on scrape semaphore  in_flight_before_me={_in_flight_before}/{_cap}",
        event_type=etype,
        project_ref=key,
    )
    try:
        async with scrape_sem:
            _slots_free_after = getattr(scrape_sem, "_value", -1)
            _in_flight_after = _cap - _slots_free_after if _slots_free_after >= 0 else -1
            log.log(
                "scraper",
                "sem_acquired",
                True,
                f"acquired scrape semaphore  in_flight_including_me={_in_flight_after}/{_cap}",
                event_type=etype,
                project_ref=key,
            )
            result = await asyncio.wait_for(
                scraper_client.get_proposal(tracking_url=ev["tracking_url"]),
                timeout=SCRAPE_TIMEOUT_SECONDS,
            )
    except asyncio.TimeoutError:
        log.log(
            "scraper",
            "timeout",
            False,
            f"scrape exceeded {SCRAPE_TIMEOUT_SECONDS}s",
            event_type=etype,
            project_ref=key,
        )
        if processed_store:
            await asyncio.to_thread(
                processed_store.mark_error,
                ev.get("message_id"),
                {"error": f"scrape_timeout_{SCRAPE_TIMEOUT_SECONDS}s"},
            )
        ev.setdefault("notes", []).append(f"scrape timeout ({SCRAPE_TIMEOUT_SECONDS}s)")
        return False
    except Exception as e:
        log.log(
            "scraper",
            "error",
            False,
            f"exception: {e}",
            event_type=etype,
            project_ref=key,
        )
        ev.setdefault("notes", []).append(f"scrape failed: {e}")
        return False

    if not result.get("mget_docs"):
        log.log(
            "scraper",
            "no_docs",
            False,
            "scraper returned no docs",
            event_type=etype,
            project_ref=key,
        )
        if processed_store:
            await asyncio.to_thread(
                processed_store.mark_error,
                ev.get("message_id"),
                {"error": "no_docs"},
            )
        ev.setdefault("notes", []).append("scrape returned no docs")
        return False

    log.log(
        "scraper",
        "fetched",
        True,
        f"{len(result.get('mget_docs') or [])} docs",
        event_type=etype,
        project_ref=key,
    )

    extracted = extract_proposal(result)
    log.log(
        "extractor",
        "extracted",
        bool(getattr(extracted, "ok", False)),
        getattr(extracted, "error", None) or "extraction ok",
        event_type=etype,
        project_ref=key,
    )
    if not getattr(extracted, "ok", False):
        ev.setdefault("notes", []).append(
            f"scrape parse failed: {getattr(extracted, 'error', None)}"
        )
        return False

    ev["customer_name"] = getattr(extracted, "full_name", None) or ev.get(
        "customer_name"
    )
    ev["address"] = getattr(extracted, "street_address", None) or ev.get("address")
    ev["roofix_id"] = getattr(extracted, "roofix_id", None)
    ev["is_accepted"] = bool(getattr(extracted, "is_accepted", False))
    ev["parse_complete"] = True
    ev["_extracted_payload"] = _extracted_to_payload(extracted)
    ev.setdefault("notes", []).append("scraped URL for better data")
    return True


async def _execute(
    decision: dict,
    ev: dict,
    phoenix,
    log: CsvLogger,
    milestone_map: Optional[dict],
    processed_store=None,
    gmail=None,
) -> None:
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
        # Terminal decision — log, then mark processed so next tick doesn't
        # re-parse this same email into this same ignore. Whether we ALSO
        # mark it read in Gmail is app.py's call (currently rule-source only;
        # AI-decided ignores stay unread for operator review).
        case "ignore":
            log.log(
                "orchestrator",
                "ignore",
                True,
                decision["reasoning"],
                event_type=etype,
                project_ref=pref,
            )
            if processed_store:
                await asyncio.to_thread(
                    processed_store.mark_ok,
                    ev.get("message_id"),
                    {
                        "action": "ignore",
                        "source": decision.get("source", "rule"),
                        "reasoning": decision["reasoning"],
                    },
                )

        # ── Noop: project already exists in Phoenix ───────────────────
        # Brain emitted this when ``context.get("project_id")`` was
        # set for a SCRAPE_EVENTS event — Phoenix already has the project,
        # so a create would be a duplicate. Terminal decision: audit-log,
        # mark processed. No Phoenix write. app.py will mark the email
        # read (this is a rule-source terminal decision, same class as
        # rule-based ignore).
        case "noop_project_exists":
            log.log(
                "orchestrator",
                "noop_project_exists",
                True,
                decision["reasoning"],
                event_type=etype,
                project_ref=pref,
            )
            if processed_store:
                await asyncio.to_thread(
                    processed_store.mark_ok,
                    ev.get("message_id"),
                    {
                        "action": "noop_project_exists",
                        "source": decision.get("source", "rule"),
                        "reasoning": decision["reasoning"],
                        "project_id": pref,
                    },
                )

        # ── Escalate / Needs human ────────────────────────────────────
        # Log for human review, skip Phoenix write. Attempt to forward the
        # original email to ESCALATION_RECIPIENTS if configured:
        #   - forward ok  → stamp _forwarded=True (app.py mark_reads it),
        #                    mark_ok(processed_store).
        #   - forward err → mark_ok anyway so we don't re-forward next tick;
        #                    _forwarded stays False so app.py leaves it unread.
        #   - no recipients → mark_ok, _forwarded=False. Original stays unread
        #                    so the operator reviews via the Roofix inbox.
        # In all three sub-cases the email is marked processed exactly once.
        case "escalate" | _ if decision.get("needs_human"):
            log.log(
                "escalate",
                action,
                True,
                "NEEDS HUMAN: " + decision["reasoning"],
                event_type=etype,
                project_ref=pref,
            )
            forwarded = False
            raw_email = ev.get("_raw_email")
            if gmail and ESCALATION_RECIPIENTS and raw_email:
                # Bundle a record for the operator: the parsed event (with
                # internal ``_``-prefixed fields stripped so we don't leak the
                # raw email dict twice) plus the brain's decision. Gives the
                # human the same picture the bridge had when it decided to
                # escalate — no need to hand-reconstruct it.
                event_snapshot = {
                    k: v for k, v in ev.items() if not k.startswith("_")
                }
                record = {"event": event_snapshot, "decision": decision}
                try:
                    await asyncio.to_thread(
                        gmail.forward_email,
                        ESCALATION_RECIPIENTS,
                        decision["reasoning"],
                        raw_email,
                        etype,
                        record,
                    )
                    forwarded = True
                    log.log(
                        "escalate",
                        "forwarded",
                        True,
                        f"forwarded to {', '.join(ESCALATION_RECIPIENTS)}",
                        event_type=etype,
                        project_ref=pref,
                    )
                except Exception as e:
                    log.log(
                        "escalate",
                        "forward_failed",
                        False,
                        f"forward exception: {e}",
                        event_type=etype,
                        project_ref=pref,
                    )
            decision["_forwarded"] = forwarded
            if processed_store:
                await asyncio.to_thread(
                    processed_store.mark_ok,
                    ev.get("message_id"),
                    {
                        "action": "escalate",
                        "forwarded": forwarded,
                        "source": decision.get("source", "rule"),
                        "reasoning": decision["reasoning"],
                    },
                )

        # ── Offline dry-run ───────────────────────────────────────────
        # If phoenix client is None, log and stop. No Phoenix write.
        case _ if phoenix is None:
            log.log(
                "orchestrator",
                action,
                True,
                "offline dry-run: " + decision["reasoning"],
                event_type=etype,
                project_ref=pref,
            )

        # ── Update chatter ────────────────────────────────────────────
        # Append a note to the Phoenix project's chatter.
        case "update_chatter":
            res = await asyncio.to_thread(
                phoenix.update_chatter,
                int(pref),
                decision["payload"]["note_text"],
            )
            log.log(
                "phoenix",
                action,
                res.ok,
                (("DRY_RUN " if res.dry_run else "") + res.detail),
                event_type=etype,
                project_ref=pref,
            )

        # ── Update milestone ──────────────────────────────────────────
        # Look up the milestone mapping, then advance the project's milestone.
        # The mapping key is the event_type (already stamped on the decision at
        # the top level — no need to duplicate it inside payload).
        case "update_milestone":
            mapping = (milestone_map or {}).get(etype)
            if not mapping:
                log.log(
                    "phoenix",
                    action,
                    False,
                    f"no milestone mapping for '{etype}' (needs Michael)",
                    event_type=etype,
                    project_ref=pref,
                )
            else:
                res = await asyncio.to_thread(
                    phoenix.update_milestone,
                    int(pref),
                    mapping["block_name"],
                    mapping["status_id"],
                )
                log.log(
                    "phoenix",
                    action,
                    res.ok,
                    (("DRY_RUN " if res.dry_run else "") + res.detail),
                    event_type=etype,
                    project_ref=pref,
                )

        # ── Create project ────────────────────────────────────────────
        # Use the already-scraped data from ``_scrape_and_extract``, which
        # stashes ``_extracted_payload`` on ev when it succeeds. If the scrape
        # never ran or failed the payload will be missing — the scrape-side
        # no_docs path already marked processed_store, so we just bail.
        case "create_project":
            # The scrape step already populated the event with the extracted
            # proposal dict. Check if we have the required data.
            payload = ev.get("_extracted_payload") or {}
            roofix_id = payload.get("roofix_id")
            if not roofix_id:
                log.log(
                    "orchestrator",
                    action,
                    False,
                    "create_project missing scraped data (scraping failed?)",
                    event_type=etype,
                    project_ref=pref,
                )
                return

            # Check if the proposal was accepted.
            if not payload.get("is_accepted"):
                log.log(
                    "orchestrator",
                    "not_accepted",
                    True,
                    "proposal not accepted, skipping create",
                    event_type=etype,
                    project_ref=pref,
                )
                if processed_store:
                    await asyncio.to_thread(
                        processed_store.mark_ok,
                        ev.get("message_id"),
                        {"roofix_id": roofix_id, "accepted": False},
                    )
                return

            # Call ensure_entity_and_project with the scraped data.
            if not phoenix:
                log.log(
                    "orchestrator",
                    action,
                    False,
                    "phoenix client required for create_project",
                    event_type=etype,
                    project_ref=pref,
                )
                return

            # Hand the full extracted proposal dict straight to Phoenix — it
            # reads whichever fields it needs (name, address, contract price,
            # etc.) and ignores keys it doesn't care about.
            res = await asyncio.to_thread(phoenix.ensure_entity_and_project, payload)

            log.log(
                "phoenix",
                action,
                res.ok,
                (("DRY_RUN " if res.dry_run else "") + res.detail),
                event_type=etype,
                project_ref=pref,
            )

            if res.ok:
                if processed_store:
                    await asyncio.to_thread(
                        processed_store.mark_ok,
                        ev.get("message_id"),
                        {
                            "roofix_id": roofix_id,
                            "phoenix_entity_id": res.data.get("entity_id"),
                            "project_id": res.data.get("project_id"),
                            "accepted": True,
                        },
                    )
            else:
                if processed_store:
                    await asyncio.to_thread(
                        processed_store.mark_error,
                        ev.get("message_id"),
                        {"error": res.detail},
                    )

        # ── Unsupported action ────────────────────────────────────────
        # Brain produced something we can't route — most likely an AI
        # hallucination, occasionally a Phase 1 action leaking into Phase 0.
        # mark_error (not mark_ok) so it's not silenced: is_processed()
        # returns False for error rows, so the next tick will re-parse and
        # give the brain another chance. If the model keeps producing the
        # same garbage the recurring errors are a signal to tune the prompt.
        case _:
            log.log(
                "orchestrator",
                action,
                False,
                f"action '{action}' not enabled in Phase 0",
                event_type=etype,
                project_ref=pref,
            )
            if processed_store:
                await asyncio.to_thread(
                    processed_store.mark_error,
                    ev.get("message_id"),
                    {
                        "error": f"unsupported action '{action}'",
                        "source": decision.get("source", "rule"),
                        "reasoning": decision.get("reasoning", ""),
                    },
                )


async def _process_group(
    key: str,
    evs: list,
    phoenix,
    log: CsvLogger,
    milestone_map: Optional[dict],
    scraper_client,
    processed_store,
    gmail,
    scrape_sem: asyncio.Semaphore,
) -> list:
    """Process one project group's events SEQUENTIALLY (timestamp order).

    Groups run in parallel with each other via ``asyncio.gather`` up in
    ``process_batch`` — but within a group we serialize because chatter/
    milestone events for the same project must apply in order (a later
    chatter append shouldn't race with an earlier one).

    Returns a list of ``{"event": <parsed event>, "decision": <decision dict>}``
    records — one per event in this group. The event is returned alongside
    the decision so callers (app.py, /tick response, tests) can see the
    fully-hydrated event that produced the decision — including
    scraper-populated fields like ``_extracted_payload``. ``_raw_email`` is
    stripped from the returned event because it holds the entire fetched
    Gmail message (headers + full HTML body) — big, redundant with what
    triggered the tick, and rarely useful downstream.
    """
    evs.sort(key=lambda e: e.get("email_timestamp") or "")
    out: list = []

    for ev in evs:
        log.log(
            "parser",
            "parsed",
            ev.get("parse_complete", False),
            "; ".join(ev.get("notes", [])) or "ok",
            event_type=ev.get("event_type", ""),
            project_ref=key,
        )

        etype = ev.get("event_type", "")

        # ── IGNORE_EVENTS short-circuit ────────────────────────────────
        # These are Roofix-side prompts for a human action (New Task,
        # Send HIC, Select Funding, …) that the brain ignores regardless
        # of Phoenix state. Skip both the Phoenix lookup AND the scrape
        # entirely — neither can change the outcome, and paying for a
        # ~30s scrape on every ignored miss is pure waste. ``decide()``
        # still runs below and produces the ignore Decision as usual.
        # ``ev["project_id"]`` stays at the None it was initialized to;
        # the audit record shows the abbreviated flow.
        if etype in IGNORE_EVENTS:
            ctx = {"found": False, "ambiguous": False}
            log.log(
                "orchestrator",
                "ignore_shortcut",
                True,
                f"'{etype}' is ignore-eligible; skipping Phoenix + scrape",
                event_type=etype,
                project_ref=key,
            )
        else:
            # Resolve the project context from Phoenix — the single
            # authoritative "does Phoenix know this project?" check. Its
            # result flows straight into decide() below, so the brain sees
            # the same answer we used to gate the scrape. Stamp the
            # resolved Phoenix id onto ev so both decide() and the returned
            # record can read it from one place — None when Phoenix missed
            # or came back ambiguous.
            ctx = await _resolve_context(ev, phoenix)
            ev["project_id"] = ctx.get("project_id")

            # ── Scrape gate ────────────────────────────────────────────
            # Two independent reasons to scrape:
            #
            #   1. Phoenix MISS on any acted-on event. The scrape yields
            #      the authoritative roofix_id from custom.order1; the
            #      re-resolve routes via ``find_project_by_roofix_id``,
            #      which forgives the name-drift (extra spaces, middle
            #      names, address formatting) that the name+address
            #      lookup can't. For Estimate-family events the scrape
            #      also carries the proposal data downstream needs.
            #
            #   2. Phoenix AMBIGUOUS (multiple candidate projects). Same
            #      fix path: pin identity by scraped id, re-resolve to
            #      one row.
            #
            # Both require a scraper_client and a tracking_url — no scrape
            # is possible without them. Single-match Phoenix hits skip
            # the scrape (already unambiguous). ``IGNORE_EVENTS`` never
            # reach this gate — they short-circuit above.
            # ``_scrape_and_extract`` logs its own scraper/extractor audit
            # rows and marks processed_store on no_docs / timeout — we
            # don't wrap here.
            has_scrape_prereqs = bool(scraper_client and ev.get("tracking_url"))
            scrape_for_miss = has_scrape_prereqs and not ctx.get("found")
            scrape_for_ambiguity = has_scrape_prereqs and ctx.get("ambiguous")
            will_scrape = scrape_for_miss or scrape_for_ambiguity

            if will_scrape:
                if scrape_for_ambiguity:
                    gate_detail = (
                        f"will scrape (phoenix returned "
                        f"{ctx.get('candidate_count', '?')} candidates — need refinement)"
                    )
                else:
                    gate_detail = "will scrape (phoenix miss on identity lookup)"
            else:
                reasons = []
                if not scraper_client:
                    reasons.append("no scraper_client")
                if not ev.get("tracking_url"):
                    reasons.append("no tracking_url")
                if ctx.get("found") and not ctx.get("ambiguous"):
                    reasons.append("phoenix resolved unambiguously")
                gate_detail = "skip: " + "; ".join(reasons)
            log.log(
                "scraper",
                "gate",
                True,
                gate_detail,
                event_type=etype,
                project_ref=key,
            )

            if will_scrape:
                if await _scrape_and_extract(
                    ev, scraper_client, log, key, processed_store, scrape_sem
                ):
                    ctx = await _resolve_context(ev, phoenix)
                    ev["project_id"] = ctx.get("project_id")

        # Decide what to do based on the event and context. Stamp the
        # source email's Gmail id onto the Decision so app.py can call
        # mark_read on the right message without reverse-lookups.
        decision = await decide(ev, ctx)
        decision.message_id = ev.get("message_id")
        d = decision.as_dict()

        log.log(
            "brain",
            d["action"],
            not d["needs_human"],
            f"[{d['source']}] {d['reasoning']}",
            event_type=ev.get("event_type", ""),
            project_ref=key,
        )

        await _execute(
            d,
            ev,
            phoenix,
            log,
            milestone_map,
            processed_store=processed_store,
            gmail=gmail,
        )

        # Shallow-copy the ev so we can drop `_raw_email` without mutating
        # the dict `_execute` just used (also keeps this loop side-effect
        # free for the caller's copy). ``project_id`` was already stamped
        # onto ev after each ``_resolve_context`` call above — always
        # present as a key, None when Phoenix missed or came back
        # ambiguous — so it comes through this copy naturally.
        ev_out = {k: v for k, v in ev.items() if k != "_raw_email"}
        out.append({"event": ev_out, "decision": d})

    return out


async def process_batch(
    raw_emails: list,
    phoenix=None,
    log: Optional[CsvLogger] = None,
    milestone_map: Optional[dict] = None,
    scraper_client=None,
    processed_store=None,
    gmail=None,
) -> list:
    """Process a batch of raw emails through the full pipeline.

    Pipeline per event:
      1. **Parse** — convert raw email dict into a structured event via ``parse_email()``.
      2. **Group** — bucket events by project identity (``_identity_key``).
      3. **Fan out across groups** — ``asyncio.gather`` runs every group
         concurrently. Groups are independent (they target different Phoenix
         projects) so this is safe.
      4. Inside each group ``_process_group`` runs sequentially: resolve
         context → optional scrape (rate-limited by ``scrape_sem``) → decide
         → execute → repeat for the next event in the group.

    Every step is logged to the CsvLogger.

    Called by: `run` (the production entry point) and tests (direct invocation).

    Args:
        raw_emails: List of raw email dicts from the listener.
        phoenix: Phoenix client instance (None for offline / dry-run mode).
        log: CsvLogger for audit trail (falls back to default if omitted).
        milestone_map: Mapping from roofix event names to Phoenix block/status ids.
        scraper_client: RoofixScraperClient instance for scraping proposals.
        processed_store: ProcessedStore instance for tracking processed emails.
        gmail: GmailClient for escalation forwarding.

    Returns:
        List of ``{"event": <parsed event>, "decision": <decision dict>}``
        records — one per event, in whatever order ``asyncio.gather`` resolves
        the groups (which preserves the input-iterable order of groups, i.e.
        first-seen event order). ``event["_raw_email"]`` is stripped from the
        returned copy; ``event["_extracted_payload"]`` is kept.
    """
    log = log or _default_log()

    # ── Step 1: Parse all raw emails into structured events ────────────────
    # Parser is pure: extracts fields from the email only. Scraping and
    # Phoenix lookups happen below, so we don't pay for them twice.
    # ``_raw_email`` is stashed so downstream (escalation forwarding) can
    # access the original headers + body without a re-fetch. Underscore
    # marks it as internal — never leaves the orchestrator.
    # ``project_id`` is the Phoenix project id — the parser never sets it
    # (Phoenix identity is not the parser's job; see parser.py docstring).
    # Initialized to None here so every event carries the key from the
    # moment it exists; ``_process_group`` fills it in after each
    # ``_resolve_context`` call.
    parsed = []
    for e in raw_emails:
        p = parse_email(e).as_dict()
        p["_raw_email"] = e
        p["project_id"] = None
        parsed.append(p)

    # ── Step 2: Group events by project identity ──────────────────────────
    # Each group contains all events belonging to the same Roofix project.
    groups: dict[str, list] = {}
    for ev in parsed:
        groups.setdefault(_identity_key(ev), []).append(ev)

    # Log the fanout shape so we can see, from the audit trail, how much
    # parallelism the tick actually has to work with. 25 events in 25 groups
    # → up to 25 group coroutines running concurrently (scrape-capped at
    # INTERCEPTOR_MAX_CONCURRENT). 25 events in 1 group → sequential.
    _group_sizes = sorted((len(evs) for evs in groups.values()), reverse=True)
    log.log(
        "orchestrator",
        "fanout",
        True,
        f"{len(parsed)} event(s) → {len(groups)} group(s); sizes={_group_sizes[:10]}"
        + (f" (+{len(_group_sizes) - 10} more)" if len(_group_sizes) > 10 else ""),
        event_type="",
        project_ref="",
    )

    # ── Step 3: One semaphore per batch, tied to the current event loop.
    # Rate-limits scrape calls across ALL groups running concurrently. See
    # the module docstring at the top of the file for the rationale.
    scrape_sem = asyncio.Semaphore(INTERCEPTOR_MAX_CONCURRENT)

    # ── Step 4: Fan out across groups (parallel), each group sequential.
    group_results = await asyncio.gather(
        *[
            _process_group(
                key,
                evs,
                phoenix,
                log,
                milestone_map,
                scraper_client,
                processed_store,
                gmail,
                scrape_sem,
            )
            for key, evs in groups.items()
        ]
    )

    # Flatten. gather() preserves the order of the input iterable, so
    # groups appear in insertion order (which follows first-seen event
    # order from ``raw_emails``). Each item is an {"event", "decision"} record.
    return [item for group in group_results for item in group]


async def run(
    listener: Callable[[], list],
    phoenix=None,
    milestone_map=None,
    log: Optional[CsvLogger] = None,
    scraper_client=None,
    processed_store=None,
    gmail=None,
    skip_dedup: bool = False,
) -> list:
    """Production entry point: fetch one batch of emails and process it.

    Called by the APScheduler ``AsyncIOScheduler`` job in ``app.py`` every
    ``TICK_INTERVAL_SECONDS`` and by ``POST /tick``. Both entry points
    serialize behind ``app._tick_lock`` so a slow tick can't be trampled by
    a fresh one.

    Args:
        listener: Callable that returns a list of raw email dicts (e.g. the
            Gmail client wrapper).
        phoenix: Phoenix client instance (None for dry-run mode).
        milestone_map: Mapping from roofix event names to Phoenix block/status ids.
        log: CsvLogger for audit trail.
        scraper_client: RoofixScraperClient instance for scraping proposals.
        processed_store: ProcessedStore instance for tracking processed emails.
        gmail: GmailClient for escalation forwarding.
        skip_dedup: When True, bypass the ``processed_store.is_processed``
            filter so already-handled emails can be re-run. Used by
            ``POST /execute/{message_id}``. Default False.

    Returns:
        List of ``{"event", "decision"}`` records (same shape as
        ``process_batch``).
    """
    log = log or _default_log()
    raw = listener()
    log.log("listener", "fetch", True, f"{len(raw)} email(s)")

    # Filter out already-processed emails. Skipped for manual re-runs.
    if processed_store and not skip_dedup:
        raw = [e for e in raw if not processed_store.is_processed(e.get("message_id"))]
        log.log(
            "listener",
            "filtered",
            True,
            f"{len(raw)} email(s) after filtering processed",
            event_type="",
            project_ref="",
        )

    return await process_batch(
        raw,
        phoenix=phoenix,
        log=log,
        milestone_map=milestone_map,
        scraper_client=scraper_client,
        processed_store=processed_store,
        gmail=gmail,
    )
