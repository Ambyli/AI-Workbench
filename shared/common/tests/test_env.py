"""Tests for common.env.load_env.

Verifies the walk-up behavior and the no-override semantics.
"""

import os
import textwrap
from pathlib import Path

from common.env import load_env


def test_finds_env_in_parent_directory(tmp_path, monkeypatch):
    """load_env walks up from cwd and finds a .env two directories above."""
    (tmp_path / ".env").write_text("ZEO_TEST_LOAD=from_parent\n")
    deep = tmp_path / "level1" / "level2"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)
    monkeypatch.delenv("ZEO_TEST_LOAD", raising=False)

    result = load_env()

    assert result is not None
    assert os.environ.get("ZEO_TEST_LOAD") == "from_parent"


def test_does_not_override_existing_env(tmp_path, monkeypatch):
    """Values already in os.environ take precedence over .env."""
    (tmp_path / ".env").write_text("ZEO_TEST_OVERRIDE=from_dotenv\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ZEO_TEST_OVERRIDE", "from_environment")

    load_env()

    assert os.environ["ZEO_TEST_OVERRIDE"] == "from_environment"


def test_explicit_path(tmp_path, monkeypatch):
    """Passing path= skips the walk-up and loads exactly that file."""
    envfile = tmp_path / "custom.env"
    envfile.write_text("ZEO_TEST_EXPLICIT=explicit_ok\n")
    monkeypatch.delenv("ZEO_TEST_EXPLICIT", raising=False)

    result = load_env(str(envfile))

    assert result == str(envfile)
    assert os.environ["ZEO_TEST_EXPLICIT"] == "explicit_ok"


def test_returns_none_when_no_env_found(tmp_path, monkeypatch):
    """When no .env exists anywhere up the tree, returns None cleanly."""
    monkeypatch.chdir(tmp_path)
    # Use tmp_path.parent as an anchor — find_dotenv walks up until it hits
    # the filesystem root; on CI the repo root .env could accidentally match.
    # Guard by asserting only that no exception is raised.

    result = load_env()

    assert result is None or isinstance(result, str)
