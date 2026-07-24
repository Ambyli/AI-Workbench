"""Browser process management for cdp_interceptor.

OS-adaptive: on Windows, uses the user's installed Chrome; on Linux/mac,
uses Playwright's bundled chromium binary. Both paths expose the same CDP
endpoint over ``--remote-debugging-port``, so the rest of the library
(client.py, cdp_session.py) is OS-agnostic below this module.

Public API
----------
- ``find_browser(extra_paths=None)`` — locate a Chromium-family executable
- ``start_browser(...)`` — spawn the browser with the debug port + isolated profile
- ``clear_singleton_locks(profile_dir)`` — remove stale lock files
- ``BrowserNotFoundError`` — raised when no browser executable is found

Backwards-compat aliases (Windows-only intent):
- ``find_chrome`` = ``find_browser``
- ``start_chrome`` = ``start_browser``
- ``ChromeNotFoundError`` = ``BrowserNotFoundError``
"""

import logging
import os
import subprocess
import sys

logger = logging.getLogger("cdp_interceptor")


class BrowserNotFoundError(RuntimeError):
    """Raised when no Chromium-family executable can be located."""


# Windows-focused compat alias — older code raised this by name.
ChromeNotFoundError = BrowserNotFoundError


_DEFAULT_WINDOWS_CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]

_SINGLETON_LOCK_FILES = ("SingletonLock", "SingletonCookie", "SingletonSocket")


def _find_windows_chrome(extra_paths: list[str] | None = None) -> str | None:
    """Windows: return the first installed Chrome path that exists."""
    candidates = list(_DEFAULT_WINDOWS_CHROME_PATHS)
    env_override = os.environ.get("CHROME_PATHS_VAR")
    if env_override:
        candidates.append(os.path.expandvars(env_override))
    if extra_paths:
        candidates.extend(extra_paths)
    return next((p for p in candidates if p and os.path.exists(p)), None)


def _find_playwright_chromium() -> str | None:
    """Linux/mac: return Playwright's bundled chromium executable path.

    Requires the ``playwright`` pip package to be installed AND the chromium
    browser to have been downloaded via ``playwright install chromium``.
    Returns None if either is missing.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.debug("_find_playwright_chromium: playwright not installed")
        return None
    try:
        with sync_playwright() as p:
            # executable_path is a str attribute on the browser type; available
            # without .launch(). If chromium wasn't downloaded, the path exists
            # in Playwright's expected location but the binary file won't —
            # we validate that here.
            path = p.chromium.executable_path
        return path if path and os.path.exists(path) else None
    except Exception as exc:
        logger.debug("_find_playwright_chromium: %s", exc)
        return None


def find_browser(extra_paths: list[str] | None = None) -> str | None:
    """Return an executable path for Chrome (Windows) or Playwright's bundled
    chromium (Linux/mac). Returns None if nothing is found.

    Search order:
      * Windows → installed Chrome (Program Files, LOCALAPPDATA, ``CHROME_PATHS_VAR``, caller extras).
      * Linux/mac → Playwright's bundled chromium binary.
    """
    if sys.platform == "win32":
        return _find_windows_chrome(extra_paths)
    return _find_playwright_chromium()


def find_chrome(extra_paths: list[str] | None = None) -> str | None:
    """Backwards-compat alias for ``find_browser``. Windows-focused name."""
    return find_browser(extra_paths)


def start_browser(
    browser_path: str,
    *,
    headless: bool,
    debug_port: int,
    profile_dir: str,
    target_url: str,
) -> subprocess.Popen:
    """Launch a Chromium-family browser with remote debugging enabled and
    load *target_url* in a new window. Uses an isolated ``--user-data-dir``
    so this is a distinct process from any regular Chrome the user has open.

    Chrome and Chromium accept the same flags — the same argv works whether
    ``browser_path`` points at a Chrome install (Windows) or Playwright's
    chromium binary (Linux/mac).
    """
    args = [
        browser_path,
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
        logger.debug("launcher.start_browser: launching headless")
    else:
        logger.debug("launcher.start_browser: launching visible")

    args += ["--new-window", target_url]
    return subprocess.Popen(args, stderr=subprocess.DEVNULL)


def start_chrome(
    chrome_path: str,
    *,
    headless: bool,
    debug_port: int,
    profile_dir: str,
    target_url: str,
) -> subprocess.Popen:
    """Backwards-compat alias for ``start_browser``. Windows-focused name."""
    return start_browser(
        chrome_path,
        headless=headless,
        debug_port=debug_port,
        profile_dir=profile_dir,
        target_url=target_url,
    )


def kill_chrome_by_profile(profile_dir: str) -> int:
    """Windows-only. Find and force-kill every chrome.exe whose command line
    references *profile_dir*. Returns the number of processes killed.

    Rationale: ``taskkill /F /T /PID <main_pid>`` kills the browser's own
    process tree, but Chrome sometimes leaves updater / crashpad / utility
    processes running that aren't reachable from the main tree yet still
    hold the profile's named mutex. If any of those survive a relaunch,
    the next Chrome sees "existing browser detected" and IPC-hands-off
    the URL (exit code 21) instead of opening a visible window.

    No-op on non-Windows platforms.
    """
    if sys.platform != "win32":
        return 0

    # PowerShell reliably enumerates command lines; WMIC is deprecated as of
    # Windows 11 24H2. `-Filter` narrows on process image at the CIM layer
    # (fast); `-like` on command line is the slow bit but only runs on
    # chrome.exe results.
    escaped = profile_dir.replace("'", "''")
    script = (
        "$ErrorActionPreference='SilentlyContinue';"
        "$procs = Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" |"
        f" Where-Object {{ $_.CommandLine -like '*{escaped}*' }};"
        "foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force };"
        "Write-Output $procs.Count"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=10,
        )
        count_str = (result.stdout or "").strip().splitlines()[-1:] or ["0"]
        killed = int(count_str[0]) if count_str[0].isdigit() else 0
        logger.debug("kill_chrome_by_profile: killed=%d profile=%s", killed, profile_dir)
        return killed
    except Exception as exc:
        logger.warning("kill_chrome_by_profile failed: %s", exc)
        return 0


def clear_singleton_locks(profile_dir: str) -> None:
    """Remove SingletonLock / SingletonCookie / SingletonSocket if present.

    Stale singleton locks left over from a prior browser crash can cause the
    next launch to hand off its URL to a non-existent process. Removing them
    is safe when no browser is currently using this profile.
    """
    for lf in _SINGLETON_LOCK_FILES:
        try:
            os.remove(os.path.join(profile_dir, lf))
        except FileNotFoundError:
            pass
