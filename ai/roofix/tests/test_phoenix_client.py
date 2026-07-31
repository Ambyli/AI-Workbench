"""
Unit tests for PhoenixClient — DRY_RUN paths only, no database required.

For reads (find_*) and ensure_entity_and_project, we patch the relevant
methods on individual instances so canned matches can drive the branch
logic without a real Phoenix connection.

Run from ai/roofix/:
    PYTHONPATH=. python tests/test_phoenix_client.py
"""

import os

# Set env BEFORE importing phoenix_client so its module-level constants pick
# up test values. AGENT_USER_ID must be set for any write to succeed.
os.environ.setdefault("PHOENIX_AGENT_USER_ID", "1399")
os.environ.setdefault("DRY_RUN", "true")
# Deliberately leave PHOENIX_DB_HOST unset — DRY_RUN writes must not call
# connect(). If a code path tries, the test will fail with a KeyError which
# is exactly the diagnostic we want.

from components.phoenix_client import (  # noqa: E402
    PhoenixClient,
    Result,
    COMPANY_ID,
    PROJECT_OBJECT_TYPE_ID,
    ENTITY_OBJECT_TYPE_ID,
    HOMEOWNER_REL_TYPE_ID,
    PROJECT_START_STATUS_ID,
)


CASES: list[tuple[str, callable]] = []  # populated by @case


def case(label: str):
    def deco(fn):
        CASES.append((label, fn))
        return fn
    return deco


# ── Constants sanity ─────────────────────────────────────────────────────────

@case("constants match Phoenix schema lookup findings")
def _test_constants():
    assert COMPANY_ID == 1, COMPANY_ID
    assert PROJECT_OBJECT_TYPE_ID == 7, PROJECT_OBJECT_TYPE_ID   # R&R / Roof
    assert ENTITY_OBJECT_TYPE_ID == 8, ENTITY_OBJECT_TYPE_ID     # Lead
    assert HOMEOWNER_REL_TYPE_ID == 7, HOMEOWNER_REL_TYPE_ID     # Homeowner
    assert PROJECT_START_STATUS_ID == 4, PROJECT_START_STATUS_ID # Qualification


# ── create_entity ────────────────────────────────────────────────────────────

@case("create_entity DRY_RUN returns sql + all params in order")
def _test_create_entity_dry_run():
    c = PhoenixClient(dry_run=True)
    r = c.create_entity(
        first_name="Gerald",
        last_name="kang",
        email="customer@example.test",
        phone="7573380000",
        street1="836 Lasser Drive",
        city="Norfolk",
        postal_code="23513",
        state_id=47,
        migration_external_id="1784844840425x142642292851362660",
    )
    assert r.ok and r.dry_run, r
    assert "INSERT INTO entity" in r.data["sql"]
    p = r.data["params"]
    # First two params are the required constants.
    assert p[0] == COMPANY_ID
    assert p[1] == ENTITY_OBJECT_TYPE_ID
    assert p[2] == "Gerald"
    assert p[3] == "kang"
    assert p[4] == "customer@example.test"
    assert p[7] == "836 Lasser Drive"  # street1
    assert p[9] == "Norfolk"  # city
    assert p[10] == "23513"  # postal_code
    assert p[11] == 47  # state_id
    assert p[12] == "1784844840425x142642292851362660"  # migration_external_id
    assert p[13] == 1399  # AGENT_USER_ID as int


@case("create_entity blocks when PHOENIX_AGENT_USER_ID is unset")
def _test_create_entity_requires_agent():
    # Temporarily null the module-level AGENT_USER_ID.
    import components.phoenix_client as pc
    saved = pc.AGENT_USER_ID
    pc.AGENT_USER_ID = None
    try:
        c = PhoenixClient(dry_run=True)
        r = c.create_entity(first_name="X", last_name="Y")
        assert not r.ok and "PHOENIX_AGENT_USER_ID" in r.detail, r
    finally:
        pc.AGENT_USER_ID = saved


# ── create_project ───────────────────────────────────────────────────────────

@case("create_project DRY_RUN returns sql + params, applies status default")
def _test_create_project_dry_run():
    c = PhoenixClient(dry_run=True)
    r = c.create_project(
        project_name="Robert Shepherd - 324 Whitely Street",
        street1="324 Whitely Street",
        city="Bridgeport",
        postal_code="43912",
        state_id=36,
        migration_external_id="1781297151690x264388044885887520",
        migration_entity_id="1781287165189x930665508813911000",
    )
    assert r.ok and r.dry_run, r
    assert "INSERT INTO project" in r.data["sql"]
    p = r.data["params"]
    assert p[0] == "Robert Shepherd - 324 Whitely Street"
    assert p[1] == COMPANY_ID
    assert p[2] == PROJECT_OBJECT_TYPE_ID
    assert p[3] == PROJECT_START_STATUS_ID   # default kicked in


@case("create_project without project_name is refused")
def _test_create_project_requires_name():
    c = PhoenixClient(dry_run=True)
    r = c.create_project(project_name="", city="X")
    assert not r.ok and "project_name" in r.detail, r


@case("create_project override object_status_id is respected")
def _test_create_project_status_override():
    c = PhoenixClient(dry_run=True)
    r = c.create_project(project_name="X", object_status_id=61)   # Installation
    assert r.ok and r.data["params"][3] == 61


# ── link_entity_project ──────────────────────────────────────────────────────

