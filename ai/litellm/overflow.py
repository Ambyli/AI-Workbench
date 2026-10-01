"""Overflow hook for LiteLLM chain aliases -- spill to the next `order` when a
local vLLM backend is queueing or down.

Registered in ai/litellm/litellm_config.yaml as::

    litellm_settings:
      callbacks: ["overflow.handler"]

and mounted at /app/overflow.py, next to /app/config.yaml, by
ai/litellm/docker-compose.litellm.yml. LiteLLM's get_instance_fn resolves
"overflow.handler" to <dirname(config)>/overflow.py :: handler
(litellm/proxy/types_utils/utils.py:30-56 at v1.95.0).

WHY IT EXISTS. The chain aliases (local-general, local-code, local-reasoning,
local-private) are model groups whose deployments carry ``order: 1 / 2 / 3``.
Plain LiteLLM only moves past order 1 when order 1 FAILS -- a vLLM that is
merely saturated queues the request instead and the caller waits. The policy
here is "local first for cost, Claude only when BOTH local models are busy or
down", so something has to notice "busy" before the call is made. That is
this hook.

WHERE IT RUNS (LiteLLM v1.95.0, router.py::async_get_healthy_deployments,
10606-10740): cooldown filter -> ``async_callback_filter_deployments`` (10686,
which calls every CustomLogger's ``async_filter_deployments``, 7340-7387) ->
pre-call context check (10694) -> order filter (10716-10720, keeps only the
lowest ``order`` left). Dropping a deployment here therefore lets the order
filter pick the next order level. NOTE that 7366-7384 RE-RAISES any exception
a filter throws, which would fail the request -- so this hook never throws.

THE RULE, per request, for a group with >= 2 distinct ``order`` values:
  * drop every deployment with an ``api_base`` whose backend is BUSY or DOWN;
  * never drop a deployment without an ``api_base`` (the Claude deployments);
  * if that would leave nothing, stay local: keep the BUSY deployments (the
    request queues inside vLLM) or, if every one is DOWN, return the list
    unchanged. For local-private, which has no Claude deployment, that is
    what keeps PII on the box.

BUSY is a waiting queue with hysteresis, from ``<api_base minus /v1>/metrics``:
``vllm:num_requests_waiting`` > 0 continuously for >= OVERFLOW_BUSY_AFTER_S
sets it; == 0 continuously for >= OVERFLOW_IDLE_AFTER_S clears it.
DOWN is a probe that failed (connection refused, timeout, non-200).
UNKNOWN -- never probed yet, data older than 5 x the poll interval, or a 200
that carries no vLLM queue metric -- keeps the deployment: when this hook
cannot see, LiteLLM behaves exactly as it would without it.

The poller is one asyncio task probing every known backend CONCURRENTLY
about once a second. It is started lazily by the first filter call (on
LiteLLM's own event loop) and restarted if it has died. Backends are learnt
from the deployments the filter sees, so there is nothing to configure.

Debugging: every spill decision logs one INFO line on the ``zeo.overflow``
logger (throttled per alias/deployment/reason), and ``handler.state()``
returns a JSON-able snapshot of what the hook believes about each backend.
See ai/litellm/LITELLM.md > Chain aliases and the overflow hook.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

try:  # LiteLLM only treats CustomLogger instances as deployment filters.
    from litellm.integrations.custom_logger import CustomLogger
except Exception:  # pragma: no cover -- litellm is always present in the image
    CustomLogger = object  # type: ignore[assignment,misc]


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.environ.get(name, "")
    try:
        value = float(raw) if raw.strip() else default
    except ValueError:
        value = default
    return max(minimum, value)


BUSY_AFTER_S = _env_float("OVERFLOW_BUSY_AFTER_S", 2.0)
IDLE_AFTER_S = _env_float("OVERFLOW_IDLE_AFTER_S", 5.0)
POLL_INTERVAL_S = _env_float("OVERFLOW_POLL_INTERVAL_S", 1.0, minimum=0.2)
PROBE_TIMEOUT_S = _env_float("OVERFLOW_PROBE_TIMEOUT_S", 0.75, minimum=0.1)
# Data older than this many poll intervals is "unknown" -> keep.
STALE_POLLS = 5
# At most one log line per (alias, deployment, reason) per this many seconds.
LOG_EVERY_S = 30.0

WAITING_METRIC = "vllm:num_requests_waiting"
RUNNING_METRIC = "vllm:num_requests_running"

OK, BUSY, DOWN, UNKNOWN = "ok", "busy", "down", "unknown"

logger = logging.getLogger("zeo.overflow")
if not logger.handlers:
    # Own handler: LiteLLM's loggers may sit at WARNING, and a spill that is
    # not in `docker logs litellm` is a spill nobody can audit.
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [overflow] %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)
    logger.propagate = False


# -- Pure helpers (unit-tested) ---------------------------------------------
def metrics_url(api_base: str) -> str:
    """http://qwen3.8-solo:8000/v1 -> http://qwen3.8-solo:8000/metrics"""
    base = api_base.strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return base + "/metrics"


