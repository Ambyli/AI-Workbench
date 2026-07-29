"""
Named-profile management for interceptor-api.

Each named profile is a Chrome ``--user-data-dir`` under ``PROFILES_ROOT``.
Keeping them separate means one container can hold live sessions for
multiple sites at once (``gmail``, ``github``, ``roofix``, …) without them
colliding.

Refresh flow (operator action; container can't show a login UI):

1. Locally: ``cdp-spy --url https://target.example.com --profile-dir C:\\tmp\\example``
   (from ``shared/common/src/common/cdp_interceptor/spy.py``) — log in.
2. ``tar czf example.tgz -C C:\\tmp\\example .``
3. ``curl -F archive=@example.tgz http://<host>:8080/profiles/example/refresh``

The endpoint wipes ``PROFILES_ROOT/example`` and unpacks the archive there.
``InterceptorClient.launch`` clears any lingering ``SingletonLock`` before
opening Chrome, so the freshly-extracted profile is safe to boot into.
"""

from __future__ import annotations

import os
import re
import shutil
import tarfile
from pathlib import Path
from typing import BinaryIO


PROFILES_ROOT = os.environ.get("INTERCEPTOR_PROFILES_ROOT", "/data/profiles")

# Profile names are used as directory names, so we restrict them to a safe
# alphabet. Anchored full-match — no path separators, no leading dots.
_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


class InvalidProfileNameError(ValueError):
    pass


def validate_name(name: str) -> str:
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise InvalidProfileNameError(
            f"invalid profile name {name!r}: must match [a-z0-9][a-z0-9_-]{{0,63}}"
        )
    return name


def profile_path(name: str) -> Path:
    validate_name(name)
    return Path(PROFILES_ROOT) / name


def _dir_size(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def profile_info(name: str) -> dict:
    p = profile_path(name)
    if not p.is_dir():
        return {"name": name, "path": str(p), "present": False, "size_bytes": 0}
    return {
        "name": name,
        "path": str(p),
        "present": any(p.iterdir()),
        "size_bytes": _dir_size(p),
        "sentinel_present": (p / "session_ok").exists(),
    }


def list_profiles() -> list[dict]:
    root = Path(PROFILES_ROOT)
    if not root.is_dir():
        return []
    out: list[dict] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        # Skip any dir whose name doesn't match our validator — someone put a
        # stray directory in the volume; don't advertise it as a profile.
        if not _NAME_RE.fullmatch(child.name):
            continue
        out.append(profile_info(child.name))
    return out


def unpack_profile(name: str, archive: BinaryIO) -> dict:
    """Wipe ``PROFILES_ROOT/name`` then extract a .tgz over it.

    Writes the ``session_ok`` sentinel after extraction so subsequent
    ``InterceptorClient`` launches go straight to headless — an operator only
    ever uploads a profile *after* successfully logging in on their laptop,
    so treating uploaded profiles as session-ready by definition matches
    reality. Without this, headless-container launches would fail because
    the client's headless gate is ``session_sentinel AND session_exists``.
    """
    p = profile_path(name)
    if p.exists():
        shutil.rmtree(p)
    p.mkdir(parents=True, exist_ok=True)

    with tarfile.open(fileobj=archive, mode="r:*") as tf:
        # ``filter="data"`` refuses paths with .. / absolute paths / device files —
        # default in Python 3.12+, explicit here (matches roofix scraper).
        tf.extractall(p, filter="data")

    (p / "session_ok").touch()

    return profile_info(name)


def delete_profile(name: str) -> dict:
    p = profile_path(name)
    existed = p.exists()
    if existed:
        shutil.rmtree(p)
    return {"name": name, "deleted": existed}
