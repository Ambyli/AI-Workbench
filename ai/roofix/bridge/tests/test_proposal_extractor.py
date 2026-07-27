"""
Tests for components.proposal_extractor.extract_proposal.

Run from ai/roofix/bridge/:   PYTHONPATH=. python tests/test_proposal_extractor.py

Fixtures are real captures from Roofix (see tests/fixtures/):
    proposal_accepted.json      Robert Shepherd — HIC signed, Job created,
                                warranty issued, project fully complete
    proposal_unaccepted.json    Gerald kang — estimate exists but no HIC has
                                been sent to homeowner yet
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from components.proposal_extractor import (
    AcceptanceSignals,
    ExtractedProposal,
    _lookup_id,
    _split_display_text,
    extract_proposal,
)


FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# ── Helper-function tests ─────────────────────────────────────────────────

def _test_lookup_id_strips_bubble_prefix() -> list[str]:
    problems = []
    got = _lookup_id("1348695171700984260__LOOKUP__1775852280409x179578235196227180")
    if got != "1775852280409x179578235196227180":
        problems.append(f"LOOKUP strip: got {got!r}")
    return problems


def _test_lookup_id_passthrough_non_lookup() -> list[str]:
    problems = []
    if _lookup_id("plain-string") != "plain-string":
        problems.append("non-LOOKUP string should pass through unchanged")
    return problems


def _test_lookup_id_none() -> list[str]:
    problems = []
    if _lookup_id(None) is not None:
        problems.append("None input should return None")
    if _lookup_id("") is not None:
        problems.append("empty string should return None")
    if _lookup_id(42) is not None:
        problems.append("non-string input should return None")
    return problems


def _test_split_display_text() -> list[str]:
    problems = []
    n, a = _split_display_text("Robert Shepherd - 324 Whitely Street")
    if n != "Robert Shepherd" or a != "324 Whitely Street":
        problems.append(f"simple split: got ({n!r}, {a!r})")

    # Address containing ' - ' — split-on-first-only means address survives.
    n, a = _split_display_text("Jane Doe - 100-B Main St - Unit 3")
    if n != "Jane Doe" or a != "100-B Main St - Unit 3":
        problems.append(f"multi-hyphen split: got ({n!r}, {a!r})")

    n, a = _split_display_text("NoSeparator")
    if n != "NoSeparator" or a is not None:
        problems.append(f"no separator: got ({n!r}, {a!r})")

    n, a = _split_display_text("")
    if n is not None or a is not None:
        problems.append(f"empty: got ({n!r}, {a!r})")

    n, a = _split_display_text(None)
    if n is not None or a is not None:
        problems.append(f"None: got ({n!r}, {a!r})")

    return problems


# ── Fixture-based extraction tests ────────────────────────────────────────

def _test_accepted_proposal_extraction() -> list[str]:
    problems = []
    result = extract_proposal(_load("proposal_accepted.json"))

    if not result.ok:
        return [f"accepted: ok=False, error={result.error!r}"]

    checks = {
        "roofix_project_id": "1781297151690x264388044885887520",
        "external_ref": "5YS73T",
        "display_text": "Robert Shepherd - 324 Whitely Street",
        "customer_name": "Robert Shepherd",
        "address": "324 Whitely Street",
        "contract_price": 12467.12,
        "staging_price": 8316.42,
        "markup": 1084.75,
        "funding_type": "home_improvement_loan",
        "financing_provider": "finsurf",
        "trade": "roofing",
        "project_type": "replacement",
        "steep_slope_product": "owens_corning_oakridge_architectural_shingles",
        "sales_rep_ref": "1775852280409x179578235196227180",
        "estimator_ref": "1665082162814x123302301803753570",
        "office_ref": "1739280246960x280095451129448220",
        "homeowner_ref": "1781287165189x930665508813911000",
        "hic_ref": "1781717783470x922006884046100600",
        "job_ref": "1781719439189x310679958652685630",
        "warranty_ref": "1781719439739x905768511679878700",
        "is_accepted": True,
        "stage_completed_internal": "job_moved_to_complete",
        "stage_upcoming_internal": "warranty_documents_sent_to_homeowner",
        "stage_completed_external": "job_moved_to_complete",
    }
    for k, want in checks.items():
        got = getattr(result, k)
        if got != want:
            problems.append(f"accepted.{k}: expected {want!r}, got {got!r}")

    s = result.acceptance_signals
    signal_checks = {
        "has_hic": True,
        "has_job": True,
        "has_warranty": True,
        "loan_signed": True,
        "ntp_received": True,
        "msa_count": 4,
    }
    for k, want in signal_checks.items():
        got = getattr(s, k)
        if got != want:
            problems.append(f"accepted.signals.{k}: expected {want!r}, got {got!r}")

    return problems


def _test_unaccepted_proposal_extraction() -> list[str]:
    problems = []
    result = extract_proposal(_load("proposal_unaccepted.json"))

    if not result.ok:
        return [f"unaccepted: ok=False, error={result.error!r}"]

    checks = {
        "roofix_project_id": "1784846253605x685099068151907800",
        "external_ref": "21GGPT",
        "display_text": "Gerald kang - 836 Lasser Drive",
        "customer_name": "Gerald kang",
        "address": "836 Lasser Drive",
        "contract_price": 14076.26,
        "staging_price": 11677.33,
        "markup": 1575.16,
        "funding_type": "cash",
        "financing_provider": None,      # no lender in cash flow
        "trade": "roofing",
        "project_type": "replacement",
        "sales_rep_ref": "1773870077554x580778452202950700",
        "estimator_ref": "1669661550637x957567333474137500",
        "office_ref": "1739280246960x280095451129448220",
        "hic_ref": None,                  # ← core signal: no HIC
        "job_ref": None,                  # ← core signal: no Job
        "warranty_ref": None,             # ← post-completion, not applicable
        "is_accepted": False,             # ← THE assertion
        "stage_completed_internal": "select_funding_type",
        "stage_upcoming_internal": "hic_sent_to_homeowner",
        "stage_completed_external": "select_funding_type",
    }
    for k, want in checks.items():
        got = getattr(result, k)
        if got != want:
            problems.append(f"unaccepted.{k}: expected {want!r}, got {got!r}")

    s = result.acceptance_signals
    signal_checks = {
        "has_hic": False,
        "has_job": False,
        "has_warranty": False,
        "loan_signed": False,
        "ntp_received": False,
        "msa_count": 0,
    }
    for k, want in signal_checks.items():
        got = getattr(s, k)
        if got != want:
            problems.append(f"unaccepted.signals.{k}: expected {want!r}, got {got!r}")

    return problems


# ── Edge-case tests ───────────────────────────────────────────────────────

def _test_missing_init_data() -> list[str]:
    result = extract_proposal({"url": "https://roofix.io/project/x", "init_data": []})
    if result.ok:
        return ["empty init_data: expected ok=False"]
    if "init_data" not in (result.error or ""):
        return [f"empty init_data: error should mention init_data, got {result.error!r}"]
    return []


def _test_no_order_doc() -> list[str]:
    """User doc present but no custom.order1 — treat as extraction failure."""
    resp = {
        "url": "x",
        "init_data": [
            {"id": "u1", "type": "user", "version": 1, "data": {"_id": "u1"}},
        ],
    }
    result = extract_proposal(resp)
    if result.ok:
        return ["no order doc: expected ok=False"]
    if "custom.order1" not in (result.error or ""):
        return [f"no order doc: error should mention custom.order1, got {result.error!r}"]
    return []


def _test_none_input() -> list[str]:
    result = extract_proposal(None)  # type: ignore[arg-type]
    if result.ok:
        return ["None input: expected ok=False"]
    return []


def _test_hic_only_still_accepted() -> list[str]:
    """A proposal where HIC exists but Job hasn't been created yet: still accepted."""
    resp = {
        "init_data": [
            {
                "id": "o1",
                "type": "custom.order1",
                "data": {
                    "_id": "o1",
                    "display_text": "Test - 1 Main St",
                    "hic_custom_hic": "org__LOOKUP__hic_id_xyz",
                    # No job_custom_job1
                },
            }
        ]
    }
    result = extract_proposal(resp)
    problems = []
    if not result.is_accepted:
        problems.append("HIC-only proposal should be accepted")
    if not result.acceptance_signals.has_hic:
        problems.append("has_hic should be True")
    if result.acceptance_signals.has_job:
        problems.append("has_job should be False when job field absent")
    return problems


