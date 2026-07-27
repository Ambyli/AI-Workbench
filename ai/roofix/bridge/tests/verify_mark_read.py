"""
Interactive smoke test for GmailClient.mark_read().

Not a pytest test — this hits real Gmail and mutates state. Named
``verify_*`` (not ``test_*``) so pytest doesn't collect it.

Flow:
    1. Fetch unread messages matching LISTENER_QUERY.
    2. Show the first one and ask for [y/N] confirmation.
    3. Mark it read.
    4. Re-fetch and confirm the same message_id is gone from the unread queue.

Real Gmail account, real state change — that's why the confirmation step is
required. Only affects ONE message per run (the oldest returned by fetch).

Run from ai/roofix/bridge/:
    PYTHONPATH=. uv run --package roofix-bridge python tests/verify_mark_read.py
"""

from __future__ import annotations

import sys

from common.env import load_env

load_env()

from components.gmail_client import GmailClient, QUERY


def _fmt(email: dict) -> str:
    return (
        f"    id={email['message_id'][:16]}...  "
        f"from={email['sender'][:32]:32s}  "
        f"subject={email['subject'][:60]}"
    )


def main() -> int:
    print(f"query: {QUERY!r}\n")
    gc = GmailClient()

    print("── Fetch #1 (before) ──")
    before = gc.fetch()
    print(f"unread count: {len(before)}")
    for e in before[:10]:
        print(_fmt(e))
    if not before:
        print("\nNothing to mark. Exit.")
        return 0

    target = before[0]
    print("\n── Target ──")
    print(f"  message_id: {target['message_id']}")
    print(f"  from:       {target['sender']}")
    print(f"  subject:    {target['subject']}")
    print(f"  timestamp:  {target['timestamp']}")

    reply = input("\nMark this message read? [y/N] ").strip().lower()
    if reply != "y":
        print("Aborted — no changes made.")
        return 1

    print(f"\nmark_read({target['message_id']!r}) ...")
    gc.mark_read(target["message_id"])
    print("done.")

    print("\n── Fetch #2 (after) ──")
    after = gc.fetch()
    print(f"unread count: {len(after)}")
    for e in after[:10]:
        print(_fmt(e))

    still_present = any(e["message_id"] == target["message_id"] for e in after)
    if still_present:
        print(f"\nFAIL: {target['message_id']} is still in the unread queue.")
        return 2

    print(
        f"\nPASS: {target['message_id']} no longer in unread queue "
        f"(count {len(before)} → {len(after)})."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
