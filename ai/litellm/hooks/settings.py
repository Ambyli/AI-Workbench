"""Every environment variable the hooks read -- here and nowhere else, read
ONCE when this module is first imported (the proxy imports it at startup, so
a change needs `make up litellm`). The values are the defaults of
Handler(...); a test passes its own instead of touching the environment.

Passed through ai/litellm/docker-compose.litellm.yml's `environment:` block;
documented in CLAUDE.md's config table and ai/litellm/LITELLM.md.
"""

from __future__ import annotations

import os

from .routing.code_route import (
    DEFAULT_CODE_ROUTE_ROUTER_CONFIG,
    DEFAULT_CODE_ROUTE_TOOL_REGEX,
    compile_code_route_regex,
    parse_code_route_cooldown,
)


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.environ.get(name, "")
    try:
        value = float(raw) if raw.strip() else default
    except ValueError:
        value = default
    return max(minimum, value)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


# -- The overflow hook (hooks/overflow) -----------------------------------------
BUSY_AFTER_S = _env_float("OVERFLOW_BUSY_AFTER_S", 2.0)
IDLE_AFTER_S = _env_float("OVERFLOW_IDLE_AFTER_S", 5.0)
POLL_INTERVAL_S = _env_float("OVERFLOW_POLL_INTERVAL_S", 1.0, minimum=0.2)
PROBE_TIMEOUT_S = _env_float("OVERFLOW_PROBE_TIMEOUT_S", 0.75, minimum=0.1)

# Context fit (hooks/overflow/context_fit.py). The estimate runs HIGH on
# purpose: English is ~4 chars/token and JSON adds keys and escapes, so 2.5
# chars/token overestimates almost any prompt -- a short chat never makes the
# extra call.
TOKENIZE_MIN_FRACTION = _env_float("OVERFLOW_TOKENIZE_MIN_FRACTION", 0.6)
TOKENIZE_CHARS_PER_TOKEN = _env_float("OVERFLOW_TOKENIZE_CHARS_PER_TOKEN", 2.5, minimum=0.5)
TOKENIZE_TIMEOUT_S = _env_float("OVERFLOW_TOKENIZE_TIMEOUT_S", 5.0, minimum=0.1)

# -- The `auto` footer (hooks/footer) --------------------------------------------
AUTO_FOOTER_ENABLED = _env_bool("AUTO_FOOTER_ENABLED", True)

# -- The code-tool remap (hooks/routing/code_route.py) --------------------------
# Unset -> the default; set but empty -> remap off (the compose file passes
# ${CODE_ROUTE_TOOL_REGEX-<default>}, so only an explicit empty value in .env
# turns it off). Invalid -> off, with the error logged when the handler is built.
CODE_ROUTE_TOOL_REGEX, CODE_ROUTE_TOOL_REGEX_ERROR = compile_code_route_regex(
    os.environ.get("CODE_ROUTE_TOOL_REGEX", DEFAULT_CODE_ROUTE_TOOL_REGEX)
)
CODE_ROUTE_COOLDOWN, CODE_ROUTE_COOLDOWN_ERROR = parse_code_route_cooldown(os.environ.get("CODE_ROUTE_COOLDOWN"))
CODE_ROUTE_ROUTER_CONFIG = os.environ.get("CODE_ROUTE_ROUTER_CONFIG", DEFAULT_CODE_ROUTE_ROUTER_CONFIG)