def _test_job_only_still_accepted() -> list[str]:
    """Hypothetical: Job exists without HIC. Still counts as accepted (edge case)."""
    resp = {
        "init_data": [
            {
                "id": "o1",
                "type": "custom.order1",
                "data": {
                    "_id": "o1",
                    "display_text": "Test - 1 Main St",
                    "job_custom_job1": "org__LOOKUP__job_id_xyz",
                },
            }
        ]
    }
    result = extract_proposal(resp)
    if not result.is_accepted:
        return ["Job-only proposal should still be accepted"]
    return []


# ── Runner ─────────────────────────────────────────────────────────────────

_CASES = [
    ("lookup_id: strips prefix", _test_lookup_id_strips_bubble_prefix),
    ("lookup_id: non-LOOKUP passthrough", _test_lookup_id_passthrough_non_lookup),
    ("lookup_id: None / empty / non-string", _test_lookup_id_none),
    ("split_display_text: various cases", _test_split_display_text),
    ("extract: accepted proposal", _test_accepted_proposal_extraction),
    ("extract: unaccepted proposal", _test_unaccepted_proposal_extraction),
    ("extract: empty init_data", _test_missing_init_data),
    ("extract: no order doc", _test_no_order_doc),
    ("extract: None input", _test_none_input),
    ("acceptance: HIC-only is accepted", _test_hic_only_still_accepted),
    ("acceptance: Job-only is accepted", _test_job_only_still_accepted),
]


def run() -> bool:
    passed = failed = 0
    for label, fn in _CASES:
        problems = fn()
        if problems:
            failed += 1
            print(f"FAIL  {label}")
            for p in problems:
                print(f"        {p}")
        else:
            passed += 1
            print(f"ok    {label}")
    print(f"\n{passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
