"""Seed the email store with a few sample handoff emails so the Emails tab is
populated on first run. Rendered through the SAME template as real cases, so the
demo emails are consistent with live ones. Only runs when the store is empty.
"""
from __future__ import annotations

from . import email_render
from .lookup import lookup

# (case, created_at) pairs, oldest first (store reverses for newest-first display).
_SEEDS: list[tuple[dict, str]] = [
    (
        {
            "mode": "lookup", "name": "Sam Sample", "address": "400 Battery Ct, Sarasota, FL 34236",
            "email": "sam.sample@example.com", "issue_type": "misc", "urgency": 5,
            "what_damaged": "Backyard ground-mount array frame",
            "location": "South fence line",
            "additional_details": "A falling branch bent two panel brackets",
            "attachments": ["branch_damage.jpg"],
            "verbatim": ["A big branch came down in the storm and bent the frame on two of the ground panels out back."],
        },
        "2026-06-18T09:21:00+00:00",
    ),
    (
        {
            "mode": "standard", "name": "Jordan Lee", "address": "500 Electric Ave, Lakeland, FL 33801",
            "email": "jordan.lee@example.com", "issue_type": "solar", "urgency": 3,
            "callback_requested": True, "callback_info": "863-555-0105, evenings",
            "attachments": [],
            "verbatim": ["My production has been noticeably lower than last summer for the past couple of weeks. Wanted to flag it."],
        },
        "2026-06-18T16:55:00+00:00",
    ),
    (
        {
            "mode": "lookup", "name": "John Smith", "address": "200 Sunny Ln, Clearwater, FL 33755",
            "email": "john.smith@example.com", "issue_type": "electrical", "urgency": 9,
            "without_power": True, "breakers_tripped": True, "affected_areas": "Whole home",
            "attachments": [],
            "verbatim": ["Half the house went dark right after the panel made a loud popping sound. None of the breakers will reset."],
        },
        "2026-06-19T11:08:00+00:00",
    ),
    (
        {
            "mode": "lookup", "name": "Jane Doe", "address": "100 Solar Way, Tampa, FL 33601",
            "email": "jane.doe@example.com", "issue_type": "roof", "urgency": 8,
            "third_parties": "Filed a claim with insurance yesterday",
            "leaking": True, "first_noticed": "Last night during the storm",
            "pre_existing": False, "attic_accessible": True,
            "location": "Front-left slope near the chimney",
            "attachments": ["roof_chimney_1.jpg", "ceiling_stain.jpg"],
            "verbatim": ["Water is dripping into the upstairs hallway whenever it rains hard. There's a brown stain spreading across the ceiling and I'm worried about the drywall."],
        },
        "2026-06-19T13:42:00+00:00",
    ),
]


def seed_if_empty() -> None:
    if not email_render.is_empty():
        return
    for case, created_at in _SEEDS:
        match = None
        if case["mode"] == "lookup":
            match = lookup(case.get("name"), case.get("address"), case.get("email"))
        email_render.render_and_store(case, match, created_at=created_at)
