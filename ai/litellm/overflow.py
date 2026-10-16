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
pre-call context check (10694, a backstop now -- see CONTEXT FIT) -> order
filter (10716-10720, keeps only the
lowest ``order`` left). Dropping a deployment here therefore lets the order
filter pick the next order level. NOTE that 7366-7384 RE-RAISES any exception
a filter throws, which would fail the request -- so this hook never throws.

THE RULE, per request, for a group with >= 2 distinct ``order`` values (on
whatever the CONTEXT FIT step below has left):
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

CONTEXT FIT (runs BEFORE the busy/down rule, same chain groups only).
LiteLLM's own pre-call context check (router.py::_pre_call_checks, 10163)
counts the prompt with::

    return litellm.token_counter(messages=messages)   # router.py:10147

-- OpenAI's tiktoken, over ``messages`` alone. Tool definitions are not
counted (Open WebUI's native tool calling sends them on every turn) and
Qwen's tokenizer is not tiktoken, so on 2026-10-12 a prompt vLLM counted at
122,881 tokens passed as under qwen3.8-solo's 114,688, vLLM refused it with
ContextWindowExceededError, and LiteLLM does not try the next order for that
error (router.py:6134-6137). So the hook counts for itself, with the
backend's own tokenizer: vLLM's ``POST <api_base minus /v1>/tokenize``
renders ``messages`` + ``tools`` through the same chat template and
``preprocess_chat`` as /v1/chat/completions and returns ``count`` and
``max_model_len`` -- exactly the numbers vLLM checks. A local deployment
(an ``api_base`` and an int ``model_info.max_input_tokens``) FITS iff::

    count <= max_input_tokens  and  count + max_out <= max_model_len

with max_out the request's max_completion_tokens / max_tokens, else the
deployment's own. One that does not fit is dropped as ``too-long``, and that
is a HARD drop: the busy/down rule runs on what is left, so its "keep the
busy ones" fallback can never bring it back. If NO local deployment fits, a
group with a Claude deployment keeps only Claude (the prompt is past every
local window -- the same outcome as before); local-private, with nothing
unpolled, gets the list back unchanged, so vLLM refuses it with its own
error and PII stays on the box.

/tokenize is only called when it could matter: a deliberately HIGH estimate
(JSON length of messages + tools / OVERFLOW_TOKENIZE_CHARS_PER_TOKEN) must
reach OVERFLOW_TOKENIZE_MIN_FRACTION x that deployment's max_input_tokens.
Each distinct backend is asked at most once per request, all of them
concurrently, within OVERFLOW_TOKENIZE_TIMEOUT_S, and answers are kept in a
small LRU (``auto``'s context_window_fallbacks and the mid-stream order
fallback re-enter with the same messages). ANY tokenize failure -- timeout,
non-200, an image part on a text-only model, a body without ``count`` --
KEEPS the deployment: LiteLLM then behaves exactly as it did before, and
_pre_call_checks stays in place as the backstop.

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
are detected by their default prompt templates on a non-streaming request --
a streamed answer whose user message carries the "### Task:" RAG template
still gets its footer), strip a previous footer
from incoming assistant messages, and never throw. AUTO_FOOTER_ENABLED=false
turns the whole feature off. See ai/litellm/LITELLM.md > The `auto` footer.

