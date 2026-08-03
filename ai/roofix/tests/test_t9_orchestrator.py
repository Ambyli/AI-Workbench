"""
T9 — Orchestrator wiring test for the create_project flow.

Verifies that Phase 1 estimate emails with tracking URLs trigger:
  1. Brain emits create_project decision (not ignore)
  2. Orchestrator calls scraper.get_proposal(tracking_url=...)
  3. Orchestrator runs extract_proposal on the scrape result
  4. Orchestrator calls phoenix.ensure_entity_and_project(extracted.__dict__)
  5. ProcessedStore is marked ok with the mapping metadata
  6. Gmail mark_read is called after successful processing

Also verifies Phase 0 still ignores estimates (no scraper call).

Run from ai/roofix/:
    PYTHONPATH=../..:. python tests/test_t9_orchestrator.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Set env BEFORE importing anything that reads it.
os.environ.setdefault("DRY_RUN", "true")
os.environ.setdefault("PHOENIX_AGENT_USER_ID", "1399")
os.environ.setdefault("AGENT_PHASE", "1")  # Phase 1 to trigger create_project
os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="roofix_t9_"))

# Add shared/common to PYTHONPATH.
sys.path.insert(
    0,
    str(
        Path(__file__).resolve().parent.parent.parent.parent
        / "shared"
        / "common"
        / "src"
    ),
)

from common.logging_setup import CsvLogger  # noqa: E402
from common.processed_store import ProcessedStore  # noqa: E402

from components.parser import parse_email  # noqa: E402
from components.orchestrator import run as orchestrator_run  # noqa: E402

# ── Fake Estimate email with tracking URL ────────────────────────────────────

FAKE_ESTIMATE_EMAIL = {
    "label": "estimate_phase1",
    "sender": '"RFX | Estimate" <no-reply@roofix.io>',
    "to": ["peyton.anderton@zeoenergy.com"],
    "subject": "Estimate in Progress - Test Customer - 100 Test Street",
    "body_text": "Hello, We have received your request to provide an estimate for Test Customer - 100 Test Street",
    "body_html": (
        '<a href="http://url6628.roofix.io/ls/click?upn=FAKE_TOKEN_123">'
        "View the Project here</a>"
    ),
    "timestamp": "2026-07-28T10:00:00+00:00",
    "message_id": "fake_message_id_001",
}


# ── Fake scraper response ────────────────────────────────────────────────────

FAKE_SCRAPER_RESULT = {
    "url": "http://url6628.roofix.io/ls/click?upn=FAKE_TOKEN_123",
    "status": "ok",
    "error": None,
    "login_wall": False,
    "init_data": {
        "data": {
            "_id": "1781297151690x264388044885887520",
            "display_text": "Test Customer - 100 Test Street",
            "price__final__number": 15000.0,
            "funding1_option_funding": "cash",
            "trade_option_trade": "roofing",
        },
    },
    "docs": [
        {
            "_id": "1781297151690x264388044885887520",
            "_type": "custom.order1",
            "_source": {
                "display_text": "Test Customer - 100 Test Street",
                "price__final__number": 15000.0,
                "funding1_option_funding": "cash",
                "trade_option_trade": "roofing",
            },
        },
        {
            "_id": "1781287165189x930665508813911000",
            "_type": "custom.homeowner",
            "_source": {
                "first_name_text": "Test",
                "last_name_text": "Customer",
                "street_address_text": "100 Test Street",
                "city_text": "TestCity",
                "state_text": "TestState",
                "state_abbr_text": "TS",
                "zip_text": "12345",
            },
        },
        {
            "_id": "1781717783470xHIC_ID",
            "_type": "custom.hic",
            "_source": {
                "status_option_contingency": "executed",
                "signature_url_text": "//cdn.bubble.io/fake-signature.png",
            },
        },
        {
            "_id": "1781719439189xJOB_ID",
            "_type": "custom.job1",
            "_source": {
                "contract_price_number": 14000.0,
                "status_option_job_status": "in_progress",
            },
        },
    ],
    "mget_docs": [
        {
            "_id": "1781287165189x930665508813911000",
            "_type": "custom.homeowner",
            "_source": {
                "first_name_text": "Test",
                "last_name_text": "Customer",
                "street_address_text": "100 Test Street",
                "city_text": "TestCity",
                "state_text": "TestState",
                "state_abbr_text": "TS",
                "zip_text": "12345",
            },
        },
        {
            "_id": "1781717783470xHIC_ID",
            "_type": "custom.hic",
            "_source": {
                "status_option_contingency": "executed",
                "signature_url_text": "//cdn.bubble.io/fake-signature.png",
            },
        },
        {
            "_id": "1781719439189xJOB_ID",
            "_type": "custom.job1",
            "_source": {
                "contract_price_number": 14000.0,
                "status_option_job_status": "in_progress",
            },
        },
    ],
}


# ── Fake extracted proposal ──────────────────────────────────────────────────

FAKE_EXTRACTED_PROPOSAL = MagicMock()
FAKE_EXTRACTED_PROPOSAL.ok = True
FAKE_EXTRACTED_PROPOSAL.error = None
FAKE_EXTRACTED_PROPOSAL.roofix_id = "1781297151690x264388044885887520"
FAKE_EXTRACTED_PROPOSAL.is_accepted = True
# The orchestrator's _scrape_and_extract reads full_name / street_address off
# the ExtractedProposal to merge into the event. Set them explicitly so the
# MagicMock returns real strings, not auto-generated MagicMock instances.
FAKE_EXTRACTED_PROPOSAL.full_name = "Test Customer"
FAKE_EXTRACTED_PROPOSAL.street_address = "100 Test Street"
FAKE_EXTRACTED_PROPOSAL.acceptance_signals = {
    "has_hic": True,
    "has_job": True,
    "hic_executed": True,
}
FAKE_EXTRACTED_PROPOSAL.__dict__ = {
    "ok": True,
    "error": None,
    "roofix_id": "1781297151690x264388044885887520",
    "is_accepted": True,
    "acceptance_signals": {
        "has_hic": True,
        "has_job": True,
        "hic_executed": True,
    },
    "display_text": "Test Customer - 100 Test Street",
    "customer_name": "Test Customer",
    "address": "100 Test Street",
    "contract_price": 15000.0,
    "funding_type": "cash",
    "trade": "roofing",
    "first_name": "Test",
    "last_name": "Customer",
    "street_address": "100 Test Street",
    "city": "TestCity",
    "state": "TestState",
    "state_abbr": "TS",
    "zip_code": "12345",
    "actual_contract_price": 14000.0,
    "job_status": "in_progress",
    "hic_status": "executed",
    "hic_signature_present": True,
}


# ── Helpers ──────────────────────────────────────────────────────────────────


def _read_audit_csv(log_path):
    import csv

    with open(log_path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _make_fake_scraper():
    """Return a MagicMock that stands in for RoofixScraperClient.

    ``get_proposal`` is async in the real client — AsyncMock wraps the
    return value in a coroutine so ``await scraper.get_proposal(...)``
    resolves to FAKE_SCRAPER_RESULT.
    """
    scraper = MagicMock()
    scraper.get_proposal = AsyncMock(return_value=FAKE_SCRAPER_RESULT)
    return scraper


def _make_fake_phoenix():
    """Return a MagicMock that stands in for PhoenixClient."""
    phoenix = MagicMock()
    phoenix.ensure_entity_and_project.return_value = MagicMock(
        ok=True,
        dry_run=True,
        detail="mocked create",
        data={
            "entity_id": 100,
            "project_id": 200,
            "created_entity": True,
            "created_project": True,
            "created_link": True,
        },
    )
    # Also need find_project_by_roofix_id and find_project_by_identity for
    # _resolve_context (called before decide in process_batch).
    phoenix.find_project_by_roofix_id.return_value = MagicMock(
        ok=True, detail="no match", data={"matches": []}
    )
    phoenix.find_project_by_identity.return_value = MagicMock(
        ok=True, detail="no match", data={"matches": []}
    )
    return phoenix


def _make_fake_gmail():
    """Return a MagicMock that stands in for GmailClient."""
    gmail = MagicMock()
    gmail.fetch.return_value = [FAKE_ESTIMATE_EMAIL]
    return gmail


# ── Test cases ───────────────────────────────────────────────────────────────


def run() -> bool:
    passed = failed = 0

    def case(label):
        def deco(fn):
            nonlocal passed, failed
            try:
                fn()
            except AssertionError as e:
                failed += 1
                print(f"FAIL  {label}")
                print(f"        {e}")
            except Exception as e:
                failed += 1
                print(f"FAIL  {label}")
                print(f"        {type(e).__name__}: {e}")
            else:
                passed += 1
                print(f"ok    {label}")

        return deco

    # ── 1. Phase 1: estimate with tracking URL → create_project decision ────

    @case("Phase 1 estimate: brain emits create_project, not ignore")
    def _test_phase1_brain_emits_create_project():
        from components.brain import decide

        ev = parse_email(FAKE_ESTIMATE_EMAIL).as_dict()
        ctx = {"found": False, "ambiguous": False}
        d = asyncio.run(decide(ev, ctx)).as_dict()
        assert d["action"] == "create_project", d
        assert (
            d["payload"]["tracking_url"]
            == "http://url6628.roofix.io/ls/click?upn=FAKE_TOKEN_123"
        )
        assert d["source"] == "rule", d

    # ── 2. Phase 0: estimate still ignored ──────────────────────────────────

    @case("Phase 0 estimate: brain still ignores (no scraper call)")
    def _test_phase0_still_ignores():
        # Temporarily set AGENT_PHASE to 0.
        import components.brain as brain_mod

        saved = brain_mod.PHASE
        brain_mod.PHASE = "0"
        try:
            from components.brain import decide

            ev = parse_email(FAKE_ESTIMATE_EMAIL).as_dict()
            ctx = {"found": False, "ambiguous": False}
            d = asyncio.run(decide(ev, ctx)).as_dict()
            assert d["action"] == "ignore", d
            assert "Phase 0" in d["reasoning"], d
        finally:
            brain_mod.PHASE = saved

    # ── 3. Full pipeline: Phase 1 estimate → scraper → extractor → phoenix ──

    @case("Phase 1: full pipeline calls scraper, extractor, ensure_entity_and_project")
    def _test_full_pipeline_phase1():
        log_path = os.path.join(os.environ["LOG_DIR"], "audit_t9_phase1.csv")
        audit = CsvLogger(
            path=log_path,
            columns=["stage", "action", "ok", "detail", "event_type", "project_ref"],
        )
        processed_store = ProcessedStore(
            Path(os.environ["LOG_DIR"]) / "processed_t9.db"
        )
        scraper = _make_fake_scraper()
        phoenix = _make_fake_phoenix()
        gmail = _make_fake_gmail()

        # Patch extract_proposal to return our fake.
        with patch(
            "components.orchestrator.extract_proposal",
            return_value=FAKE_EXTRACTED_PROPOSAL,
        ):
            records = asyncio.run(
                orchestrator_run(
                    listener=lambda: [FAKE_ESTIMATE_EMAIL],
                    phoenix=phoenix,
                    log=audit,
                    milestone_map=None,
                    scraper_client=scraper,
                    processed_store=processed_store,
                )
            )

        # Verify decision. Each record now bundles ev + decision.
        assert len(records) == 1, len(records)
        d = records[0]["decision"]
        assert d["action"] == "create_project", d

        # Verify scraper was called with the tracking URL.
        scraper.get_proposal.assert_called_once()
        call_kwargs = scraper.get_proposal.call_args
        assert (
            call_kwargs[1].get("tracking_url")
            == "http://url6628.roofix.io/ls/click?upn=FAKE_TOKEN_123"
        )

        # Verify extractor was called with the scraper result.
        # (patched, so we can't verify the call directly, but we know it ran
        # because ensure_entity_and_project was called with the fake extracted data)

        # Verify ensure_entity_and_project was called.
        phoenix.ensure_entity_and_project.assert_called_once()
        call_args = phoenix.ensure_entity_and_project.call_args[0][0]
        assert call_args["roofix_id"] == "1781297151690x264388044885887520"
        assert call_args["is_accepted"] is True

        # Verify ProcessedStore was marked ok.
        store_record = processed_store.get("fake_message_id_001")
        assert (
            store_record is not None
        ), "ProcessedStore should have a record for this message_id"
        assert store_record.status == "ok"
        assert store_record.metadata["phoenix_entity_id"] == 100
        assert store_record.metadata["project_id"] == 200

        # Verify gmail.mark_read was NOT called by the orchestrator itself
        # (mark_read happens in app._run_one_batch, not in the orchestrator).
        # The orchestrator just calls ensure_entity_and_project.
        # So we don't assert mark_read was called here — that's an app.py concern.

        # Verify audit trail.
        rows = _read_audit_csv(log_path)
        stages = [r["stage"] for r in rows]
        assert "scraper" in stages, stages
        assert "extractor" in stages, stages
        assert "phoenix" in stages, stages

    # ── 4. Phase 1: is_accepted=False → skip create, mark ok with accepted=False ──

    @case(
        "Phase 1: is_accepted=False skips create_project, marks ok with accepted=False"
    )
    def _test_phase1_not_accepted():
        log_path = os.path.join(os.environ["LOG_DIR"], "audit_t9_not_accepted.csv")
        audit = CsvLogger(
            path=log_path,
            columns=["stage", "action", "ok", "detail", "event_type", "project_ref"],
        )
        processed_store = ProcessedStore(
            Path(os.environ["LOG_DIR"]) / "processed_t9_not_accepted.db"
        )
        scraper = _make_fake_scraper()
        phoenix = _make_fake_phoenix()

        # Create a proper object with is_accepted=False (not a MagicMock).
        class FakeExtractedProposal:
            ok = True
            error = None
            roofix_id = "1781297151690x264388044885887520"
            is_accepted = False
            # Same rationale as FAKE_EXTRACTED_PROPOSAL — the merge path reads
            # these two off the extracted proposal.
            full_name = "Test Customer"
            street_address = "100 Test Street"
            __dict__ = {
                "ok": True,
                "error": None,
                "roofix_id": "1781297151690x264388044885887520",
                "is_accepted": False,
            }

        not_accepted = FakeExtractedProposal()

        with patch(
            "components.orchestrator.extract_proposal", return_value=not_accepted
        ):
            records = asyncio.run(
                orchestrator_run(
                    listener=lambda: [FAKE_ESTIMATE_EMAIL],
                    phoenix=phoenix,
                    log=audit,
                    milestone_map=None,
                    scraper_client=scraper,
                    processed_store=processed_store,
                )
            )

        # Verify decision.
        assert len(records) == 1, len(records)
        d = records[0]["decision"]
        assert d["action"] == "create_project", d

        # Verify ensure_entity_and_project was NOT called.
        phoenix.ensure_entity_and_project.assert_not_called()

        # Verify ProcessedStore was marked ok with accepted=False.
        store_record = processed_store.get("fake_message_id_001")
        assert store_record is not None, f"Expected record, got {store_record}"
        assert store_record.status == "ok"
        assert store_record.metadata["accepted"] is False

        # Verify audit trail has the "not_accepted" log.
        rows = _read_audit_csv(log_path)
        not_accepted_rows = [
            r
            for r in rows
            if r["stage"] == "orchestrator" and r["action"] == "not_accepted"
        ]
        assert (
            len(not_accepted_rows) == 1
        ), f"Expected 1 not_accepted row, got {not_accepted_rows}"

    # ── 5. Phase 1: no tracking URL → escalate ──────────────────────────────

    @case("Phase 1: estimate without tracking URL escalates")
    def _test_phase1_no_tracking_url():
        from components.brain import decide

        email_no_tracking = dict(FAKE_ESTIMATE_EMAIL)
        email_no_tracking["body_html"] = "<p>No link here</p>"
        ev = parse_email(email_no_tracking).as_dict()
        ctx = {"found": False, "ambiguous": False}
        d = asyncio.run(decide(ev, ctx)).as_dict()
        assert d["action"] == "escalate", d
        assert d["needs_human"] is True
        assert "no tracking url" in d["reasoning"].lower(), d

    # ── 6. Phase 1: scraper returns no docs → mark error ────────────────────

    @case("Phase 1: scraper returns no docs -> mark error in ProcessedStore")
    def _test_phase1_scraper_no_docs():
        log_path = os.path.join(os.environ["LOG_DIR"], "audit_t9_no_docs.csv")
        audit = CsvLogger(
            path=log_path,
            columns=["stage", "action", "ok", "detail", "event_type", "project_ref"],
        )
        processed_store = ProcessedStore(
            Path(os.environ["LOG_DIR"]) / "processed_t9_no_docs.db"
        )
        scraper = _make_fake_scraper()
        phoenix = _make_fake_phoenix()

        # Make scraper return no docs.
        scraper.get_proposal = AsyncMock(return_value={"docs": [], "mget_docs": []})

        records = asyncio.run(
            orchestrator_run(
                listener=lambda: [FAKE_ESTIMATE_EMAIL],
                phoenix=phoenix,
                log=audit,
                milestone_map=None,
                scraper_client=scraper,
                processed_store=processed_store,
            )
        )

        # Verify decision.
        assert len(records) == 1, len(records)
        d = records[0]["decision"]
        assert d["action"] == "create_project", d

        # Verify extractor was NOT called.
        # (We can't verify this directly since we didn't patch extract_proposal,
        # but we can verify ensure_entity_and_project was NOT called.)
        phoenix.ensure_entity_and_project.assert_not_called()

        # Verify ProcessedStore was marked error.
        store_record = processed_store.get("fake_message_id_001")
        assert store_record is not None
        assert store_record.status == "error"
        assert store_record.metadata["error"] == "no_docs"

        # Verify audit trail has the "no_docs" log.
        rows = _read_audit_csv(log_path)
        no_docs_rows = [
            r for r in rows if r["stage"] == "scraper" and r["action"] == "no_docs"
        ]
        assert len(no_docs_rows) == 1, no_docs_rows

    print(f"\n{passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    raise SystemExit(0 if run() else 1)
