"""Make the classifier's flat modules importable, on a throwaway /data and a
throwaway Postgres database.

`ai/classifier` is a flat package: its modules import each other by bare name
(``from config import ...``), which is what the container's WORKDIR gives
them. A test run from the repo root has neither, so this file supplies both —
and it has to do it at IMPORT time, before any test module runs, because
``config.py`` reads ``CLASSIFIER_DATA_DIR`` / ``PAYLOAD_DIR`` /
``CLASSIFIER_ARTIFACT_DIR`` / ``CLASSIFIER_REFERENCE_DIR`` and the
``CLASSIFIER_DB_*`` connection settings once at import and every other
module reads config.

**Files.** The defaults are absolute container paths (``/data/...``). Left
alone, a test that touched the store would try to create ``/data`` on the
developer's machine, so all of them are pointed at one temp directory for the
whole session. It is deliberately NOT a ``tmp_path`` fixture: the values are
frozen into module constants at import, so a per-test directory would be
ignored by everything that matters.

**The database.** The classifier keeps its job queue, references and
model-usage rows in Postgres and nowhere else. With
``TEST_POSTGRES_DSN`` set (the same variable
``shared/common/tests/test_jobs_postgres.py`` uses), this file creates one
uniquely named database for the session — ``classifier_test_<pid>_<hex>`` —
points ``CLASSIFIER_DB_*`` at it, and drops it in ``pytest_sessionfinish``
(see ``pg_testdb.py``). Without it, ``CLASSIFIER_DB_HOST`` is forced EMPTY —
so no test can ever reach a real database a developer's shell happens to
point at — and every test marked ``postgres`` is skipped, the same way the
shared Postgres tests skip.

Mark a test (or a whole module, ``pytestmark = pytest.mark.postgres``) when it
runs the app's lifespan (``with TestClient(main.app)``) or reads or writes any
of the three tables; a fixture that runs the app requests ``need_postgres``
instead, which skips every test that uses it.

Run the suite with::

    TEST_POSTGRES_DSN=postgresql://postgres@localhost:5432/postgres \\
    UV_LINK_MODE=copy uv run --no-sync --with pytest --package classifier \\
        python -m pytest unit-tests/classifier -q -p no:cacheprovider
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_CLASSIFIER = _ROOT / "ai" / "classifier"
_HERE = pathlib.Path(__file__).resolve().parent

if str(_CLASSIFIER) not in sys.path:
    sys.path.insert(0, str(_CLASSIFIER))
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from pg_testdb import DB_ENV_KEYS, ThrowawayDatabase  # noqa: E402

# One directory for the session, cleaned up by the OS rather than by us: a
# test that fails mid-write should leave its artifacts where they can be
# looked at.
_DATA = pathlib.Path(tempfile.mkdtemp(prefix="classifier-tests-"))
os.environ.setdefault("CLASSIFIER_DATA_DIR", str(_DATA))
os.environ.setdefault("PAYLOAD_DIR", str(_DATA / "payloads"))
os.environ.setdefault("CLASSIFIER_ARTIFACT_DIR", str(_DATA / "artifacts"))
# References have their own, never-swept root (config.REFERENCE_DIR).
os.environ.setdefault("CLASSIFIER_REFERENCE_DIR", str(_DATA / "references"))
# No OCR models in a unit test: loading three ONNX graphs costs a second and
# nothing here asks a question that needs them.
os.environ.setdefault("CLASSIFIER_OCR_ENGINE", "none")
# The detector is a network dependency. Empty means "off", which is the
# state every test in this directory assumes unless it stubs the client.
os.environ.setdefault("DETECTOR_URL", "")

# The session database. Assigned, never setdefault: a test run must not
# inherit a CLASSIFIER_DB_HOST pointing at a real database.
_TEST_DSN = os.environ.get("TEST_POSTGRES_DSN", "").strip()
_SESSION_DB = ThrowawayDatabase(_TEST_DSN).create() if _TEST_DSN else None
if _SESSION_DB is not None:
    os.environ.update(_SESSION_DB.env())
else:
    for _key in DB_ENV_KEYS:
        os.environ.pop(_key, None)
    os.environ["CLASSIFIER_DB_HOST"] = ""

HAVE_POSTGRES = _SESSION_DB is not None


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "postgres: needs the classifier's Postgres (TEST_POSTGRES_DSN); skipped without it",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if HAVE_POSTGRES:
        return
    skip = pytest.mark.skip(reason="TEST_POSTGRES_DSN not set")
    for item in items:
        if "postgres" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def need_postgres() -> None:
    """Request it from a fixture that runs the app (``def client(need_postgres)``)
    and every test using that fixture skips without the database — the
    fixture-level twin of the ``postgres`` marker."""
    if not HAVE_POSTGRES:
        pytest.skip("TEST_POSTGRES_DSN not set")


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if _SESSION_DB is not None:
        _SESSION_DB.drop()
