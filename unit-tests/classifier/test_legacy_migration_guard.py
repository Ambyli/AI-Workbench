"""The SQLite → Postgres migration guard, and the marker that lifts it.

Until ``bin/migrate_sqlite_to_postgres.py`` has run, every job row is still in
the old ``/data/classifier.db`` — so to the new container a queued payload or
an artifact directory with no Postgres row looks like an orphan, and its two
startup sweeps (the queue's payload sweep, the artifact sweeper) would delete
exactly the data the migration is about to give a row. ``main``'s lifespan
therefore holds both back while the old file exists WITHOUT the
``classifier.db.migrated`` marker a real run of the script writes beside it.

Pinned:

  * legacy file present, no marker — an orphan payload file and an orphan
    artifact directory survive a lifespan start, the sweeper task is never
    started, and the start logs an ERROR saying so;
  * marker present (and, as before the guard existed, no legacy file at all) —
    both are swept and the sweeper runs;
  * the script writes the marker on a real run against the default path, and
    never on ``--dry-run`` or for a non-default ``--sqlite`` path.

The lifespan tests need the classifier's Postgres (``TEST_POSTGRES_DSN``; see
conftest.py). The marker tests replace the script's ``migrate`` coroutine —
what it copies is pinned elsewhere — so they run without a database.

Run with::

    TEST_POSTGRES_DSN=postgresql://postgres@localhost:5432/postgres \\
    UV_LINK_MODE=copy uv run --no-sync --with pytest --package classifier \\
        python -m pytest unit-tests/classifier/test_legacy_migration_guard.py -q -p no:cacheprovider
"""

from __future__ import annotations

import importlib.util
import json
import logging
import pathlib
import sqlite3
import uuid

import pytest
from fastapi.testclient import TestClient

_SCRIPT = (
    pathlib.Path(__file__).resolve().parents[2]
    / "ai" / "classifier" / "bin" / "migrate_sqlite_to_postgres.py"
)


# ---------------------------------------------------------------------------
# The startup guard
# ---------------------------------------------------------------------------


def _orphans() -> tuple[str, pathlib.Path, str]:
    """An orphan payload file and an orphan artifact directory — neither has a
    row in the jobs table. Ids are fresh hex so no other test can own them."""
    from jobs.queue import queue
    from regions.store import store

    payload_id = uuid.uuid4().hex[:12]
    payload = queue.payloads.path(payload_id)
    payload.parent.mkdir(parents=True, exist_ok=True)
    payload.write_text(json.dumps({"schema": 3, "documents": [], "criteria": []}),
                       encoding="utf-8")
    artifact_id = uuid.uuid4().hex[:12]
    store.write_json(artifact_id, "regions.json", {"regions": []})
    assert store.exists(artifact_id)
    return payload_id, payload, artifact_id


@pytest.fixture
def legacy(monkeypatch, tmp_path):
    """Point main's legacy-file / marker paths at a temp directory and return
    them; the files themselves are created by each test."""
    import main

    db = tmp_path / "classifier.db"
    marker = tmp_path / "classifier.db.migrated"
    monkeypatch.setattr(main, "LEGACY_SQLITE_PATH", str(db))
    monkeypatch.setattr(main, "LEGACY_SQLITE_MARKER", str(marker))
    return db, marker


@pytest.mark.postgres
def test_unmigrated_legacy_file_holds_both_sweeps_back(legacy, caplog):
    import main
    from jobs.queue import queue, sweeper
    from regions.store import store

    db, marker = legacy
    sqlite3.connect(db).close()          # the old file is there; no marker
    payload_id, payload, artifact_id = _orphans()

    with caplog.at_level(logging.ERROR, logger="classifier"):
        with TestClient(main.app) as client:
            assert client.get("/health").status_code == 200
            assert sweeper._task is None                 # never started
            assert payload.exists()                      # orphan payload kept
            assert store.exists(artifact_id)             # orphan directory kept
            assert payload_id in queue.payloads.ids()
    assert any("has not been migrated" in r.getMessage() for r in caplog.records
               if r.levelno == logging.ERROR)
    assert not marker.exists()

    # Leave the session as we found it.
    payload.unlink()
    store.delete(artifact_id)


