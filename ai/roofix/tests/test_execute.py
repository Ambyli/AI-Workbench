"""
orchestrator._execute's escalation branch and Phoenix-write accounting, and
app.py's use of them (/status counters, mark_read).

  * Escalation: action ``escalate`` OR ``needs_human`` (whatever the action)
    takes the escalation branch -- an ``escalate`` with needs_human=false
    used to fall through to the unsupported-action case (mark_error, retried
    every tick), and a needs_human ``ignore`` was executed as an ignore.
  * ``_phoenix_write_ok`` is stamped on every decision whose Phoenix write
    ran; app._record_tick counts the False ones as
    ``phoenix_write_failures`` (it was never incremented).

Offline: Phoenix, Gmail and the processed store are fakes / sqlite.

Run from ai/roofix/:
    PYTHONPATH=. python tests/test_execute.py
"""

from __future__ import annotations

import asyncio
import csv
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Set env BEFORE importing anything that reads it.
os.environ.setdefault("PHOENIX_AGENT_USER_ID", "1399")
os.environ.setdefault("DRY_RUN", "true")
os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="roofix_execute_"))

from common.logging_setup import CsvLogger  # noqa: E402
from common.processed_store import ProcessedStore  # noqa: E402

import components.orchestrator as orch  # noqa: E402
from components.constants import LOG_COLUMNS  # noqa: E402
from components.phoenix_client import Result  # noqa: E402

LOG_DIR = Path(os.environ["LOG_DIR"])
CASES: list = []
_n = 0


def case(label: str):
    def deco(fn):
        CASES.append((label, fn))
        return fn

    return deco


def _fresh(name: str):
    """A new audit log + processed store pair for one case."""
    global _n
    _n += 1
    log_path = LOG_DIR / f"audit_{_n}_{name}.csv"
    log = CsvLogger(path=log_path, columns=LOG_COLUMNS)
    store = ProcessedStore(LOG_DIR / f"processed_{_n}_{name}.db")
    return log, log_path, store


def _rows(path: Path) -> list:
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _decision(action: str, needs_human: bool, source: str = "ai", **kw) -> dict:
    d = {
        "action": action,
        "event_type": "Something Odd",
        "target": kw.pop("target", "4242"),
        "payload": kw.pop("payload", {}),
        "reasoning": "because",
        "needs_human": needs_human,
        "source": source,
        "message_id": "msg-1",
    }
    d.update(kw)
    return d


def _event(**kw) -> dict:
    ev = {
        "event_type": "Something Odd",
        "message_id": "msg-1",
        "_raw_email": {"subject": "RFX thing", "message_id": "msg-1"},
    }
    ev.update(kw)
    return ev


def _schema_problems(decision: dict) -> list:
    """``decision`` against ``$defs/decision`` of roofix.schemas.json."""
    import json

    from common.apidocs import schemas

    path = Path(__file__).resolve().parent.parent / "roofix.schemas.json"
    defs = json.loads(path.read_text(encoding="utf-8"))["$defs"]
    return schemas.validate(schemas.standalone(defs["decision"], defs), decision)


def _execute(decision, ev, phoenix, log, store, *, gmail=None, recipients=(), milestone_map=None):
    with patch.object(orch, "ESCALATION_RECIPIENTS", list(recipients)):
        asyncio.run(
            orch._execute(
                decision, ev, phoenix, log, milestone_map, processed_store=store, gmail=gmail
            )
        )
    # Whatever _execute stamped is documented in the /tick record's decision.
    assert _schema_problems(decision) == [], (decision, _schema_problems(decision))


def _assert_escalated(decision, log_path, store, phoenix):
    rows = _rows(log_path)
    assert [r["stage"] for r in rows] == ["escalate"], rows
    assert rows[0]["detail"].startswith("NEEDS HUMAN: "), rows
    rec = store.get("msg-1")
    assert rec is not None and rec.status == "ok", rec
    assert rec.metadata["action"] == "escalate", rec.metadata
    assert "_forwarded" in decision, decision
    assert "_phoenix_write_ok" not in decision, decision
    for write in ("update_chatter", "update_milestone", "ensure_entity_and_project"):
        getattr(phoenix, write).assert_not_called()


# ── Escalation branch ────────────────────────────────────────────────────────