def parse_queue_metrics(text: str) -> Tuple[Optional[float], Optional[float]]:
    """Sum vllm:num_requests_waiting / _running over every label set.

    Returns (waiting, running); a metric that never appears is None, so the
    caller can tell "zero" from "this is not the vLLM I expected".
    """
    sums: Dict[str, Optional[float]] = {WAITING_METRIC: None, RUNNING_METRIC: None}
    for line in text.splitlines():
        if not line or line[0] == "#":
            continue
        for name in sums:
            if not line.startswith(name):
                continue
            rest = line[len(name):]
            if rest[:1] not in ("{", " ", "\t"):
                continue  # a longer metric name sharing the prefix
            if rest.startswith("{"):
                close = rest.find("}")
                if close == -1:
                    continue
                rest = rest[close + 1:]
            parts = rest.split()
            if not parts:
                continue
            try:
                value = float(parts[0])
            except ValueError:
                continue
            if value != value:  # NaN
                continue
            sums[name] = (sums[name] or 0.0) + value
    return sums[WAITING_METRIC], sums[RUNNING_METRIC]


class BackendState:
    """What the hook believes about one api_base. Mutated only by apply_*.

    Deliberately NOT a @dataclass: LiteLLM loads this file with
    importlib.util.spec_from_file_location + exec_module and never puts it in
    sys.modules (proxy/types_utils/utils.py:42-49), and dataclasses looks
    sys.modules[cls.__module__] up while processing string annotations --
    which crashes the proxy at startup with
    "'NoneType' object has no attribute '__dict__'".
    """

    def __init__(self, api_base: str, url: str) -> None:
        self.api_base = api_base
        self.url = url
        self.probed_at: Optional[float] = None  # monotonic time of the last finished probe
        self.reachable = False
        self.has_metrics = False
        self.waiting: Optional[float] = None
        self.running: Optional[float] = None
        self.busy = False
        self.waiting_since: Optional[float] = None
        self.idle_since: Optional[float] = None
        self.error = ""
        self.consecutive_failures = 0

    def apply_sample(
        self,
        now: float,
        waiting: Optional[float],
        running: Optional[float],
        busy_after: float = BUSY_AFTER_S,
        idle_after: float = IDLE_AFTER_S,
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


def _api_base(deployment: Any) -> Optional[str]:
    if not isinstance(deployment, dict):
        return None
    params = deployment.get("litellm_params") or {}
    base = params.get("api_base") if isinstance(params, dict) else None
    return base if isinstance(base, str) and base.strip() else None


def _order(deployment: Any) -> Optional[int]:
    # Same lookup as litellm.utils._get_deployment_order.
    if not isinstance(deployment, dict):
        return None
    order = (deployment.get("litellm_params") or {}).get("order")
    if order is None:
        order = (deployment.get("model_info") or {}).get("order")
    return order


def _label(deployment: Dict[str, Any]) -> str:
    params = deployment.get("litellm_params") or {}
    return f"{params.get('model', '?')}@order{_order(deployment)}"


# -- Fetch -------------------------------------------------------------------
Fetch = Callable[[str], Awaitable[str]]


class ProbeError(Exception):
    pass


class _HttpxFetch:
    """GET a /metrics page; raise on anything that is not a 200."""

    def __init__(self, timeout: float) -> None:
        self._timeout = timeout
        self._client: Any = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    async def __call__(self, url: str) -> str:
        import httpx  # LiteLLM depends on it; imported lazily for the tests

        loop = asyncio.get_running_loop()
        if self._client is None or self._loop is not loop:
            self._client = httpx.AsyncClient(timeout=self._timeout)
            self._loop = loop
        resp = await self._client.get(url)
        if resp.status_code != 200:
            raise ProbeError(f"HTTP {resp.status_code}")
        return resp.text


# -- The handler --------------------------------------------------------------
class OverflowHandler(CustomLogger):  # type: ignore[misc,valid-type]
    def __init__(
        self,
        fetch: Optional[Fetch] = None,
        clock: Callable[[], float] = time.monotonic,
        busy_after: float = BUSY_AFTER_S,
        idle_after: float = IDLE_AFTER_S,
        poll_interval: float = POLL_INTERVAL_S,
        probe_timeout: float = PROBE_TIMEOUT_S,
        start_poller: bool = True,
    ) -> None:
        try:
            super().__init__()
        except Exception:  # pragma: no cover -- CustomLogger signature drift
            pass
        self.busy_after = busy_after
        self.idle_after = idle_after
        self.poll_interval = poll_interval
        self.probe_timeout = probe_timeout
        self.stale_after = STALE_POLLS * poll_interval
        self._fetch: Fetch = fetch or _HttpxFetch(probe_timeout)
        self._clock = clock
        self._start_poller = start_poller
        self._backends: Dict[str, BackendState] = {}
        self._task: Optional[asyncio.Task] = None
        self._task_loop: Optional[asyncio.AbstractEventLoop] = None
        self._log_last: Dict[Tuple[str, str, str], float] = {}
        self._log_suppressed: Dict[Tuple[str, str, str], int] = {}
        self.stats: Dict[str, int] = {"spills": 0, "kept_all_overloaded": 0, "errors": 0, "poller_restarts": 0}

    # -- LiteLLM hook ----------------------------------------------------------
    async def async_filter_deployments(
        self,
        model: str,
        healthy_deployments: List,
        messages: Optional[List] = None,
        request_kwargs: Optional[dict] = None,
        parent_otel_span: Any = None,
    ) -> List[dict]:
        try:
            return self.filter_deployments(model, healthy_deployments)
        except Exception as exc:  # never fail a request because of this hook
            self.stats["errors"] += 1
            try:
                logger.warning("filter error for %s, deployments unchanged: %r", model, exc)
            except Exception:
                pass
            return healthy_deployments

    # -- Core rule (sync, so it is trivially testable) --------------------------
    def filter_deployments(self, model: str, healthy_deployments: List) -> List:
        if not isinstance(healthy_deployments, list) or len(healthy_deployments) < 2:
            return healthy_deployments
        orders = {_order(d) for d in healthy_deployments}
        orders.discard(None)
        if len(orders) < 2:
            return healthy_deployments  # not a chain: leave plain groups alone

        for d in healthy_deployments:
            base = _api_base(d)
            if base is not None and base not in self._backends:
                self._backends[base] = BackendState(api_base=base, url=metrics_url(base))
        self._ensure_poller()

        now = self._clock()
        kept: List = []
        dropped: List[Tuple[dict, str]] = []
        for d in healthy_deployments:
            base = _api_base(d)
            if base is None:
                kept.append(d)  # Claude (or anything unpolled): never dropped
                continue
            verdict = self._backends[base].verdict(now, self.stale_after)
            if verdict in (BUSY, DOWN):
                dropped.append((d, verdict))
            else:
                kept.append(d)

        if not dropped:
            return healthy_deployments
        if not kept:
            # Nothing unpolled to spill to (local-private): stay local. Prefer
            # queueing on a live-but-busy backend over calling a dead one;
            # with everything down, hand LiteLLM the list unchanged.
            self.stats["kept_all_overloaded"] += 1
            busy_only = [d for d, v in dropped if v == BUSY]
            result = busy_only if busy_only else healthy_deployments
            self._log(model, "*", "all-overloaded",
                      "%s: every deployment is busy/down (%s); keeping %s -- request stays local and queues",
                      model, ", ".join(f"{_label(d)}={v}" for d, v in dropped),
                      "the busy ones" if busy_only else "all")
            return result

        self.stats["spills"] += 1
        next_order = min((o for o in (_order(d) for d in kept) if o is not None), default=None)
        for d, verdict in dropped:
            st = self._backends[_api_base(d) or ""]
            self._log(model, _label(d), verdict,
                      "%s: dropping %s (%s, waiting=%s, err=%s) -> next order %s",
                      model, _label(d), verdict, st.waiting, st.error or "-", next_order)
        return kept

    # -- Poller ----------------------------------------------------------------
    def _ensure_poller(self) -> None:
        if not self._start_poller:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # called outside a loop (tests); nothing to schedule on
        task = self._task
        if task is not None and not task.done() and self._task_loop is loop:
            return
        if task is not None and task.done() and not task.cancelled():
            exc = task.exception()
            if exc is not None:
                logger.warning("poller died (%r); restarting", exc)
            self.stats["poller_restarts"] += 1
        self._task_loop = loop
        self._task = loop.create_task(self._poll_forever(), name="zeo-overflow-poller")

    async def _poll_forever(self) -> None:
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # keep polling; a dead poller = stale = no spills
                logger.warning("poll pass failed: %r", exc)
            await asyncio.sleep(self.poll_interval)

    async def poll_once(self) -> None:
        """Probe every known backend concurrently and fold in the results."""
        states = list(self._backends.values())
        if not states:
            return
        results = await asyncio.gather(
            *(asyncio.wait_for(self._fetch(s.url), timeout=self.probe_timeout) for s in states),
            return_exceptions=True,
        )
        now = self._clock()
        for st, res in zip(states, results):
            if isinstance(res, BaseException):
                if isinstance(res, asyncio.CancelledError):
                    raise res
                st.apply_failure(now, f"{type(res).__name__}: {res}" if str(res) else type(res).__name__)
            else:
                waiting, running = parse_queue_metrics(res if isinstance(res, str) else "")
                st.apply_sample(now, waiting, running, self.busy_after, self.idle_after)

    # -- Debugging -------------------------------------------------------------
    def state(self) -> Dict[str, Any]:
        now = self._clock()
        task = self._task
        return {
            "config": {
                "busy_after_s": self.busy_after,
                "idle_after_s": self.idle_after,
                "poll_interval_s": self.poll_interval,
                "probe_timeout_s": self.probe_timeout,
                "stale_after_s": self.stale_after,
            },
            "poller_running": bool(task is not None and not task.done()),
            "stats": dict(self.stats),
            "backends": {b: s.snapshot(now, self.stale_after) for b, s in self._backends.items()},
        }

    def _log(self, alias: str, dep: str, reason: str, fmt: str, *args: Any) -> None:
        key = (alias, dep, reason)
        now = self._clock()
        last = self._log_last.get(key)
        if last is not None and now - last < LOG_EVERY_S:
            self._log_suppressed[key] = self._log_suppressed.get(key, 0) + 1
            return
        suppressed = self._log_suppressed.pop(key, 0)
        self._log_last[key] = now
        if suppressed:
            fmt += " (+%d similar in the last %ds)"
            args = args + (suppressed, int(LOG_EVERY_S))
        logger.info(fmt, *args)


# The instance LiteLLM imports: litellm_settings.callbacks: ["overflow.handler"]
handler = OverflowHandler()