@pytest.mark.postgres
@pytest.mark.parametrize("state", ["marker", "no-legacy-file"])
def test_with_the_marker_or_no_legacy_file_both_sweeps_run(legacy, caplog, state):
    import main
    from jobs.queue import sweeper
    from regions.store import store

    db, marker = legacy
    if state == "marker":
        sqlite3.connect(db).close()
        marker.write_text("{}", encoding="utf-8")
    _, payload, artifact_id = _orphans()

    with caplog.at_level(logging.ERROR, logger="classifier"):
        with TestClient(main.app) as client:
            assert client.get("/health").status_code == 200
            assert sweeper._task is not None and not sweeper._task.done()
            assert not payload.exists()                  # swept at queue.start()
            assert not store.exists(artifact_id)         # swept at sweeper.start()
    assert not any("has not been migrated" in r.getMessage() for r in caplog.records)


def test_legacy_unmigrated_is_false_without_the_file(legacy):
    import main

    db, marker = legacy
    assert main._legacy_unmigrated() is False
    sqlite3.connect(db).close()
    assert main._legacy_unmigrated() is True
    marker.write_text("{}", encoding="utf-8")
    assert main._legacy_unmigrated() is False


# ---------------------------------------------------------------------------
# The marker the script writes
# ---------------------------------------------------------------------------


@pytest.fixture
def script(monkeypatch, tmp_path):
    """The migration script as a module, its default path and marker pointed
    at a temp directory, and ``migrate`` replaced by a recorder."""
    spec = importlib.util.spec_from_file_location("migrate_sqlite_to_postgres", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    default = tmp_path / "classifier.db"
    sqlite3.connect(default).close()
    monkeypatch.setattr(module, "LEGACY_SQLITE_PATH", str(default))
    monkeypatch.setattr(module, "LEGACY_SQLITE_MARKER", str(default) + ".migrated")

    calls: list[tuple[str, bool]] = []
    report = {"jobs": {"read": 2, "inserted": 2, "present": 0, "unreadable": 0}}

    async def fake_migrate(path, *, dry_run=False):
        calls.append((path, dry_run))
        return report

    monkeypatch.setattr(module, "migrate", fake_migrate)
    module.calls, module.report = calls, report
    return module, default, pathlib.Path(str(default) + ".migrated")


def test_a_real_run_on_the_default_path_writes_the_marker(script, capsys):
    module, default, marker = script
    assert module.main(["--sqlite", str(default)]) == 0
    assert module.calls == [(str(default), False)]
    body = json.loads(marker.read_text(encoding="utf-8"))
    assert body["report"] == module.report and body["migrated_at"]
    assert "Wrote" in capsys.readouterr().out


def test_the_default_argument_writes_the_marker_too(script):
    module, default, marker = script
    assert module.main([]) == 0
    assert module.calls == [(str(default), False)]
    assert marker.exists()


def test_a_dry_run_never_writes_the_marker(script):
    module, default, marker = script
    assert module.main(["--dry-run"]) == 0
    assert module.calls == [(str(default), True)]
    assert not marker.exists()


def test_a_non_default_path_never_writes_the_marker(script, tmp_path):
    module, default, marker = script
    other = tmp_path / "elsewhere" / "classifier.db"
    other.parent.mkdir()
    sqlite3.connect(other).close()
    assert module.main(["--sqlite", str(other)]) == 0
    assert module.calls == [(str(other), False)]
    assert not marker.exists()
    assert not pathlib.Path(str(other) + ".migrated").exists()


def test_a_missing_file_migrates_nothing_and_writes_no_marker(script):
    module, default, marker = script
    default.unlink()
    assert module.main([]) == 0
    assert module.calls == [] and not marker.exists()
