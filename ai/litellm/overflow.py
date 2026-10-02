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

THE `auto` FOOTER (second job of the same handler). When -- and only when --
a client calls model ``auto`` (the semantic router), the assistant text gets
a markdown footer naming the backend that actually answered::

    <answer>

    ---
    *qwen3.8-solo · local-general*

with `` · overflow (local busy|local down)`` when a Claude deployment
answered (busy/down read from THIS handler's backend state -- approximate),
and `` · router bypassed`` when ``auto`` itself fell back to local-private.
The served deployment comes from the inner LiteLLM's response headers, which
the outer openai provider keeps as ``llm_provider-x-litellm-model-id`` /
``-model-name`` / ``-model-group`` in ``_hidden_params["additional_headers"]``
(core_helpers.py:318-319; streams: streaming_handler.py:164-171); the model
id is mapped back to its deployment through the proxy's own router, because
inner and outer are the same proxy. A router bypass shows up as an outer
``data["deployment"]`` (common_request_processing.py:1707) whose model group
is not ``auto``.

The three hooks are defined DIRECTLY in the OverflowHandler class body on
purpose: the proxy activates the streaming-iterator and pre-call hooks only
when the name is in the leaf class's ``__dict__`` (proxy/utils.py:1718-1745),
so a mixin or base class would silently never run. They skip tool calls,
``response_format`` requests and Open WebUI background tasks (title / tags /
follow-ups / queries / autocomplete / emoji / legacy function calling, which
are detected by their default prompt templates), strip a previous footer
from incoming assistant messages, and never throw. AUTO_FOOTER_ENABLED=false
turns the whole feature off. See ai/litellm/LITELLM.md > The `auto` footer.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import time
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional, Tuple

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


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


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


# -- `auto` footer: pure helpers (unit-tested) --------------------------------
AUTO_FOOTER_ENABLED = _env_bool("AUTO_FOOTER_ENABLED", True)
AUTO_MODEL = "auto"
FOOTER_SEP = " · "  # " · "
_PROVIDER_PREFIXES = ("openai/", "anthropic/", "hosted_vllm/")

# Exactly the footer footer_text() emits: a blank line, a `---` rule, then
# one *italic* line of at least two " · "-separated parts, at the very end.
FOOTER_RE = re.compile(
    r"(?:\r?\n){2,}---[ \t]*\r?\n\*[^*\r\n·]+(?: · [^*\r\n·]+)+\*[ \t]*(?:\r?\n[ \t]*)*\Z"
)

# Open WebUI v0.11.4 background tasks. Their payloads carry `metadata.task`,
# but routers/openai.py pops `metadata` before the request leaves Open WebUI
# and forwards no task header -- so the prompt text is the only signal. The
# default templates (backend/open_webui/config.py:2211-2441) for title, tags,
# image prompt, follow-ups, query and autocomplete all start "### Task:";
# emoji starts with the sentence below; legacy function calling
# (utils/middleware.py:1351-1373) sends a system message starting
# "Available Tools:". MOA (config.py:2443) is a user-visible answer and is
# deliberately NOT treated as a task.
OWUI_TASK_PREFIXES = (
    "### Task:",
    "Your task is to reflect the speaker's likely facial expression",
)
OWUI_TOOLS_SYSTEM_PREFIX = "Available Tools:"


def strip_provider(model: Optional[str]) -> Optional[str]:
    """openai/qwen3.8-solo -> qwen3.8-solo; anthropic/claude-sonnet-5 -> claude-sonnet-5"""
    if not isinstance(model, str) or not model.strip():
        return None
    model = model.strip()
    for prefix in _PROVIDER_PREFIXES:
        if model.startswith(prefix):
            return model[len(prefix):]
    return model


def footer_text(label: str) -> str:
    return "\n\n---\n*" + label + "*"


def strip_footer(text: Any) -> Any:
    """Remove trailing footers this module emitted; anything else is returned as is."""
    if not isinstance(text, str):
        return text
    for _ in range(5):
        new = FOOTER_RE.sub("", text)
        if new == text:
            break
        text = new
    return text


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str):
                parts.append(p["text"])
        return "\n".join(parts)
    return ""