@case("link_entity_project DRY_RUN builds correct SQL + params")
def _test_link_dry_run():
    c = PhoenixClient(dry_run=True)
    r = c.link_entity_project(entity_id=42, project_id=100)
    assert r.ok and r.dry_run, r
    assert "INSERT INTO entity_project_relationship" in r.data["sql"]
    p = r.data["params"]
    assert p[0] == 42
    assert p[1] == 100
    assert p[2] == HOMEOWNER_REL_TYPE_ID
    assert p[3] is True   # main
    assert p[4] == 1399   # agent user id


# ── find_entity_by_identity argument validation ──────────────────────────────

@case("find_entity_by_identity refuses when no name or email")
def _test_find_entity_needs_something():
    c = PhoenixClient(dry_run=True)
    r = c.find_entity_by_identity()   # nothing supplied
    assert not r.ok and "email or name" in r.detail, r


# ── ensure_entity_and_project — the whole flow ───────────────────────────────

def _make_client_with_canned(entity_matches: list, project_matches: list) -> PhoenixClient:
    """Return a PhoenixClient whose finds return canned matches without hitting
    the DB. Also stubs create/link so their DRY_RUN payloads don't need
    real ids."""
    c = PhoenixClient(dry_run=True)
    c.find_entity_by_identity = lambda **kw: Result(
        ok=True, detail=f"{len(entity_matches)} candidate(s)",
        data={"matches": entity_matches, "unambiguous": len(entity_matches) == 1})
    c.find_project_by_roofix_id = lambda roofix_id: Result(
        ok=True, detail=f"{len(project_matches)} match(es)",
        data={"matches": project_matches})
    # State cache stubbed so resolve_state_id doesn't call connect().
    c._state_cache_by_abbr = {"OH": 36, "VA": 47}
    c._state_cache_by_name = {"ohio": 36, "virginia": 47}
    return c


_ACCEPTED_INPUT = {
    "roofix_project_id": "1781297151690x264388044885887520",
    "display_text": "Robert Shepherd - 324 Whitely Street",
    "first_name": "Robert",
    "last_name": "Shepherd",
    "full_name": "Robert Shepherd",
    "email": "customer@example.test",
    "phone": "5555550100",
    "street_address": "324 Whitely Street",
    "city": "Bridgeport",
    "state_text": "Ohio",
    "state_abbr": "OH",
    "zip_code": "43912",
    "homeowner_ref": "1781287165189x930665508813911000",
}


@case("ensure_entity_and_project: no matches -> creates both + links")
def _test_ensure_all_new():
    c = _make_client_with_canned(entity_matches=[], project_matches=[])
    r = c.ensure_entity_and_project(_ACCEPTED_INPUT)
    assert r.ok, r
    assert r.data["created_entity"] is True
    assert r.data["created_project"] is True
    assert r.data["created_link"] is True
    assert "entity=CREATED" in r.detail and "project=CREATED" in r.detail


@case("ensure_entity_and_project: entity exists -> reuse, create project + link")
def _test_ensure_entity_exists():
    c = _make_client_with_canned(
        entity_matches=[{"id": 555, "first_name": "Robert", "last_name": "Shepherd"}],
        project_matches=[],
    )
    r = c.ensure_entity_and_project(_ACCEPTED_INPUT)
    assert r.ok, r
    assert r.data["entity_id"] == 555
    assert r.data["created_entity"] is False
    assert r.data["created_project"] is True


@case("ensure_entity_and_project: both exist -> no writes in DRY_RUN")
def _test_ensure_both_exist():
    c = _make_client_with_canned(
        entity_matches=[{"id": 555}],
        project_matches=[{"id": 9001}],
    )
    r = c.ensure_entity_and_project(_ACCEPTED_INPUT)
    assert r.ok, r
    assert r.data["entity_id"] == 555
    assert r.data["phoenix_project_id"] == 9001
    assert r.data["created_entity"] is False
    assert r.data["created_project"] is False
    # In DRY_RUN, if nothing was created, we skip the link write too
    # (nothing new to attach).
    assert r.data["created_link"] is False


@case("ensure_entity_and_project: ambiguous entity match escalates")
def _test_ensure_ambiguous_entity():
    c = _make_client_with_canned(
        entity_matches=[{"id": 1}, {"id": 2}, {"id": 3}],
        project_matches=[],
    )
    r = c.ensure_entity_and_project(_ACCEPTED_INPUT)
    assert not r.ok, r
    assert "ambiguous entity" in r.detail
    assert r.data["entity_matches"] == [{"id": 1}, {"id": 2}, {"id": 3}]


@case("ensure_entity_and_project: ambiguous project match escalates")
def _test_ensure_ambiguous_project():
    c = _make_client_with_canned(
        entity_matches=[{"id": 555}],
        project_matches=[{"id": 100}, {"id": 101}],
    )
    r = c.ensure_entity_and_project(_ACCEPTED_INPUT)
    assert not r.ok, r
    assert "ambiguous project" in r.detail
    assert "roofix_id" in r.detail


@case("ensure_entity_and_project: missing roofix_project_id is rejected")
def _test_ensure_needs_roofix_id():
    c = _make_client_with_canned(entity_matches=[], project_matches=[])
    r = c.ensure_entity_and_project({"first_name": "X"})
    assert not r.ok and "roofix_project_id" in r.detail


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
