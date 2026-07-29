"""
Read-only diagnostic: fetch recent 'RFX | Estimate' emails from Gmail, run
each through parse_email, and print both the raw Contract A dict AND the
parsed result side-by-side.

Purpose: hand-verify the parser against real emails before we codify them
into roofix_email_samples.py + test_parser.py. Not a pytest test (named
verify_* so pytest skips it), does NOT mark anything read, does NOT write
anything to disk.

Run from ai/roofix/:
    PYTHONPATH=. uv run --package roofix python tests/verify_estimate_parser.py

Optional args:
    --limit N       max emails to inspect (default 5)
    --query "..."   override the default Estimate-search query
    --emit-python   also print a Python dict literal ready to paste into
                    roofix_email_samples.py (default: on)
"""

from __future__ import annotations

import argparse
import json
import pprint
import sys
from typing import Any

from common.env import load_env

load_env()

from components.gmail_client import GmailClient
from components.parser import parse_email


DEFAULT_QUERY = 'from:no-reply@roofix.io subject:Estimate newer_than:30d'


_CONTRACT_A_FIELDS = (
    "sender", "to", "subject", "body_text", "body_html",
    "timestamp", "attachments",
)


def _to_python_literal(email: dict, label: str) -> str:
    """Render the full Contract A dict as a Python literal we can paste into
    the SAMPLES list in roofix_email_samples.py.

    Includes `body_html` on purpose — that's where the tokenized tracking
    URL lives (Estimate emails' plain-text body drops the href), so the
    parser's tracking_url extraction only works if the fixture retains it.
    """
    entry: dict[str, Any] = {"label": label}
    for k in _CONTRACT_A_FIELDS:
        if k in email and email[k] not in (None, "", []):
            entry[k] = email[k]
    # Force a wide width so a long body_html doesn't wrap into an unreadable
    # mess. Long strings will still be rendered as one giant string literal.
    return pprint.pformat(entry, width=120, sort_dicts=False)


def _slugify(subject: str) -> str:
    """Produce a snake_case label from the subject for the sample dict."""
    keep = "".join(c.lower() if c.isalnum() else "_" for c in subject or "")
    return "_".join(w for w in keep.split("_") if w)[:60] or "estimate_email"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--query", default=DEFAULT_QUERY)
    ap.add_argument("--emit-python", action=argparse.BooleanOptionalAction, default=True)
    args = ap.parse_args()

    print(f"query: {args.query!r}\nlimit: {args.limit}\n")

    gc = GmailClient()
    emails = gc.fetch(max_results=args.limit, query=args.query)
    print(f"fetched {len(emails)} email(s)\n")

    if not emails:
        print("Nothing came back. Try --query to widen the search, e.g.")
        print("  --query 'from:no-reply@roofix.io subject:Estimate'")
        print("  --query 'from:no-reply@roofix.io newer_than:90d'")
        return 0

    for i, raw in enumerate(emails, 1):
        parsed = parse_email(raw).as_dict()
        label = _slugify(raw.get("subject", "") or f"estimate_{i}")

        print("=" * 78)
        print(f"[{i}/{len(emails)}] {label}")
        print("=" * 78)
        html = raw.get("body_html") or ""
        print("── Raw (Contract A) ──")
        print(f"  sender:    {raw.get('sender','')}")
        print(f"  subject:   {raw.get('subject','')}")
        print(f"  to:        {raw.get('to','')}")
        print(f"  timestamp: {raw.get('timestamp','')}")
        print(f"  body_text: {(raw.get('body_text','') or '')[:220]}...")
        print(f"  body_html: {'<present, ' + str(len(html)) + ' chars>' if html else '<absent>'}")

        print("\n── Parsed (ParsedEvent) ──")
        print(f"  event_type:     {parsed['event_type']}")
        print(f"  project_id:     {parsed['project_id']}")
        print(f"  tracking_url:   {parsed['tracking_url']}")
        print(f"  customer_name:  {parsed['customer_name']}")
        print(f"  address:        {parsed['address']}")
        print(f"  address_suffix: {parsed['address_suffix']}")
        print(f"  parse_complete: {parsed['parse_complete']}")
        print(f"  notes:          {parsed['notes']}")

        if args.emit_python:
            print("\n── Copy-paste into tests/roofix_email_samples.py's SAMPLES list ──")
            print(_to_python_literal(raw, label) + ",")

        print()

    print("=" * 78)
    print("Verify each parsed block against what you'd expect. If any field is")
    print("wrong (parser regression), tell me which sample + which field.")
    print("If they all look right, we'll paste them into roofix_email_samples.py")
    print("and add matching expected-value entries in test_parser.py's EXPECTED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