def strip_footers_from_messages(messages: Any) -> int:
    """Strip our footer from every assistant message, in place. Returns how many changed."""
    changed = 0
    if not isinstance(messages, list):
        return 0
    for m in messages:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        content = m.get("content")
        if isinstance(content, str):
            new = strip_footer(content)
            if new != content:
                m["content"] = new
                changed += 1
        elif isinstance(content, list):
            # The footer is at the end of the message: only the LAST text part.
            for p in reversed(content):
                if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str):
                    new = strip_footer(p["text"])
                    if new != p["text"]:
                        p["text"] = new
                        changed += 1
                    break
    return changed


def is_openwebui_task(data: Any) -> bool:
    """True for an Open WebUI background task request (see OWUI_TASK_PREFIXES)."""
    if not isinstance(data, dict):
        return False
    messages = data.get("messages")
    if not isinstance(messages, list):
        return False
    last_user: Optional[str] = None
    for m in messages:
        if not isinstance(m, dict):
            continue
        text = _content_text(m.get("content"))
        if m.get("role") == "system" and text.lstrip().startswith(OWUI_TOOLS_SYSTEM_PREFIX):
            return True
        if m.get("role") == "user":
            last_user = text
    if last_user is None:
        return False
    text = last_user.lstrip()
    if text.startswith(OWUI_TASK_PREFIXES):
        return True
    # Admin-customised task templates usually keep the defaults' <chat_history>
    # block; Open WebUI sends every task except MOA with stream=False.
    return not data.get("stream") and "<chat_history>" in text and "</chat_history>" in text


