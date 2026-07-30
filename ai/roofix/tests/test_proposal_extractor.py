"""
Tests for components.proposal_extractor.extract_proposal.

Run from ai/roofix/:   PYTHONPATH=. python tests/test_proposal_extractor.py

Fixtures are real captures from Roofix (see tests/fixtures/), pruned to the
doc types the extractor reads and PII-redacted:

  proposal_accepted.json     — Robert Shepherd: HIC executed with signature,
                                Job complete, warranty issued, stage="customer"
  proposal_unaccepted.json   — Gerald kang: no HIC / Job / warranty; homeowner
                                stage="opportunity"; Roofix skips the order1
                                doc in mget for unaccepted proposals, so
                                identity comes from init_data's order1 entry
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from components.proposal_extractor import (
    AcceptanceSignals,
    ExtractedProposal,
    _lookup_id,
    extract_proposal,
)


FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# ── Helper-function tests ─────────────────────────────────────────────────

def _test_lookup_id_strips_bubble_prefix() -> list[str]:
    got = _lookup_id("1348695171700984260__LOOKUP__1775852280409x179578235196227180")
    if got != "1775852280409x179578235196227180":
        return [f"LOOKUP strip: got {got!r}"]
    return []


def _test_lookup_id_edge_cases() -> list[str]:
    problems = []
    if _lookup_id("plain-string") != "plain-string":
        problems.append("non-LOOKUP passthrough failed")
    if _lookup_id(None) is not None:
        problems.append("None input should return None")
    if _lookup_id("") is not None:
        problems.append("empty string should return None")
    if _lookup_id(42) is not None:
        problems.append("non-string input should return None")
    return problems


# ── Accepted proposal ─────────────────────────────────────────────────────

def _test_accepted_extraction() -> list[str]:
    problems = []
    result = extract_proposal(_load("proposal_accepted.json"))

    if not result.ok:
        return [f"accepted: ok=False, error={result.error!r}"]

    # Identity
    checks = {
        "roofix_project_id": "1781297151690x264388044885887520",
        "external_ref": "5YS73T",
        "display_text": "Robert Shepherd - 324 Whitely Street",

        # Customer (from homeowner doc)
        "first_name": "Robert",
        "last_name": "Shepherd",
        "full_name": "Robert Shepherd",
        "email": "customer@example.test",       # PII redacted in fixture
        "phone": "5555550100",                  # PII redacted in fixture

        # Address
        "street_address": "324 Whitely Street",
        "city": "Bridgeport",
        "state": "Ohio",
        "state_abbr": "OH",
        "zip_code": "43912",
        "portal_code": "8K2RXTZVFQ06",

        # Money — proposal side (from order1)
        "estimated_price": 12467.12,
        "staging_price": 8316.42,
        "markup": 1084.75,

        # Money — contract side (from job1) — DIFFERENT from estimated_price
        "actual_contract_price": 11382.37,

        # Config — order1
        "funding_type": "home_improvement_loan",
        "financing_provider": "finsurf",
        "trade": "roofing",
        "project_type": "replacement",
        "steep_slope_product": "owens_corning_oakridge_architectural_shingles",

        # Config — job1
        "job_funding_source": "home_improvement_loan",
        "shingle_color": "1 Black",
        "install_date_ms": 1782964800000,
        "install_scheduled_date_ms": 1782848007617,
        "job_status": "completed",

        # Related Bubble records — id parts
        "sales_rep_ref": "1775852280409x179578235196227180",
        "estimator_ref": "1665082162814x123302301803753570",
        "office_ref": "1739280246960x280095451129448220",
        "homeowner_ref": "1781287165189x930665508813911000",
        "hic_ref": "1781717783470x922006884046100600",
        "job_ref": "1781719439189x310679958652685630",
        "warranty_ref": "1781719439739x905768511679878700",

        # Acceptance
        "is_accepted": True,

        # Progress tracker
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
        "hic_present": True,
        "hic_executed": True,              # status_option_contingency == "executed"
        "hic_signature_present": True,     # signature_url_text is set
        "job_present": True,
        "job_status": "completed",
        "homeowner_stage": "customer",
        "warranty_present": True,
    }
    for k, want in signal_checks.items():
        got = getattr(s, k)
        if got != want:
            problems.append(f"accepted.signals.{k}: expected {want!r}, got {got!r}")

    return problems


# ── Unaccepted proposal ───────────────────────────────────────────────────

def _test_unaccepted_extraction() -> list[str]:
    problems = []
    result = extract_proposal(_load("proposal_unaccepted.json"))

    if not result.ok:
        return [f"unaccepted: ok=False, error={result.error!r}"]

    checks = {
        # Identity — order1 IS present in init_data (Roofix's init/data always
        # emits the current project's order1, even for unaccepted proposals).
        # mget is the one that skips it for unaccepted; init_data doesn't.
        "roofix_project_id": "1784846253605x685099068151907800",
        "external_ref": "21GGPT",
        "display_text": "Gerald kang - 836 Lasser Drive",

        # Customer (from homeowner in mget) — the whole point of the switch
        "first_name": "Gerald",
        "last_name": "kang",
        "full_name": "Gerald kang",
        "street_address": "836 Lasser Drive",
        "city": "Norfolk",
        "state": "Virginia",
        "state_abbr": "VA",
        "zip_code": "23513",

        # Money — proposal side (from order1) — still available
        "estimated_price": 14076.26,
        "staging_price": 11677.33,
        "markup": 1575.16,

        # Config (from order1)
        "funding_type": "cash",
        "financing_provider": None,   # cash flow has no lender
        "trade": "roofing",
        "project_type": "replacement",
        "steep_slope_product": "class_2_shingles",

        # People (from order1)
        "sales_rep_ref": "1773870077554x580778452202950700",
        "estimator_ref": "1669661550637x957567333474137500",
        "office_ref": "1739280246960x280095451129448220",

        # Money — contract side (from job1, which is absent for unaccepted)
        "actual_contract_price": None,

        # Job-side fields — job doc absent
        "job_funding_source": None,
        "shingle_color": None,
        "install_date_ms": None,
        "install_scheduled_date_ms": None,
        "job_status": None,

        # Related record refs — hic/job/warranty absent from mget for unaccepted
        "hic_ref": None,
        "job_ref": None,
        "warranty_ref": None,

        # Acceptance
        "is_accepted": False,

        # Progress tracker (from order1)
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
        "hic_present": False,
        "hic_executed": False,
        "hic_signature_present": False,
        "job_present": False,
        "job_status": None,
        "homeowner_stage": "opportunity",   # ← the differentiator
        "warranty_present": False,
    }
    for k, want in signal_checks.items():
        got = getattr(s, k)
        if got != want:
            problems.append(f"unaccepted.signals.{k}: expected {want!r}, got {got!r}")

    return problems


# ── Edge cases ────────────────────────────────────────────────────────────

def _test_missing_mget_docs() -> list[str]:
    result = extract_proposal({"url": "https://roofix.io/project/x", "mget_docs": []})
    if result.ok:
        return ["empty mget_docs: expected ok=False"]
    if "mget_docs" not in (result.error or ""):
        return [f"empty mget_docs: error should mention mget_docs, got {result.error!r}"]
    return []


def _test_none_input() -> list[str]:
    result = extract_proposal(None)  # type: ignore[arg-type]
    if result.ok:
        return ["None input: expected ok=False"]
    return []


def _test_homeowner_only_no_order1() -> list[str]:
    """If order1 is missing but homeowner exists, extraction still succeeds
    (homeowner is enough to skip the hard-fail branch) but roofix_project_id
    is None — the URL is NOT a trusted identity source."""
    resp = {
        "url": "https://roofix.io/project/1784846253605x685099068151907800",
        "mget_docs": [
            {
                "_id": "hw1",
                "_type": "custom.homeowner",
                "_source": {
                    "_id": "hw1", "_type": "custom.homeowner",
                    "first_name_text": "Jane", "last_name_text": "Doe",
                    "stage_option_type__contact_": "opportunity",
                },
            },
        ],
    }
    result = extract_proposal(resp)
    problems = []
    if not result.ok:
        problems.append(f"homeowner-only: ok=False, error={result.error!r}")
    if result.roofix_project_id is not None:
        problems.append(
            f"homeowner-only project_id: expected None (URL is not a source), "
            f"got {result.roofix_project_id!r}")
    if result.first_name != "Jane":
        problems.append(f"homeowner-only first_name: got {result.first_name!r}")
    if result.is_accepted:
        problems.append("homeowner-only + opportunity stage: is_accepted should be False")
    return problems


def _test_hic_executed_alone_is_accepted() -> list[str]:
    """A hic doc with status=executed is sufficient acceptance, even without
    a job doc or a customer-stage homeowner. (A minimal homeowner doc is
    included so the extractor's identity guard doesn't short-circuit before
    the acceptance logic runs.)"""
    resp = {
        "url": "https://roofix.io/project/1234x5678",
        "mget_docs": [
            {
                "_id": "hw1",
                "_type": "custom.homeowner",
                "_source": {"_id": "hw1", "_type": "custom.homeowner"},
            },
            {
                "_id": "h1",
                "_type": "custom.hic",
                "_source": {
                    "_id": "h1", "_type": "custom.hic",
                    "status_option_contingency": "executed",
                    "signature_url_text": "//sig.example/x.png",
                },
            },
        ],
    }
    result = extract_proposal(resp)
    problems = []
    if not result.is_accepted:
        problems.append("hic-only executed: expected is_accepted=True")
    s = result.acceptance_signals
    if not s.hic_executed:
        problems.append("signals.hic_executed should be True")
    if not s.hic_signature_present:
        problems.append("signals.hic_signature_present should be True")
    return problems


def _test_hic_present_but_not_executed_is_not_accepted() -> list[str]:
    """A hic doc with status != executed does NOT satisfy the primary signal.
    Without a job and without customer-stage homeowner, this is not accepted."""
    resp = {
        "url": "https://roofix.io/project/1234x5678",
        "mget_docs": [
            {
                "_id": "hw1",
                "_type": "custom.homeowner",
                "_source": {"_id": "hw1", "_type": "custom.homeowner"},
            },
            {
                "_id": "h1",
                "_type": "custom.hic",
                "_source": {
                    "_id": "h1", "_type": "custom.hic",
                    "status_option_contingency": "draft",  # not executed
                },
            },
        ],
    }
    result = extract_proposal(resp)
    problems = []
    if result.is_accepted:
        problems.append("hic draft only: expected is_accepted=False")
    s = result.acceptance_signals
    if not s.hic_present:
        problems.append("signals.hic_present should be True (doc exists)")
    if s.hic_executed:
        problems.append("signals.hic_executed should be False (status != executed)")
    return problems


def _test_customer_stage_alone_is_accepted() -> list[str]:
    """Roofix classifies homeowner as 'customer' — that's enough to consider
    accepted even without hic/job present in this scrape."""
    resp = {
        "url": "https://roofix.io/project/1234x5678",
        "mget_docs": [
            {
                "_id": "hw1",
                "_type": "custom.homeowner",
                "_source": {
                    "_id": "hw1", "_type": "custom.homeowner",
                    "stage_option_type__contact_": "customer",
                },
            }
        ],
    }
    result = extract_proposal(resp)
    if not result.is_accepted:
        return ["customer-stage: expected is_accepted=True"]
    return []


def _test_job_status_alone_is_accepted() -> list[str]:
    resp = {
        "url": "https://roofix.io/project/1234x5678",
        "mget_docs": [
            {
                "_id": "hw1",
                "_type": "custom.homeowner",
                "_source": {"_id": "hw1", "_type": "custom.homeowner"},
            },
            {
                "_id": "j1",
                "_type": "custom.job1",
                "_source": {
                    "_id": "j1", "_type": "custom.job1",
                    "status_option_job_status": "in_progress",
                },
            },
        ],
    }
    result = extract_proposal(resp)
    if not result.is_accepted:
        return ["job-only: expected is_accepted=True"]
    return []


# ── Runner ─────────────────────────────────────────────────────────────────

_CASES = [
    ("lookup_id: strips prefix", _test_lookup_id_strips_bubble_prefix),
    ("lookup_id: edge cases", _test_lookup_id_edge_cases),
    ("extract: accepted proposal (Robert Shepherd)", _test_accepted_extraction),
    ("extract: unaccepted proposal (Gerald kang)", _test_unaccepted_extraction),
    ("extract: empty mget_docs", _test_missing_mget_docs),
    ("extract: None input", _test_none_input),
    ("identity: homeowner only, no order1 -> project_id=None", _test_homeowner_only_no_order1),
    ("acceptance: hic executed alone", _test_hic_executed_alone_is_accepted),
    ("acceptance: hic draft alone is NOT accepted", _test_hic_present_but_not_executed_is_not_accepted),
    ("acceptance: customer-stage alone", _test_customer_stage_alone_is_accepted),
    ("acceptance: job status alone", _test_job_status_alone_is_accepted),
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
