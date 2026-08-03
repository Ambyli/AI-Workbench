"""
Named-profile management for interceptor.

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
import sys
import tarfile
import time
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4


PROFILES_ROOT = os.environ.get("INTERCEPTOR_PROFILES_ROOT", "/data/profiles")

# Temp-profile clones live under a leading-dot subdirectory so they never
# collide with a real profile name (validator rejects leading dots).
TEMP_ROOT = Path(PROFILES_ROOT) / ".temp"

# Profile names are used as directory names, so we restrict them to a safe
# alphabet. Anchored full-match — no path separators, no leading dots.
_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")

# Chrome's process-lock artifacts. SingletonSocket is a Unix domain socket
# (copytree's copy2 raises ENXIO on it). SingletonLock and SingletonCookie
# are symlinks to <hostname>-<pid> of the Chrome that owns the profile —
# dangling the instant that Chrome exits, which trips copytree's default
# follow-symlinks behavior with FileNotFoundError. None are useful in a
# clone: they encode "this pid holds the lock," which is false by
# definition once you copy the dir, and the launcher clears any lingering
# SingletonLock before opening Chrome anyway.
_SINGLETON_FILES = frozenset({"SingletonSocket", "SingletonLock", "SingletonCookie"})


def _ignore_chrome_singletons(_src: str, names: list[str]) -> set[str]:
    """``shutil.copytree`` ignore callable — filters out the three Chrome
    singleton artifacts. See ``_SINGLETON_FILES`` for why."""
    return {n for n in names if n in _SINGLETON_FILES}


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


# ── Temp clones for same-profile concurrent captures ───────────────────────
def clone_profile(name: str) -> Path:
    """Copy ``PROFILES_ROOT/name`` into a fresh ``TEMP_ROOT/temp_profile_<uuid>``
    and return the temp dir's path.

    Callers use this on the slow path when the base profile is already in use
    by another concurrent capture. See ``ai/interceptor/app.py`` for the
    fast/slow path logic.

    ``shutil.copytree`` of a live profile is safe for Chrome's on-disk stores
    (SQLite ``Cookies``, LevelDB ``Local Storage`` / ``IndexedDB``) — they all
    use journal-based crash recovery, so a mid-write snapshot at worst yields
    a slightly-stale-but-consistent state, never corruption. What isn't safe
    is copying Chrome's process-lock files (``Singleton*``); those get
    filtered out via ``_ignore_chrome_singletons``.

    On any copy failure the partial temp dir is removed before re-raising, so
    the caller can treat this function as atomic — either it returns a good
    temp dir or it raises with nothing left behind under ``TEMP_ROOT``.
    """
    src = profile_path(name)
    if not src.is_dir():
        raise FileNotFoundError(f"profile {name!r} not present at {src}")
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    temp = TEMP_ROOT / f"temp_profile_{uuid4().hex}"
    try:
        shutil.copytree(src, temp, ignore=_ignore_chrome_singletons)
    except Exception:
        shutil.rmtree(temp, ignore_errors=True)
        raise
    return temp


def remove_temp_profile(temp_path: Path) -> None:
    """Best-effort rmtree with a brief retry loop.

    On Windows (and occasionally Linux under high load) Chrome child processes
    can hold file handles on the profile dir for a fraction of a second after
    the launcher process exits. Retry a few times before giving up.
    """
    for attempt in range(5):
        try:
            shutil.rmtree(temp_path)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            time.sleep(0.1 * (attempt + 1))
        except OSError:
            time.sleep(0.1 * (attempt + 1))
    # Final give-up — ignore_errors so the request still returns cleanly.
    shutil.rmtree(temp_path, ignore_errors=True)


def sweep_temp_profiles() -> int:
    """Delete every child of ``TEMP_ROOT`` — called at startup to clean up
    clones orphaned by a prior process crash. Returns the count removed."""
    if not TEMP_ROOT.is_dir():
        return 0
    count = 0
    for child in TEMP_ROOT.iterdir():
        try:
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
            count += 1
        except Exception as exc:
            print(
                f"[interceptor] sweep_temp_profiles: could not remove {child}: {exc}",
                file=sys.stderr,
                flush=True,
            )
    return count
