"""Session sentinel — a marker file inside the Chrome profile directory that
records "we've had at least one successful login on this profile". Presence
means future launches can go straight to headless; absence means Chrome must
launch visibly so the user can log in.
"""

import logging
import os

logger = logging.getLogger("cdp_interceptor")

_SESSION_SENTINEL = "session_ok"


def session_exists(profile_dir: str) -> bool:
    """True if a previous successful capture left a sentinel file in *profile_dir*."""
    return os.path.exists(os.path.join(profile_dir, _SESSION_SENTINEL))


def mark_session_ok(profile_dir: str) -> None:
    """Write the sentinel file so future launches can be headless."""
    try:
        open(os.path.join(profile_dir, _SESSION_SENTINEL), "w").close()
    except Exception as exc:
        logger.warning("sentinel.mark_session_ok: could not write sentinel: %s", exc)


def clear_session(profile_dir: str) -> None:
    """Remove the sentinel file (session expired / login required)."""
    try:
        os.remove(os.path.join(profile_dir, _SESSION_SENTINEL))
    except FileNotFoundError:
        pass
