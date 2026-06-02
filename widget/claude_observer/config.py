"""
config.py
---------
Loads settings from config.json (project root) and exposes all configuration
as module-level attributes.  Values are live-mutable: call apply_updates() to
change them in-place so every consumer sees the new values immediately.
"""

import json
import os
import sys
from pathlib import Path

_CONFIG_PATH = Path(__file__).parent.parent.parent / "config.json"
_ENV_PATH = Path(__file__).parent.parent.parent / ".env"

_DEFAULTS: dict = {
    "REFRESH_INTERVAL_SECONDS": 300,
    "INCLUDE_PATHS": "",
    "EXCLUDE_WEEKDAYS": "5,6",
    "CONSOLE_FETCHER_ENABLED": False,
    "BROWSER_DEBUG_PORT": 9222,
    "LLAMA_SERVER_CMD": "",
    "LLM_LOG_MAX_LINES": 200,
    "LLM_URL": "http://localhost:8001",
    "LLM_API_KEY": "sk-no-key-required",
    "LLM_MODEL": "",
    "DEBUG_LOGGING": False,
    "KEEP_LLM_ACTIVE": False,
}


def _migrate_env() -> dict:
    """Read .env key=value pairs and coerce types to match _DEFAULTS."""
    env: dict = {}
    if not _ENV_PATH.exists():
        return env
    for line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if k not in _DEFAULTS:
            continue
        default = _DEFAULTS[k]
        if isinstance(default, bool):
            env[k] = v.lower() == "true"
        elif isinstance(default, int):
            try:
                env[k] = int(v)
            except ValueError:
                pass
        else:
            env[k] = v
    return env


def _load_raw() -> dict:
    raw = dict(_DEFAULTS)
    if _CONFIG_PATH.exists():
        try:
            raw.update(json.loads(_CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    elif _ENV_PATH.exists():
        raw.update(_migrate_env())
        _CONFIG_PATH.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    return raw


def _apply(raw: dict) -> None:
    mod = sys.modules[__name__]
    mod._raw = raw

    mod.REFRESH_INTERVAL_SECONDS = int(raw.get("REFRESH_INTERVAL_SECONDS", 300))
    mod.CONSOLE_FETCHER_ENABLED  = bool(raw.get("CONSOLE_FETCHER_ENABLED", False))
    mod.BROWSER_DEBUG_PORT       = int(raw.get("BROWSER_DEBUG_PORT", 9222))
    mod.LLAMA_SERVER_CMD         = str(raw.get("LLAMA_SERVER_CMD", ""))
    mod.LLM_LOG_MAX_LINES        = int(raw.get("LLM_LOG_MAX_LINES", 200))
    mod.LLM_URL                  = str(raw.get("LLM_URL", "http://localhost:8001"))
    mod.LLM_API_KEY              = str(raw.get("LLM_API_KEY", "sk-no-key-required"))
    mod.LLM_MODEL                = str(raw.get("LLM_MODEL", ""))
    mod.DEBUG_LOGGING            = bool(raw.get("DEBUG_LOGGING", False))
    mod.KEEP_LLM_ACTIVE          = bool(raw.get("KEEP_LLM_ACTIVE", False))

    # Parsed from comma-separated raw strings
    raw_paths = str(raw.get("INCLUDE_PATHS", ""))
    mod.INCLUDE_PATHS   = [p.strip().lower() for p in raw_paths.split(",") if p.strip()]

    raw_excl = str(raw.get("EXCLUDE_WEEKDAYS", "5,6"))
    mod.EXCLUDE_WEEKDAYS = {int(d.strip()) for d in raw_excl.split(",") if d.strip()}

    # Computed — not stored in JSON
    mod.CLAUDE_DIR          = os.path.join(os.path.expanduser("~"), ".claude", "projects")
    mod.BROWSER_PROFILE_DIR = os.path.join(os.path.expanduser("~"), ".claude_widget", "chrome_profile")


def save() -> None:
    """Persist the current _raw dict to config.json."""
    mod = sys.modules[__name__]
    _CONFIG_PATH.write_text(json.dumps(mod._raw, indent=2), encoding="utf-8")


def apply_updates(updates: dict) -> None:
    """
    Merge *updates* into the live config, re-apply all module attrs, and save.
    Keys must match _DEFAULTS keys; INCLUDE_PATHS and EXCLUDE_WEEKDAYS are raw
    comma-separated strings here.
    """
    mod = sys.modules[__name__]
    raw = dict(mod._raw)
    raw.update(updates)
    _apply(raw)
    save()


# ── Bootstrap ─────────────────────────────────────────────────────────────────
_apply(_load_raw())
