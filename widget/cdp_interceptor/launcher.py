"""Chrome process management for cdp_interceptor.

Public API
----------
- ``find_chrome(extra_paths=None)`` — locate the Chrome executable
- ``start_chrome(...)`` — spawn Chrome with the debug port + isolated profile
- ``clear_singleton_locks(profile_dir)`` — remove stale lock files
- ``ChromeNotFoundError`` — raised when no Chrome executable is found
"""

import logging
import os
import subprocess

logger = logging.getLogger("cdp_interceptor")


class ChromeNotFoundError(RuntimeError):
    """Raised when no Chrome executable can be located."""


_DEFAULT_CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]

_SINGLETON_LOCK_FILES = ("SingletonLock", "SingletonCookie", "SingletonSocket")


def find_chrome(extra_paths: list[str] | None = None) -> str | None:
    """Return the first Chrome executable path that exists, or None.

    Searches the built-in default paths (Program Files, Program Files (x86),
    %LOCALAPPDATA%), plus any extras passed by the caller. Also honors the
    ``CHROME_PATHS_VAR`` environment variable if set.
    """
    candidates = list(_DEFAULT_CHROME_PATHS)
    env_override = os.environ.get("CHROME_PATHS_VAR")
    if env_override:
        candidates.append(os.path.expandvars(env_override))
    if extra_paths:
        candidates.extend(extra_paths)
    return next((p for p in candidates if p and os.path.exists(p)), None)


def start_chrome(
    chrome_path: str,
    *,
    headless: bool,
    debug_port: int,
    profile_dir: str,
    target_url: str,
) -> subprocess.Popen:
    """Launch Chrome with remote debugging enabled and load *target_url* in a
    new window. Uses an isolated ``--user-data-dir`` so the launched Chrome
    is a distinct process from any regular Chrome the user has open.
    """
    args = [
        chrome_path,
        f"--remote-debugging-port={debug_port}",
        f"--remote-allow-origins=http://localhost:{debug_port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--no-restore-last-session",
        "--disable-session-crashed-bubble",
    ]
    if headless:
        args += [
            "--headless=new",
            "--disable-gpu",
            "--window-size=1920,1080",
            "--disable-blink-features=AutomationControlled",
            (
                "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/134.0.0.0 Safari/537.36"
            ),
        ]
        logger.debug("launcher.start_chrome: launching headless")
    else:
        logger.debug("launcher.start_chrome: launching visible")
    args += ["--new-window", target_url]
    return subprocess.Popen(args, stderr=subprocess.DEVNULL)


def clear_singleton_locks(profile_dir: str) -> None:
    """Remove SingletonLock / SingletonCookie / SingletonSocket if present.

    Stale singleton locks left over from a prior Chrome crash can cause the
    next launch to hand off its URL to a non-existent process. Removing them
    is safe when no Chrome is currently using this profile.
    """
    for lf in _SINGLETON_LOCK_FILES:
        try:
            os.remove(os.path.join(profile_dir, lf))
        except FileNotFoundError:
            pass
