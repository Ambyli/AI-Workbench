"""
The ``_extracted_payload`` contract: ExtractedProposal -> orchestrator ->
PhoenixClient.ensure_entity_and_project.

``constants.EXTRACTED_PAYLOAD_FIELDS`` names the ExtractedProposal attributes
the orchestrator copies (getattr, same names) into the payload Phoenix's
create path reads. A name the dataclass lacks is silently null, and a key
Phoenix reads that the whitelist drops is silently null too -- both happened
(``state_text`` vs the field ``state``; ``homeowner_ref`` never copied), so
these cases pin the names in both directions and run the real fixture
through the real extractor, orchestrator and (DRY_RUN) Phoenix client.

Offline: no Gmail, no Phoenix connection, no interceptor.

Run from ai/roofix/:
    PYTHONPATH=. python tests/test_extracted_payload.py
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Set env BEFORE importing anything that reads it.
os.environ.setdefault("PHOENIX_AGENT_USER_ID", "1399")
os.environ.setdefault("DRY_RUN", "true")
os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="roofix_payload_"))

from common.logging_setup import CsvLogger  # noqa: E402
from common.processed_store import ProcessedStore  # noqa: E402

import components.brain as brain_mod  # noqa: E402
from components.constants import EXTRACTED_PAYLOAD_FIELDS, LOG_COLUMNS  # noqa: E402
from components.orchestrator import _extracted_to_payload, process_batch  # noqa: E402
from components.phoenix_client import PhoenixClient, Result  # noqa: E402
from components.proposal_extractor import ExtractedProposal, extract_proposal  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
SCHEMAS = Path(__file__).resolve().parent.parent / "roofix.schemas.json"
HOMEOWNER_REF = "1781287165189x930665508813911000"  # proposal_accepted.json's homeowner


def schema_problems(def_name: str, instance) -> list:
    """``instance`` against ``$defs/<def_name>`` of roofix.schemas.json (the
    documented /tick record shapes); [] when it is valid."""
    from common.apidocs import schemas

    defs = json.loads(SCHEMAS.read_text(encoding="utf-8"))["$defs"]
    return schemas.validate(schemas.standalone(defs[def_name], defs), instance)

CASES: list = []


def case(label: str):
    def deco(fn):
        CASES.append((label, fn))
        return fn

    return deco


def _accepted() -> dict:
    return json.loads((FIXTURES / "proposal_accepted.json").read_text(encoding="utf-8"))


class _RecordingDict(dict):
    """A dict that remembers every key read through ``.get`` / ``[]``."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.reads: set = set()

    def get(self, key, default=None):
        self.reads.add(key)
        return super().get(key, default)

    def __getitem__(self, key):
        self.reads.add(key)
        return super().__getitem__(key)


def _canned_client(entity_matches: list, project_matches: list) -> PhoenixClient:
    """DRY_RUN client whose reads return canned matches (no DB)."""
    c = PhoenixClient(dry_run=True)
    c.find_entity_by_identity = lambda **kw: Result(
        ok=True, data={"matches": entity_matches}
    )
    c.find_project_by_roofix_id = lambda roofix_id: Result(
        ok=True, data={"matches": project_matches}
    )
    c._state_cache_by_abbr = {"OH": 36, "VA": 47}
    c._state_cache_by_name = {"ohio": 36, "virginia": 47}
    return c


def _spy(client: PhoenixClient, name: str) -> list:
    """Wrap ``client.<name>`` and return the list its kwargs are appended to."""
    calls: list = []
    real = getattr(client, name)

    def wrapper(**kwargs):
        calls.append(kwargs)
        return real(**kwargs)

    setattr(client, name, wrapper)
    return calls


# ── The names, both directions ───────────────────────────────────────────────


@case("every EXTRACTED_PAYLOAD_FIELDS name is an ExtractedProposal field")
def _test_fields_exist():
    have = {f.name for f in dataclasses.fields(ExtractedProposal)}
    missing = [f for f in EXTRACTED_PAYLOAD_FIELDS if f not in have]
    assert missing == [], f"always-null payload keys: {missing}"
    assert len(set(EXTRACTED_PAYLOAD_FIELDS)) == len(EXTRACTED_PAYLOAD_FIELDS)


@case("every key ensure_entity_and_project reads is in the payload")
def _test_phoenix_reads_are_supplied():
    payload = _extracted_to_payload(extract_proposal(_accepted()))
    reads: set = set()
    # All three shapes of the flow: create both, reuse entity, reuse both.
    for entities, projects in (([], []), ([{"id": 5}], []), ([{"id": 5}], [{"id": 9}])):
        rec = _RecordingDict(payload)
        r = _canned_client(entities, projects).ensure_entity_and_project(rec)
        assert r.ok, r
        reads |= rec.reads
    dropped = sorted(reads - set(EXTRACTED_PAYLOAD_FIELDS))
    assert dropped == [], f"Phoenix reads keys the payload never carries: {dropped}"


@case("the orchestrator's create_project branch reads only payload keys")
def _test_orchestrator_reads_are_supplied():
    assert {"roofix_id", "is_accepted"} <= set(EXTRACTED_PAYLOAD_FIELDS)