THE CODE-TOOL REMAP (third job, in the same async_pre_call_hook). The
semantic router reads only the latest user message and cannot see tool
NAMES, so a coding chat whose latest turn is "Looks the same" lands on
local-general. When the conversation in a request for ``local-general``
contains a CALL to a tool whose function name matches CODE_ROUTE_TOOL_REGEX
in full (default: the sandbox MCP tools, ``sandbox-mcp_*``, and Open
Terminal's distinctive operationIds such as ``run_command``), the hook
rewrites ``data["model"]`` to ``local-code``. Tools that are merely offered
(``data["tools"]``) do not count: a chat with the sandbox attached but never
used stays on local-general. The history is split into EXCHANGES, one per
user message; an exchange is relevant when the model called such a tool
during it or its user message matches the semantic router's own ``code_kw``
signal (read from the router's config.yaml, CODE_ROUTE_ROUTER_CONFIG). The
remap holds while the current exchange is relevant or fewer than
CODE_ROUTE_COOLDOWN (default 3) irrelevant exchanges in a row followed the
last relevant one; after that the chat drops back to local-general until it
looks like code again.
The proxy routes on the rewritten model (pre_call_hook ->
route_request in common_request_processing.py) and the Router sets
metadata.model_group from it, so the overflow filter, the spend log and the
`auto` footer all see local-code. Only ``local-general`` is ever remapped --
never local-private (PII routing stays the router's decision),
local-reasoning, local-code itself, ``auto`` or a concrete alias. One
``[route] local-general -> local-code (tool: <name>)`` line per remap
(throttled like the spill lines). An empty CODE_ROUTE_TOOL_REGEX disables
it; an invalid one is logged and disables it. See ai/litellm/LITELLM.md >
local-code: what it adds.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sys
import time
from collections import OrderedDict
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

# Context fit (see CONTEXT FIT above). The estimate runs HIGH on purpose:
# English is ~4 chars/token and JSON adds keys and escapes, so 2.5 chars/token
# overestimates almost any prompt -- a short chat never makes the extra call.
TOKENIZE_MIN_FRACTION = _env_float("OVERFLOW_TOKENIZE_MIN_FRACTION", 0.6)
TOKENIZE_CHARS_PER_TOKEN = _env_float("OVERFLOW_TOKENIZE_CHARS_PER_TOKEN", 2.5, minimum=0.5)
TOKENIZE_TIMEOUT_S = _env_float("OVERFLOW_TOKENIZE_TIMEOUT_S", 5.0, minimum=0.1)
# (api_base, sha256 of the body) -> (count, max_model_len), least recent out.
TOKENIZE_CACHE_SIZE = 64

WAITING_METRIC = "vllm:num_requests_waiting"
RUNNING_METRIC = "vllm:num_requests_running"

OK, BUSY, DOWN, UNKNOWN = "ok", "busy", "down", "unknown"
TOO_LONG = "too-long"

logger = logging.getLogger("zeo.overflow")
if not logger.handlers:
    # Own handler: LiteLLM's loggers may sit at WARNING, and a spill that is
    # not in `make logs litellm` is a spill nobody can audit.
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [overflow] %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)
    logger.propagate = False


# -- Pure helpers (unit-tested) ---------------------------------------------
def server_url(api_base: str, path: str) -> str:
    """http://qwen3.8-solo:8000/v1 + /tokenize -> http://qwen3.8-solo:8000/tokenize

    vLLM serves /metrics and /tokenize at the server root, outside /v1 (and
    outside its /v1 API-key guard)."""
    base = api_base.strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return base + "/" + path.lstrip("/")


def metrics_url(api_base: str) -> str:
    """http://qwen3.8-solo:8000/v1 -> http://qwen3.8-solo:8000/metrics"""
    return server_url(api_base, "/metrics")


def tokenize_url(api_base: str) -> str:
    """http://qwen3.8-solo:8000/v1 -> http://qwen3.8-solo:8000/tokenize"""
    return server_url(api_base, "/tokenize")


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


def _is_chain(deployments: Any) -> bool:
    """>= 2 deployments with >= 2 distinct `order` values. Anything else is a
    plain alias, and the hook never touches it."""
    if not isinstance(deployments, list) or len(deployments) < 2:
        return False
    orders = {_order(d) for d in deployments}
    orders.discard(None)
    return len(orders) >= 2


# -- Context fit: pure helpers (unit-tested) ---------------------------------
def _positive_int(value: Any) -> Optional[int]:
    # bool is an int subclass; True is not a token budget.
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _jsonable(value: Any) -> Any:
    """json.dumps default: pydantic messages / tool objects -> plain data."""
    for attr in ("model_dump", "dict"):
        fn = getattr(value, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:
                pass
    return str(value)


def _params(deployment: Any) -> Dict[str, Any]:
    params = deployment.get("litellm_params") if isinstance(deployment, dict) else None
    return params if isinstance(params, dict) else {}


def max_input_tokens(deployment: Any) -> Optional[int]:
    info = deployment.get("model_info") if isinstance(deployment, dict) else None
    return _positive_int(info.get("max_input_tokens")) if isinstance(info, dict) else None


def request_tools(request_kwargs: Any) -> Optional[List[Any]]:
    tools = request_kwargs.get("tools") if isinstance(request_kwargs, dict) else None
    return tools if isinstance(tools, list) and tools else None


def estimate_tokens(messages: Any, tools: Optional[List[Any]], chars_per_token: float) -> int:
    """A deliberately HIGH token estimate: JSON characters / chars_per_token.

    json.dumps' default separators and ASCII escaping only add characters,
    so non-English text and markup push the estimate up, never down."""
    chars = len(json.dumps(messages, default=_jsonable))
    if tools:
        chars += len(json.dumps(tools, default=_jsonable))
    return int(chars / max(chars_per_token, 0.1)) + 1


def max_output_tokens(request_kwargs: Any, deployment: Any) -> int:
    """The completion budget vLLM will reserve: the request's own
    max_completion_tokens / max_tokens, else the deployment's (LiteLLM lays
    the request's kwargs over the deployment's litellm_params, and vLLM
    prefers max_completion_tokens). 0 when neither sets one -- vLLM then
    gives the rest of the window to the answer and only the input must fit."""
    for src in (request_kwargs if isinstance(request_kwargs, dict) else {}, _params(deployment)):
        for key in ("max_completion_tokens", "max_tokens"):
            value = _positive_int(src.get(key))
            if value is not None:
                return value
    return 0


def chat_template_kwargs(deployment: Any, request_kwargs: Any) -> Optional[Dict[str, Any]]:
    """What vLLM's chat template will be rendered with, besides the server's
    --default-chat-template-kwargs (which /tokenize applies by itself).

    LiteLLM REPLACES rather than merges (measured on the outgoing request
    body with both 1.93.0 and 1.95.0): the Router builds the call as {**deployment litellm_params,
    **request kwargs} (router.py::_acompletion), so a request `extra_body`
    replaces the deployment's whole extra_body; then, for an openai/ provider,
    a top-level request `chat_template_kwargs` is written over
    extra_body["chat_template_kwargs"] as one value
    (utils.py::add_provider_specific_params_to_optional_params). So the
    template is rendered with exactly ONE of: the request's top-level
    chat_template_kwargs, else the request's extra_body's, else -- only when
    the request sent no extra_body at all -- the deployment's. Mirroring
    that is what keeps /tokenize counting the prompt vLLM will see (the
    local-code deployments set reasoning_effort / reasoning_strength here,
    and a client that sends its own chat_template_kwargs drops them)."""
    req = request_kwargs if isinstance(request_kwargs, dict) else {}
    top = req.get("chat_template_kwargs")
    if top is not None:
        return dict(top) if isinstance(top, dict) and top else None
    extra = req["extra_body"] if "extra_body" in req else _params(deployment).get("extra_body")
    value = extra.get("chat_template_kwargs") if isinstance(extra, dict) else None
    return dict(value) if isinstance(value, dict) and value else None


def system_prompt_messages(deployment: Any, messages: Any) -> Any:
    """`messages` as LiteLLM will send them to this deployment: a
    `litellm_system_prompt` litellm_param is merged IN FRONT of the first
    system message (or becomes a new first system message) by
    main.py::completion -> add_system_prompt_to_messages(...,
    merge_with_first_system=True), AFTER the router has picked the
    deployment -- so this hook must add it itself to count it."""
    prompt = _params(deployment).get("litellm_system_prompt")
    if not isinstance(prompt, str) or not prompt or not isinstance(messages, list):
        return messages
    first = messages[0] if messages else None
    if isinstance(first, dict) and first.get("role") == "system":
        merged = dict(first)
        content = first.get("content", "")
        if isinstance(content, str):
            merged["content"] = f"{prompt.strip()}\n\n{content}"
        elif isinstance(content, list):
            merged["content"] = [{"type": "text", "text": prompt.strip()}] + list(content)
        else:
            merged["content"] = [{"type": "text", "text": prompt.strip()}]
        return [merged] + list(messages[1:])
    return [{"role": "system", "content": prompt}] + list(messages)


def tokenize_body(
    deployment: Any, messages: Any, tools: Optional[List[Any]], request_kwargs: Any
) -> Dict[str, Any]:
    """The /tokenize request (vLLM TokenizeChatRequest): the same messages
    (with the deployment's litellm_system_prompt merged in), tools and
    template kwargs the chat completion will carry, plus the generation
    prompt, so `count` is the prompt vLLM will actually see."""
    body: Dict[str, Any] = {
        "model": strip_provider(_params(deployment).get("model")) or "",
        "messages": system_prompt_messages(deployment, messages),
        "add_generation_prompt": True,
    }
    if tools:
        body["tools"] = tools
    ctk = chat_template_kwargs(deployment, request_kwargs)
    if ctk:
        body["chat_template_kwargs"] = ctk
    # Round-trip through JSON: plain data for httpx, and a stable cache key.
    return json.loads(json.dumps(body, default=_jsonable))


def tokenize_cache_key(url: str, body: Dict[str, Any]) -> Tuple[str, str]:
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return url, hashlib.sha256(raw.encode("utf-8")).hexdigest()


def context_verdict(
    count: int, max_input: int, max_model_len: Optional[int], max_out: int
) -> Optional[str]:
    """None when the prompt fits; otherwise why it does not (for the log)."""
    if count > max_input:
        return f"count={count} > max_input_tokens={max_input}"
    if max_model_len is not None and count + max_out > max_model_len:
        return f"count={count} + max_tokens={max_out} > max_model_len={max_model_len}"
    return None


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
#
# The prefixes only count on a NON-streaming request: every task above is sent
# with stream=False (routers/tasks.py), while the RAG template
# (DEFAULT_RAG_TEMPLATE) also starts "### Task:" and, with RAG_SYSTEM_CONTEXT
# unset, is PREPENDED to the user's own message (utils/middleware.py,
# add_or_update_user_message(..., append=False)) -- so a streamed chat answer
# that used knowledge retrieval would otherwise look like a task and lose its
# footer for the whole turn.
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
    # Open WebUI sends every task except MOA with stream=False; a streamed
    # request is a chat answer even when it starts "### Task:" (the RAG template).
    if last_user is None or data.get("stream"):
        return False
    text = last_user.lstrip()
    if text.startswith(OWUI_TASK_PREFIXES):
        return True
    # Admin-customised task templates usually keep the defaults' <chat_history> block.
    return "<chat_history>" in text and "</chat_history>" in text


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


# -- Code-tool remap: pure helpers (unit-tested) -------------------------------
CODE_ROUTE_FROM = "local-general"
CODE_ROUTE_TO = "local-code"
# Tool names as Open WebUI v0.11.4 presents them to the model:
#   * the sandbox MCP server's tools carry the server-id prefix `sandbox-mcp_`
#     (sandbox-mcp_run, sandbox-mcp_write_files, sandbox-mcp_exec, ...);
#   * Open Terminal's tools are its OpenAPI operationIds, UNPREFIXED --
#     utils/tools.py::get_terminal_tools uses `function_name = spec['name']`,
#     and convert_openapi_to_tool_payload sets name = operationId. The
#     open-terminal 0.13.0 operationIds in the schema are run_command,
#     get_process_status, send_process_input, kill_process, list_processes,
#     replace_file_content, write_file, read_file, display_file, list_files,
#     grep_search, glob_search, match_files, search_files, get_info,
#     read_user_terminal, send_user_terminal_input. Only the ones no other
#     tool server is likely to share are listed; run_command is always
#     present when a terminal is attached.
#
# The pattern must match the WHOLE tool name (re.fullmatch), so it needs no
# `^...$` anchors -- and a `$` in .env would have to be written `$$`. The
# same string is the default in docker-compose.litellm.yml and .env.example;
# unit-tests/litellm/test_code_route.py fails if the three drift.
DEFAULT_CODE_ROUTE_TOOL_REGEX = (
    "sandbox-mcp_.+|run_command|get_process_status|send_process_input"
    "|kill_process|replace_file_content|read_user_terminal|send_user_terminal_input"
)


def compile_code_route_regex(raw: Optional[str]) -> Tuple[Optional["re.Pattern[str]"], str]:
    """(compiled pattern or None, error). Unset / empty / whitespace disables
    the remap; an invalid pattern disables it too and returns the error to log."""
    if raw is None or not raw.strip():
        return None, ""
    try:
        return re.compile(raw.strip()), ""
    except re.error as exc:
        return None, f"invalid CODE_ROUTE_TOOL_REGEX {raw!r}: {exc}"


def _name_of(obj: Any) -> Optional[str]:
    name = obj.get("name") if isinstance(obj, dict) else None
    return name if isinstance(name, str) and name else None


def called_tool_names(messages: Any) -> List[str]:
    """Names of the tools the conversation has actually CALLED, most recent
    first. Reads every shape a chat-completions history carries a call in:
    an assistant's OpenAI ``tool_calls[].function.name`` (what Open WebUI
    replays) and legacy ``function_call.name``, Anthropic ``tool_use``
    content blocks, and a ``tool`` / ``function`` result message's ``name``.
    Offered tool definitions (``tools``) are deliberately not read."""
    names: List[str] = []
    if not isinstance(messages, list):
        return names
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        found: List[str] = []
        calls = msg.get("tool_calls")
        if isinstance(calls, list):
            found += [n for n in (_name_of(c.get("function")) for c in calls if isinstance(c, dict)) if n]
        legacy = _name_of(msg.get("function_call"))
        if legacy:
            found.append(legacy)
        content = msg.get("content")
        if isinstance(content, list):
            found += [n for n in (_name_of(b) for b in content
                                  if isinstance(b, dict) and b.get("type") == "tool_use") if n]
        if msg.get("role") in ("tool", "function"):
            result = _name_of(msg)
            if result:
                found.append(result)
        names += reversed(found)
    return names


def code_tool_match(names: Any, pattern: Any) -> Optional[str]:
    """The first of `names` that `pattern` matches in FULL (re.fullmatch),
    else None. `pattern` may be a compiled regex, a string, or None (=
    disabled)."""
    if pattern is None or not isinstance(names, list):
        return None
    if isinstance(pattern, str):
        pattern, _ = compile_code_route_regex(pattern)
        if pattern is None:
            return None
    for name in names:
        if isinstance(name, str) and pattern.fullmatch(name):
            return name
    return None


# Unset -> the default above; set but empty -> remap off (the compose file
# passes ${CODE_ROUTE_TOOL_REGEX-<default>}, so only an explicit empty value
# in .env turns it off).
CODE_ROUTE_TOOL_REGEX, CODE_ROUTE_TOOL_REGEX_ERROR = compile_code_route_regex(
    os.environ.get("CODE_ROUTE_TOOL_REGEX", DEFAULT_CODE_ROUTE_TOOL_REGEX)
)


# -- Code cooldown: pure helpers (unit-tested) ----------------------------------
# A chat stays on local-code while its recent EXCHANGES (one per user message:
# the message plus every model round it started) keep looking like code, and
# drops back to local-general after CODE_ROUTE_COOLDOWN irrelevant exchanges in
# a row. An exchange is relevant when its user message matches the semantic
# router's own `code_kw` signal (read from the router's config.yaml, mounted
# read-only, so the two can never disagree) or when the model called a tool
# CODE_ROUTE_TOOL_REGEX matches during it. Everything is recomputed from the
# request's history: no per-chat state, so both swap copies and a restart agree.
DEFAULT_CODE_ROUTE_COOLDOWN = 3
DEFAULT_CODE_ROUTE_ROUTER_CONFIG = "/app/semantic-router-config.yaml"
CODE_ROUTE_SIGNAL = "code_kw"


def parse_code_route_cooldown(raw: Any) -> Tuple[int, str]:
    """(cooldown, error). Unset / empty -> the default; a non-negative int is
    used as is (0: only the current exchange counts); anything else -> the
    default plus an error to log."""
    if raw is None or not str(raw).strip():
        return DEFAULT_CODE_ROUTE_COOLDOWN, ""
    try:
        value = int(str(raw).strip())
    except ValueError:
        value = -1
    if value < 0:
        return DEFAULT_CODE_ROUTE_COOLDOWN, (
            f"invalid CODE_ROUTE_COOLDOWN {raw!r}: expected an integer >= 0, using {DEFAULT_CODE_ROUTE_COOLDOWN}"
        )
    return value, ""


class CodeTextSignal:
    """The router's `code_kw` keyword rule, compiled for Python. Mirrors the
    Go router (pkg/classification/keyword_classifier_regex.go at v0.3.0):
    `method: regex` uses each keyword as a raw pattern, any other method
    matches it as a whole literal word; `case_sensitive: false` ignores case;
    `\\b` / `\\w` are ASCII as in RE2. Not a @dataclass -- see BackendState."""

    def __init__(self, operator: str, patterns: List["re.Pattern[str]"]) -> None:
        self.operator = operator
        self.patterns = patterns

    def matches(self, text: str) -> bool:
        if not text or not self.patterns:
            return False
        hits = [bool(p.search(text)) for p in self.patterns]
        if self.operator == "AND":
            return all(hits)
        if self.operator == "NOR":
            return not any(hits)
        return any(hits)


def compile_code_text_signal(rule: Any) -> "CodeTextSignal":
    """Compile one keyword rule ({name, operator, keywords, case_sensitive,
    method}) the way the Go router does. Raises ValueError on a bad rule."""
    if not isinstance(rule, dict) or not isinstance(rule.get("keywords"), list) or not rule["keywords"]:
        raise ValueError("keyword rule has no keywords")
    operator = str(rule.get("operator") or "OR").upper()
    if operator not in ("OR", "AND", "NOR"):
        raise ValueError(f"unsupported operator {operator!r}")
    flags = re.ASCII | (0 if rule.get("case_sensitive") else re.IGNORECASE)
    raw_regex = str(rule.get("method") or "").lower() == "regex"
    patterns = []
    for keyword in rule["keywords"]:
        if not isinstance(keyword, str) or not keyword:
            raise ValueError(f"bad keyword {keyword!r}")
        if raw_regex:
            source = keyword
        else:
            source = re.escape(keyword)
            if re.search(r"\w", keyword, re.ASCII):
                source = rf"\b{source}\b"
        try:
            patterns.append(re.compile(source, flags))
        except re.error as exc:
            raise ValueError(f"keyword {keyword!r} does not compile: {exc}") from exc
    return CodeTextSignal(operator, patterns)


def _find_keyword_rule(node: Any, name: str) -> Optional[dict]:
    if isinstance(node, dict):
        if node.get("name") == name and isinstance(node.get("keywords"), list):
            return node
        children = node.values()
    elif isinstance(node, list):
        children = node
    else:
        return None
    for child in children:
        found = _find_keyword_rule(child, name)
        if found is not None:
            return found
    return None


def load_code_text_signal(path: Optional[str], name: str = CODE_ROUTE_SIGNAL) -> Tuple[Optional[CodeTextSignal], str]:
    """(signal, error) from the router's config.yaml. Missing file, missing
    rule or a bad pattern -> (None, error): the cooldown then judges
    exchanges by tool calls only."""
    if not path:
        return None, "CODE_ROUTE_ROUTER_CONFIG is empty"
    try:
        import yaml  # LiteLLM's own dependency (the proxy loads config.yaml with it)

        with open(path, "r", encoding="utf-8") as fh:
            config = yaml.safe_load(fh)
        rule = _find_keyword_rule(config, name)
        if rule is None:
            return None, f"no keyword signal {name!r} in {path}"
        return compile_code_text_signal(rule), ""
    except Exception as exc:
        return None, f"cannot load the router's {name!r} signal from {path}: {exc}"


def message_text(content: Any) -> str:
    """The text of a chat message's content: a string, or the `text` parts of
    a multimodal list."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p["text"] for p in content
                         if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str))
    return ""


def split_exchanges(messages: Any) -> List[Tuple[str, List[dict]]]:
    """[(user text, the messages after it up to the next user message)], one
    per user message, oldest first. Anything before the first user message
    (system prompts, a compaction summary) belongs to no exchange."""
    out: List[Tuple[str, List[dict]]] = []
    if not isinstance(messages, list):
        return out
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "user":
            out.append((message_text(msg.get("content")), []))
        elif out:
            out[-1][1].append(msg)
    return out


def exchange_relevance(text: str, rounds: List[dict], tool_pattern: Any,
                       text_signal: Optional[CodeTextSignal]) -> Optional[str]:
    """Why one exchange counts as code, or None: a matching tool call during
    it (the most recent one is named) or its user message matching code_kw."""
    tool = code_tool_match(called_tool_names(rounds), tool_pattern)
    if tool is not None:
        return f"called {tool}"
    if text_signal is not None and text_signal.matches(text):
        return f"message matches {CODE_ROUTE_SIGNAL}"
    return None


def code_route_decision(messages: Any, tool_pattern: Any, text_signal: Optional[CodeTextSignal],
                        cooldown: int) -> Tuple[bool, str]:
    """(stay on / move to local-code?, reason). The current exchange (the last
    user message and the rounds so far) is relevant -> True. Otherwise count
    the irrelevant exchanges in a row before it: True while there was a
    relevant one and fewer than `cooldown` irrelevant ones since; False once
    the cooldown has run out (the reason says so) or when the chat has never
    looked like code (empty reason)."""
    exchanges = split_exchanges(messages)
    if not exchanges:
        return False, ""
    current = exchange_relevance(*exchanges[-1], tool_pattern, text_signal)
    if current is not None:
        return True, f"{current} in this exchange"
    irrelevant = 0
    for text, rounds in reversed(exchanges[:-1]):
        why = exchange_relevance(text, rounds, tool_pattern, text_signal)
        if why is not None:
            if irrelevant < cooldown:
                return True, f"{why} {irrelevant + 1} exchange(s) ago; {irrelevant}/{cooldown} irrelevant since"
            return False, f"cooldown: {irrelevant}/{cooldown} irrelevant exchanges since the last one that {why}"
        irrelevant += 1
    return False, ""


CODE_ROUTE_COOLDOWN, CODE_ROUTE_COOLDOWN_ERROR = parse_code_route_cooldown(os.environ.get("CODE_ROUTE_COOLDOWN"))
CODE_ROUTE_ROUTER_CONFIG = os.environ.get("CODE_ROUTE_ROUTER_CONFIG", DEFAULT_CODE_ROUTE_ROUTER_CONFIG)


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


# -- Tokenize ----------------------------------------------------------------
Tokenize = Callable[[str, Dict[str, Any]], Awaitable[Dict[str, Any]]]


class _HttpxTokenize:
    """POST a vLLM /tokenize request; return the JSON object or raise.

    The caller bounds the whole call with asyncio.wait_for -- httpx's own
    timeout is per read. No Authorization header: /tokenize sits outside
    vLLM's /v1 API-key guard. The response carries every token id (for a
    120k-token prompt, ~1 MB of JSON); there is no flag to omit them, which
    is one more reason it is only asked when the estimate says it matters."""

    def __init__(self, timeout: float) -> None:
        self._timeout = timeout
        self._client: Any = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    async def __call__(self, url: str, body: Dict[str, Any]) -> Dict[str, Any]:
        import httpx  # LiteLLM depends on it; imported lazily for the tests

        loop = asyncio.get_running_loop()
        if self._client is None or self._loop is not loop:
            self._client = httpx.AsyncClient(timeout=self._timeout)
            self._loop = loop
        resp = await self._client.post(url, json=body)
        if resp.status_code != 200:
            raise ProbeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        if not isinstance(data, dict):
            raise ProbeError("response is not a JSON object")
        return data


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
        tokenize: Optional[Tokenize] = None,
        tokenize_min_fraction: float = TOKENIZE_MIN_FRACTION,
        tokenize_chars_per_token: float = TOKENIZE_CHARS_PER_TOKEN,
        tokenize_timeout: float = TOKENIZE_TIMEOUT_S,
        code_route_regex: Any = "env",
        code_route_cooldown: Any = "env",
        code_text_signal: Any = "env",
    ) -> None:
        try:
            super().__init__()
        except Exception:  # pragma: no cover -- CustomLogger signature drift
            pass
        self.auto_footer = AUTO_FOOTER_ENABLED if auto_footer is None else bool(auto_footer)
        # "env" -> CODE_ROUTE_TOOL_REGEX as read at import; a str is compiled
        # here; a compiled pattern is used as is; None disables the remap.
        if isinstance(code_route_regex, str) and code_route_regex == "env":
            self.code_route_regex, route_error = CODE_ROUTE_TOOL_REGEX, CODE_ROUTE_TOOL_REGEX_ERROR
        elif isinstance(code_route_regex, str):
            self.code_route_regex, route_error = compile_code_route_regex(code_route_regex)
        else:
            self.code_route_regex, route_error = code_route_regex, ""
        if route_error:
            try:
                logger.warning("%s -- local-general -> local-code tool remap disabled", route_error)
            except Exception:
                pass
        # "env" -> CODE_ROUTE_COOLDOWN as read at import; an int is used as is.
        if isinstance(code_route_cooldown, str) and code_route_cooldown == "env":
            self.code_route_cooldown, cooldown_error = CODE_ROUTE_COOLDOWN, CODE_ROUTE_COOLDOWN_ERROR
        else:
            self.code_route_cooldown, cooldown_error = parse_code_route_cooldown(code_route_cooldown)
        # "env" -> the router's code_kw from CODE_ROUTE_ROUTER_CONFIG; a str is
        # a path to load; a CodeTextSignal is used as is; None = tool calls only.
        signal_error = ""
        if isinstance(code_text_signal, str):
            path = CODE_ROUTE_ROUTER_CONFIG if code_text_signal == "env" else code_text_signal
            self.code_text_signal, signal_error = load_code_text_signal(path)
        else:
            self.code_text_signal = code_text_signal
        for problem, consequence in ((cooldown_error, ""),
                                     (signal_error, " -- code cooldown judges exchanges by tool calls only")):
            if problem and self.code_route_regex is not None:
                try:
                    logger.warning("%s%s", problem, consequence)
                except Exception:
                    pass
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
        self.tokenize_min_fraction = tokenize_min_fraction
        self.tokenize_chars_per_token = tokenize_chars_per_token
        self.tokenize_timeout = tokenize_timeout
        self._tokenize: Tokenize = tokenize or _HttpxTokenize(tokenize_timeout)
        self._tokenize_cache: "OrderedDict[Tuple[str, str], Tuple[int, Optional[int]]]" = OrderedDict()
        self.stats: Dict[str, int] = {
            "spills": 0, "kept_all_overloaded": 0, "errors": 0, "poller_restarts": 0,
            "context_drops": 0, "tokenize_calls": 0, "tokenize_cache_hits": 0, "tokenize_errors": 0,
            "footers": 0, "footers_stripped": 0, "footer_errors": 0,
            "code_routes": 0, "code_route_cooldowns": 0, "code_route_errors": 0,
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
        # request_kwargs are the completion kwargs Router._acompletion was
        # called with (router.py:2757-2762 -> 10793 -> 10686 at v1.95.0): the
        # request's `tools`, `max_tokens`, `extra_body`, ... -- not yet merged
        # with the chosen deployment's litellm_params.
        try:
            return await self.filter_with_context(model, healthy_deployments, messages, request_kwargs)
        except Exception as exc:  # never fail a request because of this hook
            self.stats["errors"] += 1
            try:
                logger.warning("filter error for %s, deployments unchanged: %r", model, exc)
            except Exception:
                pass
            return healthy_deployments

    # -- `auto` footer hooks (+ the code-tool remap) -----------------------------
    # These three MUST stay defined in this class body. The proxy decides once
    # per callback list whether to call async_post_call_streaming_iterator_hook
    # and async_pre_call_hook by looking the name up in type(callback).__dict__
    # (proxy/utils.py:1718-1745 at v1.95.0); one inherited from a mixin or base
    # class would never run. async_post_call_success_hook is called on every
    # CustomLogger in litellm.callbacks (proxy/utils.py:2404-2410) and a
    # non-None return replaces the response. The footer parts are no-ops unless
    # the client asked for model `auto`, the remap is a no-op unless it asked
    # for local-general, and none of them ever raises.
    async def async_pre_call_hook(self, user_api_key_dict: Any, cache: Any, data: dict, call_type: Any) -> Any:
        """Two independent edits, each in its own try:

        * local-general with a code tool attached -> local-code (remap_code_tools);
        * `auto`: strip footers we emitted from incoming assistant messages, so
          the model never sees (and imitates) them and the router's signals
          never read them.

        Returns `data` (modified in place; proxy/utils.py:913-917 keeps a
        returned dict) when either changed it, else None."""
        changed = self.remap_code_tools(data)
        try:
            if not self.auto_footer or not isinstance(data, dict) or data.get("model") != AUTO_MODEL:
                return data if changed else None
            n = strip_footers_from_messages(data.get("messages"))
            if not n:
                return data if changed else None
            self.stats["footers_stripped"] += n
            return data
        except Exception as exc:
            self._footer_error("pre_call", exc)
            return data if changed else None

    def remap_code_tools(self, data: Any) -> bool:
        """local-general -> local-code while the chat's recent exchanges look
        like code (code_route_decision: a CODE_ROUTE_TOOL_REGEX tool call or a
        code_kw message, with a cooldown of CODE_ROUTE_COOLDOWN irrelevant
        exchanges). True when data["model"] was rewritten. Never raises: on
        any error `data` is left unchanged and the error is logged."""
        try:
            if self.code_route_regex is None or not isinstance(data, dict):
                return False
            if data.get("model") != CODE_ROUTE_FROM:
                return False
            remap, reason = code_route_decision(
                data.get("messages"), self.code_route_regex, self.code_text_signal, self.code_route_cooldown)
            if not remap:
                if reason:  # the chat WAS code; the cooldown has run out
                    self.stats["code_route_cooldowns"] += 1
                    self._log(CODE_ROUTE_FROM, "cooldown", "route", "[route] %s stays (%s)",
                              CODE_ROUTE_FROM, reason)
                return False
        except Exception as exc:
            try:
                self.stats["code_route_errors"] += 1
                logger.warning("[route] remap skipped after an error, request unchanged: %r", exc)
            except Exception:
                pass
            return False
        # The rewrite is the last thing that can fail, so an error above
        # always leaves the request exactly as it came in.
        data["model"] = CODE_ROUTE_TO
        self.stats["code_routes"] += 1
        try:
            kind = "current" if reason.endswith("in this exchange") else "earlier"
            self._log(CODE_ROUTE_FROM, kind, "route", "[route] %s -> %s (%s)",
                      CODE_ROUTE_FROM, CODE_ROUTE_TO, reason)
        except Exception:
            pass
        return True

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

    # -- Context fit, then the busy/down rule ------------------------------------
    async def filter_with_context(
        self,
        model: str,
        healthy_deployments: List,
        messages: Optional[List] = None,
        request_kwargs: Optional[dict] = None,
    ) -> List:
        """The whole per-request rule: drop too-long local deployments (hard),
        then apply the busy/down rule (filter_deployments) to what is left."""
        if not _is_chain(healthy_deployments) or not isinstance(messages, list) or not messages:
            return self.filter_deployments(model, healthy_deployments)
        self._learn(model, healthy_deployments)
        too_long = await self.context_check(model, healthy_deployments, messages, request_kwargs or {})
        if not too_long:
            return self.filter_deployments(model, healthy_deployments)

        fitting = [d for i, d in enumerate(healthy_deployments) if i not in too_long]
        why = ", ".join(f"{_label(healthy_deployments[i])}: {r}" for i, r in too_long.items())
        if not any(_api_base(d) is not None for d in fitting):
            if not fitting:
                # local-private: nothing unpolled to spill to. Hand LiteLLM the
                # list unchanged -- vLLM refuses the prompt with its own error
                # and it never leaves the box.
                self._log(model, "*", "none-fit",
                          "%s: no deployment fits the prompt (%s); list unchanged -- the backend will refuse it",
                          model, why)
                return healthy_deployments
            # Past every local window: only the Claude deployment(s) can take it.
            self.stats["context_drops"] += 1
            self._log(model, "*", "none-fit-local",
                      "%s: no local deployment fits the prompt (%s); keeping only %s",
                      model, why, ", ".join(_label(d) for d in fitting))
            return fitting

        self.stats["context_drops"] += 1
        result = self.filter_deployments(model, fitting)
        next_order = min((o for o in (_order(d) for d in result) if o is not None), default=None)
        for i, reason in too_long.items():
            d = healthy_deployments[i]
            self._log(model, _label(d), TOO_LONG, "%s: dropping %s (%s, %s) -> next order %s",
                      model, _label(d), TOO_LONG, reason, next_order)
        return result

    async def context_check(
        self, model: str, deployments: List, messages: List, request_kwargs: dict
    ) -> Dict[int, str]:
        """{index in `deployments`: why} for every local deployment the prompt
        does not fit. Deployments that cannot be measured are never listed."""
        tools = request_tools(request_kwargs)
        estimate: Optional[int] = None
        pending: Dict[Tuple[str, str], Tuple[str, Dict[str, Any]]] = {}
        candidates: List[Tuple[int, Tuple[str, str], int, int]] = []
        for i, d in enumerate(deployments):
            base = _api_base(d)
            limit = max_input_tokens(d)
            if base is None or limit is None:
                continue  # Claude, or a local deployment with no declared window
            if estimate is None:
                estimate = estimate_tokens(messages, tools, self.tokenize_chars_per_token)
            if estimate < self.tokenize_min_fraction * limit:
                continue  # cannot be close: no /tokenize call
            url = tokenize_url(base)
            body = tokenize_body(d, messages, tools, request_kwargs)
            key = tokenize_cache_key(url, body)
            pending.setdefault(key, (url, body))
            candidates.append((i, key, limit, max_output_tokens(request_kwargs, d)))
        if not candidates:
            return {}
        keys = list(pending)
        counted = await asyncio.gather(*(self._count(model, k, *pending[k]) for k in keys))
        results = dict(zip(keys, counted))
        out: Dict[int, str] = {}
        for i, key, limit, max_out in candidates:
            res = results.get(key)
            if res is None:
                continue  # tokenize failed: keep it, LiteLLM decides as before
            reason = context_verdict(res[0], limit, res[1], max_out)
            if reason is not None:
                out[i] = reason
        return out

    async def _count(
        self, model: str, key: Tuple[str, str], url: str, body: Dict[str, Any]
    ) -> Optional[Tuple[int, Optional[int]]]:
        """(count, max_model_len) from the cache or one bounded /tokenize call;
        None on any failure."""
        hit = self._tokenize_cache.get(key)
        if hit is not None:
            self._tokenize_cache.move_to_end(key)
            self.stats["tokenize_cache_hits"] += 1
            return hit
        self.stats["tokenize_calls"] += 1
        try:
            data = await asyncio.wait_for(self._tokenize(url, body), timeout=self.tokenize_timeout)
            count = data.get("count") if isinstance(data, dict) else None
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ProbeError("no integer `count` in the response")
            result = (count, _positive_int(data.get("max_model_len")))
        except Exception as exc:
            self.stats["tokenize_errors"] += 1
            self._log(model, url, "tokenize-failed",
                      "%s: /tokenize at %s failed (%s); keeping that backend's deployment(s)",
                      model, url, f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__)
            return None
        self._tokenize_cache[key] = result
        while len(self._tokenize_cache) > TOKENIZE_CACHE_SIZE:
            self._tokenize_cache.popitem(last=False)
        return result

    # -- Core busy/down rule (sync, so it is trivially testable) -----------------
    def _learn(self, model: str, deployments: List) -> None:
        """Register every local backend of a chain with the poller."""
        bases: List[str] = []
        for d in deployments:
            base = _api_base(d)
            if base is not None:
                bases.append(base)
                if base not in self._backends:
                    self._backends[base] = BackendState(api_base=base, url=metrics_url(base))
        if isinstance(model, str) and bases:
            # Union: a cooled-down deployment missing from this call is still in the chain.
            self._chain_backends[model] = tuple(dict.fromkeys(self._chain_backends.get(model, ()) + tuple(bases)))
        self._ensure_poller()

    def filter_deployments(self, model: str, healthy_deployments: List) -> List:
        if not _is_chain(healthy_deployments):
            return healthy_deployments  # not a chain: leave plain groups alone
        self._learn(model, healthy_deployments)

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
                "tokenize_min_fraction": self.tokenize_min_fraction,
                "tokenize_chars_per_token": self.tokenize_chars_per_token,
                "tokenize_timeout_s": self.tokenize_timeout,
            },
            "tokenize_cache_entries": len(self._tokenize_cache),
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
