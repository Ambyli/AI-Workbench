"""
Test the BRAIN's rules + escalation logic in isolation (Contract C). No AI call,
no Phoenix. Run from ai/roofix-bridge/:  PYTHONPATH=. python tests/test_brain.py
"""

import os
os.environ.setdefault("AGENT_PHASE", "0")

from components.parser import parse_email
from components.brain import decide
from tests.roofix_email_samples import SAMPLES

BY = {s["label"]: s for s in SAMPLES}
FOUND = lambda pid=101: {"found": True, "phoenix_project_id": pid, "ambiguous": False}

CASES = [
    ("new_comment_with_mention", FOUND(), "update_chatter", False),
    ("new_comment_thread",       FOUND(), "update_chatter", False),
    ("hic_executed",             FOUND(), "update_milestone", False),
    ("install_date_confirmed",   FOUND(), "update_milestone", False),
    ("estimate_complete",        FOUND(), "ignore", False),
    ("estimate_in_progress",     FOUND(), "ignore", False),
    ("new_task_select_funding",  FOUND(), "ignore", False),
    ("new_comment_thread",       {"found": False}, "escalate", True),
    ("install_date_confirmed",   {"found": False}, "escalate", True),
    ("hic_executed", {"found": True, "ambiguous": True, "candidate_count": 3},
                                                   "escalate", True),
]


def run():
    passed = failed = 0
    for label, ctx, want_action, want_human in CASES:
        ev = parse_email(BY[label]).as_dict()
        d = decide(ev, ctx).as_dict()
        ok = d["action"] == want_action and d["needs_human"] == want_human
        ctx_desc = "found" if ctx.get("found") and not ctx.get("ambiguous") else \
                   "ambiguous" if ctx.get("ambiguous") else "not-found"
        if ok:
            passed += 1
            print(f"ok    {label:26s} [{ctx_desc:9s}] -> {d['action']}")
        else:
            failed += 1
            print(f"FAIL  {label:26s} [{ctx_desc:9s}] -> got {d['action']}/"
                  f"human={d['needs_human']}, wanted {want_action}/human={want_human}")
    print(f"\n{passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    raise SystemExit(0 if run() else 1)
