"""
Test the PARSER alone against real Roofix email samples (Contract B).
Run from ai/roofix/:   PYTHONPATH=. python tests/test_parser.py
"""

from components.parser import parse_email
from tests.roofix_email_samples import SAMPLES


# Sentinel for "field must be present and truthy, exact value irrelevant."
# Use for fields we don't want to pin to an exact literal (like the tokenized
# tracking URL — the URL rotates per Roofix send, but its PRESENCE is a
# load-bearing invariant for the scraper's downstream lookup).
class _PresentSentinel:
    def __repr__(self) -> str:
        return "<PRESENT>"


PRESENT = _PresentSentinel()


EXPECTED = {
    "new_comment_with_mention": {
        "event_type": "New Comment",
        "customer_name": "LaFonda Mcwilliams Wyatt",
        "mentioned_users": ["Andrew_Lusk"],
        "parse_complete": True,
    },
    "new_comment_thread": {
        "event_type": "New Comment",
        "customer_name": "LaFonda Mcwilliams Wyatt",
        "parse_complete": True,
    },
    "new_task_select_funding": {
        "event_type": "New Task",
        "customer_name": "Debbie Bush",
        "parse_complete": True,
    },
    "estimate_complete": {
        "event_type": "Estimate Complete",
        "customer_name": "David Estes",
        "address_suffix": "Reorder",
        "parse_complete": False,
    },
    "estimate_in_progress": {
        "event_type": "Estimate",
        "customer_name": "Rosa Gonzales",
        "parse_complete": False,
    },
    "hic_executed": {
        "event_type": "HIC Executed",
        "customer_name": "Conner broaddus",
        "parse_complete": True,
    },
    "install_date_confirmed": {
        "event_type": "Install Date",
        "customer_name": "Robert Shepherd",
        "parse_complete": True,
    },
    "new_task_with_url_in_body": {
        "event_type": "New Task",
        "roofix_id": None,
        "parse_complete": True,
    },
    # ── Real "RFX | Estimate" samples ──────────────────────────────────────────
    # Load-bearing invariants for these:
    #   roofix_id is None — Roofix never puts the raw /project/<id> link in
    #     Estimate emails; only the tokenized url<NNNN>.roofix.io/ls/click...
    #     tracking URL. Scraper follows the tracking URL to acquire the id.
    #   tracking_url is PRESENT — the scraper's entrypoint. If this breaks,
    #     the Estimate pipeline is dead.
    #   parse_complete is False — Estimate is in CREATE_PROJECT_EVENTS.
    "estimate_in_progress_gerald_kang_836_lasser_drive": {
        "event_type": "Estimate",
        "customer_name": "Gerald kang",
        "address": "836 Lasser Drive",
        "roofix_id": None,
        "tracking_url": PRESENT,
        "parse_complete": False,
    },
    "estimate_in_progress_linda_ward_1512_fairlane_avenue_southwe": {
        "event_type": "Estimate",
        "customer_name": "Linda ward",
        "address": "1512 Fairlane Avenue Southwest",
        "roofix_id": None,
        "tracking_url": PRESENT,
        "parse_complete": False,
    },
    "estimate_in_progress_cynthia_stoneham_11706_pierce_court": {
        "event_type": "Estimate",
        "customer_name": "Cynthia Stoneham",
        "address": "11706 Pierce Court",
        "roofix_id": None,
        "tracking_url": PRESENT,
        "parse_complete": False,
    },
}


def _matches(got, want) -> bool:
    """Match one expected value. PRESENT sentinel means 'any truthy value'."""
    if want is PRESENT:
        return bool(got)
    return got == want


def run():
    passed = failed = 0
    for s in SAMPLES:
        ev = parse_email(s).as_dict()
        exp = EXPECTED.get(s["label"], {})
        problems = []
        for k, want in exp.items():
            got = ev.get(k)
            if not _matches(got, want):
                problems.append(f"{k}: expected {want!r}, got {got!r}")
        if problems:
            failed += 1
            print(f"FAIL  {s['label']}")
            for p in problems:
                print(f"        {p}")
        else:
            passed += 1
            print(
                f"ok    {s['label']:56s} -> {ev['event_type']}, "
                f"{ev['customer_name']}, complete={ev['parse_complete']}"
            )
    print(f"\n{passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    raise SystemExit(0 if run() else 1)
