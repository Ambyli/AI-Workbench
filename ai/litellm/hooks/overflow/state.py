"""What the overflow hook believes about one local backend, and the verdicts.

BUSY is a waiting queue with hysteresis, from ``<api_base minus /v1>/metrics``:
``vllm:num_requests_waiting`` > 0 continuously for >= OVERFLOW_BUSY_AFTER_S
sets it; == 0 continuously for >= OVERFLOW_IDLE_AFTER_S clears it.
DOWN is a probe that failed (connection refused, timeout, non-200).
UNKNOWN -- never probed yet, data older than 5 x the poll interval, or a 200
that carries no vLLM queue metric -- keeps the deployment: when the hook
cannot see, LiteLLM behaves exactly as it would without it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

OK, BUSY, DOWN, UNKNOWN = "ok", "busy", "down", "unknown"
# The context-fit drop reason (a hard drop, see context_fit.py).
TOO_LONG = "too-long"


# A @dataclass is safe here: the hooks package is imported normally (through
# the overflow.py shim), so this module is in sys.modules -- which
# dataclasses needs while it processes string annotations. Only the shim
# itself, which LiteLLM execs without registering, must stay free of them.
# eq=False keeps identity equality and hashing, as before it was a dataclass.
@dataclass(eq=False)
class BackendState:
    """What the hook believes about one api_base. Mutated only by apply_*."""

    api_base: str
    url: str
    probed_at: Optional[float] = None  # monotonic time of the last finished probe
    reachable: bool = False
    has_metrics: bool = False
    waiting: Optional[float] = None
    running: Optional[float] = None
    busy: bool = False
    waiting_since: Optional[float] = None
    idle_since: Optional[float] = None
    error: str = ""
    consecutive_failures: int = 0

    def apply_sample(
        self,
        now: float,
        waiting: Optional[float],
        running: Optional[float],
        busy_after: float,
        idle_after: float,
    ) -> None:
        """Fold one successful /metrics read into the hysteresis state."""
        self.probed_at = now
        self.reachable = True
        self.error = ""
        self.consecutive_failures = 0
        self.running = running
        if waiting is None:
            # A 200 without the queue metric: not evidence of anything.
            self.has_metrics = False
            self.waiting = None
            self.busy = False
            self.waiting_since = self.idle_since = None
            return
        self.has_metrics = True
        self.waiting = waiting
        if waiting > 0:
            self.idle_since = None
            if self.waiting_since is None:
                self.waiting_since = now
            if not self.busy and now - self.waiting_since >= busy_after:
                self.busy = True
        else:
            self.waiting_since = None
            if self.idle_since is None:
                self.idle_since = now
            if self.busy and now - self.idle_since >= idle_after:
                self.busy = False

    def apply_failure(self, now: float, error: str) -> None:
        """A failed probe means DOWN. Hysteresis restarts when it comes back."""
        self.probed_at = now
        self.reachable = False
        self.has_metrics = False
        self.error = error[:200]
        self.consecutive_failures += 1
        self.waiting = self.running = None
        self.busy = False
        self.waiting_since = self.idle_since = None

    def verdict(self, now: float, stale_after: float) -> str:
        if self.probed_at is None or now - self.probed_at > stale_after:
            return UNKNOWN
        if not self.reachable:
            return DOWN
        if not self.has_metrics:
            return UNKNOWN
        return BUSY if self.busy else OK

    def snapshot(self, now: float, stale_after: float) -> Dict[str, Any]:
        return {
            "url": self.url,
            "verdict": self.verdict(now, stale_after),
            "age_s": None if self.probed_at is None else round(now - self.probed_at, 2),
            "waiting": self.waiting,
            "running": self.running,
            "busy": self.busy,
            "waiting_for_s": None if self.waiting_since is None else round(now - self.waiting_since, 2),
            "idle_for_s": None if self.idle_since is None else round(now - self.idle_since, 2),
            "error": self.error,
            "consecutive_failures": self.consecutive_failures,
        }
