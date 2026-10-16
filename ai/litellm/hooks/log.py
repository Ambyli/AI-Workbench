"""The hooks' one logger, and the throttle every decision line goes through.

Everything logs on ``zeo.overflow`` with an ``[overflow]`` prefix -- the name
predates the package and is what ``make logs litellm | grep '\\[overflow\\]'``
and the docs search for, so it stays.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, Callable, Dict, Tuple

logger = logging.getLogger("zeo.overflow")
if not logger.handlers:
    # Own handler: LiteLLM's loggers may sit at WARNING, and a spill that is
    # not in `make logs litellm` is a spill nobody can audit.
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [overflow] %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)
    logger.propagate = False

# At most one log line per (alias, deployment, reason) per this many seconds.
LOG_EVERY_S = 30.0


class ThrottledLog:
    """logger.info, at most once per key per LOG_EVERY_S, with a "(+N similar)"
    count on the next line that gets through. One instance is shared by every
    component of a handler, so the keys of the spill, context-fit, route and
    footer lines share one table (they never collide: their reasons differ)."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._last: Dict[Tuple[str, str, str], float] = {}
        self._suppressed: Dict[Tuple[str, str, str], int] = {}

    def __call__(self, alias: str, dep: str, reason: str, fmt: str, *args: Any) -> None:
        key = (alias, dep, reason)
        now = self._clock()
        last = self._last.get(key)
        if last is not None and now - last < LOG_EVERY_S:
            self._suppressed[key] = self._suppressed.get(key, 0) + 1
            return
        suppressed = self._suppressed.pop(key, 0)
        self._last[key] = now
        if suppressed:
            fmt += " (+%d similar in the last %ds)"
            args = args + (suppressed, int(LOG_EVERY_S))
        logger.info(fmt, *args)