def _get(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _deployment_fields(deployment: Any) -> Tuple[Optional[str], Optional[str]]:
    """(model_name, litellm_params.model) of a router Deployment or its dict form."""
    if deployment is None:
        return None, None
    group = _get(deployment, "model_name")
    model = _get(_get(deployment, "litellm_params"), "model")
    return (group if isinstance(group, str) else None, model if isinstance(model, str) else None)


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _headers_of(obj: Any) -> Dict[str, Any]:
    """Lower-cased _hidden_params["additional_headers"] of a response, stream wrapper or chunk."""
    hidden = getattr(obj, "_hidden_params", None)
    if hidden is None and isinstance(obj, dict):
        hidden = obj.get("_hidden_params")
    if not isinstance(hidden, dict):
        return {}
    headers = hidden.get("additional_headers")
    if not isinstance(headers, dict):
        return {}
    return {str(k).lower(): v for k, v in headers.items()}


def _choice_has_tool_calls(obj: Any) -> bool:
    return bool(_get(obj, "tool_calls")) or bool(_get(obj, "function_call"))


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


def _proxy_router() -> Any:
    """The running proxy's Router, without importing the proxy (tests, scripts)."""
    mod = sys.modules.get("litellm.proxy.proxy_server")
    return getattr(mod, "llm_router", None) if mod is not None else None


def _clean(value: Optional[str]) -> Optional[str]:
    """Keep a footer part on one line and out of FOOTER_RE's separators."""
    if not isinstance(value, str):
        return None
    value = " ".join(value.replace("*", "").replace("·", "").split())
    return value or None


def _set(obj: Any, key: str, value: Any) -> None:
    if isinstance(obj, dict):
        obj[key] = value
    else:
        setattr(obj, key, value)


class _FooterStream:
    """Per-request state for the streaming footer. before() / finish() never raise.

    A choice qualifies when it streamed non-empty `delta.content` and no tool
    call. Its footer goes out once, ahead of its finish chunk: appended to that
    chunk's own `delta.content` when it carries text (so the footer is never
    put in front of the last words), otherwise as an extra chunk yielded just
    before it. A stream that ends without a finish chunk gets the footer last.
    Not a @dataclass -- see BackendState.
    """

    def __init__(self, handler: "OverflowHandler", wrapper: Any, data: dict) -> None:
        self.handler = handler
        self.wrapper = wrapper
        self.data = data
        self.ok = True
        self.text: Dict[int, bool] = {}
        self.tools: Dict[int, bool] = {}
        self.done: set = set()
        self.last_chunk: Any = None
        self._footer: Optional[str] = None
        self._resolved = False

    def footer(self, chunk: Any) -> Optional[str]:
        if not self._resolved:
            self._resolved = True
            headers = {**_headers_of(chunk), **_headers_of(self.wrapper)}
            label = self.handler.footer_label(headers, self.data)
            self._footer = footer_text(label) if label else None
        return self._footer

    def before(self, chunk: Any) -> List[Any]:
        if not self.ok:
            return []
        try:
            choices = getattr(chunk, "choices", None)
            if not isinstance(choices, list):
                return []
            self.last_chunk = chunk
            appends: List[Any] = []
            separate: List[int] = []
            for c in choices:
                idx = _int(_get(c, "index"))
                delta = _get(c, "delta")
                if delta is not None:
                    if _choice_has_tool_calls(delta):
                        self.tools[idx] = True
                    content = _get(delta, "content")
                    if isinstance(content, str) and content.strip():
                        self.text[idx] = True
                if not _get(c, "finish_reason") or idx in self.done:
                    continue
                self.done.add(idx)
                if not self.text.get(idx) or self.tools.get(idx):
                    continue
                if self.footer(chunk) is None:
                    continue
                content = _get(delta, "content") if delta is not None else None
                if isinstance(content, str) and content:
                    appends.append(delta)
                else:
                    separate.append(idx)
            extra = [self._make_chunk(chunk, separate)] if separate else []
            footer = self._footer or ""
            for delta in appends:
                _set(delta, "content", _get(delta, "content") + footer)
            if appends or separate:
                self.handler.stats["footers"] += 1
            return extra
        except Exception as exc:
            self.ok = False
            self.handler._footer_error("stream", exc)
            return []

    def finish(self) -> List[Any]:
        if not self.ok or self.last_chunk is None:
            return []
        try:
            pending = [i for i, has in self.text.items() if has and not self.tools.get(i) and i not in self.done]
            if not pending or self.footer(self.last_chunk) is None:
                return []
            self.done.update(pending)
            self.handler.stats["footers"] += 1
            return [self._make_chunk(self.last_chunk, sorted(pending))]
        except Exception as exc:
            self.ok = False
            self.handler._footer_error("stream-end", exc)
            return []

    def _make_chunk(self, template: Any, indices: List[int]) -> Any:
        from litellm.types.utils import Delta, ModelResponseStream, StreamingChoices

        kwargs: Dict[str, Any] = {}
        model = _get(template, "model")
        if isinstance(model, str):
            kwargs["model"] = model
        chunk = ModelResponseStream(
            id=_get(template, "id"),
            created=_get(template, "created"),
            choices=[StreamingChoices(index=i, delta=Delta(content=self._footer), finish_reason=None) for i in indices],
            **kwargs,
        )
        hidden = getattr(template, "_hidden_params", None)
        if isinstance(hidden, dict):
            chunk._hidden_params = dict(hidden)
        return chunk


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
        auto_footer: Optional[bool] = None,
        router_getter: Optional[Callable[[], Any]] = None,
    ) -> None:
        try:
            super().__init__()
        except Exception:  # pragma: no cover -- CustomLogger signature drift
            pass
        self.auto_footer = AUTO_FOOTER_ENABLED if auto_footer is None else bool(auto_footer)
        self._router_getter = router_getter or _proxy_router
        # chain alias -> the api_bases of its local deployments, learnt in
        # filter_deployments; the footer reads their verdicts.
        self._chain_backends: Dict[str, Tuple[str, ...]] = {}
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
        self.stats: Dict[str, int] = {
            "spills": 0, "kept_all_overloaded": 0, "errors": 0, "poller_restarts": 0,
            "footers": 0, "footers_stripped": 0, "footer_errors": 0,
        }

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

    # -- `auto` footer hooks -----------------------------------------------------
    # These three MUST stay defined in this class body. The proxy decides once
    # per callback list whether to call async_post_call_streaming_iterator_hook
    # and async_pre_call_hook by looking the name up in type(callback).__dict__
    # (proxy/utils.py:1718-1745 at v1.95.0); one inherited from a mixin or base
    # class would never run. async_post_call_success_hook is called on every
    # CustomLogger in litellm.callbacks (proxy/utils.py:2404-2410) and a
    # non-None return replaces the response. All three are no-ops unless the
    # client asked for model `auto`, and none of them ever raises.
    async def async_pre_call_hook(self, user_api_key_dict: Any, cache: Any, data: dict, call_type: Any) -> Any:
        """Strip footers we emitted from incoming assistant messages, so the model
        never sees (and imitates) them and the router's signals never read them."""
        try:
            if not self.auto_footer or not isinstance(data, dict) or data.get("model") != AUTO_MODEL:
                return None
            n = strip_footers_from_messages(data.get("messages"))
            if not n:
                return None
            self.stats["footers_stripped"] += n
            return data  # modified in place; proxy/utils.py:913-917 keeps a returned dict
        except Exception as exc:
            self._footer_error("pre_call", exc)
            return None

    async def async_post_call_success_hook(self, data: dict, user_api_key_dict: Any, response: Any) -> Any:
        """Non-streaming: append the footer to each qualifying choice's message."""
        try:
            if not self._footer_wanted(data):
                return None
            choices = getattr(response, "choices", None)
            if not isinstance(choices, list) or not choices:
                return None  # not a chat completion (embeddings, responses API, ...)
            hidden = getattr(response, "_hidden_params", None)
            if isinstance(hidden, dict) and hidden.get("zeo_auto_footer"):
                return None  # already done -- never twice
            label = self.footer_label(_headers_of(response), data)
            if not label:
                return None
            footer = footer_text(label)
            edits: List[Tuple[Any, str]] = []
            for choice in choices:
                message = _get(choice, "message")
                if message is None or _choice_has_tool_calls(message):
                    continue
                content = _get(message, "content")
                if not isinstance(content, str):
                    continue
                body = strip_footer(content)  # an imitated footer is replaced, not doubled
                if not body.strip():
                    continue
                edits.append((message, body + footer))
            if not edits:
                return None
            for message, new in edits:
                _set(message, "content", new)
            if isinstance(hidden, dict):
                hidden["zeo_auto_footer"] = True
            self.stats["footers"] += 1
            return response
        except Exception as exc:
            self._footer_error("success", exc)
            return None

    async def async_post_call_streaming_iterator_hook(
        self, user_api_key_dict: Any, response: Any, request_data: dict
    ) -> AsyncIterator[Any]:
        """Streaming: put the footer in front of each qualifying choice's finish
        chunk (or on its content, when the finish chunk carries text). The
        proxy wraps the stream once (proxy_server.py:7413-7419) and writes
        `data: [DONE]` only after this generator ends."""
        try:
            active = self._footer_wanted(request_data)
        except Exception as exc:
            self._footer_error("stream", exc)
            active = False
        if not active:
            async for chunk in response:
                yield chunk
            return
        stream = _FooterStream(self, response, request_data)
        async for chunk in response:
            for extra in stream.before(chunk):
                yield extra
            yield chunk
        for extra in stream.finish():
            yield extra

    # -- footer derivation -------------------------------------------------------
    def _footer_wanted(self, data: Any) -> bool:
        if not self.auto_footer or not isinstance(data, dict) or data.get("model") != AUTO_MODEL:
            return False
        if data.get("response_format"):
            return False  # structured output: a footer would break the JSON
        return not is_openwebui_task(data)

    def footer_label(self, headers: Dict[str, Any], data: Any) -> Optional[str]:
        """'qwen3.8-solo · local-general', or None when the backend is unknown.

        Inner LiteLLM (behind the router) -> llm_provider-x-litellm-model-id /
        -model-name / -model-group; the id is mapped through the proxy's own
        router because inner and outer are the same proxy. The OUTER deployment
        is data["deployment"] (common_request_processing.py:1707): when its
        group is not `auto`, `auto` fell back past the router to local-private.
        """
        headers = headers or {}
        outer_group, outer_model = _deployment_fields(data.get("deployment") if isinstance(data, dict) else None)
        bare_group = headers.get("x-litellm-model-group")
        if not outer_group and isinstance(bare_group, str) and bare_group:
            outer_group = bare_group
        has_vsr = any(k.startswith("llm_provider-x-vsr-") for k in headers)

        chain: Optional[str] = None
        served: Optional[str] = None
        inner_id = headers.get("llm_provider-x-litellm-model-id")
        if inner_id:
            try:
                router = self._router_getter()
                dep = router.get_deployment(model_id=str(inner_id)) if router is not None else None
                chain, served = _deployment_fields(dep)
            except Exception:
                chain = served = None
        if not served:
            served = headers.get("llm_provider-x-litellm-model-name") or None
        if not chain:
            chain = headers.get("llm_provider-x-litellm-model-group") or headers.get("llm_provider-x-vsr-selected-model") or None
        inner_known = bool(served or chain)

        bypassed = bool(outer_group) and outer_group != AUTO_MODEL
        if not bypassed and not has_vsr and not inner_known and outer_model and "vllm-sr/" not in outer_model:
            bypassed = True
        if bypassed:
            served, chain = outer_model, (outer_group if outer_group != AUTO_MODEL else None)

        name = _clean(strip_provider(served if isinstance(served, str) else None))
        chain = _clean(chain if isinstance(chain, str) else None)
        if not name or not chain:
            return None
        parts = [name, chain]
        if isinstance(served, str) and (served.startswith("anthropic/") or name.startswith("claude")):
            parts.append(self._overflow_tag(chain))
        if bypassed:
            parts.append("router bypassed")
        return FOOTER_SEP.join(parts)

    def _overflow_tag(self, chain: str) -> str:
        """'overflow (local busy)' / '(local down)' from this handler's CURRENT view
        of the chain's local backends -- an approximation of why Claude answered."""
        try:
            bases = self._chain_backends.get(chain)
            if not bases:
                router = self._router_getter()
                found = router.get_model_list(model_name=chain) if router is not None else None
                bases = tuple(b for b in (_api_base(d) for d in (found or [])) if b)
            now = self._clock()
            verdicts = [self._backends[b].verdict(now, self.stale_after) for b in bases if b in self._backends]
            if verdicts and all(v == DOWN for v in verdicts):
                return "overflow (local down)"
            if any(v == BUSY for v in verdicts):
                return "overflow (local busy)"
        except Exception:
            pass
        return "overflow"

    def _footer_error(self, where: str, exc: BaseException) -> None:
        try:
            self.stats["footer_errors"] += 1
            self._log("auto", "footer", where, "auto footer (%s) skipped after an error: %r", where, exc)
        except Exception:
            pass

    # -- Core rule (sync, so it is trivially testable) --------------------------
    def filter_deployments(self, model: str, healthy_deployments: List) -> List:
        if not isinstance(healthy_deployments, list) or len(healthy_deployments) < 2:
            return healthy_deployments
        orders = {_order(d) for d in healthy_deployments}
        orders.discard(None)
        if len(orders) < 2:
            return healthy_deployments  # not a chain: leave plain groups alone

        bases: List[str] = []
        for d in healthy_deployments:
            base = _api_base(d)
            if base is not None:
                bases.append(base)
                if base not in self._backends:
                    self._backends[base] = BackendState(api_base=base, url=metrics_url(base))
        if isinstance(model, str) and bases:
            # Union: a cooled-down deployment missing from this call is still in the chain.
            self._chain_backends[model] = tuple(dict.fromkeys(self._chain_backends.get(model, ()) + tuple(bases)))
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
