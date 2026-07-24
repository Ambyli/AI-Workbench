"""
Chrome profile directory management for the Roofix scraper.

`cdp_interceptor` uses `--user-data-dir` for session persistence — cookies,
localStorage, and login state live inside that dir. The scraper reuses one
profile dir across requests so a captured Roofix session stays warm across
proposal fetches.

Refresh flow (operator action; container can't present a login UI):
1. On a laptop, run `cdp-spy --url https://roofix.io --profile-dir <path>`.
   Log in interactively.
2. `tar czf profile.tgz -C <path> .`
3. POST profile.tgz to `/profile/refresh` — the endpoint unpacks it into
   `PROFILE_DIR`, replacing whatever was there.
"""

from __future__ import annotations

import os
import shutil
import tarfile
from pathlib import Path
from typing import BinaryIO


PROFILE_DIR = os.environ.get("ROOFIX_PROFILE_DIR", "/data/roofix_profile")


def profile_exists() -> bool:
    """Non-empty profile dir counts as present. An empty dir means no session."""
    p = Path(PROFILE_DIR)
    if not p.is_dir():
        return False
    return any(p.iterdir())


def profile_info() -> dict:
    """Diagnostic info for GET /profile."""
    p = Path(PROFILE_DIR)
    if not p.is_dir():
        return {"path": PROFILE_DIR, "present": False, "size_bytes": 0}
    size = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    return {
        "path": PROFILE_DIR,
        "present": any(p.iterdir()),
        "size_bytes": size,
        "sentinel_present": (p / "session_ok").exists(),
    }


def unpack_profile(archive: BinaryIO) -> dict:
    """Extract a .tgz over PROFILE_DIR.

    The existing profile dir is wiped first so stale files (e.g. an old
    SingletonLock) don't leak into the new session.
    """
    p = Path(PROFILE_DIR)
    if p.exists():
        shutil.rmtree(p)
    p.mkdir(parents=True, exist_ok=True)

    with tarfile.open(fileobj=archive, mode="r:*") as tf:
        # Filter is safer than raw extractall — refuses paths with .. or
        # absolute paths (default in Python 3.12+, explicit here for clarity).
        tf.extractall(p, filter="data")

    return profile_info()
