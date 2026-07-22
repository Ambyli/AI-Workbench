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
from typing import Callable, Optional

from components.parser import parse_email
from components.brain import decide
from components.logger import Logger

DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"


def _identity_key(ev: dict) -> str:
    if ev.get("project_id"):
        return f"id:{ev['project_id']}"
    return f"na:{(ev.get('customer_name') or '').lower()}|{(ev.get('address') or '').lower()}"


def _resolve_context(ev: dict, phoenix) -> dict:
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


def _execute(decision: dict, ev: dict, phoenix, log: Logger,
             milestone_map: Optional[dict]) -> None:
    action = decision["action"]
    etype = ev.get("event_type", "")
    pref = decision.get("target") or ""

    if action == "ignore":
        log.log("orchestrator", "ignore", True, decision["reasoning"],
                event_type=etype, project_ref=pref)
        return

    if decision.get("needs_human") or action == "escalate":
        log.log("escalate", action, True, "NEEDS HUMAN: " + decision["reasoning"],
                event_type=etype, project_ref=pref)
        return

    if phoenix is None:
        log.log("orchestrator", action, True,
                "offline dry-run: " + decision["reasoning"],
                event_type=etype, project_ref=pref)
        return

    if action == "update_chatter":
        res = phoenix.update_chatter(int(pref), decision["payload"]["note_text"])
        log.log("phoenix", action, res.ok,
                (("DRY_RUN " if res.dry_run else "") + res.detail),
                event_type=etype, project_ref=pref)
        return

    if action == "update_milestone":
        roofix_event = decision["payload"].get("roofix_event", etype)
        mapping = (milestone_map or {}).get(roofix_event)
        if not mapping:
            log.log("phoenix", action, False,
                    f"no milestone mapping for '{roofix_event}' (needs Michael)",
                    event_type=etype, project_ref=pref)
            return
        res = phoenix.update_milestone(int(pref), mapping["block_name"],
                                       mapping["status_id"])
        log.log("phoenix", action, res.ok,
                (("DRY_RUN " if res.dry_run else "") + res.detail),
                event_type=etype, project_ref=pref)
        return

    log.log("orchestrator", action, False, f"action '{action}' not enabled in Phase 0",
            event_type=etype, project_ref=pref)


def process_batch(raw_emails: list, phoenix=None, log: Optional[Logger] = None,
                  milestone_map: Optional[dict] = None) -> list:
    log = log or Logger()
    decisions = []

    parsed = [parse_email(e).as_dict() for e in raw_emails]

    groups: dict[str, list] = {}
    for ev in parsed:
        groups.setdefault(_identity_key(ev), []).append(ev)

    for key, evs in groups.items():
        evs.sort(key=lambda e: e.get("email_timestamp") or "")
        for ev in evs:
            log.log("parser", "parsed", ev.get("parse_complete", False),
                    "; ".join(ev.get("notes", [])) or "ok",
                    event_type=ev.get("event_type", ""),
                    project_ref=key)
            ctx = _resolve_context(ev, phoenix)
            d = decide(ev, ctx).as_dict()
            log.log("brain", d["action"], not d["needs_human"],
                    f"[{d['source']}] {d['reasoning']}",
                    event_type=ev.get("event_type", ""), project_ref=key)
            _execute(d, ev, phoenix, log, milestone_map)
            decisions.append(d)

    return decisions


def run(listener: Callable[[], list], phoenix=None, milestone_map=None,
        log: Optional[Logger] = None) -> list:
    """Production entry: pull a batch from the listener and process it once."""
    log = log or Logger()
    raw = listener()
    log.log("listener", "fetch", True, f"{len(raw)} email(s)")
    return process_batch(raw, phoenix=phoenix, log=log, milestone_map=milestone_map)