@case("escalate with needs_human=false is escalated (not 'unsupported')")
def _test_escalate_without_needs_human():
    log, log_path, store = _fresh("esc_false")
    phoenix = MagicMock()
    d = _decision("escalate", needs_human=False)
    _execute(d, _event(), phoenix, log, store)
    _assert_escalated(d, log_path, store, phoenix)
    assert d["_forwarded"] is False


@case("escalate with needs_human=true (the rules' shape) is escalated")
def _test_escalate_with_needs_human():
    log, log_path, store = _fresh("esc_true")
    phoenix = MagicMock()
    d = _decision("escalate", needs_human=True, source="rule", target=None)
    _execute(d, _event(), phoenix, log, store)
    _assert_escalated(d, log_path, store, phoenix)


@case("needs_human=true on another action escalates, no Phoenix write")
def _test_needs_human_other_action():
    log, log_path, store = _fresh("nh_chatter")
    phoenix = MagicMock()
    d = _decision("update_chatter", needs_human=True, payload={"note_text": "x"})
    _execute(d, _event(), phoenix, log, store)
    _assert_escalated(d, log_path, store, phoenix)


@case("needs_human=true on an AI ignore escalates")
def _test_needs_human_ignore():
    log, log_path, store = _fresh("nh_ignore")
    phoenix = MagicMock()
    d = _decision("ignore", needs_human=True)
    _execute(d, _event(), phoenix, log, store)
    _assert_escalated(d, log_path, store, phoenix)


@case("escalate with needs_human=false is forwarded when recipients are set")
def _test_escalate_forwarded():
    log, log_path, store = _fresh("esc_fwd")
    gmail = MagicMock()
    d = _decision("escalate", needs_human=False)
    _execute(d, _event(), MagicMock(), log, store, gmail=gmail, recipients=["ops@example.test"])
    gmail.forward_email.assert_called_once()
    assert d["_forwarded"] is True
    assert store.get("msg-1").metadata["forwarded"] is True
    assert [r["action"] for r in _rows(log_path)] == ["escalate", "forwarded"]


@case("a plain rule ignore is still an ignore")
def _test_rule_ignore_unchanged():
    log, log_path, store = _fresh("ignore")
    d = _decision("ignore", needs_human=False, source="rule")
    _execute(d, _event(), MagicMock(), log, store)
    assert [(r["stage"], r["action"]) for r in _rows(log_path)] == [("orchestrator", "ignore")]
    assert store.get("msg-1").metadata["action"] == "ignore"
    assert "_forwarded" not in d


# ── Phoenix write accounting ─────────────────────────────────────────────────


@case("update_chatter stamps _phoenix_write_ok with the write's result")
def _test_chatter_write_ok():
    for ok in (True, False):
        log, _, store = _fresh(f"chatter_{ok}")
        phoenix = MagicMock()
        phoenix.update_chatter.return_value = Result(ok=ok, detail="x")
        d = _decision("update_chatter", needs_human=False, source="rule",
                      payload={"note_text": "[Roofix] hi"})
        _execute(d, _event(), phoenix, log, store)
        phoenix.update_chatter.assert_called_once_with(4242, "[Roofix] hi")
        assert d["_phoenix_write_ok"] is ok, d


@case("update_milestone: stamped when written, absent with no mapping")
def _test_milestone_write_ok():
    log, _, store = _fresh("ms_nomap")
    phoenix = MagicMock()
    d = _decision("update_milestone", needs_human=False, source="rule")
    _execute(d, _event(event_type="HIC Executed"), phoenix, log, store, milestone_map={})
    phoenix.update_milestone.assert_not_called()
    assert "_phoenix_write_ok" not in d, d

    log, _, store = _fresh("ms_fail")
    phoenix.update_milestone.return_value = Result(ok=False, detail="resolved to 0 rows")
    d = _decision("update_milestone", needs_human=False, source="rule")
    _execute(d, _event(event_type="HIC Executed"), phoenix, log, store,
             milestone_map={"HIC Executed": {"block_name": "B", "status_id": 3}})
    phoenix.update_milestone.assert_called_once_with(4242, "B", 3)
    assert d["_phoenix_write_ok"] is False, d


