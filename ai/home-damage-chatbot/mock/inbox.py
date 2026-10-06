"""Show what staff would receive for the mock's submitted requests.

The mock runs with the staff API OFF (like production), so testers can't open the
Inbox page. This reads the account service's data file instead (mock/.data/account/emails.json):
which account matched, the case id, and where the handoff email was routed.
Nothing is ever sent; email delivery is forced off in the mock.

Run:  python mock/inbox.py [count]
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

DATA = Path(__file__).resolve().parent / ".data" / "account" / "emails.json"  # the account service owns staff records


def main() -> int:
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    if not DATA.is_file():
        print("No submitted requests yet (mock/.data/account/emails.json not found).")
        return 0
    records = json.loads(DATA.read_text(encoding="utf-8"))
    if not records:
        print("No submitted requests yet.")
        return 0
    for rec in records[-count:][::-1]:
        html = rec.get("html", "")
        acct = re.search(r"(\d{6}) \(Verified Match\)", html)
        closest = re.findall(r"#(\d{6}) \(address ([\d.]+), name ([\d.]+)\)", html)
        print(f"{rec.get('created_at', '')}  {rec.get('subject', '')}")
        if rec.get("matched"):
            print(f"    Account: MATCHED project {acct.group(1) if acct else '?'} "
                  f"(confidence {rec.get('match_confidence', 0):.2f}), case {rec.get('case_id')}")
        else:
            hint = "; ".join(f"#{p} addr {a} / name {n}" for p, a, n in closest) or "none"
            print(f"    Account: NOT FOUND. Closest projects for staff: {hint}")
        print(f"    Routed to: {rec.get('routed_to') or rec.get('to')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
