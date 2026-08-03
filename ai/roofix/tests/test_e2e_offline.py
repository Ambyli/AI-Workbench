"""
T8 — Offline end-to-end integration test.

Mocks Gmail + Phoenix, feeds real Estimate emails through the orchestrator,
verifies the pipeline produces the expected decisions and audit trail.

This proves the orchestration glue works. The full Estimate → scrape →
create_project flow requires T9 (orchestrator wiring) — that's a separate
test once the wiring lands.

Run from ai/roofix/:
    PYTHONPATH=. python tests/test_e2e_offline.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Set env BEFORE importing anything that reads it.
os.environ.setdefault("DRY_RUN", "true")
os.environ.setdefault("PHOENIX_AGENT_USER_ID", "1399")
os.environ.setdefault("AGENT_PHASE", "0")
os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="roofix_e2e_"))

from common.logging_setup import CsvLogger  # noqa: E402

from components.brain import decide  # noqa: E402
from components.parser import parse_email  # noqa: E402
from components.orchestrator import process_batch  # noqa: E402
from components.phoenix_client import PhoenixClient, Result  # noqa: E402

# ── Canned Estimate emails (from T3 fixtures) ────────────────────────────────

ESTIMATE_EMAILS = [
    {
        "label": "estimate_in_progress_gerald_kang",
        "sender": '"RFX | Estimate" <no-reply@roofix.io>',
        "to": ["peyton.anderton@zeoenergy.com"],
        "subject": "Estimate in Progress - Gerald kang - 836 Lasser Drive",
        "body_text": (
            "Hello, We have received your request to provide an estimate "
            "for Gerald kang - 836 Lasser Drive The Estimate is now being "
            "prepared and we will notify you as soon as it is ready."
        ),
        "body_html": (
            "Hello,<div></div><br />We have received your request to "
            "provide an estimate for Gerald kang - 836 Lasser Drive<br "
            "/><br />The Estimate is now being prepared and we will "
            "notify you as soon as it is ready. Under normal "
            "circumstances, this should take 5-10 minutes to complete."
            '<br /><br /><a href="http://url6628.roofix.io/ls/click?upn=xxx" '
            'target=_blank><font color="#0000ff">View the Project here'
            "</font></a>.<br /><br />Do not reply."
        ),
        "timestamp": "2026-07-23T22:37:43+00:00",
    },
]

# A non-Estimate email (comment) to verify the brain handles it differently.
COMMENT_EMAIL = {
    "label": "new_comment",
    "sender": '"RFX | New Comment" <no-reply@roofix.io>',
    "to": ["peyton.anderton@zeoenergy.com"],
    "subject": "New Comment - Test Project - 123 Main St",
    "body_text": "New comment on project 123 Main St.",
    "body_html": "<p>New comment on project 123 Main St.</p>",
    "timestamp": "2026-07-24T10:00:00+00:00",
}


# ── Mock Gmail listener ──────────────────────────────────────────────────────


def _mock_gmail(emails):
    """Return a listener that yields the given emails exactly once."""
    consumed = False

    def listener():
        nonlocal consumed
        if consumed:
            return []
        consumed = True
        return list(emails)

    return listener


# ── Mock Phoenix client ──────────────────────────────────────────────────────


class MockPhoenix:
    """Stands in for PhoenixClient. Records every method called and returns
    canned results. No DB required."""

    def __init__(self):
        self.calls: list[tuple[str, tuple, dict]] = []

    def _record(self, name, args, kwargs):
        self.calls.append((name, args, kwargs))

    def find_project_by_roofix_id(self, roofix_id):
        self._record("find_project_by_roofix_id", (roofix_id,), {})
        return Result(ok=True, detail="no match", data={"matches": []})

    def find_project_by_identity(self, name, address=None):
        self._record("find_project_by_identity", (name, address), {})
        return Result(ok=True, detail="no match", data={"matches": []})

    def update_chatter(self, project_id, note_text):
        self._record("update_chatter", (project_id, note_text), {})
        return Result(
            ok=True,
            detail="mocked",
            dry_run=True,
            data={"sql": "UPDATE chatter ...", "params": []},
        )

    def update_milestone(self, project_id, block_name, status_id):
        self._record("update_milestone", (project_id, block_name, status_id), {})
        return Result(
            ok=True,
            detail="mocked",
            dry_run=True,
            data={"sql": "UPDATE milestone ...", "params": []},
        )

    def ensure_entity_and_project(self, extracted):
        self._record("ensure_entity_and_project", (), {"extracted": extracted})
        return Result(
            ok=True,
            detail="mocked create",
            dry_run=True,
            data={
                "entity_id": 9999,
                "project_id": 8888,
                "created_entity": True,
                "created_project": True,
                "created_link": True,
            },
        )

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


# ── Helpers ──────────────────────────────────────────────────────────────────


def _read_audit_csv(log_path):
    """Parse the audit CSV into a list of dicts."""
    import csv

    with open(log_path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


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

    # ── 1. Estimate email → brain returns "ignore" in Phase 0 ──────────────

    @case("estimate email: parsed correctly with tracking_url")
    def _test_estimate_parsed():
        ev = parse_email(ESTIMATE_EMAILS[0])
        d = ev.as_dict()
        assert d["event_type"] == "Estimate", d
        assert d["customer_name"] == "Gerald kang", d
        assert d["address"] == "836 Lasser Drive", d
        assert d["roofix_id"] is None, d  # tokenized URL only
        assert d["tracking_url"], d  # tracking URL present
        assert d["parse_complete"] is False, d  # needs scrape

    @case("estimate email: brain returns ignore in Phase 0")
    def _test_estimate_brain_phase0():
        ev = parse_email(ESTIMATE_EMAILS[0]).as_dict()
        ctx = {"found": False, "ambiguous": False}
        d = decide(ev, ctx).as_dict()
        assert d["action"] == "ignore", d
        assert d["source"] == "rule", d
        assert "Phase 0" in d["reasoning"], d

    # ── 2. process_batch with estimate email + mock Phoenix ────────────────

    @case("process_batch: estimate email produces ignore decision")
    def _test_batch_estimate():
        log_path = os.path.join(os.environ["LOG_DIR"], "audit_e2e.csv")
        audit = CsvLogger(
            path=log_path,
            columns=["stage", "action", "ok", "detail", "event_type", "project_ref"],
        )
        phoenix = MockPhoenix()
        listener = _mock_gmail(ESTIMATE_EMAILS)

        decisions = process_batch(
            raw_emails=[],  # listeners are injected via run(), not process_batch
            phoenix=phoenix,
            log=audit,
            milestone_map=None,
        )
        # process_batch doesn't call the listener itself — that's run().
        # Re-do via run() for the full pipeline.
        decisions = None  # placeholder; we'll use run() below

    # Use run() for the full pipeline (listener → parse → decide → execute).
    @case("run(): estimate email flows through full pipeline")
    def _test_run_estimate():
        log_path = os.path.join(os.environ["LOG_DIR"], "audit_run.csv")
        audit = CsvLogger(
            path=log_path,
            columns=["stage", "action", "ok", "detail", "event_type", "project_ref"],
        )
        phoenix = MockPhoenix()
        listener = _mock_gmail(ESTIMATE_EMAILS)

        from components.orchestrator import run as orchestrator_run

        decisions = orchestrator_run(
            listener=listener, phoenix=phoenix, milestone_map=None, log=audit
        )

        assert len(decisions) == 1, f"expected 1 decision, got {len(decisions)}"
        d = decisions[0]
        assert d["action"] == "ignore", d
        assert d["source"] == "rule", d
        # Phoenix IS called for identity resolution (roofix_id=None in estimate).
        identity_calls = [
            c for c in phoenix.calls if c[0] == "find_project_by_identity"
        ]
        assert (
            len(identity_calls) == 1
        ), f"expected 1 identity call, got {len(identity_calls)}"
        assert identity_calls[0][1][0] == "Gerald kang", identity_calls

        # Audit CSV should have entries for parser + brain + orchestrator.
        rows = _read_audit_csv(log_path)
        stages = [r["stage"] for r in rows]
        assert "parser" in stages, stages
        assert "brain" in stages, stages
        assert "orchestrator" in stages, stages

    # ── 3. Comment email → brain returns update_chatter ────────────────────

    @case("run(): comment email escalates when project not found in Phoenix")
    def _test_run_comment():
        log_path = os.path.join(os.environ["LOG_DIR"], "audit_comment.csv")
        audit = CsvLogger(
            path=log_path,
            columns=["stage", "action", "ok", "detail", "event_type", "project_ref"],
        )
        phoenix = MockPhoenix()
        listener = _mock_gmail([COMMENT_EMAIL])

        from components.orchestrator import run as orchestrator_run

        decisions = orchestrator_run(
            listener=listener, phoenix=phoenix, milestone_map=None, log=audit
        )

        assert len(decisions) == 1, len(decisions)
        d = decisions[0]
        # Brain escalates when Phoenix has no project for this customer.
        assert d["action"] == "escalate", d
        assert d["needs_human"] is True, d
        assert "not found" in d["reasoning"].lower(), d
        # No chatter write — escalated decisions skip Phoenix writes.
        chatter_calls = [c for c in phoenix.calls if c[0] == "update_chatter"]
        assert len(chatter_calls) == 0, phoenix.calls

    # ── 4. Empty batch ─────────────────────────────────────────────────────

    @case("run(): empty batch produces no decisions, no errors")
    def _test_run_empty():
        log_path = os.path.join(os.environ["LOG_DIR"], "audit_empty.csv")
        audit = CsvLogger(
            path=log_path,
            columns=["stage", "action", "ok", "detail", "event_type", "project_ref"],
        )
        phoenix = MockPhoenix()
        listener = _mock_gmail([])

        from components.orchestrator import run as orchestrator_run

        decisions = orchestrator_run(
            listener=listener, phoenix=phoenix, milestone_map=None, log=audit
        )

        assert decisions == [], decisions
        assert phoenix.calls == []

    # ── 5. Multiple Estimate emails batch ──────────────────────────────────

    @case("run(): multiple estimate emails batched correctly")
    def _test_run_multiple_estimates():
        log_path = os.path.join(os.environ["LOG_DIR"], "audit_multi.csv")
        audit = CsvLogger(
            path=log_path,
            columns=["stage", "action", "ok", "detail", "event_type", "project_ref"],
        )
        phoenix = MockPhoenix()

        emails = ESTIMATE_EMAILS + [
            {
                "label": "estimate_in_progress_linda_ward",
                "sender": '"RFX | Estimate" <no-reply@roofix.io>',
                "to": ["cole.fife@zeoenergy.com"],
                "subject": "Estimate in Progress - Linda ward - 1512 Fairlane Avenue Southwest",
                "body_text": (
                    "Hello, We have received your request to provide an "
                    "estimate for Linda ward - 1512 Fairlane Avenue Southwest"
                ),
                "body_html": (
                    "Hello,<div></div><br />We have received your request "
                    "to provide an estimate for Linda ward - 1512 Fairlane "
                    "Avenue Southwest<br /><br />The Estimate is now being "
                    "prepared and we will notify you as soon as it is ready."
                    '<br /><br /><a href="http://url6628.roofix.io/ls/click?upn=yyy" '
                    'target=_blank><font color="#0000ff">View the Project '
                    "here</font></a>.<br /><br />Do not reply."
                ),
                "timestamp": "2026-07-23T19:59:36+00:00",
            },
        ]
        listener = _mock_gmail(emails)

        from components.orchestrator import run as orchestrator_run

        decisions = orchestrator_run(
            listener=listener, phoenix=phoenix, milestone_map=None, log=audit
        )

        assert len(decisions) == 2, len(decisions)
        for d in decisions:
            assert d["action"] == "ignore", d
            assert d["source"] == "rule", d
        # Two identity lookups (one per estimate, since roofix_id=None).
        identity_calls = [
            c for c in phoenix.calls if c[0] == "find_project_by_identity"
        ]
        assert (
            len(identity_calls) == 2
        ), f"expected 2 identity calls, got {len(identity_calls)}"

    # ── 6. CsvLogger audit trail integrity ──────────────────────────────────

    @case("audit CSV: every decision has parser + brain + orchestrator rows")
    def _test_audit_trail_complete():
        log_path = os.path.join(os.environ["LOG_DIR"], "audit_trail.csv")
        audit = CsvLogger(
            path=log_path,
            columns=["stage", "action", "ok", "detail", "event_type", "project_ref"],
        )
        phoenix = MockPhoenix()
        listener = _mock_gmail(ESTIMATE_EMAILS + [COMMENT_EMAIL])

        from components.orchestrator import run as orchestrator_run

        orchestrator_run(
            listener=listener, phoenix=phoenix, milestone_map=None, log=audit
        )

        rows = _read_audit_csv(log_path)
        # 2 emails × (parser + brain + orchestrator) + 1 extra escalate row = 7.
        assert len(rows) == 7, f"expected 7 rows, got {len(rows)}: {rows}"

        # Every row has a timestamp (auto-prepended).
        for r in rows:
            assert r["timestamp"], f"missing timestamp: {r}"

        # Parser rows show parse_complete status.
        parser_rows = [r for r in rows if r["stage"] == "parser"]
        assert len(parser_rows) == 2, parser_rows
        # Estimate emails: parse_complete=False.
        for r in parser_rows:
            assert r["ok"] == "False", f"estimate should have ok=False: {r}"

        # Brain rows show source.
        brain_rows = [r for r in rows if r["stage"] == "brain"]
        assert len(brain_rows) == 2, brain_rows
        sources = {r["detail"].split("[")[1].split("]")[0] for r in brain_rows}
        assert sources == {"rule"}, sources

        # Escalate stage present for the comment email.
        escalate_rows = [r for r in rows if r["stage"] == "escalate"]
        assert len(escalate_rows) == 1, escalate_rows
        assert "NEEDS HUMAN" in escalate_rows[0]["detail"], escalate_rows[0]

    print(f"\n{passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    raise SystemExit(0 if run() else 1)