# ── Real fixture through the real extractor ──────────────────────────────────


@case("payload from proposal_accepted.json carries the extractor's values")
def _test_payload_values():
    extracted = extract_proposal(_accepted())
    assert extracted.ok, extracted.error
    payload = _extracted_to_payload(extracted)
    assert list(payload) == list(EXTRACTED_PAYLOAD_FIELDS)
    want = {
        "roofix_id": "1781297151690x264388044885887520",
        "is_accepted": True,
        "display_text": "Robert Shepherd - 324 Whitely Street",
        "full_name": "Robert Shepherd",
        "first_name": "Robert",
        "last_name": "Shepherd",
        "street_address": "324 Whitely Street",
        "city": "Bridgeport",
        "state": "Ohio",
        "state_abbr": "OH",
        "zip_code": "43912",
        "homeowner_ref": HOMEOWNER_REF,
        "actual_contract_price": 11382.37,
        "job_status": "completed",
        "error": None,
    }
    for k, v in want.items():
        assert payload[k] == v, f"{k}: expected {v!r}, got {payload[k]!r}"
    # acceptance_signals is a plain dict, so the event the payload rides on
    # stays JSON-able (the brain's AI fallback json.dumps it).
    assert payload["acceptance_signals"] == extracted.acceptance_signals.as_dict()
    json.dumps(payload)
    assert schema_problems("extracted_payload", payload) == []


@case("Phoenix create gets the state (by name) and the homeowner ref")
def _test_phoenix_create_params():
    payload = _extracted_to_payload(extract_proposal(_accepted()))
    # Drop the abbreviation so only the state NAME can resolve it -- the
    # path that read the nonexistent `state_text` key before.
    payload["state_abbr"] = None
    c = _canned_client([], [])
    entities = _spy(c, "create_entity")
    projects = _spy(c, "create_project")
    r = c.ensure_entity_and_project(payload)
    assert r.ok, r
    assert r.data["created_entity"] and r.data["created_project"], r.data
    (ent,) = entities
    assert ent["state_id"] == 36, ent
    assert ent["migration_external_id"] == HOMEOWNER_REF, ent
    assert ent["first_name"] == "Robert" and ent["city"] == "Bridgeport", ent
    (proj,) = projects
    assert proj["state_id"] == 36, proj
    assert proj["migration_entity_id"] == HOMEOWNER_REF, proj
    assert proj["migration_external_id"] == "1781297151690x264388044885887520", proj
    assert proj["project_name"] == "Robert Shepherd - 324 Whitely Street", proj


# ── Through the orchestrator ─────────────────────────────────────────────────

ESTIMATE_EMAIL = {
    "sender": '"RFX | Estimate" <no-reply@roofix.io>',
    "to": ["ops@example.test"],
    "subject": "Estimate in Progress - Robert Shepherd - 324 Whitely Street",
    "body_text": "Estimate for Robert Shepherd - 324 Whitely Street",
    "body_html": '<a href="http://url6628.roofix.io/ls/click?upn=TOKEN">View</a>',
    "timestamp": "2026-07-28T10:00:00+00:00",
    "message_id": "payload_msg_001",
}


@case("process_batch hands Phoenix the real extracted payload (Phase 1)")
def _test_process_batch_payload():
    scraper = MagicMock()
    scraper.get_proposal = AsyncMock(return_value=_accepted())
    phoenix = MagicMock()
    phoenix.find_project_by_roofix_id.return_value = Result(ok=True, data={"matches": []})
    phoenix.find_project_by_identity.return_value = Result(ok=True, data={"matches": []})
    phoenix.ensure_entity_and_project.return_value = Result(
        ok=True, dry_run=True, detail="DRY_RUN: mocked", data={"entity_id": None, "project_id": None}
    )
    log_dir = Path(os.environ["LOG_DIR"])
    log = CsvLogger(path=log_dir / "audit_payload.csv", columns=LOG_COLUMNS)
    store = ProcessedStore(log_dir / "processed_payload.db")
    with patch.object(brain_mod, "PHASE", "1"):
        records = asyncio.run(
            process_batch(
                [ESTIMATE_EMAIL],
                phoenix=phoenix,
                log=log,
                scraper_client=scraper,
                processed_store=store,
            )
        )
    (rec,) = records
    assert rec["decision"]["action"] == "create_project", rec["decision"]
    phoenix.ensure_entity_and_project.assert_called_once()
    sent = phoenix.ensure_entity_and_project.call_args[0][0]
    assert sent["state"] == "Ohio" and sent["homeowner_ref"] == HOMEOWNER_REF, sent
    assert rec["event"]["_extracted_payload"] == sent
    json.dumps(rec)  # the whole record (event + decision) is JSON-able
    assert rec["decision"]["_phoenix_write_ok"] is True
    # ...and matches what roofix.schemas.json documents for /tick's records.
    assert schema_problems("event", rec["event"]) == []
    assert schema_problems("decision", rec["decision"]) == []


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