@case("create_project: a failed ensure_entity_and_project is stamped False")
def _test_create_project_write_failed():
    log, _, store = _fresh("create_fail")
    phoenix = MagicMock()
    phoenix.ensure_entity_and_project.return_value = Result(ok=False, detail="ambiguous entity match")
    ev = _event(event_type="Estimate", _extracted_payload={"roofix_id": "R1", "is_accepted": True})
    d = _decision("create_project", needs_human=False, source="rule", target="http://t")
    _execute(d, ev, phoenix, log, store)
    assert d["_phoenix_write_ok"] is False, d
    assert store.get("msg-1").status == "error"

    # Not accepted: no write attempted, nothing stamped.
    log, _, store = _fresh("create_not_accepted")
    ev = _event(event_type="Estimate", _extracted_payload={"roofix_id": "R1", "is_accepted": False})
    d = _decision("create_project", needs_human=False, source="rule", target="http://t")
    _execute(d, ev, MagicMock(), log, store)
    assert "_phoenix_write_ok" not in d, d


# ── app.py: /status counters and mark_read ───────────────────────────────────


def _app():
    import app

    return app


def _rec(**decision) -> dict:
    base = {"action": "update_chatter", "source": "rule", "needs_human": False, "message_id": None}
    base.update(decision)
    return {"event": {"event_type": "New Comment"}, "decision": base}


@case("app._record_tick counts failed Phoenix writes once each")
def _test_record_tick_counts():
    app = _app()
    before = dict(app._STATE)
    app._record_tick(
        [
            _rec(_phoenix_write_ok=True),
            _rec(_phoenix_write_ok=False),
            _rec(action="update_milestone", _phoenix_write_ok=False),
            _rec(action="ignore"),  # no write ran
            _rec(action="escalate", needs_human=False, _forwarded=False),
        ],
        error=None,
    )
    assert app._STATE["phoenix_write_failures"] - before["phoenix_write_failures"] == 2
    assert app._STATE["escalations_total"] - before["escalations_total"] == 1
    body = app.status()
    assert body["phoenix_write_failures"] == app._STATE["phoenix_write_failures"]

    import json

    from common.apidocs import schemas

    data = json.loads((Path(__file__).resolve().parent.parent / "roofix.schemas.json").read_text(encoding="utf-8"))
    spec = data["endpoints"]["GET /status"]["responses"]["200"]["schema"]
    assert schemas.validate(schemas.standalone(spec, data["$defs"]), json.loads(json.dumps(body))) == []


@case("app leaves unforwarded escalations unread, needs_human ones included")
def _test_mark_read_rules():
    app = _app()
    records = [
        _rec(message_id="m-chatter"),
        _rec(message_id="m-esc", action="escalate", needs_human=False, _forwarded=False),
        _rec(message_id="m-nh", action="update_chatter", needs_human=True, _forwarded=False),
        _rec(message_id="m-fwd", action="ignore", needs_human=True, _forwarded=True),
        _rec(message_id="m-ai-ignore", action="ignore", source="ai"),
    ]
    gmail = MagicMock()

    def ctx(instance):
        cls = MagicMock()
        cls.return_value.__enter__.return_value = instance
        return cls

    app._tick_lock = None
    with patch.object(app, "PhoenixClient", ctx(MagicMock())), \
         patch.object(app, "GmailClient", ctx(gmail)), \
         patch.object(app, "PostgresProcessedStore", ctx(MagicMock())), \
         patch.object(app, "RoofixScraperClient", MagicMock()), \
         patch.object(app, "orchestrator_run", AsyncMock(return_value=records)):
        out = asyncio.run(app._run_one_batch(raw_emails=[], skip_dedup=True))
    assert out["count"] == 5
    read = sorted(c.args[0] for c in gmail.mark_read.call_args_list)
    assert read == ["m-chatter", "m-fwd"], read
    labelled = sorted(c.args[0] for c in gmail.apply_label.call_args_list)
    assert labelled == sorted(r["decision"]["message_id"] for r in records), labelled


# ── Runner ───────────────────────────────────────────────────────────────────


def run() -> bool:
    passed = failed = 0
    for label, fn in CASES:
        try:
            fn()
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {label}\n        {e}")
        except Exception as e:
            failed += 1
            print(f"FAIL  {label}\n        {type(e).__name__}: {e}")
        else:
            passed += 1
            print(f"ok    {label}")
    print(f"\n{passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    raise SystemExit(0 if run() else 1)
