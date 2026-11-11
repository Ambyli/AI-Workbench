"""
app.FIELD_MAPPING_PATH: where the milestone map is read from.

  * The default is the ``config/field_mapping.json`` shipped beside
    ``app.py`` -- it used to be ``/app/config/field_mapping.json``, which the
    image never had (Dockerfile.roofix copies the file to
    ``/app/ai/roofix/config/``), so the map always loaded as ``{}``.
  * The default resolves to the same place inside the image: computed from
    the Dockerfile's WORKDIR / COPY lines, app.py's directory + ``config/``
    is where the config directory is copied.
  * ``FIELD_MAPPING_PATH`` still overrides it; an empty value means unset.
  * What loads today is pinned: the shipped file is still Michael's stub
    (every milestone ``phoenix_block_TBD`` / status 0), which resolves to no
    Phoenix process block, so ``update_milestone`` writes nothing. Filling it
    in makes milestone writes real once ``DRY_RUN=false`` -- this case fails
    then, on purpose, so the change is a deliberate one.

Offline: imports app.py (no Phoenix, Gmail or database is touched); the env
cases run ``import app`` in a subprocess so each reads its own environment.

Run from ai/roofix/:
    PYTHONPATH=. python tests/test_field_mapping.py
"""

from __future__ import annotations

import json
import os
import posixpath
import subprocess
import sys
import tempfile
from pathlib import Path

# Set env BEFORE importing anything that reads it.
os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="roofix_field_mapping_"))
os.environ.pop("FIELD_MAPPING_PATH", None)

ROOFIX = Path(__file__).resolve().parent.parent
SHIPPED = ROOFIX / "config" / "field_mapping.json"
CASES: list = []


def case(label: str):
    def deco(fn):
        CASES.append((label, fn))
        return fn

    return deco


def _import_app_path(env_value) -> str:
    """``app.FIELD_MAPPING_PATH`` as a fresh process sees it, with
    FIELD_MAPPING_PATH set to ``env_value`` (None: unset)."""
    env = dict(os.environ)
    env["LOG_DIR"] = tempfile.mkdtemp(prefix="roofix_field_mapping_sub_")   # this process holds its own log open
    env.pop("FIELD_MAPPING_PATH", None)
    if env_value is not None:
        env["FIELD_MAPPING_PATH"] = env_value
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(ROOFIX), env.get("PYTHONPATH", "")]))
    out = subprocess.run(
        [sys.executable, "-c", "import app; print(app.FIELD_MAPPING_PATH)"],
        cwd=ROOFIX, env=env, capture_output=True, text=True, timeout=120,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    return out.stdout.strip().splitlines()[-1]


@case("the default is the shipped config/field_mapping.json beside app.py")
def _test_default_is_the_shipped_file():
    import app

    assert Path(app.DEFAULT_FIELD_MAPPING_PATH) == SHIPPED, app.DEFAULT_FIELD_MAPPING_PATH
    assert SHIPPED.is_file()
    assert Path(app.FIELD_MAPPING_PATH) == SHIPPED, app.FIELD_MAPPING_PATH


@case("in the image, app.py's directory + config/ is where Dockerfile.roofix copies the map")
def _test_default_matches_the_image():
    workdir, copies = "/", {}
    for line in (ROOFIX / "Dockerfile.roofix").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("WORKDIR "):
            workdir = posixpath.join(workdir, line.split(None, 1)[1])
        elif line.startswith("COPY ") and "--from" not in line:
            src, dest = line.split()[1:3]
            copies[src.rstrip("/")] = posixpath.normpath(posixpath.join(workdir, dest))
    app_dest = copies["ai/roofix/app.py"]                    # a directory destination ("./ai/roofix/")
    app_file = app_dest if app_dest.endswith(".py") else posixpath.join(app_dest, "app.py")
    in_image = posixpath.join(posixpath.dirname(app_file), "config", "field_mapping.json")
    assert in_image == "/app/ai/roofix/config/field_mapping.json", in_image
    assert posixpath.join(copies["ai/roofix/config"], "field_mapping.json") == in_image, copies


@case("FIELD_MAPPING_PATH overrides the default; unset or empty means the default")
def _test_env_override():
    assert Path(_import_app_path(None)) == SHIPPED
    assert Path(_import_app_path("")) == SHIPPED
    assert _import_app_path("/config/field_mapping.json") == "/config/field_mapping.json"


@case("_load_milestone_map reads the file; a missing or malformed one is {}")
def _test_load():
    import app

    with tempfile.TemporaryDirectory() as tmp:
        good, bad = Path(tmp) / "good.json", Path(tmp) / "bad.json"
        good.write_text(json.dumps({"milestones": {"HIC Executed": {"block_name": "B", "status_id": 3}}}),
                        encoding="utf-8")
        bad.write_text("{not json", encoding="utf-8")
        saved = app.FIELD_MAPPING_PATH
        try:
            app.FIELD_MAPPING_PATH = str(good)
            assert app._load_milestone_map() == {"HIC Executed": {"block_name": "B", "status_id": 3}}
            app.FIELD_MAPPING_PATH = str(bad)
            assert app._load_milestone_map() == {}
            app.FIELD_MAPPING_PATH = str(Path(tmp) / "missing.json")
            assert app._load_milestone_map() == {}
        finally:
            app.FIELD_MAPPING_PATH = saved


@case("what loads by default is the shipped stub: 8 milestones, all phoenix_block_TBD / status 0")
def _test_what_loads():
    import app

    loaded = app._load_milestone_map()
    assert loaded == json.loads(SHIPPED.read_text(encoding="utf-8"))["milestones"]
    assert sorted(loaded) == sorted([
        "HIC Executed", "Job Scheduled", "Install Date", "Job In Progress", "Job Is Complete",
        "Deposit Invoice Sent", "Deposit Invoice Paid", "Job Approval Confirmed",
    ]), sorted(loaded)
    placeholders = {(m["block_name"], m["status_id"]) for m in loaded.values()}
    assert placeholders == {("phoenix_block_TBD", 0)}, (
        "field_mapping.json now holds real Phoenix blocks: with DRY_RUN=false update_milestone "
        f"WRITES them -- check them with Michael, then update this case: {sorted(placeholders)}")
    assert set(json.loads(SHIPPED.read_text(encoding="utf-8"))["project_creation"].values()) == {"phoenix_field_TBD"}


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
